"""真实合约分钟落盘、重启恢复和发布前持久化的离线检查。"""
from bomber.framework.dataprep.history import HistoryService
from contextlib import redirect_stdout
from dataclasses import replace
from decimal import Decimal
from importlib import import_module
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import json
import unittest

from bomber.framework.dataprep.history import FuturesMinuteArchive, HistoryBar, HistoryError, HistoryRequest
from bomber.framework.dataprep.history.file import FileHistoryProvider
from tests.test_demo_hybrid_live import Feed, calendar, load_feed_module, ns

MINUTE=60_000_000_000


def bar(label='2026-10-28 09:00:00', volume=2):
    return HistoryBar('rb2701.SHFE',ns(label)+MINUTE-1,
        open='100.025',high='100.025',low='100.025',close='100.025',
        volume=volume,value=Decimal('100.025')*volume*10,open_interest=500)


class MinuteArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'rb2701.SHFE.jsonl'

    def archive(self, **kwargs):
        archive=FuturesMinuteArchive(self.path,instrument_id='rb2701.SHFE',contract_multiplier=10,**kwargs)
        self.addCleanup(archive.close)
        return archive

    def test_append_flush_fsync_then_reopen_preserves_raw_precision_and_canonical_reader(self):
        saved=bar()
        archive=self.archive().open()
        with patch('bomber.framework.dataprep.history.recording.os.fsync',wraps=__import__('os').fsync) as sync:
            self.assertTrue(archive.append(saved))
            sync.assert_called_once()
        stored=json.loads(self.path.read_text())
        self.assertEqual(stored['value'],'2000.500')
        self.assertEqual(stored['ts_event'],saved.ts_event)
        archive.close()
        restored=self.archive().open()
        self.assertEqual(restored.recent(before_ns=saved.ts_event+1,limit=1500),(saved,))
        self.assertEqual(restored.recorded_bars,1)
        reader=FileHistoryProvider(self.path)
        reader.open()
        self.assertEqual(reader.read(HistoryRequest(saved.instrument_id,saved.ts_event-MINUTE,saved.ts_event+1)),(saved,))

    def test_same_minute_is_deduplicated_but_conflicts_and_rollback_reject(self):
        archive=self.archive().open()
        saved=bar()
        archive.append(saved)
        size=self.path.stat().st_size
        self.assertFalse(archive.append(saved))
        self.assertEqual(self.path.stat().st_size,size)
        with self.assertRaisesRegex(HistoryError,'内容冲突'):
            archive.append(replace(saved,open_interest=501))
        archive.append(bar('2026-10-28 09:01:00'))
        with self.assertRaisesRegex(HistoryError,'时间倒退'):
            archive.append(saved)
        self.assertEqual(archive.recorded_bars,2)

    def test_tail_window_bounded_but_file_retains_earlier_records(self):
        archive=self.archive(max_bars=2).open()
        points=[bar(f'2026-10-28 09:0{i}:00') for i in range(3)]
        for point in points:archive.append(point)
        self.assertEqual(archive.recent(before_ns=points[-1].ts_event+1,limit=2),tuple(points[1:]))
        self.assertEqual(len(self.path.read_text().splitlines()),3)
        archive.close()
        archive=self.archive(max_bars=2).open()
        self.assertEqual(archive.recorded_bars,3)
        self.assertEqual(archive.recent(before_ns=points[-1].ts_event,limit=2),(points[1],))

    def test_simultaneous_writer_is_rejected_and_close_releases_lock(self):
        first=self.archive().open()
        second=self.archive()
        with self.assertRaisesRegex(HistoryError,'已有写入进程'):second.open()
        first.close()
        second.open()
        second.append(bar())

    def test_damaged_tail_is_reported_without_overwriting_saved_bytes(self):
        archive=self.archive().open()
        archive.append(bar())
        archive.close()
        with self.path.open('ab') as stream:stream.write(b'{"instrument_id":')
        contents=self.path.read_bytes()
        with self.assertRaisesRegex(HistoryError,'尾行未完整写入'):self.archive().open()
        self.assertEqual(self.path.read_bytes(),contents)

    def test_instrument_and_multiplier_mismatch_reject(self):
        archive=self.archive().open()
        with self.assertRaises(HistoryError):archive.append(replace(bar(),instrument_id='rb2705.SHFE'))
        archive.append(bar())
        archive.close()
        wrong=FuturesMinuteArchive(self.path,instrument_id='rb2701.SHFE',contract_multiplier=5)
        with self.assertRaisesRegex(HistoryError,'乘数不一致'):wrong.open()
        with self.assertRaisesRegex(HistoryError,'真实合约'):
            FuturesMinuteArchive(self.path,instrument_id='../rb',contract_multiplier=10)

    def test_partial_model_window_reloads_without_history_decisions_and_reports_real_gaps(self):
        module=import_module('demos.10_hybrid_transformer.history')
        archive=self.archive(max_bars=10).open()
        points=(bar(),bar('2026-10-28 09:02:00'))
        for point in points:archive.append(point)
        archive.close()
        archive=self.archive(max_bars=10).open()
        predictor=NS(warm_history=Mock(return_value=2),on_bar=Mock())
        runner=NS(bar_archive=archive,calendar=calendar(),history_service=HistoryService(missing_policy='allow'),bound_instrument='rb2701.SHFE',
            _clock_ns=lambda:ns('2026-10-28 09:03:02'),
            strategy=NS(config=NS(contract_multiplier=Decimal(10)),predictor=predictor))
        with redirect_stdout(StringIO()) as output:
            module.warm_recording(runner,product='RB',max_bars=10)
        rows=predictor.warm_history.call_args.args[0]
        self.assertEqual(len(rows),2)
        self.assertEqual([r['trade_time'].strftime('%H:%M') for r in rows],['09:00','09:02'])
        self.assertEqual(rows[0]['vwap'],100.025)
        self.assertEqual(runner.history_service.last_audit['required'],10)
        self.assertEqual(runner.history_service.last_audit['loaded'],2)
        self.assertEqual(runner.history_service.last_audit['missing'],8)
        self.assertIn('真实缺口',output.getvalue())
        predictor.on_bar.assert_not_called()
        self.assertEqual(runner.history_last_ns,points[-1].ts_event)

    def test_old_cache_is_not_presented_as_current_window_and_future_rows_are_excluded(self):
        module=import_module('demos.10_hybrid_transformer.history')
        archive=self.archive(max_bars=10).open()
        archive.append(bar('2026-10-27 09:00:00'))
        archive.append(bar('2026-10-28 09:03:00'))
        predictor=NS(warm_history=Mock(return_value=0))
        runner=NS(bar_archive=archive,calendar=calendar(),history_service=HistoryService(missing_policy='allow'),bound_instrument='rb2701.SHFE',
            _clock_ns=lambda:ns('2026-10-28 09:03:02'),
            strategy=NS(config=NS(contract_multiplier=Decimal(10)),predictor=predictor))
        with redirect_stdout(StringIO()):module.warm_recording(runner,product='RB',max_bars=10)
        predictor.warm_history.assert_called_once_with([])
        self.assertEqual(runner.history_last_ns,-1)

    def feed(self, archive):
        module=load_feed_module()
        now=[ns('2026-10-28 09:00:00')]
        upstream=Feed()
        config=NS(source='ctp',max_age_seconds=120)
        feed=module.FuturesMinuteBarFeed('recorded',upstream,config=config,calendar=calendar(),
            clock_ns=lambda:now[0],archive=archive)
        feed.register_instrument(NS(instrument_id='rb2701.SHFE',multiplier=Decimal(10)))
        feed.subscribe('rb2701.SHFE')
        feed.connect()
        self.addCleanup(feed.disconnect)
        upstream.depth('rb2701.SHFE',ns('2026-10-28 09:00:59'),
            dict(TradingDay='20261028',Volume=10,Turnover=10000,LastPrice=100,OpenInterest=500))
        upstream.depth('rb2701.SHFE',ns('2026-10-28 09:01:20'),
            dict(TradingDay='20261028',Volume=12,Turnover=12000,LastPrice=100,OpenInterest=500))
        now[0]=ns('2026-10-28 09:02:02')
        return feed

    def test_complete_bar_is_saved_before_model_callback_and_partial_startup_minute_is_not_saved(self):
        archive=self.archive()
        feed=self.feed(archive)
        feed._emit_custom_bar=Mock(side_effect=RuntimeError('model callback failed'))
        with self.assertRaisesRegex(RuntimeError,'model callback failed'):feed.poll()
        self.assertEqual(archive.recorded_bars,1)
        self.assertEqual(json.loads(self.path.read_text())['ts_event'],ns('2026-10-28 09:02')-1)

    def test_disk_failure_never_publishes_unrecorded_minute(self):
        archive=self.archive()
        feed=self.feed(archive)
        with patch('bomber.framework.dataprep.history.recording.os.fsync',side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError,'disk failure'):feed.poll()
        self.assertFalse(feed.events)
        self.assertEqual(archive.recorded_bars,0)
        self.assertIsNotNone(feed.failure)


if __name__=='__main__':unittest.main()
