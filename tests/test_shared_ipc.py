"""独立订单与真实Unix跨进程协议；仅柜台传输/SDK类型为替身。"""

from dataclasses import replace
from contextlib import nullcontext
from decimal import Decimal
import multiprocessing
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from types import SimpleNamespace

from tests import test_shared_account as fixtures


def process_poll(path, sid, key, configuration, queue):
    from bomber.framework.trader.execution.ipc import UnixExecutionConnection
    try:
        connection = UnixExecutionConnection(Path(path), sid, key, timeout=5)
        attached = connection.call("attach", {"configuration": configuration})
        result = connection.call("poll", {"cursor": 0, "ready": True})
        queue.put({"sid": sid, "state": attached["state"], "events": result["events"]})
        connection.call("stop", {"cursor": 0})
    except Exception as error:
        queue.put({"sid": sid, "error": str(error)})


class SharedIpcTests(unittest.TestCase):
    setUp = fixtures.SharedAccountTests.setUp
    native_modules = fixtures.SharedAccountTests.native_modules
    native_account = fixtures.SharedAccountTests.native_account
    request = fixtures.SharedAccountTests.request

    def test_budget_stop_keeps_ipc_alive_for_partial_fill_cancel_and_final_ack(self):
        from bomber.framework.trader.execution.contracts import OrderBudgetExhausted
        from bomber.framework.trader.execution.ipc_client import RemoteAccountService
        from bomber.framework.trader.execution.shared import SharedRiskBudget
        with TemporaryDirectory() as directory:
            _, account, service, transport, protocol = self.prepare(Path(directory)/"account.json")
            account.driver._max_session_orders = 1
            owner = self
            class Connection:
                sid, lease, sequence = "one", None, 0
                def call(self, action, data):
                    self.sequence += 1
                    result = owner.call(protocol, "one", action, data, lease=self.lease,
                        request_id=str(self.sequence))
                    if action == "attach": self.lease = result["lease"]
                    return result
            connection = Connection()
            remote = RemoteAccountService(connection, service._ports["one"].configuration(), protocol.state("one"))
            port = remote.strategy_client("one", routes={"main": self.instrument},
                budget=SharedRiskBudget(5,50000), instrument_limits=service._ports["one"].instrument_limits)
            port.ready_check = lambda: True
            port.start(); port.drain_events()
            remote.prices.update(self.instrument, 100, self.now)
            port.submit_targets(self.request(service.client_id, strategy="one", quantity=2, timestamp=self.now))
            transport.fill(transport.sent[-1]["OrderRef"], 1)
            protocol.poll()
            with self.assertRaises(OrderBudgetExhausted):
                port.drain_events()
            self.assertEqual(remote.position_manager.position("one", "main"), 1)
            port.begin_stop()
            self.assertEqual(protocol.journal.ack["one"], len(protocol.journal.events["one"]))
            self.assertFalse(protocol.leases)
            self.assertFalse(service._failed or service._pending)
            self.assertEqual(len(transport.sent), 1)
            self.assertTrue(all(r["status"] == "completed" for r in protocol.journal.requests.values()))
            account.stop()

    def test_actual_server_budget_stop_drains_three_clients_and_writes_completed_report(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        from scripts.integration.shared_account import server, run
        from bomber.framework.trader.execution.ipc import UnixExecutionConnection
        from bomber.framework.trader.execution.ipc_client import RemoteAccountService
        from bomber.framework.trader.execution.shared import SharedRiskBudget
        from bomber.framework.trader.execution.contracts import OrderBudgetExhausted
        import json
        with nullcontext(self.enterContext(TemporaryDirectory(dir="/tmp"))) as directory:
            root = Path(directory)
            _, account, service, transport, _ = self.prepare(root/"account.json")
            account.driver._max_session_orders = 1
            cancel = transport.cancel_order
            def late_fill_before_cancel(ref):
                transport.fill(ref, 1)
                cancel(ref)
            transport.cancel_order = late_fill_before_cancel
            host = SimpleNamespace(account=account, service=service,
                strategies={sid: SimpleNamespace(session=SimpleNamespace()) for sid in service._ports},
                resources=SimpleNamespace(close=lambda: None),
                snapshot=lambda: {"execution_diagnostics": {"service_failed": service._failed}})
            args = SimpleNamespace(socket=root/"exec.sock", credentials_dir=root/"keys", resume=False,
                remaining_orders=None)
            errors = []
            def control():
                try:
                    server._run(args, {"seconds": 10, "trading_day": "20261013", "report_dir": str(root)}, threading.Event())
                except BaseException as error:
                    errors.append(error)
            with patch.object(run, "build_host", return_value=host), \
                    patch.object(server, "connect_valuation_market", return_value=None), \
                    patch.dict("sys.modules", {"dotenv": SimpleNamespace(load_dotenv=lambda *_: None)}), \
                    redirect_stdout(StringIO()):
                thread = threading.Thread(target=control)
                thread.start()
                ports = []
                try:
                    until = time.monotonic() + 5
                    while not (args.credentials_dir/"three.json").exists() and time.monotonic() < until:
                        time.sleep(0.01)
                    for sid, original in service._ports.items():
                        credentials = json.loads((args.credentials_dir/f"{sid}.json").read_text())
                        connection = UnixExecutionConnection(args.socket, sid, credentials["key"], timeout=5)
                        remote = RemoteAccountService(connection, original.configuration(), connection.call("describe", {})["state"])
                        port = remote.strategy_client(sid, routes={"main": self.instrument},
                            budget=SharedRiskBudget(5,50000), instrument_limits=original.instrument_limits)
                        port.ready_check = lambda: True
                        port.start(); port.drain_events()
                        ports.append(port)
                    ports[0].service.prices.update(self.instrument, 100, self.now)
                    ports[0].submit_targets(self.request(service.client_id, strategy="one", quantity=2, timestamp=self.now))
                    for port in ports:
                        with self.assertRaises(OrderBudgetExhausted):
                            port.drain_events()
                        port.begin_stop()
                finally:
                    thread.join(8)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors, errors)
            report = json.loads(next(root.glob("shared-account-server-*/summary.json")).read_text())
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["stop_reason"], "order_budget_exhausted")
            self.assertIsNone(report["failure"])
            self.assertIsNone(report["shutdown_failure"])
            self.assertEqual(report["final_active_orders"], 0)
            self.assertEqual(len(transport.sent), 1)
            self.assertFalse(args.socket.exists())
            self.assertEqual(ports[0].position_manager.position("one", "main"), 1)

    def test_poll_publishes_owned_route_prices_before_signals_and_rejects_future_or_foreign(self):
        with nullcontext(self.enterContext(TemporaryDirectory())) as directory:
            _, _, service, transport, protocol = self.prepare(Path(directory)/"account.json")
            lease = self.attach(protocol, "one")
            stamp = time.time_ns()
            row = {str(self.instrument): {"price": "3210", "ts_event": stamp}}
            self.call(protocol, "one", "poll", {"cursor": 0, "ready": False, "prices": row}, lease=lease)
            self.assertEqual(service.prices.get(self.instrument).price, 3210)
            self.assertFalse(transport.sent)
            self.assertFalse(protocol.leases["one"]["ready"])
            invalid = {**row, "au2612.SHFE": {"price": "1000", "ts_event": stamp}}
            for prices in (invalid, {str(self.instrument): {"price": "4000", "ts_event": stamp + 1_000_000_000}}):
                with self.assertRaisesRegex(ValueError, "路由或时效"):
                    self.call(protocol, "one", "poll", {"cursor": 0, "ready": True, "prices": prices}, lease=lease)
                self.assertFalse(protocol.leases["one"]["ready"])
                self.assertEqual(service.prices.get(self.instrument).price, 3210)
            protocol.stop_owner("one")

    def test_account_valuation_keeps_stopped_owner_contracts_priced_without_running_algorithms(self):
        from scripts.integration.shared_account.server import connect_valuation_market
        from contextlib import ExitStack
        self.native_modules()
        extra = type(self.instrument).from_str("au2612.SHFE")
        class Feed:
            source_id = "raw-risk"
            config = SimpleNamespace(front="tcp://fake", broker_id="9999", user_id="demo",
                production_mode=True, timezone="Asia/Shanghai", dce_use_receive_date=True, future_buffer_ns=100_000_000)
            health_snapshot = SimpleNamespace(state="ready")
            latest_trading_day = "20261013"
            def __init__(self, instrument):
                self.metas = {instrument: SimpleNamespace(instrument_id=instrument)}
                self.depth_observations = {}
                self.connected = 0
                self.subscriptions = []
            def get_instrument_meta(self, i): return self.metas.get(i)
            def register_instrument(self, meta): self.metas[meta.instrument_id] = meta
            def subscribe(self, i, kind): self.subscriptions.append((i, kind.value))
            def connect(self):
                self.connected += 1
                self.latest_receive_monotonic_ns = time.monotonic_ns()
                self.depth_observations = {i: SimpleNamespace(trading_day="20261013", ts_event=time.time_ns(),
                    received_monotonic_ns=self.latest_receive_monotonic_ns, timestamp_regressed=False,
                    reference_price="3210") for i in self.metas}
            def disconnect(self): pass
        first, second = Feed(self.instrument), Feed(extra)
        prices = self.risk.MarketReferencePriceStore()
        def item(feed, instrument):
            return SimpleNamespace(session=SimpleNamespace(upstream=feed,
                client=SimpleNamespace(routes={"main": instrument}),
                runner=SimpleNamespace(start=lambda: (_ for _ in ()).throw(AssertionError("账户估值不得启动算法")))))
        with ExitStack() as resources:
            host = SimpleNamespace(resources=resources, service=SimpleNamespace(prices=prices),
                strategies={"one": item(first, self.instrument), "two": item(second, extra)})
            market = connect_valuation_market(host, {"environment": "realtime", "trading_day": "20261013"})
            self.assertTrue(market.check().ready)
            self.assertEqual((first.connected, second.connected), (1, 0))
            self.assertEqual(len(first.subscriptions), 2)
            self.assertEqual(prices.get(extra).price, 3210)
            # 原策略停止后，真实深度即使价格未变仍能刷新估值，保持剩余风险约束。
            previous = prices.get(extra).ts_event
            first.depth_observations[extra] = SimpleNamespace(trading_day="20261013", ts_event=time.time_ns(),
                received_monotonic_ns=time.monotonic_ns(), timestamp_regressed=False, reference_price="3210")
            self.assertTrue(market.check().ready)
            self.assertGreater(prices.get(extra).ts_event, previous)
            first.depth_observations[extra].reference_price = None
            self.assertFalse(market.check().ready)
            # 无归属、无在途的未运行品种不闭掉其余策略；现有持仓估值仍必须健康。
            host.service.position_manager = SimpleNamespace(attribution=SimpleNamespace(
                state=lambda: {"owned": [{"instrument_id": str(self.instrument), "quantity": "1"}]}))
            host.service._active_instruments = lambda: set()
            self.assertTrue(market.check().ready)
            host.service._active_instruments = lambda: {str(extra)}
            self.assertFalse(market.check().ready)

    def test_readonly_preflight_connects_raw_depth_without_bar_or_runner_start(self):
        from scripts.integration.shared_account.preflight import connect_session_market
        calls = []
        class RawFeed:
            def subscribe(self, instrument, data_type):
                calls.append((str(instrument), data_type.value))
            def connect(self):
                if not calls:
                    raise RuntimeError("缺少订阅")
                calls.append("connected")
        class BarFeed:
            def connect(self):
                raise AssertionError("只读预检不得连接无Bar订阅的聚合器")
        upstream = RawFeed()
        session = SimpleNamespace(upstream=upstream, bar_feed=BarFeed(),
            client=SimpleNamespace(routes={"main": self.instrument, "alias": self.instrument}))
        connected = []
        connect_session_market(session, connected)
        self.assertEqual(calls, [(str(self.instrument), "quote_tick"), "connected"])
        self.assertEqual(connected, [upstream])
        def fail_connect():
            raise RuntimeError("连接部分失败")
        upstream.connect = fail_connect
        cleanup = []
        with self.assertRaisesRegex(RuntimeError, "部分失败"):
            connect_session_market(session, cleanup)
        self.assertEqual(cleanup, [upstream])

    def prepare(self, path, *, resume=False, gross=None, preserve_external=False):
        from bomber.framework.trader.execution.ipc import AccountExecutionProtocol
        modules = self.native_modules()
        account, service, transport, ports, _ = self.native_account(path, modules,
            scoped=True, execution_mode="independent", preserve_external=preserve_external)
        shared = __import__("bomber.framework.trader.execution.shared", fromlist=["SharedRiskBudget"])
        three = service.strategy_client("three", routes={"main": self.instrument},
            budget=shared.SharedRiskBudget(5, 50000))
        ports.append(three)
        protocol = AccountExecutionProtocol(service, keys={sid: f"key-{sid}" for sid in service._ports},
                                            trading_day="20261013")
        if gross is not None:
            transport.gross = dict(gross)
        account.start(resume=resume)
        service.start()
        account.authorize()
        self.addCleanup(lambda: account.driver.stop() if getattr(self, "expected_account_failure", False) else account.stop())
        return modules, account, service, transport, protocol

    def call(self, protocol, sid, action, data, *, lease=None, request_id="r1", key=None):
        return protocol.handle({"version": 1, "strategy_id": sid, "key": key or f"key-{sid}",
            "lease": lease, "request_id": request_id, "action": action, "data": data})

    def attach(self, protocol, sid):
        result = self.call(protocol, sid, "attach", {"configuration": protocol.service._ports[sid].configuration()})
        lease = result["lease"]
        self.call(protocol, sid, "poll", {"cursor": 0, "ready": True}, lease=lease)
        return lease

    def target(self, protocol, sid, quantity, *, revision=1, identity=None):
        from bomber.framework.trader.execution.ipc import wire
        request = self.request(protocol.service.client_id, strategy=sid, quantity=quantity,
            revision=revision, timestamp=self.now)
        if identity:
            request = replace(request, strategy_id=identity)
        return {"request": wire(request), "prices": {str(self.instrument): {"price": "100", "ts_event": self.now}}}

    def test_request_idempotency_conflict_identity_and_opposite_native_orders(self):
        with nullcontext(self.enterContext(TemporaryDirectory())) as directory:
            baseline = {str(self.instrument): (2, 3)}
            modules, account, service, transport, protocol = self.prepare(Path(directory) / "account.json",
                gross=baseline.copy(), preserve_external=True)
            one, two = self.attach(protocol, "one"), self.attach(protocol, "two")
            target = self.target(protocol, "one", 1)
            self.call(protocol, "one", "submit", target, lease=one)
            self.assertTrue(self.call(protocol, "one", "submit", target, lease=one)["duplicate"])
            self.assertEqual(len(transport.sent), 1)
            with self.assertRaisesRegex(RuntimeError, "冲突"):
                self.call(protocol, "one", "submit", self.target(protocol, "one", 2), lease=one)
            with self.assertRaisesRegex(ValueError, "冒用"):
                self.call(protocol, "one", "submit", self.target(protocol, "one", 1, identity="two"), lease=one)
            with self.assertRaisesRegex(ValueError, "身份"):
                self.call(protocol, "one", "describe", {}, key="wrong")
            transport.fill(transport.sent[-1]["OrderRef"])
            self.call(protocol, "two", "submit", self.target(protocol, "two", -1), lease=two)
            transport.fill(transport.sent[-1]["OrderRef"])
            self.assertEqual([row["CombOffsetFlag"] for row in transport.sent], ["0", "0"])
            self.assertEqual(transport.gross[str(self.instrument)], (3, 4))
            self.assertFalse(service.position_manager.attribution.state()["transfers"])
            self.call(protocol, "one", "submit", self.target(protocol, "one", 0, revision=2),
                lease=one, request_id="close-one")
            self.assertEqual(transport.sent[-1]["VolumeTotalOriginal"], 1)
            transport.fill(transport.sent[-1]["OrderRef"])
            account._validate_account()
            self.assertEqual(transport.gross[str(self.instrument)], (2, 4))
            self.assertEqual(service.position_manager.position("one", "main"), 0)
            self.assertEqual(service.position_manager.position("two", "main"), -1)
            self.assertEqual(account.external_gross, baseline)
            protocol.stop_owner("one")
            self.assertIn("two", protocol.leases)
            self.assertEqual(service.position_manager.position("two", "main"), -1)
            protocol.stop_owner("two")

    def test_event_journal_restore_unacknowledged_fill_and_monotonic_ack(self):
        from bomber.framework.trader.execution.ipc import ExecutionEventJournal
        with nullcontext(self.enterContext(TemporaryDirectory())) as directory:
            _, account, service, transport, protocol = self.prepare(Path(directory) / "account.json")
            lease = self.attach(protocol, "one")
            self.call(protocol, "one", "submit", self.target(protocol, "one", 2), lease=lease)
            transport.fill(transport.sent[-1]["OrderRef"], 1)
            rows = self.call(protocol, "one", "poll", {"cursor": 0, "ready": True}, lease=lease)["events"]
            self.assertTrue(any(row["type"] == "FillEvent" for row in rows))
            self.assertFalse(protocol.journal.events["two"])
            persisted = account.manager.repository.load()
            restored = ExecutionEventJournal(service._ports)
            restored.restore_state(persisted.payload["component_states"]["shared_account"]["shared"]["event_journal"])
            self.assertEqual(restored.read("one", 0), rows)
            self.call(protocol, "one", "poll", {"cursor": len(rows), "ready": True}, lease=lease)
            self.assertFalse(protocol.journal.read("one", len(rows)))
            with self.assertRaises(ValueError):
                restored.acknowledge("one", 999)
            with self.assertRaises(ValueError):
                self.call(protocol, "one", "poll", {"cursor": 0, "ready": True}, lease=lease)
            protocol.stop_owner("one")

    def test_account_restart_restores_ownership_and_replays_unacknowledged_events(self):
        with nullcontext(self.enterContext(TemporaryDirectory())) as directory:
            path = Path(directory) / "account.json"
            _, account, service, transport, protocol = self.prepare(path)
            lease = self.attach(protocol, "one")
            self.call(protocol, "one", "submit", self.target(protocol, "one", 2), lease=lease)
            transport.fill(transport.sent[-1]["OrderRef"], 1)
            protocol.stop_owner("one")
            expected = protocol.journal.read("one", 0)
            account.stop()
            _, restored, new_service, new_transport, new_protocol = self.prepare(path, resume=True, gross=transport.gross)
            new_lease = self.attach(new_protocol, "one")
            response = self.call(new_protocol, "one", "poll", {"cursor": 0, "ready": True}, lease=new_lease)
            self.assertEqual(response["events"], expected)
            self.assertEqual(response["state"]["positions"]["main"], "1")
            self.assertFalse(new_transport.sent)
            self.call(new_protocol, "one", "submit", self.target(new_protocol, "one", 1, revision=2),
                      lease=new_lease, request_id="r2")
            self.assertFalse(new_transport.sent)
            self.assertEqual(new_service.position_manager.position("one", "main"), 1)
            new_protocol.stop_owner("one")

    def test_expired_lease_cancels_only_owner_and_new_attach_retains_positions(self):
        with nullcontext(self.enterContext(TemporaryDirectory())) as directory:
            _, _, service, transport, protocol = self.prepare(Path(directory) / "account.json")
            one, two = self.attach(protocol, "one"), self.attach(protocol, "two")
            self.call(protocol, "one", "submit", self.target(protocol, "one", 2), lease=one)
            transport.fill(transport.sent[-1]["OrderRef"], 1)
            with self.assertRaises(RuntimeError):
                self.attach(protocol, "one")
            protocol.leases["one"]["deadline"] = time.monotonic() - 1
            protocol.poll()
            self.assertEqual(len(transport.canceled), 1)
            self.assertIn("two", protocol.leases)
            replacement = self.attach(protocol, "one")
            self.assertNotEqual(one, replacement)
            self.assertEqual(service.position_manager.position("one", "main"), 1)
            with self.assertRaisesRegex(ValueError, "租约"):
                self.call(protocol, "one", "cancel", {}, lease=one)
            protocol.stop_owner("one")
            protocol.stop_owner("two")

    def test_remote_client_mirrors_only_own_fill_and_drains_before_stop(self):
        from bomber.framework.trader.execution.ipc_client import RemoteAccountService, RemoteTraderSession
        from bomber.framework.trader.execution.shared import SharedRiskBudget
        with nullcontext(self.enterContext(TemporaryDirectory())) as directory:
            _, _, service, transport, protocol = self.prepare(Path(directory) / "account.json")
            owner = self
            class Connection:
                sid, lease, sequence = "one", None, 0
                def call(self, action, data):
                    self.sequence += 1
                    result = owner.call(protocol, "one", action, data, lease=self.lease,
                                        request_id=str(self.sequence))
                    if action == "attach":
                        self.lease = result["lease"]
                    return result
            connection = Connection()
            declaration = service._ports["one"].configuration()
            remote = RemoteAccountService(connection, declaration, protocol.state("one"))
            port = remote.strategy_client("one", routes={"main": self.instrument},
                budget=SharedRiskBudget(5, 50000), instrument_limits=service._ports["one"].instrument_limits)
            port.ready_check = lambda: True
            events, completed = [], []
            port.register_execution_event_handler(events.append)
            port.completed = completed.append
            port.start()
            port.drain_events()
            remote.prices.update(self.instrument, 100, self.now)
            port.submit_targets(self.request(service.client_id, strategy="one", quantity=2, timestamp=self.now))
            transport.fill(transport.sent[-1]["OrderRef"], 1)
            port.drain_events()
            self.assertEqual(remote.position_manager.position("one", "main"), 1)

            self.assertEqual(remote.position_manager.position("two", "main"), 0)
            self.assertTrue(RemoteTraderSession(remote, "20261013").is_simnow_session)
            count = len(events)
            port.drain_events()
            self.assertEqual(len(events), count)
            port.begin_stop()
            self.assertTrue(any(getattr(e, "status", None).value == "CANCELED" for e in events if hasattr(e, "status")))
            self.assertFalse(port._started)
            self.assertEqual(completed[-1], port.cursor)
            self.assertEqual(remote.position_manager.position("one", "main"), 1)

            # 行情线程disarm后，worker后续排空不能使用已关闭的租约掩盖行情故障。
            sequence, cursor, delivered = connection.sequence, port.cursor, len(events)
            port.drain_events()
            port.begin_stop()
            self.assertEqual((connection.sequence, port.cursor, len(events)), (sequence, cursor, delivered))

    def test_native_rejection_is_journaled_only_to_owner_and_blocks_new_requests(self):
        with nullcontext(self.enterContext(TemporaryDirectory())) as directory:
            _, _, service, transport, protocol = self.prepare(Path(directory) / "account.json")
            one, two = self.attach(protocol, "one"), self.attach(protocol, "two")
            self.expected_account_failure = True
            self.call(protocol, "one", "submit", self.target(protocol, "one", 1), lease=one)
            ref = transport.sent[-1]["OrderRef"]
            transport.on_order({**transport.raw(ref), "OrderSubmitStatus": "4", "StatusMsg": "controlled rejection"})
            transport.active.pop(ref)
            rows = protocol.journal.read("one", 0)
            self.assertTrue(any(row["event"].get("status") == "REJECTED" for row in rows))
            self.assertFalse(protocol.journal.read("two", 0))
            with self.assertRaises(RuntimeError):
                self.call(protocol, "two", "submit", self.target(protocol, "two", 1), lease=two)
            self.assertEqual(len(transport.sent), 1)
            self.assertFalse(service._ports["one"].is_reconciled)

    def test_three_real_processes_attach_through_unix_socket_without_second_td(self):
        from bomber.framework.trader.execution.ipc import UnixExecutionServer
        with nullcontext(self.enterContext(TemporaryDirectory(dir="/tmp"))) as directory:
            _, _, service, transport, protocol = self.prepare(Path(directory) / "account.json",
                gross={str(self.instrument): (2, 3)}, preserve_external=True)
            server = UnixExecutionServer(Path(directory) / "exec.sock", protocol)
            self.addCleanup(server.close)
            running = threading.Event()
            running.set()
            def control():
                while running.is_set():
                    server.poll()
                    time.sleep(0.005)
            thread = threading.Thread(target=control)
            context = multiprocessing.get_context("fork")
            queue = context.Queue()
            workers = [context.Process(target=process_poll, args=(str(server.path), sid, f"key-{sid}",
                port.configuration(), queue)) for sid, port in service._ports.items()]
            try:
                for worker in workers:
                    worker.start()
                thread.start()
                rows = [queue.get(timeout=10) for _ in workers]
                for worker in workers:
                    worker.join(10)
                    self.assertEqual(worker.exitcode, 0)
                self.assertEqual({row["sid"] for row in rows}, {"one", "two", "three"})
                self.assertFalse(any("error" in row for row in rows), rows)
                self.assertTrue(all(row["state"]["positions"] == {"main": "0"} for row in rows))
                self.assertEqual(transport.starts, 1)
                self.assertFalse(transport.sent)
            finally:
                running.clear()
                thread.join(2)
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                    worker.join()
                queue.close()


if __name__ == "__main__":
    unittest.main()
