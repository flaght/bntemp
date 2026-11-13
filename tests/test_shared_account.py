"""共享执行的实际账本/状态机故障回归；仅柜台传输为替身。"""

from dataclasses import replace
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import sys
from types import ModuleType, SimpleNamespace as NS
from contextlib import ExitStack
import unittest
from unittest.mock import patch

from tests import test_execution_composition as fixtures


class SharedAccountTests(unittest.TestCase):
    setUp = fixtures.ExecutionCompositionTests.setUp
    client = fixtures.ExecutionCompositionTests.client
    request = fixtures.ExecutionCompositionTests.request

    def test_order_budget_is_saved_before_transport_and_not_refunded_after_send(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory)/"account.json"
            account, service, transport, (one, _), _ = self.native_account(path, modules)
            account.start(resume=False); service.start(); one.start(); account.authorize()
            send = transport.send_order
            saved = []
            def capture(fields):
                state = json.loads(path.read_text())["payload"]["component_states"]["shared_account"]
                saved.append(state["shared"]["order_budget"]["submitted"])
                send(fields)
            transport.send_order = capture
            self.submit(one)
            self.assertEqual(saved, [1])
            self.assertEqual(account.driver.order_budget["submitted"], 1)
            one.stop(); account.stop()

    def test_budget_stop_cancel_failure_is_not_reported_as_normal_completion(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, _), _ = self.native_account(Path(directory)/"account.json", modules)
            self.addCleanup(account.driver.stop)
            account.driver._max_session_orders = 1
            account.start(resume=False); service.start(); one.start(); account.authorize()
            self.submit(one, 2)
            transport.cancel_order = lambda *_: (_ for _ in ()).throw(RuntimeError("cancel transport failed"))
            with self.assertRaisesRegex(RuntimeError, "cancel transport failed"):
                service.poll()
            self.assertTrue(service._failed)
            self.assertTrue(account.driver._orders)
            self.assertEqual(service.position_manager.working_quantity(service.client_id, self.instrument), 2)

    def test_budget_rejects_whole_two_leg_batch_before_any_leg_is_sent(self):
        modules = self.native_modules()
        extra = type(self.instrument).from_str("HC.SHFE")
        with TemporaryDirectory() as directory:
            account, service, transport, _, _ = self.native_account(Path(directory)/"account.json", modules,
                scoped=True, execution_mode="independent", extra_instruments=(extra,))
            account.driver._max_session_orders = 1
            service.prices.update(extra, 3000, self.now)
            port = service.strategy_client("pair", routes={"a": self.instrument, "b": extra},
                budget=import_module("bomber.framework.trader.execution.shared").SharedRiskBudget(5, 50000))
            account.start(resume=False); service.start(); port.start(); account.authorize()
            request = replace(self.request(service.client_id, strategy="pair", timestamp=self.now),
                targets={self.instrument: Decimal(1), extra: Decimal(-1)}, metadata={"position_attribution": tuple(
                    {"strategy_id": "pair", "target_key": key, "instrument_id": str(i), "quantity": str(q)}
                    for key, i, q in (("a", self.instrument, 1), ("b", extra, -1)))})
            port.submit_targets(request)
            self.assertEqual(service.execution_stop["requested"], 2)
            self.assertFalse(transport.sent)
            self.assertFalse(service._failed or service._pending)
            self.assertFalse(service.position_manager.attribution.state()["orders"])
            service.poll(); port.stop(); account.stop()

    def test_budget_counter_and_stop_survive_resume_without_reporting_new_sends(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory)/"account.json"
            account, service, transport, (one, _), _ = self.native_account(path, modules, execution_mode="independent")
            account.driver._max_session_orders = 1
            account.start(resume=False); service.start(); one.start(); account.authorize()
            self.submit(one)
            transport.fill(transport.sent[-1]["OrderRef"])
            service.poll(); one.stop(); account.stop()
            resumed, service2, transport2, _, _ = self.native_account(path, modules, execution_mode="independent")
            resumed.driver._max_session_orders = 1
            transport2.gross = dict(transport.gross)
            resumed.start(resume=True)
            self.assertEqual(resumed.driver.order_budget["submitted"], 1)
            self.assertEqual(resumed.driver.submitted_orders, 0)
            self.assertEqual(service2.execution_stop["reason"], "order_budget_exhausted")
            with self.assertRaisesRegex(RuntimeError, "报单上限"):
                resumed.driver.check_order_capacity(1)
            resumed.stop()
            # 只有运行入口显式声明新窗口额度时才允许重置计数，原仓和归属仍保留。
            window, service3, transport3, _, _ = self.native_account(path, modules, execution_mode="independent")
            window.driver._max_session_orders = 1
            window.driver.new_order_budget_window = True
            transport3.gross = dict(transport.gross)
            window.start(resume=True)
            self.assertEqual(window.driver.order_budget["submitted"], 0)
            self.assertIsNone(service3.execution_stop)
            self.assertEqual(service3.position_manager.position("one", "main"), 1)
            window.driver.check_order_capacity(1)
            window.stop()

    def test_independent_cffex_owners_open_opposite_and_close_only_their_own(self):
        modules = self.native_modules()
        self.instrument = type(self.instrument).from_str("IM2612.CFFEX")
        with TemporaryDirectory() as directory:
            account, service, transport, (one, two), _ = self.native_account(Path(directory)/"account.json", modules,
                scoped=True, execution_mode="independent", generic_close_venues=("CFFEX",))
            account.start(resume=False); service.start(); one.start(); two.start(); account.authorize()
            self.submit(one, 1); transport.fill(transport.sent[-1]["OrderRef"])
            self.submit(two, -1); transport.fill(transport.sent[-1]["OrderRef"])
            self.assertEqual(transport.gross[str(self.instrument)], (1, 1))
            self.submit(one, 0, revision=2)
            self.assertEqual(transport.sent[-1]["CombOffsetFlag"], "1")
            transport.fill(transport.sent[-1]["OrderRef"])
            self.assertEqual(service.position_manager.position("one", "main"), 0)
            self.assertEqual(service.position_manager.position("two", "main"), -1)
            account._validate_account()
            one.stop(); two.stop(); account.stop()

    def service(self, *, quantity=10, notional=100000, max_events=10000):
        self.timestamp = time.time_ns()
        self.now = self.timestamp
        module = import_module("bomber.framework.trader.execution.shared")
        client = self.client("account")
        service = module.SharedAccountExecutionService(client, prices=client.risk_manager.price_store,
            instrument_limits=client.risk_manager.instrument_limits,
            budget=module.SharedRiskBudget(quantity, notional), clock_ns=lambda: self.now, max_events=max_events)
        ports = [service.strategy_client(sid, routes={"main": self.instrument},
            budget=module.SharedRiskBudget(5, 50000)) for sid in ("one", "two")]
        service.start()  # 宿主租约。
        for port in ports:
            port.start()
        self.addCleanup(service.stop)
        return service, client, ports

    def submit(self, port, quantity=1, revision=1, **kwargs):
        port.submit_targets(replace(self.request(port.client_id, strategy=port.strategy_id,
            quantity=quantity, revision=revision, timestamp=self.now), **kwargs))

    def test_retained_target_checks_prices_at_send_time_not_older_trigger_time(self):
        modules = self.native_modules()
        for mode in ("independent", "netted"):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                account, service, transport, (one, _), _ = self.native_account(
                    Path(directory) / "account.json", modules, execution_mode=mode)
                self.addCleanup(account.driver.stop)
                account.start(resume=False)
                service.start(); one.start(); account.authorize()
                with patch.object(account.client.planner, "plan", return_value=()):
                    self.submit(one)
                source_stamp = service._sources["one"]["ts_event"]
                self.now += 2_000_000_000
                trigger_stamp = self.now - 1_000_000_000
                service.prices.update(self.instrument, 3000, self.now)
                self.assertTrue(service.continue_targets("one", self.instrument, trigger_stamp))
                self.assertEqual(len(transport.sent), 1)
                intent = next(iter(account.driver._orders.values()))[1]
                self.assertEqual(intent.metadata["actual_send_ts_ns"], self.now)
                self.assertEqual(intent.metadata["execution_trigger_ns"], trigger_stamp)
                self.assertEqual(service._sources["one"]["ts_event"], source_stamp)
                self.assertFalse(service._failed)
                transport.fill(transport.sent[0]["OrderRef"])
                one.stop(); account.stop()

    def test_retained_target_still_rejects_price_actually_future_at_send_time(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, _), _ = self.native_account(
                Path(directory) / "account.json", modules, execution_mode="independent")
            self.addCleanup(account.driver.stop)
            account.start(resume=False)
            service.start(); one.start(); account.authorize()
            with patch.object(account.client.planner, "plan", return_value=()):
                self.submit(one)
            service.prices.update(self.instrument, 3000, self.now + 1_000_000_000)
            with self.assertRaisesRegex(RuntimeError, "行情时间差-1000000000ns"):
                service.continue_targets("one", self.instrument, self.now)
            self.assertFalse(transport.sent)
            self.assertTrue(service._failed)

    def test_real_fills_route_only_to_owner_and_duplicate_is_ignored(self):
        service, client, (one, two) = self.service()
        events = [[], []]
        one.register_execution_event_handler(events[0].append)
        two.register_execution_event_handler(events[1].append)
        self.submit(one)
        self.assertEqual(client.position_manager.position("one", "main"), 0)
        client.backend.fill(duplicate=True)
        one.drain_events()
        two.drain_events()
        self.assertEqual(client.position_manager.position("one", "main"), 1)
        self.assertEqual(len(events[0]), 2)
        self.assertFalse(events[1])
        self.assertTrue(all(e.identity.strategy_id == "one" for e in events[0]))
        self.assertTrue(all(e.metadata["attributed_target_revision"] == 1 for e in events[0]))

    def test_pending_orders_are_not_countertraded_on_other_strategy_update(self):
        service, client, (one, two) = self.service()
        self.submit(one, 2)
        self.submit(two, -1)
        self.assertEqual(len(client.backend.sent), 1)
        client.backend.fill()
        service.continue_targets("two", self.instrument, self.now)
        client.backend.fill(1)
        self.assertEqual((client.position_manager.position("one", "main"),
                          client.position_manager.position("two", "main")), (2, -1))
        self.assertEqual(client.position_manager.account_position("account", self.instrument), 1)

    def test_merged_partial_fills_split_quantity_fee_and_keep_frozen_owners(self):
        service, client, (one, two) = self.service()
        original = client.planner
        client.planner = type("Paused", (), {"plan": lambda *_: ()})()
        self.submit(one, 2)
        self.submit(two, 3)
        client.planner = original
        service.continue_targets("two", self.instrument, self.now)
        order = client.backend.sent[0]
        self.assertEqual(order.quantity, 5)
        events = [[], []]
        one.register_execution_event_handler(events[0].append)
        two.register_execution_event_handler(events[1].append)
        report = self.contracts.ExecutionReport("account", "account-0", self.instrument,
            "PARTIALLY_FILLED", self.timestamp, filled_quantity=3, fill_price=3000,
            order_side=order.side, order_quantity=5, report_id="partial", sequence=1,
            metadata={**order.metadata, "strategy_id": order.strategy_id, "trade_id": "trade-1",
                      "commission": "6", "commission_currency": "CNY"})
        client._on_report(report)
        client._on_report(report)
        one.drain_events()
        two.drain_events()
        fills = [[e for e in rows if hasattr(e, "trade_id")] for rows in events]
        self.assertEqual([rows[0].quantity for rows in fills], [2, 1])
        self.assertEqual([rows[0].commission for rows in fills], [4, 2])
        self.assertFalse(client.report_errors)

    def test_internal_netting_is_a_transfer_without_fabricated_fill(self):
        service, client, (one, two) = self.service()
        original = client.planner
        client.planner = type("Paused", (), {"plan": lambda *_: ()})()
        self.submit(one, 2)
        client.planner = original
        self.submit(two, -2)
        self.assertFalse(client.backend.sent)
        self.assertEqual(client.position_manager.position("one", "main"), 2)
        self.assertEqual(client.position_manager.position("two", "main"), -2)
        self.assertFalse(one._events)
        self.assertEqual(len(client.position_manager.attribution.state()["transfers"]), 1)

    def test_stop_freezes_owned_position_and_other_strategy_keeps_connection(self):
        service, client, (one, two) = self.service()
        self.submit(one)
        client.backend.fill()
        one.stop()
        service.poll()
        self.submit(two, 2)
        client.backend.fill(1)
        self.assertTrue(client.backend.running)
        self.assertEqual(client.backend.starts, 1)
        self.assertEqual(client.position_manager.position("one", "main"), 1)
        self.assertEqual(client.position_manager.position("two", "main"), 2)
        self.assertEqual(next(iter(service.coordinator.snapshot().targets.values())), 3)

    def test_stop_before_first_signal_does_not_block_other_strategy(self):
        service, client, (one, two) = self.service()
        one.stop()
        service.poll()
        self.submit(two)
        self.assertEqual(len(client.backend.sent), 1)

    def test_new_target_waits_for_cancellation_and_late_fill_uses_old_plan(self):
        service, client, (one, two) = self.service()
        cancelled = []
        client.backend.cancel_strategy = cancelled.append
        self.submit(one, 2)
        self.submit(one, -1, 2)
        self.assertEqual(cancelled, ["one"])
        self.assertEqual(len(client.backend.sent), 1)
        client.backend.fill()
        self.assertEqual(client.position_manager.position("one", "main"), 2)
        self.assertEqual([e.metadata["attributed_target_revision"] for e in one._events if hasattr(e, "trade_id")], [1])
        service.poll()
        service.continue_targets("one", self.instrument, self.now)
        self.assertEqual(sum(o.quantity for o in client.backend.sent[1:]), 3)

    def test_expired_contribution_never_inherits_another_strategy_deadline(self):
        service, client, (one, two) = self.service()
        original = client.planner
        client.planner = type("Paused", (), {"plan": lambda *_: ()})()
        self.submit(one, deadline_ns=self.now + 1_000_000_000)
        self.now += 1_000_000_001
        client.planner = original
        self.submit(two)
        self.assertFalse(client.backend.sent)
        service.poll()
        self.assertIn("one", service._suspended)
        service.continue_targets("two", self.instrument, self.now)
        self.assertEqual(client.backend.sent[0].quantity, 1)

    def test_missing_stale_and_future_budget_quotes_identify_owner_without_mutating_targets(self):
        for reason in ('missing', 'stale', 'future'):
            with self.subTest(reason=reason):
                service, client, (one, _) = self.service()
                service.prices = type(service.prices)()
                limit = service.instrument_limits[self.instrument].max_market_age_ns or 120_000_000_000
                if reason != 'missing':
                    stamp = self.now-limit-1 if reason == 'stale' else self.now+1
                    service.prices.update(self.instrument, 3000, stamp)
                before = service.snapshot_state()
                with self.assertRaisesRegex(RuntimeError,
                        f'strategy_id=one target_key=main instrument={self.instrument} reason={reason}.*age_seconds=.*max_age_seconds='):
                    self.submit(one, 2)
                self.assertEqual(service.snapshot_state(), before)

    def test_strategy_and_gross_account_budgets_reject_before_mutating(self):
        service, client, (one, two) = self.service(quantity=3)
        self.submit(one, 2)
        before = service.snapshot_state()
        with self.assertRaisesRegex(ValueError, "gross_quantity=4.*max_gross_quantity=3.*gross_notional=12000"):
            self.submit(two, -2)
        self.assertEqual(service.snapshot_state(), before)
        with self.assertRaises(ValueError):
            self.submit(two, 6)

    def test_strategy_only_account_preserves_independent_same_contract_positions_and_limits(self):
        modules = self.native_modules()
        shared = import_module("bomber.framework.trader.execution.shared")
        with TemporaryDirectory() as directory:
            account, service, broker, (one, two), _ = self.native_account(
                Path(directory)/"account.json", modules, execution_mode="independent")
            service.budget = shared.SharedRiskBudget(None, None)
            account.driver._max_session_orders = 200
            one.budget = shared.SharedRiskBudget(2, 50000)
            one.instrument_limits[self.instrument] = self.risk.RiskLimits(
                max_order_quantity=4, max_abs_position=2, max_market_age_ns=120_000_000_000)
            account.start(resume=False); service.start(); one.start(); two.start(); account.authorize()
            self.submit(one, 2)
            broker.fill(broker.sent[-1]["OrderRef"])
            self.submit(two, 3)
            broker.fill(broker.sent[-1]["OrderRef"])
            self.assertEqual(service.position_manager.position("one", "main"), 2)
            self.assertEqual(service.position_manager.position("two", "main"), 3)
            self.assertEqual(broker.gross[str(self.instrument)], (5, 0))
            before = len(broker.sent)
            with self.assertRaises(ValueError):
                self.submit(one, 3, revision=2)
            self.assertEqual(len(broker.sent), before)
            self.assertEqual(service.position_manager.position("two", "main"), 3)
            self.submit(two, 1, revision=2)
            broker.fill(broker.sent[-1]["OrderRef"])
            self.assertEqual(service.position_manager.position("two", "main"), 1)
            self.assertEqual(service.position_manager.position("one", "main"), 2)
            self.assertEqual(account.driver.order_budget, {"limit": 200, "submitted": 3})
            account.driver.check_order_capacity(197)
            with self.assertRaises(import_module("bomber.framework.trader.execution.contracts").OrderBudgetExhausted):
                account.driver.check_order_capacity(198)
            state = service.snapshot_state()
            self.assertEqual(state["shared"]["budget"], {"max_gross_quantity": None, "max_gross_notional": None})
            one.stop(); two.stop(); account.stop()
            persisted = account.manager.repository.load()
            self.assertEqual(persisted.payload["component_states"]["shared_account"]["shared"]["budget"],
                state["shared"]["budget"])
            self.assertEqual(persisted.payload["component_states"]["shared_account"]["shared"]["order_budget"],
                {"limit": 200, "submitted": 3})
            resumed, service2, broker2, (one2, two2), _ = self.native_account(
                Path(directory)/"account.json", modules, execution_mode="independent")
            service2.budget = shared.SharedRiskBudget(None, None)
            resumed.driver._max_session_orders = 200
            one2.budget = one.budget
            one2.instrument_limits[self.instrument] = one.instrument_limits[self.instrument]
            broker2.gross = dict(broker.gross)
            resumed.start(resume=True); service2.start(); one2.start(); two2.start(); resumed.authorize()
            self.assertEqual(resumed.driver.order_budget, {"limit": 200, "submitted": 3})
            self.assertEqual(service2.position_manager.position("one", "main"), 2)
            self.assertEqual(service2.position_manager.position("two", "main"), 1)
            self.assertFalse(broker2.sent)
            one2.stop(); two2.stop(); resumed.stop()

    def test_unlimited_budget_is_not_valid_for_strategy_or_netted_account(self):
        shared = import_module("bomber.framework.trader.execution.shared")
        with self.assertRaises(ValueError):
            shared.SharedRiskBudget(None, 1)
        with self.assertRaisesRegex(ValueError, "independent"):
            self.service(quantity=None, notional=None)
        service, _, _ = self.service()
        with self.assertRaisesRegex(ValueError, "策略自身"):
            service.strategy_client("unbounded", routes={"main": self.instrument},
                budget=shared.SharedRiskBudget(None, None))

    def test_strategy_only_budget_does_not_require_other_strategy_quote(self):
        modules = self.native_modules()
        shared = import_module("bomber.framework.trader.execution.shared")
        with TemporaryDirectory() as directory:
            other = type(self.instrument).from_str("p2701.DCE")
            account, service, broker, (one, two), _ = self.native_account(
                Path(directory)/"account.json", modules, execution_mode="independent", extra_instruments=(other,))
            service.budget = shared.SharedRiskBudget(None, None)
            service._ports.pop("two")
            two = service.strategy_client("two", routes={"main": other}, budget=shared.SharedRiskBudget(5, 50000))
            account.start(resume=False); service.start(); one.start(); two.start(); account.authorize()
            self.submit(one, 2)
            broker.fill(broker.sent[-1]["OrderRef"])
            self.now += 121_000_000_000
            service.prices.update(other, 3000, self.now)
            account.client.refresh_account_state()
            request = replace(self.request("account", strategy="two", quantity=2, timestamp=self.now),
                targets={other: Decimal(2)}, metadata={"position_attribution": (
                    {"strategy_id": "two", "target_key": "main", "instrument_id": str(other), "quantity": "2"},)})
            two.submit_targets(request)
            self.assertEqual(broker.sent[-1]["InstrumentID"], "p2701")
            self.assertEqual(service.position_manager.position("one", "main"), 2)
            one.stop(); two.stop(); account.stop()

    def test_strategy_order_limits_are_rechecked_at_native_dispatch(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, broker, (one, _), _ = self.native_account(
                Path(directory)/"account.json", modules, execution_mode="independent")
            service.budget = import_module("bomber.framework.trader.execution.shared").SharedRiskBudget(None, None)
            one.instrument_limits[self.instrument] = self.risk.RiskLimits(
                max_order_quantity=1, max_abs_position=5, max_market_age_ns=120_000_000_000)
            account.start(resume=False); service.start(); one.start(); account.authorize()
            with self.assertRaisesRegex(self.risk.RiskRejected, "订单数量2超过上限1"):
                self.submit(one, 2)
            self.assertFalse(broker.sent)
            self.assertEqual(service.position_manager.position("one", "main"), 0)
            one.stop()
            with self.assertRaisesRegex(RuntimeError, "保持闭闸"):
                account.stop()

    def test_wrong_identity_route_or_same_version_policy_is_rejected(self):
        service, client, (one, two) = self.service()
        with self.assertRaises(ValueError):
            one.submit_targets(self.request("account", strategy="two"))
        self.submit(one)
        with self.assertRaises(ValueError):
            self.submit(one, deadline_ns=self.now + 1000)
        with self.assertRaises(ValueError):
            self.submit(two, execution_policy="TWAP")

    def test_queue_overflow_closes_account(self):
        service, client, (one, two) = self.service(max_events=1)
        self.submit(one)
        client.backend.fill()
        self.assertTrue(client.report_errors)
        with self.assertRaises(RuntimeError):
            service.poll()

    def test_checkpoint_restores_other_sources_and_holds_old_targets(self):
        service, client, (one, two) = self.service()
        self.submit(one)
        client.backend.fill()
        self.submit(two, 2)
        client.backend.fill(1)
        state = service.snapshot_state()
        restored, restored_client, ports = self.service()
        # 恢复在启动前；本用例单独核对贡献身份与保守门控。
        restored._leases = 0
        coordinator = restored.coordinator
        restored.restore_state(state)
        self.assertIs(restored.coordinator, coordinator)
        self.assertEqual(next(iter(coordinator.snapshot().targets.values())), 3)
        self.assertEqual(restored._suspended, {"one", "two"})
        self.assertFalse(restored_client.backend.sent)
        state["pending"] = True
        restored.restore_state(state)
        self.assertTrue(restored._failed)

    def test_callback_snapshot_does_not_wait_for_strategy_runner(self):
        import threading
        service, client, (one, two) = self.service()
        self.submit(one)
        entered = threading.Event()
        with one._lock:
            worker = threading.Thread(target=lambda: (service.snapshot_state(), entered.set()))
            worker.start()
            self.assertTrue(entered.wait(1))
        worker.join(1)

    def native_modules(self):
        packages = {}
        for name, relative in (("bomber.framework.trader.execution.ctp", "bomber/framework/trader/execution/ctp"),
                ("bomber.framework.trader.execution.simulation", "bomber/framework/trader/execution/simulation"),
                ("bomber.framework.trader.runtime", "bomber/framework/trader/runtime"),
                ("bomber.framework.market.stream", "bomber/framework/market/stream")):
            module = ModuleType(name)
            module.__path__ = [str(fixtures.ROOT / relative)]
            packages[name] = module
        profile = ModuleType("bomber.framework.trader.execution.ctp.profile")
        profile.CtpCommissionRule = object  # SDK费用Profile未参与本组无费用测试。
        packages[profile.__name__] = profile
        sdk_driver = ModuleType("bomber.framework.trader.execution.live.driver")
        sdk_driver.NautilusLiveDriverPort = object  # 仅Backend的类型声明；本组实际使用原生CTP Driver。
        packages[sdk_driver.__name__] = sdk_driver
        context = patch.dict(sys.modules, packages)
        context.start()
        self.addCleanup(context.stop)
        sys.modules["bomber.framework.market.basic.base"].MarketDataFeed = object
        return NS(driver=import_module("bomber.framework.trader.execution.ctp.native_driver"),
            backend=import_module("bomber.framework.trader.execution.live.backend"),
            controlled=import_module("bomber.framework.trader.execution.live.controlled"),
            ledger=import_module("bomber.framework.trader.execution.ctp.ledger"),
            planner=import_module("bomber.framework.trader.execution.ctp.limit_planner"),
            accounting=import_module("bomber.framework.trader.execution.ctp.accounting"),
            lifecycle=import_module("bomber.framework.trader.runtime.live.channels.ctp_shared"),
            runtime=import_module("bomber.framework.trader.runtime.live.shared"),
            runner=import_module("bomber.framework.trader.runner"),
            template=import_module("bomber.framework.trader.template"),
            persistence=import_module("bomber.framework.trader.persistence"),
            events=import_module("bomber.framework.trader.execution.events"))

    def native_account(self, path, modules, *, scoped=False, execution_mode="netted", preserve_external=False,
                       extra_instruments=(), generic_close_venues=()):
        owner = self
        self.timestamp = time.time_ns()
        self.now = self.timestamp
        class Transport:
            broker_id, investor_id, account_id, client_id = "9999", "demo", "demo", "account"
            front = "tcp://fake:1234"
            is_test_transport = False
            def __init__(self):
                self.sent, self.active, self.gross, self.canceled = [], {}, {}, []
                self.revision = self.starts = 0
            def connect(self, on_order, on_trade, on_disconnect):
                self.on_order, self.on_trade = on_order, on_trade
                self.starts += 1
                return modules.driver.CtpTraderSession("9999", "demo", "20261013", 0)
            def activate(self): pass
            def close(self): pass
            def query_positions(self):
                return {i: Decimal(long - short) for i, (long, short) in self.gross.items()}
            def query_gross_positions(self): return self.gross
            def query_position_details(self):
                return [{"InstrumentID": i.split(".")[0], "ExchangeID": i.split(".")[1],
                    "Position": q, "TodayPosition": q, "PosiDirection": direction,
                    "PositionDate": "1", "HedgeFlag": "1"} for i, values in self.gross.items()
                    for direction, q in zip(("2", "3"), values) if q]
            def query_account(self):
                self.revision += 1
                return modules.events.AccountStateEvent("account", "demo", self.revision, time.time_ns(),
                    {"CNY": modules.events.CurrencyBalance("CNY", total=100000, available=100000)})
            def query_active_orders(self):
                self.revision += 1
                orders = tuple(modules.events.ActiveOrder(f"CTP-9999-demo-20261013-{ref}",
                    f"{row['InstrumentID']}.{row['ExchangeID']}", "BUY" if row["Direction"] == "0" else "SELL",
                    row["VolumeTotalOriginal"], row["filled"], row["VolumeTotalOriginal"] - row["filled"])
                    for ref, row in self.active.items())
                return modules.events.ActiveOrderSnapshot("account", "demo", self.revision, time.time_ns(), orders)
            def raw(self, ref):
                row = self.active[ref]
                return {"BrokerID": "9999", "InvestorID": "demo", "InstrumentID": row["InstrumentID"],
                    "ExchangeID": row["ExchangeID"], "OrderRef": ref, "FrontID": 1, "SessionID": 2,
                    "OrderSysID": "SYS" + ref}
            def send_order(self, fields):
                self.sent.append(dict(fields))
                self.active[fields["OrderRef"]] = {**fields, "filled": 0}
                self.on_order({**self.raw(fields["OrderRef"]), "OrderStatus": "3"})
            def cancel_order(self, ref):
                self.canceled.append(ref)
                self.on_order({**self.raw(ref), "OrderStatus": "5"})
                self.active.pop(ref)
            def fill(self, ref, quantity=None):
                row = self.active[ref]
                quantity = quantity or row["VolumeTotalOriginal"] - row["filled"]
                raw = self.raw(ref)
                instrument = f"{row['InstrumentID']}.{row['ExchangeID']}"
                long, short = self.gross.get(instrument, (0, 0))
                buy, opening = row["Direction"] == "0", row["CombOffsetFlag"] == "0"
                if opening:
                    long, short = (long + quantity, short) if buy else (long, short + quantity)
                else:
                    long, short = (long, short - quantity) if buy else (long - quantity, short)
                self.gross[instrument] = (long, short)
                row["filled"] += quantity
                self.on_trade({**raw, "TradeID": f"T{ref}-{row['filled']}", "Volume": quantity,
                    "Price": row["LimitPrice"]})
                if row["filled"] == row["VolumeTotalOriginal"]:
                    self.active.pop(ref)
        transport = Transport()
        driver = modules.driver.CtpNativeTraderDriver("account", "demo", transport,
            enable_simnow_orders=True, disconnect_handler=lambda reason: None)
        positions = self.portfolio.PositionManager()
        prices = self.risk.MarketReferencePriceStore()
        prices.update(self.instrument, 3000, self.now)
        limits = {i: self.risk.RiskLimits(max_order_quantity=10, max_abs_position=10,
            max_market_age_ns=120_000_000_000, contract_multiplier=1) for i in (self.instrument, *extra_instruments)}
        ledger = modules.ledger.CtpPositionLedger("20261013")
        backend = modules.backend.NautilusLiveExecutionBackend("account", driver)
        client = modules.controlled.ControlledLiveExecutionClient("account",
            modules.planner.CtpLimitPlanner(ledger, positions, prices, 1, 0), backend, positions,
            self.risk.PreTradeRiskManager("account", positions, prices, instrument_limits=limits),
            account_id="demo", demo_environment_check=lambda: driver.is_simnow_session)
        backend.register_report_handler(modules.accounting.CtpExecutionAccounting(ledger, {self.instrument: 1}).on_report)
        scope = None
        if scoped:
            scope = import_module("bomber.framework.trader.runtime.live.recovery").InstrumentPositionScope(limits)
            # 使用实际portfolio builder验证范围绑定和最后一层报单限制。
            ctp_package = sys.modules["bomber.framework.trader.execution.ctp"]
            ctp_package.CtpExecutionAccounting = modules.accounting.CtpExecutionAccounting
            ctp_package.CtpLimitPlanner = modules.planner.CtpLimitPlanner
            ctp_package.CtpPositionLedger = modules.ledger.CtpPositionLedger
            live_package = sys.modules["bomber.framework.trader.execution.live"]
            live_package.ControlledLiveExecutionClient = modules.controlled.ControlledLiveExecutionClient
            live_package.NautilusLiveExecutionBackend = modules.backend.NautilusLiveExecutionBackend
            sys.modules["bomber.framework.trader.execution.simulation"].SimulationExecutionClient = import_module(
                "bomber.framework.trader.execution.simulation.client").SimulationExecutionClient
            execution = import_module("bomber.framework.trader.execution.builders").build_ctp_portfolio_execution(
                driver, trading_day="20261013", price_increments={i: 1 for i in limits}, multipliers={i: 1 for i in limits},
                instrument_limits=limits, session_check=lambda: driver.is_simnow_session, orders=True,
                positions=positions, prices=prices, instrument_scope=scope, limit_offset_ticks=0,
                generic_close_venues=generic_close_venues,
                account_reconciliation="observe" if preserve_external else "strict")
            client, backend, ledger = execution.client, execution.backend, execution.ledger
        shared = import_module("bomber.framework.trader.execution.shared")
        service = shared.SharedAccountExecutionService(client, prices=prices, instrument_limits=limits,
            budget=shared.SharedRiskBudget(10, 100000), clock_ns=lambda: self.now, instrument_scope=scope,
            execution_mode=execution_mode)
        ports = [service.strategy_client(sid, routes={"main": self.instrument},
            budget=shared.SharedRiskBudget(5, 50000)) for sid in ("one", "two")]
        resources = ExitStack()
        self.addCleanup(resources.close)
        execution = NS(driver=driver, client=client, ledger=ledger, positions=positions, backend=backend)
        lifecycle = modules.lifecycle.CtpSharedAccountLifecycle(execution, service, transport=transport,
            state_file=path, identity={"id": "test-shared"}, resources=resources, expected_day="20261013",
            position_scope=scope, preserve_external=preserve_external)
        self.addCleanup(client.stop)
        return lifecycle, service, transport, ports, resources

    def test_partial_nonqueueing_terminal_releases_only_remainder_and_keeps_short_one(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, _), _ = self.native_account(Path(directory)/'account.json', modules)
            self.addCleanup(account.driver.stop)
            account.start(resume=False); service.start(); one.start(); account.authorize()
            self.submit(one, -2)
            ref = transport.sent[0]['OrderRef']
            transport.fill(ref, 1)
            raw = {**transport.raw(ref), 'OrderStatus': '2', 'VolumeTraded': 1}
            transport.active.pop(ref)
            transport.on_order(raw)
            state = account.client.order_state_machine.states()[0]
            self.assertEqual(state.status.value, 'CANCELED')
            self.assertEqual(state.filled_quantity, 1)
            self.assertEqual(service.position_manager.position('one', 'main'), -1)
            self.assertEqual(service.position_manager.working_quantity('account', self.instrument), 0)
            self.assertFalse(account.driver._orders or account.driver._terminal_cancels)
            transport.on_order(raw)  # 重复终态不重复释放或产生新成交。
            one.stop(); account.stop()
            saved = account.manager.repository.load().payload
            self.assertFalse(saved['ctp_drivers']['account']['orders'])
            self.assertEqual(len(transport.sent), 1)
            self.assertEqual(account.final_gross[str(self.instrument)], (0, 1))

    def test_terminal_notification_before_trade_waits_for_fill_then_releases_remainder(self):
        modules = self.native_modules()
        for terminal in ('2', '5'):
            with self.subTest(status=terminal), TemporaryDirectory() as directory:
                account, service, transport, (one, _), _ = self.native_account(Path(directory)/'account.json', modules)
                self.addCleanup(account.driver.stop)
                account.start(resume=False); service.start(); one.start(); account.authorize()
                self.submit(one, -2)
                ref = transport.sent[0]['OrderRef']
                raw = {**transport.raw(ref), 'OrderStatus': terminal, 'VolumeTraded': 1}
                transport.on_order(raw)
                self.assertIn(ref, account.driver._orders)
                self.assertEqual(service.position_manager.position('one', 'main'), 0)
                self.assertEqual(service.position_manager.working_quantity('account', self.instrument), -2)
                transport.fill(ref, 1)
                duplicate = {**transport.raw(ref), 'TradeID': f'T{ref}-1', 'Volume': 1,
                    'Price': transport.active[ref]['LimitPrice']}
                transport.active.pop(ref)
                transport.on_trade(duplicate)
                transport.on_order(raw)
                self.assertEqual(service.position_manager.position('one', 'main'), -1)
                self.assertEqual(service.position_manager.working_quantity('account', self.instrument), 0)
                self.assertEqual(account.client.order_state_machine.states()[0].status.value, 'CANCELED')
                self.assertFalse(account.driver._orders or account.driver._terminal_cancels)
                one.stop(); account.stop()
                self.assertEqual(account.final_gross[str(self.instrument)], (0, 1))
                self.assertEqual(len(transport.sent), 1)

    def test_no_trade_nonqueueing_terminal_releases_all_working_quantity_without_fill(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, _), _ = self.native_account(Path(directory)/'account.json', modules)
            self.addCleanup(account.driver.stop)
            account.start(resume=False); service.start(); one.start(); account.authorize()
            self.submit(one, 2)
            ref = transport.sent[0]['OrderRef']
            raw = {**transport.raw(ref), 'OrderStatus': '4', 'VolumeTraded': 0}
            transport.active.pop(ref)
            transport.on_order(raw)
            self.assertEqual(service.position_manager.position('one', 'main'), 0)
            self.assertEqual(service.position_manager.working_quantity('account', self.instrument), 0)
            self.assertEqual(account.client.order_state_machine.states()[0].status.value, 'CANCELED')
            self.assertFalse(account.driver._orders or account.driver._terminal_cancels)
            one.stop(); account.stop()
            self.assertFalse(account.final_gross)

    def test_trade_larger_than_terminal_cumulative_does_not_apply_a_guessed_fill(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, _), _ = self.native_account(Path(directory)/'account.json', modules)
            self.addCleanup(account.driver.stop)
            account.start(resume=False); service.start(); one.start(); account.authorize()
            self.submit(one, -2)
            ref = transport.sent[0]['OrderRef']
            transport.on_order({**transport.raw(ref), 'OrderStatus': '2', 'VolumeTraded': 1})
            with self.assertRaisesRegex(RuntimeError, '超过终态'):
                transport.on_trade({**transport.raw(ref), 'TradeID': 'conflicting', 'Volume': 2, 'Price': 3000})
            self.assertEqual(service.position_manager.position('one', 'main'), 0)
            self.assertIn(ref, account.driver._orders)

    def test_late_order_query_final_reply_cannot_complete_next_request(self):
        self.native_modules()
        from tests.run_p4_ctp_td_transport import FakeTdApi, _transport
        class Api(FakeTdApi):
            previous = None
            def reqQryOrder(self, data, reqid):
                if self.previous is None:
                    self.previous = reqid
                    return 0  # 首次请求没有最终回报。
                self.onRspQryOrder({}, {}, reqid, True)  # 新查询正常完成且无活动单。
                return 0
        transport = _transport(Api)
        transport.connect(lambda event: None, lambda event: None, lambda reason: None)
        transport.activate()
        self.addCleanup(transport.close)
        with self.assertRaises(TimeoutError): transport.query_active_orders()
        # 旧请求最终回报先到，才能发送新查询；旧的活动单数据不能成为新快照。
        FakeTdApi.reqQryOrder(transport._api, {}, transport._api.previous)
        self.assertFalse(transport.query_active_orders().orders)

    def test_periodic_owned_reconciliation_queries_orders_once(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, _, _ = self.native_account(Path(directory)/"account.json", modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            account.start(resume=False); account.authorize()
            original = transport.query_active_orders
            calls = []
            def query():
                calls.append(1)
                return original()
            transport.query_active_orders = query
            account._next_check = 0
            account.poll()
            self.assertEqual(len(calls), 1)
            account.stop()

    def test_order_query_timeout_host_stop_drains_partial_fill_and_reconciles_without_rearming(self):
        with TemporaryDirectory() as directory:
            host, transport, rows = self.callback_host(Path(directory))
            service, account = host.service, host.account
            # netted会话同样需要异常停止支持；当时正有本系统部分成交订单。
            self.submit(rows[0].session.client, 2)
            ref = transport.sent[-1]["OrderRef"]
            transport.fill(ref, 1)
            original = transport.query_active_orders
            calls = [0]
            def query():
                calls[0] += 1
                if calls[0] in (1, 2):
                    raise TimeoutError("fixture orders timeout")
                return original()
            # 强制走运行检查中的订单查询（原callback_host为全账户策略）。
            scope = import_module("bomber.framework.trader.runtime.live.recovery").InstrumentPositionScope([self.instrument])
            account.position_scope = scope
            transport.query_active_orders = query
            account._next_check = 0
            with self.assertRaisesRegex(TimeoutError, "orders timeout"):
                host.poll()
            sends = len(transport.sent)
            host.stop()
            self.assertEqual(len(transport.sent), sends)
            self.assertEqual(account.stop_query_attempts, 2)
            self.assertFalse(account.client.is_armed)
            self.assertEqual(account.final_active_orders, 0)
            self.assertEqual(service.position_manager.position("one", "main"), 1)
            self.assertTrue(rows[0].session.strategy.fills)
            self.assertFalse(service.position_manager.is_recovery_required(service.client_id))
            self.assertTrue(all(not row.active for row in rows))
            self.assertFalse(host.snapshot()["strategies"]["one"]["execution_events"]["pending_fills"])

    def test_disconnected_host_stop_preserves_files_bindings_and_original_working_order(self):
        from unittest.mock import Mock
        self.native_modules()
        errors = import_module('bomber.framework.trader.execution.ctp.query_errors')
        for working in (False, True):
            with self.subTest(working=working), TemporaryDirectory() as directory:
                host, transport, rows = self.callback_host(Path(directory))
                account, service = host.account, host.service
                self.addCleanup(account.driver.stop)
                if working:
                    self.submit(rows[0].session.client, 2)
                else:
                    self.submit(rows[0].session.client, 1)
                    transport.fill(transport.sent[-1]['OrderRef'])
                    rows[0].session.client.drain_events()
                    rows[0].save(service)
                path = Path(directory)/'account.json'
                before = {p:p.read_bytes() for p in (path,*(row.state_file for row in rows))}
                original = service.position_manager.snapshot()
                account.query_failure = '等待CTP orders最终回报超时 (request_id=989)'
                transport.cancel_order = Mock(side_effect=AssertionError('断线不应发送撤单'))
                account.driver.on_disconnect(8193)
                account.client.mark_disconnected('ctp_front_disconnected:8193')
                with self.assertRaisesRegex(errors.CtpStopReconciliationRequired, '保留最后有效.*8193.*989'):
                    host.stop()
                self.assertTrue(all(not row.session.runner.accept_bars for row in rows))
                self.assertTrue(all(row.active and row.session.strategy.is_started for row in rows))
                self.assertFalse(host._stopped)
                self.assertFalse(account.client.is_armed)
                self.assertTrue(service.position_manager.is_recovery_required(service.client_id))
                self.assertEqual(account.stop_query_attempts, 0)
                self.assertIsNone(account.final_active_orders)
                transport.cancel_order.assert_not_called()
                self.assertEqual(original.strategy_positions,service.position_manager.snapshot().strategy_positions)
                self.assertEqual(original.working_quantities,service.position_manager.snapshot().working_quantities)
                for p,raw in before.items():self.assertEqual(p.read_bytes(),raw)
                state=host.snapshot()['stop_reconciliation']
                self.assertEqual(state['disconnect']['reason'],8193)
                self.assertIn('989',state['failure'])
                with self.assertRaisesRegex(RuntimeError,'CTP会话已断开或未登录.*8193'):
                    account.driver.checkpoint()
                with self.assertRaises(errors.CtpStopReconciliationRequired):account.stop()
                for p,raw in before.items():self.assertEqual(p.read_bytes(),raw)

    def test_funds_query_timeout_can_finish_stop_without_client_reauthorization(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, _, _ = self.native_account(Path(directory)/"account.json", modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            account.start(resume=False); account.authorize()
            original = transport.query_account
            calls = [0]
            def query():
                calls[0] += 1
                if calls[0] == 1: raise TimeoutError("fixture funds timeout")
                return original()
            transport.query_account = query
            account._next_check = 0
            with self.assertRaises(TimeoutError): account.poll()
            self.assertFalse(account.client.is_reconciled)
            account.stop()
            self.assertEqual(account.final_active_orders, 0)
            self.assertFalse(account.client.is_armed)
            self.assertFalse(transport.sent)

    def test_stop_query_timeout_exhaustion_keeps_recovery_marker_and_does_not_send(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, _, _ = self.native_account(Path(directory)/"account.json", modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            account.start(resume=False); account.authorize()
            original = transport.query_active_orders
            transport.query_active_orders = lambda: (_ for _ in ()).throw(TimeoutError("fixture timeout"))
            account._next_check = 0
            with self.assertRaises(TimeoutError): account.poll()
            with self.assertRaises(TimeoutError): account.stop()
            self.assertEqual(account.stop_query_attempts, 2)
            self.assertTrue(account.execution.positions.is_recovery_required(service.client_id))
            self.assertIsNone(account.final_active_orders)
            self.assertFalse(transport.sent)
            self.assertFalse(account.client.is_armed)
            transport.query_active_orders = original
            account.stop()

    def test_real_funds_timeout_then_rejected_stop_query_retries_without_rearming(self):
        modules = self.native_modules()
        from tests.run_p4_ctp_td_transport import FakeTdApi
        td = import_module("bomber.framework.trader.execution.ctp.td_transport")
        class Api(FakeTdApi):
            def __init__(self):
                super().__init__(); self.funds_calls = []
            def reqQryTradingAccount(self, data, reqid):
                self.funds_calls.append(reqid)
                if len(self.funds_calls) == 1: return 0
                if len(self.funds_calls) == 2: return -2
                return super().reqQryTradingAccount(data, reqid)
        with TemporaryDirectory() as directory:
            account, service, transport, (one, _), _ = self.native_account(Path(directory)/"account.json", modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            account.start(resume=False); service.start(); one.start(); account.authorize()
            self.submit(one, -1)
            transport.fill(transport.sent[-1]["OrderRef"])
            real = td.CtpTdApiTransport(client_id="account", account_id="demo", front="tcp://fake:1234",
                broker_id="9999", investor_id="demo", password="fake", app_id="fake", auth_code="fake",
                td_api_base=Api, timeout_seconds=.01)
            real.connect(lambda *_: None, lambda *_: None, lambda *_: None); real.activate()
            self.addCleanup(real.close)
            real._account_revision = transport.revision
            transport.query_account = real.query_account
            transport.query_diagnostics = real.query_diagnostics
            account._next_check = 0
            with self.assertRaises(TimeoutError): account.poll()
            self.assertTrue(account.execution.positions.is_recovery_required(service.client_id))
            old = real._pending.reqid
            real._api.onRspQryTradingAccount({"Balance": 999999}, {}, old, True)
            sent = len(transport.sent)
            with patch.object(modules.lifecycle.time, "sleep") as sleep:
                account.stop()
            sleep.assert_called_once_with(1.0)
            self.assertEqual(account.stop_query_attempts, 2)
            self.assertEqual(account.stop_query_errors[0]["code"], -2)
            self.assertIn("最终回报超时", account.query_failure)
            self.assertEqual(account.final_active_orders, 0)
            self.assertEqual(service.position_manager.position("one", "main"), -1)
            self.assertFalse(account.execution.positions.is_recovery_required(service.client_id))
            self.assertFalse(account.client.is_armed)
            self.assertEqual(len(transport.sent), sent)
            self.assertEqual(len(real._api.funds_calls), 3)

    def test_stop_query_rejection_budget_and_fatal_errors_are_distinct(self):
        modules = self.native_modules()
        error_type = import_module("bomber.framework.trader.execution.ctp.query_errors").CtpQueryRejected
        for code in (-2, -3, -1):
            with self.subTest(code=code), TemporaryDirectory() as directory:
                path = Path(directory)/"account.json"
                account, service, transport, _, _ = self.native_account(path, modules,
                    scoped=True, execution_mode="independent", preserve_external=True)
                account.start(resume=False); account.authorize()
                original, before, calls = transport.query_account, path.read_bytes(), []
                def query():
                    calls.append(1)
                    raise error_type("account", "reqQryTradingAccount", len(calls), code)
                transport.query_account = query
                account._next_check = 0
                with self.assertRaises(error_type): account.poll()
                with patch.object(modules.lifecycle.time, "sleep") as sleep:
                    with self.assertRaises(error_type): account.stop()
                expected = 2 if code in (-2,-3) else 1
                self.assertEqual(account.stop_query_attempts, expected)
                self.assertEqual(len(calls), expected+1)
                self.assertEqual(sleep.call_count, expected-1)
                self.assertEqual(path.read_bytes(), before)
                self.assertIsNone(account.final_active_orders)
                self.assertTrue(account.execution.positions.is_recovery_required(service.client_id))
                self.assertFalse(account.client.is_armed or transport.sent)
                transport.query_account = original
                account.stop()

    def test_independent_same_instrument_external_baseline_never_becomes_owned_or_closed(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, service, transport, (one, two), _ = self.native_account(path, modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            transport.gross = {str(self.instrument): (2, 3), "ag2612.SHFE": (0, 1)}
            baseline = dict(transport.gross)
            account.start(resume=False)
            service.start(); one.start(); two.start(); account.authorize()
            self.assertEqual(service.position_manager.account_position(service.client_id, self.instrument), 0)
            self.assertEqual(service.position_manager.position("one", "main"), 0)
            self.submit(one, 1)
            self.assertEqual(transport.sent[-1]["CombOffsetFlag"], "0")
            transport.fill(transport.sent[-1]["OrderRef"])
            self.assertEqual(transport.gross[str(self.instrument)], (3, 3))
            self.assertEqual(service.position_manager.position("one", "main"), 1)
            self.assertEqual(service.position_manager.position("two", "main"), 0)
            account._validate_account()
            self.submit(one, 0, revision=2)
            self.assertEqual(transport.sent[-1]["CombOffsetFlag"], "3")
            self.assertEqual(transport.sent[-1]["VolumeTotalOriginal"], 1)
            transport.fill(transport.sent[-1]["OrderRef"])
            account.stop()
            self.assertEqual(transport.gross, baseline)
            restored, service2, transport2, _, _ = self.native_account(path, modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            transport2.gross = dict(baseline)
            restored.start(resume=True)
            self.assertEqual(restored.external_gross, baseline)
            self.assertEqual(service2.position_manager.position("one", "main"), 0)
            self.assertFalse(transport2.sent)
            restored.stop()

    def test_same_instrument_external_change_with_identical_net_blocks_restore(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, _, transport, _, _ = self.native_account(path, modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            transport.gross = {str(self.instrument): (1, 1)}
            account.start(resume=False); account.authorize(); account.stop()
            before = path.read_bytes()
            restored, _, changed, _, _ = self.native_account(path, modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            changed.gross = {str(self.instrument): (2, 2)}
            with self.assertRaisesRegex(RuntimeError, "总仓"):
                restored.start(resume=True)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(changed.sent)

    def test_same_instrument_external_yesterday_is_preserved_while_own_today_closes(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, _), _ = self.native_account(Path(directory)/"account.json", modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            transport.gross = {str(self.instrument): (0, 2)}
            original = transport.query_position_details
            def details():
                return [{**row, "PositionDate": "2", "TodayPosition": 0}
                    if row["PosiDirection"] == "3" else row for row in original()]
            transport.query_position_details = details
            account.start(resume=False); service.start(); one.start(); account.authorize()
            self.submit(one, 1); transport.fill(transport.sent[-1]["OrderRef"])
            self.submit(one, 0, revision=2); transport.fill(transport.sent[-1]["OrderRef"])
            account.stop()
            self.assertEqual(transport.gross[str(self.instrument)], (0, 2))
            self.assertEqual(account.external_buckets[str(self.instrument)], (0, 0, 0, 2))

    def test_external_baseline_query_retries_known_fill_instead_of_assigning_or_halting(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, _), _ = self.native_account(Path(directory)/"account.json", modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            transport.gross = {str(self.instrument): (0, 1)}
            account.start(resume=False); service.start(); one.start(); account.authorize()
            self.submit(one, 1)
            query = transport.query_position_details
            def fill_between_queries():
                transport.query_position_details = query
                transport.fill(transport.sent[-1]["OrderRef"])
                return query()
            transport.query_position_details = fill_between_queries
            account._validate_account()
            self.assertTrue(account.client.is_armed)
            self.assertEqual(service.position_manager.position("one", "main"), 1)
            self.assertEqual(account.external_gross[str(self.instrument)], (0, 1))
            account.stop()

    def test_position_observation_uses_one_complete_query_for_gross_and_today_yesterday(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory)/"account.json"
            account, _, transport, _, _ = self.native_account(path, modules)
            def row(symbol, venue, direction, date, quantity, **extra):
                return dict(InstrumentID=symbol, ExchangeID=venue, PosiDirection=direction,
                    PositionDate=date, Position=quantity, HedgeFlag="1", **extra)
            rows = (
                row("MO2611-C-6800", "CFFEX", "3", "1", "1"),
                row("MO2611-C-6800", "CFFEX", "3", "2", "2"),
                row("rb2701", "SHFE", "2", "1", "2"),
                row("rb2701", "SHFE", "2", "2", "3"),
                row("p2701", "DCE", "3", "1", "5", TodayPosition="2", YdPosition="9"),
                row("MA701", "CZCE", "2", "1", "4", TodayPosition="1", YdPosition="8"),
                row("sc2612", "INE", "3", "2", "1"),
                row("unused", "SHFE", "2", "1", "0"),
            )
            with patch.object(transport, "query_position_details", return_value=rows) as query, \
                    patch.object(transport, "query_gross_positions", side_effect=AssertionError("禁止第二次持仓查询")):
                gross, buckets = account._observe_positions()
            query.assert_called_once_with()
            self.assertEqual(gross, {"MO2611-C-6800.CFFEX": (0, 3), "rb2701.SHFE": (5, 0),
                "p2701.DCE": (0, 5), "MA701.CZCE": (4, 0), "sc2612.INE": (0, 1)})
            self.assertEqual(buckets, {"MO2611-C-6800.CFFEX": (0, 0, 1, 2), "rb2701.SHFE": (2, 3, 0, 0),
                "p2701.DCE": (0, 0, 2, 3), "MA701.CZCE": (1, 3, 0, 0), "sc2612.INE": (0, 0, 0, 1)})
            self.assertFalse(path.exists())

    def test_position_parse_failure_logs_query_and_safe_rows_and_does_not_save(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory)/"account.json"
            account, _, transport, _, _ = self.native_account(path, modules)
            row = dict(InstrumentID="p2701", ExchangeID="DCE", PosiDirection="3", PositionDate="1",
                Position="3", TodayPosition="4", HedgeFlag="1", InvestorID="private-investor")
            with patch.object(transport, "query_position_details", return_value=(row,)), \
                    self.assertLogs("CtpPositionReconciliation", level="ERROR") as logs, \
                    self.assertRaisesRegex(RuntimeError, "TodayPosition"):
                account._observe_positions()
            diagnostic = json.loads(logs.records[0].args[0])
            self.assertEqual(diagnostic["query"]["row_count"], 1)
            self.assertLessEqual(diagnostic["query"]["started_at_ns"], diagnostic["query"]["completed_at_ns"])
            self.assertEqual(diagnostic["query"]["trading_day"], "20261013")
            self.assertEqual(diagnostic["position_rows"][0]["InstrumentID"], "p2701")
            self.assertNotIn("private-investor", logs.output[0])
            self.assertFalse(path.exists())

    def test_position_total_bucket_difference_logs_quantities_from_the_same_query(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, _, transport, _, _ = self.native_account(Path(directory)/"account.json", modules)
            key = str(self.instrument)
            transport.gross = {key: (0, 3)}
            with patch.object(modules.lifecycle, "position_buckets", return_value={
                    key: NS(quantities=(Decimal(0), Decimal(0), Decimal(1), Decimal(1)))}), \
                    self.assertLogs("CtpPositionReconciliation", level="ERROR") as logs, \
                    self.assertRaisesRegex(RuntimeError, "权威总仓与今昨明细不一致"):
                account._observe_positions()
            row = json.loads(logs.records[0].args[0])["positions"][0]
            self.assertEqual(row["instrument_id"], key)
            self.assertEqual(row["counter_gross"], {"long": "0", "short": "3"})
            self.assertEqual(row["counter_buckets"]["short_yesterday"], "1")
            self.assertEqual(row["bucket_totals_minus_gross"]["short"], "-1")

    def test_real_owned_ledger_mismatch_is_still_rejected_and_logged(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory)/"account.json"
            account, service, transport, (one, _), _ = self.native_account(path, modules)
            account.start(resume=False); service.start(); one.start(); account.authorize()
            self.submit(one, 2); transport.fill(transport.sent[-1]["OrderRef"])
            before = path.read_bytes()
            transport.gross[str(self.instrument)] = (1, 0)
            with self.assertLogs("CtpPositionReconciliation", level="ERROR") as logs, \
                    self.assertRaisesRegex(RuntimeError, "柜台总仓与保存CTP账本不一致"):
                account._validate_account()
            diagnostic = json.loads(logs.records[0].args[0])
            row = next(row for row in diagnostic["positions"] if row["instrument_id"] == str(self.instrument))
            self.assertEqual(row["counter_gross"]["long"], "1")
            self.assertEqual(row["local_ledger_buckets"]["long_today"], "2")
            self.assertEqual(service.position_manager.position("one", "main"), 2)
            self.assertEqual(path.read_bytes(), before)
            transport.gross[str(self.instrument)] = (2, 0)
            one.stop(); account.stop()

    def test_repeated_stop_after_contribution_frozen_does_not_persist_draining_again(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory)/"account.json"
            account, service, transport, (one, two), _ = self.native_account(path, modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            one.register_execution_event_handler(lambda event: None)
            two.register_execution_event_handler(lambda event: None)
            account.start(resume=False); service.start(); one.start(); two.start(); account.authorize()
            self.submit(one, 1); transport.fill(transport.sent[-1]["OrderRef"])
            one.begin_stop(); service.poll()
            self.assertTrue(one.finish_stop_events())
            one.stop()
            service.suspend_strategy("one")  # Runner/disconnect重复清理。
            self.assertNotIn("one", service._draining)
            self.assertNotIn("one", account.manager.repository.load().payload[
                "component_states"]["shared_account"]["shared"]["draining"])
            self.assertEqual(service.position_manager.position("one", "main"), 1)
            two.begin_stop(); service.poll(); two.finish_stop_events(); two.stop(); account.stop()

    def test_same_contract_two_owners_and_external_are_isolated_across_venues(self):
        modules = self.native_modules()
        for venue in ("CFFEX", "DCE", "CZCE", "SHFE", "INE"):
            with self.subTest(venue=venue), TemporaryDirectory() as directory:
                self.instrument = type(self.instrument).from_str("fixture."+venue)
                path = Path(directory)/"account.json"
                generic = (venue,) if venue in {"CFFEX", "DCE", "CZCE"} else ()
                account, service, transport, (one, two), _ = self.native_account(path, modules,
                    scoped=True, execution_mode="independent", preserve_external=True,
                    generic_close_venues=generic)
                transport.gross = {str(self.instrument): (2, 3)}
                account.start(resume=False); service.start(); one.start(); two.start(); account.authorize()
                self.submit(one, -1); transport.fill(transport.sent[-1]["OrderRef"])
                self.submit(two, -1); transport.fill(transport.sent[-1]["OrderRef"])
                self.assertEqual(transport.gross[str(self.instrument)], (2, 5))
                self.submit(one, 0, revision=2)
                self.assertEqual(transport.sent[-1]["VolumeTotalOriginal"], 1)
                self.assertEqual(transport.sent[-1]["CombOffsetFlag"], "1" if generic else "3")
                transport.fill(transport.sent[-1]["OrderRef"])
                self.assertEqual(transport.gross[str(self.instrument)], (2, 4))
                self.assertEqual(service.position_manager.position("one", "main"), 0)
                self.assertEqual(service.position_manager.position("two", "main"), -1)
                self.assertEqual(account.external_gross[str(self.instrument)], (2, 3))
                one.stop(); two.stop(); account.stop()
                restored, service2, transport2, _, _ = self.native_account(path, modules,
                    scoped=True, execution_mode="independent", preserve_external=True,
                    generic_close_venues=generic)
                transport2.gross = dict(transport.gross)
                restored.start(resume=True); restored.authorize()
                self.assertEqual(service2.position_manager.position("two", "main"), -1)
                self.assertFalse(transport2.sent)
                restored.stop()

    def test_generic_close_external_physical_buckets_are_not_strategy_ownership(self):
        modules = self.native_modules()
        self.instrument = type(self.instrument).from_str("MO2611-C-7100.CFFEX")
        with TemporaryDirectory() as directory:
            account, service, transport, (one, two), _ = self.native_account(Path(directory)/"account.json", modules,
                scoped=True, execution_mode="independent", preserve_external=True, generic_close_venues=("CFFEX",))
            key = str(self.instrument)
            transport.gross = {key: (0, 2)}
            account.start(resume=False); service.start(); one.start(); two.start(); account.authorize()
            self.submit(one, -1); transport.fill(transport.sent[-1]["OrderRef"])
            self.submit(two, 1); transport.fill(transport.sent[-1]["OrderRef"])
            # 柜台通用CLOSE的物理桶与逻辑成交桶可以不同，不能据此转移策略归属。
            def details():
                long, short = transport.gross[key]
                return [dict(InstrumentID="MO2611-C-7100", ExchangeID="CFFEX", Position=q,
                    PosiDirection=d, PositionDate=b, HedgeFlag="1")
                    for d, b, q in (("2", "1", long), ("3", "1", max(short-2, 0)), ("3", "2", min(short, 2))) if q]
            transport.query_position_details = details
            account._validate_account()
            self.submit(one, 0, revision=2); transport.fill(transport.sent[-1]["OrderRef"])
            account._validate_account()
            self.assertEqual(service.position_manager.position("one", "main"), 0)
            self.assertEqual(service.position_manager.position("two", "main"), 1)
            self.assertEqual(account.external_gross[key], (0, 2))
            self.assertEqual(account.external_buckets[key], (0, 0, 0, 2))
            one.stop(); two.stop(); account.stop()

    def test_independent_explicit_yesterday_close_uses_owned_ledger(self):
        from dataclasses import replace
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, two), _ = self.native_account(Path(directory)/"account.json", modules,
                scoped=True, execution_mode="independent", preserve_external=True)
            key = str(self.instrument)
            transport.gross = {key: (0, 2)}
            account.start(resume=False); service.start(); one.start(); two.start(); account.authorize()
            self.submit(one, -1); transport.fill(transport.sent[-1]["OrderRef"])
            old = account.ledger.snapshot(self.instrument)
            account.ledger.restore(modules.ledger.CtpLedgerState("20261013", {self.instrument: replace(old,
                short_today=Decimal(0), short_today_basis=Decimal(0),
                short_yesterday=old.short_total, short_yesterday_basis=old.short_today_basis)}))
            def details():
                short = transport.gross[key][1]
                return [dict(InstrumentID=key.split(".")[0], ExchangeID="SHFE", Position=q,
                    PosiDirection="3", PositionDate=b, HedgeFlag="1")
                    for b, q in (("1", 2), ("2", short-2)) if q]
            transport.query_position_details = details
            account._validate_account()
            self.submit(one, 0, revision=2)
            self.assertEqual(transport.sent[-1]["VolumeTotalOriginal"], 1)
            self.assertEqual(transport.sent[-1]["CombOffsetFlag"], "4")
            transport.fill(transport.sent[-1]["OrderRef"])
            self.assertEqual(transport.gross[key], (0, 2))
            self.assertEqual(service.position_manager.position("one", "main"), 0)
            one.stop(); two.stop(); account.stop()

    def test_native_independent_opposite_orders_open_separately_and_close_only_owner(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, two), _ = self.native_account(
                Path(directory) / "account.json", modules, scoped=True, execution_mode="independent")
            account.start(resume=False)
            service.start()
            one.start()
            two.start()
            account.authorize()
            self.submit(one, 1)
            transport.fill(transport.sent[-1]["OrderRef"])
            self.submit(two, -1)
            self.assertEqual([row["CombOffsetFlag"] for row in transport.sent], ["0", "0"])
            transport.fill(transport.sent[-1]["OrderRef"])
            self.assertEqual(transport.gross[str(self.instrument)], (1, 1))
            self.assertEqual((service.position_manager.position("one", "main"),
                              service.position_manager.position("two", "main")), (1, -1))
            self.assertFalse(service.position_manager.attribution.state()["transfers"])
            self.submit(one, 0, 2)
            self.assertEqual(transport.sent[-1]["CombOffsetFlag"], "3")
            transport.fill(transport.sent[-1]["OrderRef"])
            self.assertEqual(transport.gross[str(self.instrument)], (0, 1))
            self.assertEqual((service.position_manager.position("one", "main"),
                              service.position_manager.position("two", "main")), (0, -1))
            self.submit(one, -1, 3)
            self.assertEqual(transport.sent[-1]["CombOffsetFlag"], "0")
            transport.fill(transport.sent[-1]["OrderRef"])
            self.submit(one, 1, 4)
            self.assertEqual(transport.sent[-1]["CombOffsetFlag"], "3")
            transport.fill(transport.sent[-1]["OrderRef"])
            service.continue_targets("one", self.instrument, self.now)
            self.assertEqual(transport.sent[-1]["CombOffsetFlag"], "0")
            transport.fill(transport.sent[-1]["OrderRef"])
            self.assertEqual(transport.gross[str(self.instrument)], (1, 1))
            self.assertEqual((service.position_manager.position("one", "main"),
                              service.position_manager.position("two", "main")), (1, -1))
            self.assertFalse(account.client.report_errors)
            account.stop()
            state = service.snapshot_state()
            self.assertEqual(state["shared"]["execution_mode"], "independent")
            modified = json.loads(json.dumps(state))
            modified["shared"]["execution_mode"] = "netted"
            with self.assertRaisesRegex(ValueError, "模式"):
                service.restore_state(modified)

    def test_native_shared_order_cancelled_by_allocation_owner_and_other_target_replanned(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, two), _ = self.native_account(Path(directory) / "account.json", modules)
            account.start(resume=False)
            service.start()
            one.start()
            two.start()
            account.authorize()
            original = account.client.planner
            account.client.planner = type("Paused", (), {"plan": lambda *_: ()})()
            self.submit(one, 2)
            account.client.planner = original
            self.submit(two, 3)
            ref = transport.sent[0]["OrderRef"]
            self.assertEqual(transport.sent[0]["VolumeTotalOriginal"], 5)
            self.assertEqual(account.driver._orders[ref][1].strategy_id, "two")
            transport.fill(ref, 1)
            one.stop()  # 母单由two提交，one仍可按冻结份额撤销。
            self.assertEqual(transport.canceled, [ref])
            service.poll()
            service.continue_targets("two", self.instrument, self.now)
            self.assertEqual(transport.sent[1]["VolumeTotalOriginal"], 3)
            transport.fill(transport.sent[1]["OrderRef"])
            self.assertEqual((service.position_manager.position("one", "main"),
                service.position_manager.position("two", "main")), (1, 3))
            account.stop()
            self.assertEqual(account.final_active_orders, 0)

    def test_native_shared_partial_duplicate_cancel_and_peer_replan(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, two), _ = self.native_account(Path(directory) / "account.json", modules)
            account.start(resume=False)
            service.start()
            one.start()
            two.start()
            account.authorize()
            events = [[], []]
            for port, received in zip((one, two), events):
                port.register_execution_event_handler(received.append)
            original = account.client.planner
            account.client.planner = type("Paused", (), {"plan": lambda *_: ()})()
            self.submit(one, 2)
            account.client.planner = original
            self.submit(two, 3)
            ref = transport.sent[0]["OrderRef"]
            self.assertEqual(transport.sent[0]["VolumeTotalOriginal"], 5)
            raw = transport.raw(ref)
            transport.fill(ref, 3)  # one先分2手，two分1手；母单仍有2手。
            transport.on_trade({**raw, "TradeID": f"T{ref}-3", "Volume": 3,
                                "Price": transport.sent[0]["LimitPrice"]})
            for port in (one, two):
                port.drain_events()
            self.assertEqual((service.position_manager.position("one", "main"),
                              service.position_manager.position("two", "main")), (2, 1))
            self.assertEqual([sum(e.quantity for e in rows if isinstance(e, modules.events.FillEvent))
                              for rows in events], [2, 1])
            original_cancel = transport.cancel_order
            transport.cancel_order = lambda requested: None
            one.begin_stop()  # 母单由two提交；one份额已全成交，仍须等待母单撤单终态。
            self.assertFalse(one.finish_stop_events())
            self.assertEqual(len(transport.sent), 1)
            original_cancel(ref)
            service.poll()
            self.assertTrue(one.finish_stop_events())
            one.stop()
            two.drain_events()
            service.continue_targets("two", self.instrument, self.now)
            self.assertEqual(transport.sent[1]["VolumeTotalOriginal"], 2)
            transport.fill(transport.sent[1]["OrderRef"])
            two.drain_events()
            self.assertEqual((service.position_manager.position("one", "main"),
                              service.position_manager.position("two", "main")), (2, 3))
            self.assertEqual(service.position_manager.account_position("account", self.instrument), 5)
            self.assertEqual([sum(e.quantity for e in rows if isinstance(e, modules.events.FillEvent))
                              for rows in events], [2, 3])
            self.assertFalse(account.client.report_errors)
            self.assertEqual(one.event_diagnostics()["pending_events"], 0)
            self.assertTrue(any(e.status.value == "CANCELED" for e in events[0]
                                if isinstance(e, modules.events.OrderUpdateEvent)))
            account.stop()
            self.assertEqual(account.final_active_orders, 0)

    def test_native_shared_rejection_notifies_both_owners_and_closes_account(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, two), _ = self.native_account(
                Path(directory) / "account.json", modules, scoped=True)
            account.start(resume=False)
            service.start()
            one.start()
            two.start()
            account.authorize()
            events = [[], []]
            for port, received in zip((one, two), events):
                port.register_execution_event_handler(received.append)
            original = account.client.planner
            account.client.planner = type("Paused", (), {"plan": lambda *_: ()})()
            self.submit(one, 2)
            account.client.planner = original
            self.submit(two, 3)
            ref = transport.sent[0]["OrderRef"]
            rejected = {**transport.raw(ref), "OrderSubmitStatus": "4", "StatusMsg": "controlled rejection"}
            transport.on_order(rejected)
            transport.active.pop(ref)
            transport.on_order(rejected)  # 重复终态不重通知，也不改变归属。
            for port in (one, two):
                port.drain_events()
            for rows in events:
                self.assertEqual(sum(isinstance(e, modules.events.OrderUpdateEvent)
                    and e.status.value == "REJECTED" for e in rows), 1)
                self.assertFalse(any(isinstance(e, modules.events.FillEvent) for e in rows))
            self.assertFalse(account.client.is_armed)
            self.assertTrue(account.client.portfolio_failure)
            self.assertTrue(service.position_manager.is_recovery_required(service.client_id))
            self.assertEqual((service.position_manager.position("one", "main"),
                              service.position_manager.position("two", "main")), (0, 0))
            with self.assertRaises(RuntimeError):
                service.poll()
            before = service.snapshot_state()
            with self.assertRaises(RuntimeError):
                self.submit(one, 1, 2)
            with self.assertRaises(RuntimeError):
                self.submit(two, 1, 2)
            self.assertEqual(service.snapshot_state(), before)
            self.assertEqual(len(transport.sent), 1)
            self.assertFalse(account.driver.reconcile_active_orders().orders)

    def test_native_write_before_send_expiry_never_reaches_transport(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, two), _ = self.native_account(Path(directory) / "account.json", modules)
            account.start(resume=False)
            service.start()
            one.start()
            two.start()
            account.authorize()
            clock = self.now
            calls = [0]
            original = account.manager.save
            def save():
                value = original()
                calls[0] += 1
                if calls[0] == 2:
                    calls[0] = 100
                return value
            account.manager.save = save
            service._checkpoint = save
            # Driver安装的before_send使用Manager.save动态方法；第二次保存后时间前进。
            with patch("time.time_ns", side_effect=lambda: clock + (2_000_000_000 if calls[0] >= 100 else 0)):
                with self.assertRaises(RuntimeError):
                    self.submit(one, deadline_ns=clock + 1_000_000_000)
            self.assertFalse(transport.sent)
            self.assertTrue(service._failed)

    def test_native_account_checkpoint_restores_two_books_without_replaying_orders(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, service, transport, (one, two), _ = self.native_account(path, modules)
            account.start(resume=False)
            service.start()
            one.start()
            two.start()
            account.authorize()
            self.submit(one)
            transport.fill(transport.sent[0]["OrderRef"])
            self.submit(two, 2)
            transport.fill(transport.sent[1]["OrderRef"])
            one.stop()
            two.stop()
            account.stop()
            restored, service2, transport2, ports2, _ = self.native_account(path, modules)
            transport2.gross = dict(transport.gross)
            restored.start(resume=True)
            self.assertFalse(transport2.sent)
            self.assertEqual(service2.position_manager.position("one", "main"), 1)
            self.assertEqual(service2.position_manager.position("two", "main"), 2)
            self.assertEqual(service2._suspended, {"one", "two"})
            restored.stop()

    def test_readonly_reconciliation_repairs_finished_failed_checkpoint_without_sending(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, service, transport, (one, two), _ = self.native_account(path, modules, scoped=True)
            transport.gross = {"ag2612.SHFE": (0, 1)}
            account.start(resume=False)
            service.start()
            one.start()
            two.start()
            account.authorize()
            self.submit(one, 1)
            transport.fill(transport.sent[0]["OrderRef"])
            one.stop()
            two.stop()
            account.stop()
            # 模拟已记录新意图但发送失败；保存failed/pending及尚待冻结状态。
            stored = account.manager.repository.load()
            state = stored.payload["component_states"]["shared_account"]
            source = state["sources"]["one"]
            source["targets"][str(self.instrument)] = "-1"
            source["rows"][0]["quantity"] = "-1"
            state["failed"] = state["pending"] = True
            state["shared"]["draining"] = ["one", "two"]
            account.manager.repository.save(stored.payload, expected_generation=stored.generation)
            before = path.read_bytes()
            repaired, service2, transport2, _, _ = self.native_account(path, modules, scoped=True)
            transport2.gross = dict(transport.gross)
            result = repaired.reconcile_checkpoint()
            self.assertEqual(Path(result["backup_file"]).read_bytes(), before)
            self.assertFalse(transport2.sent)
            self.assertFalse(transport2.canceled)
            self.assertEqual(result["orders_submitted"], 0)
            self.assertFalse(result["authorized"])
            self.assertFalse(result["strategies_started"])
            saved = repaired.manager.repository.load().payload["component_states"]["shared_account"]
            self.assertFalse(saved["failed"])
            self.assertFalse(saved["pending"])
            self.assertFalse(saved["shared"]["draining"])
            self.assertEqual(saved["sources"]["one"]["revision"], source["revision"])
            self.assertEqual(saved["sources"]["one"]["targets"][str(self.instrument)], "1")
            self.assertEqual(saved["shared"]["suspended"], ["one", "two"])
            normal, service3, transport3, _, _ = self.native_account(path, modules, scoped=True)
            transport3.gross = dict(transport2.gross)
            normal.start(resume=True)
            self.assertFalse(transport3.sent)
            self.assertEqual(service3.position_manager.position("one", "main"), 1)
            normal.stop()

    def test_readonly_reconciliation_rejects_gross_conflict_without_changing_file(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, service, transport, _, _ = self.native_account(path, modules, scoped=True)
            account.start(resume=False)
            account.authorize()
            account.stop()
            stored = account.manager.repository.load()
            state = stored.payload["component_states"]["shared_account"]
            state["failed"] = state["pending"] = True
            account.manager.repository.save(stored.payload, expected_generation=stored.generation)
            before = path.read_bytes()
            repaired, _, transport2, _, _ = self.native_account(path, modules, scoped=True)
            transport2.gross = {str(self.instrument): (1, 1)}  # 净0但总仓不一致。
            with self.assertRaisesRegex(RuntimeError, "总仓"):
                repaired.reconcile_checkpoint()
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(transport2.sent)
            self.assertFalse(tuple(Path(directory).glob("*.before-reconcile-*.json")))

    def test_readonly_budget_migration_preserves_ownership_and_requires_new_resume_identity(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, service, transport, (one, two), _ = self.native_account(path, modules, scoped=True)
            old_account = {"client_id": "account", "max_gross_quantity": "10", "max_gross_notional": "100000"}
            new_account = {**old_account, "max_gross_notional": "120000"}
            account.manager.state_components["session_identity"].identity["account"] = old_account
            account.start(resume=False)
            service.start()
            one.start()
            two.start()
            account.authorize()
            self.submit(one)
            transport.fill(transport.sent[0]["OrderRef"])
            one.stop()
            two.stop()
            account.stop()
            before = path.read_bytes()
            repaired, service2, transport2, _, _ = self.native_account(path, modules, scoped=True)
            repaired.manager.state_components["session_identity"].identity["account"] = dict(old_account)
            transport2.gross = dict(transport.gross)
            result = repaired.reconcile_checkpoint(new_account_config=new_account)
            self.assertEqual(Path(result["backup_file"]).read_bytes(), before)
            self.assertFalse(transport2.sent)
            self.assertEqual(result["previous_budget"]["max_gross_notional"], "100000")
            self.assertEqual(result["saved_budget"]["max_gross_notional"], "120000")
            self.assertEqual(service2.position_manager.position("one", "main"), 1)
            saved = repaired.manager.repository.load().payload["component_states"]
            self.assertEqual(saved["session_identity"]["identity"]["account"], new_account)
            original, _, _, _, _ = self.native_account(path, modules, scoped=True)
            original.manager.state_components["session_identity"].identity["account"] = dict(old_account)
            with self.assertRaises(ValueError):
                original.start(resume=True)
            normal, service3, transport3, _, _ = self.native_account(path, modules, scoped=True)
            service3.budget = type(service3.budget)(10, 120000)
            normal.manager.state_components["session_identity"].identity["account"] = dict(new_account)
            transport3.gross = dict(transport.gross)
            normal.start(resume=True)
            self.assertFalse(transport3.sent)
            self.assertEqual(service3.position_manager.position("one", "main"), 1)
            normal.stop()

    def test_readonly_budget_migration_save_failure_rolls_back_budget_identity_and_file(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            old_account = {"client_id": "account", "max_gross_quantity": "10", "max_gross_notional": "100000"}
            new_account = {**old_account, "max_gross_notional": "120000"}
            account, _, _, _, _ = self.native_account(path, modules, scoped=True)
            account.manager.state_components["session_identity"].identity["account"] = dict(old_account)
            account.start(resume=False)
            account.authorize()
            account.stop()
            before = path.read_bytes()
            repaired, service, transport, _, _ = self.native_account(path, modules, scoped=True)
            repaired.manager.state_components["session_identity"].identity["account"] = dict(old_account)
            with patch.object(repaired.manager, "save", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    repaired.reconcile_checkpoint(new_account_config=new_account)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(service.budget.max_gross_notional, 100000)
            self.assertEqual(repaired.manager.state_components["session_identity"].identity["account"], old_account)
            self.assertFalse(transport.sent)
            self.assertEqual(len(tuple(Path(directory).glob("*.before-reconcile-*.json"))), 1)

    def test_readonly_reconciliation_rejects_unknown_active_orders_without_canceling(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, service, _, _, _ = self.native_account(path, modules)
            account.start(resume=False)
            account.authorize()
            account.stop()
            stored = account.manager.repository.load()
            state = stored.payload["component_states"]["shared_account"]
            state["failed"] = state["pending"] = True
            account.manager.repository.save(stored.payload, expected_generation=stored.generation)
            before = path.read_bytes()
            repaired, _, transport2, _, _ = self.native_account(path, modules)
            transport2.active["999"] = {"InstrumentID": "RB", "ExchangeID": "SHFE", "Direction": "0",
                "VolumeTotalOriginal": 1, "filled": 0}
            with self.assertRaises(RuntimeError):
                repaired.reconcile_checkpoint()
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(transport2.canceled)
            self.assertFalse(transport2.sent)

    def test_actual_runners_receive_scoped_events_and_stop_independently(self):
        modules = self.native_modules()
        service, client, ports = self.service()
        contracts = import_module("bomber.framework.trader.contracts")
        class Observer(modules.template.StrategyTemplate):
            def __init__(self, sid):
                super().__init__(sid)
                self.fills = []
            def on_fill(self, event):
                self.fills.append((event.identity.strategy_id, event.quantity, self.position("main")))
        runners, strategies = [], []
        for port in ports:
            runner = modules.runner.UnifiedStrategyRunner("live", position_manager=service.position_manager)
            runner.add_execution_client(port)
            strategy = Observer(port.strategy_id)
            runner.add_strategy(strategy, data_bindings=(), execution_routes=(
                contracts.ExecutionRoute("main", service.client_id, self.instrument),))
            runner.start()
            self.addCleanup(runner.stop)
            runners.append(runner)
            strategies.append(strategy)
        strategies[0].set_target("main", 1, self.now)
        client.backend.fill()
        ports[0].drain_events()
        ports[1].drain_events()
        self.assertEqual(strategies[0].fills, [("one", 1, 1)])
        self.assertFalse(strategies[1].fills)
        runners[0].stop()
        service.poll()
        strategies[1].set_target("main", 2, self.now)
        client.backend.fill(1)
        ports[1].drain_events()
        self.assertEqual(strategies[1].fills, [("two", 2, 2)])
        self.assertTrue(client.backend.running)

    def callback_host(self, root, *, prepare=None):
        modules = self.native_modules()
        contracts = import_module("bomber.framework.trader.contracts")
        account, service, transport, ports, resources = self.native_account(root / "account.json", modules)
        class Observer(modules.template.StrategyTemplate):
            def __init__(self, sid):
                super().__init__(sid)
                self.fills = []
            def on_fill(self, event):
                self.fills.append((event.event_id, event.quantity, self.position("main")))
        rows = []
        for port in ports:
            runner = modules.runner.UnifiedStrategyRunner("live", position_manager=service.position_manager)
            runner.add_execution_client(port)
            strategy = Observer(port.strategy_id)
            runner.add_strategy(strategy, data_bindings=(), execution_routes=(
                contracts.ExecutionRoute("main", service.client_id, self.instrument),))
            runner.failure, runner.fixed_spec, runner.accept_bars = None, "fixed", False
            runner.session_ready = lambda: True
            runner.begin_bars = lambda runner=runner: setattr(runner, "accept_bars", True)
            session = NS(runner=runner, strategy=strategy, client=port,
                         references=NS(spec="fixed", refresh=lambda: None))
            rows.append(modules.runtime.SharedStrategySession(session, root / (port.strategy_id + ".json"),
                {"id": port.strategy_id, "routes": port.configuration()}))
        host = modules.runtime.SharedAccountLiveHost(account, service, rows, resources=resources)
        if prepare is not None:
            prepare(host, transport, rows)
        host.start()
        return host, transport, rows

    def test_poll_exception_captures_health_before_strategy_feed_is_stopped(self):
        with TemporaryDirectory() as directory:
            host, transport, rows = self.callback_host(Path(directory))
            runner = rows[0].session.runner
            contracts = import_module('bomber.framework.trader.runtime.live.contracts')
            source = contracts.ChannelIdentity('market', 'dolphindb', 'fixture')
            result = contracts.ReadinessResult((source,), (contracts.ReadinessIssue(
                source, 'EVENT_STALE', '行情超过允许时效', {'age_seconds':121}),))
            runner._session_check = NS(last_result=result)
            def fail():
                raise RuntimeError('共享执行预算缺少当前有效参考价: instrument=other.DCE')
            runner.poll_live_inputs = fail
            stop = runner.stop
            def cleanup():
                runner._session_check.last_result = None
                stop()
            runner.stop = cleanup
            try:
                host.poll()
                self.assertFalse(rows[0].active)
                self.assertTrue(rows[1].active)
                context = runner.failure_readiness
                self.assertEqual(context['channel_readiness']['issues'][0]['code'], 'EVENT_STALE')
                self.assertIn('instrument=other.DCE', context['error'])
                self.assertIn('instrument=other.DCE', runner.failure)
                self.assertEqual(context['error_type'], 'RuntimeError')
                self.assertIsNone(runner._session_check.last_result)
            finally:
                host.stop()

    def test_host_history_runs_with_bars_closed_before_account_authorization(self):
        calls = []
        def configure(host, transport, rows):
            runner = rows[0].session.runner
            runner.mark_history_live_start = lambda: calls.append("boundary")
            def warm():
                self.assertFalse(runner.accept_bars)
                self.assertFalse(host.account.client.is_armed)
                self.assertEqual(transport.sent, [])
                calls.append("history")
            runner.prepare_history = warm
        with TemporaryDirectory() as directory:
            host, transport, rows = self.callback_host(Path(directory), prepare=configure)
            try:
                self.assertEqual(calls, ["boundary", "history"])
                self.assertTrue(rows[0].session.runner.accept_bars)
                self.assertTrue(host.account.client.is_armed)
                self.assertEqual(transport.sent, [])
            finally:
                host.stop()

    def test_host_preopen_warms_without_quotes_waits_unarmed_then_rechecks_at_open(self):
        calls, captured = [], []
        def configure(host, transport, rows):
            captured.append(host)
            ready = [False]
            for row in rows:
                runner = row.session.runner
                runner.session_ready = lambda: ready[0]
                runner.mark_history_live_start = lambda: None
                def warm(runner=runner):
                    self.assertFalse(runner.accept_bars)
                    self.assertFalse(host.account.client.is_armed)
                    calls.append('history')
                runner.prepare_history = warm
            def wait():
                self.assertEqual(calls, ['history', 'history'])
                for row in rows:
                    row.save(host.service)  # 回调也不能在账户事务提交前写信号文件。
                self.assertFalse(host.account.client.is_armed)
                self.assertTrue(all(not row.session.runner.accept_bars for row in rows))
                self.assertTrue(all(not row.state_file.exists() for row in rows))
                host.poll_preparation()  # 尚无新行情时仍维护账户，不以行情就绪为等待前提。
                self.assertEqual(transport.sent, [])
                calls.append('open')
                ready[0] = True
            host.activation_wait = wait
        with TemporaryDirectory() as directory:
            host, transport, rows = self.callback_host(Path(directory), prepare=configure)
            try:
                self.assertEqual(calls, ['history', 'history', 'open'])
                self.assertTrue(host.account.client.is_armed)
                self.assertTrue(all(row.session.runner.accept_bars for row in rows))
                self.assertTrue(all(row.state_file.exists() for row in rows))
                self.assertEqual(transport.sent, [])
            finally:
                host.stop()

    def test_preopen_operator_stop_never_authorizes_or_advances_signal_files(self):
        from bomber.framework.trader.runtime.live.continuous import StopRequested
        captured = []
        def configure(host, transport, rows):
            captured.extend((host, transport, rows))
            def wait():
                self.assertFalse(host.account.client.is_armed)
                raise StopRequested()
            host.activation_wait = wait
        with TemporaryDirectory() as directory:
            with self.assertRaises(StopRequested):
                self.callback_host(Path(directory), prepare=configure)
            host, transport, rows = captured
            self.assertFalse(host._prepared)
            self.assertFalse(host.account.client.is_armed)
            self.assertEqual(transport.sent, [])
            self.assertTrue(all(not row.session.runner.accept_bars for row in rows))
            self.assertTrue(all(not row.state_file.exists() for row in rows))

    def test_host_history_failure_or_lost_health_never_authorizes_or_opens_bars(self):
        for lose_health in (False, True):
            with self.subTest(lose_health=lose_health), TemporaryDirectory() as directory:
                captured = []
                def configure(host, transport, rows):
                    captured.extend((host, transport, rows))
                    runner = rows[0].session.runner
                    runner.mark_history_live_start = lambda: None
                    def warm():
                        if lose_health:
                            runner.session_ready = lambda: False
                        else:
                            raise RuntimeError("missing history")
                    runner.prepare_history = warm
                with self.assertRaises(RuntimeError):
                    self.callback_host(Path(directory), prepare=configure)
                host, transport, rows = captured
                self.assertFalse(host._prepared)
                self.assertFalse(host.account.client.is_armed)
                self.assertEqual(transport.sent, [])
                self.assertTrue(all(not row.session.runner.accept_bars for row in rows))

    def test_ema_history_updates_real_indicators_without_targets_or_account_orders(self):
        modules = self.native_modules()
        history = import_module("demos.01_main_ema.history")
        checkpoint = import_module("demos.01_main_ema.checkpoint")
        history_api = import_module("bomber.framework.dataprep.history")
        from threading import RLock
        stamps = (60_000_000_000 - 1, 120_000_000_000 - 1, 180_000_000_000 - 1)
        class Provider:
            def open(self): pass
            def close(self): pass
            def read(self, request):
                return tuple(history_api.HistoryBar(str(self_instrument), stamp,
                    adjusted_close=Decimal(price)) for stamp, price in zip(stamps, (100, 102, 104)))
        self_instrument = self.instrument
        class Factory:
            @staticmethod
            def create_for(purpose, backend, config):
                return Provider()
        with TemporaryDirectory() as directory:
            host, transport, rows = self.callback_host(Path(directory))
            try:
                strategy = rows[0].session.strategy
                strategy.fast = checkpoint.AnchoredEma(2, 0, 0)
                strategy.slow = checkpoint.AnchoredEma(3, 0, 0)
                strategy.bars_used, strategy.last_processed_ns = 0, -1
                service = history_api.HistoryService(database_config={}, factory=Factory)
                runner = NS(ema_checkpoint=checkpoint.EmaCheckpoint(strategy, self.instrument),
                    calendar=NS(last_minutes=lambda before, count: stamps), history_service=service,
                    history_minutes=3, _submit_lock=RLock(), _clients={"one": rows[0].session.client})
                history.warm_history(runner, 180_000_000_000, phase="recovery")
                self.assertTrue(strategy.slow.initialized)
                self.assertEqual(strategy.bars_used, 3)
                self.assertEqual(strategy._revision, 0)
                self.assertEqual(transport.sent, [])
                self.assertEqual(service.last_audit["sources"], ("dolphindb",))
                self.assertEqual(host.service.position_manager.position("one", "main"), 0)
            finally:
                host.stop()

    def test_host_stop_delivers_fill_already_queued_before_unbinding(self):
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(TemporaryDirectory())
            host, transport, rows = self.callback_host(Path(directory))
            cleanup.callback(host.stop)
            strategy = rows[0].session.strategy
            strategy.set_target("main", 1, self.now)
            transport.fill(transport.sent[0]["OrderRef"])
            self.assertFalse(strategy.fills)  # 账户已入账，控制线程尚未消费。
            host.stop_strategy("one", reason="configured_stop")
            self.assertEqual([row[1:] for row in strategy.fills], [(1, 1)])
            self.assertFalse(rows[0].session.client._events)
            self.assertTrue(rows[1].active)
            self.assertTrue(rows[1].session.strategy.is_started)
            host.poll()
            self.assertEqual(len(strategy.fills), 1)
            diagnostics = rows[0].session.client.event_diagnostics()
            self.assertEqual((diagnostics["fills_enqueued"], diagnostics["fills_delivered"],
                              diagnostics["pending_events"]), (1, 1, 0))
            self.assertEqual(diagnostics["last_fill_enqueued"], diagnostics["last_fill_delivered"])
            self.assertTrue(diagnostics["closed"])
            self.assertLessEqual(rows[0].stop_requested_at_ns, rows[0].stopped_at_ns)

    def test_host_stop_waits_for_late_partial_fill_and_cancel_before_unbinding(self):
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(TemporaryDirectory())
            host, transport, rows = self.callback_host(Path(directory))
            cleanup.callback(host.stop)
            strategy = rows[0].session.strategy
            strategy.set_target("main", 2, self.now)
            original_cancel = transport.cancel_order
            pending = []
            transport.cancel_order = pending.append
            def deliver(_seconds):
                self.assertTrue(strategy.is_started)
                self.assertTrue(rows[0].active)
                self.assertTrue(pending)
                ref = pending.pop()
                transport.fill(ref, 1)
                original_cancel(ref)
            module = import_module("bomber.framework.trader.runtime.live.shared")
            with patch.object(module.time, "sleep", side_effect=deliver):
                host.stop_strategy("one")
            self.assertEqual([row[1:] for row in strategy.fills], [(1, 1)])
            self.assertFalse(rows[0].session.client._events)
            self.assertFalse(host.service._draining)
            self.assertTrue(rows[1].active)
            self.now += 1
            rows[1].session.strategy.set_target("main", 1, self.now)
            transport.fill(transport.sent[-1]["OrderRef"])
            host.poll()
            self.assertEqual(len(rows[1].session.strategy.fills), 1)

    def test_host_stop_timeout_keeps_runner_bound_and_rejects_new_target(self):
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(TemporaryDirectory())
            host, transport, rows = self.callback_host(Path(directory))
            cleanup.callback(host.stop)
            strategy = rows[0].session.strategy
            strategy.set_target("main", 1, self.now)
            original_cancel = transport.cancel_order
            transport.cancel_order = lambda ref: None
            host.account.cancel_seconds = 0
            with self.assertRaisesRegex(RuntimeError, "保留绑定及账户锁"):
                host.stop_strategy("one")
            self.assertTrue(strategy.is_started)
            self.assertTrue(rows[0].active)
            self.assertFalse(rows[0].session.client.event_diagnostics()["closed"])
            with self.assertRaisesRegex(RuntimeError, "正在停止"):
                self.submit(rows[0].session.client, 2, 2)
            original_cancel(transport.sent[0]["OrderRef"])
            host.stop_strategy("one")
            self.assertFalse(rows[0].session.client._events)

    def test_host_stop_starts_both_cancellations_before_waiting_for_first(self):
        with TemporaryDirectory() as directory:
            host, transport, rows = self.callback_host(Path(directory))
            first = rows[0].session.client
            finish = first.finish_stop_events
            def check_all_stopped():
                self.assertTrue(all(not row.session.runner.accept_bars for row in rows))
                self.assertTrue(all(row.session.client._stopping for row in rows))
                return finish()
            first.finish_stop_events = check_all_stopped
            host.stop()
            self.assertTrue(all(not row.active for row in rows))
            self.assertTrue(host._stopped)

    def test_host_final_reconciliation_finishes_late_cancel_without_losing_fill_or_position(self):
        with TemporaryDirectory() as directory:
            host, transport, rows = self.callback_host(Path(directory))
            strategy = rows[0].session.strategy
            strategy.set_target("main", 2, self.now)
            ref = transport.sent[0]["OrderRef"]
            cancel = transport.cancel_order
            calls = []
            def late_cancel(order_ref):
                calls.append(order_ref)
                if len(calls) >= 2:
                    transport.fill(order_ref, 1)
                    cancel(order_ref)  # 首次超时后，账户停机阶段才收到终态。
            transport.cancel_order = late_cancel
            stop = host.stop_strategy
            first = [True]
            def expire_first(sid, **kwargs):
                if sid == "one" and first[0]:
                    first[0] = False
                    seconds = host.account.cancel_seconds
                    host.account.cancel_seconds = 0
                    try:
                        return stop(sid, **kwargs)
                    finally:
                        host.account.cancel_seconds = seconds
                return stop(sid, **kwargs)
            host.stop_strategy = expire_first
            host.stop()
            self.assertTrue(host._stopped)
            self.assertFalse(rows[0].active)
            self.assertEqual([row[1:] for row in strategy.fills], [(1, 1)])
            self.assertEqual(host.service.position_manager.position("one", "main"), 1)
            self.assertEqual(transport.gross[str(self.instrument)], (1, 0))
            self.assertFalse(transport.active)
            self.assertEqual(rows[0].session.client.event_diagnostics()["pending_fills"], 0)
            saved = host.account.manager.repository.load().payload
            self.assertFalse(saved["ctp_drivers"]["account"]["orders"])
            self.assertTrue(all(row["status"] in {"FILLED", "CANCELED", "REJECTED"}
                for row in saved["orders"]["account"]))
            self.assertEqual(len(transport.sent), 1)  # 停机不平仓、不续发目标。

    def test_scheduled_query_interruption_stops_readonly_and_clears_guard_for_final_reconciliation(self):
        with TemporaryDirectory() as directory:
            host, transport, rows = self.callback_host(Path(directory))
            from bomber.framework.trader.execution.ctp.query_errors import CtpQueryInterrupted
            query = transport.query_account
            stop = [False]
            transport.query_stop_requested = lambda: stop[0]
            def interrupted():
                if transport.query_stop_requested is not None and stop[0]:
                    raise CtpQueryInterrupted("fixture scheduled stop")
                return query()
            transport.query_account = interrupted
            host.account._next_check = 0
            stop[0] = True
            with self.assertRaises(CtpQueryInterrupted):
                host.account.poll()
            self.assertFalse(host.account.client.is_armed)
            host.stop()
            self.assertIsNone(transport.query_stop_requested)
            self.assertTrue(host._stopped)
            self.assertEqual(host.account.final_active_orders, 0)
            self.assertFalse(transport.sent)
            self.assertFalse(host.account.execution.positions.is_recovery_required('account'))

    def test_window_check_after_slow_account_poll_blocks_strategy_work(self):
        from bomber.framework.trader.runtime.live.continuous import StopRequested
        with TemporaryDirectory() as directory:
            host, transport, rows = self.callback_host(Path(directory))
            elapsed = [False]
            def check():
                if elapsed[0]:
                    raise StopRequested()
            host.preparation_check = check
            original = host.account.poll
            def slow_poll():
                original()
                elapsed[0] = True
            host.account.poll = slow_poll
            with self.assertRaises(StopRequested):
                host.poll()
            self.assertFalse(transport.sent)
            host.preparation_check = None
            host.stop()

    def test_window_check_after_slow_reference_refresh_propagates_normal_stop(self):
        from bomber.framework.trader.runtime.live.continuous import StopRequested
        with TemporaryDirectory() as directory:
            host, transport, rows = self.callback_host(Path(directory))
            elapsed = [False]
            def check():
                if elapsed[0]:
                    raise StopRequested()
            host.preparation_check = check
            rows[0].session.references.refresh = lambda: elapsed.__setitem__(0, True)
            with self.assertRaises(StopRequested):
                host.poll()
            self.assertTrue(all(row.active for row in rows))
            self.assertFalse(transport.sent)
            host.preparation_check = None
            host.stop()

    def test_callback_failure_keeps_event_pending_and_closes_account(self):
        service, client, (one, two) = self.service()
        self.submit(one)
        client.backend.fill()
        def fail(event):
            raise RuntimeError("callback failed")
        one.register_execution_event_handler(fail)
        one.begin_stop()
        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            one.finish_stop_events()
        self.assertTrue(service._failed)
        self.assertEqual(one.event_diagnostics()["fills_delivered"], 0)
        self.assertEqual(one.event_diagnostics()["pending_fills"], 1)

    def test_cancel_probe_selects_partial_shared_mother_and_keeps_peer_running(self):
        from scripts.integration.shared_account.cancel_probe import SharedMotherCancelProbe
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(TemporaryDirectory())
            host, transport, rows = self.callback_host(Path(directory))
            cleanup.callback(host.stop)
            original = host.account.client.planner
            host.account.client.planner = type("Paused", (), {"plan": lambda *_: ()})()
            rows[0].session.strategy.set_target("main", 2, self.now)
            host.account.client.planner = original
            rows[1].session.strategy.set_target("main", 3, self.now)
            ref = transport.sent[0]["OrderRef"]
            transport.fill(ref, 3)
            probe = SharedMotherCancelProbe("one")
            probe.poll(host)
            self.assertEqual(transport.canceled, [ref])
            self.assertFalse(rows[0].active)
            self.assertTrue(rows[1].active)
            self.assertEqual(rows[0].failure, "acceptance_shared_cancel")
            self.assertEqual(probe.mother["submitting_strategy"], "two")
            self.assertEqual(len(transport.sent), 1)  # 观察入口没有制造额外目标/订单。
            self.now += 1
            host.service.continue_targets("two", self.instrument, self.now)
            self.assertEqual(transport.sent[1]["VolumeTotalOriginal"], 2)
            transport.fill(transport.sent[1]["OrderRef"])
            host.poll()
            result = probe.result(host, runtime_status="completed")
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["partial_fill_observed"])
            self.assertEqual(result["peer_fills_after_stop"], 1)
            self.assertGreater(result["peer_target_continuations_after_stop"], 0)
            self.assertEqual(result["extra_targets_submitted"], 0)
            probe.poll(host)
            self.assertEqual(transport.canceled, [ref])
            self.assertEqual(probe.result(host, runtime_status="failed")["status"], "failed")

    def test_cancel_probe_does_not_stop_for_filled_or_single_owner_orders(self):
        from scripts.integration.shared_account.cancel_probe import SharedMotherCancelProbe
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(TemporaryDirectory())
            host, transport, rows = self.callback_host(Path(directory))
            cleanup.callback(host.stop)
            rows[0].session.strategy.set_target("main", 1, self.now)
            probe = SharedMotherCancelProbe("one")
            probe.poll(host)
            self.assertIsNone(probe.mother)
            transport.fill(transport.sent[0]["OrderRef"])
            probe.poll(host)
            self.assertIsNone(probe.mother)
            self.assertEqual(probe.result(host, runtime_status="completed")["status"], "not_exercised")
            self.assertTrue(rows[0].active)
            self.assertTrue(rows[1].active)
            self.assertFalse(transport.canceled)

    def test_cancel_probe_fill_racing_cancel_is_not_a_cancel_pass(self):
        from scripts.integration.shared_account.cancel_probe import SharedMotherCancelProbe
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(TemporaryDirectory())
            host, transport, rows = self.callback_host(Path(directory))
            cleanup.callback(host.stop)
            original = host.account.client.planner
            host.account.client.planner = type("Paused", (), {"plan": lambda *_: ()})()
            rows[0].session.strategy.set_target("main", 1, self.now)
            host.account.client.planner = original
            rows[1].session.strategy.set_target("main", 1, self.now)
            transport.cancel_order = lambda ref: transport.fill(ref)
            probe = SharedMotherCancelProbe("one")
            probe.poll(host)
            result = probe.result(host, runtime_status="completed")
            self.assertEqual(result["status"], "not_exercised")
            self.assertFalse(result["canceled_report_ids"])
            self.assertTrue(rows[1].active)

    def test_host_single_strategy_restart_keeps_peer_revision_position_and_td(self):
        modules = self.native_modules()
        contracts = import_module("bomber.framework.trader.contracts")
        class Observer(modules.template.StrategyTemplate):
            pass
        with TemporaryDirectory() as directory:
            root = Path(directory)
            account, service, transport, ports, resources = self.native_account(root / "account.json", modules)
            def session(port):
                runner = modules.runner.UnifiedStrategyRunner("live", position_manager=service.position_manager)
                runner.add_execution_client(port)
                strategy = Observer(port.strategy_id)
                runner.add_strategy(strategy, data_bindings=(), execution_routes=(
                    contracts.ExecutionRoute("main", service.client_id, self.instrument),))
                runner.failure, runner.fixed_spec, runner.accept_bars = None, "fixed", False
                runner.session_ready = lambda: port.is_reconciled
                runner.begin_bars = lambda: setattr(runner, "accept_bars", True)
                references = NS(spec="fixed", refresh=lambda: None)
                return NS(runner=runner, strategy=strategy, client=port, references=references)
            rows = [modules.runtime.SharedStrategySession(session(port), root / (port.strategy_id + ".json"),
                {"id": port.strategy_id, "routes": port.configuration()}) for port in ports]
            host = modules.runtime.SharedAccountLiveHost(account, service, rows, resources=resources)
            host.start()
            rows[0].session.strategy.set_target("main", 1, self.now)
            transport.fill(transport.sent[0]["OrderRef"])
            rows[1].session.strategy.set_target("main", 2, self.now)
            transport.fill(transport.sent[1]["OrderRef"])
            host.poll()
            peer = service.snapshot_state()["sources"]["two"]
            host.stop_strategy("one")
            host.poll()
            module = import_module("bomber.framework.trader.execution.shared")
            port = service.strategy_client("one", routes={"main": self.instrument},
                budget=module.SharedRiskBudget(5, 50000))
            fresh = session(port)
            history_calls = []
            fresh.runner.mark_history_live_start = lambda: history_calls.append("boundary")
            fresh.runner.prepare_history = lambda: history_calls.append("history")
            host.restart_strategy("one", fresh)
            self.assertEqual(history_calls, ["boundary", "history"])
            self.assertEqual(service.snapshot_state()["sources"]["two"], peer)
            self.assertEqual(service.position_manager.position("two", "main"), 2)
            self.assertEqual(transport.starts, 1)
            self.now += 1
            fresh.strategy.set_target("main", 0, self.now)
            transport.fill(transport.sent[2]["OrderRef"])
            host.poll()
            self.assertEqual(service.snapshot_state()["sources"]["one"]["revision"], 2)
            self.assertEqual(service.position_manager.position("one", "main"), 0)
            host.stop()

    def test_full_host_resume_after_one_strategy_stopped_without_signal_preserves_peer_book(self):
        modules = self.native_modules()
        contracts = import_module("bomber.framework.trader.contracts")
        class Observer(modules.template.StrategyTemplate):
            pass
        with TemporaryDirectory() as directory:
            root = Path(directory)
            def build(*, resume, gross=None):
                account, service, transport, ports, resources = self.native_account(root / "account.json", modules, scoped=True)
                transport.gross = dict(gross or {"ag2612.SHFE": (0, 1)})
                rows = []
                for port in ports:
                    runner = modules.runner.UnifiedStrategyRunner("live", position_manager=service.position_manager)
                    runner.add_execution_client(port)
                    strategy = Observer(port.strategy_id)
                    runner.add_strategy(strategy, data_bindings=(), execution_routes=(
                        contracts.ExecutionRoute("main", service.client_id, self.instrument),))
                    runner.failure, runner.fixed_spec, runner.accept_bars = None, "fixed", False
                    runner.session_ready = lambda port=port: port.is_reconciled
                    runner.begin_bars = lambda runner=runner: setattr(runner, "accept_bars", True)
                    rows.append(modules.runtime.SharedStrategySession(NS(runner=runner, strategy=strategy,
                        client=port, references=NS(spec="fixed", refresh=lambda: None)),
                        root / (port.strategy_id + ".json"), {"id": port.strategy_id, "routes": port.configuration()}))
                return modules.runtime.SharedAccountLiveHost(account, service, rows, resources=resources, resume=resume), transport
            first, transport = build(resume=False)
            first.start()
            first.stop_strategy("one", reason="configured_stop")
            first.strategies["two"].session.strategy.set_target("main", 2, self.now)
            transport.fill(transport.sent[0]["OrderRef"])
            first.poll()
            first.stop()
            restarted, transport2 = build(resume=True, gross=transport.gross)
            restarted.start()
            restarted.poll()
            service = restarted.service
            self.assertFalse(transport2.sent)  # 恢复旧目标不发单。
            self.assertNotIn("one", service.snapshot_state()["sources"])
            self.assertEqual(service.position_manager.position("two", "main"), 2)
            self.assertEqual(service.position_manager.position("one", "main"), 0)
            self.assertEqual(service.position_manager.account_position("account", fixtures.Instrument("ag2612.SHFE")), -1)
            self.assertEqual(restarted.strategies["two"].session.strategy._revision, 1)
            self.now += 1
            restarted.strategies["one"].session.strategy.set_target("main", 1, self.now)
            transport2.fill(transport2.sent[0]["OrderRef"])
            restarted.poll()
            self.assertEqual(service.position_manager.position("one", "main"), 1)
            self.assertEqual(service.position_manager.position("two", "main"), 2)
            self.assertEqual(service.snapshot_state()["sources"]["two"]["revision"], 1)
            self.assertEqual(service.position_manager.account_position("account", self.instrument), 3)
            restarted.stop_strategy("one")
            restarted.poll()
            self.now += 1
            restarted.strategies["two"].session.strategy.set_target("main", 1, self.now)
            transport2.fill(transport2.sent[1]["OrderRef"])
            restarted.poll()
            self.assertEqual(service.snapshot_state()["sources"]["two"]["revision"], 2)
            self.assertEqual(service.position_manager.position("one", "main"), 1)
            self.assertEqual(service.position_manager.position("two", "main"), 1)
            self.assertEqual(transport2.starts, 1)
            restarted.stop()

    def test_restored_route_corruption_is_rejected_before_state_mutation(self):
        service, client, (one, two) = self.service()
        self.submit(one)
        state = service.snapshot_state()
        state["sources"]["one"]["rows"][0]["target_key"] = "foreign"
        service._leases = 0
        before = service.snapshot_state()
        with self.assertRaises(ValueError):
            service.restore_state(state)
        self.assertEqual(service.snapshot_state(), before)

    def test_existing_ctp_driver_regression(self):
        self.native_modules()
        import_module("tests.run_p4_ctp_driver").main()

    def test_existing_ctp_autosave_regression(self):
        self.native_modules()
        import_module("tests.run_p4_ctp_autosave").main()

    def test_existing_ctp_recovery_regression(self):
        self.native_modules()
        import_module("tests.run_p4_ctp_recovery").main()

    def test_expiry_before_internal_transfer_does_not_change_owned_books(self):
        service, client, (one, two) = self.service()
        original = client.planner
        client.planner = type("Paused", (), {"plan": lambda *_: ()})()
        deadline = self.now + 1_000_000_000
        self.submit(one, 1, deadline_ns=deadline)
        client.planner = original
        expired = [False]
        risk_check = client.risk_manager.check
        def risk(*args, **kwargs):
            result = risk_check(*args, **kwargs)
            expired[0] = True
            return result
        client.risk_manager.check = risk
        with patch("time.time_ns", side_effect=lambda: deadline + 1 if expired[0] else self.now):
            with self.assertRaises(RuntimeError):
                self.submit(two, -1, deadline_ns=deadline)
        self.assertEqual(service.position_manager.position("one", "main"), 0)
        self.assertEqual(service.position_manager.position("two", "main"), 0)
        self.assertFalse(client.backend.sent)

    def test_orphan_logical_position_cannot_become_a_shared_account_fact(self):
        service, client, (one, two) = self.service()
        service.position_manager.set_strategy_position("one", "main", 1)
        with self.assertRaises(ValueError):
            service.validate_owned_state()

    def test_host_snapshot_exposes_execution_gate_conflict_without_reconciling(self):
        modules = self.native_modules()
        service, client, _ = self.service()
        service._failed = True
        client._report_errors.append(RuntimeError("conflicting fill sequence"))
        service.position_manager.mark_recovery_required(service.client_id)
        host = object.__new__(modules.runtime.SharedAccountLiveHost)
        host.service, host.strategies = service, {}
        state = host.snapshot()["execution_diagnostics"]
        self.assertTrue(state["service_failed"])
        self.assertTrue(state["recovery_required"])
        self.assertEqual(state["report_errors"], ["conflicting fill sequence"])

    def test_readiness_failure_context_keeps_false_readiness_and_per_contract_ages(self):
        self.native_modules()
        module = import_module("bomber.framework.trader.runtime.live.health")
        contracts = import_module("bomber.framework.trader.runtime.live.contracts")
        identity = contracts.ChannelIdentity("market", "ctp", "cross-md")
        result = contracts.ReadinessResult((identity,), (contracts.ReadinessIssue(identity,
            "INSTRUMENT_UNHEALTHY", "AU", {"receive_age_seconds": 11, "max_age_seconds": 10}),))
        self.assertFalse(result)
        stale = {"AU": {"receive_age_seconds": 11, "max_age_seconds": 10}}
        runner = NS(_session_check=NS(last_result=result), failure=None, stale_instruments=lambda: stale,
            references=NS(spec="original"), fixed_spec="original")
        context = module.readiness_failure_context(runner)
        self.assertEqual(context["channel_readiness"]["issues"][0]["code"], "INSTRUMENT_UNHEALTHY")
        self.assertEqual(context["stale_instruments"], stale)
        self.assertTrue(context["reference_current"])
        self.assertIsNone(runner.failure)

    def test_scoped_native_account_preserves_external_positions_across_fill_stop_and_restore(self):
        modules = self.native_modules()
        external = {"MO2611-C-7400.CFFEX": (0, 1), "sc2611.INE": (1, 0),
            "ni2611.SHFE": (1, 0), "i2701.DCE": (0, 1), "ag2612.SHFE": (0, 1),
            "cu2611.SHFE": (8, 8)}  # 超出管理预算、净仓为零的外部双向仓也必须保存。
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, service, transport, (one, two), _ = self.native_account(path, modules, scoped=True)
            transport.gross = dict(external)
            account.start(resume=False)
            service.start()
            one.start()
            two.start()
            account.authorize()
            self.submit(one)
            transport.fill(transport.sent[0]["OrderRef"])
            self.assertEqual(service.position_manager.position("one", "main"), 1)
            self.assertEqual(service.position_manager.position("two", "main"), 0)
            self.assertEqual(set(account.ledger.state().positions), {self.instrument})
            for symbol, (long, short) in external.items():
                instrument = fixtures.Instrument(symbol)
                self.assertEqual(service.position_manager.account_position("account", instrument), long - short)
                self.assertEqual(service.position_manager.unassigned_position("account", instrument), long - short)
            self.assertTrue(all(row["instrument_id"] == str(self.instrument)
                for row in service.position_manager.attribution.state()["owned"]))
            one.stop()
            two.stop()
            account.stop()
            self.assertEqual(account.scope_diagnostics()["final_managed_gross"], {str(self.instrument): (1, 0)})
            self.assertEqual(account.external_gross, external)
            restored, service2, transport2, _, _ = self.native_account(path, modules, scoped=True)
            transport2.gross = dict(transport.gross)
            restored.start(resume=True)
            self.assertEqual(service2.position_manager.position("one", "main"), 1)
            self.assertEqual(restored.external_gross, external)
            self.assertFalse(transport2.sent)
            restored.stop()

    def test_default_account_scope_still_rejects_existing_external_position(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, _, transport, _, _ = self.native_account(path, modules)
            transport.gross = {"ag2612.SHFE": (0, 1)}
            with self.assertRaisesRegex(RuntimeError, "账户已有总仓"):
                account.start(resume=False)
            self.assertFalse(transport.sent)
            self.assertFalse(path.exists())

    def test_scoped_new_session_rejects_managed_hedge_even_with_zero_net(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, _, transport, _, _ = self.native_account(path, modules, scoped=True)
            transport.gross = {str(self.instrument): (1, 1)}
            with self.assertRaisesRegex(RuntimeError, "账户已有总仓"):
                account.start(resume=False)
            self.assertFalse(transport.sent)
            self.assertFalse(path.exists())

    def test_scoped_external_gross_change_with_same_net_closes_gate_without_reassigning(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, _, _ = self.native_account(Path(directory) / "account.json", modules, scoped=True)
            transport.gross = {"ag2612.SHFE": (1, 1)}
            account.start(resume=False)
            account.authorize()
            transport.gross["ag2612.SHFE"] = (2, 2)
            account._next_check = 0
            with self.assertRaisesRegex(RuntimeError, "范围外持仓"):
                account.poll()
            self.assertFalse(account.client.is_armed)
            self.assertTrue(service.position_manager.is_recovery_required("account"))
            self.assertEqual(account.external_gross, {"ag2612.SHFE": (1, 1)})
            self.assertFalse(service.position_manager.attribution.state()["owned"])
            self.assertFalse(transport.sent)

    def test_scoped_external_today_yesterday_change_blocks_restore_and_preserves_file(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            account, _, transport, _, _ = self.native_account(path, modules, scoped=True)
            transport.gross = {"ag2612.SHFE": (0, 1)}
            account.start(resume=False)
            account.authorize()
            account.stop()
            before = path.read_bytes()
            restored, _, transport2, _, _ = self.native_account(path, modules, scoped=True)
            transport2.gross = dict(transport.gross)
            query = transport2.query_position_details
            transport2.query_position_details = lambda: [{**row, "PositionDate": "2", "TodayPosition": 0}
                                                         for row in query()]
            with self.assertRaisesRegex(RuntimeError, "范围外持仓"):
                restored.start(resume=True)
            restored.abort_preparation()
            restored.stop()
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(transport2.sent)

    def test_scoped_backend_rejects_external_instrument_before_transport(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, _, transport, _, _ = self.native_account(Path(directory) / "account.json", modules, scoped=True)
            account.start(resume=False)
            account.authorize()
            order = self.contracts.OrderIntent(strategy_id="one", backend_id="account",
                instrument_id=fixtures.Instrument("ag2612.SHFE"), side="BUY", quantity=1)
            with self.assertRaisesRegex(RuntimeError, "订单合约不在"):
                account.execution.backend.submit_order(order)
            self.assertFalse(transport.sent)

    def test_scoped_external_active_order_closes_gate_and_is_not_canceled(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, _, transport, _, _ = self.native_account(Path(directory) / "account.json", modules, scoped=True)
            account.start(resume=False)
            account.authorize()
            transport.active["999"] = {"InstrumentID": "ag2612", "ExchangeID": "SHFE", "Direction": "1",
                "VolumeTotalOriginal": 1, "filled": 0}
            account._next_check = 0
            with self.assertRaisesRegex(RuntimeError, "范围外活动订单"):
                account.poll()
            self.assertFalse(account.client.is_armed)
            self.assertFalse(transport.canceled)
            self.assertFalse(transport.sent)

    def test_scoped_checkpoint_rejects_managed_instrument_as_external_without_mutation(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, _, _, _, _ = self.native_account(Path(directory) / "account.json", modules, scoped=True)
            before = account.snapshot_state()
            invalid = {**before, "gross": {str(self.instrument): ["1", "0"]},
                "buckets": {str(self.instrument): ["1", "0", "0", "0"]}}
            with self.assertRaisesRegex(ValueError, "范围外持仓检查点"):
                account.restore_state(invalid)
            self.assertEqual(account.snapshot_state(), before)

    def test_scoped_poll_does_not_treat_managed_fill_between_queries_as_external_conflict(self):
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            account, service, transport, (one, two), _ = self.native_account(Path(directory) / "account.json", modules, scoped=True)
            transport.gross = {"ag2612.SHFE": (0, 1)}
            account.start(resume=False)
            service.start()
            one.start()
            two.start()
            account.authorize()
            self.submit(one)
            query = transport.query_position_details
            def during_query():
                transport.fill(transport.sent[0]["OrderRef"])
                transport.query_position_details = query
                return query()
            transport.query_position_details = during_query
            account._next_check = 0
            account.poll()
            self.assertTrue(account.client.is_armed)
            self.assertEqual(service.position_manager.position("one", "main"), 1)
            one.stop()
            two.stop()
            account.stop()

    def test_application_binds_union_of_strategy_routes_to_all_shared_components(self):
        modules = self.native_modules()
        from scripts.integration.shared_account import run, factories
        from contextlib import redirect_stdout
        from io import StringIO
        with TemporaryDirectory() as directory:
            # 初始化实际builder导出；仅传输、行情/资料工厂及锁获取使用测试边界。
            _, _, transport, _, _ = self.native_account(Path(directory) / "unused.json", modules, scoped=True)
            transport.gross = {"ag2612.SHFE": (0, 1)}
            package = sys.modules["bomber.framework.trader.execution.ctp"]
            package.CtpNativeTraderDriver = modules.driver.CtpNativeTraderDriver
            package.CtpTdApiTransport = object
            ctp_channel = import_module("bomber.framework.trader.runtime.live.channels.ctp")
            instruments = {"one": self.instrument, "two": fixtures.Instrument("HC.SHFE")}
            spec = NS(tick=Decimal(1), multiplier=Decimal(1))
            def prepare(args, context, lifecycle):
                return NS(references=NS(instrument_id=args.instrument, spec=spec), upstream=NS())
            def assemble(args, references, driver, upstream, *, shared_service, strategy_id):
                port = shared_service.strategy_client(strategy_id, routes={"main": args.instrument},
                    budget=import_module("bomber.framework.trader.execution.shared").SharedRiskBudget(2, 50000))
                return NS(client=port, strategy=NS(strategy_id=strategy_id),
                    runner=NS(session_ready=lambda: True), references=references)
            entry = NS(prepare_inputs=prepare, assemble=assemble)
            def arguments(row, config, *, resume):
                return entry, NS(instrument=instruments[row["strategy_id"]])
            config = SharedConfigTests().config()
            config.update(environment="realtime", seconds=1, position_scope="strategy_instruments",
                account_state_file=str(Path(directory) / "account.json"))
            for row in config["strategies"]:
                row["state_file"] = str(Path(directory) / row["state_file"])
            diagnostics = {}
            with patch.object(ctp_channel, "build_simnow_transport", return_value=transport), \
                    patch.object(ctp_channel, "required", return_value="tcp://fake:30011"), \
                    patch.object(modules.lifecycle.CtpSharedAccountLifecycle, "acquire"), \
                    patch.object(factories, "strategy_arguments", side_effect=arguments), redirect_stdout(StringIO()):
                host = run.build_host(config, resume=False, diagnostics=diagnostics)
            self.addCleanup(host.resources.close)
            self.assertEqual(diagnostics["managed_instruments"], ["HC.SHFE", "RB.SHFE"])
            self.assertEqual(diagnostics["external_gross"], transport.gross)
            self.assertEqual(host.service.instrument_scope, host.account.position_scope)
            self.assertEqual(host.account.position_scope, host.account.execution.backend.instrument_scope)
            self.assertFalse(host.service.client.is_armed)
            self.assertFalse(transport.sent)
            host.account.start(resume=False)
            host.account.stop()
            # 运行调度不属于策略/账户恢复身份；改变argv仍改变身份。
            original_identity = host.account.manager.state_components["session_identity"].snapshot_state()
            original_strategy_identity = {sid: row.identity for sid, row in host.strategies.items()}
            host.resources.close()  # 原宿主已停止；释放新增的检查点文件锁后重建。
            config["seconds"] = 600
            config["ctp_instrument_max_age_seconds"] = 30
            config["strategies"][0]["stop_after_seconds"] = 480
            with patch.object(ctp_channel, "build_simnow_transport", return_value=transport), \
                    patch.object(ctp_channel, "required", return_value="tcp://fake:30011"), \
                    patch.object(modules.lifecycle.CtpSharedAccountLifecycle, "acquire"), \
                    patch.object(factories, "strategy_arguments", side_effect=arguments), redirect_stdout(StringIO()):
                resumed = run.build_host(config, resume=True, diagnostics={})
            self.addCleanup(resumed.resources.close)
            self.assertEqual(resumed.account.manager.state_components["session_identity"].snapshot_state(), original_identity)
            self.assertEqual({sid: row.identity for sid, row in resumed.strategies.items()}, original_strategy_identity)
            resumed.account.start(resume=True)
            self.assertFalse(transport.sent)
            resumed.account.stop()


class SharedConfigTests(unittest.TestCase):
    def test_factory_allows_ema_history_on_fresh_and_shared_resume_only(self):
        from scripts.integration.shared_account import factories
        calls = []
        parsed = NS(history_minutes=5, history_config=Path("cu-history.json"),
                    history_file=None, run_forever=False)
        def parse(argv, **kwargs):
            calls.append((argv, kwargs))
            return parsed
        entry = NS(parse_args=parse)
        row = {"kind": "main_ema", "argv": ["--history-minutes", "5"], "state_file": "ema.json"}
        config = {"trading_day": "20261015", "seconds": 60, "environment": "realtime"}
        with patch.object(factories, "import_module", return_value=entry):
            for resume in (False, True):
                self.assertIs(factories.strategy_arguments(row, config, resume=resume)[1], parsed)
                self.assertEqual(calls[-1][1], {"shared_execution": True})
                self.assertEqual("--resume" in calls[-1][0], resume)
            row["kind"] = "calendar_spread"
            self.assertIs(factories.strategy_arguments(row, config, resume=False)[1], parsed)
            self.assertEqual(calls[-1][1], {"shared_execution": True})
            row["kind"] = "cross_section"
            self.assertIs(factories.strategy_arguments(row, config, resume=False)[1], parsed)
            row["kind"] = "option_vega"
            with self.assertRaisesRegex(ValueError, "仅支持01"):
                factories.strategy_arguments(row, config, resume=False)
            self.assertEqual(calls[-1][1], {})
            row["kind"] = "main_ema"
            parsed.run_forever = True
            with self.assertRaises(ValueError):
                factories.strategy_arguments(row, config, resume=False)

    def config(self):
        return {"version": 1, "trading_day": "20261013", "account_state_file": "account.json",
            "account": {"client_id": "shared", "max_gross_quantity": "4", "max_gross_notional": "100000",
                "max_session_orders": 20, "limit_offset_ticks": 1},
            "strategies": [{"strategy_id": sid, "kind": "main_ema", "state_file": sid + ".json",
                "argv": ["--product", "RB", "--expected-source-day", "20261012"]} for sid in ("one", "two")]}

    def read(self, config):
        from scripts.integration.shared_account.config import read_config
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config))
            return read_config(path)

    def test_config_has_distinct_absolute_state_files_and_no_sdk_import(self):
        config = self.read(self.config())
        self.assertTrue(Path(config["account_state_file"]).is_absolute())
        self.assertEqual(len(config["strategies"]), 2)

    def test_position_scope_is_explicit_and_invalid_policies_are_rejected(self):
        self.assertNotIn("position_scope", self.read(self.config()))
        config = self.config()
        config["position_scope"] = "strategy_instruments"
        self.assertEqual(self.read(config)["position_scope"], "strategy_instruments")
        for value in ("all_existing", "RB,HC", None):
            config["position_scope"] = value
            with self.assertRaisesRegex(ValueError, "position_scope"):
                self.read(config)

    def test_strategy_only_account_limits_require_explicit_independent_mode(self):
        config = self.config()
        config["execution_mode"] = "independent"
        config["account"].update(max_gross_quantity=None, max_gross_notional=None, max_session_orders=200)
        parsed = self.read(config)
        self.assertIsNone(parsed["account"]["max_gross_quantity"])
        self.assertIsNone(parsed["account"]["max_gross_notional"])
        self.assertEqual(parsed["account"]["max_session_orders"], 200)
        config["execution_mode"] = "netted"
        with self.assertRaisesRegex(ValueError, "independent"):
            self.read(config)
        config["execution_mode"] = "independent"
        config["account"]["max_gross_quantity"] = 4
        with self.assertRaisesRegex(ValueError, "同时为null"):
            self.read(config)

    def test_duplicate_ids_files_and_execution_overrides_are_rejected(self):
        for mutation in (lambda c: c["strategies"][1].update(strategy_id="one"),
                         lambda c: c["strategies"][0].update(state_file="account.json"),
                         lambda c: c["strategies"][0]["argv"].append("--mode=model")):
            config = self.config()
            mutation(config)
            with self.assertRaises(ValueError):
                self.read(config)

    def test_order_authorization_is_explicit(self):
        from scripts.integration.shared_account.run import parse_args
        from contextlib import redirect_stderr
        from io import StringIO
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_args(["--config", "example.json", "--connect"])
        self.assertFalse(parse_args(["--config", "example.json"]).connect)

    def test_reconcile_only_requires_connection_resume_and_forbids_order_authorization(self):
        from scripts.integration.shared_account.run import parse_args
        from contextlib import redirect_stderr
        from io import StringIO
        valid = ["--config", "session.json", "--connect", "--resume", "--reconcile-only"]
        self.assertTrue(parse_args(valid).reconcile_only)
        for argv in (valid + ["--enable-orders", "--confirm-simnow"],
                [arg for arg in valid if arg != "--resume"], [arg for arg in valid if arg != "--connect"]):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parse_args(argv)

    def test_cancel_probe_requires_order_authorization_and_declared_owner(self):
        from scripts.integration.shared_account import run
        parse_args = run.parse_args
        from contextlib import redirect_stderr
        from io import StringIO
        valid = ["--config", "session.json", "--connect", "--enable-orders", "--confirm-simnow",
                 "--resume", "--acceptance-cancel-owner", "one"]
        self.assertEqual(parse_args(valid).acceptance_cancel_owner, "one")
        for argv in ([arg for arg in valid if arg != "--connect"],
                     [arg for arg in valid if arg != "--enable-orders"],
                     valid + ["--reconcile-only"]):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parse_args(argv)
        unknown = valid[:-1] + ["unknown"]
        with patch.object(run, "read_config", return_value=self.read(self.config())), \
                patch.object(run, "build_host") as build:
            with self.assertRaisesRegex(ValueError, "已声明"):
                run.main(unknown)
            build.assert_not_called()

    def test_budget_migration_only_allows_account_notional_increase(self):
        from copy import deepcopy
        from scripts.integration.shared_account.config import validate_budget_increase
        original = self.read(self.config())
        updated = deepcopy(original)
        updated["account"]["max_gross_notional"] = "120000"
        self.assertEqual(validate_budget_increase(original, updated), updated["account"])
        for mutate in (lambda c: c["account"].update(max_gross_notional="90000"),
                lambda c: c["account"].update(max_gross_quantity="5"),
                lambda c: c["strategies"][0]["argv"].extend(("--max-notional", "120000")),
                lambda c: c.update(account_state_file="another.json")):
            candidate = deepcopy(updated)
            mutate(candidate)
            with self.assertRaises(ValueError):
                validate_budget_increase(original, candidate)
        from scripts.integration.shared_account.run import parse_args
        from contextlib import redirect_stderr
        from io import StringIO
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parse_args(["--config", "old.json", "--budget-config", "new.json"])

    def test_missing_config_reports_path_without_starting_host(self):
        from scripts.integration.shared_account import run
        from contextlib import redirect_stderr
        from io import StringIO
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "session.json"
            output = StringIO()
            with patch.object(run, "build_host") as build, redirect_stderr(output), self.assertRaises(SystemExit) as raised:
                run.main(["--config", str(missing), "--connect", "--enable-orders", "--confirm-simnow"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn(str(missing), output.getvalue())
            self.assertIn("未建立任何连接", output.getvalue())
            build.assert_not_called()

    def test_td_mismatch_exposes_actual_day_and_retains_diagnostics(self):
        from scripts.integration.shared_account.run import validate_td_preflight
        from contextlib import redirect_stdout
        from io import StringIO
        config, driver, diagnostics = self.config(), NS(trading_day="20261014", submitted_orders=0), {}
        config["environment"] = "realtime"
        output = StringIO()
        with redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "actual=20261014 expected=20261013"):
            validate_td_preflight(config, driver, md_front="tcp://fake:30011", td_front="tcp://fake:30001",
                                  diagnostics=diagnostics)
        self.assertEqual(diagnostics["observed_td_trading_day"], "20261014")
        self.assertEqual(diagnostics["preflight_orders_submitted"], 0)
        self.assertIn("actual_TD=20261014", output.getvalue())
        self.assertEqual(config["trading_day"], "20261013")

    def test_matching_td_day_keeps_original_replay_time_semantics(self):
        from scripts.integration.shared_account.run import validate_td_preflight
        from contextlib import redirect_stdout
        from io import StringIO
        config = {**self.config(), "environment": "replay", "replay_md_trading_day": "20260930"}
        diagnostics = {}
        with redirect_stdout(StringIO()):
            validate_td_preflight(config, NS(trading_day="20261013", submitted_orders=0),
                md_front="tcp://fake:40011", td_front="tcp://fake:40001", diagnostics=diagnostics)
        self.assertEqual(diagnostics["observed_td_trading_day"], "20261013")
        self.assertEqual(config["replay_md_trading_day"], "20260930")

    def test_build_failure_still_reports_td_preflight_without_host(self):
        from scripts.integration.shared_account import run
        from contextlib import redirect_stdout
        from io import StringIO
        reported = {}
        class Report:
            def __init__(self, *args, **kwargs): pass
            def begin(self): pass
            def write(self, **kwargs): reported.update(kwargs)
        replacements = {"dotenv": NS(load_dotenv=lambda *_: None)}
        for name in ("bomber", "bomber.framework", "bomber.framework.trader", "bomber.framework.trader.runtime"):
            module = ModuleType(name)
            module.__path__ = []
            replacements[name] = module
        replacements["bomber.framework.trader.runtime.reports"] = NS(LiveRunReport=Report)
        def fail(config, *, resume, diagnostics):
            run.validate_td_preflight(config, NS(trading_day="20261014", submitted_orders=0),
                md_front="tcp://fake:30011", td_front="tcp://fake:30001", diagnostics=diagnostics)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text(json.dumps({**self.config(), "environment": "realtime"}))
            with patch.dict(sys.modules, replacements), patch.object(run, "build_host", side_effect=fail), \
                    redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "actual=20261014"):
                run.main(["--config", str(path), "--connect", "--enable-orders", "--confirm-simnow"])
        self.assertEqual(reported["status"], "failed")
        self.assertIsNone(reported["session"])
        self.assertEqual(reported["fields"]["td_preflight"]["observed_td_trading_day"], "20261014")

    def test_shutdown_failure_preserves_primary_failure_and_pre_shutdown_gate(self):
        from scripts.integration.shared_account import run
        from contextlib import redirect_stdout
        from io import StringIO
        reported = {}
        class Report:
            def __init__(self, *args, **kwargs): pass
            def begin(self): pass
            def write(self, **kwargs): reported.update(kwargs)
        replacements = {"dotenv": NS(load_dotenv=lambda *_: None)}
        for name in ("bomber", "bomber.framework", "bomber.framework.trader", "bomber.framework.trader.runtime"):
            module = ModuleType(name)
            module.__path__ = []
            replacements[name] = module
        replacements["bomber.framework.trader.runtime.reports"] = NS(LiveRunReport=Report)
        class Host:
            strategies = {}
            account = NS(final_gross=None, final_active_orders=None, scope_diagnostics=lambda: {},
                execution=NS(backend=NS(reports=())))
            def start(self): raise RuntimeError("initial readiness failure")
            def stop(self): raise RuntimeError("shutdown incomplete")
            def snapshot(self): return {"execution_diagnostics": {"report_errors": ["bad sequence"]}}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text(json.dumps(self.config()))
            with patch.dict(sys.modules, replacements), patch.object(run, "build_host", return_value=Host()), \
                    redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "shutdown incomplete"):
                run.main(["--config", str(path), "--connect", "--enable-orders", "--confirm-simnow"])
        self.assertEqual(reported["status"], "failed")
        self.assertEqual(reported["fields"]["failure"], "initial readiness failure")
        self.assertEqual(reported["fields"]["shutdown_failure"], "shutdown incomplete")
        self.assertEqual(reported["fields"]["failure_execution_diagnostics"]["report_errors"], ["bad sequence"])

    def test_cli_reconcile_only_does_not_start_or_authorize_strategy_host(self):
        from scripts.integration.shared_account import run
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import Mock
        reported = {}
        class Report:
            def __init__(self, *args, **kwargs): pass
            def begin(self): pass
            def write(self, **kwargs): reported.update(kwargs)
        replacements = {"dotenv": NS(load_dotenv=lambda *_: None)}
        for name in ("bomber", "bomber.framework", "bomber.framework.trader", "bomber.framework.trader.runtime",
                "bomber.framework.trader.runtime.live"):
            module = ModuleType(name)
            module.__path__ = []
            replacements[name] = module
        replacements["bomber.framework.trader.runtime.reports"] = NS(LiveRunReport=Report)
        replacements["bomber.framework.trader.runtime.live.local"] = NS(strategy_state_lock=Mock())
        account = NS(reconcile_checkpoint=Mock(return_value={"orders_submitted": 0}),
            client=NS(stop=Mock()), driver=NS(stop=Mock()), authorize=Mock(),
            final_gross={}, final_active_orders=0, scope_diagnostics=lambda: {}, execution=NS(backend=NS(reports=())))
        host = NS(account=account, strategies={}, resources=ExitStack(), start=Mock(), stop=Mock(), snapshot=lambda: {})
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text(json.dumps(self.config()))
            with patch.dict(sys.modules, replacements), patch.object(run, "build_host", return_value=host) as build, \
                    redirect_stdout(StringIO()):
                run.main(["--config", str(path), "--connect", "--resume", "--reconcile-only"])
        self.assertTrue(build.call_args.kwargs["read_only"])
        self.assertFalse(build.call_args.kwargs["strategy_processes"])
        host.start.assert_not_called()
        account.authorize.assert_not_called()
        self.assertEqual(reported["status"], "reconciled")
        self.assertEqual(reported["fields"]["verification"], "checkpoint_reconciled_no_strategy_no_orders")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text(json.dumps({**self.config(), "execution_mode": "independent"}))
            with patch.dict(sys.modules, replacements), patch.object(run, "build_host", return_value=host) as build, \
                    redirect_stdout(StringIO()):
                run.main(["--config", str(path), "--connect", "--resume", "--reconcile-only"])
        self.assertTrue(build.call_args.kwargs["strategy_processes"])
        host.start.assert_not_called()
        account.authorize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
