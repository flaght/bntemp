"""08共享执行及跨日恢复；真实公共组件，柜台/行情/SDK实体使用离线夹具。"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from bomber.framework.market.basic.base import InstrumentId, make_bar
from bomber.framework.trader.execution.builders import build_ctp_portfolio_execution
from bomber.framework.trader.execution.ctp.native_driver import CtpNativeTraderDriver
from bomber.framework.trader.execution.shared import SharedAccountExecutionService, SharedRiskBudget
from bomber.framework.trader.persistence import JsonStateStore, StatePersistenceError
from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle
from bomber.framework.trader.runtime.live.contracts import LiveRunContext
from bomber.framework.trader.runtime.live.shared import SharedAccountLiveHost, SharedStrategySession
from scripts.integration.shared_account import factories, rollover
from tests import test_option_simnow_migrate as fixture
from tests.test_option_simnow_migrate import Transport, inputs, entry, runtime, CLIENT, OPTION


class DetailsTransport(Transport):
    is_test_transport = False
    def query_position_details(self):
        return super().query_position_details() + [dict(InstrumentID="ag2612", ExchangeID="SHFE", Position=1,
            PosiDirection="3", PositionDate=self.bucket, HedgeFlag="1", PositionCost=10000)]


class OptionSharedRolloverTests(unittest.TestCase):
    setUp = fixture.MigrationTests.setUp
    identity = fixture.MigrationTests.identity

    def test_legacy_index_metadata_addition_requires_same_verified_declaration(self):
        import json
        from bomber.framework.dataprep.features import DEFAULT_FEATURE_CONFIG
        from bomber.framework.datahub.features.schema import dataset_from_definition
        module = import_module("demos.08_option_vega.rollover")
        spec = dataset_from_definition(json.loads(DEFAULT_FEATURE_CONFIG.read_text())["datasets"][0]["schema"])
        old, new = deepcopy(self.identity), deepcopy(self.new_identity)
        index = old["sources"]["index"]
        index.update(channel="dolphindb-dfs", dataset_id=spec.dataset_id, schema_version=spec.schema_version,
            schema_fingerprint=spec.fingerprint)
        index.pop("alignment", None); index.pop("computation_version", None)
        new["sources"]["index"] = {**index, "alignment": "EXACT", "computation_version": spec.computation_version}
        # 本次实际问题发生在DolphinDB到DolphinDB，不是CTP来源迁移。
        old["sources"]["market"] = deepcopy(new["sources"]["market"])
        old["labels"] = deepcopy(new["labels"])
        before = deepcopy(old)
        module.validate_identity(old, new)
        self.assertEqual(old, before)
        for key, value in (("alignment", "ASOF"), ("computation_version", "other-calculation"),
                           ("schema_fingerprint", "other"), ("table", "other"), ("availability", "source-time")):
            changed = deepcopy(new); changed["sources"]["index"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "差异字段"):
                module.validate_identity(old, changed)
        # 已存在的字段不能覆盖，包括显式声明的旧计算版本。
        declared = deepcopy(old); declared["sources"]["index"]["computation_version"] = "older-explicit"
        with self.assertRaisesRegex(ValueError, "差异字段"):
            module.validate_identity(declared, new)

    def standalone(self, transport=None):
        args = deepcopy(self.new_args); args.state_file = self.source; args.resume = True
        transport = transport or DetailsTransport("20260814")
        recovery = import_module("demos.08_option_vega.recovery")
        with patch("bomber.framework.trader.execution.ctp.CtpNativeTraderDriver", CtpNativeTraderDriver, create=True):
            lifecycle = recovery.OptionCtpLifecycle(transport, md_front=None, orders=True, resume=True,
                scope_to_reference_instruments=True, expected_trading_day="20260814", valuation_day="20260814",
                state_file=self.source, require_fills=False)
        resources = ExitStack(); self.addCleanup(resources.close)
        context = lifecycle.prepare(resources); context.references = self.current.references
        lifecycle.finish_preparation(context)
        session = runtime.assemble(args, self.current, lifecycle.driver)
        return lifecycle, session, context, transport

    def frame(self, session, slot="13:35"):
        stamp = int(datetime.fromisoformat(str(session.references.service.valuation_day) + "T" + slot + ":00").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()) * 1_000_000_000
        now = [stamp + 14_000_000_000]
        session.runner.clock_ns = lambda: now[0]
        for key, price in (("MO2609-C-6600", 82.2), ("IM2609", 6500), ("000852", 6400)):
            instrument = next(i for i, symbol in session.runner.key_by_instrument.items() if symbol == key)
            feed = session.runner.sources[key]
            session.runner.publish(feed, make_bar(instrument_id=instrument, open=price, high=price, low=price,
                close=price, volume=1, ts_event=stamp, ts_init=stamp+14_000_000_000,
                meta=session.feeds[feed].get_instrument_meta(instrument)))
        now[0] += 21_000_000_000
        session.runner.flush_frames()

    def shared(self, path=None, current=None, transport=None, preserve_external=False):
        current = current or self.current
        day = current.references.service.valuation_day.strftime("%Y%m%d")
        args = deepcopy(self.new_args); args.expected_trading_day = args.valuation_day = day
        transport = transport or DetailsTransport(day)
        driver = CtpNativeTraderDriver(CLIENT, "fixture", transport, enable_simnow_orders=True,
            disconnect_handler=lambda reason: None)
        specs = factories.instrument_specs("option_vega", current.references)
        multipliers = {i: s.multiplier for i, s in specs.items()}
        limits = runtime.risk_limits(self.new_args.config, current.references.contracts,
            current.references.instrument_ids, multipliers, self.new_args.max_notional)
        scope = runtime.InstrumentPositionScope(specs)
        execution = build_ctp_portfolio_execution(driver, trading_day=day,
            price_increments={i: s.tick for i, s in specs.items()}, multipliers=multipliers,
            instrument_limits=limits, session_check=lambda: True, orders=True, instrument_scope=scope,
            account_reconciliation="observe" if preserve_external else "strict",
            generic_close_venues=("CFFEX",), generic_close_today_first_venues=("CFFEX",))
        service = SharedAccountExecutionService(execution.client, prices=execution.prices,
            instrument_limits=limits, budget=SharedRiskBudget(10, 10000000), instrument_scope=scope,
            execution_mode="independent")
        session = factories.assemble_strategy_session(entry, args, current, service=service,
            driver=driver, strategy_id=CLIENT)
        identity = dict(strategy_id=CLIENT, kind="option_vega", argv=["unchanged"], trading_day=day,
            environment="realtime", routes=session.client.configuration(), reference=session.session_identity)
        resources = ExitStack(); self.addCleanup(resources.close)
        account_identity = dict(trading_day=day, environment="realtime", strategies={CLIENT: identity})
        if preserve_external:
            account_identity["existing_position_policy"] = "preserve_external"
        account = CtpSharedAccountLifecycle(execution, service, transport=transport, state_file=path or self.source,
            identity=account_identity,
            resources=resources, expected_day=day, position_scope=scope,
            rollover_transform=rollover.payload_for_day, preserve_external=preserve_external)
        account.option_sessions = {CLIENT: session}
        return account, service, session, identity, transport

    def test_host_defers_rollover_commit_until_signal_restore_succeeds(self):
        account, service, session, identity, transport = self.shared()
        transport.bucket = "2"
        item = SharedStrategySession(session, self.root/"worker.json", identity)
        item.restore = Mock(side_effect=ValueError("signal restore failed"))
        resources = ExitStack()
        self.addCleanup(resources.close)
        host = SharedAccountLiveHost(account, service, [item], resources=resources, resume=True)
        with self.assertRaisesRegex(ValueError, "signal restore failed"):
            host.start()
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertTrue(account.manager.describe_checkpoint()["restore_pending"])
        self.assertFalse(account.client.is_armed)
        self.assertFalse(transport.sent)

    def test_account_preparation_preserves_original_until_explicit_authorization(self):
        account, _, _, _, transport = self.shared()
        transport.bucket = "2"
        account.start_preparation(resume=True)
        try:
            self.assertEqual(self.source.read_bytes(), self.before)
            self.assertTrue(account.manager.describe_checkpoint()["restore_pending"])
            self.assertFalse(account.client.is_armed)
            account.authorize()
            self.assertFalse(account.manager.describe_checkpoint()["restore_pending"])
            self.assertNotEqual(self.source.read_bytes(), self.before)
        finally:
            account.stop()

    def test_normal_resume_rolls_authoritative_yesterday_keeps_original_and_can_select_today(self):
        transport = DetailsTransport("20260814"); transport.bucket = "2"
        lifecycle, session, context, transport = self.standalone(transport)
        lifecycle.start(session, context)
        self.assertTrue(lifecycle.resume_ready)
        self.assertEqual(session.ledger.snapshot(InstrumentId.from_str(OPTION)).short_yesterday, 1)
        self.assertEqual(session.strategy.position(OPTION), -1)
        self.assertNotIn(date(2026, 8, 14), session.strategy.sent_days)
        self.assertFalse(session.strategy.latest)
        self.assertEqual(self.source.with_name(self.source.name+".before-rollover-g1.json").read_bytes(), self.before)
        self.frame(session)
        self.assertEqual(session.strategy.frames, 1)
        self.assertEqual(session.strategy.option_goals["MO2609-C-6600"], -1)
        self.assertFalse(transport.sent)
        session.client._wall_clock_ns = lambda: session.runner.clock_ns()
        self.frame(session, "13:36")
        self.assertIn(date(2026, 8, 14), session.strategy.sent_days)
        self.assertEqual(session.strategy.entry_audit[-1]["outcome"], "RISK_LIMIT")
        # 原仓已占单合约1手上限；每日累加受限仍须继续管理原仓。
        self.assertEqual(session.strategy.option_goals["MO2609-C-6600"], -1)
        self.assertEqual(session.strategy.position(OPTION), -1)
        self.assertFalse(transport.sent)
        lifecycle.shutdown(session, context)
        self.assertEqual(JsonStateStore(self.source).load().payload["ctp_ledgers"][CLIENT]["trading_day"], "20260814")

    def test_bad_cost_or_gross_conflict_preserves_original_and_does_not_submit(self):
        for mode in ("cost", "gross"):
            transport = DetailsTransport("20260814")
            if mode == "cost":
                transport.query_position_details = lambda: [dict(InstrumentID="MO2609-C-6600", ExchangeID="CFFEX",
                    Position=1, PosiDirection="3", PositionDate="2", HedgeFlag="1", PositionCost=0)]
            else:
                transport.query_gross_positions = lambda: {OPTION: (Decimal(0), Decimal(2))}
            lifecycle, session, context, _ = self.standalone(transport)
            with self.assertRaisesRegex((ValueError, RuntimeError), "成本|总仓"):
                lifecycle.start(session, context)
            lifecycle.shutdown(session, context)
            context.resources.close()
            self.assertEqual(self.source.read_bytes(), self.before)
            self.assertFalse(transport.sent)

    def test_staged_restore_cannot_save_before_authoritative_commit(self):
        lifecycle, session, context, _ = self.standalone()
        lifecycle.restore_session(session, context)
        with self.assertRaisesRegex(StatePersistenceError, "核验"):
            session.manager.save()
        self.assertEqual(self.source.read_bytes(), self.before)
        lifecycle.shutdown(session, context)

    def test_config_or_index_source_change_refuses_before_changing_original(self):
        for field in ("config", "index"):
            lifecycle, session, context, transport = self.standalone()
            if field == "config": session.session_identity["config"]["nav"] = 1
            else: session.session_identity["sources"]["index"]["table"] = "unconfirmed"
            with self.assertRaisesRegex(ValueError, "配置|指数"):
                lifecycle.start(session, context)
            lifecycle.shutdown(session, context)
            context.resources.close()
            self.assertEqual(self.source.read_bytes(), self.before)
            self.assertFalse(transport.sent)

    def test_import_native_into_shared_restores_same_owner_and_signal_then_manages_fresh_frame(self):
        account, service, session, identity, transport = self.shared()
        transport.bucket = "2"
        account.start(resume=True)
        self.assertEqual(service.position_manager.position(CLIENT, OPTION), -1)
        self.assertEqual(account.ledger.snapshot(InstrumentId.from_str(OPTION)).short_yesterday, 1)
        item = SharedStrategySession(session, self.root/"worker.json", identity)
        item.restore(service, resume=True)
        service.start(); session.runner.start(); account.authorize()
        session.runner.begin_bars(); self.frame(session)
        item.save(service)
        worker = JsonStateStore(item.state_file).load()
        self.assertIn("signal", worker.payload)
        self.assertEqual(session.strategy.position(OPTION), -1)
        self.assertEqual(session.strategy.frames, 1)
        self.assertFalse(transport.sent)
        session.runner.stop(); service.poll(); account.stop(); service.stop()
        state = JsonStateStore(self.source).load()
        self.assertIn("shared_account", state.payload["component_states"])
        self.assertEqual(self.source.with_name(self.source.name+".before-rollover-g1.json").read_bytes(), self.before)

    def test_shared_ports_register_exact_option_routes_and_no_index_execution(self):
        account, service, session, identity, _ = self.shared()
        self.assertIs(session.runner.position_manager, service.position_manager)
        self.assertIsNone(session.manager)
        self.assertEqual(set(session.client.routes), {OPTION, "IM2609.CFFEX"})
        self.assertNotIn("000852.XSHG", session.client.routes)
        self.assertIn("index-bars", session.feeds)
        self.assertEqual(set(specs := factories.instrument_specs("option_vega", session.references)),
            set(session.references.instrument_ids.values()))
        self.assertEqual(specs[InstrumentId.from_str(OPTION)].multiplier, 100)

    def test_shared_next_day_and_worker_restart_preserve_history_and_owner(self):
        account, service, session, identity, transport = self.shared()
        account.start(resume=True)
        item = SharedStrategySession(session, self.root/"worker.json", identity)
        item.restore(service, resume=True); item.save(service)
        account.authorize(); account.stop()
        previous = self.source.read_bytes()
        restored, service2, session2, identity2, transport2 = self.shared(current=inputs(date(2026, 8, 17), "dolphindb"))
        transport2.bucket = "2"
        restored.start(resume=True)
        self.assertEqual(service2.position_manager.position(CLIENT, OPTION), -1)
        self.assertEqual(restored.ledger.snapshot(InstrumentId.from_str(OPTION)).short_yesterday, 1)
        worker = SharedStrategySession(session2, item.state_file, identity2)
        worker.restore(service2, resume=True); worker.save(service2)
        self.assertEqual(JsonStateStore(item.state_file).load().payload["identity"]["trading_day"], "20260817")
        self.assertEqual(restored.client.order_state_machine.checkpoints()[0],
                         account.client.order_state_machine.checkpoints()[0])
        self.assertFalse(transport2.sent)
        restored.authorize(); restored.stop()

    def test_old_shared_state_migrates_same_day_without_claiming_candidate_or_same_contract_external(self):
        account, _, _, _, _ = self.shared()
        account.start(resume=True); account.authorize(); account.stop()
        before = self.source.read_bytes()
        old = JsonStateStore(self.source).load().payload
        transport = DetailsTransport("20260814")
        gross = {OPTION: (Decimal(1), Decimal(3)), "IM2609.CFFEX": (Decimal(0), Decimal(1)),
                 "ag2612.SHFE": (Decimal(0), Decimal(1))}
        transport.query_gross_positions = lambda: gross
        transport.query_positions = lambda: {key: long-short for key, (long, short) in gross.items()}
        transport.query_position_details = lambda: [dict(InstrumentID=key.split(".")[0],
            ExchangeID=key.split(".")[1], PosiDirection=direction, Position=quantity,
            PositionDate="1", HedgeFlag="1", PositionCost=10000)
            for key, values in gross.items() for direction, quantity in zip(("2", "3"), values) if quantity]
        new, service, session, _, _ = self.shared(transport=transport, preserve_external=True)
        new.start(resume=True); new.authorize()
        self.assertEqual(service.position_manager.position(CLIENT, OPTION), -1)
        self.assertEqual(service.position_manager.position(CLIENT, "IM2609.CFFEX"), 0)
        self.assertEqual(new.external_gross[OPTION], (1, 2))
        self.assertEqual(new.external_gross["IM2609.CFFEX"], (0, 1))
        self.assertEqual(new.ledger.snapshot(InstrumentId.from_str(OPTION)).short_total, 1)
        self.assertEqual(new.ledger.snapshot(InstrumentId.from_str(OPTION)).short_today_basis, Decimal("82.2"))
        new.stop()
        saved = JsonStateStore(self.source).load().payload
        self.assertEqual(saved["positions"]["attribution"], old["positions"]["attribution"])
        self.assertEqual(saved["orders"], old["orders"])
        self.assertEqual(saved["component_states"]["external_positions"]["version"], 2)
        self.assertFalse(transport.sent)
        self.assertTrue(any(p.read_bytes() == before for p in self.root.glob(self.source.name+".before-rollover-g*.json")))
        tomorrow = DetailsTransport("20260817")
        tomorrow.query_gross_positions = transport.query_gross_positions
        tomorrow.query_positions = transport.query_positions
        tomorrow.query_position_details = lambda: [dict(row, PositionDate="2")
            for row in transport.query_position_details()]
        later, service2, _, _, _ = self.shared(current=inputs(date(2026, 8, 17), "dolphindb"),
            transport=tomorrow, preserve_external=True)
        later.start(resume=True); later.authorize()
        self.assertEqual(service2.position_manager.position(CLIENT, OPTION), -1)
        self.assertEqual(later.ledger.snapshot(InstrumentId.from_str(OPTION)).short_yesterday_basis, Decimal("82.2"))
        self.assertEqual(later.external_gross[OPTION], (1, 2))
        self.assertFalse(tomorrow.sent)
        later.stop()

    def test_cross_day_terminal_draining_finishes_after_authoritative_check(self):
        import json
        from bomber.framework.dataprep.features import DEFAULT_FEATURE_CONFIG
        from bomber.framework.datahub.features.schema import dataset_from_definition
        spec = dataset_from_definition(json.loads(DEFAULT_FEATURE_CONFIG.read_text())["datasets"][0]["schema"])
        account, _, _, _, _ = self.shared(preserve_external=True)
        account.start(resume=True); account.authorize(); account.stop()
        store = JsonStateStore(self.source)
        saved = store.load(); payload = deepcopy(saved.payload)
        payload["component_states"]["shared_account"]["shared"].update(
            draining=[CLIENT], suspended=[CLIENT])
        # 实际归属组件对全成订单保留released=False，冻结剩余量为0。
        historical = payload["orders"][CLIENT][0]
        payload["positions"]["attribution"]["orders"]["filled-history"] = dict(
            client_id=CLIENT, client_order_id=historical["client_order_id"], instrument_id=OPTION,
            submitting_strategy=CLIENT, direction=-1, quantity="1", released=False,
            legs=[dict(strategy_id=CLIENT, target_key=OPTION, quantity="1", filled="1")])
        previous_index = payload["component_states"]["session_identity"]["identity"]["strategies"][CLIENT][
            "reference"]["sources"]["index"]
        previous_index.update(dataset_id=spec.dataset_id, schema_version=spec.schema_version,
            schema_fingerprint=spec.fingerprint)
        previous_index.pop("alignment", None); previous_index.pop("computation_version", None)
        store.save(payload, expected_generation=saved.generation)
        before = self.source.read_bytes()
        transport = DetailsTransport("20260817"); transport.bucket = "2"
        current = inputs(date(2026, 8, 17), "dolphindb")
        current.source_identity["index"] = {**previous_index,
            "alignment": "EXACT", "computation_version": spec.computation_version}
        new, service, _, _, _ = self.shared(current=current,
            transport=transport, preserve_external=True)
        new.start(resume=True)
        self.assertFalse(service._draining)
        self.assertEqual(new.rollover_audit["completed_draining"], [CLIENT])
        self.assertEqual(service.position_manager.position(CLIENT, OPTION), -1)
        self.assertFalse(transport.sent)
        self.assertTrue(any(p.read_bytes() == before for p in self.root.glob(self.source.name+".before-rollover-g*.json")))
        new.authorize(); new.stop()
        self.assertFalse(store.load().payload["component_states"]["shared_account"]["shared"]["draining"])

    def test_draining_does_not_hide_failed_pending_or_query_failure(self):
        for mode in ("failed", "pending", "query", "attribution_remaining"):
            path = self.root / (mode+".json"); path.write_bytes(self.before)
            account, _, _, _, _ = self.shared(path=path, preserve_external=True)
            account.start(resume=True); account.authorize(); account.stop()
            store = JsonStateStore(path); saved = store.load(); payload = deepcopy(saved.payload)
            shared = payload["component_states"]["shared_account"]
            shared["shared"]["draining"] = [CLIENT]
            if mode in {"failed", "pending"}: shared[mode] = True
            if mode == "attribution_remaining":
                payload["positions"]["attribution"]["orders"]["pending-plan"] = dict(
                    client_id=CLIENT, client_order_id=None, instrument_id=OPTION,
                    submitting_strategy=CLIENT, direction=-1, quantity="1", released=False,
                    legs=[dict(strategy_id=CLIENT, target_key=OPTION, quantity="1", filled="0")])
            store.save(payload, expected_generation=saved.generation)
            before = path.read_bytes()
            transport = DetailsTransport("20260817")
            if mode == "query":
                transport.query_gross_positions = lambda: (_ for _ in ()).throw(RuntimeError("fixture query failure"))
            new, _, _, _, _ = self.shared(path=path, current=inputs(date(2026, 8, 17), "dolphindb"),
                transport=transport, preserve_external=True)
            with self.assertRaises((ValueError, RuntimeError)):
                new.start(resume=True)
            new.stop()
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(transport.sent)

    def test_owned_migration_rejects_missing_owned_quantity_without_overwriting_checkpoint(self):
        account, _, _, _, _ = self.shared()
        account.start(resume=True); account.authorize(); account.stop()
        before = self.source.read_bytes()
        transport = DetailsTransport("20260814")
        transport.query_gross_positions = lambda: {"ag2612.SHFE": (Decimal(0), Decimal(1))}
        transport.query_positions = lambda: {"ag2612.SHFE": Decimal(-1)}
        transport.query_position_details = lambda: [dict(InstrumentID="ag2612", ExchangeID="SHFE",
            PosiDirection="3", Position=1, PositionDate="1", HedgeFlag="1", PositionCost=10000)]
        new, _, _, _, _ = self.shared(transport=transport, preserve_external=True)
        with self.assertRaisesRegex(RuntimeError, "不足以覆盖"):
            new.start(resume=True)
        new.stop()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(transport.sent)

    def rollover_with_external(self, gross, *, name="external", initial_quantity=1):
        path = self.root / (name + ".json")
        path.write_bytes(self.before)
        initial = DetailsTransport("20260814")
        initial.query_gross_positions = lambda: {OPTION: (Decimal(0), Decimal(1)),
            "ag2612.SHFE": (Decimal(0), Decimal(initial_quantity))}
        initial.query_positions = lambda: {OPTION: Decimal(-1), "ag2612.SHFE": Decimal(-initial_quantity)}
        original_details = initial.query_position_details
        initial.query_position_details = lambda: [dict(row, Position=initial_quantity)
            if row["ExchangeID"] == "SHFE" else row for row in original_details()]
        account, _, _, _, _ = self.shared(path=path, transport=initial)
        account.start(resume=True); account.authorize(); account.stop()
        before = path.read_bytes()
        transport = DetailsTransport("20260817"); transport.bucket = "2"
        positions = {OPTION: (Decimal(0), Decimal(1)), **gross}
        transport.query_gross_positions = lambda: deepcopy(positions)
        transport.query_positions = lambda: {key: long-short for key, (long, short) in positions.items()}
        def details():
            rows = []
            for key, values in positions.items():
                symbol, venue = key.rsplit(".", 1)
                for direction, quantity in zip(("2", "3"), values):
                    if quantity:
                        rows.append(dict(InstrumentID=symbol, ExchangeID=venue, Position=quantity,
                            PosiDirection=direction, PositionDate="2", HedgeFlag="1", PositionCost=10000))
            return rows
        transport.query_position_details = details
        restored = self.shared(path=path, current=inputs(date(2026, 8, 17), "dolphindb"), transport=transport)
        return restored, path, before

    def test_cross_day_external_increase_decrease_close_new_and_hedged_positions(self):
        cases = ((1, {"ag2612.SHFE": (Decimal(0), Decimal(3))}),
                 (3, {"ag2612.SHFE": (Decimal(0), Decimal(1))}), (1, {}),
                 (1, {"ag2612.SHFE": (Decimal(0), Decimal(1)), "cu2611.SHFE": (Decimal(2), Decimal(2))}),
                 (1, {"cu2611.SHFE": (Decimal(1), Decimal(0))}))
        for n, (initial_quantity, gross) in enumerate(cases):
            # 柜台总仓接口不返回零仓合约；双向等量仓必须保留，不能只比净仓。
            gross = {key: values for key, values in gross.items() if any(values)}
            with self.subTest(gross=gross):
                (account, service, _, _, transport), path, before = self.rollover_with_external(
                    gross, name=str(n), initial_quantity=initial_quantity)
                account.start(resume=True)
                self.assertEqual(service.position_manager.position(CLIENT, OPTION), -1)
                self.assertEqual(account.external_gross, gross)
                self.assertEqual(account.ledger.snapshot(InstrumentId.from_str(OPTION)).short_yesterday, 1)
                self.assertTrue(account.rollover_audit["external_baseline_refresh"]["changes"])
                self.assertFalse(any(row["instrument_id"] in gross and Decimal(row["quantity"])
                    for row in service.position_manager.attribution.state()["owned"]))
                account.authorize(); account.stop()
                state = JsonStateStore(path).load()
                self.assertEqual(state.payload["component_states"]["external_positions"]["gross"],
                    {key: [str(q) for q in values] for key, values in gross.items()})
                self.assertFalse(transport.sent)
                self.assertTrue(any(backup.read_bytes() == before
                    for backup in path.parent.glob(path.name+".before-rollover-g*.json")))
                again, service2, _, _, _ = self.shared(path=path,
                    current=inputs(date(2026, 8, 17), "dolphindb"), transport=transport)
                again.start(resume=True); again.authorize()
                self.assertEqual(service2.position_manager.position(CLIENT, OPTION), -1)
                self.assertEqual(again.external_gross, gross)
                again.stop()

    def test_external_refresh_query_mismatch_or_race_preserves_checkpoint(self):
        for mode in ("details", "race", "managed", "query_failure", "final_query"):
            with self.subTest(mode=mode):
                (account, _, _, _, transport), path, before = self.rollover_with_external(
                    {"ag2612.SHFE": (Decimal(0), Decimal(2))}, name=mode)
                original = transport.query_position_details
                calls = [0]
                def details():
                    calls[0] += 1
                    rows = original()
                    if calls[0] >= 2:
                        if mode == "query_failure":
                            raise RuntimeError("fixture external query failure")
                        if mode == "details":
                            return [row for row in rows if row["ExchangeID"] == "CFFEX"]
                        if mode == "race":
                            transport.query_gross_positions = lambda: {OPTION: (Decimal(0), Decimal(1))}
                    return rows
                transport.query_position_details = details
                if mode == "managed":
                    transport.query_gross_positions = lambda: {OPTION: (Decimal(0), Decimal(2))}
                if mode == "final_query":
                    original_gross = transport.query_gross_positions
                    gross_calls = [0]
                    def gross():
                        gross_calls[0] += 1
                        if gross_calls[0] >= 4:
                            raise RuntimeError("fixture final query failure")
                        return original_gross()
                    transport.query_gross_positions = gross
                with self.assertRaises((RuntimeError, ValueError)):
                    account.start(resume=True)
                account.stop()
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(transport.sent)

    def test_same_day_external_change_still_refuses_resume(self):
        (account, _, _, _, transport), path, _ = self.rollover_with_external(
            {"ag2612.SHFE": (Decimal(0), Decimal(2))})
        account.start(resume=True); account.authorize(); account.stop()
        before = path.read_bytes()
        transport.query_gross_positions = lambda: {OPTION: (Decimal(0), Decimal(1))}
        transport.query_positions = lambda: {OPTION: Decimal(-1)}
        again, _, _, _, _ = self.shared(path=path,
            current=inputs(date(2026, 8, 17), "dolphindb"), transport=transport)
        with self.assertRaises(RuntimeError):
            again.start(resume=True)
        again.stop()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(transport.sent)

    def test_query_failure_in_last_prearm_check_preserves_original(self):
        lifecycle, session, context, transport = self.standalone()
        original = transport.query_gross_positions
        calls = [0]
        def gross():
            calls[0] += 1
            if calls[0] >= 3:
                raise RuntimeError("fixture final query failure")
            return original()
        transport.query_gross_positions = gross
        with self.assertRaisesRegex(RuntimeError, "query failure"):
            lifecycle.start(session, context)
        lifecycle.shutdown(session, context)
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertFalse(transport.sent)

    def test_cffex_yesterday_close_uses_generic_close_and_only_own_quantity(self):
        from bomber.framework.trader.contracts import ExecutionRequest
        from bomber.framework.trader.execution.contracts import PositionEffect
        account, service, session, _, transport = self.shared()
        transport.bucket = "2"
        account.start(resume=True)
        instrument = InstrumentId.from_str(OPTION)
        prices = service.prices
        prices.update(instrument, Decimal("82.2"), 123)
        request = ExecutionRequest(CLIENT, 2, CLIENT, 123, {instrument: Decimal(0)}, "DIRECT",
            metadata={"shared_execution_mode": "independent", "position_attribution": [dict(
                strategy_id=CLIENT, target_key=OPTION, instrument_id=OPTION, quantity="0")]})
        planned = service.client.planner.plan(request)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].quantity, 1)
        self.assertEqual(planned[0].position_effect, PositionEffect.CLOSE)
        self.assertTrue(planned[0].reduce_only)
        self.assertEqual(planned[0].strategy_id, CLIENT)
        self.assertFalse(transport.sent)
        account.stop()

    def test_shared_contract_chain_zero_expansion_keeps_held_leg(self):
        account, service, session, identity, transport = self.shared()
        account.start(resume=True); account.authorize(); account.stop()
        current = inputs(date(2026, 8, 17), "dolphindb")
        added = "MO2609-C-6800"
        info = next(iter(current.references.contracts.values()))
        from dataclasses import replace
        current.references.contracts[added] = replace(info, symbol=added, strike=6800)
        instrument = InstrumentId.from_str(added+".CFFEX")
        current.references.instrument_ids[added] = instrument
        current.references.options_by_day[current.references.service.valuation_day] = tuple(current.references.contracts)
        current.sources[added] = "market-bars"
        from bomber.framework.market.basic.base import InstrumentMeta
        current.feeds["market-bars"].register_instrument(InstrumentMeta(instrument, price_precision=1, multiplier=100))
        restarted, service2, session2, _, _ = self.shared(current=current)
        restarted.start(resume=True)
        self.assertEqual(service2.position_manager.position(CLIENT, OPTION), -1)
        self.assertEqual(service2.position_manager.position(CLIENT, str(instrument)), 0)
        self.assertIn(str(instrument), session2.client.routes)
        restarted.authorize(); restarted.stop()

    def test_fast_entry_config_preserves_strategy_and_dates_are_not_pinned_in_argv(self):
        from scripts.integration.options.shared import session_config
        from scripts.integration.shared_account.config import read_config
        import json
        config = session_config(self.identity, day="20260814", account_state=self.source,
            signal_state=self.root/"signal.json", report_dir=self.root/"reports")
        path = self.root/"config.json"; path.write_text(json.dumps(config))
        parsed = read_config(path)
        row, = parsed["strategies"]
        self.assertEqual(row["kind"], "option_vega")
        self.assertEqual(parsed["execution_mode"], "independent")
        self.assertNotIn("--valuation-day", row["argv"])
        module, args = factories.strategy_arguments(row, parsed, resume=True)
        self.assertFalse(args.resume)
        self.assertEqual(args.valuation_day, "20260814")
        self.assertEqual(args.config.option_slot, "13:36")
        self.assertEqual(args.market_source, "dolphindb")
        self.assertEqual(args.index_source, "dolphindb-dfs")

    def test_application_host_imports_original_without_second_td_or_manual_migration(self):
        from scripts.integration.options.shared import session_config
        from scripts.integration.shared_account import run
        import sys
        transport = DetailsTransport("20260814")
        config = session_config(self.identity, day="20260814", account_state=self.source,
            signal_state=self.root/"signal.json", report_dir=self.root/"reports")
        package = sys.modules["bomber.framework.trader.execution.ctp"]
        with patch.object(package, "CtpNativeTraderDriver", CtpNativeTraderDriver, create=True), \
                patch.object(package, "CtpTdApiTransport", object, create=True), \
                patch("bomber.framework.trader.runtime.live.channels.ctp.build_simnow_transport", return_value=transport), \
                patch.object(entry, "prepare_inputs", return_value=self.current):
            host = run.build_host(config, resume=True)
        self.addCleanup(host.resources.close)
        host.start()
        self.assertEqual(len(host.strategies), 1)
        self.assertEqual(host.service.position_manager.position(CLIENT, OPTION), -1)
        self.assertIn("shared_account", JsonStateStore(self.source).load().payload["component_states"])
        self.assertTrue((self.root/"signal.json").is_file())
        self.assertTrue(host.service.client.is_armed)
        self.assertFalse(transport.sent)
        host.stop()

    def test_observer_and_restored_host_share_runtime_td_timeout_without_identity_changes(self):
        import os
        import sys
        from scripts.integration.options import shared as fast
        from scripts.integration.shared_account import run
        channel = import_module("bomber.framework.trader.runtime.live.channels.ctp")
        builder, created = channel.build_simnow_transport, []
        def transport_factory(**fields):
            result = DetailsTransport("20260814")
            result.timeout_seconds = fields["timeout_seconds"]
            created.append((fields["client_id"], result))
            return result
        def build(factory, **kwargs):
            return builder(transport_factory, **kwargs)
        config = fast.session_config(self.identity, day="20260814", account_state=self.source,
            signal_state=self.root/"signal.json", report_dir=self.root/"reports")
        original = deepcopy(config)
        package = sys.modules["bomber.framework.trader.execution.ctp"]
        env = {"CTP_BROKER_ID": "9999", "CTP_ACCOUNT_ID": "fixture", "CTP_PASSWORD": "fake",
               "CTP_TD_ADDRESS": "tcp://fake:1234", "CTP_TD_TIMEOUT_SECONDS": "45"}
        with patch.dict(os.environ, env), \
                patch.object(package, "CtpNativeTraderDriver", CtpNativeTraderDriver, create=True), \
                patch.object(package, "CtpTdApiTransport", object, create=True), \
                patch.object(channel, "build_simnow_transport", side_effect=build), \
                patch.object(entry, "prepare_inputs", return_value=self.current):
            self.assertEqual(fast.observe_day(), "20260814")
            host = run.build_host(config, resume=True)
        self.addCleanup(host.resources.close)
        host.start()
        self.assertEqual([sid for sid, _ in created], ["option-vega-observe", CLIENT])
        self.assertTrue(all(t.timeout_seconds == 45 for _, t in created))
        self.assertEqual(config, original)
        self.assertEqual(host.service.position_manager.position(CLIENT, OPTION), -1)
        self.assertEqual(host.account.manager.state_components["session_identity"].identity["account"], original["account"])
        self.assertFalse(any(t.sent for _, t in created))
        host.stop()

    def test_fast_entry_same_day_reuses_persisted_argv_without_numeric_drift(self):
        from scripts.integration.options.shared import session_config, load_identity
        from scripts.integration.shared_account import run
        import sys
        package = sys.modules["bomber.framework.trader.execution.ctp"]
        def build(config):
            with patch.object(package, "CtpNativeTraderDriver", CtpNativeTraderDriver, create=True), \
                    patch.object(package, "CtpTdApiTransport", object, create=True), \
                    patch("bomber.framework.trader.runtime.live.channels.ctp.build_simnow_transport",
                          return_value=DetailsTransport("20260814")), \
                    patch.object(entry, "prepare_inputs", return_value=self.current):
                return run.build_host(config, resume=True)
        first = session_config(self.identity, day="20260814", account_state=self.source,
            signal_state=self.root/"exact-signal.json", report_dir=self.root/"reports")
        host = build(first); self.addCleanup(host.resources.close)
        host.start(); host.stop()
        saved, reference = load_identity(self.source)
        stored_identity = saved.payload["component_states"]["session_identity"]["identity"]
        rebuilt = session_config(reference, day="20260814", account_state=self.source,
            signal_state=self.root/"exact-signal.json", report_dir=self.root/"reports")
        # 原缺陷：值相同，但显式CLI解析将整数默认值5变成浮点5.0。
        self.assertNotEqual(rebuilt["strategies"][0]["argv"], first["strategies"][0]["argv"])
        original_bytes = self.source.read_bytes()
        rejected = build(rebuilt); self.addCleanup(rejected.resources.close)
        with self.assertRaisesRegex(ValueError, "差异字段.*argv"):
            rejected.start()
        self.assertEqual(self.source.read_bytes(), original_bytes)
        self.assertFalse(rejected.account.driver.submitted_orders)

        fixed = session_config(reference, day="20260814", account_state=self.source,
            signal_state=self.root/"exact-signal.json", report_dir=self.root/"reports",
            shared_identity=stored_identity)
        self.assertEqual(fixed["strategies"][0]["argv"], first["strategies"][0]["argv"])
        self.assertEqual(fixed["account"], stored_identity["account"])
        again = build(fixed); self.addCleanup(again.resources.close)
        again.start()
        self.assertEqual(again.service.position_manager.position(CLIENT, OPTION), -1)
        self.assertTrue(again.service.client.is_armed)
        self.assertFalse(again.account.driver.submitted_orders)
        again.stop()
        original_bytes = self.source.read_bytes()
        altered = deepcopy(fixed)
        argv = altered["strategies"][0]["argv"]
        argv[argv.index("--option-slot")+1] = "13:37"
        wrong = build(altered); self.addCleanup(wrong.resources.close)
        with self.assertRaisesRegex(ValueError, "差异字段"):
            wrong.start()
        self.assertEqual(self.source.read_bytes(), original_bytes)
        self.assertFalse(wrong.account.driver.submitted_orders)
        # 保持预算约束；参数漂移修复不是放宽账户身份。
        with self.assertRaisesRegex(ValueError, "订单上限"):
            session_config(reference, day="20260814", account_state=self.source,
                signal_state=self.root/"exact-signal.json", report_dir=self.root/"reports", max_orders=21,
                shared_identity=stored_identity)

    def test_identity_error_names_paths_but_does_not_disclose_values(self):
        from bomber.framework.trader.runtime.live.recovery import SessionIdentityCheckpoint
        check = SessionIdentityCheckpoint({"password": "new_secret", "strategies": {CLIENT: {"argv": ["5.0"]}}})
        with self.assertRaisesRegex(ValueError, "差异字段") as caught:
            check.restore_state({"version": 1, "identity": {"password": "old_secret", "strategies": {CLIENT: {"argv": ["5"]}}}})
        self.assertIn(".password", str(caught.exception))
        self.assertIn("argv[0]", str(caught.exception))
        self.assertNotIn("new_secret", str(caught.exception))
        self.assertNotIn("old_secret", str(caught.exception))

    def test_shared_fast_entry_stops_at_calendar_close_and_finally_saves(self):
        from scripts.integration.options import shared as fast
        from bomber.framework.calendars.sessions import TradingWindow
        from threading import Event
        from unittest.mock import MagicMock
        zone = ZoneInfo("Asia/Shanghai")
        window = TradingWindow("20260814", datetime(2026, 8, 14, 13, tzinfo=zone),
                               datetime(2026, 8, 14, 14, tzinfo=zone), "early_close")
        now = [window.start]
        host = MagicMock()
        host.start.side_effect = lambda: now.__setitem__(0, window.end)
        host.strategies = {CLIENT: NS(session=NS())}
        host.snapshot.return_value = {}
        host.account.rollover_audit = {}
        host.account.execution.backend.reports = ()
        report = MagicMock()
        with patch("scripts.integration.shared_account.run.build_host", return_value=host), \
                patch("bomber.framework.trader.runtime.reports.LiveRunReport", return_value=report), \
                patch.object(entry, "describe", return_value={}):
            result = fast.run_session(dict(trading_day="20260814", report_dir=str(self.root), seconds=86400),
                resume=True, window=window, stop_event=Event(), clock=lambda: now[0])
        host.start.assert_called_once(); host.stop.assert_called_once(); host.poll.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(report.write.call_args.kwargs["status"], "completed")

    def test_operator_stop_before_connect_keeps_state_bytes(self):
        from scripts.integration.options import shared as fast
        from bomber.framework.calendars.sessions import TradingWindow
        from threading import Event
        from unittest.mock import MagicMock
        stop = Event(); stop.set()
        zone = ZoneInfo("Asia/Shanghai")
        window = TradingWindow("20260814", datetime(2026, 8, 14, 13, tzinfo=zone),
                               datetime(2026, 8, 14, 15, tzinfo=zone))
        original = self.source.read_bytes()
        with patch("scripts.integration.shared_account.run.build_host") as build, \
                patch("bomber.framework.trader.runtime.reports.LiveRunReport", return_value=MagicMock()):
            result = fast.run_session(dict(trading_day="20260814", report_dir=str(self.root), seconds=86400),
                resume=True, window=window, stop_event=stop)
        self.assertEqual(result["status"], "completed")
        build.assert_not_called()
        self.assertEqual(self.source.read_bytes(), original)

    def test_window_adapter_uses_actual_td_and_saved_day_budget(self):
        from scripts.integration.options import shared as fast
        from bomber.framework.calendars.sessions import TradingWindow
        from threading import Event
        zone = ZoneInfo("Asia/Shanghai")
        window = TradingWindow("20260814", datetime(2026, 8, 14, 13, tzinfo=zone),
                               datetime(2026, 8, 14, 15, tzinfo=zone))
        args = NS(state_file=self.source, signal_state=self.root/"signal.json", report_dir=self.root,
                  max_session_orders=20)
        adapter = fast.OptionWindowRuntime(args, NS(provenance={"fixture": True}), window, Event())
        with patch.object(fast, "observe_day", return_value="20260814"), \
                patch.object(fast, "run_session", return_value={"status": "completed"}) as run:
            self.assertEqual(adapter.run()["status"], "completed")
            self.assertEqual(run.call_args.kwargs["session_order_budget"], 20)
            self.assertEqual(run.call_args.args[0]["trading_day"], "20260814")
        with patch.object(fast, "observe_day", return_value="20260813"), patch.object(fast, "run_session") as run:
            with self.assertRaisesRegex(RuntimeError, "窗口"):
                adapter.run()
            run.assert_not_called()


    def test_stop_during_preparation_prevents_authorization_and_keeps_owned_state(self):
        from bomber.framework.trader.runtime.live.shared import SharedAccountLiveHost
        from bomber.framework.trader.runtime.live.continuous import StopRequested
        account, service, session, identity, transport = self.shared()
        host = SharedAccountLiveHost(account, service,
            [SharedStrategySession(session, self.root/"prepared-signal.json", identity)],
            resources=account.resources, resume=True)
        checks = [0]
        def cancelled():
            checks[0] += 1
            if checks[0] == 3:
                raise StopRequested()
        host.preparation_check = cancelled
        with self.assertRaises(StopRequested):
            host.start()
        self.assertFalse(service.client.is_armed)
        self.assertFalse(transport.sent)
        self.assertEqual(service.position_manager.position(CLIENT, OPTION), -1)
        self.assertTrue(host._stopped)

    def test_public_scheduler_preserves_owned_position_across_real_shared_windows(self):
        from bomber.framework.calendars import CalendarSnapshot, TradingHoursSnapshot, SessionSegment, SessionCalendar, SessionBinding
        from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime
        from bomber.framework.trader.runtime.live.shared import SharedAccountLiveHost
        from threading import Event
        days = (date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17))
        dates = CalendarSnapshot("china.cffex", "fixture", days[0], days[-1], days, "fixture")
        hours = TradingHoursSnapshot(tuple(("IM", SessionSegment(name, "TRADING_DAY", 0, start, 0, end))
            for name, start, end in (("morning", "09:30", "11:30"), ("afternoon", "13:00", "15:00"))), "fixture")
        calendar = SessionCalendar(hours, dates, SessionBinding("MO", "IM", "china.cffex", "Asia/Shanghai", "CFFEX", "OPTION"))
        now = [datetime(2026, 8, 14, 10, tzinfo=calendar.zone)]
        stop, windows, generations = Event(), [], []
        from scripts.integration.options import shared as fast
        def factory(window, event):
            def run():
                current = inputs(datetime.strptime(window.trading_day, "%Y%m%d").date(), "dolphindb")
                account, service, session, identity, transport = self.shared(current=current)
                host = SharedAccountLiveHost(account, service,
                    [SharedStrategySession(session, self.root/"daily-signal.json", identity)], resources=account.resources,
                    resume=True)
                # 公共账户/策略生命周期实际启动，只有传输与时钟为夹具。
                original_poll = host.poll
                def poll():
                    self.frame(session, "10:01" if window.segment_id == "morning" else "13:35")
                    session.client._wall_clock_ns = session.runner.clock_ns
                    original_poll()
                    now[0] = window.end
                host.poll = poll
                with patch("scripts.integration.shared_account.run.build_host", return_value=host):
                    result = fast.run_session(dict(trading_day=window.trading_day, report_dir=str(self.root), seconds=86400),
                        resume=True, window=window, stop_event=event, clock=lambda: now[0])
                self.assertEqual(service.position_manager.position(CLIENT, OPTION), -1)
                self.assertGreaterEqual(session.strategy.frames, 1)
                self.assertFalse(transport.sent)
                state = JsonStateStore(self.source).load()
                self.assertEqual(state.payload["ctp_ledgers"][CLIENT]["trading_day"], window.trading_day)
                generations.append(state.generation); windows.append((window.trading_day, window.segment_id))
                if window.trading_day == "20260817":
                    event.set()
                return result
            return NS(run=run)
        runtime = ContinuousPositionRuntime(calendar, factory, run_forever=True, stop_event=stop,
            clock=lambda: now[0], wait=lambda _: now.__setitem__(0, now[0]+__import__("datetime").timedelta(minutes=30)),
            status=lambda _: None)
        self.assertTrue(runtime.run()["stopped_by_operator"])
        self.assertEqual(windows, [("20260814", "morning"), ("20260814", "afternoon"), ("20260817", "morning")])
        self.assertEqual(generations, sorted(set(generations)))
        self.assertEqual(self.source.with_name(self.source.name+".before-rollover-g1.json").read_bytes(), self.before)

    def test_same_day_shared_restart_keeps_sent_day_and_does_not_select_again(self):
        account, service, session, identity, transport = self.shared()
        account.start(resume=True)
        item = SharedStrategySession(session, self.root/"worker.json", identity)
        item.restore(service, resume=True)
        session.strategy.sent_days.add(date(2026, 8, 14)); item.save(service)
        account.authorize(); account.stop()
        recovered, service2, session2, identity2, transport2 = self.shared()
        recovered.start(resume=True)
        worker = SharedStrategySession(session2, item.state_file, identity2)
        worker.restore(service2, resume=True)
        service2.start(); session2.runner.start(); recovered.authorize(); session2.runner.begin_bars()
        self.frame(session2, "13:36")
        self.assertEqual(session2.strategy.entry_audit, [])
        self.assertEqual(session2.strategy.position(OPTION), -1)
        self.assertFalse(transport2.sent)
        session2.runner.stop(); service2.poll(); recovered.stop(); service2.stop()

    def test_two_option_strategies_share_account_but_do_not_inherit_peer_position(self):
        account, service, first, _, transport = self.shared()
        second = factories.assemble_strategy_session(entry, self.new_args,
            inputs(date(2026, 8, 14), "dolphindb"), service=service, driver=account.driver,
            strategy_id="vega-peer")
        account.start(resume=True)
        # 原归属只有08；另一个注册策略不接管它，也不复制账户空仓到自己。
        service.start(); first.runner.start(); second.runner.start(); account.authorize()
        first.runner.begin_bars(); second.runner.begin_bars()
        self.frame(first); self.frame(second)
        self.assertEqual(first.strategy.position(OPTION), -1)
        self.assertEqual(second.strategy.position(OPTION), 0)
        self.assertEqual(first.strategy.frames, 1)
        self.assertEqual(second.strategy.frames, 1)
        self.assertFalse(transport.sent)
        first.runner.stop(); service.poll()
        self.assertTrue(second.client.is_reconciled)
        second.runner.stop(); service.poll(); account.stop(); service.stop()


if __name__ == "__main__":
    unittest.main()
