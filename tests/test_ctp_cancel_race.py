"""真实Transport/Driver的成交撤单竞态；只替换无网络CTP SDK。"""
from importlib import import_module
import unittest

from tests import test_execution_composition as fixtures
from tests import test_shared_account as shared_fixtures


class CtpCancelRaceTests(unittest.TestCase):
    setUp = fixtures.ExecutionCompositionTests.setUp

    def working_order(self):
        shared_fixtures.SharedAccountTests.native_modules(self)
        transport_module = import_module("bomber.framework.trader.execution.ctp.td_transport")
        driver_module = import_module("bomber.framework.trader.execution.ctp.native_driver")
        from tests.run_p4_ctp_td_transport import FakeTdApi
        transport = transport_module.CtpTdApiTransport(client_id="test", account_id="demo",
            front="tcp://fake:1234", broker_id="9999", investor_id="demo", password="fake",
            app_id="fake", auth_code="fake", td_api_base=FakeTdApi)
        reports, disconnects = [], []
        driver = driver_module.CtpNativeTraderDriver("test", "demo", transport,
            enable_simnow_orders=True, disconnect_handler=disconnects.append)
        driver.start(reports.append)
        self.addCleanup(driver.stop)
        order = self.contracts.OrderIntent("one", "test", self.instrument, "BUY", 2,
            order_type="LIMIT", price=3000, position_effect="OPEN")
        driver.submit_order(order)
        ref = next(iter(driver._orders))
        symbol, _, venue = str(self.instrument).rpartition(".")
        raw = dict(BrokerID="9999", InvestorID="demo", InstrumentID=symbol, ExchangeID=venue,
            OrderRef=ref, OrderSysID="SYS18", FrontID=driver._session.front_id,
            SessionID=driver._session.session_id, OrderStatus="3", OrderSubmitStatus="3",
            VolumeTotalOriginal=2, VolumeTraded=0, VolumeTotal=2)
        transport._api.onRtnOrder(raw)
        driver.cancel_strategy("one")
        return driver, transport, reports, disconnects, raw, transport._reqid

    def fill(self, transport, raw, quantity=2):
        transport._api.onRtnTrade({**raw, "TradeID": "REAL-1", "Volume": quantity, "Price": 3000})

    def rejection(self, transport, raw, reqid, code=26):
        # 实际撤单按FrontID/SessionID/OrderRef发送，没有指定OrderSysID；
        # InputOrderAction回报的这个字段可以为空，与缓存的交易所订单号不同。
        transport._api.onRspOrderAction({**raw, "OrderSysID": ""}, {"ErrorID": code,
            "ErrorMsg": "CTP:报单已全成交或已撤销，不能再撤"}, reqid, True)

    def test_filled_before_cancel_rejection_and_duplicate_callback_keeps_connection(self):
        driver, transport, reports, disconnects, raw, reqid = self.working_order()
        self.fill(transport, raw)
        before = list(reports)
        # Trade已完整收到，最终OnRtnOrder还未到；不能只根据缓存OrderStatus判断。
        self.assertEqual(transport._last_orders[raw["OrderRef"]]["OrderStatus"], "3")
        with self.assertLogs("CtpQuery", level="WARNING") as logs:
            self.rejection(transport, raw, reqid)
            transport._api.onErrRtnOrderAction(raw, {"ErrorID": 26, "ErrorMsg": "duplicate"})
        self.assertTrue(transport._ready and driver.is_simnow_authenticated)
        self.assertFalse(disconnects)
        self.assertEqual(reports, before)
        self.assertEqual(len([r for r in reports if r.report_type.value == "FILLED"]), 1)
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(all(r["terminal_confirmed"] for r in transport.query_diagnostics()["recent"][-2:]))
        # 同一个连接仍能正常查询，且未生成新的委托。
        self.assertIsNotNone(transport.query_account())
        self.assertEqual(transport._api.request_names.count("insert"), 1)
        self.assertEqual(transport._api.request_names.count("cancel"), 1)

    def test_cancel_terminal_requires_all_reported_trades_before_ignoring_26(self):
        for filled in (0, 1):
            with self.subTest(filled=filled):
                driver, transport, reports, disconnects, raw, reqid = self.working_order()
                if filled:
                    self.fill(transport, raw, filled)
                transport._api.onRtnOrder({**raw, "OrderStatus": "5", "VolumeTraded": filled,
                                           "VolumeTotal": 2-filled})
                before = list(reports)
                with self.assertLogs("CtpQuery", level="WARNING"):
                    self.rejection(transport, raw, reqid)
                self.assertTrue(transport._ready and driver.is_simnow_authenticated)
                self.assertFalse(disconnects)
                self.assertEqual(reports, before)
                self.assertEqual(reports[-1].report_type.value, "CANCELED")

    def test_terminal_order_without_complete_trade_reports_still_closes_gate(self):
        for status, reported, delivered in (("0", 2, 0), ("5", 1, 0), ("5", 2, 1)):
            with self.subTest(status=status, reported=reported, delivered=delivered):
                driver, transport, reports, disconnects, raw, reqid = self.working_order()
                if delivered:
                    self.fill(transport, raw, delivered)
                transport._api.onRtnOrder({**raw, "OrderStatus": status,
                    "VolumeTraded": reported, "VolumeTotal": 2-reported})
                self.assertFalse(driver._is_order_terminal(raw["OrderRef"]))
                before = list(reports)
                with self.assertLogs("CtpQuery", level="ERROR"):
                    self.rejection(transport, raw, reqid)
                self.assertFalse(transport._ready)
                self.assertEqual(disconnects, ["ctp_front_disconnected:-26"])
                self.assertEqual(reports, before)

    def test_working_or_partially_filled_order_still_closes_gate(self):
        for filled in (0, 1):
            with self.subTest(filled=filled):
                _, transport, _, disconnects, raw, reqid = self.working_order()
                if filled:
                    self.fill(transport, raw, filled)
                with self.assertLogs("CtpQuery", level="ERROR"):
                    self.rejection(transport, raw, reqid)
                self.assertFalse(transport._ready)
                self.assertEqual(disconnects, ["ctp_front_disconnected:-26"])

    def test_terminal_rejection_must_match_cancel_identity_and_request(self):
        for changed, request in (({"InstrumentID": "other"}, "same"),
                ({"OrderRef": "999"}, "same"), ({"BrokerID": "other"}, "same"),
                ({"SessionID": 999}, "same"), ({}, "unknown"), ({}, "never_sent")):
            with self.subTest(changed=changed, request=request):
                _, transport, _, disconnects, raw, reqid = self.working_order()
                self.fill(transport, raw)
                if request == "unknown":
                    reqid += 999
                elif request == "never_sent":
                    transport._action_requests.clear()
                    reqid = None
                with self.assertLogs("CtpQuery", level="ERROR"):
                    self.rejection(transport, {**raw, **changed}, reqid)
                self.assertFalse(transport._ready)
                self.assertEqual(len(disconnects), 1)

    def test_other_error_and_unknown_order_are_not_ignored(self):
        for code, known in ((25, True), (31, True), (26, False)):
            with self.subTest(code=code, known=known):
                _, transport, _, disconnects, raw, reqid = self.working_order()
                self.fill(transport, raw)
                if not known:
                    transport._last_orders.clear()
                with self.assertLogs("CtpQuery", level="ERROR"):
                    self.rejection(transport, raw, reqid, code)
                self.assertFalse(transport._ready)
                self.assertEqual(disconnects, [f"ctp_front_disconnected:{-code if known else -1}"])

    def test_partial_error_payload_requires_verified_cancel_request(self):
        for payload in ({}, {"OrderRef": "18"}):
            with self.subTest(payload=payload):
                driver, transport, reports, disconnects, raw, reqid = self.working_order()
                self.fill(transport, raw)
                before = list(reports)
                with self.assertLogs("CtpQuery", level="WARNING"):
                    self.rejection(transport, payload, reqid)
                self.assertTrue(transport._ready and driver.is_simnow_authenticated)
                self.assertFalse(disconnects)
                self.assertEqual(reports, before)

    def test_recovery_or_failed_terminal_processing_does_not_confirm_completion(self):
        for state in ("recovery", "disconnected", "unbound"):
            with self.subTest(state=state):
                driver, transport, _, _, raw, reqid = self.working_order()
                self.fill(transport, raw)
                if state == "recovery":
                    driver._replaying_recovery = True
                elif state == "disconnected":
                    driver._connected = False
                else:
                    transport._terminal_order_check = None
                with self.assertLogs("CtpQuery", level="ERROR"):
                    self.rejection(transport, raw, reqid)
                self.assertFalse(transport._ready)

    def test_real_disconnect_after_terminal_order_still_closes_gate(self):
        driver, transport, _, disconnects, raw, _ = self.working_order()
        self.fill(transport, raw)
        transport._api.onFrontDisconnected(8193)
        self.assertFalse(transport._ready or driver.is_simnow_authenticated)
        self.assertEqual(disconnects, ["ctp_front_disconnected:8193"])


if __name__ == "__main__":
    unittest.main()
