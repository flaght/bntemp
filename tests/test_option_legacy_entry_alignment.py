"""旧选约时点／无手数上限对齐，保留实际持仓及显式零目标。"""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from importlib import import_module
from types import SimpleNamespace as NS
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import pandas as pd

from tests import test_demo_option_vega as fixture
from tests import test_option_fill_hedge as fill_fixture

signal=import_module('demos.08_option_vega.vega_signal')
backtest=import_module('demos.08_option_vega.run_backtest')


class LegacyEntryAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 仅替换本机缺失的SDK行情类型，沿用真实模板、配置和风控契约。
        fill_fixture.FillHedgeTests.setUpClass.__func__(cls)

    def setUp(self):
        self.f=fixture.StrategyTests();self.f.setUp()
        self.s,self.ctx=self.f.strategy,self.f.ctx

    def config(self,**kwargs):
        return signal.OptionVegaConfig(entry_quote_policy='latest',
            index_signal_minutes=5,max_option_lots=0,max_total_option_lots=0,max_future_lots=0,**kwargs)

    def test_zero_disables_only_quantity_limits_and_negative_remains_invalid(self):
        cfg=self.config()
        limits=signal.risk_limits(cfg,self.s.contracts,self.s.instruments,
            {instrument:D(100 if key in self.s.contracts else 200)for key,instrument in self.s.instruments.items()},D('1000000000'))
        for limit in limits.values():
            self.assertIsNone(limit.max_abs_position)
            self.assertIsNone(limit.max_order_quantity)
            self.assertEqual(limit.max_abs_position_notional,D('1000000000'))
            self.assertEqual(limit.max_order_notional,D('1000000000'))
            self.assertEqual(limit.max_market_age_ns,300_000_000_000)
        for field in ['max_option_lots','max_total_option_lots','max_future_lots']:
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'非负整数'):
                replace(cfg,**{field:-1})

    def test_same_feature_file_can_select_original_close_instead_of_blend(self):
        example=import_module('demos.08_option_vega.feature.example_dataframe')
        features=import_module('demos.08_option_vega.feature')
        with TemporaryDirectory()as directory:
            report=example.run_example(Path(directory))
            args=NS(index_feature_file=Path(report['output']),index_feature_config=Path(report['dataset_config']),
                index_dataset=report['dataset_id'],index_schema_version=1,index_code='000852',index_value_field='close')
            _,_,records,identity=features.read_index_features(args,0,2**63-1)
            self.assertEqual(identity['value_field'],'close')
            last=records[-1]
            self.assertNotEqual(last.payload.close,last.payload.index_blend)
            self.assertEqual(features._index_price(last,'close'),D(str(last.payload.close)))

    def test_latest_accepts_last_completed_m5_but_not_future_or_stale_quotes(self):
        stamp=fixture.ns('2022-07-25 13:58')
        old=fixture.ns('2022-07-25 13:55')
        self.assertTrue(signal.entry_quote_times_valid([old,stamp,stamp],stamp,'latest',300))
        self.assertFalse(signal.entry_quote_times_valid([old,stamp,stamp],stamp,'synchronized',300))
        self.assertFalse(signal.entry_quote_times_valid([stamp+120_000_000_000,stamp,stamp],stamp,'latest',300))
        self.assertFalse(signal.entry_quote_times_valid([stamp-301_000_000_000,stamp,stamp],stamp,'latest',300))
        self.assertFalse(signal.entry_quote_times_valid([None,stamp,stamp],stamp,'latest',300))

    def test_index_m5_close_is_separate_from_m1_clock_and_later_prices(self):
        self.s.config=self.config();self.s.options_by_day={}
        def frame(clock,price):
            stamp=fixture.ns('2022-07-25 '+clock)
            self.s.on_market_frame(NS(valuation_ns=stamp,request_ns=stamp+5_000_000_000,
                observations={'000852':NS(price=D(str(price)))}))
        frame('13:55',6937.39)
        frame('13:58',8000)
        stamp=fixture.ns('2022-07-25 13:58')
        self.assertEqual(self.s._entry_index(stamp),(6937.39,fixture.ns('2022-07-25 13:55')))
        self.assertEqual(self.s._price('000852',stamp),8000)
        frame('14:00',6941.23)
        self.assertEqual(self.s._entry_index(fixture.ns('2022-07-25 14:00'))[0],6941.23)

    def test_backtest_preflight_and_strategy_agree_for_1358_m5(self):
        cfg=self.config()
        times=lambda values:pd.to_datetime(['2022-07-25 '+v for v in values]).tz_localize('Asia/Shanghai')
        index=set(times(['13:55','13:58']))
        self.assertTrue(backtest.entry_quotes_available(cfg,date(2022,7,25),index,
            times(['13:55','14:00']),times(['13:57','13:58','13:59'])))
        self.assertFalse(backtest.entry_quotes_available(replace(cfg,entry_quote_policy='synchronized'),date(2022,7,25),index,
            times(['13:55','14:00']),times(['13:58'])))
        self.assertFalse(backtest.entry_quotes_available(cfg,date(2022,7,25),index,
            times(['14:00']),times(['13:58'])))
        self.assertFalse(backtest.entry_quotes_available(cfg,date(2022,7,25),index,
            times(['13:50']),times(['13:58'])))
        self.assertFalse(backtest.entry_quotes_available(cfg,date(2022,7,25),set(),
            times(['13:55']),times(['13:58'])))
        self.assertFalse(backtest.entry_quotes_available(cfg,date(2022,7,25),index,[],times(['13:58'])))

    def test_m5_clock_requires_explicit_latest_policy_for_1358(self):
        args=NS(option_bar_minutes=5,option_slot='13:58',bar_timestamp='end',options_bar_timestamp='end',
            option_ohlc_policy='expand_range',entry_quote_policy='latest')
        self.assertEqual(backtest.backtest_bar_specs(args)['option'].interval_seconds,300)
        args.entry_quote_policy='synchronized'
        with self.assertRaisesRegex(ValueError,'entry-quote-policy latest'):
            backtest.backtest_bar_specs(args)

    def test_1358_quote_snapshot_reproduces_old_actual_selection(self):
        prices={7000:110.8,7100:78.2,7200:53.8,7300:35}
        contracts={f'MO2208-C-{k}':signal.Contract(f'MO2208-C-{k}','IM2208',k,D(100),
            date(2022,6,1),date(2022,8,19),date(2022,8,19),D('.2'))for k in prices}
        ids={k:k+'.CFFEX'for k in [*contracts,'IM2208']}
        days=tuple(d.date()for d in pd.bdate_range('2022-07-25','2022-08-19'))
        s=self.f.Strategy('legacy-entry-test',self.config(),contracts,
            {'IM2208':{'contMultNum':D(200)}},ids,'000852',days,
            options_by_day={date(2022,7,25):tuple(contracts)})
        ctx=fixture.Context();s._bind(ctx);s._start()
        stamp=fixture.ns('2022-07-25 13:58');old=fixture.ns('2022-07-25 13:55')
        s.latest={'000852':(stamp,8000),'IM2208':(stamp,6861.2),
            **{f'MO2208-C-{k}':(old,p)for k,p in prices.items()}}
        s.entry_index_latest=(old,6937.39)
        s._frame(stamp,stamp+5_000_000_000)
        self.assertEqual({k:int(q)for k,q in s.option_goals.items()if q},
            {'MO2208-C-7000':-2,'MO2208-C-7100':-2,'MO2208-C-7200':-4})
        self.assertEqual(s.future_goals['IM2208'],0)  # 仍不把未成交目标当成实际仓位。
        self.assertTrue(ctx.intents)

    def test_uncapped_daily_allocation_matches_old_formula_above_200_lots(self):
        from tests.test_option_source_logic import original
        old,_=original();old._nav=4000000000
        prices=[];candidates=[]
        for k in [6600,6700,6800,6900]:
            key=f'MO2609-C-{k}';p=signal.call_price(6500,k,26/252,.02,.25)
            prices.append(dict(Code=key,strike=k,closePrice=p,futClosePrice=6500,
                Shibor=.02,lastTradingDate=pd.Timestamp('2026-09-18'),**{'剩余交易日':26}))
            candidates.append((key,signal.source_greeks(6500,k,26/252,.02,.25,36/365),D(100)))
        import contextlib,io
        with contextlib.redirect_stdout(io.StringIO()):expected=old._calc_option_positions(pd.DataFrame(prices),'2026-08-13')
        expected={k:int(q)for k,q in expected.items()if q}
        actual=signal.allocate_short(candidates,old.DELTA_TARGETS,50000000,0,0)
        self.assertEqual(actual,expected)
        self.assertGreater(sum(map(abs,actual.values())),200)

    def test_uncapped_cumulative_position_can_grow_and_survives_checkpoint_restore(self):
        self.s.config=self.config();key=self.f.info.symbol;stamp=fixture.ns('2026-08-13 13:58')
        self.s.option_goals[key]=D(-250);self.ctx.actual[self.f.ids[key]]=D(-250)
        self.s.latest={'000852':(stamp,6400),'IM2609':(stamp,6500),key:(stamp-180_000_000_000,100)}
        self.s.entry_index_latest=(stamp-180_000_000_000,6400)
        self.s._frame(stamp,stamp+5_000_000_000)
        self.assertEqual(self.s.entry_audit[-1]['outcome'],'TARGET')
        self.assertLess(self.s.option_goals[key],-250)
        checkpoint=signal.OptionVegaCheckpoint(self.s);state=checkpoint.snapshot_state()
        checkpoint.restore_state(state)
        self.assertLess(self.s.option_goals[key],-250)
        self.assertIsNone(self.s.entry_index_latest)

    def test_future_target_above_100_is_not_clipped_or_blocked(self):
        self.s.config=self.config();key=self.f.info.symbol
        stamp=fixture.ns('2026-08-13 10:00');now=pd.Timestamp(stamp,tz='UTC').tz_convert('Asia/Shanghai')
        self.s._greeks=lambda *args,**kwargs:NS(delta=.5)
        goals,_=self.s._future_targets({key:D(-500),'IM2609':D(0)},stamp,now,set(),set())
        self.assertEqual(goals['IM2609'],125)

    def test_zero_target_close_remains_explicit_and_does_not_pretend_fill(self):
        self.s.config=self.config(min_time_value=0);key=self.f.info.symbol
        self.s.option_goals[key]=D(-2);self.ctx.actual[self.f.ids[key]]=D(-2)
        self.s.future_goals['IM2609']=D(1);self.ctx.actual[self.f.ids['IM2609']]=D(1)
        stamp=fixture.ns('2026-08-13 13:58')
        self.s.latest={'000852':(stamp,6400),'IM2609':(stamp,6700),key:(stamp-180_000_000_000,1)}
        self.s.entry_index_latest=(stamp-180_000_000_000,6400)
        self.s._frame(stamp,stamp+5_000_000_000)
        self.assertEqual(self.ctx.intents[-1].targets[self.f.ids[key]],0)
        self.assertEqual(self.s.position(self.f.ids[key]),-2)
        self.assertEqual(self.ctx.intents[-1].targets[self.f.ids['IM2609']],1)

if __name__=='__main__':unittest.main()
