"""中立OrderIntent与Nautilus原生Strategy订单API之间的内部网关。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from bomber.model.data import Bar, QuoteTick, TradeTick
from bomber.model.enums import OrderSide as NativeOrderSide
from bomber.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderDenied,
    OrderExpired,
    OrderFilled,
    OrderRejected,
)
from bomber.model.identifiers import ClientOrderId
from bomber.trading.config import StrategyConfig
from bomber.trading.strategy import Strategy

from bomber.framework.trader.execution.contracts import (
    ExecutionReport,
    ExecutionReportType,
    OrderIntent,
    OrderSide,
    OrderType,
    PositionEffect,
)


@dataclass
class _PendingOrder:
    intent: OrderIntent
    native_order: Any
    after_ts: int
    remaining_bars: int
    last_market_ts: int


class NautilusOrderGateway(Strategy):
    """Backend内部唯一可以调用Nautilus原生下单API的Strategy宿主。"""

    def __init__(
        self,
        backend_id: str,
        report_sink: Callable[[ExecutionReport], None],
        *,
        delay_bars: int = 1,
    ) -> None:
        super().__init__(StrategyConfig())
        self._backend_id = backend_id
        self._report_sink = report_sink
        self._delay_bars = delay_bars
        self._ready = False
        self._queued: list[tuple[OrderIntent, int]] = []
        self._pending: list[_PendingOrder] = []
        self._market_gated = False
        self._streams: dict[tuple[type, Any], None] = {}
        self._intents: dict[ClientOrderId, OrderIntent] = {}
        self._report_sequences: dict[ClientOrderId, int] = {}

    def enqueue(self, intent: OrderIntent) -> None:
        # request_ts可以早于处理时钟（延迟收到的信号），两者都必须越过。
        after_ts = max(int(intent.metadata.get("request_ts", 0)),
                       self.clock.timestamp_ns() if self._ready else 0)
        if self._ready:
            self._prepare(intent, after_ts)
        else:
            self._queued.append((intent, after_ts))

    def watch_market_event(self, event: Any) -> None:
        """流式模式下，只在本合约严格更晚的行情处理后放行订单。"""
        self._market_gated = True
        if isinstance(event, Bar):
            stream = (Bar, event.bar_type)
        elif isinstance(event, (QuoteTick, TradeTick)):
            stream = (type(event), event.instrument_id)
        else:
            return  # 不支持的数据类型仍交由Engine校验。
        if self._delay_bars > 1:
            if not isinstance(event, Bar):
                raise ValueError("execution_delay_bars大于1时只支持Bar回放")
            for kind, key in self._streams:
                if (kind is Bar and key.instrument_id == event.bar_type.instrument_id
                        and key != event.bar_type):
                    raise ValueError("延迟多根Bar要求每个合约只使用一种Bar周期")
        if stream not in self._streams:
            self._streams[stream] = None
            if self._ready:
                self._subscribe(stream)

    def _subscribe(self, stream: tuple[type, Any]) -> None:
        kind, key = stream
        if kind is Bar:
            self.subscribe_bars(key)
        elif kind is QuoteTick:
            self.subscribe_quote_ticks(key)
        else:
            self.subscribe_trade_ticks(key)

    def on_start(self) -> None:
        self._ready = True
        for stream in self._streams:
            self._subscribe(stream)
        queued, self._queued = self._queued, []
        for intent, after_ts in queued:
            self._prepare(intent, after_ts)

    def on_stop(self) -> None:
        self.cancel_pending_orders(reason="simulation ended before eligible market data")
        self._ready = False

    def cancel_strategy_orders(self, strategy_id: str) -> None:
        self.cancel_pending_orders(strategy_id=strategy_id, reason="strategy canceled")
        for client_order_id, intent in tuple(self._intents.items()):
            if intent.strategy_id != strategy_id:
                continue
            native_order = self.cache.order(client_order_id)
            if native_order is not None and native_order.is_open:
                self.cancel_order(native_order)

    def cancel_pending_orders(self, *, strategy_id: str | None = None, reason: str,
                               instrument_id=None) -> None:
        matches = lambda intent: ((strategy_id is None or intent.strategy_id == strategy_id)
                                  and (instrument_id is None or intent.instrument_id == instrument_id))
        selected = [item for item in self._pending
                    if matches(item.intent)]
        self._pending = [item for item in self._pending if not matches(item.intent)]
        queued, self._queued = self._queued, []
        for intent, after_ts in queued:
            if not matches(intent):
                self._queued.append((intent, after_ts))
                continue
            order = self._make_order(intent)
            self._intents[order.client_order_id] = intent
            selected.append(_PendingOrder(intent, order, after_ts, self._delay_bars, after_ts))
        for item in selected:
            self._finish(item.native_order.client_order_id, self.clock.timestamp_ns(),
                         ExecutionReportType.CANCELED, reason)

    def on_bar(self, bar: Bar) -> None:
        self._release_orders(bar.bar_type.instrument_id, bar.ts_event)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self._release_orders(tick.instrument_id, tick.ts_event)

    def on_trade_tick(self, tick: TradeTick) -> None:
        self._release_orders(tick.instrument_id, tick.ts_event)

    def _release_orders(self, instrument_id: Any, ts_event: int) -> None:
        # Engine先更新交易所行情，再回调此处，最后处理订单消息。
        # 完整Bar模式使用新Bar的收盘盘口，不允许订单回用该Bar之前的OHLC。
        for item in tuple(self._pending):
            if (item.intent.instrument_id != instrument_id or ts_event <= item.after_ts
                    or ts_event <= item.last_market_ts):
                continue
            # 原生同步回报可能触发取消，后续意图须仍可被找到并取消。
            if item.native_order.client_order_id not in self._intents:
                continue
            item.last_market_ts = ts_event
            item.remaining_bars -= 1
            if item.remaining_bars:
                continue
            self._pending.remove(item)
            self.submit_order(item.native_order)

    def _prepare(self, intent: OrderIntent, after_ts: int) -> None:
        native_order = self._make_order(intent)
        self._intents[native_order.client_order_id] = intent
        if self._market_gated:
            self._pending.append(_PendingOrder(intent, native_order, after_ts, self._delay_bars, after_ts))
        else:
            self.submit_order(native_order)

    def _make_order(self, intent: OrderIntent) -> Any:
        instrument = self.cache.instrument(intent.instrument_id)
        if instrument is None:
            raise RuntimeError(f"找不到模拟合约: {intent.instrument_id}")
        native_side = (
            NativeOrderSide.BUY if intent.side is OrderSide.BUY else NativeOrderSide.SELL
        )
        quantity = instrument.make_qty(intent.quantity)
        reducing = intent.reduce_only or intent.position_effect in {
            PositionEffect.CLOSE,
            PositionEffect.CLOSE_TODAY,
            PositionEffect.CLOSE_YESTERDAY,
        }
        if intent.position_effect is PositionEffect.OPEN and intent.reduce_only:
            raise ValueError("OPEN订单不能同时设置reduce_only")

        if intent.order_type is OrderType.MARKET:
            native_order = self.order_factory.market(
                instrument_id=intent.instrument_id,
                order_side=native_side,
                quantity=quantity,
                reduce_only=reducing,
            )
        else:
            if intent.price is None:  # OrderIntent自身已经校验，此处保护类型收窄。
                raise ValueError("限价单缺少价格")
            native_order = self.order_factory.limit(
                instrument_id=intent.instrument_id,
                order_side=native_side,
                quantity=quantity,
                price=instrument.make_price(intent.price),
                reduce_only=reducing,
            )

        return native_order

    def on_order_accepted(self, event: OrderAccepted) -> None:
        intent = self._intents.get(event.client_order_id)
        if intent is None:
            return
        self._emit(intent, event.client_order_id, ExecutionReportType.ACCEPTED, event.ts_event)

    def on_order_filled(self, event: OrderFilled) -> None:
        intent = self._intents.get(event.client_order_id)
        if intent is None:
            return
        native_order = self.cache.order(event.client_order_id)
        closed = native_order is not None and native_order.is_closed
        self._emit(
            intent,
            event.client_order_id,
            ExecutionReportType.FILLED if closed else ExecutionReportType.PARTIALLY_FILLED,
            event.ts_event,
            filled_quantity=Decimal(str(event.last_qty)),
            fill_price=Decimal(str(event.last_px)),
            metadata={
                "trade_id": str(event.trade_id),
                "venue_order_id": str(event.venue_order_id),
                "commission": str(event.commission),
            },
        )
        if closed:
            self._intents.pop(event.client_order_id, None)

    def on_order_rejected(self, event: OrderRejected) -> None:
        self._finish(
            event.client_order_id,
            event.ts_event,
            ExecutionReportType.REJECTED,
            event.reason,
        )

    def on_order_denied(self, event: OrderDenied) -> None:
        self._finish(
            event.client_order_id,
            event.ts_event,
            ExecutionReportType.REJECTED,
            event.reason,
        )

    def on_order_canceled(self, event: OrderCanceled) -> None:
        self._finish(event.client_order_id, event.ts_event, ExecutionReportType.CANCELED, None)

    def on_order_expired(self, event: OrderExpired) -> None:
        self._finish(event.client_order_id, event.ts_event, ExecutionReportType.CANCELED, "expired")

    def _finish(
        self,
        client_order_id: ClientOrderId,
        ts_event: int,
        report_type: ExecutionReportType,
        reason: object | None,
    ) -> None:
        intent = self._intents.pop(client_order_id, None)
        if intent is None:
            return
        self._emit(
            intent,
            client_order_id,
            report_type,
            ts_event,
            reason=None if reason is None else str(reason),
        )

    def _emit(
        self,
        intent: OrderIntent,
        client_order_id: ClientOrderId,
        report_type: ExecutionReportType,
        ts_event: int,
        *,
        filled_quantity: Decimal = Decimal(0),
        fill_price: Decimal | None = None,
        reason: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        sequence = self._report_sequences.get(client_order_id, 0) + 1
        self._report_sequences[client_order_id] = sequence
        report_metadata = {
            "strategy_id": intent.strategy_id,
            "position_effect": intent.position_effect.value,
            **dict(intent.metadata),
            **(metadata or {}),
        }
        self._report_sink(
            ExecutionReport(
                backend_id=self._backend_id,
                client_order_id=str(client_order_id),
                instrument_id=intent.instrument_id,
                report_type=report_type,
                ts_event=ts_event,
                filled_quantity=filled_quantity,
                fill_price=fill_price,
                order_side=intent.side,
                order_quantity=intent.quantity,
                position_effect=intent.position_effect,
                reason=reason,
                report_id=f"{client_order_id}:{sequence}",
                sequence=sequence,
                metadata=report_metadata,
            ),
        )
