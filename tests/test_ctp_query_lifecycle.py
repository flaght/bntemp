"""公共CTP查询超时/迟到/拒绝的无网络测试；真实Transport，SDK仅为FakeTdApi。"""
from importlib import import_module
import threading
import unittest
from tests import test_execution_composition as fixtures


class CtpQueryLifecycleTests(unittest.TestCase):
    setUp = fixtures.ExecutionCompositionTests.setUp

    def transport(self, behavior=None):
        from tests.test_shared_account import SharedAccountTests
        SharedAccountTests.native_modules(self)
        self.mod = import_module("bomber.framework.trader.execution.ctp.td_transport")
        self.error = import_module("bomber.framework.trader.execution.ctp.query_errors").CtpQueryRejected
        from tests.run_p4_ctp_td_transport import FakeTdApi
        class Api(FakeTdApi):
            def __init__(self):
                super().__init__()
                self.calls = []
            def reqQryTradingAccount(api, data, reqid):
                api.calls.append(reqid)
                return behavior(api, reqid) if behavior else FakeTdApi.reqQryTradingAccount(api, data, reqid)
        transport = self.mod.CtpTdApiTransport(client_id="test", account_id="demo", front="tcp://fake:1234",
            broker_id="9999", investor_id="demo", password="fake", app_id="fake", auth_code="fake",
            td_api_base=Api, timeout_seconds=.02)
        transport.connect(lambda *_: None, lambda *_: None, lambda *_: None)
        transport.activate()
        self.addCleanup(transport.close)
        return transport

    def test_timeout_retains_request_and_blocks_every_later_query_until_final(self):
        t = self.transport(lambda api, rid: 0)
        with self.assertRaises(TimeoutError): t.query_account()
        rid = t._pending.reqid
        with self.assertRaisesRegex(TimeoutError, "旧请求尚未结束"): t.query_account()
        with self.assertRaisesRegex(TimeoutError, "旧请求尚未结束"): t.query_positions()
        self.assertEqual(t._api.calls, [rid])
        self.assertEqual(t._account_revision, 0)
        self.assertTrue(t.query_diagnostics()["pending"]["timed_out"])

    def test_late_final_unblocks_waiter_without_reusing_old_rows_or_request_id(self):
        def behavior(api, rid):
            if len(api.calls) == 1: return 0
            api.onRspQryTradingAccount(dict(BrokerID="9999", AccountID="demo", CurrencyID="CNY",
                Balance=100, Available=80, CurrMargin=20), {}, rid, True)
            return 0
        t = self.transport(behavior)
        with self.assertRaises(TimeoutError): t.query_account()
        old = t._pending.reqid
        t.timeout_seconds = .5
        result = []
        started = threading.Event()
        def query():
            started.set()
            try: result.append(t.query_account())
            except BaseException as error: result.append(error)
        worker = threading.Thread(target=query)
        worker.start()
        self.assertTrue(started.wait(1))
        t._api.onRspQryTradingAccount({"Balance": 999999}, {}, old, False)
        self.assertEqual(t._api.calls, [old])
        t._api.onRspQryTradingAccount({}, {}, old, True)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result), 1)
        self.assertFalse(isinstance(result[0], BaseException), result)
        self.assertEqual(result[0].balances["CNY"].equity, 100)
        self.assertNotEqual(t._api.calls[-1], old)
        self.assertEqual(t._account_revision, 1)
        self.assertIn("late_final", [r["event"] for r in t.query_diagnostics()["recent"]])

    def test_old_or_wrong_kind_final_cannot_release_unfinished_request(self):
        t = self.transport(lambda api, rid: 0)
        with self.assertRaises(TimeoutError): t.query_account()
        rid = t._pending.reqid
        t._response("orders", {}, {}, rid, True)
        t._response("account", {}, {}, rid-1, True)
        with self.assertRaisesRegex(TimeoutError, "旧请求尚未结束"): t.query_account()
        self.assertEqual(t._api.calls, [rid])

    def test_disconnect_wakes_query_waiting_for_old_final(self):
        t = self.transport(lambda api, rid: 0)
        with self.assertRaises(TimeoutError): t.query_account()
        t.timeout_seconds = 5
        errors, started = [], threading.Event()
        def query():
            started.set()
            try: t.query_account()
            except BaseException as error: errors.append(error)
        worker = threading.Thread(target=query)
        worker.start()
        self.assertTrue(started.wait(1))
        t._front_down(4097)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(len(t._api.calls), 1)

    def test_rejected_query_is_typed_not_accepted_and_never_auto_retried(self):
        t = self.transport(lambda api, rid: -2)
        for code in (-2, -3, -1, 7):
            with self.subTest(code=code):
                t._api.reqQryTradingAccount = lambda fields, rid, code=code: code
                with self.assertRaises(self.error) as raised: t.query_account()
                error = raised.exception
                self.assertEqual(error.code, code)
                self.assertEqual(error.retryable, code in (-2,-3))
                self.assertEqual(error.kind, "account")
                self.assertIsNone(t._pending)
        self.assertEqual(t._account_revision, 0)

    def test_error_without_final_does_not_release_query_capacity(self):
        t = self.transport(lambda api, rid: (api.onRspQryTradingAccount({}, {"ErrorID": 42}, rid, False), 0)[1])
        with self.assertRaises(TimeoutError): t.query_account()
        old = t._pending.reqid
        with self.assertRaisesRegex(TimeoutError, "旧请求尚未结束"): t.query_account()
        t._api.onRspQryTradingAccount({}, {}, old, True)
        t._api.reqQryTradingAccount = lambda fields, rid: -3
        with self.assertRaises(self.error): t.query_account()

    def test_rejected_order_and_cancel_are_never_retried(self):
        t = self.transport()
        calls = []
        def reject(fields, rid):
            calls.append(rid)
            return -2
        t._api.reqOrderInsert = t._api.reqOrderAction = reject
        with self.assertRaisesRegex(RuntimeError, "reqOrderInsert请求被拒绝"):
            t.send_order({"OrderRef": "18"})
        t._last_orders["18"] = dict(FrontID=1, SessionID=2, InstrumentID="rb2704", ExchangeID="SHFE")
        with self.assertRaisesRegex(RuntimeError, "reqOrderAction请求被拒绝"):
            t.cancel_order("18")
        self.assertEqual(len(calls), 2)
        self.assertFalse(t._insert_requests or t._action_requests)

    def test_rejected_handshake_is_not_classified_as_query_retry(self):
        t = self.transport()
        t._api.reqAuthenticate = lambda fields, rid: -2
        with self.assertRaises(self.error) as raised:
            t._request("auth", "reqAuthenticate", {})
        self.assertFalse(raised.exception.retryable)

    def test_send_exception_keeps_unconfirmed_request_instead_of_resending(self):
        def behavior(api, rid):
            raise RuntimeError("fixture uncertain API send")
        t = self.transport(behavior)
        with self.assertRaisesRegex(RuntimeError, "uncertain API send"): t.query_account()
        rid = t._pending.reqid
        with self.assertRaisesRegex(TimeoutError, "旧请求尚未结束"): t.query_account()
        self.assertEqual(t._api.calls, [rid])
        t._api.onRspQryTradingAccount({}, {}, rid, True)
        self.assertTrue(t.query_diagnostics()["pending"]["final_received"])


if __name__ == "__main__":
    unittest.main()
