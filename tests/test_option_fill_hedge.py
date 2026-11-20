"""成交入账后的实际仓位、在途净额与定时对冲回归；不伪装原生撮合。"""
from dataclasses import replace
from decimal import Decimal as D
from importlib import import_module
from types import SimpleNamespace
from enum import Enum
import sys,types
import unittest
from unittest.mock import Mock,patch

from tests import test_demo_option_vega as fixture


class FillHedgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 与原策略夹具一致：仅替换本机缺失的行情扩展类型，保留真实事件和Planner。
        trader=types.ModuleType('bomber.framework.trader')
        trader.__path__=[str(fixture.root/'bomber/framework/trader')]
        execution=types.ModuleType('bomber.framework.trader.execution')
        execution.__path__=[str(fixture.root/'bomber/framework/trader/execution')]
        market=types.ModuleType('bomber.framework.market.basic.base')
        market.InstrumentId=str;market.Bar=fixture.Bar
        market.DataType=Enum('DataType','BAR CUSTOM_BAR TRADE_TICK QUOTE_TICK')
        for name in ('CustomBar','TradeTick','QuoteTick'):setattr(market,name,type(name,(),{}))
        patches=patch.dict(sys.modules,{'bomber.framework.trader':trader,
            'bomber.framework.trader.execution':execution,'bomber.framework.market.basic.base':market})
        patches.start();cls.addClassCleanup(patches.stop)
        fixture.StrategyTests.setUpClass()
        cls.events=import_module('bomber.framework.trader.execution.events')

    def setUp(self):
        self.f=fixture.StrategyTests();self.f.setUp()
        self.s,self.ctx=self.f.strategy,self.f.ctx
        self.option=self.f.info.symbol;self.oid=self.f.ids[self.option]
        self.future='IM2609';self.fid=self.f.ids[self.future]
        self.s.option_goals[self.option]=D(-20)
        self.ctx.working[self.oid]=D(-20)
        self.s._greeks=Mock(return_value=SimpleNamespace(delta=.5))
        self.sequence=0

    def fill(self,key,side,qty,clock='2026-08-13 14:05',*,metadata=None):
        """模拟框架先应用成交及减少在途，再发真实类型的增量FillEvent。"""
        self.sequence+=1;instrument=self.s.instruments[key]
        signed=D(qty)*(1 if side=='BUY'else -1)
        self.ctx.actual[instrument]=self.ctx.position(instrument)+signed
        self.ctx.working[instrument]=self.ctx.strategy_working_quantity(instrument)-signed
        identity=self.events.ExecutionIdentity('model','account',self.s.strategy_id,
            f'order-{self.sequence}',str(instrument))
        event=self.events.FillEvent(identity,f'event-{self.sequence}',f'trade-{self.sequence}',
            side,D(qty),D(100),D('.15')*D(qty),'CNY',fixture.ns(clock),metadata=metadata or {})
        self.s.on_fill(event)
        return event

    def test_partial_and_remaining_fills_recompute_actual_without_waiting_for_slot(self):
        self.fill(self.option,'SELL',4)
        self.assertEqual(self.s.future_goals[self.future],1)
        first=self.ctx.intents[-1]
        self.assertEqual(first.ts_event,fixture.ns('2026-08-13 14:05'))
        self.assertEqual(first.targets[self.oid],-20)  # 没有把期权余单当成已成交。
        self.assertEqual(first.targets[self.fid],1)
        self.ctx.working[self.fid]=D(1)
        self.fill(self.option,'SELL',4)
        self.assertEqual(self.s.future_goals[self.future],2)
        self.assertEqual(self.ctx.intents[-1].targets[self.fid],2)
        self.ctx.working[self.fid]=D(2)
        count=len(self.ctx.intents)
        self.fill(self.option,'SELL',2)  # round(10 * .5 * 100 / 200) == 2。
        self.assertEqual(len(self.ctx.intents),count)
        self.fill(self.option,'SELL',10)  # 全部20手现已真实成交。
        self.assertEqual(self.s.future_goals[self.future],5)
        self.assertEqual(self.ctx.actual[self.oid],-20)
        self.assertFalse(self.s.hedge_sent)

    def test_real_net_planner_does_not_duplicate_option_or_pending_future_orders(self):
        # 使用公共Planner验证净订单，而不只检查策略意图内容。
        planner_module=import_module('bomber.framework.trader.execution.planner')
        request_class=import_module('bomber.framework.trader.contracts').ExecutionRequest
        positions=SimpleNamespace(effective_position=lambda client,key:
            self.ctx.position(key)+self.ctx.strategy_working_quantity(key))
        planner=planner_module.NetTargetOrderPlanner(positions)
        self.fill(self.option,'SELL',4)
        intent=self.ctx.intents[-1]
        request=request_class(self.s.strategy_id,intent.revision,'model',intent.ts_event,
            intent.targets,'DIRECT')
        orders=planner.plan(request)
        self.assertEqual([(o.instrument_id,o.quantity)for o in orders],[(self.fid,D(1))])
        self.ctx.working[self.fid]=D(1)
        self.fill(self.option,'SELL',4)
        intent=self.ctx.intents[-1]
        orders=planner.plan(replace(request,revision=intent.revision,targets=intent.targets))
        self.assertEqual([(o.instrument_id,o.quantity)for o in orders],[(self.fid,D(1))])

    def test_partial_close_then_full_close_reduces_hedge_from_actual(self):
        self.ctx.actual.update({self.oid:D(-12),self.fid:D(3)})
        self.ctx.working[self.oid]=D(12);self.s.option_goals[self.option]=D(0)
        self.s.future_goals[self.future]=D(3)
        self.fill(self.option,'BUY',4)
        self.assertEqual(self.ctx.intents[-1].targets[self.fid],2)
        self.ctx.working[self.fid]=D(-1)
        self.fill(self.option,'BUY',8)
        self.assertEqual(self.ctx.intents[-1].targets[self.fid],0)
        self.assertEqual(self.ctx.intents[-1].targets[self.oid],0)

    def test_opposing_inflight_adjustment_continues_on_future_fill_without_timer(self):
        self.fill(self.option,'SELL',4)
        self.ctx.working[self.fid]=D(1)
        self.s.option_goals[self.option]=D(0)
        self.ctx.working[self.oid]=D(4)
        self.fill(self.option,'BUY',4)
        self.assertEqual(len(self.ctx.intents),1)
        self.assertEqual(self.s._fill_hedge_targets,{self.future:D(0)})
        self.fill(self.future,'BUY',1,'2026-08-13 14:06')
        self.assertEqual(len(self.ctx.intents),2)
        self.assertEqual(self.ctx.intents[-1].targets[self.fid],0)
        self.assertEqual(self.ctx.intents[-1].ts_event,fixture.ns('2026-08-13 14:06'))
        self.assertEqual(self.s._fill_hedge_targets,{self.future:D(0)})
        self.ctx.working[self.fid]=D(-1)
        self.fill(self.future,'SELL',1,'2026-08-13 14:07')
        self.assertEqual(len(self.ctx.intents),2)  # 期货成交不递归生成新对冲。
        self.assertFalse(self.s._fill_hedge_targets)

    def test_new_option_fill_supersedes_obsolete_waiting_target(self):
        self.fill(self.option,'SELL',4);self.ctx.working[self.fid]=D(1)
        self.ctx.working[self.oid]=D(4);self.s.option_goals[self.option]=D(0)
        self.fill(self.option,'BUY',4)
        self.assertTrue(self.s._fill_hedge_targets)
        self.ctx.working[self.oid]=D(-4);self.s.option_goals[self.option]=D(-4)
        self.fill(self.option,'SELL',4)
        self.assertFalse(self.s._fill_hedge_targets)
        self.assertEqual(len(self.ctx.intents),1)

    def test_only_same_month_is_recalculated_and_scheduled_hedging_still_runs(self):
        other=replace(self.f.info,symbol='MO2610-C-6700',future='IM2610')
        self.s.contracts[other.symbol]=other
        self.s.futures[other.future]={'contMultNum':D(200)}
        self.s.instruments.update({other.symbol:other.symbol+'.CFFEX',other.future:other.future+'.CFFEX'})
        self.s.option_goals[other.symbol]=D(-20);self.s.future_goals[other.future]=D(7)
        self.ctx.actual[other.symbol+'.CFFEX']=D(-20)
        self.ctx.actual[other.future+'.CFFEX']=D(7)
        self.fill(self.option,'SELL',4)
        self.assertEqual(self.s.future_goals[other.future],7)
        self.assertEqual(self.s._greeks.call_count,1)
        self.ctx.actual[self.fid]=D(1);self.ctx.working[self.fid]=D(0)
        self.s._greeks.return_value=SimpleNamespace(delta=.9)
        stamp=fixture.ns('2026-08-13 14:55')
        self.s._frame(stamp,stamp+5_000_000_000)
        self.assertIn('2026-08-13_14:55',self.s.hedge_sent)
        self.assertEqual(self.s.future_goals[self.future],2)
        self.assertEqual(self.s.future_goals[other.future],9)

    def test_final_flat_and_stopping_never_reopen_futures_on_option_fill(self):
        for attr in ('flatten_requested','stopping'):
            with self.subTest(attr=attr):
                setattr(self.s,attr,True)
                self.fill(self.option,'SELL',4)
                self.assertFalse(self.ctx.intents)
                setattr(self.s,attr,False)
        self.s._greeks.assert_not_called()

    def test_cash_settlement_does_not_trigger_ordinary_hedge(self):
        self.s.option_last_trade_times[self.option]=fixture.ns('2026-08-13 15:00')
        self.ctx.actual[self.oid]=D(-4);self.ctx.working[self.oid]=D(4)
        self.fill(self.option,'BUY',4,'2026-08-13 16:00',metadata={
            'execution_origin':import_module('bomber.framework.trader.execution.option_settlement').SETTLEMENT_ORIGIN})
        self.s._greeks.assert_not_called()
        self.assertFalse(self.ctx.intents)

    def test_expired_future_and_risk_limit_are_not_bypassed(self):
        self.s.future_last_trade_times[self.future]=fixture.ns('2026-08-13 15:00')
        self.fill(self.option,'SELL',4,'2026-08-13 15:01')
        self.assertFalse(self.ctx.intents)
        self.s.future_last_trade_times.clear()
        self.s.config=replace(self.s.config,max_future_lots=1)
        with self.assertRaisesRegex(RuntimeError,'超过上限'):
            self.fill(self.option,'SELL',8)

    def test_late_report_does_not_backdate_request_or_read_future_quotes(self):
        stamp=fixture.ns('2026-08-13 14:06')
        self.s.last_event_ns=stamp;self.s._last_request_ns=stamp+5_000_000_000
        self.fill(self.option,'SELL',4,'2026-08-13 14:05')
        self.assertEqual(self.ctx.intents[-1].ts_event,stamp+5_000_000_000)
        self.assertEqual(self.s._greeks.call_args.args[1],stamp+5_000_000_000)

    def test_quote_age_guard_remains_in_force_for_immediate_hedge(self):
        self.s._greeks=self.s.__class__._greeks.__get__(self.s)
        self.s.latest={self.future:(fixture.ns('2026-08-13 13:59'),6500),
            self.option:(fixture.ns('2026-08-13 14:05'),100)}
        with self.assertRaisesRegex(RuntimeError,'持仓计算缺少新鲜行情'):
            self.fill(self.option,'SELL',4)

    def test_real_iv_and_delta_match_legacy_formula_for_filled_inventory(self):
        self.s._greeks=self.s.__class__._greeks.__get__(self.s)
        stamp=fixture.ns('2026-08-13 14:05')
        self.s.latest={self.future:(stamp,6500),self.option:(stamp,100)}
        self.fill(self.option,'SELL',4)
        from tests.test_option_source_logic import SourceLifecycleTests
        import pandas as pd
        expected=SourceLifecycleTests().legacy_hedge(pd.Timestamp('2026-08-13 14:05',tz='Asia/Shanghai'),self.s,self.ctx)
        self.assertEqual(self.s.future_goals[self.future],expected[self.future])
        self.assertEqual(self.ctx.intents[-1].targets[self.fid],1)


if __name__=='__main__':unittest.main()
