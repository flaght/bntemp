"""In-memory session inputs for deterministic unit tests, independent from deployment JSON."""
from datetime import date

from bomber.framework.calendars import (CalendarSnapshot, NativeCalendarProvider,
    SessionBinding, SessionCalendar, SessionOverride, SessionSegment, TradingHoursSnapshot)


def declared_day_sessions(product, day, *, start="09:00", end="09:05"):
    dates = CalendarSnapshot("test.declared", "fixture-v1", day, day, (day,), "declared")
    hours = TradingHoursSnapshot(((product, SessionSegment("day", "TRADING_DAY", 0, start, 0, end)),),
                                 "in-memory-test")
    return SessionCalendar(hours, dates, SessionBinding(product, product, dates.calendar_id, "Asia/Shanghai"))


def index_sessions(product):
    dates = CalendarSnapshot("test.index", "fixture-v1", date(2026, 9, 21), date(2026, 9, 22),
                             (date(2026, 9, 21), date(2026, 9, 22)), "declared")
    hours = TradingHoursSnapshot(tuple((product, SessionSegment(name, "TRADING_DAY", 0, begin, 0, end))
        for name, begin, end in (("day_1", "09:30", "11:30"), ("day_2", "13:00", "15:00"))),
        "in-memory-test")
    return SessionCalendar(hours, dates, SessionBinding(product, product, dates.calendar_id,
                                                       "Asia/Shanghai", "CFFEX"))


def commodity_sessions(product):
    dates = NativeCalendarProvider("china.sse").snapshot()
    parts = (("day_1", "TRADING_DAY", "09:00", 0, "10:15"),
             ("day_2", "TRADING_DAY", "10:30", 0, "11:30"),
             ("day_3", "TRADING_DAY", "13:30", 0, "15:00"),
             ("night", "NIGHT_START", "21:00", 1, "01:00"))
    hours = TradingHoursSnapshot(tuple((product, SessionSegment(name, anchor, 0, begin, offset, end))
        for name, anchor, begin, offset, end in parts), "in-memory-test")
    # This is an explicit exception fixture, not an inferred exchange policy.
    overrides = (SessionOverride("NIGHT_START", date(2026, 9, 30), "CANCEL", "NIGHT"),)
    return SessionCalendar(hours, dates, SessionBinding(product, product, dates.calendar_id,
        "Asia/Shanghai", "SHFE"), overrides=overrides)
