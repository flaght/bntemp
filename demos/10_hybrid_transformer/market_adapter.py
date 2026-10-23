"""把已准备的一分钟行情同时送给撮合与模型策略。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from bomber.framework.market.basic.base import DataType, InstrumentId, make_custom_bar
from bomber.framework.market.replay.parsers.bar.fixed import FixedInstrumentBarParser
from bomber.framework.market.replay.parsers.base import (
    ParsedEvent, ParserContext, nonnegative, number,
)


class ResearchCustomBarParser:
    """标准 Bar 先推进撮合，同一时间 CustomBar 携带完整精度的研究输入。"""

    def __init__(self, instrument_id: InstrumentId, *,
                 trade_time_column: str = "source_timestamp",
                 value_column: str = "value",
                 open_interest_column: str = "openint") -> None:
        self.instrument_id = instrument_id
        self.trade_time_column = trade_time_column
        self.value_column = value_column
        self.open_interest_column = open_interest_column
        self._execution = FixedInstrumentBarParser(
            instrument_id, timestamp="datetime", available_nanoseconds="available_ns")

    def reset(self) -> None:
        self._execution.reset()

    def parse(self, row: Mapping[str, Any], context: ParserContext) -> Iterable[ParsedEvent]:
        trade_time = row.get(self.trade_time_column)
        if trade_time is None:
            raise context.error(f"缺少原始时间标签: {self.trade_time_column}")
        factors = {
            field: number(row, field, context)
            for field in ("open", "high", "low", "close")
        }
        factors["volume"] = nonnegative(row, "volume", context)
        factors["value"] = nonnegative(row, self.value_column, context)
        factors["openint"] = nonnegative(row, self.open_interest_column, context)
        factors["trade_time"] = trade_time
        for parsed in self._execution.parse(row, context):
            # 只在准备好的行情可用时刻发出模型事件，避免抢在原生撮合 Bar 之前。
            yield parsed
            yield ParsedEvent(
                DataType.CUSTOM_BAR, self.instrument_id,
                make_custom_bar(parsed.payload, factors), "1-MINUTE",
            )
