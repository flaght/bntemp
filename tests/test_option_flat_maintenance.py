"""期权维护复用公共CTP流程；保留选约/对冲标记，实际恢复不重放旧组合。"""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date
from importlib import import_module
from io import StringIO
import json
import unittest
from unittest.mock import patch

from tests import test_option_simnow_migrate as fixture
from tests import test_option_shared_rollover as shared_fixture
from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.runtime.live.shared import SharedStrategySession

repair=import_module('demos.08_option_vega.reconcile_flat')


class OptionFlatMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.MigrationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        saved=JsonStateStore(self.f.source).load();payload=deepcopy(saved.payload)
        payload['component_states']['vega_signal']['hedge_sent']=['2026-08-13_14:00']
        self.original=JsonStateStore(self.f.source).save(payload,expected_generation=saved.generation)
        self.before=self.f.source.read_bytes()
        self.transport=fixture.Transport('20260813')
        self.transport.query_gross_positions=lambda:{'ag2612.SHFE':(0,1)}

    def run_repair(self,archive=False):
        with patch('bomber.framework.trader.runtime.live.channels.ctp_shared.CtpSharedAccountLifecycle.acquire'),redirect_stdout(StringIO()):
            return repair.reconcile(state_file=self.f.source,transport=self.transport,archive_unknown_close=archive)

    def test_native_keeps_execution_history_parameters_and_processed_slots_and_restores_actual_strategy(self):
        saved=self.run_repair()
        old=self.original.payload['component_states']['vega_signal']
        signal=saved.payload['component_states']['vega_signal']
        self.assertEqual(signal['sent_days'],old['sent_days'])
        self.assertEqual(signal['hedge_sent'],old['hedge_sent'])
        for name in ('submissions','entry_decisions','flatten_requested'):
            self.assertEqual(signal[name],old[name])
        self.assertTrue(all(not int(q) for key in ('option_goals','future_goals') for q in signal[key].values()))
        self.assertEqual(saved.payload['orders'],self.original.payload['orders'])
        self.assertEqual(saved.payload['component_states']['session_identity'],self.original.payload['component_states']['session_identity'])
        args=fixture.entry.parse_args(['--connect','--mode','simnow','--enable-orders','--confirm-simnow',
            '--expected-trading-day','20260813','--state-file',str(self.f.source),'--resume','--option-slot','13:36',
            '--delta-targets','0.25','--frame-wait-seconds','20'])
        session=fixture.runtime.assemble(args,fixture.inputs(date(2026,8,13),'ctp'),
            fixture.CtpNativeTraderDriver(fixture.CLIENT,'fixture',self.transport))
        session.manager.restore()
        import_module('demos.08_option_vega.recovery').validate_restored_session(session,'20260813')
        self.assertEqual(session.strategy.option_goals['MO2609-C-6600'],0)
        self.assertIn(date(2026,8,13),session.strategy.sent_days)
        self.assertIn('2026-08-13_14:00',session.strategy.hedge_sent)
        self.assertEqual(session.runner.position_manager.position(fixture.CLIENT,fixture.OPTION),0)
        self.assertEqual(self.transport.sent,[])
        self.assertEqual(self.transport.canceled,[])
        backup=next(self.f.root.glob('*.before-flat-alignment-*.bak'))
        self.assertEqual(backup.read_bytes(),self.before)

    def test_multileg_nonzero_hedge_or_active_order_or_failed_query_keeps_original(self):
        from types import SimpleNamespace as NS
        for stage in ('hedge','active','query'):
            with self.subTest(stage=stage):
                self.transport.query_gross_positions=lambda:{}
                self.transport.query_active_orders=lambda:NS(orders=())
                if stage=='hedge':self.transport.query_gross_positions=lambda:{'IM2609.CFFEX':(1,0)}
                if stage=='active':self.transport.query_active_orders=lambda:NS(orders=(NS(instrument_id='IM2609.CFFEX'),))
                if stage=='query':self.transport.query_gross_positions=lambda:(_ for _ in ()).throw(TimeoutError('query failed'))
                with self.assertRaises((RuntimeError,TimeoutError)):self.run_repair()
                self.assertEqual(self.f.source.read_bytes(),self.before)
                self.assertFalse(list(self.f.root.glob('*.before-flat-alignment-*.bak')))

    def pending_close(self,payload):
        client=fixture.CLIENT;order_id='CTP-9999-fixture-20260813-9'
        payload['orders'][client].append(dict(backend_id=client,client_order_id=order_id,instrument_id=fixture.OPTION,
            side='BUY',order_quantity='1',filled_quantity='0',remaining_quantity='1',status='ACCEPTED',
            last_sequence=1,last_ts_event=2,seen_keys=['old-close:1']))
        payload['ctp_drivers'][client]['orders']=[dict(order_ref='9',client_order_id=order_id,filled_quantity='0',
            sequence=1,accepted=True,seen_trades=[],intent=dict(strategy_id=client,backend_id=client,
                instrument_id=fixture.OPTION,side='BUY',quantity='1',order_type='LIMIT',price='82.2',
                position_effect='CLOSE',reduce_only=True,metadata={}))]
        payload['positions']['working_quantities']=[dict(key=dict(client_id=client,instrument_id=fixture.OPTION),quantity='1')]
        payload['positions']['attribution']['orders']['pending-close']=dict(client_id=client,instrument_id=fixture.OPTION,
            direction=1,quantity='1',client_order_id=order_id,released=False,
            legs=[dict(strategy_id=client,target_key=fixture.OPTION,quantity='1',filled='0',target_revision=1)])
        return order_id

    def test_pending_close_needs_explicit_archive_and_later_day_and_does_not_fabricate_fill(self):
        payload=deepcopy(self.original.payload);order_id=self.pending_close(payload)
        self.original=JsonStateStore(self.f.source).save(payload,expected_generation=self.original.generation)
        self.before=self.f.source.read_bytes()
        with self.assertRaises(ValueError):self.run_repair()
        with self.assertRaisesRegex(ValueError,'后续交易日'):self.run_repair(archive=True)
        self.assertEqual(self.f.source.read_bytes(),self.before)
        self.transport.day='20260814'
        saved=self.run_repair(archive=True)
        self.assertFalse(saved.payload['ctp_drivers'][fixture.CLIENT]['orders'])
        self.assertFalse(any(r['client_order_id']==order_id for r in saved.payload['orders'][fixture.CLIENT]))
        audit=json.loads(next(self.f.root.glob('*.flat-alignment-*.json')).read_text())
        self.assertEqual(audit['old_order_terminal_status'],'UNKNOWN')
        self.assertFalse(audit['realized_pnl_reconstructed'])
        self.assertEqual(audit['orders_submitted'],0)
        self.assertEqual(saved.payload['component_states']['vega_signal']['hedge_sent'],['2026-08-13_14:00'])
        self.assertEqual(self.transport.sent,[])

    def test_archive_refuses_old_open_order(self):
        payload=deepcopy(self.original.payload);self.pending_close(payload)
        payload['ctp_drivers'][fixture.CLIENT]['orders'][0]['intent']['position_effect']='OPEN'
        with self.assertRaisesRegex(ValueError,'旧平仓单'):
            repair.aligned_payload(payload,trading_day='20260814',archive_unknown_close=True)
        self.assertEqual(self.f.source.read_bytes(),self.before)


class OptionSharedFlatMaintenanceTests(unittest.TestCase):
    def test_shared_multi_instrument_signal_file_lag_uses_aligned_business_copy_and_preserves_marks(self):
        f=shared_fixture.OptionSharedRolloverTests();f.setUp();self.addCleanup(f.doCleanups)
        account,service,session,identity,transport=f.shared(preserve_external=True)
        transport.bucket='2'
        with redirect_stdout(StringIO()):account.start(resume=True)
        self.addCleanup(account.driver.stop)
        row=SharedStrategySession(session,f.root/'signal.json',identity)
        row.restore(service,resume=True)
        session.strategy.sent_days.add(date(2026,8,14))
        session.strategy.hedge_sent.add('2026-08-14_14:00')
        row.save(service)
        account_config=dict(client_id=fixture.CLIENT,max_gross_quantity=10,max_gross_notional=10000000,
            max_session_orders=200,limit_offset_ticks=1)
        account.manager.state_components['session_identity'].identity['account']=account_config
        original=account.manager.save()
        original=JsonStateStore(f.source).load()
        signal_bytes=row.state_file.read_bytes()
        config=dict(account=account_config,environment='realtime',existing_position_policy='preserve_external',
            account_state_file=str(f.source),strategies=[dict(strategy_id=fixture.CLIENT,kind='option_vega',state_file=str(row.state_file))])
        observer=shared_fixture.DetailsTransport('20260814')
        observer.query_gross_positions=lambda:{'ag2612.SHFE':(0,1)}
        with patch('bomber.framework.trader.runtime.live.channels.ctp_shared.CtpSharedAccountLifecycle.acquire'),redirect_stdout(StringIO()):
            saved=repair.reconcile_shared(config,strategy_id=fixture.CLIENT,transport=observer)
        self.assertEqual(row.state_file.read_bytes(),signal_bytes)
        source=saved.payload['component_states']['shared_account']['sources'][fixture.CLIENT]
        aligned=source['metadata']['checkpoint_alignment']['signal']
        self.assertEqual(aligned['option_goals']['MO2609-C-6600'],'0')
        self.assertIn('2026-08-14',aligned['sent_days'])
        self.assertIn('2026-08-14_14:00',aligned['hedge_sent'])
        self.assertEqual(saved.payload['component_states']['external_positions'],original.payload['component_states']['external_positions'])
        # 无已确认副本时，旧文件的选约/对冲标记不能凭执行仓位推测。
        invalid=deepcopy(saved.payload)
        invalid['component_states']['shared_account']['sources'][fixture.CLIENT]['metadata'].pop('checkpoint_alignment')
        latest=JsonStateStore(f.source).save(invalid,expected_generation=saved.generation)
        before=f.source.read_bytes()
        with patch('bomber.framework.trader.runtime.live.channels.ctp_shared.CtpSharedAccountLifecycle.acquire'):
            with self.assertRaisesRegex(ValueError,'不能猜测'):
                repair.reconcile_shared(config,strategy_id=fixture.CLIENT,transport=observer)
        self.assertEqual(f.source.read_bytes(),before)
        JsonStateStore(f.source).save(saved.payload,expected_generation=latest.generation)
        # 真正走账户及业务恢复；不能因旧信号文件仍写着空1手而恢复旧目标。
        observer.query_positions=lambda:{'ag2612.SHFE':-1}
        observer.query_position_details=lambda:[dict(InstrumentID='ag2612',ExchangeID='SHFE',Position=1,
            PosiDirection='3',PositionDate='2',HedgeFlag='1',PositionCost=10000)]
        a,s,restored,new_identity,t=f.shared(transport=observer,preserve_external=True)
        a.manager.state_components['session_identity'].identity['account']=account_config
        with redirect_stdout(StringIO()):
            a.start(resume=True)
            SharedStrategySession(restored,row.state_file,new_identity).restore(s,resume=True)
        self.addCleanup(a.driver.stop)
        self.assertEqual(restored.strategy.option_goals['MO2609-C-6600'],0)
        self.assertIn(date(2026,8,14),restored.strategy.sent_days)
        self.assertIn('2026-08-14_14:00',restored.strategy.hedge_sent)
        self.assertEqual(s.position_manager.position(fixture.CLIENT,fixture.OPTION),0)
        decisions=restored.strategy.entry_decisions
        s.start();restored.runner.start();a.authorize()
        self.addCleanup(restored.runner.stop)
        with redirect_stdout(StringIO()):
            restored.runner.begin_bars()
            f.frame(restored,slot='13:36')
            f.frame(restored,slot='13:37')
        self.assertGreater(restored.strategy.frames,0)
        self.assertEqual(restored.strategy.entry_decisions,decisions)
        self.assertEqual(restored.strategy.last_decision['entry'],'ALREADY_SELECTED')
        self.assertFalse(t.sent)


if __name__=='__main__':unittest.main()
