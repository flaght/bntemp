"""Hybrid实际策略/批次接入共享服务及IPC；柜台和模型为离线替身。"""
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest

from tests import test_hybrid_continuous as hybrid_fixture
from bomber.framework.trader.execution.ipc import AccountExecutionProtocol, ExecutionEventJournal
from bomber.framework.trader.execution.ipc_client import RemoteAccountService, RemoteTraderSession
from bomber.framework.trader.runtime.live.shared import SharedStrategySession
from bomber.framework.calendars import SessionCalendar

live = hybrid_fixture.live
shared = import_module('demos.10_hybrid_transformer.shared')
launcher = import_module('demos.10_hybrid_transformer.run_shared')


class HybridSharedLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release = self.root/'release'; self.release.mkdir()
        self.directions = self.root/'directions.csv'; self.directions.write_text('formula,direction\nf,1\n')
        self.signals = self.root/'signals.json'
        self.signals.write_text(json.dumps({
            'RB': dict(signal_id='1301', signal_method='threshold_signal',signal_params={'threshold': .02},hold_bars=5),
            'P': dict(signal_id='1101', signal_method='linear_signal',signal_params={'threshold': .05},hold_bars=5)}))
        self.market = live.MarketSourceConfig(source='dolphindb', table='fixture',
            timestamp_label='start',volume_mode='per-bar')
        original = hybrid_fixture.calendar()
        days = tuple(date(2026,10,day) for day in range(12,17))
        dates = replace(original.calendar,start=days[0],end=days[-1],days=days)
        self.cal = SessionCalendar(original.hours,dates,original.binding)
        self.now = hybrid_fixture.ns('2026-10-13 09:02:02')
        self.clock = patch('time.time_ns',side_effect=lambda:self.now)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.rb=hybrid_fixture.InstrumentId.from_str('rb2701.SHFE')
        self.p=hybrid_fixture.InstrumentId.from_str('p2701.DCE')
        self.owner=NS(now=self.now)
        self.account,self.service,self.transport=self.make_account()
        self.sessions = []

    def make_account(self, preserve_external=False):
        from contextlib import ExitStack
        import sys
        from bomber.framework.trader.execution import builders
        from bomber.framework.trader.execution.shared import SharedAccountExecutionService, SharedRiskBudget
        from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle
        from bomber.framework.trader.runtime.live.recovery import InstrumentPositionScope
        from bomber.framework.trader.execution.events import AccountStateEvent, CurrencyBalance, ActiveOrderSnapshot
        ctp=sys.modules['bomber.framework.trader.execution.ctp']
        for name,suffix in [('CtpExecutionAccounting','accounting'),('CtpLimitPlanner','limit_planner'),
                            ('CtpPositionLedger','ledger'),('CtpNativeTraderDriver','native_driver')]:
            setattr(ctp,name,getattr(import_module(ctp.__name__+'.'+suffix),name))
        pkg=sys.modules['bomber.framework.trader.execution.live']
        for name,suffix in [('ControlledLiveExecutionClient','controlled'),('NautilusLiveExecutionBackend','backend')]:
            setattr(pkg,name,getattr(import_module(pkg.__name__+'.'+suffix),name))
        from bomber.framework.trader.execution.ctp.native_driver import CtpTraderSession
        now=lambda:self.now
        class Transport:
            broker_id='9999'
            investor_id=account_id='fixture'
            client_id='account'
            front='tcp://182.254.243.31:30001'
            production_mode=True
            is_test_transport=False
            def __init__(self):self.active={};self.sent=[];self.gross={};self.revision=0
            def connect(self,on_order,on_trade,on_disconnect):
                self.starts=getattr(self,'starts',0)+1
                self.on_order,self.on_trade=on_order,on_trade
                return CtpTraderSession('9999','fixture','20261013',0)
            def activate(self):pass
            def close(self):pass
            def query_positions(self):return {i:Decimal(a-b) for i,(a,b) in self.gross.items()}
            def query_gross_positions(self):return {k:v for k,v in self.gross.items() if any(v)}
            def query_position_details(self):
                return [dict(InstrumentID=i.split('.')[0],ExchangeID=i.split('.')[1],Position=q,TodayPosition=q,
                    PosiDirection=d,PositionDate='1',HedgeFlag='1') for i,pair in self.gross.items()
                    for d,q in zip(('2','3'),pair) if q]
            def query_account(self):
                self.revision+=1
                return AccountStateEvent('account','fixture',self.revision,now(),
                    {'CNY':CurrencyBalance('CNY',total=100000000,available=100000000)})
            def query_active_orders(self):
                self.revision+=1
                return ActiveOrderSnapshot('account','fixture',self.revision,now(),())
            def raw(self,ref):
                row=self.active[ref]
                return dict(BrokerID='9999',InvestorID='fixture',InstrumentID=row['InstrumentID'],
                    ExchangeID=row['ExchangeID'],OrderRef=ref,FrontID=1,SessionID=2,OrderSysID='SYS'+ref)
            def send_order(self,fields):
                self.sent.append(dict(fields));self.active[fields['OrderRef']]={**fields,'filled':0}
                self.on_order({**self.raw(fields['OrderRef']),'OrderStatus':'3'})
            def cancel_order(self,ref):
                self.on_order({**self.raw(ref),'OrderStatus':'5'});self.active.pop(ref)
            def fill(self,ref):
                row=self.active[ref];q=row['VolumeTotalOriginal'];key=row['InstrumentID']+'.'+row['ExchangeID']
                long,short=self.gross.get(key,(0,0));buy=row['Direction']=='0';opening=row['CombOffsetFlag']=='0'
                if opening:long,short=(long+q,short) if buy else (long,short+q)
                else:long,short=(long,short-q) if buy else (long-q,short)
                self.gross[key]=(long,short)
                self.on_trade({**self.raw(ref),'TradeID':'T'+ref,'Volume':q,'Price':row['LimitPrice']})
                self.active.pop(ref)
        transport=Transport()
        driver=ctp.CtpNativeTraderDriver('account','fixture',transport,enable_simnow_orders=True,disconnect_handler=lambda _:None)
        limits={i:live.RiskLimits(max_order_quantity=10,max_abs_position=10,max_market_age_ns=120_000_000_000,
            contract_multiplier=1) for i in (self.rb,self.p)}
        scope=InstrumentPositionScope(limits)
        execution=builders.build_ctp_portfolio_execution(driver,trading_day='20261013',
            price_increments={i:1 for i in limits},multipliers={i:1 for i in limits},instrument_limits=limits,
            session_check=lambda:driver.is_simnow_session,orders=True,limit_offset_ticks=0,
            instrument_scope=scope,generic_close_venues=('DCE',),
            account_reconciliation='observe' if preserve_external else 'strict')
        service=SharedAccountExecutionService(execution.client,prices=execution.prices,instrument_limits=limits,
            budget=SharedRiskBudget(4,1000000),clock_ns=now,instrument_scope=scope,execution_mode='independent')
        service.event_journal=ExecutionEventJournal(('hybrid-rb','hybrid-p'))
        resources=ExitStack();self.addCleanup(resources.close)
        account=CtpSharedAccountLifecycle(execution,service,transport=transport,state_file=self.root/'account.json',
            identity={'id':'hybrid-test'},resources=resources,expected_day='20261013',position_scope=scope,
            preserve_external=preserve_external)
        return account,service,transport

    def session(self, product, *, service=None, driver=None):
        instrument = self.rb if product=='RB' else self.p
        refs = NS(instrument_id=instrument,trading_day=date(2026,10,13),
            started_ns=self.now-120_000_000_000,refresh_on_event=False,
            spec=NS(venue='SHFE' if product=='RB' else 'DCE',currency='CNY',tick=Decimal(1),multiplier=Decimal(1)),
            snapshot=lambda stamp:NS(instrument=lambda p,r:str(instrument.symbol).lower(),factor=lambda p,r:Decimal(1)))
        refs.instrument_meta=lambda:hybrid_fixture.base.InstrumentMeta(instrument,price_precision=0,multiplier=Decimal(1))
        calendar = (self.cal if product=='RB' else SessionCalendar(self.cal.hours,self.cal.calendar,
            replace(self.cal.binding,product='P',exchange='DCE')))
        with patch.object(hybrid_fixture.adapter,'MizarWorkflowAdapter',hybrid_fixture.Predictor):
            session = live.assemble(NS(references=refs,calendar=calendar,upstream=hybrid_fixture.Upstream('fixture')),
                NS(driver=driver or self.account.driver),product=product,release_path=self.release,
                directions_file=self.directions,signal_config=self.signals,market_config=self.market,
                mode='simnow',max_contracts=2,entry_contracts=2,
                shared_service=service or self.service,strategy_id='hybrid-'+product.lower(),
                bar_directory=self.root/'market')
        session.runner._clock_ns=lambda:self.now
        session.runner._session_check=lambda:True
        session.runner.instrument_health=None
        session.runner.first_complete_bar_ns=0
        return session

    def start(self, *sessions):
        self.owner.now=self.now
        self.account.start(resume=False); self.service.start(); self.account.authorize()
        self.addCleanup(self.account.driver.stop)
        for session in sessions:
            session.runner.start();session.runner.accept_bars=True
            self.sessions.append(session)
            self.addCleanup(session.runner.stop)

    def bar(self, session, label, signal):
        from datetime import timedelta
        trade_time=hybrid_fixture.dt(label)
        stamp=int((trade_time+timedelta(minutes=1)).timestamp())*10**9-1
        self.now=stamp+2_000_000_001
        session.strategy.predictor.on_bar=lambda row:dict(predicted_z=.03,signal=signal)
        instrument=session.references.instrument_id
        bar=hybrid_fixture.base.make_bar(instrument_id=instrument,meta=session.references.instrument_meta(),
            ts_event=stamp,ts_init=self.now,open=100,high=100,low=100,close=100,volume=2)
        event=hybrid_fixture.base.make_custom_bar(bar,dict(open=100,high=100,low=100,close=100,
            volume=2,value=200,open_interest=500,trade_time=trade_time))
        session.runner.publish('model-bars',bar)
        session.runner.publish('model-bars',event)

    def test_account_budget_failure_names_other_products_stale_quote_and_retains_owned_position(self):
        rb, p = self.session('RB'), self.session('P')
        self.start(rb, p)
        self.bar(p, '2026-10-13 09:01', -1)
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        p.client.drain_events()
        with self.assertRaisesRegex(RuntimeError,
                'strategy_id=hybrid-p target_key=p_main instrument=p2701.DCE reason=stale'):
            self.bar(rb, '2026-10-13 09:04', 1)
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.service.position_manager.position('hybrid-p', 'p_main'), -2)
        self.assertEqual(self.service.position_manager.position('hybrid-rb', 'rb_main'), 0)
        self.assertIn('instrument=p2701.DCE', rb.runner.failure_readiness['error'])
        self.assertEqual(rb.runner.failure_readiness['error_type'], 'RuntimeError')

    def test_same_process_two_real_runners_continuous_quantity_and_independent_five_minute_expiry(self):
        rb,p=self.session('RB'),self.session('P')
        self.start(rb,p)
        self.bar(rb,'2026-10-13 09:01',1)
        self.bar(p,'2026-10-13 09:01',.3)
        self.assertEqual([o['VolumeTotalOriginal'] for o in self.transport.sent],[2,1])
        for order in list(self.transport.sent):self.transport.fill(order['OrderRef'])
        rb.client.drain_events();p.client.drain_events()
        self.assertEqual(self.service.position_manager.position('hybrid-rb','rb_main'),2)
        self.assertEqual(self.service.position_manager.position('hybrid-p','p_main'),1)
        expiry=rb.strategy.holdings.active[0].expires_at_ns
        self.bar(rb,'2026-10-13 09:02',1)
        self.assertEqual(rb.strategy.holdings.active[0].expires_at_ns,expiry)
        self.assertEqual(len(self.transport.sent),2)
        self.bar(p,'2026-10-13 09:05',0)  # P仍有仓位，保持其估值行情新鲜。
        self.bar(rb,'2026-10-13 09:06',0)
        self.assertEqual(rb.strategy.last_targets,{'rb2701':Decimal(0)})
        self.assertEqual(self.transport.sent[-1]['InstrumentID'],'rb2701')
        self.assertEqual(self.transport.sent[-1]['VolumeTotalOriginal'],2)
        self.transport.fill(self.transport.sent[-1]['OrderRef']);rb.client.drain_events()
        self.assertEqual(self.service.position_manager.position('hybrid-rb','rb_main'),0)
        self.assertEqual(self.service.position_manager.position('hybrid-p','p_main'),1)
        self.assertEqual(p.strategy.holdings.active[0].quantity,1)
        self.assertEqual(self.service._sources['hybrid-rb']['metadata']['hybrid_signal_state']['hybrid']['revision'],2)

    def test_fresh_strategy_keeps_existing_same_contract_long_as_external_and_closes_only_own_short(self):
        self.account,self.service,self.transport=self.make_account(preserve_external=True)
        self.transport.gross={str(self.rb):(2,0),'sc2611.INE':(1,0)}
        rb,p=self.session('RB'),self.session('P')
        self.start(rb,p)
        self.assertEqual(self.service.position_manager.position('hybrid-rb','rb_main'),0)
        self.assertEqual(self.service.position_manager.position('hybrid-p','p_main'),0)
        self.assertEqual(self.service.position_manager.account_position('account',self.rb),0)
        self.assertEqual(self.account.external_gross[str(self.rb)],(2,0))
        self.assertEqual(self.account.external_gross['sc2611.INE'],(1,0))
        self.assertFalse(rb.strategy.holdings.active)
        self.assertFalse(self.transport.sent)
        self.bar(rb,'2026-10-13 09:01',-1)
        opening=self.transport.sent[-1]
        self.assertEqual((opening['Direction'],opening['CombOffsetFlag'],opening['VolumeTotalOriginal']),('1','0',2))
        self.transport.fill(opening['OrderRef']);rb.client.drain_events()
        self.assertEqual(self.transport.gross[str(self.rb)],(2,2))
        self.assertEqual(self.service.position_manager.position('hybrid-rb','rb_main'),-2)
        self.account._validate_account()
        self.bar(rb,'2026-10-13 09:06',0)
        closing=self.transport.sent[-1]
        self.assertEqual((closing['Direction'],closing['CombOffsetFlag'],closing['VolumeTotalOriginal']),('0','3',2))
        self.transport.fill(closing['OrderRef']);rb.client.drain_events()
        self.assertEqual(self.transport.gross[str(self.rb)],(2,0))
        self.assertEqual(self.transport.gross['sc2611.INE'],(1,0))
        self.assertEqual(self.service.position_manager.position('hybrid-rb','rb_main'),0)
        self.assertFalse(rb.strategy.holdings.active)
        self.account._validate_account()
        self.assertEqual(len(self.transport.sent),2)

    def test_ipc_runner_uses_same_batch_logic_and_continuous_size(self):
        # server只装配执行声明；算法在另一个Remote端口实例运行。
        server_rb,server_p=self.session('RB'),self.session('P')
        protocol=AccountExecutionProtocol(self.service,keys={'hybrid-rb':'rb-key','hybrid-p':'p-key'},trading_day='20261013')
        self.owner.now=self.now
        self.account.start(resume=False);self.service.start();self.account.authorize()
        self.addCleanup(self.account.driver.stop)
        class Connection:
            sid='hybrid-p'
            lease=None
            sequence=0
            def call(connection, action, data):
                connection.sequence+=1
                result=protocol.handle(dict(version=1,strategy_id=connection.sid,key='p-key',
                    lease=connection.lease,request_id=f'p-{connection.sequence}',action=action,data=data))
                if action=='attach':connection.lease=result['lease']
                return result
        remote=RemoteAccountService(Connection(),server_p.client.configuration(),protocol.state('hybrid-p'))
        session=self.session('P',service=remote,driver=RemoteTraderSession(remote,'20261013'))
        session.client.ready_check=lambda:True
        session.runner.start();session.runner.accept_bars=True
        self.addCleanup(session.runner.stop)
        self.bar(session,'2026-10-13 09:01',.3)
        self.assertEqual(self.transport.sent[-1]['VolumeTotalOriginal'],1)
        self.transport.fill(self.transport.sent[-1]['OrderRef']);session.client.drain_events()
        self.assertEqual(session.client.position_manager.position('hybrid-p','p_main'),1)
        self.bar(session,'2026-10-13 09:06',0)
        self.assertFalse(session.strategy.holdings.active)
        self.assertEqual(self.transport.sent[-1]['CombOffsetFlag'],'1')
        self.transport.fill(self.transport.sent[-1]['OrderRef']);session.client.drain_events()
        self.assertEqual(self.service.position_manager.position('hybrid-p','p_main'),0)
        self.assertFalse(self.service._pending or self.service._failed)

    def test_same_process_checkpoint_restores_through_remote_port_without_resetting_expiry(self):
        from bomber.framework.trader.persistence import JsonStateStore
        rb,p=self.session('RB'),self.session('P')
        self.start(rb,p)
        self.bar(rb,'2026-10-13 09:01',1)
        self.transport.fill(self.transport.sent[-1]['OrderRef']);rb.client.drain_events()
        before=rb.strategy.holdings.snapshot_state()
        item=SharedStrategySession(rb,self.root/'rb-signal.json',{'id':'rb-stable'})
        item.save(self.service)
        saved=JsonStateStore(item.state_file).load()
        self.assertEqual(saved.payload['signal']['hybrid']['holdings'],before)
        for session in (rb,p):
            session.client.begin_stop();self.service.poll_stopping();session.client.drain_events()
            self.assertTrue(session.client.finish_stop_events())
            session.runner.stop()
        self.account.stop()
        gross=dict(self.transport.gross)
        self.account,self.service,self.transport=self.make_account()
        self.transport.gross=gross
        server_rb,server_p=self.session('RB'),self.session('P')
        self.account.start(resume=True);self.service.start();self.account.authorize()
        self.addCleanup(self.account.driver.stop)
        protocol=AccountExecutionProtocol(self.service,keys={'hybrid-rb':'rb','hybrid-p':'p'},trading_day='20261013')
        class Connection:
            sid='hybrid-rb';lease=None;sequence=0
            def call(connection,action,data):
                connection.sequence+=1
                result=protocol.handle(dict(version=1,strategy_id=connection.sid,key='rb',lease=connection.lease,
                    request_id=f'restored-{connection.sequence}',action=action,data=data))
                if action=='attach':connection.lease=result['lease']
                return result
        state=protocol.state('hybrid-rb')
        remote=RemoteAccountService(Connection(),server_rb.client.configuration(),state)
        resumed=self.session('RB',service=remote,driver=RemoteTraderSession(remote,'20261013'))
        resumed.restore_signal(saved.payload['signal'])
        resumed.client.ready_check=lambda:True
        resumed.client.set_cursor(state['ack'])
        self.assertEqual(resumed.strategy.holdings.snapshot_state(),before)
        resumed.runner.start();resumed.runner.accept_bars=True;self.addCleanup(resumed.runner.stop)
        self.bar(resumed,'2026-10-13 09:06',0)
        self.assertEqual(self.transport.sent[-1]['VolumeTotalOriginal'],2)
        self.assertEqual(self.transport.sent[-1]['CombOffsetFlag'],'3')
        self.assertFalse(resumed.strategy.holdings.active)
        self.transport.fill(self.transport.sent[-1]['OrderRef']);resumed.client.drain_events()
        self.assertEqual(self.service.position_manager.position('hybrid-rb','rb_main'),0)
        # IPC worker写出的同一信号文件再次切回本地端口，不换文件/策略ID。
        from bomber.framework.trader.persistence import JsonStateStore
        current=JsonStateStore(item.state_file).load()
        JsonStateStore(item.state_file).save(dict(version=1,identity={'id':'rb-stable'},
            revision=resumed.strategy._revision,cursor=resumed.client.cursor,
            signal=resumed.signal_checkpoint.snapshot_state()),expected_generation=current.generation)
        resumed.runner.stop();self.account.stop()
        self.account,self.service,self.transport=self.make_account()
        local_rb,local_p=self.session('RB'),self.session('P')
        self.account.start(resume=True);self.service.start();self.account.authorize()
        self.addCleanup(self.account.driver.stop)
        SharedStrategySession(local_rb,item.state_file,{'id':'rb-stable'}).restore(self.service,resume=True)
        self.assertEqual(local_rb.strategy._revision,2)
        self.assertFalse(local_rb.strategy.holdings.active)
        self.assertFalse(self.transport.sent)

    def rejected_target_checkpoint(self, *, previous_target=True, renew=False):
        from bomber.framework.trader.persistence import JsonStateStore
        rb, p = self.session('RB'), self.session('P')
        self.start(rb, p)
        item = SharedStrategySession(rb, self.root/'rejected-rb.json', {'id':'rejected-rb'})
        rb.strategy.bind_checkpoint(lambda: item.save(self.service))
        if previous_target:
            self.bar(rb, '2026-10-13 09:01', 1)
            self.transport.fill(self.transport.sent[-1]['OrderRef']); rb.client.drain_events()
        self.bar(p, '2026-10-13 09:01', -1)
        self.transport.fill(self.transport.sent[-1]['OrderRef']); p.client.drain_events()
        if renew:
            self.bar(p, '2026-10-13 09:05', -1)
            self.bar(rb, '2026-10-13 09:06', 1)
            self.bar(p, '2026-10-13 09:07', -1)
        accepted = rb.signal_checkpoint.snapshot_state()
        with self.assertRaisesRegex(RuntimeError, 'instrument=p2701.DCE reason=stale'):
            self.bar(rb, '2026-10-13 09:11' if renew else
                '2026-10-13 09:06' if previous_target else '2026-10-13 09:04',
                -1 if previous_target else 1)
        item.save(self.service)
        saved = JsonStateStore(item.state_file).load()
        self.assertEqual(saved.payload['signal']['hybrid']['revision'], saved.payload['revision']+1)
        self.assertTrue(saved.payload['signal']['hybrid']['targets_pending'])
        rb.client.begin_stop(); self.service.poll_stopping()
        self.assertTrue(rb.client.finish_stop_events())
        rb.runner.stop()
        return rb, p, item.state_file, saved, accepted

    def test_rejected_reversal_recovers_accepted_batch_and_original_expiry_without_orders(self):
        from bomber.framework.trader.persistence import JsonStateStore
        rb, p, path, saved, accepted = self.rejected_target_checkpoint()
        expiry = accepted['hybrid']['holdings']['active'][0]['expires_at_ns']
        original = path.read_bytes()
        resumed = self.session('RB')
        item = SharedStrategySession(resumed, path, {'id':'rejected-rb'})
        account_before, sent = self.service.snapshot_state(), len(self.transport.sent)
        item.restore(self.service, resume=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.service.snapshot_state(), account_before)
        self.assertEqual(resumed.strategy._revision, saved.payload['revision'])
        self.assertFalse(resumed.strategy._targets_pending)
        self.assertEqual(resumed.strategy.holdings.active[0].quantity, 2)
        self.assertEqual(resumed.strategy.holdings.active[0].expires_at_ns, expiry)
        self.assertEqual(resumed.strategy.last_targets, {'rb2701':Decimal(2)})
        self.assertEqual(self.service.position_manager.position('hybrid-p','p_main'), -2)
        self.assertEqual(len(self.transport.sent), sent)
        # 账户已保存的回报从同代metadata游标重新交付，成交计数不丢失，也不重复下单。
        resumed.runner.start(); self.addCleanup(resumed.runner.stop)
        resumed.client.drain_events()
        self.assertEqual(resumed.strategy.fills_received, 1)
        item.save(self.service)
        current = JsonStateStore(path).load()
        self.assertEqual(current.payload['revision'], current.payload['signal']['hybrid']['revision'])
        self.assertEqual(len(self.transport.sent), sent)

    def test_rejected_target_preserves_latest_same_direction_renewal_deadline(self):
        _, _, path, saved, accepted = self.rejected_target_checkpoint(renew=True)
        original = self.service._sources['hybrid-rb']['metadata']['hybrid_signal_state']
        latest_expiry = accepted['hybrid']['holdings']['active'][0]['expires_at_ns']
        self.assertGreater(latest_expiry, original['hybrid']['holdings']['active'][0]['expires_at_ns'])
        resumed = self.session('RB')
        SharedStrategySession(resumed,path,{'id':'rejected-rb'}).restore(self.service,resume=True)
        self.assertEqual(resumed.strategy.holdings.active[0].expires_at_ns, latest_expiry)
        self.assertEqual(resumed.strategy.holdings.active[0].quantity, 2)
        self.assertEqual(len(self.transport.sent), 2)

    def test_old_rejected_checkpoint_without_accepted_copy_uses_account_metadata(self):
        from copy import deepcopy
        _, _, path, saved, accepted = self.rejected_target_checkpoint()
        old = deepcopy(saved.payload['signal'])
        old.pop('accepted_state',None)
        resumed = self.session('RB')
        resumed.restore_signal(old)
        self.assertEqual(resumed.strategy.holdings.active[0].expires_at_ns,
            accepted['hybrid']['holdings']['active'][0]['expires_at_ns'])
        self.assertEqual(resumed.strategy._revision, saved.payload['revision'])
        self.assertFalse(resumed.strategy._targets_pending)

    def test_rejected_first_entry_recovers_empty_own_batches_preserving_other_strategy_position(self):
        _, p, path, saved, _ = self.rejected_target_checkpoint(previous_target=False)
        resumed = self.session('RB')
        account_before = self.service.snapshot_state()
        SharedStrategySession(resumed, path, {'id':'rejected-rb'}).restore(self.service, resume=True)
        self.assertEqual(resumed.strategy._revision, 0)
        self.assertFalse(resumed.strategy._targets_pending)
        self.assertFalse(resumed.strategy.holdings.active)
        self.assertEqual(resumed.strategy.last_targets, {})
        self.assertEqual(self.service.position_manager.position('hybrid-p','p_main'), -2)
        self.assertEqual(self.service.snapshot_state(), account_before)
        self.assertEqual(len(self.transport.sent), 1)

    def test_rejected_target_does_not_bypass_changed_model_or_sizing_identity(self):
        from copy import deepcopy
        _, _, path, saved, _ = self.rejected_target_checkpoint()
        resumed = self.session('RB')
        for key in ('model', 'calendar', 'config'):
            bad = deepcopy(saved.payload['signal'])
            if key == 'config':
                bad['identity'][key]['max_contracts'] = 3
            else:
                bad['identity'][key] = 'changed'
            before = resumed.signal_checkpoint.snapshot_state()
            with self.assertRaisesRegex(ValueError, '恢复身份不一致；差异字段=session.'+key):
                resumed.restore_signal(bad)
            self.assertEqual(resumed.signal_checkpoint.snapshot_state(), before)

    def test_unexplained_version_jump_or_missing_accepted_batches_still_rejects(self):
        from copy import deepcopy
        _, _, path, saved, _ = self.rejected_target_checkpoint()
        resumed = self.session('RB')
        for pending, delta in ((False,0),(True,1)):
            bad = deepcopy(saved.payload['signal'])
            bad['hybrid']['targets_pending'] = pending
            bad['hybrid']['revision'] += delta
            with self.assertRaisesRegex(ValueError, '不是单次待提交目标'):
                resumed.restore_signal(bad)
        source = self.service._sources['hybrid-rb']['metadata']
        previous = source.pop('hybrid_signal_state')
        try:
            with self.assertRaisesRegex(ValueError, '缺少账户已受理的同代Hybrid批次'):
                resumed.restore_signal(saved.payload['signal'])
            self.assertEqual(resumed.strategy._revision, 0)
        finally:
            source['hybrid_signal_state'] = previous

    def test_empty_accepted_target_cannot_clear_existing_owned_quantity(self):
        _, _, path, saved, _ = self.rejected_target_checkpoint(previous_target=False)
        resumed = self.session('RB')
        self.service.position_manager.set_strategy_position('hybrid-rb','rb_main',1)
        try:
            with self.assertRaisesRegex(ValueError, '已有策略归属或在途数量'):
                resumed.restore_signal(saved.payload['signal'])
            self.assertEqual(resumed.strategy._revision, 0)
        finally:
            self.service.position_manager.set_strategy_position('hybrid-rb','rb_main',0)

    def test_remote_worker_uses_same_accepted_state_for_rejected_target_recovery(self):
        _, _, path, saved, accepted = self.rejected_target_checkpoint()
        server_rb = self.session('RB')
        protocol = AccountExecutionProtocol(self.service, keys={'hybrid-rb':'rb','hybrid-p':'p'},
            trading_day='20261013')
        state = protocol.state('hybrid-rb')
        remote = RemoteAccountService(NS(sid='hybrid-rb'), server_rb.client.configuration(), state)
        resumed = self.session('RB', service=remote, driver=RemoteTraderSession(remote,'20261013'))
        resumed.restore_signal(saved.payload['signal'])
        self.assertEqual(resumed.strategy._revision, state['source_revision'])
        self.assertEqual(resumed.strategy.holdings.active[0].expires_at_ns,
            accepted['hybrid']['holdings']['active'][0]['expires_at_ns'])
        self.assertEqual(resumed.strategy.holdings.active[0].quantity, 2)
        self.assertFalse(resumed.strategy._targets_pending)
        self.assertEqual(len(self.transport.sent), 2)

    def test_account_metadata_recovers_batch_when_strategy_file_lags_one_target(self):
        rb,p=self.session('RB'),self.session('P')
        initial=rb.signal_checkpoint.snapshot_state()
        self.start(rb,p)
        self.bar(rb,'2026-10-13 09:01',1)
        self.transport.fill(self.transport.sent[-1]['OrderRef']);rb.client.drain_events()
        expiry=rb.strategy.holdings.active[0].expires_at_ns
        # 新目标已在账户文件保存，但旧策略文件仍是开仓前的版本。
        current=self.service._sources['hybrid-rb']['metadata']['hybrid_signal_state']
        rb.runner.stop()
        target=shared.execution_only_strategy(rb.references,self.cal,product='RB',signal_config=self.signals,
            max_contracts=2,entry_contracts=2,strategy_id='hybrid-rb')
        # 使用同一策略/Runner对象的未启动状态验证纯恢复，不重新注册同名账户端口。
        from bomber.framework.trader.portfolio import TargetStore
        rb.runner.target_store.restore({})
        rb.strategy.holdings=target.holdings
        rb.signal_checkpoint.restore_state(initial)
        self.assertEqual(rb.strategy.holdings.active[0].expires_at_ns,expiry)
        self.assertEqual(rb.strategy._revision,current['hybrid']['revision'])
        self.assertTrue(rb.strategy._targets_pending)

    def test_same_process_signal_save_does_not_ack_undelivered_fills(self):
        rb,p=self.session('RB'),self.session('P')
        self.start(rb,p)
        self.bar(rb,'2026-10-13 09:01',1)
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        journal=self.service.event_journal
        item=SharedStrategySession(rb,self.root/'rb-signal.json',{'id':'rb'})
        item.save(self.service)
        self.assertEqual(journal.ack['hybrid-rb'],0)
        rb.client.drain_events();item.save(self.service)
        self.assertEqual(journal.ack['hybrid-rb'],len(journal.events['hybrid-rb']))
        self.assertEqual(rb.strategy.fills_received,1)

    def test_native_import_rejects_changed_strategy_config_identity_or_calendar_before_mutation(self):
        from copy import deepcopy
        rb,p=self.session('RB'),self.session('P')
        self.account.hybrid_sessions={'hybrid-rb':rb,'hybrid-p':p}
        self.account.preserve_external=True
        original={'component_states':{'hybrid':rb.signal_checkpoint.hybrid.snapshot_state(),
            'live_session':{'identity':rb.model_identity},
            'session_rules':{'fingerprint':self.cal.provenance['sha256']}},
            'ctp_drivers':{'account':{}},'positions':{'attribution':{'owned':[]}},
            'portfolio':{'contributions':{}}}
        variants=[]
        config=deepcopy(original);config['component_states']['hybrid']['config']['max_contracts']=3;variants.append(config)
        model=deepcopy(original);model['component_states']['live_session']['identity']['sha256']='changed';variants.append(model)
        calendar=deepcopy(original);calendar['component_states']['session_rules']['fingerprint']='changed';variants.append(calendar)
        owner=deepcopy(original);owner['positions']['attribution']['owned']=[{'strategy_id':'someone-else'}];variants.append(owner)
        for state in variants:
            before=deepcopy(state)
            with self.assertRaises(ValueError):shared.import_native_payload(self.account,state)
            self.assertEqual(state,before)
        self.assertFalse(self.transport.sent)

    def test_actual_shared_factory_builds_two_strategies_and_prepares_history_without_loading_sdk_models(self):
        from scripts.integration.shared_account import run
        refs={product:self.session(product) for product in ('RB','P')}
        config={'version':1,'trading_day':'20261013','environment':'realtime','seconds':60,
            'execution_mode':'independent','position_scope':'strategy_instruments','existing_position_policy':'preserve_external',
            'account_state_file':str(self.root/'host-account.json'),'report_dir':str(self.root/'reports'),
            'account':dict(client_id='account',max_gross_quantity=4,max_gross_notional=1000000,
                max_session_orders=100,limit_offset_ticks=0),
            'strategies':[dict(strategy_id='hybrid-'+product.lower(),kind='hybrid_transformer',
                state_file=str(self.root/(product+'-signal.json')),argv=['--product',product,
                    '--release-path',str(self.release),'--directions-file',str(self.directions),
                    '--signal-config',str(self.signals),'--market-source','dolphindb',
                    '--market-config',str(self.root/'market.json'),'--max-contracts','2','--entry-contracts','2',
                    '--no-session-overrides','--report-dir',str(self.root/(product+'-reports'))]) for product in ('RB','P')]}
        (self.root/'market.json').write_text(json.dumps(dict(source='dolphindb',table='fixture',
            timestamp_label='start',volume_mode='per-bar')))
        def prepare(context,lifecycle,*,product,**kwargs):
            source=refs[product]
            source.references.refresh=lambda:None
            return NS(references=source.references,calendar=source.runner.calendar,
                upstream=hybrid_fixture.Upstream('host-'+product))
        with patch.object(shared,'previous_source_day',return_value='20261012'), \
                patch.object(live,'prepare_inputs',side_effect=prepare), \
                patch.object(hybrid_fixture.adapter,'MizarWorkflowAdapter',hybrid_fixture.Predictor), \
                patch('bomber.framework.trader.runtime.live.channels.ctp.build_simnow_transport',return_value=type(self.transport)()):
            host=run.build_host(config,resume=False)
        self.addCleanup(host.account.driver.stop)
        for item in host.strategies.values():
            item.session.runner._clock_ns=lambda:self.now
            item.session.runner._session_check=lambda:True
            item.session.runner.instrument_health=None
        host.start()
        self.assertEqual(set(host.strategies),{'hybrid-rb','hybrid-p'})
        self.assertEqual(host.service.execution_mode,'independent')
        self.assertIsNotNone(host.service.event_journal)
        active=host.strategies['hybrid-rb'].session
        host.service.clock_ns=lambda:self.now
        self.bar(active,'2026-10-13 09:03',1)
        host.account.transport.fill(host.account.transport.sent[-1]['OrderRef'])
        active.client.drain_events()
        native_signal=active.signal_checkpoint.hybrid.snapshot_state()
        host.stop()
        self.assertTrue(Path(config['account_state_file']).is_file())
        self.assertTrue((self.root/'RB-reports/hybrid-simnow-RB-20261013').is_dir())
        self.assertTrue((self.root/'P-reports/hybrid-simnow-P-20261013').is_dir())
        from copy import deepcopy
        from bomber.framework.trader.persistence import JsonStateStore
        store=JsonStateStore(config['account_state_file']);saved=store.load()
        native=deepcopy(saved.payload)
        native['component_states']={'hybrid':native_signal,
            'live_session':{'version':1,'identity':active.model_identity},
            'session_rules':{'version':1,'fingerprint':active.runner.calendar.provenance['sha256']}}
        native_saved=store.save(native,expected_generation=saved.generation)
        original_bytes=Path(config['account_state_file']).read_bytes()
        Path(config['strategies'][0]['state_file']).unlink()  # 首次接入时RB信号文件尚未创建。
        next_transport=type(self.transport)();next_transport.gross=dict(host.account.transport.gross)
        with patch.object(shared,'previous_source_day',return_value='20261012'), \
                patch.object(live,'prepare_inputs',side_effect=prepare), \
                patch.object(hybrid_fixture.adapter,'MizarWorkflowAdapter',hybrid_fixture.Predictor), \
                patch('bomber.framework.trader.runtime.live.channels.ctp.build_simnow_transport',return_value=next_transport):
            resumed=run.build_host(config,resume=True)
        self.addCleanup(resumed.account.driver.stop)
        for item in resumed.strategies.values():
            item.session.runner._clock_ns=lambda:self.now
            item.session.runner._session_check=lambda:True
            item.session.runner.instrument_health=None
        # 启动准备/历史阶段不能覆盖原Native账户文件；authorize完成后才提交备份。
        original_authorize=resumed.account.authorize
        def authorize():
            self.assertEqual(Path(config['account_state_file']).read_bytes(),original_bytes)
            return original_authorize()
        resumed.account.authorize=authorize
        resumed.start()
        self.assertFalse(next_transport.sent)
        self.assertEqual(resumed.strategies['hybrid-rb'].session.strategy.holdings.snapshot_state(),native_signal['holdings'])
        self.assertFalse(resumed.strategies['hybrid-p'].session.strategy.holdings.active)
        backup=Path(config['account_state_file']+'.before-rollover-g'+str(native_saved.generation)+'.json')
        self.assertEqual(backup.read_bytes(),original_bytes)
        resumed.stop()
        # 多进程server走同一工厂，但不能加载任何Transformer模型。
        declaration_transport=type(self.transport)();declaration_transport.gross=dict(next_transport.gross)
        with patch.object(shared,'previous_source_day',return_value='20261012'), \
                patch.object(live,'prepare_inputs',side_effect=prepare), \
                patch.object(hybrid_fixture.adapter,'MizarWorkflowAdapter',side_effect=AssertionError('server loaded model')), \
                patch('bomber.framework.trader.runtime.live.channels.ctp.build_simnow_transport',return_value=declaration_transport):
            declaration_host=run.build_host(config,resume=True,strategy_processes=True)
        self.assertTrue(all(isinstance(item.session.strategy.predictor,shared.ExecutionOnlyPredictor)
                            for item in declaration_host.strategies.values()))
        self.assertTrue(all(item.session.shared_report is None for item in declaration_host.strategies.values()))
        declaration_host.account.driver.stop();declaration_host.resources.close()

    def test_two_real_worker_processes_run_hybrid_through_one_unix_account_service(self):
        import multiprocessing
        import os
        import threading
        import time
        from bomber.framework.trader.execution.ipc import UnixExecutionConnection, UnixExecutionServer
        rb,p=self.session('RB'),self.session('P')
        protocol=AccountExecutionProtocol(self.service,keys={'hybrid-rb':'rb','hybrid-p':'p'},trading_day='20261013')
        self.account.start(resume=False);self.service.start();self.account.authorize()
        self.addCleanup(self.account.driver.stop)
        directory=TemporaryDirectory(dir='/tmp');self.addCleanup(directory.cleanup)
        server=UnixExecutionServer(Path(directory.name)/'exec.sock',protocol);self.addCleanup(server.close)
        context=multiprocessing.get_context('fork');queue=context.Queue()
        def worker(product,configuration):
            session=None
            try:
                sid='hybrid-'+product.lower()
                connection=UnixExecutionConnection(server.path,sid,product.lower(),timeout=5)
                state=connection.call('describe',{})['state']
                remote=RemoteAccountService(connection,configuration,state)
                session=self.session(product,service=remote,driver=RemoteTraderSession(remote,'20261013'))
                session.client.ready_check=lambda:True
                session.runner.start();session.runner.accept_bars=True
                self.bar(session,'2026-10-13 09:01',1 if product=='RB' else .3)
                until=time.monotonic()+10
                target=2 if product=='RB' else 1
                key='rb_main' if product=='RB' else 'p_main'
                while session.client.position_manager.position(sid,key)!=target:
                    session.client.drain_events()
                    if time.monotonic()>until:raise RuntimeError('fixture fill timeout')
                    time.sleep(.01)
                queue.put(dict(product=product,pid=os.getpid(),quantity=target,
                    expires=session.strategy.holdings.active[0].expires_at_ns))
                session.runner.stop()
            except BaseException as error:
                queue.put(dict(product=product,error=repr(error)))
                raise
        running=threading.Event();running.set()
        failures=[]
        def control():
            try:
                while running.is_set():
                    server.poll();protocol.poll()
                    for ref in list(self.transport.active):self.transport.fill(ref)
                    time.sleep(.002)
            except BaseException as error:failures.append(error)
        thread=threading.Thread(target=control)
        processes=[context.Process(target=worker,args=(product,session.client.configuration()))
                   for product,session in [('RB',rb),('P',p)]]
        try:
            for process in processes:process.start()
            thread.start()
            rows=[queue.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(10);self.assertEqual(process.exitcode,0)
            self.assertFalse(failures,failures)
            self.assertFalse(any('error' in row for row in rows),rows)
            self.assertEqual(len({row['pid'] for row in rows}),2)
            self.assertNotIn(os.getpid(),{row['pid'] for row in rows})
            self.assertEqual({row['product']:row['quantity'] for row in rows},{'RB':2,'P':1})
            self.assertEqual(self.transport.starts,1)
            self.assertEqual(len(self.transport.sent),2)
            self.assertEqual({row['expires'] for row in rows},{hybrid_fixture.ns('2026-10-13 09:07')})
        finally:
            running.clear()
            if thread.ident is not None:thread.join(2)
            for process in processes:
                if process.is_alive():process.terminate()
                process.join(2)
            queue.close()

    def test_native_rb_import_preserves_original_orders_batches_and_initializes_p_without_new_orders(self):
        from copy import deepcopy
        from bomber.framework.trader.persistence import JsonStateStore
        from scripts.integration.shared_account.rollover import payload_for_day
        rb,p=self.session('RB'),self.session('P')
        self.start(rb,p)
        self.bar(rb,'2026-10-13 09:01',1)
        self.transport.fill(self.transport.sent[-1]['OrderRef']);rb.client.drain_events()
        original=rb.signal_checkpoint.hybrid.snapshot_state()
        gross=dict(self.transport.gross)
        for session in (rb,p):
            session.client.begin_stop();self.service.poll_stopping();session.client.drain_events()
            session.client.finish_stop_events();session.runner.stop()
        self.account.stop()
        store=JsonStateStore(self.root/'account.json');previous=store.load()
        native=deepcopy(previous.payload)
        native['component_states']={'hybrid':deepcopy(original),
            'live_session':{'version':1,'identity':rb.model_identity},
            'session_rules':{'version':1,'fingerprint':self.cal.provenance['sha256']}}
        saved=store.save(native,expected_generation=previous.generation)
        before=(self.root/'account.json').read_bytes()
        self.account,self.service,self.transport=self.make_account(preserve_external=True)
        self.transport.gross=gross
        restored_rb,restored_p=self.session('RB'),self.session('P')
        self.account.hybrid_sessions={'hybrid-rb':restored_rb,'hybrid-p':restored_p}
        self.account.rollover_transform=payload_for_day
        self.account.start(resume=True)
        self.addCleanup(self.account.driver.stop)
        self.assertFalse(self.transport.sent)
        self.assertEqual(self.service.position_manager.position('hybrid-rb','rb_main'),2)
        self.assertEqual(self.service.position_manager.position('hybrid-p','p_main'),0)
        for sid,session in self.account.hybrid_sessions.items():
            SharedStrategySession(session,self.root/(sid+'.json'),{'id':sid}).restore(self.service,resume=True)
        self.assertEqual(restored_rb.strategy.holdings.snapshot_state(),original['holdings'])
        self.assertFalse(restored_p.strategy.holdings.active)
        updated=store.load()
        self.assertEqual(updated.payload['orders'],native['orders'])
        backup=(self.root/'account.json').with_name(f'account.json.before-rollover-g{saved.generation}.json')
        self.assertEqual(backup.read_bytes(),before)
        self.assertIn('shared_account',updated.payload['component_states'])


class HybridLayoutConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'release').mkdir();(self.root/'directions.csv').write_text('formula,direction\nf,1\n')
        (self.root/'signals.json').write_text(json.dumps({'P':dict(signal_id='1101',signal_method='linear_signal',
            signal_params={'threshold':.05},hold_bars=5)}))
        self.path=self.root/'shared.json'
        self.config=dict(version=1,trading_day='20261013',environment='realtime',seconds=60,
            execution_mode='independent',position_scope='strategy_instruments',
            account_state_file='account.json',report_dir='reports',
            account=dict(client_id='account',max_gross_quantity=2,max_gross_notional=1000000,
                max_session_orders=100,limit_offset_ticks=1),strategies=[dict(strategy_id='hybrid-p',
                    kind='hybrid_transformer',state_file='signal.json',argv=['--product','P',
                        '--release-path','release','--directions-file','directions.csv','--signal-config','signals.json',
                        '--max-contracts','2','--entry-contracts','2','--market-source','ctp','--no-session-overrides'])])
        self.path.write_text(json.dumps(self.config))

    def test_paths_resolve_once_and_both_layouts_accept_same_config_without_model_loading(self):
        from scripts.integration.shared_account.config import read_config
        from scripts.integration.shared_account.factories import strategy_arguments
        parsed=read_config(self.path)
        with patch.object(shared,'previous_source_day',return_value='20261012'):
            entry,args=strategy_arguments(parsed['strategies'][0],parsed,resume=False)
        self.assertEqual(entry.__name__,live.__name__)
        self.assertEqual(args.product,'P');self.assertEqual(args.expected_source_day,'20261012')
        self.assertEqual(args.release_path,(self.root/'release').resolve())
        for layout in ('same-process','separate-processes'):launcher.validate_layout(parsed,layout)
        self.assertNotIn('process_layout',parsed)
        self.assertEqual(args.entry_contracts,2)

    def test_example_config_has_independent_dolphindb_market_and_history_for_rb_and_p(self):
        from scripts.integration.shared_account.config import read_config
        path=Path(__file__).resolve().parents[1]/'demos/10_hybrid_transformer/config/shared_rb_p.json'
        config=read_config(path)
        launcher.validate_layout(config,'same-process');launcher.validate_layout(config,'separate-processes')
        rb,p=config['strategies']
        self.assertEqual(rb['argv'][rb['argv'].index('--history-minutes')+1],'260')
        self.assertTrue(Path(rb['argv'][rb['argv'].index('--history-config')+1]).is_absolute())
        self.assertEqual(p['argv'][p['argv'].index('--history-minutes')+1],'260')
        self.assertEqual(p['argv'][p['argv'].index('--market-source')+1],'dolphindb')
        market_path=Path(p['argv'][p['argv'].index('--market-config')+1])
        history_path=Path(p['argv'][p['argv'].index('--history-config')+1])
        market=json.loads(market_path.read_text())
        history=json.loads(history_path.read_text())
        self.assertEqual(market['table'],'market_bar_state_v2_dce')
        self.assertEqual(market['volume_mode'],'per-bar')
        self.assertEqual((history['database'],history['table'],history['venue']),
            ('dfs://ctp','bar_1min_dce','DCE'))
        self.assertEqual(history['volume_mode'],'cumulative')
        rb_market=json.loads((path.parent/'market_dolphindb_rb.json').read_text())
        rb_history=json.loads((path.parent/'history_rb.json').read_text())
        self.assertEqual({**rb_market,'table':'market_bar_state_v2_dce'},market)
        self.assertEqual({**rb_history,'table':'bar_1min_dce','venue':'DCE'},history)
        product_signal=import_module('demos.10_hybrid_transformer.signal_config').load_product_signal(
            p['argv'][p['argv'].index('--signal-config')+1],'P')
        self.assertEqual((product_signal.signal_method,product_signal.signal_params['threshold']),('linear_signal',.05))
        self.assertEqual(rb['strategy_id'],config['account']['client_id'])

    def test_config_directory_uses_fresh_state_without_reading_old_rb_checkpoint(self):
        from scripts.integration.shared_account.config import read_config, strategy_report_dir
        root=Path(__file__).resolve().parents[1]
        directory=root/'demos/10_hybrid_transformer/config'
        config=read_config(directory/'shared_rb_p.json')
        self.assertEqual(Path(config['account_state_file']),root/'demos/10_hybrid_transformer/results/live/state/account_rb_p.json')
        self.assertEqual(config['existing_position_policy'],'preserve_external')
        self.assertEqual(Path(config['report_dir']),root/'demos/10_hybrid_transformer/results/live')
        for row,product in zip(config['strategies'],('RB','P')):
            self.assertEqual(Path(row['state_file']),root/f'demos/10_hybrid_transformer/results/live/state/{product.lower()}_signal.json')
            self.assertEqual(Path(strategy_report_dir(row,config)),root/f'demos/10_hybrid_transformer/results/live/{product}')
            args=row['argv']
            for flag in ('--market-config','--signal-config','--history-config'):
                path=Path(args[args.index(flag)+1])
                self.assertEqual(path.parent,directory)
                self.assertTrue(path.is_file(),str(path))

    def test_native_entry_explains_shared_account_format_without_touching_state(self):
        lifecycle=object.__new__(live.HybridSessionLifecycle)
        session=NS(manager=NS(repository=NS(load=lambda:NS(payload={'component_states':{'shared_account':{}}}))))
        with self.assertRaisesRegex(ValueError,'run_shared'):
            lifecycle.restore_session(session,NS(trading_day='20261013'))

    def test_each_product_report_dir_is_resolved_and_shared_by_host_and_worker(self):
        from copy import deepcopy
        from scripts.integration.shared_account.config import read_config, strategy_report_dir
        for flags in (['--report-dir','p_reports'],['--report-dir=p_reports']):
            raw=deepcopy(self.config)
            raw['strategies'][0]['argv']+=flags
            rb=deepcopy(raw['strategies'][0]);rb['strategy_id']='hybrid-rb';rb['state_file']='rb.json'
            rb['argv']=list(self.config['strategies'][0]['argv'])
            raw['strategies'].append(rb)
            self.path.write_text(json.dumps(raw))
            parsed=read_config(self.path)
            self.assertEqual(strategy_report_dir(parsed['strategies'][0],parsed),str((self.root/'p_reports').resolve()))
            self.assertEqual(strategy_report_dir(parsed['strategies'][1],parsed),parsed['report_dir'])
        raw=deepcopy(self.config);raw['strategies'][0]['argv']+=['--report-dir']
        self.path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError,'缺少文件路径'):read_config(self.path)

    def test_offline_cli_exercises_both_layout_options_without_connections(self):
        for layout in ('same-process','separate-processes'):
            with redirect_stdout(StringIO()) as output:
                launcher.main(['--config',str(self.path),'--process-layout',layout])
            self.assertIn('未连接',output.getvalue())

    def test_prepare_seconds_defaults_and_override_reach_calendar_scheduler(self):
        from contextlib import nullcontext
        import threading
        from bomber.framework.trader.runtime.live import continuous
        for extra, seconds in (([],300),(['--prepare-seconds','600'],600),(['--prepare-seconds','0'],0)):
            with self.subTest(seconds=seconds), redirect_stdout(StringIO()) as output, \
                    patch.object(launcher,'load_calendar',return_value=object()), \
                    patch('dotenv.load_dotenv'), \
                    patch('bomber.framework.trader.runtime.live.local.strategy_state_lock',return_value=nullcontext()), \
                    patch.object(continuous,'StopSignal',return_value=nullcontext(threading.Event())), \
                    patch.object(continuous,'ContinuousPositionRuntime') as runtime:
                launcher.main(['--config',str(self.path),'--run-forever','--connect',
                    '--enable-orders','--confirm-simnow',*extra])
                self.assertEqual(runtime.call_args.kwargs['prepare_seconds'],seconds)
                self.assertIn(f'prepare_seconds={seconds}',output.getvalue())
                self.assertFalse((self.root/'state').exists())

    def test_negative_prepare_seconds_is_rejected_before_connections(self):
        from contextlib import redirect_stderr
        with redirect_stderr(StringIO()), patch.object(launcher,'load_calendar') as load, \
                self.assertRaises(SystemExit):
            launcher.main(['--config',str(self.path),'--prepare-seconds','-1'])
        load.assert_not_called()

    def test_layout_change_cannot_silently_change_netting_policy(self):
        self.config['execution_mode']='netted'
        for layout in ('same-process','separate-processes'):
            with self.assertRaisesRegex(ValueError,'independent'):launcher.validate_layout(self.config,layout)

    def test_calendar_validation_keeps_product_calendars_and_rejects_different_hours(self):
        from scripts.integration.shared_account.config import read_config
        from copy import deepcopy
        other=deepcopy(self.config['strategies'][0]);other['strategy_id']='hybrid-other';other['state_file']='other.json'
        self.config['strategies'].append(other);self.path.write_text(json.dumps(self.config))
        parsed=read_config(self.path)
        first=hybrid_fixture.calendar()
        different=SessionCalendar(first.hours,first.calendar,first.binding,
            max_minutes=first.max_minutes if hasattr(first,'max_minutes') else 10000)
        different.segments=tuple(replace(segment,end_time='11:00') if segment.segment_id=='am' else segment
                                 for segment in different.segments)
        with patch.object(shared,'previous_source_day',return_value='20261012'), \
                patch('bomber.framework.dataprep.session_inputs.load_session_inputs',side_effect=[first,first]):
            self.assertIs(launcher.load_calendar(parsed),first)
        with patch.object(shared,'previous_source_day',return_value='20261012'), \
                patch('bomber.framework.dataprep.session_inputs.load_session_inputs',side_effect=[first,different]):
            with self.assertRaisesRegex(ValueError,'时段不同'):launcher.load_calendar(parsed)

    def test_shared_config_rejects_ignored_history_overrides_for_hybrid(self):
        from scripts.integration.shared_account.config import read_config
        for key,value in [('strategy_history_minutes',{'hybrid-p':260}),('history_missing_policy','fail')]:
            candidate={**self.config,key:value};self.path.write_text(json.dumps(candidate))
            with self.assertRaisesRegex(ValueError,'Hybrid历史根数'):read_config(self.path)

    def test_same_process_connects_and_prewarms_before_open_but_waits_before_decisions(self):
        from datetime import timedelta
        from unittest.mock import Mock
        import threading
        window = hybrid_fixture.TradingWindow(trading_day='20261013', segment_id='am',
            start=hybrid_fixture.dt('2026-10-13 09:00'), end=hybrid_fixture.dt('2026-10-13 11:30'))
        now = [window.start-timedelta(minutes=5)]
        host = Mock()
        host.strategies = {'rb': NS(active=True,session=NS(runner=NS()))}
        host.snapshot.return_value = {}
        host.account.execution.backend.reports = ()
        calls = []
        def poll_preparation():
            calls.append('preopen')
            self.assertLess(now[0], window.start)
            now[0] = window.start
        host.poll_preparation.side_effect = poll_preparation
        def start():
            self.assertEqual(now[0], window.start-timedelta(minutes=5))
            host.activation_wait()
            self.assertEqual(now[0], window.start)
            calls.append('open')
        host.start.side_effect = start
        host.poll.side_effect = lambda: now.__setitem__(0, window.end-timedelta(seconds=30))
        with patch('scripts.integration.shared_account.run.build_host',return_value=host), \
                patch('bomber.framework.trader.runtime.reports.TradingDayLiveReport'):
            result = launcher.run_same_process(self.config,window,threading.Event(),
                resume=False,remaining=100,clock=lambda:now[0])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(calls, ['preopen', 'open'])
        self.assertEqual(launcher.PREPARE_SECONDS, 300)
        host.stop.assert_called_once()

    def test_same_process_begins_stop_thirty_seconds_before_close(self):
        from datetime import timedelta
        from unittest.mock import Mock
        import threading
        window = hybrid_fixture.TradingWindow('20261013', 'am',
            hybrid_fixture.dt('2026-10-13 09:00'), hybrid_fixture.dt('2026-10-13 11:30'))
        now = [window.end-timedelta(seconds=31)]
        host = Mock()
        host.strategies = {'rb': NS(active=True,session=NS(runner=NS()))}
        host.snapshot.return_value = {}
        host.account.execution.backend.reports = ()
        host.poll.side_effect = lambda: now.__setitem__(0, window.end-timedelta(seconds=30))
        with patch('scripts.integration.shared_account.run.build_host',return_value=host), \
                patch('bomber.framework.trader.runtime.reports.TradingDayLiveReport'):
            result = launcher.run_same_process(self.config,window,threading.Event(),
                resume=False,remaining=100,clock=lambda:now[0])
        self.assertEqual(result['status'],'completed')
        host.poll.assert_called_once()
        host.stop.assert_called_once()
        self.assertLess(now[0],window.end)
        self.assertEqual(host.strategies['rb'].session.runner.continuous_window,window)

    def test_all_stopped_exception_includes_each_strategy_failure(self):
        from unittest.mock import Mock
        import threading
        window = hybrid_fixture.TradingWindow(trading_day='20261013', segment_id='am',
            start=hybrid_fixture.dt('2026-10-13 09:00'), end=hybrid_fixture.dt('2026-10-13 11:30'))
        host = Mock()
        host.strategies = {'rb': NS(active=False,failure='价格缺失',session=NS(runner=NS())),
            'p': NS(active=False,failure='行情超时',session=NS(runner=NS()))}
        host.snapshot.return_value = {}
        host.account.execution.backend.reports = ()
        with patch('scripts.integration.shared_account.run.build_host',return_value=host), \
                patch('bomber.framework.trader.runtime.reports.TradingDayLiveReport'):
            with self.assertRaisesRegex(RuntimeError, 'rb: 价格缺失.*p: 行情超时'):
                launcher.run_same_process(self.config,window,threading.Event(),
                    resume=False,remaining=100,clock=lambda:window.start)
        host.stop.assert_called_once()

    def test_same_process_scheduled_query_interruption_is_normal_stop(self):
        from datetime import timedelta
        from unittest.mock import Mock
        from bomber.framework.trader.execution.ctp.query_errors import CtpQueryInterrupted
        import threading
        window = hybrid_fixture.TradingWindow('20261013','am',
            hybrid_fixture.dt('2026-10-13 09:00'), hybrid_fixture.dt('2026-10-13 11:30'))
        now=[window.end-timedelta(seconds=31)]
        host=Mock()
        host.strategies={'rb':NS(active=True,session=NS(runner=NS()))}
        host.snapshot.return_value={}
        host.account.execution.backend.reports=()
        def delayed_query():
            now[0]=window.end-timedelta(seconds=30)
            self.assertTrue(host.account.transport.query_stop_requested())
            raise CtpQueryInterrupted('fixture scheduled stop')
        host.poll.side_effect=delayed_query
        with patch('scripts.integration.shared_account.run.build_host',return_value=host), \
                patch('bomber.framework.trader.runtime.reports.TradingDayLiveReport'):
            result=launcher.run_same_process(self.config,window,threading.Event(),
                resume=False,remaining=100,clock=lambda:now[0])
        self.assertEqual(result['status'],'completed')
        host.stop.assert_called_once()

    def test_same_process_does_not_start_during_close_grace_period(self):
        from datetime import timedelta
        import threading
        window = hybrid_fixture.TradingWindow('20261013', 'am',
            hybrid_fixture.dt('2026-10-13 09:00'), hybrid_fixture.dt('2026-10-13 11:30'))
        with patch('scripts.integration.shared_account.run.build_host') as build, \
                patch('bomber.framework.trader.runtime.reports.TradingDayLiveReport'):
            result=launcher.run_same_process(self.config,window,threading.Event(),
                resume=False,remaining=100,clock=lambda:window.end-timedelta(seconds=20))
        self.assertEqual(result['status'],'completed')
        build.assert_not_called()

    def test_layout_window_uses_same_account_and_signal_paths_and_deducts_daily_budget(self):
        from scripts.integration.shared_account.config import read_config
        from bomber.framework.trader.persistence import JsonStateStore
        import threading
        parsed=read_config(self.path)
        JsonStateStore(parsed['account_state_file']).save({'component_states':{'shared_account':{}},
            'orders':{'account':[{'client_order_id':'CTP-9999-fixture-20261013-7'}]}},expected_generation=0)
        window=hybrid_fixture.TradingWindow('20261013','am',hybrid_fixture.dt('2026-10-13 09:00'),
            hybrid_fixture.dt('2026-10-13 11:30'))
        with patch.object(launcher,'run_same_process',return_value={'status':'completed'}) as same:
            launcher.run_window(parsed,'same-process',None,window,threading.Event())
            self.assertEqual(same.call_args.kwargs['remaining'],99)
            self.assertTrue(same.call_args.kwargs['resume'])
        with patch('scripts.integration.shared_account.continuous.ProcessWindowRuntime') as separate:
            separate.return_value.run.return_value={'status':'completed'}
            launcher.run_window(parsed,'separate-processes',None,window,threading.Event())
            self.assertEqual(separate.call_args.args[3],99)
            self.assertEqual(separate.call_args.kwargs['stop_before_seconds'],30)
            self.assertEqual(same.call_args.args[0]['account_state_file'],separate.call_args.args[0]['account_state_file'])
            self.assertEqual(same.call_args.args[0]['strategies'],separate.call_args.args[0]['strategies'])


if __name__=='__main__':
    unittest.main()
