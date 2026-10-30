"""外部手工平仓后只读核实、归档旧状态、保留订单预算并恢复新配置。"""
from contextlib import redirect_stdout
from copy import deepcopy
from importlib import import_module
from io import StringIO
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
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

    def test_real_transport_login_settlement_activate_then_queries_in_order(self):
        from bomber.framework.trader.execution.ctp.td_transport import CtpTdApiTransport
        from tests.run_p4_ctp_td_transport import FakeTdApi
        apis=[]
        class Api(FakeTdApi):
            def __init__(api):
                super().__init__()
                apis.append(api)
            def reqUserLogin(api,data,reqid):
                api.request_names.append('login')
                api.onRspUserLogin(dict(BrokerID='9999',UserID='fixture',TradingDay='20261028',MaxOrderRef='17'),{},reqid,True)
                return 0
            def reqSettlementInfoConfirm(api,data,reqid):
                api.request_names.append('settlement')
                api.onRspSettlementInfoConfirm(dict(BrokerID='9999',InvestorID='fixture'),{},reqid,True)
                return 0
            def reqQryInvestorPosition(api,data,reqid):
                api.request_names.append('positions')
                api.onRspQryInvestorPosition({}, {}, reqid, True)
                return 0
            def reqQryOrder(api,data,reqid):
                api.request_names.append('orders')
                api.onRspQryOrder({}, {}, reqid, True)
                return 0
        transport=CtpTdApiTransport(client_id=self.client,account_id='fixture',investor_id='fixture',
            broker_id='9999',front='tcp://fake:1234',password='fake',app_id='fake',auth_code='fake',
            td_api_base=Api,flow_path=str(self.f.root/'td'),timeout_seconds=.05)
        # connect真实实现不会自行置_ready；回归遗漏activate时必须在第一次查询处失败。
        activate=transport.activate
        def activate_and_mark():
            self.assertFalse(transport._ready)
            self.assertEqual(apis[0].request_names[-1],'settlement')
            activate()
            apis[0].request_names.append('activate')
        with patch.object(transport,'activate',side_effect=activate_and_mark),redirect_stdout(StringIO()):
            updated=maintenance.reconcile(state_file=self.f.path,transport=transport)
        self.assertEqual(apis[0].request_names,['create','auth','login','settlement','activate','positions','orders'])
        self.assertTrue(apis[0].exited)
        self.assertEqual(updated.payload['component_states']['hybrid']['config']['max_contracts'],5)
        self.assertFalse(transport._ready)

    def test_activation_failure_keeps_original_state_and_still_releases_connection(self):
        self.transport.activate.side_effect=RuntimeError('activation refused')
        with self.assertRaisesRegex(RuntimeError,'activation refused'):
            self.run_reconcile()
        self.assertEqual(self.f.path.read_bytes(),self.before)
        self.transport.query_gross_positions.assert_not_called()
        self.transport.close.assert_called_once()
        self.assertFalse(list(self.f.root.glob('*.before-manual-flat-*')))

    def test_nonzero_gross_even_when_net_is_zero_blocks_all_writes(self):
        self.transport.query_gross_positions.return_value={'rb2701.SHFE':(6,6)}
        with self.assertRaisesRegex(RuntimeError,'RB合约柜台仍有多空总仓'):
            self.run_reconcile()
        self.assertEqual(self.f.path.read_bytes(),self.before)
        self.assertFalse(list(self.f.root.glob('*.before-manual-flat-*')))

    def test_active_orders_block_writes_even_when_broker_is_flat(self):
        self.transport.query_active_orders.return_value=NS(orders=(NS(instrument_id='rb2701.SHFE'),))
        with self.assertRaisesRegex(RuntimeError,'RB合约柜台仍有活动订单'):
            self.run_reconcile()
        self.assertEqual(self.f.path.read_bytes(),self.before)

    def test_shared_account_other_positions_and_orders_do_not_block_rb_alignment(self):
        external = {'IF2611.CFFEX': (1, 0), 'sc2611.INE': (1, 0),
            'MO2611-C-7100.CFFEX': (0, 1), 'MO2611-C-6900.CFFEX': (0, 1), 'IM2611.CFFEX': (0, 2)}
        self.transport.query_gross_positions.return_value = external.copy()
        self.transport.query_active_orders.return_value = NS(orders=(NS(instrument_id='IF2611.CFFEX'),))
        updated = self.run_reconcile()
        self.assertEqual(updated.payload['component_states']['hybrid']['last_targets'], {})
        self.assertEqual(updated.payload['orders'], self.saved.payload['orders'])
        audit = json.loads(next(self.f.root.glob('*.manual-flat-*.json')).read_text())
        self.assertEqual(audit['managed_instruments'], ['rb2701.SHFE'])
        self.assertEqual(audit['external_gross'], {key: [str(q) for q in value] for key, value in external.items()})
        self.assertEqual(audit['external_active_orders'], 1)
        self.assertEqual(self.transport.query_gross_positions.return_value, external)
        self.transport.send_order.assert_not_called()
        self.transport.cancel_order.assert_not_called()

    def test_scope_is_exact_contract_and_broker_symbol_case_does_not_bypass_check(self):
        self.transport.query_gross_positions.return_value = {'rb2705.SHFE': (0, 2), 'RB2701.SHFE': (0, 1)}
        with self.assertRaisesRegex(RuntimeError, 'RB2701.SHFE'):
            self.run_reconcile()
        self.assertEqual(self.f.path.read_bytes(), self.before)
        self.transport.query_gross_positions.return_value = {'rb2705.SHFE': (0, 2)}
        self.run_reconcile()
        self.transport.send_order.assert_not_called()

    def test_scope_refuses_to_clear_non_rb_state_or_guess_missing_contract(self):
        invalid = deepcopy(self.saved.payload)
        invalid['positions']['account_positions'][0]['key']['instrument_id'] = 'IF2611.CFFEX'
        with self.assertRaisesRegex(ValueError, '非RB合约'):
            maintenance.aligned_payload(invalid, trading_day='20261028')
        empty = maintenance.aligned_payload(self.saved.payload, trading_day='20261028')
        empty['orders'][self.client] = []
        with self.assertRaisesRegex(ValueError, '缺少具体RB合约'):
            maintenance.managed_instruments(empty)
        self.assertEqual(self.f.path.read_bytes(), self.before)

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
