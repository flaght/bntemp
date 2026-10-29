"""将 DolphinDB 逐笔及分钟 Bar 数据行转换为标准行情事件。"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as datetime_time, timedelta, timezone, tzinfo
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bomber.framework.market.basic.base import (
    Bar,
    CustomBar,
    DataType,
    InstrumentId,
    InstrumentMeta,
    QuoteTick,
    TradeTick,
    make_bar,
    make_custom_bar,
    make_quote_tick,
    make_trade_tick,
)


@dataclass(frozen=True)
class ConvertedEvent:
    data_type: DataType
    event: QuoteTick | TradeTick | Bar | CustomBar
    bar_spec: str | None = None


@dataclass
class _TickState:
    cumulative_volume: int | None = None
    quote_key: tuple[float, float, float, float] | None = None


class DolphinDbMarketConverter:
    def __init__(self, timezone_name: str = "Asia/Shanghai") -> None:
        self.timezone = _load_timezone(timezone_name)
        self._tick_states: dict[InstrumentId, _TickState] = {}
        self._bar_keys: set[tuple[InstrumentId, str, int]] = set()
        self._bar_volumes: dict[tuple[InstrumentId, str], tuple[str, int, float]] = {}

    def reset(self) -> None:
        self._tick_states.clear()
        self._bar_keys.clear()
        self._bar_volumes.clear()

    def convert_tick(
        self,
        row: Mapping[str, Any],
        instrument_id: InstrumentId,
        meta: InstrumentMeta,
    ) -> list[ConvertedEvent]:
        ts_event = _combine_ns(
            row.get("ActionDay") or row.get("date") or row.get("TradingDay"),
            row.get("UpdateTime") or row.get("tradeTime"),
            _integer(row.get("UpdateMillisec", 0), "UpdateMillisec", minimum=0, maximum=999),
            self.timezone,
        )
        ts_init = _timestamp_ns(row.get("receivedTime"), self.timezone) or time.time_ns()
        state = self._tick_states.setdefault(instrument_id, _TickState())
        events: list[ConvertedEvent] = []

        bid_price = _positive(row.get("BidPrice1"), "BidPrice1", optional=True)
        ask_price = _positive(row.get("AskPrice1"), "AskPrice1", optional=True)
        bid_size = _nonnegative(row.get("BidVolume1"), "BidVolume1", optional=True)
        ask_size = _nonnegative(row.get("AskVolume1"), "AskVolume1", optional=True)
        if None not in (bid_price, ask_price, bid_size, ask_size):
            assert bid_price is not None and ask_price is not None
            assert bid_size is not None and ask_size is not None
            if bid_price > ask_price:
                raise ValueError(f"crossed quote: bid={bid_price}, ask={ask_price}")
            quote_key = (bid_price, ask_price, bid_size, ask_size)
            if quote_key != state.quote_key:
                events.append(
                    ConvertedEvent(
                        DataType.QUOTE_TICK,
                        make_quote_tick(
                            instrument_id=instrument_id,
                            bid_price=bid_price,
                            ask_price=ask_price,
                            bid_size=bid_size,
                            ask_size=ask_size,
                            ts_event=ts_event,
                            ts_init=ts_init,
                            meta=meta,
                        ),
                    ),
                )
                state.quote_key = quote_key

        cumulative = _optional_integer(row.get("Volume"), minimum=0)
        if cumulative is not None:
            previous = state.cumulative_volume
            state.cumulative_volume = cumulative
            if previous is not None and cumulative > previous:
                last_price = _positive(row.get("LastPrice"), "LastPrice", optional=True)
                if last_price is not None:
                    events.append(
                        ConvertedEvent(
                            DataType.TRADE_TICK,
                            make_trade_tick(
                                instrument_id=instrument_id,
                                price=last_price,
                                size=cumulative - previous,
                                trade_id=_trade_id(instrument_id, ts_event, cumulative),
                                ts_event=ts_event,
                                ts_init=ts_init,
                                meta=meta,
                            ),
                        ),
                    )
        return events

    def convert_bar(
        self,
        row: Mapping[str, Any],
        instrument_id: InstrumentId,
        meta: InstrumentMeta,
        bar_spec: str,
        *, volume_mode: str = "per-bar", received_ns: int | None = None, timestamp_field: str | None = None,
    ) -> list[ConvertedEvent]:
        resolved_spec = bar_spec.upper()
        # minTime 是 Bar 时间标识；updateTime 是入库或更新时间，
        # 不是行情 Bar 的时间戳。
        ts_event = (_timestamp_ns(row.get(timestamp_field), self.timezone) if timestamp_field
                    else _combine_ns(row.get("date"), row.get("minTime"), 0, self.timezone))
        if ts_event is None:
            raise ValueError("分钟行情缺少完整时间戳")
        ts_init = received_ns if received_ns is not None else (
            _timestamp_ns(row.get("updateTime"), self.timezone) or time.time_ns())
        key = (instrument_id, resolved_spec, ts_event)
        if key in self._bar_keys:
            return []

        open_price = _positive(row.get("open"), "open")
        high_price = _positive(row.get("high"), "high")
        low_price = _positive(row.get("low"), "low")
        close_price = _positive(row.get("close"), "close")
        volume = _nonnegative(row.get("volume"), "volume")
        assert None not in (open_price, high_price, low_price, close_price, volume)
        if high_price < max(open_price, low_price, close_price):
            raise ValueError("high is below an OHLC component")
        if low_price > min(open_price, high_price, close_price):
            raise ValueError("low is above an OHLC component")
        if volume_mode not in {"per-bar", "cumulative"}:
            raise ValueError("Bar volume_mode must be per-bar or cumulative")
        volume_key = (instrument_id, resolved_spec)
        cumulative = volume
        day = _date_text(row.get("date"))
        previous = self._bar_volumes.get(volume_key)
        if volume_mode == "cumulative":
            if previous is not None and day == previous[0]:
                if ts_event <= previous[1] or cumulative < previous[2]:
                    raise ValueError("累计分钟量时间倒退或同日累计量下降")
                volume = cumulative - previous[2]
            else:
                # Joining intraday cannot assign all prior trades to the first Bar.
                volume = 0

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
            bar_type=resolved_spec,
        )
        factors = {
            name: value
            for name in (
                "prev_close",
                "prev_settlement",
                "turnover",
                "pct_change",
                "open_interest",
                "prev_open_interest",
            )
            if (value := _finite(row.get(name), name, optional=True)) is not None
        }
        # 完整期货模型显式使用的原始精度；标准 BAR 消费者保持原行为。
        for name in ("open", "high", "low", "close", "volume", "value", "openint"):
            raw = _finite(row.get(name), name, optional=True)
            if raw is not None:
                factors[name] = float(volume) if name == "volume" else raw
        if volume_mode == "cumulative":
            factors["source_volume"] = float(cumulative)
        self._bar_keys.add(key)
        if volume_mode == "cumulative":
            self._bar_volumes[volume_key] = (day, ts_event, cumulative)
        events = [ConvertedEvent(DataType.BAR, bar, resolved_spec)]
        if factors:
            events.append(
                ConvertedEvent(
                    DataType.CUSTOM_BAR,
                    make_custom_bar(bar, factors),
                    resolved_spec,
                ),
            )
        return events


def _finite(value: Any, name: str, optional: bool = False) -> float | None:
    if value is None or str(value) in {"", "NaN", "nan", "NaT", "None"}:
        if optional:
            return None
        raise ValueError(f"{name} is required")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric, got {value!r}") from exc
    if not math.isfinite(result) or abs(result) >= 1e100:
        if optional:
            return None
        raise ValueError(f"{name} must be finite, got {value!r}")
    return result


def _positive(value: Any, name: str, optional: bool = False) -> float | None:
    result = _finite(value, name, optional)
    if result is None:
        return None
    if result <= 0:
        if optional:
            return None
        raise ValueError(f"{name} must be positive, got {value!r}")
    return result


def _nonnegative(value: Any, name: str, optional: bool = False) -> float | None:
    result = _finite(value, name, optional)
    if result is None:
        return None
    if result < 0:
        if optional:
            return None
        raise ValueError(f"{name} must be non-negative, got {value!r}")
    return result


def _integer(
    value: Any,
    name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    result = _finite(value, name)
    assert result is not None
    if not result.is_integer():
        raise ValueError(f"{name} must be an integer, got {value!r}")
    integer = int(result)
    if minimum is not None and integer < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {integer}")
    if maximum is not None and integer > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {integer}")
    return integer


def _optional_integer(value: Any, minimum: int | None = None) -> int | None:
    try:
        result = _finite(value, "integer", optional=True)
    except ValueError:
        return None
    if result is None or not result.is_integer():
        return None
    integer = int(result)
    return None if minimum is not None and integer < minimum else integer


def _combine_ns(day_value: Any, clock_value: Any, millisec: int, tz: tzinfo) -> int:
    day = _date_text(day_value)
    clock = _time_text(clock_value)
    timestamp = datetime.fromisoformat(f"{day}T{clock}")
    timestamp = timestamp.replace(tzinfo=tz)
    if millisec:
        timestamp = timestamp.replace(microsecond=millisec * 1_000)
    return _datetime_to_ns(timestamp)


def _timestamp_ns(value: Any, tz: tzinfo) -> int | None:
    if value is None or str(value) in {"", "NaT", "None"}:
        return None
    if isinstance(value, datetime):
        timestamp = value
    else:
        text = str(value).strip().replace("/", "-")
        if "T" not in text and " " not in text:
            return None
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=tz)
    return _datetime_to_ns(timestamp)


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip().replace(".", "-").replace("/", "-")
    if "T" in text:
        text = text.split("T", 1)[0]
    elif " " in text:
        text = text.split(" ", 1)[0]
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    datetime.fromisoformat(text)
    return text


def _time_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None).isoformat()
    if isinstance(value, datetime_time):
        return value.replace(tzinfo=None).isoformat()
    text = str(value or "").strip()
    if "T" in text:
        text = text.split("T", 1)[1]
    elif " " in text:
        text = text.rsplit(" ", 1)[1]
    text = text.split("+", 1)[0].split("Z", 1)[0]
    datetime_time.fromisoformat(text)
    return text


def _datetime_to_ns(timestamp: datetime) -> int:
    utc_timestamp = timestamp.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc_timestamp - epoch
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _load_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        raise


def _trade_id(instrument_id: InstrumentId, ts_event: int, cumulative: int) -> str:
    source = f"{instrument_id}|{ts_event}|{cumulative}".encode()
    return "ddb-" + hashlib.blake2b(source, digest_size=16).hexdigest()

class DolphinDbMinuteStateBuffer:
    """同一合约下一分钟到达才封闭上一分钟；不以墙钟猜测完成。"""
    def __init__(self, timezone_name="Asia/Shanghai"):
        self.timezone = _load_timezone(timezone_name)
        self._pending = {}
        self._closed = {}
        self.ignored_old_versions = 0
        self.discarded_baseline_minutes = 0

    def reset(self):
        self._pending.clear()
        self._closed.clear()

    def _signature(self, row):
        return tuple(_nonnegative(row.get(key), key) for key in
            ("open", "high", "low", "close", "volume", "turnover", "open_interest"))

    def push(self, key, row, *, received_ns):
        row = dict(row)
        stamp = _timestamp_ns(row.get("barTime"), self.timezone)
        revision = _timestamp_ns(row.get("updateTime"), self.timezone)
        if stamp is None or stamp % 60_000_000_000 or stamp > received_ns or revision is None:
            raise ValueError("分钟状态须有已开始的完整barTime时间戳及updateTime版本")
        baseline = row.get("volume_baseline_ok")
        if baseline is None or str(baseline).lower() not in {"true", "false"}:
            raise ValueError("分钟状态须明确volume_baseline_ok布尔值")
        signature = self._signature(row) + (str(baseline).lower(),)
        closed = self._closed.get(key)
        if closed is not None and stamp <= closed[0]:
            if stamp == closed[0] and signature == closed[2]:
                return ()
            raise ValueError("已封闭分钟收到不同内容或倒退状态，不能改写已发布行情")
        previous = self._pending.get(key)
        current = (stamp, revision, signature, row)
        if previous is None:
            self._pending[key] = current
            return ()
        if stamp < previous[0]:
            raise ValueError("分钟状态时间倒退")
        if stamp == previous[0]:
            if revision < previous[1]:
                self.ignored_old_versions += 1
                return ()
            if revision == previous[1] and signature != previous[2]:
                raise ValueError("同一状态版本内容冲突")
            if signature[4] < previous[2][4] or signature[5] < previous[2][5]:
                raise ValueError("同一分钟状态的成交量或成交额倒退")
            self._pending[key] = current
            return ()
        self._closed[key] = previous
        self._pending[key] = current
        if previous[2][-1] != "true":
            self.discarded_baseline_minutes += 1
            return ()
        return (previous[3],)
