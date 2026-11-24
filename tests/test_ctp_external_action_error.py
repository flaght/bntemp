"""独立CTP通道的外部撤单错误隔离；真实归属账本/Driver/Transport，无网络。"""
from copy import deepcopy
from importlib import import_module
import unittest

from tests import test_execution_composition as fixtures
from tests import test_shared_account as shared_fixtures


class ExternalActionErrorTests(unittest.TestCase):
    setUp = fixtures.ExecutionCompositionTests.setUp

    def channel(self):
        shared_fixtures.SharedAccountTests.native_modules(self)
        direct = import_module("bomber.framework.trader.execution.ctp.independent")
        from tests.run_p4_ctp_td_transport import FakeTdApi

        class Api(FakeTdApi):
            def reqUserLogin(api, data, reqid):
                api.onRspUserLogin(dict(BrokerID="9999", UserID="demo", TradingDay="20260922",
                    MaxOrderRef="17", FrontID=1, SessionID=2), {}, reqid, True)
                return 0

        transport = direct.IndependentTdTransport(client_id="option", account_id="demo",
            front="tcp://fake:1234", broker_id="9999", investor_id="demo", password="fake",
            app_id="fake", auth_code="fake", td_api_base=Api)
        reports, disconnects = [], []
        driver = direct.IndependentCtpDriver("option", "demo", transport,
            enable_simnow_orders=True, disconnect_handler=disconnects.append)
        driver.buckets = direct.StrategyBuckets("20260922")
        driver.bind_durability(lambda *_: None, lambda: None, lambda *_: None)
        driver.start(reports.append)
        self.addCleanup(driver.stop)
        instrument = type(self.instrument).from_str("MO2612-C-7100.CFFEX")
        driver.submit_order(self.contracts.OrderIntent("option-live", "option", instrument,
            "BUY", 2, order_type="LIMIT", price=100, position_effect="OPEN"))
        ref = next(iter(driver._orders))
        own = dict(BrokerID="9999", InvestorID="demo", InstrumentID="MO2612-C-7100",
            ExchangeID="CFFEX", FrontID=1, SessionID=2, OrderRef=ref, OrderSysID="OWN",
            OrderStatus="3", OrderSubmitStatus="3", VolumeTotalOriginal=2,
            VolumeTraded=0, VolumeTotal=2, Direction="0", OffsetFlag="0")
        transport._api.onRtnOrder(own)
        foreign = dict(BrokerID="9999", InvestorID="demo", InstrumentID="p2701",
            ExchangeID="DCE", FrontID=9, SessionID=99, OrderRef="1742", OrderSysID="",
            TradingDay="20260922")
        return driver, transport, reports, disconnects, own, foreign

    @staticmethod
    def error(transport, raw, reqid=None, code=26):
        error = {"ErrorID": code, "ErrorMsg": "CTP:报单已全成交或已撤销，不能再撤"}
        if reqid is None:
            transport._api.onErrRtnOrderAction(raw, error)
        else:
            transport._api.onRspOrderAction(raw, error, reqid, True)

    def test_palm_error_cannot_stop_option_channel_or_change_its_owned_state(self):
        driver, transport, reports, disconnects, own, foreign = self.channel()
        before = (driver.book.snapshot_state(), driver.buckets.snapshot_state(), list(reports),
                  deepcopy(transport._last_orders), dict(transport._action_requests))
        with self.assertLogs("CtpQuery", level="WARNING") as logs:
            self.error(transport, foreign, 203)
            self.error(transport, foreign)
        self.assertTrue(transport._ready and driver.is_simnow_authenticated)
        self.assertFalse(disconnects)
        self.assertEqual((driver.book.snapshot_state(), driver.buckets.snapshot_state(), reports,
                          transport._last_orders, transport._action_requests), before)
        self.assertEqual(len(logs.output), 2)
        errors = [r for r in transport.query_diagnostics()["recent"] if r["event"] == "ctp_error"]
        self.assertEqual([r["request_id"] for r in errors], [203, None])
        self.assertTrue(all(r["external_order"] and r["order_ref"] == "1742" for r in errors))
        self.assertIsNotNone(transport.query_account())
        # 外部错误后，本策略仍正常处理自己的真实成交。
        transport._api.onRtnTrade(dict(own, TradeID="OWN-FILL", Volume=2, Price=100))
        self.assertEqual(driver.buckets.quantities("option-live", "MO2612-C-7100.CFFEX")[0], 2)
        self.assertEqual(reports[-1].report_type.value, "FILLED")
        self.assertEqual(transport._api.request_names.count("insert"), 1)
        self.assertNotIn("cancel", transport._api.request_names)

    def test_foreign_order_ref_and_request_id_collisions_do_not_imply_ownership(self):
        driver, transport, reports, disconnects, own, foreign = self.channel()
        driver.cancel_strategy("option-live")
        reqid = transport._reqid
        before = (driver.book.snapshot_state(), driver.buckets.snapshot_state(), list(reports),
                  dict(transport._action_requests))
        foreign = dict(foreign, OrderRef=own["OrderRef"])
        with self.assertLogs("CtpQuery", level="WARNING"):
            self.error(transport, foreign, reqid)
            self.error(transport, foreign)
        self.assertTrue(transport._ready and driver.is_simnow_authenticated)
        self.assertFalse(disconnects)
        self.assertEqual((driver.book.snapshot_state(), driver.buckets.snapshot_state(), reports,
                          transport._action_requests), before)

    def test_missing_or_invalid_identity_keeps_protection(self):
        for missing in ("BrokerID", "InvestorID", "InstrumentID", "ExchangeID", "OrderRef",
                        "FrontID", "SessionID"):
            with self.subTest(missing=missing):
                _, transport, _, disconnects, _, foreign = self.channel()
                del foreign[missing]
                with self.assertLogs("CtpQuery", level="ERROR"):
                    self.error(transport, foreign)
                self.assertFalse(transport._ready)
                self.assertTrue(disconnects)

    def test_wrong_account_day_or_zero_session_keeps_protection(self):
        for change in ({"BrokerID": "other"}, {"InvestorID": "other"}, {"FrontID": 0},
                       {"SessionID": 0}, {"FrontID": "invalid"}, {"OrderRef": ""},
                       {"TradingDay": "20260923"}, {"FrontID": 9.5}, {"SessionID": True}):
            with self.subTest(change=change):
                _, transport, _, disconnects, _, foreign = self.channel()
                with self.assertLogs("CtpQuery", level="ERROR"):
                    self.error(transport, dict(foreign, **change))
                self.assertFalse(transport._ready)
                self.assertTrue(disconnects)

    def test_unknown_order_in_current_session_keeps_protection(self):
        for front, sid in ((1, 2), ("0001", "0002")):
            with self.subTest(front=front, sid=sid):
                _, transport, _, disconnects, _, foreign = self.channel()
                foreign.update(FrontID=front, SessionID=sid)
                with self.assertLogs("CtpQuery", level="ERROR"):
                    self.error(transport, foreign)
                self.assertFalse(transport._ready)
                self.assertTrue(disconnects)

    def test_verified_foreign_cancel_errors_do_not_depend_on_error_code_26(self):
        for code in (25, 31, 90):
            with self.subTest(code=code):
                driver, transport, reports, disconnects, _, foreign = self.channel()
                before = list(reports)
                with self.assertLogs("CtpQuery", level="WARNING"):
                    self.error(transport, foreign, code=code)
                self.assertTrue(transport._ready and driver.is_simnow_authenticated)
                self.assertFalse(disconnects)
                self.assertEqual(reports, before)

    def test_real_network_disconnect_still_closes_independent_channel(self):
        driver, transport, _, disconnects, _, _ = self.channel()
        transport._api.onFrontDisconnected(8193)
        self.assertFalse(transport._ready or driver.is_simnow_authenticated)
        self.assertEqual(disconnects, ["ctp_front_disconnected:8193"])

    def test_restored_owned_session_is_not_misclassified_as_external(self):
        driver, transport, _, disconnects, own, _ = self.channel()
        record = driver.book.records[own["OrderRef"]]
        record["session"][:2] = ["7", "70"]
        driver.book._index()
        with self.assertLogs("CtpQuery", level="ERROR"):
            self.error(transport, dict(own, FrontID=7, SessionID=70))
        self.assertFalse(transport._ready)
        self.assertTrue(disconnects)

    def test_foreign_session_with_owned_exchange_order_id_is_ambiguous(self):
        _, transport, _, disconnects, own, foreign = self.channel()
        with self.assertLogs("CtpQuery", level="ERROR"):
            self.error(transport, dict(own, FrontID=foreign["FrontID"],
                                      SessionID=foreign["SessionID"]))
        self.assertFalse(transport._ready)
        self.assertTrue(disconnects)

    def test_foreign_exchange_order_hint_alone_cannot_bypass_protection(self):
        driver, transport, _, disconnects, _, foreign = self.channel()
        foreign.update(OrderSysID="FOREIGN", OrderStatus="3")
        transport._api.onRtnOrder(foreign)
        self.assertIn(("DCE", "FOREIGN"), driver.book.foreign)
        del foreign["SessionID"]
        with self.assertLogs("CtpQuery", level="ERROR"):
            self.error(transport, foreign)
        self.assertFalse(transport._ready)
        self.assertTrue(disconnects)

    def test_own_nonterminal_error_still_closes_gate(self):
        driver, transport, _, disconnects, own, _ = self.channel()
        driver.cancel_strategy("option-live")
        with self.assertLogs("CtpQuery", level="ERROR"):
            self.error(transport, own, transport._reqid)
        self.assertFalse(transport._ready)
        self.assertEqual(disconnects, ["ctp_front_disconnected:-26"])

    def test_own_terminal_cancel_race_still_uses_existing_verified_path(self):
        driver, transport, reports, disconnects, own, _ = self.channel()
        driver.cancel_strategy("option-live")
        reqid = transport._reqid
        transport._api.onRtnTrade(dict(own, TradeID="OWN-FILL", Volume=2, Price=100))
        before = list(reports)
        with self.assertLogs("CtpQuery", level="WARNING"):
            self.error(transport, own, reqid)
            self.error(transport, own)
        self.assertTrue(transport._ready and driver.is_simnow_authenticated)
        self.assertFalse(disconnects)
        self.assertEqual(reports, before)


if __name__ == "__main__":
    unittest.main()
