"""显式字段映射的供应商 Bar 解析器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from bomber.framework.market.basic.base import CustomBar, DataType, InstrumentId, make_bar, make_custom_bar
from bomber.framework.market.replay.parsers.base import (
    ParsedEvent,
    ParserContext,
    nonnegative,
    parse_iso_timestamp,
    positive,
    required,
)


@dataclass(frozen=True)
class BarColumns:
    symbol: str
    exchange: str
    timestamp: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    value: str | None = None
    open_interest: str | None = None
    vwap: str | None = None
    bar_spec: str | None = None
    available_timestamp: str | None = None
    available_nanoseconds: str | None = None
    trade_time: str | None = None


class MappedBarParser:
    """将供应商字段映射为 Bar，配置伴生字段时再发 CustomBar。

    preserve_raw_ohlcv 保留转为合约精度之前的已校验 OHLCV 数值；
    columns.trade_time 原样透传来源时间标签，不改变事件或可用时间。
    同一行始终先发 Bar，再发共享该 Bar 的 CustomBar。
    """

    def __init__(
        self,
        columns: BarColumns,
        bar_spec: str = "1-MINUTE",
        timezone: str = "Asia/Shanghai",
        exchange_aliases: Mapping[str, str] | None = None,
        *,
        preserve_raw_ohlcv: bool = False,
    ) -> None:
        self.columns = columns
        self.preserve_raw_ohlcv = preserve_raw_ohlcv
        self.bar_spec = bar_spec.upper()
        self.timezone = ZoneInfo(timezone)
        self.exchange_aliases = {
            source.upper(): target.upper()
            for source, target in (exchange_aliases or {}).items()
        }

    def reset(self) -> None:
        pass

    def parse(
        self,
        row: Mapping[str, Any],
        context: ParserContext,
    ) -> Iterable[ParsedEvent]:
        c = self.columns
        exchange = required(row, c.exchange, context).upper()
        exchange = self.exchange_aliases.get(exchange, exchange)
        instrument_id = InstrumentId.from_str(f"{required(row, c.symbol, context)}.{exchange}")
        meta = context.require_meta(instrument_id)
        bar_spec = (
            required(row, c.bar_spec, context).upper()
            if c.bar_spec is not None
            else self.bar_spec
        )
        ts_event = parse_iso_timestamp(
            required(row, c.timestamp, context),
            context,
            self.timezone,
        )
        ts_init = (parse_iso_timestamp(required(row, c.available_timestamp, context),
                   context, self.timezone) if c.available_timestamp else ts_event)
        if c.available_nanoseconds is not None:
            from decimal import Decimal, InvalidOperation
            try:
                value = Decimal(required(row, c.available_nanoseconds, context))
                if not value.is_finite() or value != value.to_integral_value():
                    raise ValueError("non-integer availability")
                ts_init = int(value)
            except (InvalidOperation, ValueError, OverflowError) as exc:
                raise context.error("bar availability must be integer nanoseconds") from exc
        if ts_init < ts_event:
            raise context.error("bar availability precedes completed bar time")
        open_price = positive(row, c.open, context)
        high_price = positive(row, c.high, context)
        low_price = positive(row, c.low, context)
        close_price = positive(row, c.close, context)
        volume = nonnegative(row, c.volume, context)
        if high_price < max(open_price, low_price, close_price):
            raise context.error("high is below an OHLC component")
        if low_price > min(open_price, high_price, close_price):
            raise context.error("low is above an OHLC component")

        bar = make_bar(
            instrument_id=instrument_id,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
            ts_event=ts_event,
            ts_init=ts_init,
            meta=meta,
            bar_type=bar_spec,
        )
        events = [ParsedEvent(DataType.BAR, instrument_id, bar, bar_spec)]
        factors: dict[str, Any] = {}
        if self.preserve_raw_ohlcv:
            factors.update(open=open_price, high=high_price, low=low_price,
                           close=close_price, volume=volume)
        if c.value is not None:
            factors["value"] = nonnegative(row, c.value, context)
        if c.open_interest is not None:
            factors["open_interest"] = nonnegative(row, c.open_interest, context)
        if c.vwap is not None:
            factors["vwap"] = positive(row, c.vwap, context)
        if c.trade_time is not None:
            required(row, c.trade_time, context)
            factors["trade_time"] = row[c.trade_time]
        if factors:
            custom_bar: CustomBar = make_custom_bar(bar, factors)
            events.append(
                ParsedEvent(DataType.CUSTOM_BAR, instrument_id, custom_bar, bar_spec),
            )
        return events
