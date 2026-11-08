"""共享账户空仓基线对齐；复用真实批次、冻结和恢复链，仅柜台及模型为替身。"""
from copy import deepcopy
from contextlib import redirect_stdout
from importlib import import_module
from io import StringIO
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tests import test_hybrid_shared_layout as layout_fixture
from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.runtime.live.shared import SharedStrategySession

repair = import_module('demos.10_hybrid_transformer.reconcile_shared_flat')


class SharedFlatAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = layout_fixture.HybridSharedLayoutTests()
        f.setUp(); self.addCleanup(f.doCleanups)
        f.account, f.service, f.transport = f.make_account(preserve_external=True)
        self.account_config = dict(client_id='account', max_gross_quantity=4,
            max_gross_notional=1000000, max_session_orders=200, limit_offset_ticks=0)
        f.account.manager.state_components['session_identity'].identity = dict(
            account=self.account_config, trading_day='20261013')
        self.rb, self.p = f.session('RB'), f.session('P')
        with redirect_stdout(StringIO()):
            f.start(self.rb, self.p)
            f.bar(self.rb, '2026-10-13 09:01', -1)
            f.transport.fill(f.transport.sent[-1]['OrderRef']); self.rb.client.drain_events()
            f.bar(self.p, '2026-10-13 09:01', -.3)
            f.transport.fill(f.transport.sent[-1]['OrderRef']); self.p.client.drain_events()
            f.service.prices.update(f.p, 100, f.now+300_000_000_000)
            f.bar(self.rb, '2026-10-13 09:06', 0)  # 已受理但尚未收到成交的BUY CLOSE_TODAY。
        self.path = f.root/'account.json'
        self.signal_path = f.root/'rb_signal.json'
        self.p_path = f.root/'p_signal.json'
        SharedStrategySession(self.rb, self.signal_path, {'id':'rb'}).save(f.service)
        SharedStrategySession(self.p, self.p_path, {'id':'p'}).save(f.service)
        self.original = JsonStateStore(self.path).load()
        self.signal = self.rb.signal_checkpoint.snapshot_state()
        self.config = dict(account=self.account_config, account_state_file=str(self.path),
            environment='realtime', existing_position_policy='preserve_external', strategies=[
                dict(strategy_id='hybrid-rb', kind='hybrid_transformer', state_file=str(self.signal_path)),
                dict(strategy_id='hybrid-p', kind='hybrid_transformer', state_file=str(self.p_path))])
        self.p_bytes = self.p_path.read_bytes()
        self.rb_bytes = self.signal_path.read_bytes()
        self.bytes = self.path.read_bytes()
        self.broker = NS(account_id='fixture', investor_id='fixture', broker_id='9999',
            connect=lambda *_: NS(trading_day='20261014'), activate=lambda:None, close=lambda:None,
            query_gross_positions=lambda:{'p2701.DCE': (0,1)},
            query_active_orders=lambda:NS(orders=()))

    def candidate(self, payload=None):
        return repair.align_flat_strategy(payload or self.original.payload, self.signal,
            strategy_id='hybrid-rb', operation_id='test')

    def run_repair(self):
        # 文件写入与恢复是真实代码；账户租约只在此fixture内替换，避免占用开发机共享账户锁。
        with patch('bomber.framework.trader.runtime.live.channels.ctp_shared.CtpSharedAccountLifecycle.acquire') as acquire:
            with redirect_stdout(StringIO()):
                result = repair.reconcile(self.config, strategy_id='hybrid-rb', transport=self.broker)
            acquire.assert_called_once()
            return result

    def test_archives_unknown_order_zeroes_only_rb_and_preserves_p_and_order_history(self):
        before = deepcopy(self.original.payload)
        after, ledger, evidence = self.candidate()
        self.assertEqual(self.original.payload, before)
        positions = after['positions']['strategy_positions']
        self.assertEqual({r['strategy_id']:r['quantity'] for r in positions},
            {'hybrid-rb':'0', 'hybrid-p':'-1'})
        self.assertEqual(ledger.positions[self.fixture.p].short_total, 1)
        self.assertNotIn(self.fixture.rb, ledger.positions)
        self.assertEqual(after['component_states']['shared_account']['sources']['hybrid-p'],
            before['component_states']['shared_account']['sources']['hybrid-p'])
        retired = {r['client_order_id'] for r in evidence['archived_order_states']}
        self.assertEqual(len(retired), 1)
        self.assertEqual(evidence['archived_order_states'][0]['status'], 'ACCEPTED')
        self.assertEqual(evidence['old_order_terminal_status'], 'UNKNOWN')
        self.assertFalse(evidence['realized_pnl_reconstructed'])
        self.assertIsNone(evidence['actual_close_price'])
        expected = [r for r in before['orders']['account'] if r['client_order_id'] not in retired]
        self.assertEqual(after['orders']['account'], expected)
        self.assertEqual(after['ctp_drivers']['account']['orders'], [])
        self.assertTrue(all(r['quantity']=='0' for r in after['positions']['working_quantities']))
        self.assertEqual(after['component_states']['shared_account']['shared']['order_budget'],
            before['component_states']['shared_account']['shared']['order_budget'])

    def test_candidate_restores_real_account_and_overrides_old_rb_signal_without_resetting_p(self):
        after, _, _ = self.candidate()
        after['positions']['recovery_required_clients'] = ['account']
        JsonStateStore(self.path).save(after, expected_generation=self.original.generation)
        f = self.fixture
        f.account, f.service, f.transport = f.make_account(preserve_external=True)
        f.account.manager.state_components['session_identity'].identity = dict(
            account=self.account_config, trading_day='20261013')
        rb, p = f.session('RB'), f.session('P')
        f.transport.gross = {'p2701.DCE':(0,1)}
        with redirect_stdout(StringIO()):
            f.account.start(resume=True)
            SharedStrategySession(rb, self.signal_path, {'id':'rb'}).restore(f.service, resume=True)
            SharedStrategySession(p, self.p_path, {'id':'p'}).restore(f.service, resume=True)
        self.addCleanup(f.account.driver.stop)
        self.assertEqual(f.service.position_manager.position('hybrid-rb','rb_main'), 0)
        self.assertEqual(f.service.position_manager.position('hybrid-p','p_main'), -1)
        self.assertEqual(rb.strategy.holdings.active, ())
        self.assertEqual(sum(rb.strategy.last_targets.values()), 0)
        self.assertGreater(rb.strategy._revision, self.signal['hybrid']['revision'])
        self.assertEqual(sum(abs(batch.quantity) for batch in p.strategy.holdings.active), 1)
        self.assertEqual(f.transport.sent, [])

    def test_aligned_state_passes_normal_cross_day_rollover_with_p_cost_and_ownership_preserved(self):
        from scripts.integration.shared_account.rollover import payload_for_day
        from dataclasses import replace
        self.save_rb_external_baseline()
        after, _, _ = self.candidate()
        old_identity = dict(account=self.account_config, trading_day='20261013', environment='realtime',
            strategies={sid:dict(trading_day='20261013', environment='realtime', kind='hybrid_transformer')
                for sid in ('hybrid-rb','hybrid-p')})
        after['component_states']['session_identity']['identity'] = old_identity
        state = after['component_states']['shared_account']['shared']
        state['draining'] = ['hybrid-p']
        state['suspended'] = ['hybrid-rb','hybrid-p']
        JsonStateStore(self.path).save(after, expected_generation=self.original.generation)
        f = self.fixture
        f.account, f.service, f.transport = f.make_account(preserve_external=True)
        rb, p = f.session('RB'), f.session('P')
        current = deepcopy(old_identity); current['trading_day']='20261014'
        for row in current['strategies'].values(): row['trading_day']='20261014'
        f.account.expected_day='20261014'
        f.account.manager.state_components['session_identity'].identity = current
        f.account.rollover_transform=payload_for_day
        connect=f.transport.connect
        f.transport.connect=lambda *args:replace(connect(*args), trading_day='20261014')
        f.transport.gross={'p2701.DCE':(0,1),'rb2701.SHFE':(3,0)}
        with redirect_stdout(StringIO()): f.account.start(resume=True)
        self.addCleanup(f.account.driver.stop)
        self.assertEqual(f.account.ledger.state().trading_day,'20261014')
        self.assertEqual(f.account.driver.trading_day,'20261014')
        self.assertEqual(f.service.position_manager.position('hybrid-rb','rb_main'),0)
        self.assertEqual(f.service.position_manager.position('hybrid-p','p_main'),-1)
        self.assertEqual(f.account.ledger.state().positions[f.p].short_yesterday,1)
        self.assertEqual(f.account.ledger.state().positions[f.p].short_yesterday_basis,100)
        self.assertEqual(f.account.external_gross,{'rb2701.SHFE':(3,0)})
        self.assertEqual(f.transport.sent,[])

    def test_commits_one_account_file_after_two_broker_checks_with_backup_and_unknown_audit(self):
        calls=[]
        self.broker.query_gross_positions=lambda:(calls.append('gross') or {'p2701.DCE':(0,1)})
        self.broker.query_active_orders=lambda:(calls.append('orders') or NS(orders=()))
        self.broker.close=lambda:calls.append('close')
        saved=self.run_repair()
        self.assertEqual(calls, ['gross','orders','gross','orders','close'])
        self.assertEqual(saved.generation, self.original.generation+1)
        self.assertEqual(self.p_path.read_bytes(), self.p_bytes)
        self.assertEqual(self.signal_path.read_bytes(), self.rb_bytes)
        backups=list(self.path.parent.glob('account.json.before-flat-alignment-*.bak'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),self.bytes)
        audit=json.loads(next(self.path.parent.glob('account.json.flat-alignment-*.json')).read_text())
        self.assertEqual(audit['orders_submitted'],0)
        self.assertEqual(audit['old_order_terminal_status'],'UNKNOWN')
        self.assertEqual(audit['persisted_trading_day'],'20261013')
        self.assertEqual(saved.payload['ctp_drivers']['account']['trading_day'],'20261013')

    def assert_unchanged(self):
        self.assertEqual(self.path.read_bytes(), self.bytes)
        self.assertEqual(self.p_path.read_bytes(), self.p_bytes)
        self.assertEqual(self.signal_path.read_bytes(), self.rb_bytes)
        self.assertFalse(list(self.path.parent.glob('account.json.*.bak')))
        self.assertFalse(list(self.path.parent.glob('account.json.flat-alignment-*.json')))

    def test_refuses_rb_nonzero_or_p_changed_or_managed_active_orders_without_writing(self):
        for gross, active in [({'rb2701.SHFE':(1,0),'p2701.DCE':(0,1)},()),
                ({'p2701.DCE':(1,0)},()),
                ({'p2701.DCE':(0,1)},(NS(instrument_id='p2701.DCE'),))]:
            with self.subTest(gross=gross,active=active):
                self.broker.query_gross_positions=lambda:gross
                self.broker.query_active_orders=lambda:NS(orders=active)
                with self.assertRaises(RuntimeError): self.run_repair()
                self.assert_unchanged()

    def test_shared_manual_mode_keeps_terminal_order_and_supports_same_day(self):
        f=self.fixture
        f.transport.cancel_order(f.transport.sent[-1]['OrderRef'])
        self.original=JsonStateStore(self.path).load()
        self.broker.connect=lambda *_:NS(trading_day='20261013')
        with patch('bomber.framework.trader.runtime.live.channels.ctp_shared.CtpSharedAccountLifecycle.acquire'),redirect_stdout(StringIO()):
            saved=repair.reconcile(self.config,strategy_id='hybrid-rb',transport=self.broker,archive_unknown_close=False)
        self.assertEqual(saved.payload['orders'],self.original.payload['orders'])
        self.assertEqual({r['strategy_id']:r['quantity'] for r in saved.payload['positions']['strategy_positions']},
            {'hybrid-rb':'0','hybrid-p':'-1'})
        audit=json.loads(next(self.path.parent.glob('*.flat-alignment-*.json')).read_text())
        self.assertEqual(audit['operation'],'manual_flat')
        self.assertEqual(audit['archived_order_states'],[])

    def test_refuses_same_trading_day_or_failed_query_without_writing(self):
        self.broker.connect=lambda *_:NS(trading_day='20261013')
        with self.assertRaisesRegex(ValueError,'后续交易日'): self.run_repair()
        self.assert_unchanged()
        self.broker.connect=lambda *_:NS(trading_day='20261014')
        self.broker.query_gross_positions=lambda: (_ for _ in ()).throw(TimeoutError('query timeout'))
        with self.assertRaises(TimeoutError): self.run_repair()
        self.assert_unchanged()

    def test_rejects_open_order_or_another_strategys_order_without_mutating_input(self):
        for change in ('open','other'):
            payload=deepcopy(self.original.payload)
            row=payload['ctp_drivers']['account']['orders'][0]
            row['intent']['position_effect']='OPEN' if change=='open' else row['intent']['position_effect']
            if change=='other': row['intent']['strategy_id']='hybrid-p'
            before=deepcopy(payload)
            with self.assertRaises(ValueError): self.candidate(payload)
            self.assertEqual(payload,before)

    def save_rb_external_baseline(self):
        payload=deepcopy(self.original.payload)
        payload['component_states']['external_positions'].update(
            gross={'rb2701.SHFE':['3','0']}, buckets={'rb2701.SHFE':['3','0','0','0']})
        self.original=JsonStateStore(self.path).save(payload,expected_generation=self.original.generation)
        self.bytes=self.path.read_bytes()

    def test_known_same_contract_external_long_is_preserved_when_only_strategy_becomes_flat(self):
        self.save_rb_external_baseline()
        self.broker.query_gross_positions=lambda:{'rb2701.SHFE':(3,0),'p2701.DCE':(0,1)}
        saved=self.run_repair()
        self.assertEqual(saved.payload['component_states']['external_positions'],
            self.original.payload['component_states']['external_positions'])
        owned={r['strategy_id']:r['quantity'] for r in saved.payload['positions']['strategy_positions']}
        self.assertEqual(owned,{'hybrid-rb':'0','hybrid-p':'-1'})
        self.assertFalse(any(r['instrument_id']=='rb2701.SHFE'
            for r in saved.payload['ctp_ledgers']['account']['positions']))
        self.assertEqual(self.p_path.read_bytes(),self.p_bytes)
        audit=json.loads(next(self.path.parent.glob('account.json.flat-alignment-*.json')).read_text())
        self.assertEqual(audit['preserved_external_gross'],{'rb2701.SHFE':['3','0']})
        self.assertEqual(audit['aligned_strategy_gross'],['0','0'])

    def test_unknown_same_contract_difference_never_changes_external_baseline_and_names_both_sides(self):
        self.save_rb_external_baseline()
        # 净仓相同也不能忽略多空总仓变化；原基线被平掉也不能按策略空仓通过。
        for actual in ((2,0),(4,1),(0,0)):
            with self.subTest(actual=actual):
                self.broker.query_gross_positions=lambda:{'rb2701.SHFE':actual,'p2701.DCE':(0,1)}
                with self.assertRaisesRegex(RuntimeError,
                        f'柜台多仓={actual[0]} 空仓={actual[1]}.*原外部基线多仓=3 空仓=0'):
                    self.run_repair()
                self.assert_unchanged()

    def test_external_positions_outside_managed_scope_are_preserved(self):
        self.broker.query_gross_positions=lambda:{'p2701.DCE':(0,1),'IF2611.CFFEX':(2,0)}
        saved=self.run_repair()
        self.assertEqual(saved.payload['component_states']['external_positions'],
            self.original.payload['component_states']['external_positions'])


if __name__=='__main__': unittest.main()
