"""公共CTP查询超时/迟到/拒绝的无网络测试；真实Transport，SDK仅为FakeTdApi。"""
from importlib import import_module
import threading
import time
import unittest
from unittest.mock import patch
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

    def test_disconnect_reason_is_available_before_handler_and_survives_handler_error(self):
        from types import SimpleNamespace
        from tests.test_shared_account import SharedAccountTests
        SharedAccountTests.native_modules(self)
        from bomber.framework.trader.execution.ctp.native_driver import CtpNativeTraderDriver
        observations=[]
        def disconnected(message):
            observations.append((message,driver._connected,dict(driver.last_disconnect)))
            raise RuntimeError('handler failed')
        driver=CtpNativeTraderDriver('test','demo',SimpleNamespace(),disconnect_handler=disconnected)
        self.assertIsNone(driver.last_disconnect)
        driver._connected=True
        with patch('bomber.framework.trader.execution.ctp.native_driver.time.time_ns',return_value=12345):
            with self.assertRaisesRegex(RuntimeError,'handler failed'):
                driver.on_disconnect(8193)
        self.assertEqual(observations,[('ctp_front_disconnected:8193',False,{'reason':8193,'ts_ns':12345})])
        self.assertEqual(driver.last_disconnect,{'reason':8193,'ts_ns':12345})

    def test_timeout_audit_counts_partial_and_late_responses_without_using_incomplete_rows(self):
        def partial(api, reqid):
            api.onRspQryTradingAccount({'Balance':123}, {}, reqid, False)
            return 0
        t=self.transport(partial)
        with self.assertRaises(TimeoutError):t.query_account()
        pending=t.query_diagnostics()['pending']
        self.assertEqual(pending['response_count'],1)
        self.assertEqual(pending['row_count'],1)
        self.assertIsNotNone(pending['last_response_age_seconds'])
        self.assertFalse(pending['final_received'])
        self.assertFalse(t._pending.rows)
        timeout=t.query_diagnostics()['recent'][-1]
        self.assertEqual(timeout['event'],'timeout')
        self.assertGreater(timeout['ts_ns'],0)
        self.assertEqual(timeout['response_count'],1)
        t._api.onRspQryTradingAccount({'Balance':456},{},pending['request_id'],True)
        late=t.query_diagnostics()['recent'][-1]
        self.assertEqual(late['event'],'late_final')
        self.assertEqual(late['response_count'],2)
        self.assertEqual(late['row_count'],2)
        self.assertFalse(t._pending.rows)  # 超时后的真实回报只诊断，不冒充完整快照。

    def test_no_response_timeout_is_distinct_from_partial_response(self):
        t=self.transport(lambda api,reqid:0)
        with self.assertRaises(TimeoutError):t.query_account()
        pending=t.query_diagnostics()['pending']
        self.assertEqual(pending['response_count'],0)
        self.assertEqual(pending['row_count'],0)
        self.assertIsNone(pending['last_response_age_seconds'])

    def test_trade_query_preserves_real_execution_details(self):
        t = self.transport()
        def query(fields, reqid):
            self.assertEqual(fields, {'BrokerID':'9999','InvestorID':'demo'})
            t._api.onRspQryTrade(dict(BrokerID='9999',InvestorID='demo',InstrumentID='p2701',
                ExchangeID='DCE',OrderRef='223',OrderSysID='SYS223',TradeID='REAL223',Direction='0',
                TradingDay='20260922',TradeDate='20260922',TradeTime='10:14:59',OffsetFlag='1',
                Volume=1,Price=8981),{},reqid,False)
            t._api.onRspQryTrade({}, {}, reqid, True)
            return 0
        t._api.reqQryTrade = query
        rows = t.query_trades()
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['TradeID'],rows[0]['Price'],rows[0]['Volume']),('REAL223',8981,1))
        with self.assertRaises(TypeError): rows[0]['Price']=1

    def test_trade_query_requires_new_native_binding_and_complete_final_reply(self):
        t = self.transport()
        with self.assertRaisesRegex(RuntimeError,'0.1.2'): t.query_trades()
        t._api.reqQryTrade = lambda fields, reqid: 0
        with self.assertRaises(TimeoutError): t.query_trades()
        self.assertEqual(t._pending.kind,'trades')
        self.assertTrue(t._pending.timed_out)

    def test_nonqueueing_terminal_statuses_are_excluded_from_active_order_query(self):
        t = self.transport()
        states = ('0', '1', '2', '3', '4', '5', 'a', 'b', 'c')
        def query(fields, reqid):
            for number, status in enumerate(states):
                filled = 2 if status == '0' else 1 if status in {'1', '2'} else 0
                row = dict(BrokerID='9999', InvestorID='demo', InstrumentID='p2701', ExchangeID='DCE',
                    OrderRef=str(number), OrderStatus=status, OrderSubmitStatus='3', Direction='1',
                    VolumeTotalOriginal=2, VolumeTraded=filled, VolumeTotal=2-filled)
                t._api.onRspQryOrder(row, {}, reqid, number == len(states)-1)
            return 0
        t._api.reqQryOrder = query
        orders = t.query_active_orders().orders
        expected = [str(i) for i, status in enumerate(states) if status in {'1', '3', 'a', 'b', 'c'}]
        self.assertEqual([row.client_order_id.rsplit('-', 1)[-1] for row in orders], expected)
        self.assertTrue(all(row.instrument_id == 'p2701.DCE' for row in orders))
        self.assertEqual(t.order_query_diagnostics['CTP-9999-demo-20260922-2']['OrderStatus'], '2')
        self.assertEqual(t.order_query_diagnostics['CTP-9999-demo-20260922-2']['VolumeTraded'], '1')

    def test_stop_before_query_sends_no_request(self):
        t = self.transport()
        t.query_stop_requested = lambda: True
        with self.assertRaises(self.mod.CtpQueryInterrupted):
            t.query_account()
        self.assertFalse(t._api.calls)
        self.assertIsNone(t._pending)

    def test_window_stop_interrupts_long_wait_but_retains_identity_until_late_final(self):
        sent, stop = threading.Event(), threading.Event()
        def behavior(api, rid):
            if len(api.calls) == 1:
                sent.set()
                return 0
            api.onRspQryTradingAccount(dict(BrokerID="9999", AccountID="demo", CurrencyID="CNY",
                Balance=200, Available=180, CurrMargin=20), {}, rid, True)
            return 0
        t = self.transport(behavior)
        t.timeout_seconds = 120
        t.query_stop_requested = stop.is_set
        errors = []
        def query():
            try:
                t.query_account()
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=query, daemon=True)
        worker.start()
        try:
            self.assertTrue(sent.wait(1))
            stop.set()
            worker.join(1)
            self.assertFalse(worker.is_alive(), "调度停止不应等待完整120秒")
        finally:
            stop.set()
            worker.join(1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], self.mod.CtpQueryInterrupted)
        old = t._pending.reqid
        self.assertTrue(t._pending.timed_out)
        self.assertEqual(t._account_revision, 0)
        self.assertEqual(t.query_diagnostics()["recent"][-1]["event"], "interrupted")
        # 停机撤单请求不经过只读查询等待；旧查询未结束也能请求撤单。
        t._last_orders['17'] = dict(FrontID=1, SessionID=2, InstrumentID='rb2701', ExchangeID='SHFE')
        t.cancel_order('17')
        self.assertIn('cancel', t._api.request_names)
        self.assertEqual(t._pending.reqid, old)
        self.assertFalse(t._pending.done)
        t._api.onRspQryTradingAccount(dict(Balance=999), {}, old, True)
        self.assertTrue(t._pending.done)
        self.assertFalse(t._pending.rows)
        t.query_stop_requested = None
        self.assertEqual(t.query_account().balances['CNY'].equity, 200)
        self.assertEqual(len(t._api.calls), 2)
        self.assertNotEqual(t._api.calls[-1], old)

    def test_stop_interrupts_wait_for_previous_without_sending_another_query(self):
        t = self.transport(lambda api, rid: 0)
        with self.assertRaises(TimeoutError):
            t.query_account()
        old = t._pending.reqid
        stop, waiting = threading.Event(), threading.Event()
        t.timeout_seconds = 120
        t.query_stop_requested = stop.is_set
        errors = []
        wait = t._cv.wait
        def observed_wait(seconds):
            waiting.set()
            return wait(seconds)
        def query():
            try:
                t.query_account()
            except BaseException as error:
                errors.append(error)
        with patch.object(t._cv, 'wait', side_effect=observed_wait):
            worker = threading.Thread(target=query, daemon=True)
            worker.start()
            try:
                self.assertTrue(waiting.wait(1))
                stop.set()
                worker.join(1)
                self.assertFalse(worker.is_alive())
            finally:
                stop.set()
                worker.join(1)
        self.assertIsInstance(errors[0], self.mod.CtpQueryInterrupted)
        self.assertEqual(t._pending.reqid, old)
        self.assertEqual(len(t._api.calls), 1)

    def test_timeout_retains_request_and_blocks_every_later_query_until_final(self):
        t = self.transport(lambda api, rid: 0)
        with self.assertRaises(TimeoutError): t.query_account()
        rid = t._pending.reqid
        with self.assertRaisesRegex(TimeoutError, "旧请求尚未结束"): t.query_account()
        with self.assertRaisesRegex(TimeoutError, "旧请求尚未结束"): t.query_positions()
        self.assertEqual(t._api.calls, [rid])
        self.assertEqual(t._account_revision, 0)
        self.assertTrue(t.query_diagnostics()["pending"]["timed_out"])

    def test_16_second_response_times_out_at_15_but_completes_at_30(self):
        t = self.transport(lambda api, rid: 0)
        for budget in (15, 30):
            with self.subTest(timeout=budget):
                now = [time.monotonic()]
                t.timeout_seconds = budget
                def wait(remaining):
                    now[0] += min(16.396848, remaining)
                    if remaining >= 16.396848:
                        t._api.onRspQryTradingAccount(dict(BrokerID="9999", AccountID="demo", CurrencyID="CNY",
                            Balance=100, Available=80, CurrMargin=20), {}, t._pending.reqid, True)
                with patch.object(t._cv, "wait", side_effect=wait), \
                        patch.object(self.mod.time, "monotonic", side_effect=lambda: now[0]):
                    if budget == 15:
                        with self.assertRaises(TimeoutError): t.query_account()
                    else:
                        self.assertEqual(t.query_account().balances["CNY"].equity, 100)
                if budget == 15:
                    t._api.onRspQryTradingAccount({}, {}, t._pending.reqid, True)
        self.assertEqual(t._account_revision, 1)
        self.assertEqual(t.query_diagnostics()["timeout_seconds"], 30)
        self.assertEqual(t.query_diagnostics()["recent"][-1]["timeout_seconds"], 30)
        self.assertEqual(t.query_diagnostics()["recent"][-1]["event"], "final")

    def test_handshake_phases_log_budget_and_elapsed_at_info_without_credentials(self):
        with self.assertLogs("CtpQuery", level="INFO") as logs:
            t = self.transport()
        text = "\n".join(logs.output)
        for kind in ("auth", "login", "settlement"):
            self.assertIn(f"'kind': '{kind}'", text)
        self.assertIn("elapsed_seconds", text)
        self.assertIn("timeout_seconds", text)
        for secret in ("password", "auth_code", "BrokerID", "Available"):
            self.assertNotIn(secret, text)
        self.assertEqual(t.query_diagnostics()["timeout_seconds"], .02)

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
