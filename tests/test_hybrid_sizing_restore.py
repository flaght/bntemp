"""空仓直接改两个手数参数；只读柜台替身，验证实际联合恢复、备份及拒绝路径。"""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import json
import unittest

from tests import test_hybrid_manual_flat as manual_fixture
from tests.test_hybrid_continuous import checkpoint, live
from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.runtime.live.continuous import orders_for_day


class SizingRestoreTests(unittest.TestCase):
    def setUp(self):
        old=manual_fixture.ManualFlatTests('runTest')
        old.setUp()
        self.addCleanup(old.doCleanups)
        self.old=old
        self.f=old.f
        self.store=JsonStateStore(self.f.path)
        saved=self.store.load()
        flat=manual_fixture.maintenance.aligned_payload(saved.payload,trading_day='20261028')
        self.saved=self.store.save(flat,expected_generation=saved.generation)
        self.before=self.f.path.read_bytes()
        self.session,self.driver,self.transport=self.f.session(mode='simnow',policy='off',
            max_contracts=2,entry_contracts=2)
        self.transport.quantity=0
        self.lifecycle=object.__new__(live.HybridSessionLifecycle)
        self.lifecycle.window=None
        self.lifecycle.driver,self.lifecycle.transport=self.driver,self.transport

    def restore(self):
        with redirect_stdout(StringIO()):
            return self.lifecycle.restore_session(self.session,NS(trading_day='20261028'))

    def test_flat_resize_is_staged_then_committed_with_original_backup_and_order_budget(self):
        self.restore()
        self.assertEqual(self.f.path.read_bytes(),self.before)
        self.assertEqual((self.session.strategy.config.max_contracts,
                          self.session.strategy.config.max_entry_contracts),(2,2))
        self.assertFalse(self.session.strategy.holdings.active)
        self.driver.start(lambda _:None)
        self.addCleanup(self.driver.stop)
        self.driver.reconcile_account_state()
        self.driver.reconcile_active_orders()
        self.session.manager.commit_restore()
        updated=self.store.load()
        hybrid=updated.payload['component_states']['hybrid']
        self.assertEqual((hybrid['holdings']['max_contracts'],hybrid['holdings']['max_entry_contracts']),(2,2))
        self.assertEqual(updated.payload['orders'],self.saved.payload['orders'])
        self.assertEqual(orders_for_day(updated.payload,'20261028'),1)
        self.assertEqual(updated.payload['component_states']['live_session'],
                         self.saved.payload['component_states']['live_session'])
        backup=self.f.path.with_name(self.f.path.name+f'.before-rollover-g{self.saved.generation}.json')
        self.assertEqual(backup.read_bytes(),self.before)
        self.assertEqual(updated.generation,self.saved.generation+1)

    def test_original_payload_is_not_mutated_by_sizing_transform(self):
        config=live.HybridCheckpoint(self.session.strategy).config()
        old=deepcopy(self.saved.payload)
        with redirect_stdout(StringIO()):
            transformed=checkpoint.resize_flat_checkpoint(old,config)
        self.assertEqual(old,self.saved.payload)
        expected=deepcopy(old)
        for name in ('config','holdings'):
            expected['component_states']['hybrid'][name].update(max_contracts=2,max_entry_contracts=2)
        self.assertEqual(transformed,expected)

    def test_nonflat_or_pending_or_working_or_live_orders_cannot_be_resized(self):
        variants=[]
        variants.append(deepcopy(self.old.saved.payload))  # 原未平的6手和三个批次。
        pending=deepcopy(self.saved.payload)
        pending['component_states']['hybrid']['targets_pending']=True
        variants.append(pending)
        working=deepcopy(self.saved.payload)
        working['positions']['working_quantities']=[dict(
            key=dict(client_id=live.CLIENT_ID,instrument_id='rb2701.SHFE'),quantity='1')]
        variants.append(working)
        orders=deepcopy(self.saved.payload)
        orders['orders'][live.CLIENT_ID][0].update(status='ACCEPTED',filled_quantity='0',remaining_quantity='6')
        variants.append(orders)
        for payload in variants:
            with self.subTest(payload=variants.index(payload)):
                original=deepcopy(payload)
                with self.assertRaisesRegex(ValueError,'请先用原参数处理旧仓'):
                    checkpoint.resize_flat_checkpoint(payload,live.HybridCheckpoint(self.session.strategy).config())
                self.assertEqual(payload,original)
        self.assertEqual(self.f.path.read_bytes(),self.before)

    def test_broker_gross_on_managed_contract_blocks_resize_even_if_net_is_zero(self):
        self.transport.query_gross_positions=Mock(return_value={'RB2701.SHFE':(1,1)})
        with self.assertRaisesRegex(RuntimeError,'管理合约柜台仍有持仓'):
            self.restore()
        self.assertEqual(self.f.path.read_bytes(),self.before)
        self.assertFalse(list(self.f.root.glob('*.before-rollover-*')))

    def test_external_other_contract_positions_do_not_block_resize(self):
        self.transport.query_gross_positions=Mock(return_value={
            'IF2611.CFFEX':(1,0),'sc2611.INE':(1,0),'IM2611.CFFEX':(0,2)})
        self.restore()
        self.assertFalse(self.session.strategy.holdings.active)
        self.assertEqual(self.f.path.read_bytes(),self.before)

    def test_changed_model_identity_is_still_rejected_without_touching_original(self):
        payload=deepcopy(self.saved.payload)
        payload['component_states']['live_session']['identity']['min_raw_bars']=300
        self.store.save(payload,expected_generation=self.saved.generation)
        original=self.f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'min_raw_bars'):
            self.restore()
        self.assertEqual(self.f.path.read_bytes(),original)
        self.assertFalse(list(self.f.root.glob('*.before-rollover-*')))

    def test_other_strategy_parameters_are_not_implicitly_migrated(self):
        config=live.HybridCheckpoint(self.session.strategy).config()
        config['hold_bars']=3
        transformed=checkpoint.resize_flat_checkpoint(self.saved.payload,config)
        self.assertIs(transformed,self.saved.payload)
        self.assertEqual(self.f.path.read_bytes(),self.before)

    def test_cross_day_can_resize_flat_state_while_preserving_terminal_orders(self):
        self.session,self.driver,self.transport=self.f.session('20261029',mode='simnow',policy='off',
            max_contracts=2,entry_contracts=2)
        self.transport.quantity=0
        self.lifecycle.driver,self.lifecycle.transport=self.driver,self.transport
        self.lifecycle.window=NS()
        with redirect_stdout(StringIO()):
            self.lifecycle.restore_session(self.session,NS(trading_day='20261029'))
        self.assertEqual(self.session.strategy.config.max_entry_contracts,2)
        self.assertEqual(self.session.ledger.state().trading_day,'20261029')
        self.assertEqual(self.session.client.order_state_machine.checkpoints()[0].state.status.value,'FILLED')
        self.assertEqual(self.f.path.read_bytes(),self.before)

    def seed_nonflat(self):
        saved=self.store.load()
        self.store.save(self.old.saved.payload,expected_generation=saved.generation)
        self.before=self.f.path.read_bytes()
        self.lifecycle.confirm_manual_flat=True

    def test_confirmed_manual_flat_syncs_and_resizes_in_one_restore_with_backup(self):
        self.seed_nonflat()
        self.restore()
        self.assertEqual(self.f.path.read_bytes(),self.before)
        self.assertFalse(self.session.strategy.holdings.active)
        self.assertEqual(self.session.strategy.last_targets,{})
        self.assertEqual(self.session.ledger.snapshot(self.session.references.instrument_id).short_total,0)
        self.assertEqual(self.session.manual_flat_sync['previous_targets'],{'rb2701':'-6'})
        self.assertEqual(self.session.manual_flat_sync['previous_batches'],3)
        self.assertIsNone(self.session.manual_flat_sync['actual_close_prices'])
        self.driver.start(lambda _:None)
        self.addCleanup(self.driver.stop)
        self.driver.reconcile_account_state()
        self.driver.reconcile_active_orders()
        self.session.manager.commit_restore()
        updated=self.store.load()
        self.assertEqual(updated.payload['orders'],self.old.saved.payload['orders'])
        self.assertEqual(orders_for_day(updated.payload,'20261028'),1)
        config=updated.payload['component_states']['hybrid']['config']
        self.assertEqual((config['max_contracts'],config['max_entry_contracts']),(2,2))
        backup=next(self.f.root.glob('*.before-rollover-*.json'))
        self.assertEqual(backup.read_bytes(),self.before)

    def test_manual_flat_can_clear_old_day_records_before_normal_rollover(self):
        self.seed_nonflat()
        self.session,self.driver,self.transport=self.f.session('20261029',mode='simnow',policy='off',
            max_contracts=2,entry_contracts=2)
        self.transport.quantity=0
        self.lifecycle.driver,self.lifecycle.transport=self.driver,self.transport
        self.lifecycle.window=NS()
        with redirect_stdout(StringIO()):
            self.lifecycle.restore_session(self.session,NS(trading_day='20261029'))
        self.assertFalse(self.session.strategy.holdings.active)
        self.assertEqual(self.session.strategy.last_targets,{})
        self.assertEqual(self.session.ledger.state().trading_day,'20261029')
        self.assertEqual(self.session.manual_flat_sync['previous_trading_day'],'20261028')
        self.assertEqual(self.session.manual_flat_sync['observed_trading_day'],'20261029')
        self.assertEqual(self.f.path.read_bytes(),self.before)

    def test_manual_flat_also_handles_existing_two_by_two_state_without_resizing(self):
        self.seed_nonflat()
        saved=self.store.load()
        payload=deepcopy(saved.payload)
        hybrid=payload['component_states']['hybrid']
        hybrid['config'].update(max_contracts=2,max_entry_contracts=2)
        hybrid['holdings'].update(max_contracts=2,max_entry_contracts=2)
        hybrid['holdings']['active']=hybrid['holdings']['active'][:1]
        hybrid['last_targets']={'rb2701':'-2'}
        for key in ('strategy_positions','account_positions'):
            for row in payload['positions'][key]:
                row['quantity']='-2'
        payload['positions']['attribution']['owned'][0]['quantity']='-2'
        plan=payload['positions']['attribution']['orders']['old-filled-plan']
        plan['quantity']='2'
        plan['legs'][0].update(quantity='2',filled='2')
        payload['ctp_ledgers'][live.CLIENT_ID]['positions'][0]['short_today']='2'
        payload['targets'][0]['targets']['rb_main']='-2'
        payload['portfolio']['contributions'][live.CLIENT_ID][0]['quantity']='-2'
        payload['orders'][live.CLIENT_ID][0].update(order_quantity='2',filled_quantity='2')
        self.store.save(payload,expected_generation=saved.generation)
        original=self.f.path.read_bytes()
        self.restore()
        self.assertFalse(self.session.strategy.holdings.active)
        self.assertEqual(self.session.manual_flat_sync['previous_targets'],{'rb2701':'-2'})
        self.assertEqual(self.session.strategy.config.max_entry_contracts,2)
        self.assertEqual(self.f.path.read_bytes(),original)

    def test_manual_confirmation_still_requires_real_flat_and_no_active_order(self):
        self.seed_nonflat()
        self.transport.quantity=1
        with self.assertRaisesRegex(RuntimeError,'柜台仍有持仓'):
            self.restore()
        self.assertEqual(self.f.path.read_bytes(),self.before)
        self.transport.quantity=0
        self.transport.query_active_orders=Mock(return_value=NS(orders=(NS(instrument_id='RB2701.SHFE'),)))
        with self.assertRaisesRegex(RuntimeError,'柜台仍有活动订单'):
            self.restore()
        self.assertEqual(self.f.path.read_bytes(),self.before)
        self.assertFalse(list(self.f.root.glob('*.before-rollover-*')))

    def test_manual_confirmation_does_not_invent_terminal_order_updates(self):
        self.seed_nonflat()
        saved=self.store.load()
        payload=deepcopy(saved.payload)
        payload['orders'][live.CLIENT_ID][0].update(status='ACCEPTED',filled_quantity='0',remaining_quantity='6')
        self.store.save(payload,expected_generation=saved.generation)
        before=self.f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'仍有未结束订单'):
            self.restore()
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_pending_unsent_target_can_be_discarded_after_confirmed_broker_flat(self):
        self.seed_nonflat()
        saved=self.store.load()
        payload=deepcopy(saved.payload)
        payload['component_states']['hybrid']['targets_pending']=True
        self.store.save(payload,expected_generation=saved.generation)
        self.restore()
        self.assertFalse(self.session.strategy._targets_pending)
        self.assertFalse(self.session.strategy.holdings.active)
        self.assertEqual(self.session.client.order_state_machine.checkpoints()[0].state.status.value,'FILLED')

    def test_manual_flat_keeps_other_contract_positions_and_model_identity_checks(self):
        self.seed_nonflat()
        external={'IF2611.CFFEX':(1,0),'sc2611.INE':(1,0)}
        self.transport.query_gross_positions=Mock(return_value=external.copy())
        self.restore()
        self.assertEqual(self.transport.query_gross_positions.return_value,external)
        self.assertFalse(self.session.strategy.holdings.active)
        saved=self.store.load()
        payload=deepcopy(saved.payload)
        payload['component_states']['live_session']['identity']['min_raw_bars']=300
        self.store.save(payload,expected_generation=saved.generation)
        before=self.f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'min_raw_bars'):
            self.restore()
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_confirmation_is_only_used_for_first_continuous_window(self):
        from datetime import date
        from threading import Event
        from bomber.framework.trader.runtime.live.continuous import LiveWindowContext
        window=self.f.cal.windows(date(2026,10,28))[0]
        market=live.MarketSourceConfig(source='dolphindb',table='fixture',timestamp_label='start',volume_mode='per-bar')
        for carried,expected in (({},True),({'model':{}},False)):
            context=LiveWindowContext(self.f.cal,window,Event(),'20261027',resume=True,state=carried)
            with patch.object(live,'build_simnow_transport',return_value=Mock()), \
                    patch.object(live,'HybridSessionLifecycle',return_value=NS()):
                runtime=live.build_runtime(product='RB',release_path=self.f.release,directions_file=self.f.directions,
                    signal_config=self.f.signal,mode='simnow',max_contracts=2,entry_contracts=2,
                    market_config=market,state_file=self.f.path,confirm_manual_flat=True,
                    report_dir=self.f.root/'reports',window_context=context)
            self.assertIs(runtime.controller.confirm_manual_flat,expected)

    def test_manual_confirmation_cli_requires_original_state_and_simnow(self):
        command=self.f.cli()+['--run-forever','--mode','simnow','--enable-orders','--confirm-simnow',
            '--state-file',str(self.f.path),'--confirm-manual-flat','--max-contracts','2','--entry-contracts','2']
        self.assertTrue(live.parse_args(command).confirm_manual_flat)
        command[command.index(str(self.f.path))]=str(self.f.root/'missing.json')
        with patch('sys.stderr'),self.assertRaises(SystemExit):
            live.parse_args(command)
        with patch('sys.stderr'),self.assertRaises(SystemExit):
            live.parse_args(self.f.cli()+['--run-forever','--confirm-manual-flat'])


if __name__=='__main__':
    unittest.main()
