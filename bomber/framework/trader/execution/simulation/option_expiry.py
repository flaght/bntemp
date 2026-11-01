"""以原生结算订单/成交事件更新账户，不通过市场 Bar 伪造结算价格。"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from heapq import heapify, heappop
from uuid import uuid4

from bomber.backtest.config import SimulationModuleConfig
from bomber.backtest.modules import SimulationModule
from bomber.core.data import Data
from bomber.core.uuid import UUID4
from bomber.model.data import CustomData, DataType
from bomber.model.enums import LiquiditySide, OrderSide as NativeSide, OrderType
from bomber.model.events import OrderAccepted, OrderFilled, OrderSubmitted
from bomber.model.identifiers import ClientOrderId, InstrumentId, TradeId, VenueOrderId
from bomber.model.objects import Money, Price
from bomber.model.orders import MarketOrder

from ..contracts import ExecutionReport, ExecutionReportType, OrderSide, PositionEffect
from ..option_settlement import SETTLEMENT_ORIGIN


class SettlementClock(Data):
    """仅推进内核时钟的控制数据；不更新任何标的价格或成交量。"""
    def __init__(self, timestamp_ns):
        self.timestamp_ns = timestamp_ns

    @property
    def ts_event(self):
        return self.timestamp_ns

    @property
    def ts_init(self):
        return self.timestamp_ns


def settlement_clock(timestamp_ns):
    return CustomData(DataType(SettlementClock), SettlementClock(timestamp_ns))


class OptionCashSettlementModule(SimulationModule):
    """显式管理期权生命周期，现金结算真实残仓，允许零终值。

    合约需由 make_option(expiry_policy='CASH_SETTLEMENT') 创建，避免内核同时
    执行另一套到期处理。原生订单/成交和ExecEngine仍负责资金及Position入账。
    """
    def __init__(self, backend_id, last_trade_times, settlements, report_sink, cancel_pending):
        super().__init__(SimulationModuleConfig())
        self.backend_id = backend_id
        self.last_trade_times = dict(last_trade_times)
        self.settlements = dict(settlements)
        for code, stamp in self.last_trade_times.items():
            if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0:
                raise ValueError(f"Invalid option last trading time: {code}")
            InstrumentId.from_str(code)
        for code, item in self.settlements.items():
            if code != item.instrument_id or self.last_trade_times.get(code) != item.last_trade_ns:
                raise ValueError(f"Settlement lifecycle differs from registered option: {code}")
        # 15:00 的已完成 Bar 属于最后交易区间。延后 1ns 封闭生命周期，
        # 让所有合约同一时刻的最后一根 Bar 先完成撮合，消除合约遍历顺序影响。
        self._cutoffs = [(stamp + 1, code) for code, stamp in self.last_trade_times.items()]
        heapify(self._cutoffs)
        self.report_sink = report_sink
        self.cancel_pending = cancel_pending
        self.expired = set()
        self.settled = set()
        self.records = []

    def register_venue(self, exchange):
        super().register_venue(exchange)
        times = {stamp + 1 for stamp in self.last_trade_times.values()} | {
            max(item.available_ns, item.last_trade_ns + 1) for item in self.settlements.values()}
        for stamp in sorted(times):
            if stamp >= self.clock.timestamp_ns():
                self.clock.set_time_alert_ns(name=f"OPTION-SETTLEMENT:{self.backend_id}:{stamp}",
                    alert_time_ns=stamp, callback=self._on_clock)

    def _on_clock(self, event):
        self.process(event.ts_event)

    def pre_process(self, data):
        # 仅允许到最后交易时刻为止的已完成 Bar；之后的行情先撤单再撮合。
        self._expire(int(data.ts_init))

    def _expire(self, now):
        while self._cutoffs and self._cutoffs[0][0] <= now:
            _, code = heappop(self._cutoffs)
            self.expired.add(code)
            instrument_id = InstrumentId.from_str(code)
            self.cancel_pending(instrument_id)
            matching = self.exchange.get_matching_engine(instrument_id)
            if matching is None:
                raise RuntimeError(f"Missing matching engine for expiring option {code}")
            for order in tuple(matching.get_open_orders()):
                matching.cancel_order(order)

    def process(self, ts_now):
        now = int(ts_now)
        self._expire(now)
        for code in sorted(self.expired - self.settled):
            instrument_id = InstrumentId.from_str(code)
            positions = tuple(self.exchange.cache.positions_open(venue=None, instrument_id=instrument_id))
            item = self.settlements.get(code)
            if item is None:
                if positions:
                    raise RuntimeError(f"Missing final settlement for held option {code}")
                self.settled.add(code)
                continue
            if now < item.available_ns:
                continue
            # 标记在发事件前完成，避免同步回报重入导致重复结算；失败则终止本次回测。
            self.settled.add(code)
            for position in positions:
                self._settle(position, item, now)

    def _settle(self, position, item, now):
        cache = self.exchange.cache
        contract = cache.instrument(position.instrument_id)
        side = NativeSide.SELL if position.is_long else NativeSide.BUY
        token = f"OPTION-SETTLEMENT-{uuid4().hex}"
        client_id, venue_id = ClientOrderId(token), VenueOrderId(token)
        order = MarketOrder(trader_id=position.trader_id, strategy_id=position.strategy_id,
            instrument_id=position.instrument_id, client_order_id=client_id, order_side=side,
            quantity=position.quantity, init_id=UUID4(), ts_init=now, reduce_only=True,
            tags=["OPTION_CASH_SETTLEMENT"])
        # account_id 是原生Order的只读属性，通过合法状态事件设置。
        order.apply(OrderSubmitted(trader_id=position.trader_id, strategy_id=position.strategy_id,
            instrument_id=position.instrument_id, client_order_id=client_id,
            account_id=position.account_id, event_id=UUID4(), ts_event=now, ts_init=now))
        cache.add_order(order, position_id=position.id)
        cache.add_venue_order_id(client_id, venue_id)
        common = dict(trader_id=position.trader_id, strategy_id=position.strategy_id,
            instrument_id=position.instrument_id, client_order_id=client_id,
            venue_order_id=venue_id, account_id=position.account_id, ts_event=now, ts_init=now)
        self.exchange.msgbus.send(endpoint="ExecEngine.process", msg=OrderAccepted(
            **common, event_id=UUID4()))
        quantity = Decimal(str(position.quantity))
        fee = (quantity * item.fee_per_contract).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        # 最终结算价可以有比市场 tick 更细的精度，不按交易 tick 舍入。
        native_price = Price.from_str(format(item.price, "f"))
        self.exchange.msgbus.send(endpoint="ExecEngine.process", msg=OrderFilled(
            **common, trade_id=TradeId(token), position_id=position.id, order_side=side,
            order_type=OrderType.MARKET, last_qty=position.quantity, last_px=native_price,
            currency=contract.quote_currency, commission=Money(fee, contract.quote_currency),
            liquidity_side=LiquiditySide.TAKER, event_id=UUID4()))
        remaining = cache.positions_open(venue=None, instrument_id=position.instrument_id)
        if any(item_position.id == position.id and not item_position.is_closed for item_position in remaining):
            raise RuntimeError(f"Native settlement did not close {position.instrument_id}")
        self.report_sink(ExecutionReport(self.backend_id, token, position.instrument_id,
            ExecutionReportType.FILLED, now, quantity, item.price,
            OrderSide.SELL if side == NativeSide.SELL else OrderSide.BUY, quantity,
            PositionEffect.CLOSE, report_id=token, sequence=1,
            metadata={"execution_origin": SETTLEMENT_ORIGIN, "trade_id": token,
                "venue_order_id": token, "commission": str(fee),
                "commission_currency": str(contract.quote_currency), "settlement_source": item.source,
                "last_trade_ns": item.last_trade_ns, "available_ns": item.available_ns}))
        self.records.append(dict(instrument_id=str(position.instrument_id), quantity=str(quantity),
            side="SELL" if side == NativeSide.SELL else "BUY", price=str(item.price),
            commission=str(fee), ts_event=now, available_ns=item.available_ns,
            last_trade_ns=item.last_trade_ns, client_order_id=token, source=item.source))

    def log_diagnostics(self, logger):
        logger.info(f"Option cash settlements: {len(self.records)}")

    def reset(self):
        self.expired.clear()
        self.settled.clear()
        self.records.clear()
        self._cutoffs = [(stamp + 1, code) for code, stamp in self.last_trade_times.items()]
        heapify(self._cutoffs)
