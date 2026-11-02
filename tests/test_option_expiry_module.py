"""结算模块边界回归；原生事件宿主记录事件，原生账户由独立验收覆盖。"""
from decimal import Decimal
from types import SimpleNamespace as NS
import sys
import unittest
from unittest.mock import patch

from tests.test_option_by_pos_framework import environment, package, load, Identity, PREFIX


class SimulationModule:
    def __init__(self, config):
        self.alerts = []
        self.clock = NS(timestamp_ns=lambda: 0,
                        set_time_alert_ns=lambda **kwargs: self.alerts.append(kwargs))
    def register_venue(self, exchange): self.exchange = exchange


class Event:
    def __init__(self, **fields): self.__dict__.update(fields)


class BoundedTradeId(Identity):
    """复现原生 TradeId 的 36 字符上限，避免宽松替身漏掉约束。"""
    def __new__(cls, value):
        if not 1 <= len(value) <= 36:
            raise ValueError("TradeId length must be in [1, 36]")
        return super().__new__(cls, value)


class NativeOrder(Event):
    """原生 Order 的 account_id 只读，只能经事件设置。"""
    @property
    def account_id(self): return self._account_id
    def apply(self, submitted): self._account_id = submitted.account_id


class ExpiryModuleTests(unittest.TestCase):
    def setUp(self):
        scope = environment()
        self.env = scope.__enter__()
        self.addCleanup(lambda: scope.__exit__(None, None, None))
        overrides = {
            "bomber.backtest.config": package("config", SimulationModuleConfig=object),
            "bomber.backtest.modules": package("modules", SimulationModule=SimulationModule),
            "bomber.core.data": package("data", Data=object),
            "bomber.core.uuid": package("uuid", UUID4=lambda: "uuid"),
            "bomber.model.data": package("data", CustomData=lambda kind, data: NS(kind=kind, data=data), DataType=lambda cls: cls),
            "bomber.model.enums": package("enums", LiquiditySide=NS(TAKER="TAKER"),
                OrderSide=NS(BUY="BUY", SELL="SELL"), OrderType=NS(MARKET="MARKET")),
            "bomber.model.events": package("events", OrderAccepted=Event, OrderFilled=Event, OrderSubmitted=Event),
            "bomber.model.identifiers": package("identifiers", ClientOrderId=Identity,
                InstrumentId=Identity, TradeId=BoundedTradeId, VenueOrderId=Identity),
            "bomber.model.objects": package("objects", Price=NS(from_str=Decimal), Money=lambda fee, currency: fee),
            "bomber.model.orders": package("orders", MarketOrder=NativeOrder),
        }
        ctx = patch.dict(sys.modules, overrides)
        ctx.__enter__()
        self.addCleanup(lambda: ctx.__exit__(None, None, None))
        self.module = load(PREFIX + ".simulation.option_expiry", "bomber/framework/trader/execution/simulation/option_expiry.py")
        self.code = "MO2606-C-7000.CFFEX"
        self.position = NS(instrument_id=Identity(self.code), quantity=Decimal(2), is_long=False,
            id="POSITION", trader_id="TRADER", strategy_id="GATEWAY", account_id="CFFEX-001", is_closed=False)
        self.cancelled, self.native_events, self.orders, self.reports = [], [], [], []
        self.cache = NS(positions_open=lambda **kw: [] if self.position.is_closed else [self.position],
            instrument=lambda key: NS(quote_currency="CNY"),
            add_order=lambda order, **kw: self.orders.append(order), add_venue_order_id=lambda *args: None)
        def native_send(endpoint, msg):
            self.native_events.append(msg)
            if hasattr(msg, "last_px"):
                self.position.is_closed = True
                self.position.quantity = Decimal(0)
        self.ordinary_cancels = []
        matching = NS(get_open_orders=lambda: ["ORDINARY"], cancel_order=self.ordinary_cancels.append)
        self.exchange = NS(cache=self.cache, msgbus=NS(send=native_send), get_matching_engine=lambda key: matching)

    def create(self, price=0, missing=False):
        item = self.env.calculation.OptionSettlement(self.code, 150, 160, price, "1.2", "fixture")
        obj = self.module.OptionCashSettlementModule("sim", {self.code: 150},
            {} if missing else {self.code: item}, self.reports.append, self.cancelled.append)
        obj.register_venue(self.exchange)
        return obj

    def test_cutoff_cancels_before_matching_and_waits_for_published_final_value(self):
        obj = self.create()
        obj.pre_process(NS(ts_init=149))
        self.assertEqual(self.cancelled, [])
        obj.pre_process(NS(ts_init=150))
        self.assertEqual(self.cancelled, [])  # 最后一根 Bar 仍能撮合早先的委托。
        obj.pre_process(NS(ts_init=151))
        obj.process(159)
        self.assertEqual(self.cancelled, [self.code])
        self.assertEqual(self.ordinary_cancels, ["ORDINARY"])
        self.assertEqual(self.native_events, [])
        self.assertEqual([item["alert_time_ns"] for item in obj.alerts], [151, 160])
        obj._on_clock(NS(ts_event=160))
        obj.process(161)
        self.assertTrue(self.position.is_closed)
        self.assertEqual(len(self.reports), 1)
        report = self.reports[0]
        self.assertEqual((report.ts_event, report.fill_price, report.filled_quantity), (160, 0, 2))
        self.assertEqual(Decimal(report.metadata["commission"]), Decimal("2.4"))
        self.assertEqual(len(self.orders), 1)
        self.assertEqual(self.orders[0].account_id, "CFFEX-001")
        self.assertEqual(len(obj.records), 1)

    def test_terminal_price_precision_is_independent_of_market_tick(self):
        obj = self.create("120.13")
        obj.process(160)
        self.assertEqual(self.native_events[-1].last_px, Decimal("120.13"))
        self.assertEqual(self.native_events[-1].last_qty, 2)

    def test_settlement_identifiers_fit_native_trade_id_and_match_report(self):
        obj = self.create()
        obj.process(160)
        fill = self.native_events[-1]
        token = str(fill.trade_id)
        self.assertLessEqual(len(token), 36)
        self.assertEqual(token, str(fill.client_order_id))
        self.assertEqual(token, str(fill.venue_order_id))
        self.assertEqual(token, self.reports[0].metadata["trade_id"])
        self.assertEqual(token, obj.records[0]["client_order_id"])

    def test_invalid_native_trade_id_fails_before_cache_or_execution_mutation(self):
        obj = self.create()
        with patch.object(self.module, "TradeId", side_effect=ValueError("invalid native trade ID")):
            with self.assertRaisesRegex(ValueError, "invalid native trade ID"):
                obj.process(160)
        self.assertEqual(self.orders, [])
        self.assertEqual(self.native_events, [])
        self.assertEqual(self.reports, [])
        self.assertFalse(self.position.is_closed)

    def test_missing_terminal_value_cannot_silently_clear_remaining_position(self):
        obj = self.create(missing=True)
        with self.assertRaisesRegex(RuntimeError, f"{self.code}:.*SHORT 2.*--option-settlements"):
            obj.process(151)
        self.assertFalse(self.position.is_closed)
        self.assertEqual(self.reports, [])

    def test_close_on_final_bar_needs_no_settlement_file(self):
        obj = self.create(missing=True)
        obj.process(150)  # 最后一根 Bar 撮合前仍有仓位，尚不要求最终结算价。
        self.position.is_closed = True  # 最后一根 Bar 已按普通成交平仓。
        obj.process(151)
        obj.process(160)
        self.assertIn(self.code, obj.settled)
        self.assertEqual(self.reports, [])


if __name__ == "__main__": unittest.main()
