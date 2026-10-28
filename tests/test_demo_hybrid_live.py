"""Hybrid Live 公共分钟与装配契约；原生行情实体用替身，不连接柜台。"""
import ast
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum, auto
from importlib import import_module
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MINUTE = 60_000_000_000
ZONE = ZoneInfo('Asia/Shanghai')


def ns(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=ZONE).timestamp()) * 10**9


class DataType(Enum):
    BAR = auto()
    CUSTOM_BAR = auto()
    TRADE_TICK = auto()


class Value:
    def __init__(self, value): self.value = Decimal(str(value))
    def as_decimal(self): return self.value
    def as_double(self): return float(self.value)


class CustomBar:
    def __init__(self, bar, factors):
        self.bar, self.factors, self.ts_event = bar, factors, bar.ts_event


def make_bar(instrument_id, ts_event, ts_init=None, **values):
    return NS(ts_event=ts_event, ts_init=ts_init,
        bar_type=NS(instrument_id=instrument_id),
        **{k: Value(values[k]) for k in ('open', 'high', 'low', 'close', 'volume')})


class Feed:
    def __init__(self, source_id='stub'):
        self.source_id = source_id
        self._subscriptions, self._instruments = {}, {}
        self._is_connected = False
        self.events = []
        if type(self) is Feed:
            self.health_snapshot = NS()
    def register_instrument(self, meta): self._instruments[meta.instrument_id] = meta
    def get_instrument_meta(self, key): return self._instruments.get(key)
    def subscribe(self, instrument, data_type=None, bar_spec=None, fields=(), **kwargs):
        self._subscriptions.setdefault(instrument, []).append(data_type)
    def register_depth_handler(self, handler): self.depth = handler
    def register_custom_bar_handler(self, handler): self.custom = handler
    def register_health_handler(self, handler): pass
    def connect(self): self._is_connected = True
    def disconnect(self): self._is_connected = False
    def _emit_bar(self, bar): self.events.append(('bar', bar))
    def _emit_custom_bar(self, event): self.events.append(('custom', event))


def load_feed_module():
    stub = ModuleType('bomber.framework.market.basic.base')
    stub.__dict__.update(DataType=DataType, MarketDataFeed=Feed,
        make_bar=make_bar, make_custom_bar=CustomBar)
    spec = spec_from_file_location('hybrid_futures_minute_fixture',
        ROOT/'bomber/framework/market/stream/futures_minute.py')
    module = module_from_spec(spec)
    with patch.dict('sys.modules', {stub.__name__: stub}):
        spec.loader.exec_module(module)
    return module


def calendar():
    from bomber.framework.calendars import (
        CalendarSnapshot, SessionBinding, SessionCalendar, SessionSegment, TradingHoursSnapshot)
    days = (date(2026,10,22), date(2026,10,23), date(2026,10,26), date(2026,10,27), date(2026,10,28), date(2026,10,29))
    dates = CalendarSnapshot('fixture', 'v1', days[0], days[-1], days, 'test')
    rows = [('RB', SessionSegment('am', 'TRADING_DAY', 0, '09:00', 0, '11:30')),
            ('RB', SessionSegment('pm', 'TRADING_DAY', 0, '13:30', 0, '15:00')),
            ('RB', SessionSegment('night', 'NIGHT_START', 0, '21:00', 1, '02:30'))]
    return SessionCalendar(TradingHoursSnapshot(tuple(rows), 'fixture'), dates,
        SessionBinding('RB', 'RB', 'fixture', 'Asia/Shanghai'))


class FuturesMinuteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.module = load_feed_module()

    def feed(self, source='ctp', mode='per-bar', label='end', now='2026-10-28 09:00:00'):
        self.now = ns(now)
        upstream = Feed()
        config = NS(source=source, volume_mode=mode, timestamp_label=label, max_age_seconds=120)
        feed = self.module.FuturesMinuteBarFeed('hybrid', upstream,
            config=config, calendar=calendar(), clock_ns=lambda:self.now)
        feed.register_instrument(NS(instrument_id='rb2701.SHFE', multiplier=Decimal(10)))
        feed.subscribe('rb2701.SHFE')
        feed.connect()
        self.feed_instance, self.upstream = feed, upstream
        return feed

    def depth(self, time, volume, amount, price=100, interest=500, day='20261028'):
        self.upstream.depth('rb2701.SHFE', ns(time), dict(Volume=volume, Turnover=amount,
            LastPrice=price, OpenInterest=interest, TradingDay=day))

    def bar(self, time, volume=2, amount=2000, price=100, **extra):
        bar=make_bar('rb2701.SHFE', ns(time), open=price,high=price,low=price,close=price,volume=volume)
        fields=dict(value=amount,open_interest=500, **extra)
        return CustomBar(bar,fields)

    def test_ctp_raw_amount_and_interest_and_bar_before_custom(self):
        feed=self.feed()
        self.depth('2026-10-28 09:00:59',10,10000)
        self.depth('2026-10-28 09:01:10',12,12000)
        self.depth('2026-10-28 09:01:50',15,15000,interest=510)
        self.now=ns('2026-10-28 09:02:02')
        feed.poll()
        self.assertEqual([kind for kind,_ in feed.events],['bar','custom'])
        event=feed.events[-1][1]
        self.assertEqual(event.ts_event,ns('2026-10-28 09:02:00')-1)
        self.assertEqual(event.factors['value'],5000)
        self.assertEqual(event.factors['volume'],5)
        self.assertEqual(event.factors['open_interest'],510)
        self.assertEqual(event.factors['trade_time'].strftime('%H:%M'),'09:01')

    def test_ctp_first_partial_minute_is_never_emitted(self):
        feed=self.feed(now='2026-10-28 09:01:00')
        self.depth('2026-10-28 09:01:05',10,10000)
        self.depth('2026-10-28 09:01:40',15,15000)
        self.now=ns('2026-10-28 09:02:02')
        feed.poll()
        self.assertFalse(feed.events)

    def test_ctp_exact_session_end_is_merged_once(self):
        feed=self.feed(now='2026-10-28 11:28:00')
        self.depth('2026-10-28 11:28:59',10,10000)
        self.depth('2026-10-28 11:29:20',12,12000)
        self.depth('2026-10-28 11:30:00',15,15000)
        self.now=ns('2026-10-28 11:30:02')
        feed.poll()
        event=feed.events[-1][1]
        self.assertEqual(event.ts_event,ns('2026-10-28 11:30:00')-1)
        self.assertEqual(event.factors['volume'],5)
        feed.poll()
        self.assertEqual(len(feed.events),2)

    def test_ctp_rollback_and_unmatched_amount_fail(self):
        for volume,amount in ((9,9000),(10,11000)):
            feed=self.feed()
            self.depth('2026-10-28 09:00:59',10,10000)
            self.depth('2026-10-28 09:01:10',volume,amount)
            with self.assertRaises(ValueError): feed.poll()
            self.assertIsNotNone(feed.failure)

    def test_ctp_no_fictional_volume_across_missing_minutes(self):
        feed=self.feed()
        self.depth('2026-10-28 09:01:00',10,10000)
        self.depth('2026-10-28 09:04:00',20,20000)
        with self.assertRaisesRegex(ValueError,'缺口'): feed.poll()

    def test_dolphin_per_bar_and_duplicate_and_off_session(self):
        feed=self.feed(source='dolphindb')
        self.now=ns('2026-10-28 09:02:02')
        event=self.bar('2026-10-28 09:02:00')
        self.upstream.custom(event); self.upstream.custom(event)
        feed.poll()
        self.assertEqual(len(feed.events),2)
        self.now=ns('2026-10-28 12:00:02')
        self.upstream.custom(self.bar('2026-10-28 12:00:00'))
        feed.poll()
        self.assertEqual(len(feed.events),2)

    def test_dolphin_start_label_and_raw_price_precision(self):
        feed=self.feed(source='dolphindb',label='start')
        self.now=ns('2026-10-28 09:03:00')
        event=self.bar('2026-10-28 09:01:00',amount=2000.5)
        event.factors.update(open=100.025,high=100.025,low=100.025,close=100.025)
        self.upstream.custom(event);feed.poll()
        self.assertEqual(feed.events[-1][1].factors['open'],100.025)
        self.assertEqual(feed.events[-1][1].ts_event,ns('2026-10-28 09:02:00')-1)

    def test_dolphin_cumulative_amount_and_volume_across_midnight(self):
        feed=self.feed(source='dolphindb',mode='cumulative',now='2026-10-27 23:58:00')
        self.now=ns('2026-10-28 00:01:02')
        self.upstream.custom(self.bar('2026-10-27 23:59:00',volume=0,amount=10000,source_volume=10))
        self.upstream.custom(self.bar('2026-10-28 00:01:00',volume=0,amount=15000,source_volume=15))
        feed.poll()
        self.assertEqual(feed.events[-1][1].factors['volume'],5)
        self.assertEqual(feed.events[-1][1].factors['value'],5000)

    def test_dolphin_missing_amount_fails_and_future_bar_fails(self):
        for event in (self.bar('2026-10-28 09:02:00'),self.bar('2026-10-28 09:05:00')):
            feed=self.feed(source='dolphindb')
            self.now=ns('2026-10-28 09:02:02')
            if event.ts_event==ns('2026-10-28 09:02:00'): event.factors.pop('value')
            self.upstream.custom(event)
            with self.assertRaises(ValueError): feed.poll()


    def test_ctp_preopen_invalid_last_price_is_not_a_model_bar(self):
        feed=self.feed(now='2026-10-28 08:58:00')
        self.depth('2026-10-28 08:58:10',0,0,price='1.7976931348623157e308')
        self.depth('2026-10-28 08:58:20',0,0,price='1.7976931348623157e308')
        feed.poll()
        self.assertFalse(feed.events)
        self.assertIsNone(feed.failure)

    def test_dolphin_outside_cumulative_tail_is_not_counted_again(self):
        feed=self.feed(source='dolphindb',mode='cumulative')
        self.now=ns('2026-10-28 13:31:02')
        self.upstream.custom(self.bar('2026-10-28 11:29:00',volume=0,amount=10000,source_volume=10))
        self.upstream.custom(self.bar('2026-10-28 11:31:00',volume=0,amount=12000,source_volume=12))
        self.upstream.custom(self.bar('2026-10-28 13:31:00',volume=0,amount=15000,source_volume=15))
        feed.poll()
        self.assertEqual(feed.events[-1][1].factors['volume'],3)
        self.assertEqual(feed.events[-1][1].factors['value'],3000)


class WarmHistoryTests(unittest.TestCase):
    def test_file_history_uses_raw_values_without_historical_decisions(self):
        import json
        history=import_module('demos.10_hybrid_transformer.history')
        with TemporaryDirectory() as directory:
            path=Path(directory)/'bars.jsonl'
            rows=[dict(instrument_id='rb2701.SHFE',ts_event=ns(time)-1,open=100,high=100,
                       low=100,close=100,volume=2,value=2000,open_interest=500)
                  for time in ('2026-10-28 09:01:00','2026-10-28 09:02:00')]
            path.write_text('\n'.join(map(json.dumps,rows)))
            predictor=NS(warm_history=Mock(return_value=2),on_bar=Mock())
            runner=NS(calendar=calendar(),bound_instrument='rb2701.SHFE',
                _clock_ns=lambda:ns('2026-10-28 09:02:03'),
                strategy=NS(predictor=predictor,config=NS(contract_multiplier=Decimal(10))),
                history_service=history.build_history(history_file=path))
            history.warm_history(runner,product='RB',history_minutes=2)
            prepared=predictor.warm_history.call_args.args[0]
            self.assertEqual(prepared[0]['trade_time'].strftime('%H:%M'),'09:00')
            self.assertEqual(prepared[0]['vwap'],100)
            self.assertEqual(prepared[0]['openint'],500)
            predictor.on_bar.assert_not_called()
            self.assertEqual(runner.history_last_ns,rows[-1]['ts_event'])
            rows[0].pop('value')
            path.write_text('\n'.join(map(json.dumps,rows)))
            predictor.warm_history.reset_mock()
            with self.assertRaisesRegex(ValueError,'原始'):
                history.warm_history(runner,product='RB',history_minutes=2)
            predictor.warm_history.assert_not_called()


class LiveRunnerTests(unittest.TestCase):
    def runner_type(self):
        class Parent:
            def publish(self,feed,event): self.delivered.append(event)
            def continue_execution_target(self,*args,**kwargs): self.calls.append('continue')
        tree=ast.parse((ROOT/'demos/10_hybrid_transformer/run_live.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='HybridLiveRunner')
        namespace=dict(SessionRoleLiveRunner=Parent,CustomBar=CustomBar,MINUTE=MINUTE,
            warm_history=lambda *args,**kwargs:None,replace=replace,MODEL_BAR_TIMESTAMP='start')
        exec(compile(ast.Module(body=[node],type_ignores=[]),'run_live.py','exec'),namespace)
        return namespace['HybridLiveRunner']

    def test_clock_expiry_is_independent_of_new_prediction(self):
        runner=object.__new__(self.runner_type())
        runner.accept_bars=True;runner._submit_lock=RLock();runner._clock_ns=lambda:123
        runner.strategy=NS(on_time=Mock(),last_targets={})
        runner.orders=False
        runner.expire_due()
        runner.strategy.on_time.assert_called_once_with(123)

    def test_live_never_replays_history_orders(self):
        runner=object.__new__(self.runner_type())
        runner.history_last_ns=ns('2026-10-28 09:02:00')-1
        runner.delivered=[]
        runner.accept_bars=True
        runner.first_complete_bar_ns=runner.history_last_ns+MINUTE
        runner.publish('model-bars',CustomBar(make_bar('rb2701.SHFE',runner.history_last_ns,
            open=100,high=100,low=100,close=100,volume=2),{}))
        self.assertFalse(runner.delivered)


if __name__=='__main__': unittest.main()
