"""Linux原生环境的跨期Live离线验收；假行情/柜台，不连接网络。"""

from argparse import Namespace
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import date
from decimal import Decimal
from importlib import import_module
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import time
import unittest
from unittest.mock import patch

from bomber.framework.dataprep.contracts import InstrumentSpec
from bomber.framework.datahub.sector_roles import SectorDataUnavailable
from bomber.framework.dataprep.live_references import LiveFuturesReferences
from bomber.framework.dataprep.sources import ReferenceBatch, ReferenceDataset
from bomber.framework.market.basic.base import InstrumentId, InstrumentMeta, MarketDataFeed, make_bar
from bomber.framework.market.stream.health import MarketHealthState, MarketHealthReason, MarketHealthSnapshot
from bomber.framework.trader.runtime.live.recovery import restored_target_revision
from bomber.framework.trader.execution.ctp import CtpTdApiTransport, CtpNativeTraderDriver
from tests.run_p4_ctp_td_transport import FakeTdApi

runtime = import_module("demos.03_calendar_spread.live_runtime")
entry = import_module("demos.03_calendar_spread.run_live")
reference_module = import_module("demos.03_calendar_spread.live_references")
recovery = import_module("demos.03_calendar_spread.recovery")
strategy_module = import_module("demos.03_calendar_spread.strategy")
MINUTE = runtime.MINUTE
NOW = 1_791_511_200_000_000_000


class Feed(MarketDataFeed):
    latest_trading_day = "20261009"

    @property
    def health_snapshot(self):
        return MarketHealthSnapshot(self.source_id, MarketHealthState.READY, MarketHealthReason.HEALTHY,
            "offline fixture", 1, NOW, NOW, NOW, NOW, 0, 0, 0, 0)

    @property
    def latest_receive_monotonic_ns(self):
        return time.monotonic_ns()

    def _on_subscription_added(self, request):
        pass

    def _on_subscription_removed(self, request):
        pass

    def connect(self):
        self._is_connected = True

    def disconnect(self):
        self._is_connected = False


class References:
    roles = ("secondary", "far")
    product = "RB"
    trading_day = date(2026, 10, 9)
    publication_lock = None
    refresh_on_event = False
    paths = ()
    factor_date = None

    def __init__(self):
        self.specs = {role: InstrumentSpec("future", symbol, "RB", "SHFE", "CNY",
            date(2026, 1, 1), date(2027, 10, 1), Decimal(1), Decimal(10))
            for role, symbol in zip(self.roles, ("rb2701", "rb2705"))}
        self.instrument_ids = {r: InstrumentId.from_str(s.symbol + ".SHFE") for r, s in self.specs.items()}
        self.spec = tuple(self.specs.items())
        self.manifest = {"fingerprint": "fixed-calendar-fixture", "ready": True}

    def validate(self):
        pass

    def assert_expected(self, expected):
        if expected and expected != {r: str(i) for r, i in self.instrument_ids.items()}:
            raise ValueError("fixture预期不一致")

    def refresh(self):
        pass

    def snapshot(self, now):
        return NS(source_day=date(2026, 10, 8))

    def instrument_meta(self, role):
        return InstrumentMeta(self.instrument_ids[role], price_precision=0, size_precision=0,
            price_increment=Decimal(1), multiplier=Decimal(10), exchange="SHFE")


class CalendarTdApi(FakeTdApi):
    def __init__(self):
        super().__init__()
        self.sent = []

    def reqUserLogin(self, data, reqid):
        self.onRspUserLogin({"BrokerID": "9999", "UserID": "demo", "TradingDay": "20261009",
            "MaxOrderRef": "17", "FrontID": 1, "SessionID": 2}, {}, reqid, True)
        return 0

    def reqQryInvestorPosition(self, data, reqid):
        self.onRspQryInvestorPosition({}, {}, reqid, True)
        return 0

    def reqQryOrder(self, data, reqid):
        self.onRspQryOrder({}, {}, reqid, True)
        return 0

    def reqOrderInsert(self, data, reqid):
        self.sent.append(dict(data))
        return 0


class RejectTdApi(CalendarTdApi):
    def reqOrderInsert(self, data, reqid):
        super().reqOrderInsert(data, reqid)
        self.onRspOrderInsert(data, {"ErrorID": 42, "ErrorMsg": "fixture reject"}, reqid, True)
        return 0


class LiveAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.now = NOW
        self.sessions = []
        self.addCleanup(lambda: [s.runner.stop() for s in self.sessions])

    def build(self, mode="model", *, start=True, driver=None, **changes):
        shared_service, strategy_id = changes.pop("shared_service", None), changes.pop("strategy_id", None)
        args = Namespace(mode=mode, leg1_role="secondary", leg2_role="far", quantity=Decimal(1),
            lookback=2, entry_z=2., exit_z=.5, rebalance_interval=1,
            simnow_environment="realtime", replay_md_trading_day=None, expected_instruments={},
            instrument_max_age_seconds=10, max_notional=Decimal(1000000),
            limit_offset_ticks=1, state_file=self.path, account_reconciliation=None, strategy_position_source=None)
        vars(args).update(changes)
        session = runtime.assemble(args, References(), driver, Feed("calendar-fixture"), clock_ns=lambda: self.now,
                                   shared_service=shared_service, strategy_id=strategy_id)
        self.sessions.append(session)
        # 同步帧用例不声称核验真实深度；单独用例保留真实公共门控。
        session.runner.instrument_health = None
        session.readiness.channels[0].instrument_health = None
        if start:
            session.runner.start()
            session.runner.begin_bars()
        return session

    def bar(self, session, role, minute, price):
        stamp = NOW + (minute + 2) * MINUTE - 1
        self.now = max(self.now, stamp + 1_000_000_000)
        return make_bar(instrument_id=session.references.instrument_ids[role],
            meta=session.references.instrument_meta(role), bar_type="1-MINUTE",
            open=price, high=price, low=price, close=price, volume=1, ts_event=stamp, ts_init=stamp)

    def frame(self, session, minute, spread):
        for role, price in (("secondary", 100 + spread), ("far", 100)):
            session.runner.publish("ctp-bars", self.bar(session, role, minute, price))

    def warm_and_enter(self, session):
        for minute, spread in enumerate((0, 2, 4)):
            self.frame(session, minute, spread)

    def test_recording_history_hook_and_buffer_do_not_submit_until_next_live_frame(self):
        from bomber.framework.dataprep.history import HistoryService, HistoryBar
        from tests.session_fixtures import declared_day_sessions
        history = import_module("demos.03_calendar_spread.history")
        calendar = declared_day_sessions("RB", date(2026, 10, 9), start="09:00", end="11:30")
        class Factory:
            @staticmethod
            def create_for(*args):
                class Provider:
                    def open(self): pass
                    def close(self): pass
                    def read(self, request):
                        return tuple(HistoryBar(request.instrument_id, stamp,
                            close=100 + index if request.instrument_id.startswith("rb2701") else 100)
                            for index, stamp in enumerate(calendar.last_minutes(request.end_ns, 2)))
                return Provider()
        coordinator = history.CalendarHistory(HistoryService(database_config={}, factory=Factory,
            missing_policy="allow"), calendar, 2)
        with patch.object(history, "build_history", return_value=coordinator):
            s = self.build(mode="recording", start=False, history_minutes=2, history_config=None,
                history_db="dolphindb", history_file=None, history_stage="recovery", history_missing_policy="fail",
                reference_timeout=15, _session_calendar=calendar)
        s.runner.start()
        s.runner.mark_history_live_start()
        s.runner.prepare_history()
        self.assertEqual(s.strategy.complete_frames, 2)
        self.assertEqual(len(s.client.requests), 0)
        # 查询后的完整实时帧先缓冲为预热，不重放其中的历史交易目标。
        for role, price in (("secondary", 103), ("far", 100)):
            s.runner.publish("ctp-bars", self.bar(s, role, -1, price))
        s.runner.begin_bars()
        self.assertEqual(s.strategy.complete_frames, 3)
        self.assertEqual(len(s.client.requests), 0)
        self.assertFalse(s.runner.history_buffering)
        self.frame(s, 0, 10)
        self.assertEqual(s.strategy.complete_frames, 4)
        self.assertEqual(len(s.client.requests), 1)

    def test_failed_history_local_lifecycle_keeps_decisions_closed(self):
        from bomber.framework.dataprep.history import HistoryUnavailable
        s = self.build(mode="recording", start=False)
        s.runner.mark_history_live_start = lambda: None
        s.runner.prepare_history = lambda: (_ for _ in ()).throw(HistoryUnavailable("missing leg"))
        with patch.dict("os.environ", {"CTP_MD_ADDRESS": "tcp://fixture:1",
                "CTP_ACCOUNT_ID": "fixture", "CTP_PASSWORD": "fixture"}, clear=True):
            lifecycle = recovery.CalendarLocalLifecycle(mode="recording", expected_trading_day="20261009",
                environment="realtime", replay_md_trading_day=None, resume=False, state_file=None, market_source="ctp")
            with self.assertRaises(HistoryUnavailable):
                lifecycle.start(s, NS())
        self.assertFalse(s.runner.accept_bars)
        self.assertEqual(s.client.requests, ())

    def test_model_shared_signal_and_equal_opposite_attributed_fills(self):
        s = self.build()
        self.warm_and_enter(s)
        self.assertEqual(s.strategy.last_z, 3)
        self.assertEqual(s.strategy.direction, -1)
        self.assertEqual([s.strategy.position(str(i)) for i in s.runner.instruments.values()], [-1, 1])
        self.assertEqual(s.strategy.fills_received, 2)
        self.assertTrue(self.path.is_file())
        self.frame(s, 3, 0)
        self.assertEqual(s.strategy.direction, 1)
        self.assertEqual([s.strategy.position(str(i)) for i in s.runner.instruments.values()], [1, -1])

    def test_shared_calendar_assembly_uses_declared_strategy_and_account_routes(self):
        from bomber.framework.trader.execution.builders import build_model_execution
        from bomber.framework.trader.execution.shared import SharedAccountExecutionService, SharedRiskBudget
        from bomber.framework.trader.execution.risk import RiskLimits
        references = References()
        limits = {i: RiskLimits(max_order_quantity=2, max_abs_position=2, contract_multiplier=Decimal(10))
                  for i in references.instrument_ids.values()}
        execution = build_model_execution("shared-calendar-account", instrument_limits=limits)
        service = SharedAccountExecutionService(execution.client, prices=execution.prices,
            instrument_limits=limits, budget=SharedRiskBudget(4, 4000000), clock_ns=lambda: self.now)
        driver = NS(trading_day="20261009", is_simnow_session=True, _connected=True)
        session = self.build("simnow", start=False, driver=driver, shared_service=service,
                             strategy_id="calendar-independent-id")
        self.assertEqual(session.strategy.strategy_id, "calendar-independent-id")
        self.assertEqual(session.client.client_id, "shared-calendar-account")
        self.assertIs(session.runner.position_manager, service.position_manager)
        self.assertIs(session.execution.prices, service.prices)
        self.assertIsNone(session.manager)
        self.assertFalse(self.path.exists())
        self.assertEqual(session.client.routes, {str(i): i for i in references.instrument_ids.values()})
        first, second = tuple(references.instrument_ids.values())
        service.position_manager.set_strategy_position(session.strategy.strategy_id, str(first), 1)
        service.position_manager.set_strategy_position(session.strategy.strategy_id, str(second), -1)
        self.assertEqual(runtime.restore_direction(session), 1)
        service.position_manager.set_strategy_position(session.strategy.strategy_id, str(second), 0)
        with self.assertRaisesRegex(RuntimeError, "不平衡"):
            runtime.restore_direction(session)

    def test_recording_records_targets_without_fills_or_state(self):
        s = self.build("recording", state_file=None)
        self.warm_and_enter(s)
        self.assertEqual(len(s.client.requests), 1)
        self.assertEqual(s.strategy.fills_received, 0)
        self.assertIsNone(s.manager)
        self.assertEqual([s.strategy.position(str(i)) for i in s.runner.instruments.values()], [0, 0])

    def test_interleaved_minutes_preserve_slow_leg_previous_bar(self):
        s = self.build("recording", state_file=None)
        for role, minute, price in (("secondary", 0, 100), ("secondary", 1, 102),
                                    ("far", 0, 100), ("far", 1, 100)):
            s.runner.publish("ctp-bars", self.bar(s, role, minute, price))
        self.assertEqual(s.strategy.complete_frames, 2)
        self.assertEqual(list(s.strategy.signal.history), [0, 2])

    def test_missing_leg_and_duplicate_do_not_advance_signal(self):
        s = self.build("recording", state_file=None)
        bar = self.bar(s, "secondary", 0, 100)
        s.runner.publish("ctp-bars", bar)
        s.runner.publish("ctp-bars", bar)
        self.assertEqual(s.strategy.complete_frames, 0)
        s.runner.publish("ctp-bars", self.bar(s, "far", 0, 100))
        s.runner.publish("ctp-bars", bar)
        self.assertEqual(s.strategy.complete_frames, 1)

    def test_changed_references_close_gate_before_targets(self):
        s = self.build()
        s.references.manifest = {**s.references.manifest, "fingerprint": "new-role-version"}
        with self.assertRaisesRegex(RuntimeError, "失效"):
            self.frame(s, 0, 0)
        self.assertFalse(s.runner.accept_bars)
        self.assertFalse(s.runner.target_store.all())

    def test_real_instrument_gate_requires_both_depths(self):
        from bomber.framework.trader.runtime.live.channels.ctp import InstrumentHealthGate
        s = self.build()
        s.readiness.channels[0].instrument_health = InstrumentHealthGate(
            s.upstream, s.runner.fixed_ids, "20261009", clock_ns=lambda: self.now)
        s.upstream.depth_observations = {}
        self.assertFalse(s.readiness())
        self.assertEqual(len(s.readiness.last_result.issues), 2)

    def test_future_bar_fails_closed(self):
        s = self.build()
        bar = self.bar(s, "secondary", 0, 100)
        self.now = bar.ts_event - 1
        with self.assertRaisesRegex(RuntimeError, "未来"):
            s.runner.publish("ctp-bars", bar)
        self.assertFalse(s.runner.accept_bars)

    def test_restore_keeps_positions_and_revision_but_does_not_send_old_target(self):
        s = self.build()
        self.warm_and_enter(s)
        s.manager.save()
        revision = s.runner.target_store.get(s.strategy.strategy_id).revision
        s.runner.stop()
        restored = self.build(start=False)
        restored.manager.restore()
        restored.strategy.restore_target_revision(restored_target_revision(restored, restored.recovery_binding))
        restored.runner.start()
        restored.runner.begin_bars()
        before = len(restored.execution.backend.reports)
        self.assertEqual(restored.strategy.direction, -1)
        self.assertIsNone(restored.strategy.last_targets)
        for minute, spread in ((5, 0), (6, 2)):
            self.frame(restored, minute, spread)
        self.assertEqual(len(restored.execution.backend.reports), before)
        # z=0.5，重预热后首次零目标也必须提交，平掉保留的旧仓位。
        self.frame(restored, 7, 1)
        target = restored.runner.target_store.get(restored.strategy.strategy_id)
        self.assertEqual(target.revision, revision + 1)
        self.assertEqual(set(target.targets.values()), {Decimal(0)})

    def test_restore_changed_config_rejects_and_preserves_original_file(self):
        s = self.build()
        self.warm_and_enter(s)
        s.manager.save()
        original = self.path.read_bytes()
        changed = self.build(start=False, lookback=3)
        with self.assertRaises(Exception):
            changed.manager.restore()
        self.assertEqual(self.path.read_bytes(), original)

    def test_restored_unbalanced_target_is_rejected(self):
        s = self.build("recording", state_file=None)
        self.warm_and_enter(s)
        target = s.runner.target_store.get(s.strategy.strategy_id)
        s.runner.target_store.restore({s.strategy.strategy_id: replace(target,
            targets={str(i): Decimal(1) for i in s.runner.fixed_ids})})
        with self.assertRaisesRegex(RuntimeError, "反向双腿"):
            runtime.restore_direction(s)

    def test_fills_requirement_is_explicit(self):
        s = self.build("recording", state_file=None)
        self.warm_and_enter(s)
        control = recovery.CalendarControl()
        control.resuming = False
        control.verify(s)
        control.require_fills = True
        with self.assertRaisesRegex(RuntimeError, "归属成交"):
            control.verify(s)

    def test_empty_model_checkpoint_resumes_and_can_publish_first_signal(self):
        original = self.build()
        original.manager.save()
        original.runner.stop()
        restored = self.build(start=False)
        restored.manager.restore()
        revision = restored_target_revision(restored, restored.recovery_binding, allow_empty=True)
        self.assertEqual(revision, 0)
        restored.strategy.restore_target_revision(revision)
        restored.runner.start()
        restored.runner.begin_bars()
        self.warm_and_enter(restored)
        self.assertEqual(restored.runner.target_store.get(restored.strategy.strategy_id).revision, 1)
        self.assertEqual(restored.strategy.fills_received, 2)

    def test_require_fills_rejects_single_filled_leg(self):
        s, transport = self.native()
        self.warm_and_enter(s)
        self.fill(transport, 0)
        control = recovery.CalendarControl()
        control.resuming, control.require_fills = False, True
        with self.assertRaisesRegex(RuntimeError, "双腿目标未完成"):
            control.verify(s)

    def test_require_fills_rejects_working_quantity_after_positions_match(self):
        s = self.build()
        self.warm_and_enter(s)
        instrument = next(iter(s.runner.fixed_ids))
        s.runner.position_manager.adjust_working_quantity(runtime.CLIENT_ID, instrument, Decimal(1))
        control = recovery.CalendarControl()
        control.resuming, control.require_fills = False, True
        with self.assertRaisesRegex(RuntimeError, "双腿目标未完成"):
            control.verify(s)

    def native(self, api=CalendarTdApi):
        transport = CtpTdApiTransport(client_id=runtime.CLIENT_ID, account_id="demo", front="tcp://fake:1234",
            broker_id="9999", investor_id="demo", password="fake", app_id="fake", auth_code="fake",
            td_api_base=api, flow_path=str(Path(self.temp.name) / "td"), timeout_seconds=.05)
        holder = {}

        def disconnected(reason):
            session = holder.get("session")
            if session is None:
                raise RuntimeError(f"假CTP装配期间断线: {reason}")
            session.runner.close_role_gate(reason)

        driver = CtpNativeTraderDriver(runtime.CLIENT_ID, "demo", transport,
            enable_simnow_orders=True, max_session_orders=20, disconnect_handler=disconnected)
        self.addCleanup(driver.stop)
        session = self.build("simnow", driver=driver, start=False)
        holder["session"] = session
        session.runner.start()
        session.runner.begin_bars()
        session.client.arm_demo(session.client.DEMO_CONFIRMATION)
        return session, transport

    def fill(self, transport, index):
        fields = transport._api.sent[index]
        raw = {"BrokerID": "9999", "InvestorID": "demo", "InstrumentID": fields["InstrumentID"],
            "ExchangeID": fields["ExchangeID"], "OrderRef": fields["OrderRef"], "OrderSysID": f"SYS{index}",
            "FrontID": 1, "SessionID": 2}
        transport._api.onRtnOrder({**raw, "OrderStatus": "3"})
        transport._api.onRtnTrade({**raw, "TradeID": f"T{index}", "Volume": fields["VolumeTotalOriginal"],
            "Price": fields["LimitPrice"]})

    def test_native_acceptance_does_not_count_as_fill_then_actual_callbacks_update_both_legs(self):
        s, transport = self.native()
        self.warm_and_enter(s)
        self.assertEqual(len(transport._api.sent), 2)
        self.assertEqual(s.strategy.fills_received, 0)
        self.assertEqual({r["Direction"] for r in transport._api.sent}, {"0", "1"})
        for i in range(2):
            self.fill(transport, i)
        self.assertEqual(s.strategy.fills_received, 2)
        self.assertEqual([s.strategy.position(str(i)) for i in s.runner.instruments.values()], [-1, 1])

    def test_changed_native_target_cancels_old_orders_and_waits_for_callbacks(self):
        for role in ("secondary", "far"):
            with self.subTest(filled_role=role):
                self.now = NOW
                self.path = Path(self.temp.name) / f"cancel-{role}.json"
                self.check_changed_native_target_cancels_old_orders(role)

    def check_changed_native_target_cancels_old_orders(self, filled_role):
        s, transport = self.native()
        self.warm_and_enter(s)
        api = transport._api
        for index, fields in enumerate(api.sent):
            api.onRtnOrder({"BrokerID": "9999", "InvestorID": "demo",
                "InstrumentID": fields["InstrumentID"], "ExchangeID": fields["ExchangeID"],
                "OrderRef": fields["OrderRef"], "OrderSysID": f"SYS{index}",
                "FrontID": 1, "SessionID": 2, "OrderStatus": "3"})
        previous = s.runner.target_store.get(s.strategy.strategy_id)
        previous = replace(previous, revision=previous.revision + 1)
        s.runner.submit(previous)
        self.assertEqual(api.request_names.count("cancel"), 0)
        zero = replace(previous, revision=previous.revision + 1,
            targets={key: Decimal(0) for key in previous.targets})
        s.runner.submit(zero)
        self.assertEqual(api.request_names.count("cancel"), 2)
        self.assertEqual(len(api.sent), 2)
        self.assertEqual([s.runner.position_manager.working_quantity(runtime.CLIENT_ID, i)
            for i in s.runner.instruments.values()], [-1, 1])
        # 撤单在途期间先到成交：仍须归属记账，不能把撤单请求当作清零。
        filled_instrument = str(s.runner.instruments[filled_role])
        fill_index = next(index for index, row in enumerate(api.sent)
            if f"{row['InstrumentID']}.{row['ExchangeID']}" == filled_instrument)
        cancel_index = 1 - fill_index
        self.fill(transport, fill_index)
        fields = api.sent[cancel_index]
        api.onRtnOrder({"BrokerID": "9999", "InvestorID": "demo",
            "InstrumentID": fields["InstrumentID"], "ExchangeID": fields["ExchangeID"],
            "OrderRef": fields["OrderRef"], "OrderSysID": f"SYS{cancel_index}", "FrontID": 1,
            "SessionID": 2, "OrderStatus": "5", "VolumeTraded": 0})
        expected = [-1, 0] if filled_role == "secondary" else [0, 1]
        self.assertEqual([s.strategy.position(str(i)) for i in s.runner.instruments.values()], expected)
        self.assertEqual([s.runner.position_manager.working_quantity(runtime.CLIENT_ID, i)
            for i in s.runner.instruments.values()], [0, 0])
        s.runner.submit(replace(zero, revision=zero.revision + 1))
        self.assertEqual(api.request_names.count("cancel"), 2)
        self.assertEqual(len(api.sent), 3)
        self.assertEqual(f"{api.sent[-1]['InstrumentID']}.{api.sent[-1]['ExchangeID']}", filled_instrument)
        self.assertEqual(api.sent[-1]["Direction"], "0" if filled_role == "secondary" else "1")
        self.assertEqual(api.sent[-1]["CombOffsetFlag"], "3")
        self.assertFalse(s.client.report_errors)
        with self.assertRaisesRegex(RuntimeError, "ctp_front_disconnected"):
            s.driver.on_disconnect(4097)
        self.assertFalse(s.runner.accept_bars)
        self.assertIn("ctp_front_disconnected", s.runner.failure)

    def test_native_first_leg_rejection_stops_second_leg(self):
        s, transport = self.native(RejectTdApi)
        with self.assertRaises(RuntimeError):
            self.warm_and_enter(s)
        self.assertEqual(len(transport._api.sent), 1)
        self.assertFalse(s.runner.accept_bars)
        self.assertIsNotNone(s.client.portfolio_failure)


class CliTests(unittest.TestCase):
    common = ["--connect", "--product", "RB", "--expected-trading-day", "20261009",
              "--expected-source-day", "20261008"]

    def test_im_recording_history_cli_uses_completed_cffex_source(self):
        config = Path(__file__).resolve().parents[1] / "demos/03_calendar_spread/history_im.json"
        args = entry.parse_args(["--connect", "--product", "IM", "--expected-trading-day", "20261016",
            "--expected-source-day", "20261015", "--history-config", str(config), "--history-minutes", "120",
            "--calendar-id", "china.cffex", "--no-session-overrides"])
        self.assertEqual(args.history_minutes, 120)
        with patch.dict("os.environ", {"CTP_MD_ADDRESS": "tcp://fixture:1", "CTP_ACCOUNT_ID": "fixture",
                "CTP_PASSWORD": "fixture"}, clear=True), patch.object(entry, "build_simnow_transport",
                side_effect=AssertionError("Recording不应构造TD")):
            managed = entry.build_runtime(args)
        self.assertIsNone(managed.controller.driver)

    def test_defaults_roles_and_recording_no_td_policy(self):
        args = entry.parse_args(self.common)
        self.assertEqual((args.mode, args.leg1_role, args.leg2_role), ("recording", "secondary", "far"))
        self.assertEqual(args.execution_policy.account_reconciliation.value, "off")

    def test_history_configuration_requires_calendar_and_accepts_completed_au_source(self):
        config = Path(__file__).resolve().parents[1] / "demos/03_calendar_spread/history_au.json"
        argv = self.common + ["--history-config", str(config), "--history-minutes", "120",
            "--calendar-id", "china.sse", "--no-session-overrides"]
        args = entry.parse_args(argv)
        self.assertEqual(args.history_minutes, 120)
        self.assertEqual(args.history_missing_policy, "fail")
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            entry.parse_args(self.common + ["--history-minutes", "-1"])

    def test_invalid_roles_dates_quantities_and_authorization(self):
        cases = [["--leg2-role", "secondary"], ["--quantity", "1.5"], ["--entry-z", "nan"],
            ["--exit-z", "2"], ["--seconds", "inf"], ["--require-fills"], ["--enable-orders"],
            ["--expected-source-day", "20261009"], ["--simnow-environment", "replay"],
            ["--expected-instrument", "secondary=rb2701.SHFE"], ["--account-reconciliation", "strict"]]
        for extra in cases:
            with self.subTest(extra=extra), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                entry.parse_args(self.common + extra)

    def test_new_and_resume_state_and_native_authorization(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            model = entry.parse_args(self.common + ["--mode", "model", "--state-file", str(path)])
            self.assertEqual(model.execution_policy.position_source.value, "model")
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                entry.parse_args(self.common + ["--mode", "simnow", "--state-file", str(path)])
            native = entry.parse_args(self.common + ["--mode", "simnow", "--state-file", str(path),
                "--enable-orders", "--confirm-simnow"])
            self.assertEqual(native.execution_policy.position_source.value, "fills")
            path.write_text("existing")
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                entry.parse_args(self.common + ["--mode", "model", "--state-file", str(path)])
            self.assertTrue(entry.parse_args(self.common + ["--mode", "model", "--state-file", str(path), "--resume"]).resume)

    def test_recording_runtime_construction_requires_no_td_credentials_or_connection(self):
        args = entry.parse_args(self.common)
        with patch.dict("os.environ", {"CTP_MD_ADDRESS": "tcp://fixture:1", "CTP_ACCOUNT_ID": "fixture",
                "CTP_PASSWORD": "fixture"}, clear=True), patch.object(entry, "build_simnow_transport",
                side_effect=AssertionError("Recording构造了TD")):
            result = entry.build_runtime(args)
        self.assertIsNone(result.controller.driver)
        self.assertIsNone(result.session)


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.roles = dict(trade_date=date(2026, 10, 8), code="RB", second="rb2701", far="rb2705")
        self.basics = [dict(symbol=symbol, code="RB", exchangeCD="SHFE", currencyCD="CNY",
            contMultNum=10, minChgPriceNum=1, listDate=date(2026, 1, 1), lastTradeDate=date(2027, 10, 1))
            for symbol in ("rb2701", "rb2705")]

    def references(self):
        def batch(dataset, rows):
            return ReferenceBatch(dataset, tuple(rows), tuple(rows[0]), "calendar-fixture")
        source = NS(contract_structure=lambda query: batch(ReferenceDataset.CONTRACT_STRUCTURE, [self.roles]),
            futures_basic=lambda query: batch(ReferenceDataset.FUTURES_BASIC, self.basics),
            adjustment_factors=lambda query: self.fail("跨期不得请求因子"))
        service = LiveFuturesReferences(source, products=("RB",), trading_day="20261009", started_ns=self.now,
            roles=("secondary", "far"), factor_roles=(), factor_date_basis="source",
            factor_availability="source-day-end", refresh_seconds=0, clock_ns=lambda: self.now)
        return reference_module.CalendarReferences(service, "RB", ("secondary", "far"), clock_ns=lambda: self.now)

    def test_requested_roles_only_and_no_factors(self):
        refs = self.references()
        self.assertEqual(set(refs.instrument_ids), {"secondary", "far"})
        self.assertFalse(refs.manifest["adjustment_factors_used"])
        self.assertNotIn("factor", " ".join(refs.manifest["sources"]))
        self.assertEqual(refs.snapshot(self.now).instrument("RB", "secondary"), "rb2701")

    def test_duplicate_legs_rejected(self):
        self.roles["far"] = "rb2701"
        with self.assertRaisesRegex(ValueError, "不同真实合约"):
            self.references()

    def test_future_publication_and_expired_cache_fail_closed(self):
        self.roles["available_ns"] = self.now + 1
        with self.assertRaisesRegex(SectorDataUnavailable, "尚未生效或发布"):
            self.references()
        del self.roles["available_ns"]
        refs = self.references()
        self.now += 31_000_000_000
        with self.assertRaisesRegex(RuntimeError, "过期"):
            refs.snapshot(self.now)

    def test_event_snapshot_does_not_query_source(self):
        refs = self.references()
        with patch.object(refs.service, "snapshot", side_effect=AssertionError("行情线程查询数据库")):
            refs.snapshot(self.now)
            refs.spec

    def test_native_lifecycle_snapshot_handles_actual_no_factor_references(self):
        refs = self.references()
        transport = CtpTdApiTransport(client_id=runtime.CLIENT_ID, account_id="demo", front="tcp://fake:1234",
            broker_id="9999", investor_id="demo", password="fake", app_id="fake", auth_code="fake",
            td_api_base=CalendarTdApi, timeout_seconds=.05)
        lifecycle = recovery.CalendarCtpLifecycle(transport, md_front="tcp://fake:1234", orders=True,
            state_file=Path("unused-calendar-fixture.json"), expected_trading_day="20261009",
            scope_to_reference_instruments=True)
        self.addCleanup(lifecycle.driver.stop)
        session = NS(runner=NS(expected_day="20261009", bound_instrument=None,
                instruments=refs.instrument_ids, fixed_ids=frozenset(refs.instrument_ids.values()),
                pending_frames={}, expired_frame_count=0), client=NS(audit_records=[]), bar_feed=NS(timing_snapshot={}))
        for current in (None, session):
            with self.subTest(session=current is not None):
                fields = lifecycle.snapshot(current, NS(references=refs))
                self.assertNotIn("factor_date", fields)
                self.assertNotIn("factor_date_basis", fields)
                self.assertFalse(fields["reference_manifest"]["adjustment_factors_used"])


class StrategyExposureTests(unittest.TestCase):
    def test_roll_reads_only_owned_positions_and_pending(self):
        ids = {s: InstrumentId.from_str(s + ".SHFE") for s in ("rb2701", "rb2705")}
        strategy = strategy_module.CalendarSpreadStrategy("owned", strategy_module.CalendarSpreadConfig(),
            (strategy_module.CalendarSelection(date(2026, 10, 9), 0, *ids),), ids)
        strategy._bind(NS(position=lambda key: Decimal(0), strategy_working_quantity=lambda key: Decimal(1),
            account_position=lambda key: self.fail("读取物理仓位"), working_quantity=lambda key: self.fail("读取账户挂单")))
        self.assertTrue(strategy._exposure(tuple(ids)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
