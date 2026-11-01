"""空仓直接改两个手数参数；只读柜台替身，验证实际联合恢复、备份及拒绝路径。"""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from types import SimpleNamespace as NS
from unittest.mock import Mock
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


if __name__=='__main__':
    unittest.main()
