"""Compatibility entry points; session algorithms live in framework.calendars."""
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo
import json

from bomber.framework.calendars.hours import SessionSegment, TradingHoursSnapshot
from bomber.framework.calendars.sessions import SessionBinding, SessionOverride, TradingWindow
from bomber.framework.calendars.sessions import SessionCalendar as CalendarService
from bomber.framework.calendars.trading import CalendarSnapshot

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _legacy_dates(data, start, end, key):
    days = [date.fromisoformat(d) for d in data.get(key, [])]
    if len(set(days)) != len(days) or any(not start <= d <= end for d in days):
        raise ValueError("日历日期重复或超出覆盖范围")
    return set(days)


def _legacy_segments(rows, *, allow_empty=False):
    if not isinstance(rows, list) or (not rows and not allow_empty):
        raise ValueError("session_segments须显式声明非空会话时段")
    result = []
    for index, row in enumerate(rows):
        if not row.keys() <= {"start", "end", "end_day_offset", "trading_day_rule"}:
            raise ValueError("未知会话时段字段")
        rule = row["trading_day_rule"]
        if rule not in {"same_day", "next_trading_day"}:
            raise ValueError("未知交易日归属规则")
        result.append(SessionSegment(f"legacy_{index}",
            "TRADING_DAY" if rule == "same_day" else "NIGHT_START", 0, row["start"],
            row.get("end_day_offset", 0), row["end"]))
    return tuple(result)


def _legacy_service(data, path, raw, product):
    name = data["product"].upper()
    if product is not None and product.upper() != name:
        raise ValueError(f"日历品种{name}与请求{product}不一致")
    first, last = date.fromisoformat(data["start"]), date.fromisoformat(data["end"])
    if first > last:
        raise ValueError("日历覆盖范围不合法")
    holidays = _legacy_dates(data, first, last, "holidays")
    no_night = _legacy_dates(data, first, last, "no_night")
    if "trading_days" in data:
        if "weekdays" in data or holidays:
            raise ValueError("显式交易日期不能同时声明weekdays/holidays")
        days = _legacy_dates(data, first, last, "trading_days")
    else:
        weekdays = data["weekdays"]
        if (not isinstance(weekdays, list) or not weekdays or len(set(weekdays)) != len(weekdays)
                or any(type(d) is not int or not 0 <= d <= 6 for d in weekdays)):
            raise ValueError("weekdays须显式声明0至6且不重复")
        days = {first + timedelta(days=i) for i in range((last - first).days + 1)
                if (first + timedelta(days=i)).weekday() in weekdays and first + timedelta(days=i) not in holidays}
    version = sha256(raw).hexdigest()
    calendar_id = f"legacy.{name.lower()}"
    calendar = CalendarSnapshot(calendar_id, version, first, last, tuple(sorted(days)), "legacy-config")
    segments = _legacy_segments(data["session_segments"])
    hours = TradingHoursSnapshot(tuple((name, s) for s in segments), str(Path(path).resolve()))
    overrides = []
    replacements = data.get("overrides", {})
    for day in no_night:
        if str(day) not in replacements:
            overrides.append(SessionOverride("NIGHT_START", day, "CANCEL", "NIGHT"))
    for day, rows in replacements.items():
        day = date.fromisoformat(day)
        parts = _legacy_segments(rows, allow_empty=True)
        overrides.append(SessionOverride("TRADING_DAY", day, "REPLACE", "DAY",
            tuple(s for s in parts if s.date_anchor == "TRADING_DAY")))
        overrides.append(SessionOverride("NIGHT_START", day, "REPLACE", "NIGHT",
            tuple(s for s in parts if s.date_anchor == "NIGHT_START")))
    return CalendarService(hours, calendar, SessionBinding(name, name, calendar_id, data["timezone"]),
                           overrides=overrides)


class SessionCalendar:
    """Load old JSON or v2 source assembly, then delegate every query to one service."""
    def __init__(self, path, product=None, *, purpose=None):
        raw = Path(path).read_bytes()
        data = json.loads(raw)
        self.schema_version = data.get("schema_version")
        if data.get("schema_version") == 1:
            self._service = _legacy_service(data, path, raw, product)
        elif data.get("schema_version") == 2:
            from bomber.framework.dataprep.trading_hours import load_session_config
            self._service = load_session_config(data, path, product=product, purpose=purpose)
        else:
            raise ValueError("会话配置须声明schema_version=1或2")

    def __getattr__(self, name):
        return getattr(self._service, name)


CopperSessions = SessionCalendar
IndexSessions = SessionCalendar


def load_sessions(path, product, *, purpose=None):
    return SessionCalendar(path, product, purpose=purpose)


def validate_sessions(path, product, *, purpose=None):
    """CLI preflight must not query a database before explicit runtime startup."""
    data = json.loads(Path(path).read_bytes())
    if data.get("schema_version") == 2:
        from bomber.framework.dataprep.trading_hours import validate_session_config
        validate_session_config(data, path, product=product, purpose=purpose)
    else:
        load_sessions(path, product, purpose=purpose)
