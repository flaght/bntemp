"""账户保存的公共市场时段快照；恢复不读取策略配置或外部规则文件。"""
from dataclasses import asdict
from datetime import date

from .hours import SessionSegment, TradingHoursSnapshot
from .sessions import SessionBinding, SessionCalendar, SessionOverride
from .trading import CalendarSnapshot


def calendar_to_state(calendar):
    def override(row):
        value = asdict(row)
        value['date'] = row.date.isoformat()
        return value
    dates = asdict(calendar.calendar)
    dates.update(start=dates['start'].isoformat(), end=dates['end'].isoformat(),
        days=[day.isoformat() for day in dates['days']])
    return dict(binding=asdict(calendar.binding), hours=list(calendar.hours.records()),
        calendar=dates, overrides=[override(row) for row in calendar.overrides])


def calendar_from_state(state):
    if not isinstance(state, dict) or set(state) != {'binding','hours','calendar','overrides'}:
        raise ValueError('市场时段快照格式无效')
    rows = []
    for raw in state['hours']:
        row = dict(raw)
        code = row.pop('code')
        rows.append((code, SessionSegment(**row)))
    dates = dict(state['calendar'])
    dates.update(start=date.fromisoformat(dates['start']), end=date.fromisoformat(dates['end']),
        days=tuple(date.fromisoformat(day) for day in dates['days']))
    overrides = []
    for raw in state['overrides']:
        row = dict(raw)
        row['date'] = date.fromisoformat(row['date'])
        row['segments'] = tuple(SessionSegment(**part) for part in row['segments'])
        overrides.append(SessionOverride(**row))
    return SessionCalendar(TradingHoursSnapshot(tuple(rows),'account-execution-registry'),
        CalendarSnapshot(**dates), SessionBinding(**state['binding']), overrides=overrides)
