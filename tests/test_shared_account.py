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
        with self.assertRaises(ValueError):
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

    def native_account(self, path, modules):
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
        shared = import_module("bomber.framework.trader.execution.shared")
        service = shared.SharedAccountExecutionService(client, prices=prices, instrument_limits=limits,
            budget=shared.SharedRiskBudget(10, 100000), clock_ns=lambda: self.now)
        ports = [service.strategy_client(sid, routes={"main": self.instrument},
            budget=shared.SharedRiskBudget(5, 50000)) for sid in ("one", "two")]
        resources = ExitStack()
        self.addCleanup(resources.close)
        execution = NS(driver=driver, client=client, ledger=ledger, positions=positions, backend=backend)
        lifecycle = modules.lifecycle.CtpSharedAccountLifecycle(execution, service, transport=transport,
            state_file=path, identity={"id": "test-shared"}, resources=resources, expected_day="20261013")
        self.addCleanup(client.stop)
        return lifecycle, service, transport, ports, resources

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


if __name__ == "__main__":
    unittest.main()
