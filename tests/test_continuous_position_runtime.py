"""公共日历调度，无网络、无真实等待；不是重复仓位算法的替身实现。"""
from datetime import date, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import unittest

from bomber.framework.calendars.hours import SessionSegment, TradingHoursSnapshot
from bomber.framework.calendars.sessions import SessionBinding, SessionCalendar, SessionOverride
from bomber.framework.calendars.trading import CalendarSnapshot
from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, orders_for_day, wait_for_open, StopRequested

ZONE = ZoneInfo('Asia/Shanghai')


def calendar(overrides=()):
    dates = CalendarSnapshot('china.cffex', 'fixture-v1', date(2026, 10, 9), date(2026, 10, 20),
        tuple(date(2026, 10, n) for n in (9, 12, 13, 14, 16, 19, 20)), 'fixture')
    hours = TradingHoursSnapshot(tuple(('IM', SessionSegment(name, 'TRADING_DAY', 0, start, 0, end))
        for name, start, end in (('morning', '09:30', '11:30'), ('afternoon', '13:00', '15:00'))), 'fixture')
    return SessionCalendar(hours, dates, SessionBinding('MO', 'IM', 'china.cffex', 'Asia/Shanghai',
        'CFFEX', 'OPTION'), overrides=overrides)


class ContinuousPositionTests(unittest.TestCase):
    def runtime(self, start, *, forever=False, overrides=(), fail=None):
        self.now = datetime.fromisoformat(start).replace(tzinfo=ZONE)
        self.stop, self.seen = Event(), []
        def factory(window, stop):
            self.seen.append((window.trading_day, window.segment_id))
            def run():
                if fail:
                    raise RuntimeError(fail)
                self.now = window.end
                if forever and window.trading_day == '20261019':
                    stop.set()
                return {'status': 'completed'}
            return SimpleNamespace(run=run)
        def wait(seconds):
            self.now += timedelta(minutes=15)
        return ContinuousPositionRuntime(calendar(overrides), factory, run_forever=forever,
            stop_event=self.stop, clock=lambda: self.now, wait=wait, status=lambda _: None)

    def test_lunch_reassembles_once_same_day_and_daily_exits(self):
        runtime = self.runtime('2026-10-14T09:20')
        result = runtime.run()
        self.assertEqual(self.seen, [('20261014', 'morning'), ('20261014', 'afternoon')])
        self.assertEqual(result['windows'], 2)
        self.assertFalse(result['stopped_by_operator'])
        self.assertEqual(self.now.hour, 15)

    def test_next_day_skips_declared_holiday_and_weekend(self):
        runtime = self.runtime('2026-10-14T14:40', forever=True)
        result = runtime.run()
        self.assertEqual(self.seen, [('20261014', 'afternoon'), ('20261016', 'morning'),
            ('20261016', 'afternoon'), ('20261019', 'morning')])
        self.assertTrue(result['stopped_by_operator'])

    def test_override_early_close_drives_stop(self):
        override = SessionOverride('TRADING_DAY', date(2026, 10, 14), 'REPLACE', 'afternoon',
            (SessionSegment('afternoon', 'TRADING_DAY', 0, '13:00', 0, '14:00'),))
        runtime = self.runtime('2026-10-14T13:10', overrides=(override,))
        self.assertEqual(runtime.run()['windows'], 1)
        self.assertEqual(self.now.hour, 14)

    def test_fault_is_not_automatically_retried(self):
        runtime = self.runtime('2026-10-14T09:35', forever=True, fail='归属冲突')
        with self.assertRaisesRegex(RuntimeError, '归属冲突'):
            runtime.run()
        self.assertEqual(len(self.seen), 1)

    def test_failed_shutdown_status_blocks_next_window(self):
        runtime = self.runtime('2026-10-14T09:35', forever=True)
        runtime.session_factory = lambda *_: SimpleNamespace(run=lambda: {'status': 'failed'})
        with self.assertRaisesRegex(RuntimeError, '停机核验'):
            runtime.run()

    def test_operator_can_stop_while_closed_without_connecting(self):
        runtime = self.runtime('2026-10-15T10:00', forever=True)
        runtime.wait = lambda _: self.stop.set()
        self.assertTrue(runtime.run()['stopped_by_operator'])
        self.assertEqual(self.seen, [])

    def test_calendar_coverage_failure_is_not_guessed(self):
        runtime = self.runtime('2026-10-21T10:00', forever=True)
        with self.assertRaisesRegex(RuntimeError, '覆盖不足'):
            runtime.run()
        self.assertEqual(self.seen, [])

    def test_clock_regression_blocks_old_window_reentry(self):
        runtime = self.runtime('2026-10-14T09:35')
        runtime.now()
        self.now -= timedelta(minutes=1)
        with self.assertRaisesRegex(RuntimeError, '墙钟倒退'):
            runtime.run()

    def test_normal_return_early_does_not_restart_within_same_window(self):
        runtime = self.runtime('2026-10-14T14:30')
        runtime.session_factory = lambda *_: SimpleNamespace(run=lambda: {'status': 'completed'})
        self.assertEqual(runtime.run()['windows'], 1)

    def test_preopen_prepares_each_session_five_minutes_early_without_reentry(self):
        now = [datetime(2026, 10, 14, 9, 20, tzinfo=ZONE)]
        seen, messages = [], []
        def factory(window, stop):
            seen.append((now[0], window))
            def run():
                now[0] = window.end
                return {'status': 'completed'}
            return SimpleNamespace(run=run)
        runtime = ContinuousPositionRuntime(calendar(), factory, prepare_seconds=300,
            clock=lambda: now[0], wait=lambda seconds: now.__setitem__(0, now[0]+timedelta(seconds=seconds)),
            status=messages.append)
        self.assertEqual(runtime.run()['windows'], 2)
        self.assertEqual([at.strftime('%H:%M') for at, _ in seen], ['09:25', '12:55'])
        self.assertTrue(all(window.start-at == timedelta(minutes=5) for at, window in seen))
        self.assertEqual(sum('进入盘前准备窗口' in line for line in messages), 2)

    def test_preopen_night_uses_next_trading_day_and_holiday_override(self):
        cal = calendar(overrides=(SessionOverride('TRADING_DAY', date(2026, 10, 19),
            'CANCEL', 'morning', ()),))
        hours = TradingHoursSnapshot(cal.hours.rows + (('IM', SessionSegment(
            'night', 'NIGHT_START', 0, '21:00', 0, '23:00')),), 'night-fixture')
        cal = SessionCalendar(hours, cal.calendar, cal.binding, overrides=cal.overrides)
        now = datetime(2026, 10, 16, 20, 55, tzinfo=ZONE)
        stop, seen = Event(), []
        def factory(window, event):
            seen.append(window)
            event.set()
            return SimpleNamespace(run=lambda: {'status': 'completed'})
        result = ContinuousPositionRuntime(cal, factory, run_forever=True, prepare_seconds=300,
            clock=lambda: now, stop_event=stop, status=lambda _: None).run()
        self.assertEqual(result['windows'], 1)
        self.assertEqual(seen[0].trading_day, '20261019')
        self.assertEqual(seen[0].start.date(), date(2026, 10, 16))
        morning = datetime(2026, 10, 19, 9, 25, tzinfo=ZONE)
        self.assertIsNone(cal.next_window(morning, before=morning+timedelta(minutes=5)))

    def test_next_window_finds_short_overridden_segment_within_prepare_period(self):
        override = SessionOverride('TRADING_DAY', date(2026, 10, 14), 'REPLACE', 'morning',
            (SessionSegment('morning', 'TRADING_DAY', 0, '09:30', 0, '09:31'),))
        now = datetime(2026, 10, 14, 9, 29, tzinfo=ZONE)
        window = calendar((override,)).next_window(now, before=now+timedelta(minutes=5))
        self.assertEqual(window.start.hour, 9)
        self.assertEqual(window.start.minute, 30)

    def test_preopen_wait_polls_with_decisions_closed_until_boundary_and_honors_stop(self):
        start = datetime(2026, 10, 14, 9, 30, tzinfo=ZONE)
        now, polls, messages = [start-timedelta(minutes=5)], [], []
        stop = Event()
        def poll():
            polls.append(now[0])
            now[0] = start
        wait_for_open(start, stop, clock=lambda: now[0], poll=poll, wait=lambda _: None,
                      status=messages.append)
        self.assertEqual(len(polls), 1)
        self.assertIn('报单保持关闭', messages[0])
        stop.set()
        with self.assertRaises(StopRequested):
            wait_for_open(start, stop, clock=lambda: now[0])
        with self.assertRaises(ValueError):
            wait_for_open(start.replace(tzinfo=None), Event())

    def test_preopen_poll_failure_is_not_hidden_or_retried(self):
        start = datetime(2026, 10, 14, 9, 30, tzinfo=ZONE)
        def fail():
            raise RuntimeError('账户失去连接')
        with self.assertRaisesRegex(RuntimeError, '账户失去连接'):
            wait_for_open(start, Event(), clock=lambda: start-timedelta(minutes=1),
                poll=fail, status=lambda _: None)
        for invalid in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                ContinuousPositionRuntime(calendar(), None, prepare_seconds=invalid)

    def test_persisted_order_budget_keeps_today_across_windows(self):
        payload = {'orders': {'option-vega-live': [
            {'client_order_id': 'CTP-9999-investor-20261013-1'},
            {'client_order_id': 'CTP-9999-investor-20261014-1'},
            {'client_order_id': 'CTP-9999-investor-20261014-2'}]}}
        self.assertEqual(orders_for_day(payload, '20261014'), 2)
        self.assertEqual(orders_for_day(payload, '20261016'), 0)


if __name__ == '__main__':
    unittest.main()
