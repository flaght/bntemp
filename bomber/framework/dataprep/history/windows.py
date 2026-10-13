"""Historical windows consume the public session service; IM is a thin probe compatibility wrapper."""
from datetime import date
from bomber.framework.calendars.hours import SessionSegment, TradingHoursSnapshot
from bomber.framework.calendars.sessions import SessionBinding, SessionCalendar, datetime_ns
from bomber.framework.calendars.trading import CalendarSnapshot
from .base import HistoryError

SessionHistoryWindow = SessionCalendar


class ImDayWindow:
    """Single declared IM day retained for existing probes, using shared minute enumeration."""
    def __init__(self, trading_day):
        if type(trading_day) is not date or trading_day.weekday() >= 5:
            raise HistoryError("须声明工作日IM交易日期；真实休市日由权威数据核验")
        self.day = trading_day
        hours = TradingHoursSnapshot((("IM", SessionSegment("day_1", "TRADING_DAY", 0, "09:30", 0, "11:30")),
                                      ("IM", SessionSegment("day_2", "TRADING_DAY", 0, "13:00", 0, "15:00"))),
                                     "declared_im_probe")
        calendar = CalendarSnapshot("declared.im", "probe-v1", trading_day, trading_day,
                                    (trading_day,), "declared")
        self._service = SessionCalendar(hours, calendar, SessionBinding("IM", "IM", calendar.calendar_id,
                                                                      "Asia/Shanghai"))
        self.end_ns = datetime_ns(self._service.windows(trading_day)[-1].end)
        self.stamps = self._service.missing_minutes(datetime_ns(self._service.windows(trading_day)[0].start),
                                                     self.end_ns)

    def last_minutes(self, before_ns, count):
        try:
            return self._service.last_minutes(before_ns, count)
        except (RuntimeError, ValueError) as error:
            raise HistoryError(str(error)) from error

    def missing_minutes(self, after_ns, before_ns):
        try:
            return self._service.missing_minutes(after_ns, before_ns)
        except (RuntimeError, ValueError) as error:
            raise HistoryError(str(error)) from error
