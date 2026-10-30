"""外部手工平仓后只读核实、归档旧状态、保留订单预算并恢复新配置。"""
from contextlib import redirect_stdout
from copy import deepcopy
from importlib import import_module
from io import StringIO
from types import SimpleNamespace as NS
from unittest.mock import Mock
import json
import unittest

from tests import test_hybrid_recover_expired as legacy
from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.runtime.live.continuous import orders_for_day

maintenance = import_module('demos.10_hybrid_transformer.reconcile_manual_flat')


class ManualFlatTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.OldExpiryRecoveryTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.store = JsonStateStore(self.f.path)
        saved = self.store.load()
        payload = deepcopy(saved.payload)
        self.client = legacy.live.CLIENT_ID
        payload['orders'][self.client] = [dict(backend_id=self.client,
            client_order_id='CTP-9999-fixture-20261028-4', instrument_id='rb2701.SHFE', side='SELL',
            order_quantity='6', filled_quantity='6', remaining_quantity='0', status='FILLED',
            last_sequence=2, last_ts_event=legacy.ns('2026-10-28 09:04'), seen_keys=['fixture:1','fixture:2'])]
        payload['positions']['attribution']['orders']['old-filled-plan'] = dict(
            client_id=self.client,instrument_id='rb2701.SHFE',direction=-1,quantity='6',
            client_order_id='CTP-9999-fixture-20261028-4',released=False,
            legs=[dict(strategy_id=self.client,target_key='rb_main',quantity='6',filled='6')])
        self.saved = self.store.save(payload, expected_generation=saved.generation)
        self.before = self.f.path.read_bytes()
        self.transport = Mock(account_id='fixture', investor_id='fixture', broker_id='9999')
        self.transport.connect.return_value = NS(trading_day='20261028')
        self.transport.query_gross_positions.return_value = {}
        self.transport.query_active_orders.return_value = NS(orders=())

    def run_reconcile(self):
        with redirect_stdout(StringIO()):
            return maintenance.reconcile(state_file=self.f.path, transport=self.transport)

    def test_manual_flat_reconciles_all_components_keeps_terminal_orders_and_budget_and_real_restore_passes(self):
        updated = self.run_reconcile()
        payload = updated.payload
        self.assertEqual(payload['orders'], self.saved.payload['orders'])
        self.assertEqual(payload['positions']['attribution']['orders'],self.saved.payload['positions']['attribution']['orders'])
        self.assertEqual(orders_for_day(payload,'20261028'),1)
        self.assertEqual(updated.generation,self.saved.generation+1)
        current,_,_=self.f.session(mode='simnow',policy='off')
        current.manager.restore()
        self.assertEqual(current.strategy.config.max_contracts,5)
        self.assertEqual(current.strategy.config.max_entry_contracts,1)
        self.assertEqual(current.strategy._revision,0)
        self.assertFalse(current.strategy.holdings.active)
        self.assertEqual(current.strategy.last_targets,{})
        self.assertEqual(current.runner.position_manager.position(self.client,'rb_main'),0)
        self.assertEqual(current.ledger.snapshot(current.references.instrument_id).short_total,0)
        self.assertEqual(len(current.client.order_state_machine.checkpoints()),1)
        current.manager.state_components['hybrid'].validate_targets(current.runner.target_store)
        backups=list(self.f.root.glob('*.before-manual-flat-*.bak'))
        audits=list(self.f.root.glob('*.manual-flat-*.json'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),self.before)
        audit=json.loads(audits[0].read_text())
        self.assertEqual(audit['previous_targets'],{'rb2701':'-6'})
        self.assertEqual(len(audit['previous_batches']),3)
        self.assertFalse(audit['realized_pnl_reconstructed'])
        self.assertIsNone(audit['actual_close_prices'])
        self.transport.send_order.assert_not_called()
        self.transport.close.assert_called_once()

    def test_nonzero_gross_even_when_net_is_zero_blocks_all_writes(self):
        self.transport.query_gross_positions.return_value={'rb2701.SHFE':(6,6)}
        with self.assertRaisesRegex(RuntimeError,'柜台仍有多空总仓'):
            self.run_reconcile()
        self.assertEqual(self.f.path.read_bytes(),self.before)
        self.assertFalse(list(self.f.root.glob('*.before-manual-flat-*')))

    def test_active_orders_block_writes_even_when_broker_is_flat(self):
        self.transport.query_active_orders.return_value=NS(orders=('active',))
        with self.assertRaisesRegex(RuntimeError,'柜台仍有活动订单'):
            self.run_reconcile()
        self.assertEqual(self.f.path.read_bytes(),self.before)

    def test_wrong_account_or_day_or_unfinished_local_orders_do_not_change_state(self):
        self.transport.account_id='other-account'
        with self.assertRaisesRegex(ValueError,'账户与旧检查点不一致'):
            self.run_reconcile()
        self.assertEqual(self.f.path.read_bytes(),self.before)
        self.transport.account_id='fixture'
        self.transport.connect.return_value=NS(trading_day='20261029')
        with self.assertRaisesRegex(ValueError,'同一交易日'):
            self.run_reconcile()
        self.assertEqual(self.f.path.read_bytes(),self.before)
        invalid=deepcopy(self.saved.payload)
        invalid['orders'][self.client][0].update(status='ACCEPTED',filled_quantity='0',remaining_quantity='6')
        self.transport.connect.return_value=NS(trading_day='20261028')
        self.store.save(invalid,expected_generation=self.saved.generation)
        before=self.f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'未结束订单'):
            self.run_reconcile()
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_repeated_alignment_is_idempotent_and_does_not_reset_daily_order_count(self):
        first=self.run_reconcile()
        before=self.f.path.read_bytes()
        second=self.run_reconcile()
        self.assertEqual(first.generation,second.generation)
        self.assertEqual(self.f.path.read_bytes(),before)
        self.assertEqual(orders_for_day(second.payload,'20261028'),1)


if __name__ == '__main__':
    unittest.main()
