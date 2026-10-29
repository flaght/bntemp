"""原始期货历史分钟校验；数值和VWAP与实时CustomBar共用实现。"""
from .base import HistoryError
from ...calendars.sessions import ns_datetime
from ...market.futures import futures_values


def futures_history_values(bar, *, calendar, contract_multiplier):
    fields = ('open', 'high', 'low', 'close', 'volume', 'value', 'open_interest')
    identity = f"instrument={bar.instrument_id} ts_event={bar.ts_event}"
    if any(getattr(bar, field) is None for field in fields):
        raise HistoryError('期货历史分钟必须包含原始OHLCV、value、open_interest')
    if bar.cumulative_factor is not None or bar.adjusted_close is not None:
        raise HistoryError('原始期货历史不能包含复权分钟')
    if calendar.window(ns_datetime(bar.ts_event, calendar.zone)) is None:
        raise HistoryError('期货历史包含盘外分钟')
    try:
        return futures_values({field: getattr(bar, field) for field in fields},
                              contract_multiplier=contract_multiplier)
    except ValueError as error:
        raise HistoryError(f"期货历史数值无效: {identity} volume={bar.volume} "
                           f"value={bar.value} low={bar.low} high={bar.high}: {error}") from error
