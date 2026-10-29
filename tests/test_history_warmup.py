"""公共单路历史预热：覆盖/来源失败/归档隔离/合并和历史实时数值一致性。"""
from contextlib import redirect_stdout
from dataclasses import replace
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest

from tests import run_live_history_offline as fixtures
from tests.test_demo_hybrid_live import calendar, ns
from tests.test_dolphindb_connections import SPLIT
from bomber.framework.dataprep.history import (
    HistoryBar, HistoryError, HistoryUnavailable, HistoryService, build_history_service)
from bomber.framework.dataprep.history.futures import futures_history_values
from bomber.framework.market.futures import futures_values, futures_bar_values
from bomber.framework.trader.runtime.live.history import warm_history_window, merge_history_rows

ROOT=Path(__file__).resolve().parents[1]


class HistoryWarmupTests(unittest.TestCase):
    def setUp(self):
        self.calendar=calendar()
        self.end=ns('2026-10-28 09:03')
        self.stamps=self.calendar.last_minutes(self.end,3)
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'history.json'
        self.values=json.loads((ROOT/'demos/10_hybrid_transformer/history_rb.json').read_text())
        self.path.write_text(json.dumps(self.values))

    def point(self, stamp=None, **values):
        return HistoryBar('rb2701.SHFE',self.stamps[-1] if stamp is None else stamp,
            **dict(dict(open=100,high=101,low=99,close=100,volume=2,value=2000,open_interest=500),**values))

    def service(self, policy='allow', **kwargs):
        with patch.dict('os.environ',SPLIT,clear=True):
            return build_history_service(history_config=self.path,calendar=self.calendar,
                missing_policy=policy,**kwargs)

    def load(self, service, consume, **kwargs):
        return warm_history_window(service,calendar=self.calendar,instrument='rb2701.SHFE',
            before_ns=self.end,minutes=3,consume=consume,**kwargs)

    def test_config_builder_is_offline_uses_market_endpoint_and_checks_declared_fields(self):
        with patch('bomber.framework.dataprep.history.dolphindb._session_factory') as sdk:
            service=self.service(required_fields=('value','open_interest'))
        sdk.assert_not_called()
        self.assertEqual(service.config.connection.host,'market')
        self.assertEqual(service.config.connection.port,8850)
        self.assertIs(service.config.session_calendar,self.calendar)
        self.assertTrue(service.config.allow_missing_minutes)
        self.values['columns'].pop('open_interest');self.path.write_text(json.dumps(self.values))
        with self.assertRaisesRegex(HistoryError,'open_interest'):
            self.service(required_fields=('open_interest',))

    def test_defaults_remain_strict_and_json_can_explicitly_keep_strict_counter_gaps(self):
        service=self.service(policy='fail')
        self.assertFalse(service.config.allow_missing_minutes)
        self.values['allow_missing_minutes']=False;self.path.write_text(json.dumps(self.values))
        self.assertFalse(self.service().config.allow_missing_minutes)
        self.assertEqual(build_history_service().missing_policy,'fail')

    def test_invalid_json_shape_and_missing_calendar_fail_before_connection(self):
        self.path.write_text('[]')
        with self.assertRaisesRegex(HistoryError,'JSON对象'):self.service()
        self.path.write_text(json.dumps(self.values))
        with self.assertRaisesRegex(HistoryError,'会话日历'):
            build_history_service(history_config=self.path)

    def test_empty_and_partial_success_keep_exact_coverage_and_only_call_consumer(self):
        for count in (0,2,3):
            with self.subTest(count=count):
                service=self.service();consumer=Mock();provider=Mock()
                provider.read.return_value=tuple(self.point(s) for s in self.stamps[-count:]) if count else ()
                with patch('bomber.framework.dataprep.history.service.HistoryProviderFactory.create_for',
                        return_value=provider),redirect_stdout(StringIO()) as output:
                    result=self.load(service,consumer)
                consumer.assert_called_once_with(result.bars)
                self.assertEqual(service.last_audit['loaded'],count)
                self.assertEqual(service.last_audit['missing'],3-count)
                self.assertIn(f'requested=3 loaded={count} missing={3-count}',output.getvalue())
                self.assertNotIn('market-secret',output.getvalue())
                self.assertNotIn('market-user',output.getvalue())
                provider.close.assert_called_once()

    def test_provider_outage_is_fatal_even_when_missing_is_allowed(self):
        service=self.service();consumer=Mock();provider=Mock()
        provider.read.side_effect=HistoryUnavailable('connection failed')
        with patch('bomber.framework.dataprep.history.service.HistoryProviderFactory.create_for',
                return_value=provider),redirect_stdout(StringIO()) as output:
            with self.assertRaisesRegex(HistoryUnavailable,'历史行情源读取失败'):
                self.load(service,consumer)
        consumer.assert_not_called();provider.close.assert_called_once()
        self.assertEqual(service.last_audit['unavailable_sources'],('dolphindb',))
        self.assertIn('历史读取审计',output.getvalue())

    def test_consumer_validation_failure_keeps_original_exception_and_audit(self):
        service=self.service();consumer=Mock(side_effect=HistoryError('invalid raw OHLC'))
        provider=Mock();provider.read.return_value=tuple(self.point(s) for s in self.stamps)
        with patch('bomber.framework.dataprep.history.service.HistoryProviderFactory.create_for',
                return_value=provider),redirect_stdout(StringIO()) as output:
            with self.assertRaisesRegex(HistoryError,'invalid raw OHLC'):self.load(service,consumer)
        self.assertEqual(service.last_audit['loaded'],3)
        self.assertIn('预热失败',output.getvalue());provider.close.assert_called_once()

    def test_zero_history_does_not_read_or_mutate_window(self):
        service=Mock();consumer=Mock()
        with redirect_stdout(StringIO()):
            result=warm_history_window(service,calendar=self.calendar,instrument='rb2701.SHFE',
                before_ns=self.end,minutes=0,consume=consumer)
        self.assertIsNone(result);service.load.assert_not_called();consumer.assert_not_called()

    def test_archive_reports_entire_window_and_filters_old_future_rows(self):
        service=HistoryService(missing_policy='allow');consumer=Mock()
        archive=NS(path=Path('fixture.jsonl'),recorded_bars=3,recent=Mock(return_value=(
            self.point(self.stamps[0]-60_000_000_000),self.point(self.stamps[1]),self.point(self.end+59_999_999_999))))
        with redirect_stdout(StringIO()):result=self.load(service,consumer,archive=archive)
        self.assertEqual(result.bars,(self.point(self.stamps[1]),))
        self.assertEqual(service.last_audit['required'],3)
        self.assertEqual(service.last_audit['missing'],2)
        self.assertEqual(len(service.audit_history),1)
        service.missing_policy='fail';consumer.reset_mock()
        with redirect_stdout(StringIO()),self.assertRaisesRegex(HistoryUnavailable,'缺少2根'):
            self.load(service,consumer,archive=archive)
        consumer.assert_not_called()

    def test_archive_cannot_feed_another_contract(self):
        service=HistoryService(missing_policy='allow');consumer=Mock()
        archive=NS(path=Path('wrong.jsonl'),recorded_bars=1,
            recent=lambda **kw:(replace(self.point(),instrument_id='rb2705.SHFE'),))
        with redirect_stdout(StringIO()),self.assertRaisesRegex(HistoryError,'真实合约'):
            self.load(service,consumer,archive=archive)
        consumer.assert_not_called()

    def test_merge_is_ordered_bounded_deduplicated_and_never_picks_a_conflicting_version(self):
        rows=[dict(end=s,value=i) for i,s in enumerate(self.stamps)]
        merged=merge_history_rows([rows[2],rows[0],rows[1],rows[1],{'end':self.end+59_999_999_999,'value':9}],
            calendar=self.calendar,before_ns=self.end,minutes=3,event_ns=lambda r:r['end'])
        self.assertEqual(merged,tuple(rows))
        with self.assertRaisesRegex(HistoryError,'内容冲突'):
            merge_history_rows([rows[0],dict(rows[0],value=99)],calendar=self.calendar,
                before_ns=self.end,minutes=3,event_ns=lambda r:r['end'])
        with self.assertRaisesRegex(HistoryError,'完整分钟'):
            merge_history_rows([{'end':self.end-2}],calendar=self.calendar,
                before_ns=self.end,minutes=3,event_ns=lambda r:r['end'])

    def test_historical_and_live_futures_use_the_same_raw_numeric_and_vwap_rules(self):
        bar=self.point(value=2000.5)
        raw={field:getattr(bar,field) for field in ('open','high','low','close','volume','value','open_interest')}
        historical=futures_history_values(bar,calendar=self.calendar,contract_multiplier=10)
        realtime=futures_bar_values(NS(factors=raw,bar=None),contract_multiplier=10)
        self.assertEqual(historical,realtime)
        self.assertEqual(historical['vwap'],100.025)
        for bad in (dict(raw,volume=0,value=0),dict(raw,value=3000)):
            invalid=self.point(**{k:bad[k] for k in ('volume','value')})
            with self.assertRaises(HistoryError):
                futures_history_values(invalid,calendar=self.calendar,contract_multiplier=10)
            with self.assertRaises(ValueError):futures_values(bad,contract_multiplier=10)
        with self.assertRaisesRegex(HistoryError,'复权'):
            futures_history_values(replace(bar,adjusted_close=100),calendar=self.calendar,contract_multiplier=10)


if __name__=='__main__':unittest.main()
