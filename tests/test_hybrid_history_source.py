"""Hybrid共用DolphinDB历史源：交易日归属、累计量额和预热，不联网。"""
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
import json
import unittest

import pandas as pd

from bomber.framework.calendars import (
    CalendarSnapshot, SessionBinding, SessionCalendar, SessionSegment, TradingHoursSnapshot)
from bomber.framework.dataprep.history import (
    DolphinDbHistoryConfig, DolphinDbHistoryProvider, HistoryError, HistoryRequest)
from bomber.framework.dataprep.sources import DolphinDbReferenceConfig

ZONE = ZoneInfo('Asia/Shanghai')
MINUTE = 60_000_000_000
ROOT = Path(__file__).resolve().parents[1]


def ns(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=ZONE).timestamp()) * 10**9


def sessions():
    days = (date(2026,10,15), date(2026,10,16), date(2026,10,19), date(2026,10,20))
    dates = CalendarSnapshot('fixture', 'v1', days[0], days[-1], days, 'test')
    parts = (('night', 'NIGHT_START', 0, '21:00', 1, '00:02'),
             ('am', 'TRADING_DAY', 0, '09:00', 0, '09:03'),
             ('pm', 'TRADING_DAY', 0, '13:30', 0, '13:32'))
    hours = TradingHoursSnapshot(tuple(('RB', SessionSegment(*part)) for part in parts), 'test')
    return SessionCalendar(hours, dates, SessionBinding('RB','RB','fixture','Asia/Shanghai'))


def row(clock, volume, value=None, day='2026.10.19', interest=500):
    return dict(instrument_id='rb2701', history_day=day, history_time=clock,
        open=100, high=100, low=100, close=100, volume=volume,
        value=volume*1000 if value is None else value, open_interest=interest)


class TradingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.calendar = sessions()
        self.connection = DolphinDbReferenceConfig('fake',8848,'fake','fake')
        self.config = DolphinDbHistoryConfig(self.connection, 'dfs://min_bar', 'shfe_1min',
            columns=dict(instrument_id='Code',open='open',high='high',low='low',close='close',
                volume='volume',value='turnover',open_interest='open_interest'),
            date_column='date',time_column='minTime',source_timezone='Asia/Shanghai',
            time_label='start',date_basis='trading',volume_mode='cumulative',
            instrument_format='symbol',venue='SHFE',session_calendar=self.calendar)

    def read(self, rows, start, end, config=None):
        self.sdk = Mock()
        self.sdk.connect.return_value = True
        self.sdk.run.return_value = pd.DataFrame(rows)
        provider = DolphinDbHistoryProvider(config or self.config, session_factory=lambda **kwargs:self.sdk)
        provider.open()
        try:
            return provider.read(HistoryRequest('rb2701.SHFE',ns(start),ns(end)))
        finally:
            provider.close()

    def test_monday_trading_day_restores_friday_night_and_differences_amount(self):
        bars = self.read([row('21:00:00.000',2),row('21:01:00.000',5,interest=510)],
            '2026-10-16 21:00','2026-10-16 21:02')
        self.assertEqual([b.ts_event for b in bars], [ns('2026-10-16 21:01')-1, ns('2026-10-16 21:02')-1])
        self.assertEqual([b.volume for b in bars],[2,3])
        self.assertEqual([b.value for b in bars],[2000,3000])
        self.assertEqual(bars[-1].open_interest,510)
        query = self.sdk.run.call_args.args[0]
        self.assertIn('date in [2026.10.19]',query)
        self.assertIn('Code="rb2701"',query)
        self.assertNotIn('concatDateTime',query)
        self.sdk.close.assert_called_once()

    def test_midnight_does_not_reset_and_input_sorted_by_actual_time(self):
        bars = self.read([row('00:00:00.000',12),row('23:59:00.000',10)],
            '2026-10-17 00:00','2026-10-17 00:01')
        self.assertEqual(len(bars),1)
        self.assertEqual(bars[0].volume,2)
        self.assertEqual(bars[0].ts_event,ns('2026-10-17 00:01')-1)

    def test_intraday_window_reads_earlier_baseline_and_excludes_current_minute(self):
        bars = self.read([row('09:00:00.000',100),row('09:01:00.000',102),row('09:02:00.000',105)],
            '2026-10-19 09:01','2026-10-19 09:02')
        self.assertEqual([b.volume for b in bars],[2])
        self.assertEqual(bars[0].value,2000)

    def test_lunch_break_does_not_reset(self):
        bars = self.read([row('09:02:00.000',100),row('13:30:00.000',103)],
            '2026-10-19 13:30','2026-10-19 13:31')
        self.assertEqual([b.volume for b in bars],[3])

    def test_new_trading_day_resets_at_its_first_night_minute(self):
        bars = self.read([row('09:02:00.000',100),row('21:00:00.000',2,day='2026.10.20')],
            '2026-10-19 09:02','2026-10-19 21:01')
        self.assertEqual([b.volume for b in bars],[2])
        self.assertEqual(bars[0].ts_event,ns('2026-10-19 21:01')-1)

    def test_intraday_first_row_cannot_claim_all_prior_volume(self):
        bars = self.read([row('09:01:00.000',102)],'2026-10-19 09:01','2026-10-19 09:02')
        self.assertEqual(bars,())

    def test_gap_does_not_assign_multiple_minutes_to_last_bar(self):
        with self.assertRaisesRegex(HistoryError,'缺少前一分钟'):
            self.read([row('09:00:00.000',100),row('09:02:00.000',105)],
                '2026-10-19 09:02','2026-10-19 09:03')

    def test_decrease_or_unmatched_amount_rejected(self):
        for point in [row('09:01:00.000',99),row('09:01:00.000',100,101000)]:
            with self.subTest(point=point),self.assertRaisesRegex(HistoryError,'倒退或不同步'):
                self.read([row('09:00:00.000',100),point],
                    '2026-10-19 09:01','2026-10-19 09:02')

    def test_duplicate_conflict_rejected_and_identical_duplicate_deduplicated(self):
        points=[row('21:00:00.000',2),row('21:00:00.000',2)]
        self.assertEqual(len(self.read(points,'2026-10-16 21:00','2026-10-16 21:01')),1)
        points[-1]['open_interest']=501
        with self.assertRaisesRegex(HistoryError,'内容冲突'):
            self.read(points,'2026-10-16 21:00','2026-10-16 21:01')

    def test_per_bar_values_are_not_differenced(self):
        bars=self.read([row('09:00:00.000',2),row('09:01:00.000',3)],
            '2026-10-19 09:00','2026-10-19 09:02',replace(self.config,volume_mode='per-bar'))
        self.assertEqual([b.volume for b in bars],[2,3])

    def test_end_label_supported_without_shifting_one_minute(self):
        bars=self.read([row('21:01:00.000',2)],'2026-10-16 21:00','2026-10-16 21:01',
            replace(self.config,time_label='end'))
        self.assertEqual(bars[0].ts_event,ns('2026-10-16 21:01')-1)

    def test_truncation_and_missing_calendar_fail(self):
        with self.assertRaisesRegex(HistoryError,'会话日历'):
            replace(self.config,session_calendar=None)
        sdk=Mock()
        sdk.connect.return_value=True
        sdk.run.return_value=pd.DataFrame([row('21:00:00.000',2),row('21:01:00.000',3)])
        provider=DolphinDbHistoryProvider(self.config,session_factory=lambda **kwargs:sdk)
        provider.open()
        try:
            with self.assertRaisesRegex(HistoryError,'截断'):
                provider.read(HistoryRequest('rb2701.SHFE',ns('2026-10-16 21:00'),ns('2026-10-16 21:02'),max_rows=1))
        finally:
            provider.close()

    def test_actual_hybrid_build_and_warm_only_calls_predictor(self):
        history=import_module('demos.10_hybrid_transformer.history')
        source=import_module('bomber.framework.dataprep.history.dolphindb')
        config_path=ROOT/'demos/10_hybrid_transformer/history_rb.json'
        def current_frame(points):
            converted=[]
            for point in points:
                point=dict(point)
                label='2026-10-16 '+point.pop('history_time')
                point['history_time_ns']=ns(label)+8*3600*10**9
                converted.append(point)
            return pd.DataFrame(converted)
        sdk=Mock()
        sdk.connect.return_value=True
        sdk.run.return_value=current_frame([row('21:00:00.000',2),row('21:01:00.000',5)])
        predictor=NS(warm_history=Mock(return_value=2),on_bar=Mock())
        from tests.test_dolphindb_connections import SPLIT
        with patch.dict('os.environ', SPLIT, clear=True):
            service=history.build_history(history_config=config_path,calendar=self.calendar)
        self.assertEqual(service.config.connection.host, 'market')
        runner=NS(calendar=self.calendar,bound_instrument='rb2701.SHFE',history_service=service,
            _clock_ns=lambda:ns('2026-10-16 21:02:03'),
            strategy=NS(config=NS(contract_multiplier=Decimal(10)),predictor=predictor))
        with patch.object(source,'_session_factory',lambda **kwargs:sdk),redirect_stdout(StringIO()) as output:
            history.warm_history(runner,product='RB',history_minutes=2)
        sdk.connect.assert_called_once_with('market', 8850, 'market-user', 'market-secret',
            reconnect=False, readTimeout=15, writeTimeout=15)
        prepared=predictor.warm_history.call_args.args[0]
        self.assertEqual([r['volume'] for r in prepared],[2,3])
        self.assertEqual(prepared[0]['trade_time'].isoformat(),'2026-10-16T21:00:00+08:00')
        self.assertEqual([r['vwap'] for r in prepared],[100,100])
        predictor.on_bar.assert_not_called()
        self.assertEqual(runner.history_last_ns,ns('2026-10-16 21:02')-1)
        self.assertIn('dolphindb',output.getvalue())
        predictor.warm_history.reset_mock()
        sdk.run.return_value=current_frame([row('21:00:00.000',2),row('21:01:00.000',5,99000)])
        with patch.object(source,'_session_factory',lambda **kwargs:sdk),redirect_stdout(StringIO()) as output:
            with self.assertRaisesRegex(HistoryError,'VWAP'):
                history.warm_history(runner,product='RB',history_minutes=2)
        predictor.warm_history.assert_not_called()
        self.assertIn('Hybrid预热失败',output.getvalue())
        self.assertIn('value=97000',output.getvalue())


if __name__ == '__main__':
    unittest.main()
