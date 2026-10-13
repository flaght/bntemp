"""Immutable trading-hours values; source adapters supply lowercase field names."""
from dataclasses import dataclass
from datetime import time, timedelta
from hashlib import sha256
from numbers import Integral
import json
import re

HOURS_COLUMNS = ("code", "segment_id", "date_anchor", "start_day_offset", "start_time",
                 "end_day_offset", "end_time")
DAY_NS = 86_400_000_000_000
MINUTE_NS = 60_000_000_000


def clock_ns(value):
    """Accept clock strings/time or duration scalars, never epoch timestamps/numeric guesses."""
    if isinstance(value, str):
        if re.fullmatch(r"\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?", value) is None:
            raise ValueError("时段时间须为HH:MM[:SS[.fraction]]")
        if "." in value and int(value.rsplit(".", 1)[1]):
            raise ValueError("首期时段须对齐完整分钟，不能丢弃亚秒精度")
        value = time.fromisoformat(value)
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise ValueError("时钟时间不得携带日期或时区")
        result = ((value.hour * 3600 + value.minute * 60 + value.second) * 1_000_000
                  + value.microsecond) * 1000
    elif isinstance(value, timedelta):
        result = ((value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds) * 1000
        if hasattr(value, "nanoseconds"):
            result += value.nanoseconds
    elif type(value).__module__ == "numpy" and type(value).__name__ == "timedelta64":
        import numpy as np
        if np.isnat(value):
            raise ValueError("时段时间不能为空")
        result = int(value.astype("timedelta64[ns]").astype("int64"))
    else:
        raise ValueError("时段时间须为时钟字符串、time或明确单位的timedelta")
    if not 0 <= result < DAY_NS or result % MINUTE_NS:
        raise ValueError("首期时段须在一天内并对齐完整分钟")
    return result


def clock_text(value):
    seconds = value // 1_000_000_000
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}须为非空字符串")
    return value.strip()


@dataclass(frozen=True)
class SessionSegment:
    segment_id: str
    date_anchor: str
    start_day_offset: int
    start_time: str
    end_day_offset: int
    end_time: str

    def __post_init__(self):
        object.__setattr__(self, "segment_id", required_text(self.segment_id, "segment_id"))
        if self.segment_id in {"ALL", "DAY", "NIGHT"}:
            raise ValueError("segment_id不能使用例外分组保留标识ALL/DAY/NIGHT")
        anchor = required_text(self.date_anchor, "date_anchor").upper()
        if anchor not in {"TRADING_DAY", "NIGHT_START"}:
            raise ValueError("date_anchor须为TRADING_DAY或NIGHT_START")
        object.__setattr__(self, "date_anchor", anchor)
        for name in ("start_day_offset", "end_day_offset"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Integral) or value not in {0, 1}:
                raise ValueError("首期日期偏移须为整数0或1")
            object.__setattr__(self, name, int(value))
        for name in ("start_time", "end_time"):
            object.__setattr__(self, name, clock_text(clock_ns(getattr(self, name))))
        if self.end_offset_ns <= self.start_offset_ns:
            raise ValueError("时段结束须晚于开始，跨午夜须声明日期偏移")

    @property
    def start_offset_ns(self):
        return self.start_day_offset * DAY_NS + clock_ns(self.start_time)

    @property
    def end_offset_ns(self):
        return self.end_day_offset * DAY_NS + clock_ns(self.end_time)

    def row(self, code):
        return dict(code=code, segment_id=self.segment_id, date_anchor=self.date_anchor,
                    start_day_offset=self.start_day_offset, start_time=self.start_time,
                    end_day_offset=self.end_day_offset, end_time=self.end_time)


@dataclass(frozen=True)
class TradingHoursSnapshot:
    """Complete static groups, identified by normalized content, without a sidecar manifest."""
    rows: tuple[tuple[str, SessionSegment], ...]
    source: str

    def __post_init__(self):
        normalized = tuple(sorted(((required_text(code, "code").upper(), segment)
                                   for code, segment in self.rows),
                                  key=lambda item: (item[0], item[1].segment_id)))
        if not normalized or any(not isinstance(segment, SessionSegment) for _, segment in normalized):
            raise ValueError("交易时段快照须包含完整非空分段")
        keys = [(code, segment.segment_id) for code, segment in normalized]
        if len(keys) != len(set(keys)):
            raise ValueError("code/segment_id重复，不能依赖读取顺序选择")
        for code in {code for code, _ in normalized}:
            for anchor in ("TRADING_DAY", "NIGHT_START"):
                parts = sorted((s for c, s in normalized if c == code and s.date_anchor == anchor),
                               key=lambda s: s.start_offset_ns)
                if any(a.end_offset_ns > b.start_offset_ns for a, b in zip(parts, parts[1:])):
                    raise ValueError("同一日期锚点的会话分段重叠")
        object.__setattr__(self, "rows", normalized)

    def segments(self, code):
        code = required_text(code, "code").upper()
        result = tuple(segment for c, segment in self.rows if c == code)
        if not result:
            raise ValueError(f"缺少{code}交易时段，不回退其他品种")
        return result

    @property
    def fingerprint(self):
        payload = [segment.row(code) for code, segment in self.rows]
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def records(self):
        return tuple(segment.row(code) for code, segment in self.rows)


def parse_trading_hours(frame, *, source):
    """Shared parser for database-standardized rows and Feather; no Code alias here."""
    import pandas as pd
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        raise ValueError("交易时段须为列名唯一的DataFrame")
    missing, extra = set(HOURS_COLUMNS) - set(frame), set(frame) - set(HOURS_COLUMNS)
    if missing or extra:
        raise ValueError(f"交易时段字段不符，缺少{sorted(missing)}，未知{sorted(extra)}")
    rows = []
    for row in frame.to_dict("records"):
        code = required_text(row.pop("code"), "code").upper()
        rows.append((code, SessionSegment(**row)))
    return TradingHoursSnapshot(tuple(rows), source)
