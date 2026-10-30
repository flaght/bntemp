"""历史不足保持订阅、实际加载计数和累计量额缺口的离线验证。"""
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import json
import unittest

from bomber.framework.dataprep.history import (
    HistoryBar, HistoryError, HistoryUnavailable, HistoryRequest, DolphinDbHistoryProvider)
from tests.test_demo_hybrid_live import calendar, ns
from tests import test_hybrid_history_source as legacy
from tests.test_dolphindb_connections import SPLIT

ROOT=Path(__file__).resolve().parents[1]
MINUTE=60_000_000_000


def point(stamp):
    return HistoryBar('rb2701.SHFE',stamp,open=100,high=100,low=100,close=100,
        volume=2,value=2000,open_interest=500)


class PartialHistoryTests(unittest.TestCase):
    def setUp(self):
        self.history=import_module('demos.10_hybrid_transformer.history')
        self.calendar=calendar()
        self.end=ns('2026-10-28 09:03:00')
        self.stamps=self.calendar.last_minutes(self.end,300)

    def service(self):
        with patch.dict('os.environ',SPLIT,clear=True):
            return self.history.build_history(history_config=ROOT/'demos/10_hybrid_transformer/history_rb.json',
                calendar=self.calendar)

    def runner(self, service):
        workflow=NS(history=[],min_raw_bars=300)
        def warm(rows):
            workflow.history=list(rows)
            return len(rows)
        predictor=NS(workflow=workflow,warm_history=Mock(side_effect=warm),on_bar=Mock())
        return NS(calendar=self.calendar,history_service=service,bound_instrument='rb2701.SHFE',
            _clock_ns=lambda:self.end+2_000_000_000,
            strategy=NS(predictor=predictor,config=NS(contract_multiplier=Decimal(10))))

    def test_zero_partial_and_full_history_all_report_actual_count_and_do_not_replay_signals(self):
        factory='bomber.framework.dataprep.history.service.HistoryProviderFactory.create_for'
        for count in (0,103,299,300):
            with self.subTest(count=count):
                service=self.service()
                self.assertEqual(service.missing_policy,'allow')
                self.assertTrue(service.config.allow_missing_minutes)
                provider=Mock()
                provider.read.return_value=tuple(point(s) for s in self.stamps[-count:]) if count else ()
                runner=self.runner(service)
                with patch(factory,return_value=provider),redirect_stdout(StringIO()) as output:
                    self.history.warm_history(runner,product='RB',history_minutes=300)
                self.assertEqual(len(runner.strategy.predictor.workflow.history),count)
                self.assertEqual(service.last_audit['loaded'],count)
                self.assertEqual(service.last_audit['missing'],300-count)
                self.assertIn(f'requested=300 loaded={count} missing={300-count}',output.getvalue())
                self.assertIn(f'warmup_remaining={300-count}',output.getvalue())
                if count<300:self.assertIn('Hybrid模型预热等待',output.getvalue())
                self.assertEqual(runner.history_last_ns,self.stamps[-1] if count else -1)
                runner.strategy.predictor.on_bar.assert_not_called()
                provider.close.assert_called_once()

    def test_query_failure_is_not_treated_as_an_empty_history_window(self):
        provider=Mock()
        provider.read.side_effect=HistoryUnavailable('query failed')
        runner=self.runner(self.service())
        with patch('bomber.framework.dataprep.history.service.HistoryProviderFactory.create_for',return_value=provider), \
                redirect_stdout(StringIO()),self.assertRaisesRegex(HistoryUnavailable,'历史行情源读取失败'):
            self.history.warm_history(runner,product='RB',history_minutes=300)
        runner.strategy.predictor.warm_history.assert_not_called()
        provider.close.assert_called_once()

    def test_invalid_raw_fields_still_fail_before_model_is_mutated(self):
        provider=Mock()
        provider.read.return_value=(replace(point(self.stamps[-1]),value=None),)
        runner=self.runner(self.service())
        with patch('bomber.framework.dataprep.history.service.HistoryProviderFactory.create_for',return_value=provider), \
                redirect_stdout(StringIO()),self.assertRaisesRegex(HistoryError,'原始'):
            self.history.warm_history(runner,product='RB',history_minutes=300)
        runner.strategy.predictor.warm_history.assert_not_called()

    def test_cumulative_gap_omits_noncomputable_minute_and_recovers_from_new_baseline(self):
        fixture=legacy.TradingHistoryTests('runTest')
        fixture.setUp()
        config=replace(fixture.config,allow_missing_minutes=True)
        provider=DolphinDbHistoryProvider(config)
        rows=[legacy.row('09:00:00.000',100),legacy.row('09:02:00.000',105),legacy.row('13:30:00.000',107)]
        request=HistoryRequest('rb2701.SHFE',ns('2026-10-19 09:00'),ns('2026-10-19 13:31'))
        result=provider._read_trading(rows,request)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].ts_event,ns('2026-10-19 13:31')-1)
        self.assertEqual(result[0].volume,2)
        rows[1]['volume']=99
        with self.assertRaisesRegex(HistoryError,'倒退'):
            provider._read_trading(rows,request)
        rows[1]['volume']=100
        with self.assertRaisesRegex(HistoryError,'不同步'):
            provider._read_trading(rows,request)

    def test_live_runner_waits_with_299_history_bars_and_predicts_only_after_next_complete_bar(self):
        self._assert_live_window_readiness(299, minimum=300)

    def test_260_requested_history_bars_still_require_40_live_bars_before_first_target(self):
        self._assert_live_window_readiness(260, minimum=300)

    def test_default_260_minimum_predicts_on_the_260th_raw_bar(self):
        self._assert_live_window_readiness(259)

    def test_260_history_bars_are_ready_but_do_not_replay_historical_signals(self):
        self._assert_live_window_readiness(260)

    def _assert_live_window_readiness(self, history_count, *, minimum=None):
        from tests import run_hybrid_live_offline as fixture
        live=fixture.live
        minimum=live.MODEL_MIN_RAW_BARS if minimum is None else minimum
        class WaitingPredictor(fixture.Predictor):
            def __init__(self,**kwargs):
                super().__init__(**kwargs)
                self.workflow.min_raw_bars=kwargs['min_raw_bars']
            def on_bar(self,row):
                self.workflow.history.append(dict(row))
                if len(self.workflow.history)<self.workflow.min_raw_bars:return None
                return dict(predicted_z=.1,signal=1)
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'release').mkdir()
            (root/'directions.csv').write_text('formula,direction\nf,1\n')
            signal=root/'signals.json'
            signal.write_text(json.dumps({'RB':dict(signal_id='1301',signal_method='threshold_signal',
                signal_params={'threshold':.02},hold_bars=5)}))
            instrument=fixture.InstrumentId.from_str('rb2701.SHFE')
            refs=NS(instrument_id=instrument,trading_day=date(2026,10,28),
                started_ns=ns('2026-10-28 09:00'),refresh_on_event=False,
                spec=NS(venue='SHFE',currency='CNY',tick=Decimal(1),multiplier=Decimal(10)),
                snapshot=lambda stamp:NS(instrument=lambda p,r:'rb2701',factor=lambda p,r:Decimal(1)))
            refs.instrument_meta=lambda:fixture.base.InstrumentMeta(instrument,price_precision=0,multiplier=Decimal(10))
            with patch.dict('os.environ',SPLIT,clear=True),patch.object(fixture.adapter,'MizarWorkflowAdapter',WaitingPredictor), \
                    patch.object(live,'MODEL_MIN_RAW_BARS',minimum):
                session=live.assemble(NS(references=refs,calendar=self.calendar,upstream=fixture.Upstream('fixture')),
                    NS(driver=None),product='RB',release_path=root/'release',directions_file=root/'directions.csv',
                    signal_config=signal,market_config=live.load_market_config(ROOT/'demos/10_hybrid_transformer/market_dolphindb_rb.json'),
                    history_minutes=260 if history_count==260 else 300,history_config=ROOT/'demos/10_hybrid_transformer/history_rb.json')
            now=[self.end+2_000_000_000]
            session.runner._clock_ns=lambda:now[0]
            session.runner._session_check=lambda:True
            session.runner.instrument_health=None
            provider=Mock()
            provider.read.return_value=tuple(point(s) for s in self.stamps[-history_count:])
            session.runner.start()
            try:
                with patch('bomber.framework.dataprep.history.service.HistoryProviderFactory.create_for',return_value=provider), \
                        redirect_stdout(StringIO()):
                    session.runner.begin_bars()
                self.assertTrue(session.runner.accept_bars)
                self.assertEqual(len(session.strategy.predictor.workflow.history),history_count)
                self.assertIsNone(session.strategy.last_prediction)
                self.assertFalse(session.client.requests)
                self.assertFalse(session.strategy.holdings.active)
                required=max(1,minimum-history_count)
                for index in range(required):
                    now[0]=ns('2026-10-28 09:05:02')+index*MINUTE
                    stamp=ns('2026-10-28 09:05')+index*MINUTE-1
                    bar=fixture.base.make_bar(instrument_id=instrument,meta=refs.instrument_meta(),ts_event=stamp,
                        ts_init=now[0],open=100,high=100,low=100,close=100,volume=2)
                    from bomber.framework.calendars.sessions import ns_datetime
                    event=fixture.base.make_custom_bar(bar,dict(open=100,high=100,low=100,close=100,
                        volume=2,value=2000,open_interest=500,
                        trade_time=ns_datetime(stamp+1-MINUTE,self.calendar.zone)))
                    session.runner.publish('model-bars',event)
                    if index<required-1:
                        self.assertIsNone(session.strategy.last_prediction)
                        self.assertFalse(session.client.requests)
                        self.assertFalse(session.strategy.holdings.active)
                self.assertEqual(session.strategy.predictor.workflow.min_raw_bars,minimum)
                self.assertEqual(len(session.strategy.predictor.workflow.history),history_count+required)
                self.assertEqual(session.strategy.bars_used,required)
                self.assertEqual(len(session.strategy.holdings.active),1)
                self.assertEqual(dict(session.client.requests[-1].logical_targets),{'rb_main':Decimal(1)})
            finally:session.runner.stop()


if __name__=='__main__':unittest.main()
