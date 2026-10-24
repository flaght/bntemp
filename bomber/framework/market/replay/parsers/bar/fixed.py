"""文件路径已经确定标的时，为逐行Bar补充固定身份。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from bomber.framework.market.basic.base import InstrumentId
from bomber.framework.market.replay.parsers.bar.mapped import BarColumns, MappedBarParser
from bomber.framework.market.replay.parsers.base import ParsedEvent, ParserContext


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
