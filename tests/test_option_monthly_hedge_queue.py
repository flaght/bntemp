"""Monthly scheduled hedge queue, authoritative fill positions and static-only optimizations."""
from dataclasses import replace
from decimal import Decimal as D
from importlib import import_module,util
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
from pathlib import Path
import json,unittest
from tests import test_demo_option_vega as f
from tests import test_option_fill_hedge as fills

class MonthlyQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fills.FillHedgeTests.setUpClass.__func__(cls)
    setUp=fills.FillHedgeTests.setUp
    fill=fills.FillHedgeTests.fill
    def add_month(self):
        other=replace(self.f.info,symbol='MO2610-C-6700',future='IM2610')
        self.s.contracts[other.symbol]=other;self.s.futures[other.future]={'contMultNum':D(200)}
        self.s.instruments.update({other.symbol:other.symbol+'.CFFEX',other.future:other.future+'.CFFEX'})
        self.s.option_goals[other.symbol]=D(-20);self.s.future_goals[other.future]=D(7)
        self.ctx.actual[other.symbol+'.CFFEX']=D(-20);self.ctx.actual[other.future+'.CFFEX']=D(7)
        return other

    def due(self,clock='2026-08-13 14:00'):
        stamp=f.ns(clock);self.s._frame(stamp,stamp+5_000_000_001);return stamp

    def prepare_block(self):
        self.ctx.working[self.oid]=D(0);self.ctx.actual[self.oid]=D(-20)
        self.ctx.working[self.fid]=D(2);self.s.future_goals[self.future]=D(2)

    def test_one_month_inflight_does_not_block_another_month(self):
        other=self.add_month();self.prepare_block();self.s._greeks.return_value=NS(delta=.9)
        self.due()
        self.assertEqual(self.s.future_goals[self.future],2)
        self.assertEqual(self.s.future_goals[other.future],9)
        self.assertEqual(set(self.s._pending_scheduled_hedges),{self.future})
        self.assertEqual(self.s.hedge_sent,{'2026-08-13_14:00'})
        planner=import_module('bomber.framework.trader.execution.planner').NetTargetOrderPlanner(
            NS(effective_position=lambda client,key:self.ctx.position(key)+self.ctx.strategy_working_quantity(key)))
        request=import_module('bomber.framework.trader.contracts').ExecutionRequest
        intent=self.ctx.intents[-1]
        orders=planner.plan(request(self.s.strategy_id,intent.revision,'model',intent.ts_event,intent.targets,'DIRECT'))
        self.assertEqual([(r.instrument_id,r.quantity)for r in orders],[(other.future+'.CFFEX',D(2))])

    def test_partial_future_fill_keeps_task_final_fill_recalculates_current_actual(self):
        self.prepare_block();self.due();self.s._greeks.reset_mock()
        self.fill(self.future,'BUY',1,'2026-08-13 14:01')
        self.s._greeks.assert_not_called();self.assertIn(self.future,self.s._pending_scheduled_hedges)
        self.s._greeks.return_value=NS(delta=.8)
        self.fill(self.future,'BUY',1,'2026-08-13 14:02')
        self.assertEqual(self.s.future_goals[self.future],8)
        self.assertFalse(self.s._pending_scheduled_hedges)
        self.assertEqual(self.ctx.intents[-1].ts_event,f.ns('2026-08-13 14:02'))
        self.assertEqual(self.ctx.intents[-1].metadata['action'],'SCHEDULED_HEDGE_RETRY')
        self.assertEqual(self.s._greeks.call_args.args[1],f.ns('2026-08-13 14:02'))

    def test_previous_month_49_to_39_is_not_blocked_by_new_month_order(self):
        other=self.add_month();self.ctx.working[self.oid]=D(0);self.ctx.actual[self.oid]=D(-200)
        self.ctx.actual[self.fid]=D(49);self.s.future_goals[self.future]=D(49)
        self.ctx.actual[other.future+'.CFFEX']=D(0);self.ctx.working[other.future+'.CFFEX']=D(1)
        self.s.future_goals[other.future]=D(1)
        self.s._greeks.side_effect=lambda key,*args,**kw:NS(delta=.39 if key==self.option else .2)
        self.due()
        self.assertEqual(self.ctx.intents[-1].targets[self.fid],39)
        self.assertEqual(self.ctx.intents[-1].targets[other.future+'.CFFEX'],1)
        self.assertEqual(set(self.s._pending_scheduled_hedges),{other.future})

    def test_option_close_and_opposing_inflight_still_emit_zero_after_actual_fill(self):
        self.prepare_block();self.ctx.actual[self.oid]=D(-8);self.ctx.actual[self.fid]=D(1)
        self.ctx.working[self.fid]=D(1);self.s.future_goals[self.future]=D(2)
        self.due();self.s.option_goals[self.option]=D(0);self.ctx.working[self.oid]=D(8)
        self.fill(self.option,'BUY',8,'2026-08-13 14:00')
        self.assertEqual(self.s._fill_hedge_targets[self.future],0)
        self.assertIn(self.future,self.s._pending_scheduled_hedges)
        self.fill(self.future,'BUY',1,'2026-08-13 14:01')
        self.assertEqual(self.ctx.intents[-1].targets[self.fid],0)
        self.assertEqual(self.ctx.intents[-1].targets[self.oid],0)
        self.assertEqual(self.ctx.actual[self.fid],2) # 等真实卖平成交，不能写actual=0。
        self.assertFalse(self.s._pending_scheduled_hedges);self.assertFalse(self.s._fill_hedge_targets)

    def test_multiple_waiting_slots_coalesce_with_original_tokens_and_no_duplicate_orders(self):
        self.prepare_block();self.ctx.working[self.fid]=D(1);self.s.future_goals[self.future]=D(1)
        self.due('2026-08-13 10:00');self.due('2026-08-13 11:00')
        self.assertEqual(len(self.s._pending_scheduled_hedges[self.future]),2)
        self.s._greeks.reset_mock();self.fill(self.future,'BUY',1,'2026-08-13 11:02')
        self.assertEqual(self.s._greeks.call_count,1)
        row=[r for r in self.s.signals if r['action']=='SCHEDULED_HEDGE_CALCULATED'][-1]
        self.assertEqual([z[0]for z in row['scheduled_slots'][self.future]],['2026-08-13_10:00','2026-08-13_11:00'])
        count=len(self.ctx.intents);self.due('2026-08-13 11:03');self.assertEqual(len(self.ctx.intents),count)

    def test_pending_can_retry_on_next_real_frame_without_exact_slot(self):
        self.prepare_block();self.due();self.ctx.working[self.fid]=D(0)
        self.due('2026-08-13 14:02')
        self.assertFalse(self.s._pending_scheduled_hedges)
        self.assertEqual(self.s.future_goals[self.future],5)

    def test_ready_month_does_not_erase_another_month_fill_target(self):
        other=self.add_month();self.prepare_block();self.s._fill_hedge_targets[self.future]=D(0)
        self.due();self.assertEqual(self.s._fill_hedge_targets[self.future],0)
        self.assertNotIn(other.future,self.s._fill_hedge_targets)

    def test_stopping_and_final_flat_cancel_retry_without_reopening(self):
        self.prepare_block();self.due();count=len(self.ctx.intents)
        self.s.stopping=True;self.fill(self.future,'BUY',2,'2026-08-13 14:01')
        self.assertEqual(len(self.ctx.intents),count)
        self.s.stopping=False;self.s.final_day=f.date(2026,8,13)
        self.s.latest={key:(f.ns('2026-08-13 14:50'),6500 if key==self.future else 100)for key in [self.option,self.future]}
        self.due('2026-08-13 14:50');self.assertFalse(self.s._pending_scheduled_hedges)
        count=len(self.ctx.intents);self.fill(self.option,'SELL',1,'2026-08-13 14:51')
        self.assertEqual(len(self.ctx.intents),count)

    def test_expiry_and_cash_settlement_do_not_reopen_queued_future(self):
        self.prepare_block();self.due();self.s.future_last_trade_times[self.future]=f.ns('2026-08-13 14:01')
        count=len(self.ctx.intents);self.fill(self.future,'BUY',2,'2026-08-13 14:02')
        self.assertEqual(len(self.ctx.intents),count);self.assertFalse(self.s._pending_scheduled_hedges)

    def test_checkpoint_preserves_pending_months_and_retries_after_fresh_resume(self):
        self.prepare_block();self.due();cp=import_module('demos.08_option_vega.vega_signal').OptionVegaCheckpoint(self.s)
        state=json.loads(json.dumps(cp.snapshot_state()));cp.restore_state(state)
        self.assertIn(self.future,self.s._pending_scheduled_hedges)
        self.ctx.working[self.fid]=D(0)
        stamp=f.ns('2026-08-13 14:02');self.s.on_market_frame(NS(valuation_ns=stamp,request_ns=stamp+5_000_000_000,
            observations={k:NS(price=D(6500 if k==self.future else 100))for k in [self.option,self.future,'000852']}))
        self.assertFalse(self.s._pending_scheduled_hedges);self.assertEqual(self.s.future_goals[self.future],5)

    def test_checkpoint_rejects_invalid_pending_future_token_and_timestamp(self):
        self.prepare_block();self.due();cp=import_module('demos.08_option_vega.vega_signal').OptionVegaCheckpoint(self.s)
        state=cp.snapshot_state()
        for value in [{'UNKNOWN':[('2026-08-13_14:00',f.ns('2026-08-13 14:00'))]},
            {self.future:[('2026-08-13_11:00',f.ns('2026-08-13 14:00'))]},
            {self.future:[('2026-08-13_14:00',f.ns('2026-08-13 14:00')+1)]}]:
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'待办对冲'):
                cp.restore_state({**state,'pending_scheduled_hedges':value})

    def test_large_universe_retry_does_not_repeat_identity_validation_per_target(self):
        for i in range(300):
            key=f'MO2609-C-{8000+i}'
            self.s.contracts[key]=replace(self.f.info,symbol=key,strike=8000+i)
            self.s.instruments[key]=key+'.CFFEX';self.s.option_goals[key]=D(0)
        self.prepare_block();self.due()
        with patch.object(self.s,'_target_keys',wraps=self.s._target_keys)as lookup:
            self.fill(self.future,'BUY',2,'2026-08-13 14:01')
        self.assertLessEqual(lookup.call_count,5)
        self.assertEqual(self.ctx.intents[-1].targets[self.fid],5)

class StaticOptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):fills.FillHedgeTests.setUpClass.__func__(cls)
    def setUp(self):
        self.f=f.StrategyTests();self.f.setUp();self.s,self.ctx=self.f.strategy,self.f.ctx

    def test_native_id_string_cache_does_not_cache_actual_or_working_positions(self):
        calls=[]
        class Identity:
            def __init__(self,value):self.value=value
            def __str__(self):calls.append(self.value);return self.value
        self.s.instruments={k:Identity(v)for k,v in self.s.instruments.items()}
        bulk=Mock(side_effect=lambda keys:({k:self.ctx.actual.get(k,D(0))for k in keys},{k:self.ctx.working.get(k,D(0))for k in keys}))
        self.ctx.position_quantities=bulk
        self.s._position_snapshot();count=len(calls)
        for i in range(12):
            self.ctx.actual[self.f.ids[self.f.info.symbol]]=D(-i);self.ctx.working[self.f.ids['IM2609']]=D(i)
            actual,working=self.s._position_snapshot()
            self.assertEqual(actual[self.f.info.symbol],-i);self.assertEqual(working['IM2609'],i)
        self.assertEqual(len(calls),count);self.assertEqual(bulk.call_count,13)
        self.s.instruments['IM2609']=Identity('OTHER.CFFEX');self.ctx.actual['OTHER.CFFEX']=D(9)
        self.assertEqual(self.s._position_snapshot()[0]['IM2609'],9)

    def test_required_market_keys_uses_authoritative_bulk_not_per_symbol_scalar_reads(self):
        self.ctx.position_quantities=lambda keys:({k:D(-2 if k==self.f.ids[self.f.info.symbol]else 0)for k in keys},dict.fromkeys(keys,D(0)))
        self.ctx.position=Mock(side_effect=AssertionError('per-symbol position scan'))
        self.ctx.strategy_working_quantity=Mock(side_effect=AssertionError('per-symbol working scan'))
        self.assertEqual(self.s.required_market_keys(),{self.f.info.symbol,'IM2609','000852'})

    def test_remaining_days_cache_preserves_formula_and_error_checks(self):
        namespace=self.s._remaining_days.__func__.__globals__;calc=Mock(wraps=namespace['remaining_trading_days'])
        with patch.dict(namespace,remaining_trading_days=calc):
            first=self.s._remaining_days(f.date(2026,8,13),self.f.info.last_day)
            for _ in range(20):self.assertEqual(self.s._remaining_days(f.date(2026,8,13),self.f.info.last_day),first)
            self.assertEqual(calc.call_count,1)
            with self.assertRaises(ValueError):self.s._remaining_days(f.date(2026,9,18),f.date(2027,9,18))

    def test_unblocked_decisions_and_fresh_position_changes_match_previous_strategy(self):
        path=Path(__file__).parent/'fixtures/option_vega/entry_aligned_before_queue.py'
        spec=util.spec_from_file_location('demos.08_option_vega._entry_before_queue',path)
        reference=util.module_from_spec(spec);spec.loader.exec_module(reference)
        old=reference.OptionVegaStrategy('test',self.s.config,self.s.contracts,self.s.futures,
            self.s.instruments,self.s.index_id,self.s.trading_days,self.s.final_day,self.s.options_by_day)
        old_context=f.Context();old._bind(old_context);old._start()
        for day in ['2026-08-13','2026-08-14']:
            for clock in ['09:35','10:00','10:01','11:00','13:30','13:55','13:58','13:59','14:00','14:01','14:50','14:51','14:55']:
                stamp=f.ns(day+' '+clock)
                for strategy,context in [(old,old_context),(self.s,self.ctx)]:
                    if context.intents:context.actual.update(context.intents[-1].targets)
                    strategy.latest={'000852':(stamp,6400),'IM2609':(stamp,6500),self.f.info.symbol:(stamp,80)}
                    strategy._frame(stamp,stamp+5_000_000_001)
                self.assertEqual(self.s.option_goals,old.option_goals)
                self.assertEqual(self.s.future_goals,old.future_goals)
                self.assertEqual([(z.targets,z.ts_event)for z in self.ctx.intents],[(z.targets,z.ts_event)for z in old_context.intents])

if __name__=='__main__':unittest.main()
