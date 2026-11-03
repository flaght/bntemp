"""按交易日归档真实报告写出路径；无网络、无报单。"""
from contextlib import redirect_stdout
import csv
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests import test_hybrid_continuous as fixture
from bomber.framework.trader.runtime.reports import LiveRunReport, TradingDayLiveReport


class TradingDayReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def report(self, day='20261103', stamp=1, prefix='hybrid-simnow-RB'):
        report = TradingDayLiveReport(self.root, prefix=prefix, trading_day=day, clock_ns=lambda: stamp)
        report.begin()
        return report

    def rows(self, path):
        with path.open(newline='', encoding='utf-8') as stream:
            return list(csv.DictReader(stream))

    def test_night_day_and_restart_share_directory_and_append_without_duplicate_headers(self):
        for stamp, label in [(1, 'night'), (2, 'morning'), (3, 'restart')]:
            report = self.report(stamp=stamp)
            signals = [dict(trade_time=label, signal=1, detail='逗号,引号"')]
            events = [label+' accepted', label+' filled']
            report.record_csv('signals.csv', signals)
            report.record_csv('signals.csv', signals)  # 下一轮poll不能重复写。
            report.record_events(events)
            report.record_events(events)
            self.assertIn(label+' filled', (report.path/'events.txt').read_text())
            report.write(status='passed', fields={'bars_used': 1}, session=None, events=events)
        path = self.root/'hybrid-simnow-RB-20261103'
        self.assertEqual(list(self.root.iterdir()), [path])
        rows = self.rows(path/'signals.csv')
        self.assertEqual([row['trade_time'] for row in rows], ['night', 'morning', 'restart'])
        self.assertEqual([row['run_id'] for row in rows], ['1', '2', '3'])
        self.assertEqual({row['trading_day'] for row in rows}, {'20261103'})
        self.assertTrue(all(row['detail']=='逗号,引号"' for row in rows))
        self.assertEqual(len((path/'events.txt').read_text().splitlines()), 6)
        summaries = [json.loads(line) for line in (path/'sessions.jsonl').read_text().splitlines()]
        self.assertEqual([row['run_id'] for row in summaries], ['1', '2', '3'])
        latest = json.loads((path/'summary.json').read_text())
        self.assertEqual(latest['run_id'], '3')
        self.assertEqual(latest['summary_scope'], 'latest_session')

    def test_trading_day_not_natural_date_and_next_day_product_mode_are_separate(self):
        night = self.report(stamp=fixture.ns('2026-11-02 22:00'))
        day = self.report(stamp=fixture.ns('2026-11-03 09:00'))
        self.assertEqual(night.path, day.path)
        next_day = self.report(day='20261104')
        product = self.report(prefix='hybrid-simnow-CU')
        mode = self.report(prefix='hybrid-recording-RB')
        self.assertEqual(len({night.path, next_day.path, product.path, mode.path}), 4)

    def test_shutdown_appends_new_reports_without_overwriting_previous_window(self):
        night = self.report()
        night.write(status='passed', fields={}, session=None, events=['night fill'])
        day = self.report(stamp=2)
        day.record_events(['day accepted'])
        day.write(status='passed', fields={}, session=None,
                  events=['day accepted', 'day fill during shutdown'])
        self.assertEqual((day.path/'events.txt').read_text().splitlines(),
                         ['night fill', 'day accepted', 'day fill during shutdown'])

    def test_failed_preparation_retains_previous_details_and_records_failure_summary(self):
        night = self.report()
        night.record_csv('signals.csv', [dict(signal=1)])
        night.write(status='passed', fields={}, session=None, events=['night fill'])
        before = (night.path/'signals.csv').read_bytes()
        failed = self.report(stamp=2)
        failed.write(status='failed', fields={'failure': 'connection'}, session=None, events=[])
        self.assertEqual((failed.path/'signals.csv').read_bytes(), before)
        self.assertEqual((failed.path/'events.txt').read_text(), 'night fill\n')
        self.assertEqual(len((failed.path/'sessions.jsonl').read_text().splitlines()), 2)

    def test_schema_change_does_not_overwrite_existing_csv(self):
        report = self.report()
        report.record_csv('signals.csv', [dict(signal=1)])
        path = report.path/'signals.csv'
        before = path.read_bytes()
        restarted = self.report(stamp=2)
        with self.assertRaisesRegex(ValueError, '列不一致'):
            restarted.record_csv('signals.csv', [dict(prediction=1)])
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_report_path_and_end_of_session_behavior_are_unchanged(self):
        report = LiveRunReport(self.root, prefix='other', clock_ns=lambda: 9)
        report.begin()
        report.write(status='passed', fields={}, session=object(), events=['fill'])
        self.assertEqual(report.path, self.root/'other-9')
        self.assertEqual((report.path/'events.txt').read_text(), 'fill\n')
        self.assertFalse((report.path/'sessions.jsonl').exists())


class HybridDailyReportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ContinuousTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.market = fixture.live.MarketSourceConfig(source='dolphindb', table='fixture',
            timestamp_label='start', volume_mode='per-bar')
        self.session, _, _ = self.fixture.session(market_config=self.market)

    def runtime(self, day='20261028'):
        f = self.fixture
        return fixture.live.build_runtime(product='RB', release_path=f.release,
            directions_file=f.directions, signal_config=f.signal,
            expected_trading_day=day, expected_source_day='20261027',
            market_config=self.market, report_dir=f.root/'reports')

    def test_progress_records_new_signals_batches_and_late_events_without_bar_change(self):
        runtime = self.runtime()
        report = runtime.report
        report.begin()
        strategy = self.session.strategy
        strategy.signal_records.append(dict(trade_time='night', signal=1))
        strategy.batch_records.append(dict(event='open', quantity=2))
        events = ['accepted']
        with patch.object(runtime.controller, 'events', side_effect=lambda session: tuple(events)):
            runtime.progress(self.session)
            events.append('filled later')
            runtime.progress(self.session)  # bars_used未变，仍必须记录成交。
        with (report.path/'signals.csv').open(newline='') as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)
        with (report.path/'batches.csv').open(newline='') as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)
        self.assertEqual((report.path/'events.txt').read_text(), 'accepted\nfilled later\n')
        events.append('shutdown cancellation')
        report.write(status='passed', fields={}, session=self.session, events=events)
        self.assertTrue((report.path/'strategy_state.json').is_file())
        self.assertIn('shutdown cancellation', (report.path/'events.txt').read_text())
        self.assertEqual(report.path.name, 'hybrid-recording-RB-20261028')

    def test_continuous_window_trading_day_drives_report_directory(self):
        f = self.fixture
        context = fixture.continuous.LiveWindowContext(
            f.cal, fixture.TradingWindow('20261029', 'night',
                fixture.dt('2026-10-28 21:00'), fixture.dt('2026-10-29 02:30')),
            fixture.Event(), '20261028', resume=False)
        runtime = fixture.live.build_runtime(product='RB', release_path=f.release,
            directions_file=f.directions, signal_config=f.signal,
            market_config=self.market, report_dir=f.root/'reports', window_context=context)
        runtime.report.begin()
        self.assertEqual(runtime.report.path.name, 'hybrid-recording-RB-20261029')


if __name__ == '__main__':
    with redirect_stdout(StringIO()):
        unittest.main()
