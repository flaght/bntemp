"""Hybrid常驻的窗口、跨日联合恢复和模型续接；无网络、无真实订单。"""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, RLock
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest

# 真实SDK未安装的本机复用项目现有实体夹具，不替代被测恢复/策略实现。
from tests.run_hybrid_live_offline import live, adapter, Predictor, Upstream, InstrumentId, base
from tests.test_demo_hybrid_live import calendar, ns, ZONE
from bomber.framework.calendars.sessions import TradingWindow
from bomber.framework.trader.contracts import TargetPortfolio
from bomber.framework.trader.portfolio import AccountTargetKey, PortfolioCoordinatorState
from bomber.framework.trader.execution.ctp.native_driver import CtpNativeTraderDriver, CtpTraderSession
from bomber.framework.trader.execution.events import AccountStateEvent, ActiveOrderSnapshot, CurrencyBalance
from bomber.framework.trader.execution.policy import ExecutionPolicy
from bomber.framework.trader.persistence import JsonStateStore

continuous=import_module('bomber.framework.trader.runtime.live.continuous')
checkpoint=import_module('demos.10_hybrid_transformer.checkpoint')
history=import_module('demos.10_hybrid_transformer.history')
MINUTE=60_000_000_000
CLIENT=live.CLIENT_ID


def dt(text): return datetime.fromisoformat(text).replace(tzinfo=ZONE)


class Transport:
    is_test_transport=True
    account_id=investor_id='fixture'
    broker_id='9999'
    client_id=CLIENT
    production_mode=True
    front='tcp://182.254.243.31:30001'
    def __init__(self, day, quantity=1): self.day,self.quantity,self.revision=day,quantity,0
    def connect(self,*handlers): return CtpTraderSession('9999','fixture',self.day,7)
    def activate(self): pass
    def close(self): pass
    def query_positions(self): return {'rb2701.SHFE':Decimal(self.quantity)} if self.quantity else {}
    def query_gross_positions(self):
        return {'rb2701.SHFE':(Decimal(self.quantity),Decimal(0))} if self.quantity else {}
    def query_position_details(self):
        return [dict(InstrumentID='rb2701',ExchangeID='SHFE',Position=self.quantity,
            PosiDirection='2',PositionDate='2',HedgeFlag='1',PositionCost=1000)] if self.quantity else []
    def query_account(self):
        self.revision+=1
        return AccountStateEvent(CLIENT,'fixture',self.revision,ns('2026-10-28 21:00'),
            {'CNY':CurrencyBalance('CNY',total=100000,available=80000)})
    def query_active_orders(self):
        self.revision+=1
        return ActiveOrderSnapshot(CLIENT,'fixture',self.revision,ns('2026-10-28 21:00'),())
    def send_order(self,*args): raise AssertionError('测试不能发送订单')
    def cancel_order(self,*args): raise AssertionError('测试不能撤单')


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.release=self.root/'release';self.release.mkdir()
        self.directions=self.root/'directions.csv';self.directions.write_text('formula,direction\nf,1\n')
        self.signal=self.root/'signals.json'
        self.signal.write_text(json.dumps({'RB':dict(signal_id='1301',signal_method='threshold_signal',
            signal_params={'threshold':.02},hold_bars=5)}))
        self.path=self.root/'state.json'
        self.cal=calendar()

    def cli(self):
        return ['--connect','--product','RB','--release-path',str(self.release),
            '--directions-file',str(self.directions),'--signal-config',str(self.signal),
            '--no-session-overrides']

    def session(self, day='20261028', mode='recording', symbol='rb2701', policy='strict', state=None, **extras):
        instrument=InstrumentId.from_str(symbol+'.SHFE')
        refs=NS(instrument_id=instrument,trading_day=datetime.strptime(day,'%Y%m%d').date(),
            started_ns=ns('2026-10-28 14:00'),refresh_on_event=False,
            spec=NS(venue='SHFE',currency='CNY',tick=Decimal(1),multiplier=Decimal(10)),
            snapshot=lambda stamp:NS(instrument=lambda p,r:symbol,factor=lambda p,r:Decimal(1)))
        refs.instrument_meta=lambda:base.InstrumentMeta(instrument,price_precision=0,multiplier=Decimal(10))
        transport=Transport(day)
        driver=CtpNativeTraderDriver(CLIENT,'fixture',transport,enable_test_orders=True,disconnect_handler=lambda _:None)
        with patch.object(adapter,'MizarWorkflowAdapter',Predictor):
            session=live.assemble(NS(references=refs,calendar=self.cal,upstream=Upstream('fixture')),
                NS(driver=driver if mode=='simnow' else None),product='RB',release_path=self.release,
                directions_file=self.directions,signal_config=self.signal,
                market_config=live.MarketSourceConfig(),mode=mode,state_file=state or self.path,
                execution_policy=ExecutionPolicy(policy,'fills'),bar_directory=self.root/'market',**extras)
        return session,driver,transport

    def old_session(self, policy='strict', flat=False):
        session,driver,transport=self.session(mode='simnow',policy=policy)
        s=session.strategy
        label='2026-10-28 14:50' if flat else '2026-10-28 14:57'
        stamp=ns(label)+MINUTE-1
        update=s.holdings.advance('rb2701',1,dt(label),timestamp_ns=stamp)
        s.restore_target_revision(1)
        s.last_processed_ns=stamp
        s.bars_used=1;s.last_main='rb2701';s.last_targets={'rb2701':Decimal(1)}
        if flat:
            # 先在原日到期平掉再换约；不是丢弃未到期批次。
            s.holdings.expire(ns('2026-10-28 14:56'))
            s.last_targets={'rb2701':Decimal(0)}
        q=Decimal(0 if flat else 1)
        instrument=session.references.instrument_id
        positions=session.runner.position_manager
        if q:
            positions.set_account_position(CLIENT,instrument,q)
            positions.attribution.adopt(CLIENT,instrument,CLIENT,'rb_main',q)
            session.ledger.apply_fill(instrument,'BUY','OPEN',q,100,10)
        session.runner.target_store.apply(TargetPortfolio(CLIENT,1,ns('2026-10-28 14:58'),{'rb_main':q}))
        key=AccountTargetKey(CLIENT,instrument)
        session.runner.portfolio_coordinator.restore(PortfolioCoordinatorState(1,{CLIENT:{key:q}},
            {CLIENT:1},frozenset({key})))
        driver.start(lambda _:None);session.manager.save();driver.stop()
        return session

    def test_forever_cli_uses_calendar_dates_but_timed_mode_requires_dates(self):
        args=live.parse_args(self.cli()+['--run-forever'])
        self.assertTrue(args.run_forever);self.assertIsNone(args.expected_trading_day)
        with redirect_stdout(StringIO()),patch('sys.stderr',StringIO()),self.assertRaises(SystemExit):
            live.parse_args(self.cli())

    def test_history_read_count_is_independent_of_model_minimum(self):
        config=self.root/'history.json'
        config.write_text('{}')
        for count in (0,1,260,299,300,1500):
            command=self.cli()+['--run-forever','--history-minutes',str(count)]
            if count:command+=['--history-config',str(config)]
            parsed=live.parse_args(command)
            self.assertEqual(parsed.history_minutes,count)
            identity=live.model_identity(release_path=self.release,directions_file=self.directions,
                signal_config=self.signal,market_config=parsed.market_source_config,trading_day='20261030')
            self.assertEqual(identity['min_raw_bars'],260)
        for count in (-1,1501):
            with patch('sys.stderr',StringIO()),self.assertRaises(SystemExit):
                live.parse_args(self.cli()+['--run-forever','--history-minutes',str(count),
                    '--history-config',str(config)])

    def test_calendar_waits_for_lunch_and_enters_next_trading_day_at_night(self):
        now=[dt('2026-10-28 11:29')];stop=Event();seen=[]
        def factory(*, window_context):
            seen.append(window_context)
            def run():
                now[0]=window_context.window.end
                if len(seen)==3:stop.set()
                return {'status':'passed'}
            return NS(run=run,session=NS(window_components={
                'recording':NS(snapshot_state=lambda:{'fixture':True})}))
        def wait(_): now[0]+=timedelta(minutes=1)
        with redirect_stdout(StringIO()):
            result=continuous.run_continuous(factory,calendar=self.cal,stop_event=stop,
                clock=lambda:now[0],wait=wait)
        self.assertEqual([x.trading_day for x in seen],['20261028','20261028','20261029'])
        self.assertEqual([x.source_day for x in seen],['20261027','20261027','20261028'])
        self.assertEqual(seen[0].state,{})
        self.assertEqual(seen[1].state,{'recording':{'fixture':True}})
        self.assertEqual(result['windows'],3)

    def test_closed_stop_does_not_connect_and_fault_does_not_retry(self):
        stop=Event();factory=Mock()
        result=continuous.run_continuous(factory,calendar=self.cal,stop_event=stop,
            clock=lambda:dt('2026-10-28 12:00'),wait=lambda _:stop.set())
        factory.assert_not_called();self.assertTrue(result['stopped_by_operator'])
        factory.side_effect=RuntimeError('query failure')
        with self.assertRaisesRegex(RuntimeError,'query failure'):
            continuous.run_continuous(factory,calendar=self.cal,stop_event=Event(),
                clock=lambda:dt('2026-10-28 14:00'))
        self.assertEqual(factory.call_count,1)

    def test_scheduled_pause_precedes_stale_checks_and_disarms_without_flattening(self):
        lifecycle=object.__new__(live.HybridSessionLifecycle)
        window=TradingWindow('20261028',dt('2026-10-28 09:00'),dt('2026-10-28 11:30'),'am')
        lifecycle.resuming=False
        runner=NS(_submit_lock=RLock(),accept_bars=True,failure=None)
        session=NS(runner=runner,client=Mock(report_errors=[]),bar_feed=Mock(close_delay_ns=0),
            strategy=NS(bars_used=0),flush_completed_bars=True)
        context=continuous.LiveWindowContext(self.cal,window,Event(),'20261027',
            clock=lambda:dt('2026-10-28 11:30'))
        self.assertTrue(context.pause_due(session))
        lifecycle.scheduled_pause=context.scheduled_pause
        self.assertFalse(runner.accept_bars);self.assertTrue(runner.scheduled_pause)
        session.client.disarm.assert_called_once();session.client.submit_targets.assert_not_called()
        lifecycle.verify(session)
        runner.failure='actual fault'
        with self.assertRaisesRegex(RuntimeError,'actual fault'):lifecycle.verify(session)

    def test_trading_time_health_fault_still_propagates(self):
        lifecycle=object.__new__(live.HybridSessionLifecycle)
        lifecycle.window=TradingWindow('20261028',dt('2026-10-28 09:00'),dt('2026-10-28 11:30'),'am')
        lifecycle.stop_event=Event()
        session=NS(bar_feed=Mock())
        with patch.object(live,'datetime') as wall,patch.object(live.CtpSessionLifecycle,'poll',side_effect=RuntimeError('stale')):
            wall.now.return_value=dt('2026-10-28 11:00')
            with self.assertRaisesRegex(RuntimeError,'stale'):lifecycle.poll(session)

    def test_actual_joint_restore_keeps_batch_deadline_and_ownership_across_day(self):
        for policy in ('strict','off'):
            with self.subTest(policy=policy):
                self.path=self.root/f'{policy}.json'
                old=self.old_session(policy)
                before=self.path.read_bytes()
                saved=deepcopy(old.strategy.holdings.snapshot_state())
                new,driver,transport=self.session('20261029',mode='simnow',policy=policy)
                new.manager.restore(payload_transform=lambda p:checkpoint.payload_for_day(new,p,
                    driver=driver,transport=transport,day='20261029'))
                self.assertEqual(new.strategy.holdings.snapshot_state(),saved)
                self.assertEqual(self.path.read_bytes(),before)
                self.assertEqual(new.ledger.state().trading_day,'20261029')
                self.assertEqual(new.ledger.snapshot(new.references.instrument_id).long_yesterday,1)
                self.assertEqual(new.runner.position_manager.position(CLIENT,'rb_main'),1)
                driver.start(lambda _:None);driver.reconcile_account_state();driver.reconcile_active_orders()
                new.manager.commit_restore();driver.stop()
                self.assertTrue(list(self.root.glob(self.path.name+'.before-rollover-*')))
                batch=new.strategy.holdings.active[0]
                self.assertEqual(batch.expires_at_ns,ns('2026-10-28 21:03'))
                self.assertFalse(new.strategy.holdings.expire(ns('2026-10-28 21:02')).expired)
                self.assertEqual(new.strategy.holdings.expire(ns('2026-10-28 21:03')).expired,(batch,))

    def test_rollover_failure_keeps_original_file_and_model_changes_are_rejected(self):
        self.old_session();before=self.path.read_bytes()
        new,driver,transport=self.session('20261029',mode='simnow')
        transport.quantity=2
        with self.assertRaisesRegex(RuntimeError,'柜台总仓'):
            new.manager.restore(payload_transform=lambda p:checkpoint.payload_for_day(new,p,
                driver=driver,transport=transport,day='20261029'))
        self.assertEqual(self.path.read_bytes(),before)
        old={'trading_day':'20261028','sha256':'old','market':{'source':'ctp'}}
        for field,value in [('sha256','changed'),('market',{'source':'dolphindb'}),('trading_day','20261027')]:
            current={**old,'trading_day':'20261029',field:value}
            with self.assertRaises(ValueError):checkpoint.validate_model_identity(old,current)

    def test_live_old_batch_is_not_rebound_to_new_main(self):
        self.old_session();before=self.path.read_bytes()
        new,driver,transport=self.session('20261029',mode='simnow',symbol='rb2705')
        with self.assertRaisesRegex(ValueError,'旧合约仍有'):
            new.manager.restore(payload_transform=lambda p:checkpoint.payload_for_day(new,p,
                driver=driver,transport=transport,day='20261029'))
        self.assertEqual(self.path.read_bytes(),before)

    def test_flat_old_route_can_move_to_new_main_without_changing_terminal_audit(self):
        self.old_session(flat=True)
        new,driver,transport=self.session('20261029',mode='simnow',symbol='rb2705')
        transport.quantity=0
        new.manager.restore(payload_transform=lambda p:checkpoint.payload_for_day(new,p,
            driver=driver,transport=transport,day='20261029'))
        self.assertEqual(new.strategy.last_main,'rb2705')
        self.assertEqual(new.strategy.last_targets,{'rb2705':Decimal(0)})
        self.assertFalse(new.strategy.holdings.active)
        self.assertEqual(new.client.order_state_machine.checkpoints(),())

    def test_partial_model_window_merges_on_resume_without_replaying_strategy(self):
        session,_,_=self.session()
        runner=session.runner;end=ns('2026-10-28 13:31')
        runner._clock_ns=lambda:end+1_000_000_000
        def row(stamp):
            return dict(trade_time=datetime.fromtimestamp((stamp+1-MINUTE)/1e9,ZONE),code='RB',
                open=100,high=100,low=100,close=100,volume=2,value=2000,openint=500,vwap=100)
        stamps=self.cal.last_minutes(end,300)
        prior=[row(s) for s in stamps[:-1]]
        session.strategy.predictor.warm_history([row(stamps[-1])])
        seed=dict(instrument='rb2701.SHFE',identity=session.model_identity,rows=prior)
        with redirect_stdout(StringIO()) as output:
            history.warm_model_seed(runner,seed,identity=session.model_identity,
                max_bars=1500,minimum=300,minute_ns=MINUTE)
        self.assertEqual(len(session.strategy.predictor.workflow.history),300)
        self.assertEqual(session.strategy.bars_used,0);self.assertFalse(session.strategy.holdings.active)
        self.assertIn('warmup_remaining=0',output.getvalue())
        self.assertEqual(runner.history_last_ns,stamps[-1])
        conflict=deepcopy(seed);conflict['rows'][-1]['close']=101
        session.strategy.predictor.warm_history(prior)
        with self.assertRaisesRegex(ValueError,'内容冲突'):
            history.warm_model_seed(runner,conflict,identity=session.model_identity,
                max_bars=1500,minimum=300,minute_ns=MINUTE)

    def test_stop_during_preparation_does_not_start_warmup_or_expire_positions(self):
        stop=Event();stop.set()
        window=TradingWindow('20261028',dt('2026-10-28 13:30'),dt('2026-10-28 15:00'),'pm')
        session,_,_=self.session(window=window,stop_event=stop)
        session.runner._clock_ns=lambda:ns('2026-10-28 14:00')
        with patch.object(live,'warm_history') as history,patch.object(live,'warm_recording') as archive:
            session.runner.begin_bars()
        self.assertFalse(session.runner.accept_bars)
        self.assertTrue(session.runner.scheduled_pause)
        history.assert_not_called();archive.assert_not_called()
        self.assertFalse(session.strategy.holdings.active)

    def test_closed_bar_only_updates_raw_window_and_does_not_create_a_batch(self):
        window=TradingWindow('20261028',dt('2026-10-28 13:30'),dt('2026-10-28 15:00'),'pm')
        session,_,_=self.session(window=window)
        session.runner._clock_ns=lambda:ns('2026-10-28 15:00:02')
        instrument=session.references.instrument_id
        stamp=ns('2026-10-28 15:00')-1
        bar=base.make_bar(instrument_id=instrument,meta=session.references.instrument_meta(),
            ts_event=stamp,ts_init=stamp,open=100,high=100,low=100,close=100,volume=2)
        event=base.make_custom_bar(bar,dict(open=100,high=100,low=100,close=100,volume=2,
            value=2000,open_interest=500,trade_time=dt('2026-10-28 14:59')))
        session.runner.publish('model-bars',event)
        session.runner.publish('model-bars',event)
        self.assertEqual(len(session.strategy.predictor.workflow.history),1)
        self.assertEqual(session.strategy.bars_used,0)
        self.assertFalse(session.strategy.holdings.active)
        self.assertFalse(session.client.requests)

    def test_recording_retains_unexpired_batch_and_revision_in_next_window(self):
        old=self.old_session()
        saved=checkpoint.recording_snapshot(old)
        current,_,_=self.session('20261029',recording_state=saved)
        self.assertEqual(current.strategy.holdings.snapshot_state(),old.strategy.holdings.snapshot_state())
        self.assertEqual(current.strategy._revision,old.strategy._revision)
        self.assertFalse(current.client.requests)
        self.assertEqual(current.strategy.bars_used,1)

    def test_daily_order_limit_survives_window_recreation(self):
        from contextlib import nullcontext
        factory=Mock();stop=Event()
        orders={'x':[{'client_order_id':'CTP-9999-fixture-20261028-1'},
                     {'client_order_id':'CTP-9999-fixture-20261027-1'}]}
        saved=NS(payload={'orders':orders})
        factory.return_value.run.return_value={'status':'failed'}
        with patch.object(JsonStateStore,'load',return_value=saved),self.assertRaises(RuntimeError):
            continuous.run_continuous(factory,calendar=self.cal,stop_event=stop,
                state_file=self.path,max_session_orders=10,owner_lock=nullcontext(),
                clock=lambda:dt('2026-10-28 13:30'))
        self.assertEqual(factory.call_args.kwargs['window_context'].max_session_orders,9)
        factory.reset_mock()
        with patch.object(JsonStateStore,'load',return_value=saved),self.assertRaisesRegex(RuntimeError,'额度'):
            continuous.run_continuous(factory,calendar=self.cal,stop_event=stop,
                state_file=self.path,max_session_orders=1,owner_lock=nullcontext(),
                clock=lambda:dt('2026-10-28 13:30'))
        factory.assert_not_called()


if __name__=='__main__':unittest.main()
