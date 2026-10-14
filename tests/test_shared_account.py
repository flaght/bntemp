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

    def test_strategy_and_gross_account_budgets_reject_before_mutating(self):
        service, client, (one, two) = self.service(quantity=3)
        self.submit(one, 2)
        before = service.snapshot_state()
        with self.assertRaisesRegex(ValueError, "gross_quantity=4.*max_gross_quantity=3.*gross_notional=12000"):
            self.submit(two, -2)
        self.assertEqual(service.snapshot_state(), before)
        with self.assertRaises(ValueError):
            self.submit(two, 6)

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

    def native_account(self, path, modules, *, scoped=False, execution_mode="netted"):
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
        limits = {self.instrument: self.risk.RiskLimits(max_order_quantity=10, max_abs_position=10,
            max_market_age_ns=120_000_000_000, contract_multiplier=1)}
        ledger = modules.ledger.CtpPositionLedger("20261013")
        backend = modules.backend.NautilusLiveExecutionBackend("account", driver)
        client = modules.controlled.ControlledLiveExecutionClient("account",
            modules.planner.CtpLimitPlanner(ledger, positions, prices, 1, 0), backend, positions,
            self.risk.PreTradeRiskManager("account", positions, prices, instrument_limits=limits),
            account_id="demo", demo_environment_check=lambda: driver.is_simnow_session)
        backend.register_report_handler(modules.accounting.CtpExecutionAccounting(ledger, {self.instrument: 1}).on_report)
        scope = None
        if scoped:
            scope = import_module("bomber.framework.trader.runtime.live.recovery").InstrumentPositionScope((self.instrument,))
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
                driver, trading_day="20261013", price_increments={self.instrument: 1}, multipliers={self.instrument: 1},
                instrument_limits=limits, session_check=lambda: driver.is_simnow_session, orders=True,
                positions=positions, prices=prices, instrument_scope=scope, limit_offset_ticks=0)
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
            position_scope=scope)
        self.addCleanup(client.stop)
        return lifecycle, service, transport, ports, resources

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

    def callback_host(self, root):
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
        host.start()
        return host, transport, rows

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
            host.restart_strategy("one", fresh)
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
            config["seconds"] = 600
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
        host.start.assert_not_called()
        account.authorize.assert_not_called()
        self.assertEqual(reported["status"], "reconciled")
        self.assertEqual(reported["fields"]["verification"], "checkpoint_reconciled_no_strategy_no_orders")


if __name__ == "__main__":
    unittest.main()
