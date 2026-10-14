"""公共日历调度，无网络、无真实等待；不是重复仓位算法的替身实现。"""
from datetime import date, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import unittest

from bomber.framework.calendars.hours import SessionSegment, TradingHoursSnapshot
from bomber.framework.calendars.sessions import SessionBinding, SessionCalendar, SessionOverride
from bomber.framework.calendars.trading import CalendarSnapshot
from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, orders_for_day

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

    def test_persisted_order_budget_keeps_today_across_windows(self):
        payload = {'orders': {'option-vega-live': [
            {'client_order_id': 'CTP-9999-investor-20261013-1'},
            {'client_order_id': 'CTP-9999-investor-20261014-1'},
            {'client_order_id': 'CTP-9999-investor-20261014-2'}]}}
        self.assertEqual(orders_for_day(payload, '20261014'), 2)
        self.assertEqual(orders_for_day(payload, '20261016'), 0)


if __name__ == '__main__':
    unittest.main()
