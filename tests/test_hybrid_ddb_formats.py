"""新DolphinDB历史TIMESTAMP与分钟状态流；用现有SDK替身离线运行。"""
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import json
import unittest
from pathlib import Path

import pandas as pd
from bomber.framework.dataprep.history import DolphinDbHistoryProvider, HistoryError, HistoryRequest
from tests import test_hybrid_history_source as legacy_history
from tests.test_hybrid_history_source import ns, row

MINUTE=60_000_000_000


class TimestampHistoryTests(unittest.TestCase):
    def setUp(self):
        fixture=legacy_history.TradingHistoryTests('runTest')
        fixture.setUp()
        self.config=replace(fixture.config,time_column_kind='timestamp')

    def timestamp_row(self, natural, volume, day='2026.10.19'):
        point=row('unused',volume,day=day)
        point.pop('history_time')
        point['history_time_ns']=ns(natural)+8*3600*10**9
        return point

    def read(self, points, start, end):
        sdk=Mock()
        sdk.connect.return_value=True
        sdk.run.return_value=pd.DataFrame(points)
        provider=DolphinDbHistoryProvider(self.config,session_factory=lambda **kwargs:sdk)
        provider.open()
        try:
            bars=provider.read(HistoryRequest('rb2701.SHFE',ns(start),ns(end)))
        finally:provider.close()
        self.script=sdk.run.call_args.args[0]
        return bars

    def test_full_timestamp_keeps_friday_night_in_monday_trading_partition(self):
        bars=self.read([self.timestamp_row('2026-10-16 21:00',2),
            self.timestamp_row('2026-10-16 21:01',5)],'2026-10-16 21:00','2026-10-16 21:02')
        self.assertEqual([b.volume for b in bars],[2,3])
        self.assertEqual(bars[0].ts_event,ns('2026-10-16 21:01')-1)
        self.assertIn('long(nanotimestamp(minTime)) as history_time_ns',self.script)
        self.assertNotIn('concatDateTime',self.script)

    def test_forming_and_outside_session_rows_are_not_used_as_complete_minutes(self):
        bars=self.read([self.timestamp_row('2026-10-19 08:59',100),
            self.timestamp_row('2026-10-19 09:00',102),self.timestamp_row('2026-10-19 09:01',105),
            self.timestamp_row('2026-10-19 09:02',110)],'2026-10-19 09:01','2026-10-19 09:02')
        self.assertEqual(len(bars),1)
        self.assertEqual(bars[0].volume,3)

    def test_natural_date_cannot_be_invented_from_an_incorrect_trading_date(self):
        with self.assertRaisesRegex(HistoryError,'交易日分区不一致'):
            self.read([self.timestamp_row('2026-10-16 21:00',2,day='2026.10.16')],
                '2026-10-16 21:00','2026-10-16 21:01')

    def test_new_history_sample_has_timestamp_and_confirmed_cumulative_amounts(self):
        path=Path(__file__).resolve().parents[1]/'demos/10_hybrid_transformer/history_rb.json'
        data=json.loads(path.read_text())
        self.assertEqual((data['database'],data['table']),('dfs://ctp','bar_1min_shfe'))
        self.assertEqual(data['time_column_kind'],'timestamp')
        self.assertEqual(data['volume_mode'],'cumulative')


class MinuteStateTests(unittest.TestCase):
    def setUp(self):
        converter=import_module('bomber.framework.market.stream.dolphin.converter')
        self.buffer=converter.DolphinDbMinuteStateBuffer()

    def state(self, minute='10:55', volume=2, second='01', baseline=True):
        return dict(barTime=f'2026-10-29T{minute}:00.000',date='2026-10-29',Code='rb2701',exchange='SHFE',
            open=100,high=100,low=100,close=100,volume=volume,turnover=volume*1000,
            open_interest=500,cum_volume=10000+volume,cum_turnover=10000000+volume*1000,
            volume_baseline_ok=baseline,updateTime=f'2026-10-29T{minute}:{second}.000')

    def push(self, point):
        return self.buffer.push('rb2701',point,received_ns=ns('2026-10-29 11:00'))

    def test_repeated_versions_emit_only_last_snapshot_when_next_minute_arrives(self):
        first=self.state(volume=2)
        last=self.state(volume=5,second='59')
        self.assertEqual(self.push(first),())
        self.assertEqual(self.push(last),())
        closed=self.push(self.state('10:56',volume=1))
        self.assertEqual(closed,(last,))
        self.assertEqual(self.push(last),())

    def test_late_conflicting_closed_revision_rejects_instead_of_silent_dedup(self):
        self.push(self.state())
        self.push(self.state('10:56'))
        with self.assertRaisesRegex(ValueError,'已封闭分钟'):
            self.push(self.state(volume=3,second='59'))

    def test_older_versions_ignored_but_equal_version_conflict_and_volume_rollback_reject(self):
        self.push(self.state(volume=5,second='30'))
        self.assertEqual(self.push(self.state(volume=2,second='01')),())
        self.assertEqual(self.buffer.ignored_old_versions,1)
        with self.assertRaisesRegex(ValueError,'版本内容冲突'):
            self.push(self.state(volume=6,second='30'))
        with self.assertRaisesRegex(ValueError,'成交量或成交额倒退'):
            self.push(self.state(volume=3,second='31'))

    def test_nanosecond_revisions_remain_ordered_for_sdk_and_string_values(self):
        import numpy as np
        for representation in (str, pd.Timestamp, np.datetime64):
            with self.subTest(representation=representation.__name__):
                self.buffer.reset()
                first=self.state(volume=2)
                last=self.state(volume=5)
                first['updateTime']=representation('2026-10-29T10:55:01.123456001')
                last['updateTime']=representation('2026-10-29T10:55:01.123456999')
                self.assertEqual(self.push(first),())
                self.assertEqual(self.push(last),())
                # 迟到的旧纳秒版本仍被忽略，而非覆盖最后状态。
                self.assertEqual(self.push(first),())
                self.assertEqual(self.push(self.state('10:56')),(last,))

    def test_timestamp_timezone_and_fraction_precision_are_preserved(self):
        converter=import_module('bomber.framework.market.stream.dolphin.converter')
        base=ns('2026-10-29 10:55:01')
        for fraction,delta in [('',0),('.1',100_000_000),('.123',123_000_000),
                               ('.123456',123_456_000),('.123456789',123_456_789)]:
            for stamp in ('2026-10-29T10:55:01'+fraction,
                          '2026-10-29 10:55:01'+fraction+'+08:00',
                          '2026-10-29T02:55:01'+fraction+'Z'):
                with self.subTest(stamp=stamp):
                    self.assertEqual(converter._timestamp_ns(stamp,self.buffer.timezone),base+delta)

    def test_submicrosecond_bar_time_is_not_rounded_into_a_complete_minute(self):
        point=self.state()
        point['barTime']='2026-10-29T10:55:00.000000001'
        with self.assertRaisesRegex(ValueError,'完整barTime'):
            self.push(point)

    def test_true_equal_version_conflict_reports_exact_field_values_without_replacing_pending(self):
        first=self.state(volume=2)
        changed=self.state(volume=3)
        self.push(first)
        with self.assertRaisesRegex(ValueError,'同一状态版本内容冲突') as raised:
            self.push(changed)
        message=str(raised.exception)
        for detail in ('rb2701','barTime=', 'revision_ns=', 'previous_updateTime=',
                       'current_updateTime=', "'volume': {'previous': 2.0, 'current': 3.0}",
                       "'turnover': {'previous': 2000.0, 'current': 3000.0}"):
            self.assertIn(detail,message)
        self.assertEqual(self.push(self.state('10:56')),(first,))

    def test_unknown_baseline_minute_discarded_and_session_last_minute_waits_for_next(self):
        self.push(self.state(baseline=False))
        self.assertEqual(self.push(self.state('10:56')),())
        self.assertEqual(self.buffer.discarded_baseline_minutes,1)
        self.assertEqual(len(self.buffer._pending),1)

    def test_source_future_minute_rejected(self):
        with self.assertRaisesRegex(ValueError,'已开始'):
            self.buffer.push('rb2701',self.state(),received_ns=ns('2026-10-29 10:54'))

    def test_real_feed_preserves_final_per_minute_amount_and_full_timestamp(self):
        from bomber.framework.market.basic.base import DataType, InstrumentId, InstrumentMeta
        from bomber.framework.market.stream.dolphin import DolphinDbConfig,DolphinDbStreamSpec,DolphinDbLiveDataFeed
        from bomber.framework.market.stream.futures_minute import FuturesMinuteBarFeed
        from bomber.framework.market.sources import MarketSourceConfig
        from tests.test_demo_hybrid_live import calendar
        from decimal import Decimal
        spec=DolphinDbStreamSpec.bar('market_bar_state_v2_shfe',auto_schema=True,
            volume_mode='per-bar',bar_mode='state',local_receipt_time=True)
        feed=DolphinDbLiveDataFeed(DolphinDbConfig('fake',8848,'u','p',streams=(spec,)))
        instrument=InstrumentId.from_str('rb2701.SHFE')
        meta=InstrumentMeta(instrument,price_precision=0,multiplier=Decimal(10))
        feed.register_instrument(meta)
        feed.subscribe(instrument,DataType.CUSTOM_BAR,'1-MINUTE')
        converted=[]
        feed.enqueue_event=converted.append
        with patch('bomber.framework.market.stream.dolphin.feed.time.time_ns',return_value=ns('2026-10-29 10:56:02')):
            feed.on_stream_row(spec,self.state(volume=2))
            feed.on_stream_row(spec,self.state(volume=5,second='59'))
            self.assertFalse(converted)
            feed.on_stream_row(spec,self.state('10:56',volume=1))
        self.assertEqual(len(converted),1)
        event=converted[0]
        self.assertEqual(event.ts_event,ns('2026-10-29 10:55'))
        self.assertEqual(event.factors['turnover'],5000)
        wrapper=FuturesMinuteBarFeed('complete',Mock(),config=MarketSourceConfig(source='dolphindb',
            table=spec.table_name,timestamp_label='start',volume_mode='per-bar',bar_mode='state'),calendar=calendar())
        wrapper.register_instrument(meta)
        wrapper.subscribe(instrument)
        result=[]
        wrapper._emit_custom_bar=result.append
        wrapper._dolphin(event,ns('2026-10-29 10:56:02'))
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].factors['volume'],5)
        self.assertEqual(result[0].factors['value'],5000)
        self.assertEqual(result[0].ts_event,ns('2026-10-29 10:56')-1)
        self.assertEqual(feed.manifest['conversion_errors'],0)

    def test_native_subscription_requires_barTime_and_existing_ctp_identity_is_unchanged(self):
        from bomber.framework.market.basic.base import DataType,InstrumentId,InstrumentMeta
        from bomber.framework.market.stream.dolphin import DolphinDbConfig,DolphinDbStreamSpec,DolphinDbLiveDataFeed
        from bomber.framework.market.sources import MarketSourceConfig
        spec=DolphinDbStreamSpec.bar('state',auto_schema=True,bar_mode='state')
        driver=NS(start=Mock(),subscribe=Mock(),stop=Mock(),resolved_columns={})
        feed=DolphinDbLiveDataFeed(DolphinDbConfig('fake',8848,'u','p',streams=(spec,)),driver_factory=lambda:driver)
        instrument=InstrumentId.from_str('rb2701.SHFE')
        feed.register_instrument(InstrumentMeta(instrument,price_precision=0))
        feed.subscribe(instrument,DataType.BAR,'1-MINUTE')
        feed.connect()
        try:
            fields=driver.subscribe.call_args.args[0].required_columns
            self.assertIn('barTime',fields)
            self.assertNotIn('minTime',fields)
        finally:feed.disconnect()
        self.assertNotIn('bar_mode',MarketSourceConfig().identity)
        with self.assertRaises(ValueError):MarketSourceConfig(source='ctp',bar_mode='state')


if __name__=='__main__':unittest.main()
