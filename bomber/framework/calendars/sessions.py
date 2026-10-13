"""Source-independent sessions: reusable hours + versioned date calendar + exceptions."""
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from types import MappingProxyType
from zoneinfo import ZoneInfo
import json

from .hours import MINUTE_NS, SessionSegment, TradingHoursSnapshot, required_text
from .trading import CalendarSnapshot


def datetime_ns(value):
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1000


def ns_datetime(value, zone):
    if type(value) is not int or value < 0:
        raise ValueError("时间戳须为非负整数纳秒")
    return (datetime(1970, 1, 1, tzinfo=timezone.utc)
            + timedelta(seconds=value // 10**9, microseconds=value % 10**9 // 1000)).astimezone(zone)


@dataclass(frozen=True)
class SessionBinding:
    product: str
    hours_code: str
    calendar_id: str
    timezone: str
    exchange: str = ""
    asset_type: str = "FUTURE"
    night_anchor_rule: str = "PREVIOUS_TRADING_DAY"

    def __post_init__(self):
        for name in ("product", "hours_code", "calendar_id", "timezone"):
            object.__setattr__(self, name, required_text(getattr(self, name), name))
        object.__setattr__(self, "product", self.product.upper())
        object.__setattr__(self, "hours_code", self.hours_code.upper())
        ZoneInfo(self.timezone)
        if self.asset_type not in {"FUTURE", "OPTION"}:
            raise ValueError("会话绑定资产类型须为FUTURE或OPTION")
        if self.night_anchor_rule != "PREVIOUS_TRADING_DAY":
            raise ValueError("尚未实现此夜盘锚点规则")


@dataclass(frozen=True)
class SessionOverride:
    date_basis: str
    date: date
    action: str
    target: str
    segments: tuple[SessionSegment, ...] = ()

    def __post_init__(self):
        if self.date_basis not in {"TRADING_DAY", "NIGHT_START"} or type(self.date) is not date:
            raise ValueError("例外须声明日期及TRADING_DAY/NIGHT_START日期口径")
        if self.action not in {"CANCEL", "REPLACE"} or not self.target:
            raise ValueError("例外须声明CANCEL/REPLACE及DAY/NIGHT/ALL或分段标识")
        parts = tuple(self.segments)
        if (self.action == "CANCEL" and parts) or any(not isinstance(s, SessionSegment) for s in parts):
            raise ValueError("取消例外不能包含替换分段；替换分段须为SessionSegment")
        if self.date_basis == "NIGHT_START" and self.target in {"DAY", "ALL"}:
            raise ValueError("NIGHT_START例外仅能影响夜盘")
        allowed = ({"NIGHT_START"} if self.date_basis == "NIGHT_START" or self.target == "NIGHT" else
                   {"TRADING_DAY"} if self.target == "DAY" else {"TRADING_DAY", "NIGHT_START"})
        if any(s.date_anchor not in allowed for s in parts):
            raise ValueError("替换分段日期锚点与例外作用范围不一致")
        object.__setattr__(self, "segments", parts)


@dataclass(frozen=True)
class TradingWindow:
    trading_day: str
    start: datetime
    end: datetime
    segment_id: str = ""


class SessionCalendar:
    """Expand only requested natural-date anchors; all consumers share the same minute buckets."""
    def __init__(self, hours: TradingHoursSnapshot, calendar: CalendarSnapshot,
                 binding: SessionBinding, *, overrides=(), max_minutes=10000):
        if not isinstance(hours, TradingHoursSnapshot) or not isinstance(calendar, CalendarSnapshot):
            raise TypeError("会话服务需要标准时段与交易日快照")
        if calendar.calendar_id != binding.calendar_id:
            raise ValueError("绑定与日历身份不一致")
        if type(max_minutes) is not int or max_minutes < 1:
            raise ValueError("分钟资源上限须为正整数")
        self.hours, self.calendar, self.binding = hours, calendar, binding
        self.product, self.zone = binding.product, ZoneInfo(binding.timezone)
        self.start, self.end = calendar.start, calendar.end
        self.days = frozenset(calendar.days)
        self.segments = hours.segments(binding.hours_code)
        self.overrides = tuple(overrides)
        self.max_minutes = max_minutes
        for override in self.overrides:
            if not isinstance(override, SessionOverride):
                raise TypeError("例外须为SessionOverride")
            calendar.require_range(override.date, override.date)
            if not calendar.is_trading_day(override.date):
                raise ValueError("例外日期必须是其锚点对应的交易日期")
            if override.target not in {"DAY", "NIGHT", "ALL"} and override.target not in {
                    s.segment_id for s in self.segments}:
                raise ValueError("例外引用未知分段")
            if override.target not in {"DAY", "NIGHT", "ALL"}:
                original = next(s for s in self.segments if s.segment_id == override.target)
                if any(s.date_anchor != original.date_anchor for s in override.segments):
                    raise ValueError("单分段替换不能更换日期锚点")
        self._cache = {}
        identity = {"binding": asdict(binding), "overrides": [asdict(o) for o in self.overrides],
                    "hours": hours.fingerprint, "calendar": calendar.fingerprint}
        self._provenance = MappingProxyType({"source": hours.source,
            "hours_fingerprint": hours.fingerprint, "calendar": calendar.manifest,
            "sha256": sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest(),
            "schema_version": 1, "evidence": "static_snapshot_not_historical_revision_asof"})

    @property
    def provenance(self):
        return {**self._provenance, "calendar": dict(self._provenance["calendar"])}

    def is_trading_day(self, day):
        try:
            return self.calendar.is_trading_day(day)
        except ValueError as error:
            raise RuntimeError(str(error)) from error

    def _require_day(self, day):
        self.is_trading_day(day)

    def previous_trading_day(self, day):
        parsed = datetime.strptime(day, "%Y%m%d").date() if isinstance(day, str) else day
        self._require_day(parsed)
        index = bisect_left(self.calendar.days, parsed)
        if index == 0:
            raise RuntimeError("日历未覆盖此前来源交易日")
        return self.calendar.days[index - 1].strftime("%Y%m%d")

    def _select_segments(self, anchor, owner, basis):
        selected = {s.segment_id: s for s in self.segments if s.date_anchor == basis}
        touched = set()
        for override in self.overrides:
            matches = (override.date == owner if override.date_basis == "TRADING_DAY"
                       else basis == "NIGHT_START" and override.date == anchor)
            if not matches:
                continue
            if override.target == "DAY" and basis != "TRADING_DAY":
                continue
            if override.target == "NIGHT" and basis != "NIGHT_START":
                continue
            ids = (set(selected) if override.target in {"ALL", "DAY", "NIGHT"}
                   else {override.target} & set(selected))
            replacements = [s for s in override.segments if s.date_anchor == basis]
            if override.target not in {"ALL", "DAY", "NIGHT"} and any(
                    s.segment_id != override.target for s in replacements):
                raise ValueError("单分段替换须保留目标分段标识")
            affected = ids | {s.segment_id for s in replacements}
            # Group cancellations conflict even after an earlier rule emptied the group.
            token = override.target if override.target in {"ALL", "DAY", "NIGHT"} else None
            if (affected & touched or (token is not None and token in touched)
                    or "ALL" in touched or ("DAY" if basis == "TRADING_DAY" else "NIGHT") in touched):
                raise ValueError("同日会话例外冲突，不能依赖读取顺序")
            touched.update(affected)
            if token:
                touched.add(token)
            for key in ids:
                del selected[key]
            for segment in replacements:
                if segment.segment_id in selected:
                    raise ValueError("替换分段标识冲突")
                selected[segment.segment_id] = segment
        return tuple(selected.values())

    def _anchor_windows(self, anchor):
        if anchor in self._cache:
            return self._cache[anchor]
        if anchor not in self.days:
            return ()
        following = bisect_right(self.calendar.days, anchor)
        night_owner = self.calendar.days[following] if following < len(self.calendar.days) else None
        result = []
        for basis, owner in (("TRADING_DAY", anchor), ("NIGHT_START", night_owner)):
            for segment in self._select_segments(anchor, owner, basis):
                midnight = datetime.combine(anchor, time(), self.zone)
                left = midnight + timedelta(days=segment.start_day_offset,
                                             seconds=segment.start_offset_ns % (86400 * 10**9) // 10**9)
                right = midnight + timedelta(days=segment.end_day_offset,
                                              seconds=segment.end_offset_ns % (86400 * 10**9) // 10**9)
                if right <= left:
                    raise ValueError("展开后会话区间无效")
                result.append(TradingWindow(owner.strftime("%Y%m%d") if owner else "", left, right,
                                            segment.segment_id))
        result.sort(key=lambda w: w.start)
        self._check_overlap(result)
        self._cache[anchor] = tuple(result)
        return self._cache[anchor]

    @staticmethod
    def _check_overlap(windows):
        if any(a.end > b.start for a, b in zip(windows, windows[1:])):
            raise ValueError("展开后会话区间重叠或冲突")

    def windows(self, trading_day):
        day = datetime.strptime(trading_day, "%Y%m%d").date() if isinstance(trading_day, str) else trading_day
        if not self.is_trading_day(day):
            return ()
        anchors = [day]
        if (any(s.date_anchor == "NIGHT_START" for s in self.segments)
                or any(s.date_anchor == "NIGHT_START" for o in self.overrides for s in o.segments)):
            previous = self.previous_trading_day(day)
            anchors.insert(0, datetime.strptime(previous, "%Y%m%d").date())
        result = sorted((w for anchor in anchors for w in self._anchor_windows(anchor)
                         if w.trading_day == day.strftime("%Y%m%d")), key=lambda w: w.start)
        self._check_overlap(result)
        return tuple(result)

    def window(self, now):
        if now.tzinfo is None:
            raise ValueError("日历时间必须携带时区")
        now = now.astimezone(self.zone)
        self._require_day(now.date())
        anchors = (now.date() - timedelta(days=1), now.date())
        candidates = sorted((w for anchor in anchors if self.start <= anchor <= self.end
                             for w in self._anchor_windows(anchor)), key=lambda w: w.start)
        self._check_overlap(candidates)
        for window in candidates:
            if window.start <= now < window.end:
                if not window.trading_day:
                    raise RuntimeError("日历未覆盖夜盘所属的下一交易日")
                return window
        # An offset-1 segment at the left edge requires a missing prior natural date.
        if now.date() == self.start and any(s.start_day_offset or s.end_day_offset
                                          for s in self.segments):
            seconds = (now.hour * 3600 + now.minute * 60 + now.second) * 10**9
            if any(s.start_offset_ns <= 86400 * 10**9 + seconds < s.end_offset_ns for s in self.segments):
                raise RuntimeError("日历未覆盖跨午夜会话的起始日期")
        return None

    def _minutes(self, before_ns, after_ns=None):
        self._require_day(ns_datetime(before_ns - 1, self.zone).date())
        if after_ns is not None:
            self._require_day(ns_datetime(after_ns, self.zone).date())
            if after_ns >= before_ns:
                raise ValueError("分钟查询结束须晚于断点")
        first_day = self.start if after_ns is None else ns_datetime(after_ns, self.zone).date()
        first_day = max(self.start, first_day - timedelta(days=1))
        last_day = ns_datetime(before_ns - 1, self.zone).date()
        windows = sorted((w for day in self.calendar.days if first_day <= day <= last_day
                          for w in self._anchor_windows(day)), key=lambda w: w.start)
        self._check_overlap(windows)
        for window in windows:
            left, right = datetime_ns(window.start), datetime_ns(window.end)
            end = min(right, before_ns // MINUTE_NS * MINUTE_NS)
            if after_ns is not None:
                left = max(left, ((after_ns + 1) // MINUTE_NS) * MINUTE_NS)
            if left >= end:
                continue
            if not window.trading_day:
                raise RuntimeError("日历未覆盖该分钟所属的下一交易日")
            for start in range(left, end, MINUTE_NS):
                stamp = start + MINUTE_NS - 1
                if after_ns is None or stamp > after_ns:
                    yield stamp

    def missing_minutes(self, after_ns, before_ns):
        # Validate the current edge as well, especially missing midnight anchors.
        self.window(ns_datetime(before_ns - 1, self.zone))
        self.window(ns_datetime(after_ns, self.zone))
        return tuple(self._minutes(before_ns, after_ns))

    def last_minutes(self, before_ns, count):
        if type(count) is not int or not 0 <= count <= self.max_minutes:
            raise ValueError(f"历史分钟窗口须为0至{self.max_minutes}整数")
        if not count:
            return ()
        self.window(ns_datetime(before_ns - 1, self.zone))
        # Walk anchors backwards, stopping once enough completed buckets are found.
        result = []
        limit = before_ns // MINUTE_NS * MINUTE_NS
        day = ns_datetime(before_ns - 1, self.zone).date()
        while day >= self.start:
            lower = datetime_ns(datetime.combine(day, time(), self.zone))
            upper = min(limit, datetime_ns(datetime.combine(day + timedelta(days=1), time(), self.zone)))
            anchors = (day - timedelta(days=1), day)
            windows = sorted((w for anchor in anchors if self.start <= anchor <= self.end
                              for w in self._anchor_windows(anchor)), key=lambda w: w.start)
            self._check_overlap(windows)
            for window in reversed(windows):
                left, right = max(datetime_ns(window.start), lower), min(datetime_ns(window.end), upper)
                if right <= left:
                    continue
                if not window.trading_day:
                    raise RuntimeError("日历未覆盖该分钟所属的下一交易日")
                for stamp in range(right - 1, left - 1, -MINUTE_NS):
                    result.append(stamp)
                    if len(result) == count:
                        return tuple(reversed(result))
            day -= timedelta(days=1)
        if len(result) < count:
            raise RuntimeError("日历覆盖范围内的完整交易分钟不足")
        return tuple(reversed(result))


class SessionRulesCheckpoint:
    """Checkpoint component: identical normalized rules are required on resume."""
    def __init__(self, calendar):
        self.fingerprint = calendar.provenance["sha256"]

    def snapshot_state(self):
        return {"version": 1, "fingerprint": self.fingerprint}

    def restore_state(self, state):
        if state != self.snapshot_state():
            raise ValueError("恢复时交易时段、日历或日期例外发生变化，须明确迁移规则")
