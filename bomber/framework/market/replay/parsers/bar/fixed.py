"""文件路径已经确定标的时，为逐行Bar补充固定身份。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from decimal import Decimal, InvalidOperation

from bomber.framework.market.basic.base import BarType, DataType, InstrumentId, make_bar
from bomber.framework.market.replay.parsers.bar.mapped import BarColumns, MappedBarParser
from bomber.framework.market.replay.parsers.base import (ParsedEvent, ParserContext,
    nonnegative, positive, required)


class FixedInstrumentBarParser:
    """为无 symbol/exchange 列的行情指定固定合约。

    默认只发 OHLCV Bar。可显式映射 value、open_interest、vwap、trade_time，
    并用 preserve_raw_ohlcv 保留原始 OHLCV；转换与事件顺序统一由 MappedBarParser 处理。
    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        *,
        timestamp: str = "datetime",
        bar_spec: str = "1-MINUTE",
        timezone: str = "Asia/Shanghai",
        available_timestamp: str | None = None,
        available_nanoseconds: str | None = None,
        value: str | None = None,
        open_interest: str | None = None,
        vwap: str | None = None,
        trade_time: str | None = None,
        preserve_raw_ohlcv: bool = False,
    ) -> None:
        self.instrument_id = instrument_id
        self._mapped = MappedBarParser(
            columns=BarColumns(
                symbol="__fixed_symbol__",
                exchange="__fixed_exchange__",
                timestamp=timestamp,
                open="open",
                high="high",
                low="low",
                close="close",
                volume="volume",
                value=value,
                open_interest=open_interest,
                vwap=vwap,
                trade_time=trade_time,
                available_timestamp=available_timestamp,
                available_nanoseconds=available_nanoseconds,
            ),
            bar_spec=bar_spec,
            timezone=timezone,
            preserve_raw_ohlcv=preserve_raw_ohlcv,
        )

    def reset(self) -> None:
        self._mapped.reset()

    def parse(self, row: Mapping[str, Any], context: ParserContext) -> Iterable[ParsedEvent]:
        enriched = {
            **row,
            "__fixed_symbol__": self.instrument_id.symbol.value,
            "__fixed_exchange__": str(self.instrument_id.venue),
        }
        return self._mapped.parse(enriched, context)


class PreparedBarParser:
    """Read the standard execution frame's integer clocks and fixed identity.

    Used explicitly by prepared-file callers. Monetary checks and make_bar's
    price/quantity rounding stay identical to the generic parser. reset() binds
    metadata and BarType again for a new load, including forced reloads.
    """

    def __init__(self, instrument_id: InstrumentId, *, bar_spec="1-MINUTE"):
        self.instrument_id = instrument_id
        self.bar_spec = bar_spec.upper()
        self.reset()

    def reset(self):
        self._meta = None
        self._bar_type = None

    @staticmethod
    def _clock(row, name, context):
        value = row.get(name)
        if type(value) is int:
            return value
        try:
            value = Decimal(required(row, name, context))
            if not value.is_finite() or value != value.to_integral_value():
                raise ValueError("non-integer clock")
            return int(value)
        except (InvalidOperation, ValueError, OverflowError) as exc:
            label = "availability" if name == "available_ns" else "event time"
            raise context.error(f"bar {label} must be integer nanoseconds") from exc

    def parse(self, row: Mapping[str, Any], context: ParserContext) -> Iterable[ParsedEvent]:
        if self._meta is None:
            self._meta = context.require_meta(self.instrument_id)
        ts_event = self._clock(row, "event_ns", context)
        ts_init = self._clock(row, "available_ns", context)
        if ts_init < ts_event:
            raise context.error("bar availability precedes completed bar time")
        opening = positive(row, "open", context)
        high = positive(row, "high", context)
        low = positive(row, "low", context)
        close = positive(row, "close", context)
        volume = nonnegative(row, "volume", context)
        if high < max(opening, low, close):
            raise context.error("high is below an OHLC component")
        if low > min(opening, high, close):
            raise context.error("low is above an OHLC component")
        if self._bar_type is None:
            text = (self.bar_spec if "-" in self.bar_spec and len(self.bar_spec.split("-")) >= 4
                else f"{self.instrument_id}-{self.bar_spec}-LAST-EXTERNAL")
            self._bar_type = BarType.from_str(text)
        bar = make_bar(instrument_id=self.instrument_id, open=opening, high=high, low=low,
            close=close, volume=volume, ts_event=ts_event, ts_init=ts_init,
            meta=self._meta, bar_type=self._bar_type)
        return (ParsedEvent(DataType.BAR, self.instrument_id, bar, self.bar_spec),)
