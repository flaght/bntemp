"""当前仓位人工对齐：真实Hybrid批次、直连恢复及备份，柜台使用离线替身。"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import json
import unittest

from tests import test_hybrid_shared_layout as fixtures
from bomber.framework.trader.persistence import JsonStateStore

repair = import_module('demos.10_hybrid_transformer.reconcile_direct_positions')
app = import_module('demos.direct_ctp')


class DirectPositionAlignmentTests(unittest.TestCase):
    session = fixtures.HybridSharedLayoutTests.session
    start = fixtures.HybridSharedLayoutTests.start
    bar = fixtures.HybridSharedLayoutTests.bar

    def setUp(self):
        fixtures.HybridSharedLayoutTests.setUp(self)

    def make_account(self):
        independent = import_module('bomber.framework.trader.execution.ctp.independent')
        lifecycle = import_module('bomber.framework.trader.runtime.live.channels.ctp_independent')
        with patch('bomber.framework.trader.execution.ctp.native_driver.CtpNativeTraderDriver', independent.IndependentCtpDriver), \
                patch('bomber.framework.trader.runtime.live.channels.ctp_shared.CtpSharedAccountLifecycle', lifecycle.IndependentCtpLifecycle):
            account, service, transport = fixtures.HybridSharedLayoutTests.make_account(self, preserve_external=True)
        connect, raw = transport.connect, transport.raw
        transport.connect = lambda *args: replace(connect(*args), front_id=1, session_id=2)
        transport.raw = lambda ref: dict(raw(ref), Direction=transport.active[ref]['Direction'],
            OffsetFlag=transport.active[ref]['CombOffsetFlag'], TradingDay='20261013')
        return account, service, transport

    def dispose_old(self):
        self.service._checkpoint = None
        self.service._cancel_owner = lambda sid: None
        for session in self.sessions:
            session.runner.stop()
        self.account.driver.stop()

    def pending_checkpoint(self):
        rb, palm = self.session('RB'), self.session('P')
        identity = {'trading_day':'20261013', 'strategies': {
            sid: dict(strategy_id=sid, trading_day='20261013', kind='hybrid_transformer', environment='realtime')
            for sid in ('hybrid-rb','hybrid-p')}}
        self.account.manager.state_components['session_identity'].identity = identity
        self.start(rb, palm)
        self.bar(rb, '2026-10-13 09:01', 0)
        self.bar(palm, '2026-10-13 09:01', .5)
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        self.bar(palm, '2026-10-13 09:02', .5)
        pending_id = next(iter(self.account.driver.buckets.frozen))
        palm.client.drain_events()
        saved = self.account.manager.save()
        signals = {session.strategy.strategy_id: dict(version=1, revision=session.strategy._revision,
                    identity={}, cursor=0, signal=session.signal_checkpoint.snapshot_state())
                   for session in (rb, palm)}
        observed = {'quantities':{str(self.rb):[Decimal(0)]*4, str(self.p):[Decimal(0),Decimal(2),Decimal(0),Decimal(0)]},
            'bases':{str(self.rb):[Decimal(0)]*4, str(self.p):[Decimal(0),Decimal(9000),Decimal(0),Decimal(0)]},
            'raw_positions':[]}
        return saved, signals, observed, pending_id

    def test_unknown_open_is_archived_and_two_long_palm_restores_on_next_day_with_old_expiry(self):
        saved, signals, observed, pending_id = self.pending_checkpoint()
        expected = {str(self.rb):Decimal(0), str(self.p):Decimal(2)}
        original = deepcopy(saved.payload)
        candidate, evidence = repair.prepare_alignment(saved.payload, expected, observed,
            trading_day='20261014', signals=signals, operation_id='offline')
        self.assertEqual(saved.payload, original)
        self.assertEqual(evidence['old_order_terminal_status'], 'UNKNOWN')
        self.assertFalse(evidence['historical_fills_reconstructed'])
        self.assertFalse(candidate['ctp_drivers']['account']['orders'])
        self.assertFalse(candidate['component_states']['strategy_buckets']['frozen'])
        self.assertNotIn(pending_id, [row['client_order_id'] for row in candidate['orders']['account']])
        old_journal = original['component_states']['shared_account']['shared']['event_journal']
        self.assertEqual(candidate['component_states']['shared_account']['shared']['event_journal']['events'], old_journal['events'])
        metadata = candidate['component_states']['shared_account']['sources']['hybrid-p']['metadata']
        aligned = metadata['checkpoint_alignment']['signal']
        self.assertEqual(aligned['hybrid']['holdings']['active'], signals['hybrid-p']['signal']['hybrid']['holdings']['active'])
        for name in ('fills_received','order_updates_received'):
            self.assertEqual(aligned['hybrid'][name], signals['hybrid-p']['signal']['hybrid'][name])
        self.assertEqual(candidate['ctp_ledgers']['account']['positions'][0]['long_yesterday'], '2')
        self.assertEqual(candidate['ctp_ledgers']['account']['positions'][0]['long_yesterday_basis'], '9000')
        # 同一文件经普通prepare_payload和IndependentCtpLifecycle恢复，无旧订单重发。
        self.dispose_old()
        account, service, transport = self.make_account()
        account.expected_day = '20261014'
        account.driver.book.day = account.driver.buckets.day = '20261014'
        new_identity = deepcopy(original['component_states']['session_identity']['identity'])
        new_identity['trading_day'] = '20261014'
        for value in new_identity['strategies'].values(): value['trading_day'] = '20261014'
        account.manager.state_components['session_identity'].identity = new_identity
        account.import_transform = lambda _: app.prepare_payload(account, candidate)
        connect = transport.connect
        transport.connect = lambda *args: replace(connect(*args), trading_day='20261014')
        transport.gross = {str(self.p):(Decimal(2),Decimal(0))}
        details = transport.query_position_details
        transport.query_position_details = lambda: [dict(row, TodayPosition=0, PositionDate='2') for row in details()]
        rb, palm = self.session('RB',service=service,driver=account.driver), self.session('P',service=service,driver=account.driver)
        account.start(resume=True)
        service.start()
        self.addCleanup(account.driver.stop)
        self.assertEqual(service.position_manager.position('hybrid-p','p_main'), 2)
        self.assertEqual(service.position_manager.position('hybrid-rb','rb_main'), 0)
        for session in (rb,palm):
            sid = session.strategy.strategy_id
            path = self.root/(sid+'-original-signal.json')
            JsonStateStore(path).save(signals[sid], expected_generation=0)
            from bomber.framework.trader.runtime.live.shared import SharedStrategySession
            item = SharedStrategySession(session, path, {})
            item.restore(service, resume=True)
            self.assertEqual(session.strategy._revision,
                candidate['component_states']['shared_account']['sources'][sid]['revision'])
        self.assertEqual(sum(batch.quantity for batch in palm.strategy.holdings.active), 2)
        self.assertFalse(transport.sent)
        self.assertEqual(palm.restored_signal_cursor, len(old_journal['events']['hybrid-p']))

    def test_matching_batch_can_be_recovered_from_pending_order_when_latest_signal_is_flat(self):
        saved, signals, observed, _ = self.pending_checkpoint()
        original_batches = deepcopy(signals['hybrid-p']['signal']['hybrid']['holdings']['active'])
        sid='hybrid-p'
        flat = deepcopy(signals[sid]['signal'])
        flat['hybrid']['holdings']['active'] = []
        flat['hybrid']['last_targets'] = {'p2701':'0'}
        flat['hybrid']['last_processed_ns'] += 60_000_000_000
        signals[sid]['signal'] = flat
        saved.payload['component_states']['shared_account']['sources'][sid]['metadata']['hybrid_signal_state'] = flat
        candidate, _ = repair.prepare_alignment(saved.payload,{str(self.rb):Decimal(0),str(self.p):Decimal(2)}, observed,
            trading_day='20261014', signals=signals,operation_id='offline')
        restored = candidate['component_states']['shared_account']['sources'][sid]['metadata']['checkpoint_alignment']['signal']
        self.assertEqual(restored['hybrid']['holdings']['active'], original_batches)

    def test_current_broker_cost_builds_basis_without_treating_order_limit_as_fill(self):
        transport=Mock()
        transport.query_gross_positions.return_value={'p2701.DCE':(Decimal(2),Decimal(0))}
        transport.query_active_orders.return_value=NS(orders=())
        transport.query_position_details.return_value=[dict(InstrumentID='p2701',ExchangeID='DCE',
            Position=2,TodayPosition=0,PositionDate='1',HedgeFlag='1',PosiDirection='2',PositionCost=180000)]
        observed=repair.observe_positions(transport,{'p2701.DCE':Decimal(2)}, {'p2701.DCE':10})
        self.assertEqual(observed['quantities']['p2701.DCE'],[0,2,0,0])
        self.assertEqual(observed['bases']['p2701.DCE'],[0,9000,0,0])
        transport.query_position_details.return_value[0]['TodayPosition']=1
        mixed=repair.observe_positions(transport,{'p2701.DCE':Decimal(2)}, {'p2701.DCE':10})
        self.assertEqual(mixed['quantities']['p2701.DCE'],[1,1,0,0])
        self.assertEqual(mixed['bases']['p2701.DCE'],[9000,9000,0,0])

    def test_opposite_gross_position_or_active_order_rejects_alignment(self):
        transport=Mock()
        transport.query_gross_positions.return_value={'p2701.DCE':(Decimal(3),Decimal(1))}
        with self.assertRaisesRegex(RuntimeError,'柜台仓位与声明不一致'):
            repair.observe_positions(transport,{'p2701.DCE':Decimal(2)}, {'p2701.DCE':10})
        transport.query_gross_positions.return_value={'p2701.DCE':(Decimal(2),Decimal(0))}
        transport.query_active_orders.return_value=NS(orders=(NS(instrument_id='p2701.DCE'),))
        with self.assertRaisesRegex(RuntimeError,'仍有活动委托'):
            repair.observe_positions(transport,{'p2701.DCE':Decimal(2)}, {'p2701.DCE':10})

    def test_file_repair_backs_up_unknown_order_and_leaves_signal_files_for_authoritative_restore(self):
        saved, signals, observed, _ = self.pending_checkpoint()
        self.dispose_old()
        rows=[]
        for sid, signal in signals.items():
            path=self.root/(sid+'.json')
            JsonStateStore(path).save(signal,expected_generation=0)
            rows.append(dict(strategy_id=sid,state_file=str(path)))
        original = (self.root/'account.json').read_bytes()
        signal_bytes={row['state_file']:Path(row['state_file']).read_bytes() for row in rows}
        transport=Mock(broker_id='9999',investor_id='fixture',account_id='fixture')
        transport.connect.return_value=NS(trading_day='20261014')
        with patch.object(repair,'observe_positions',return_value=observed):
            updated=repair.reconcile(state_file=self.root/'account.json',rows=rows,
                expected={str(self.rb):Decimal(0),str(self.p):Decimal(2)},transport=transport)
        self.assertGreater(updated.generation,saved.generation)
        backups=list(self.root.glob('account.json.before-position-alignment-*.bak'))
        audits=list(self.root.glob('account.json.position-alignment-*.json'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),original)
        evidence=json.loads(audits[0].read_text())
        self.assertEqual(evidence['orders_submitted'],0)
        for path, data in signal_bytes.items(): self.assertEqual(Path(path).read_bytes(),data)
        transport.send_order.assert_not_called()
        transport.cancel_order.assert_not_called()

    def test_changed_second_observation_keeps_state_file_intact(self):
        _, signals, observed, _ = self.pending_checkpoint()
        self.dispose_old()
        rows=[]
        for sid, signal in signals.items():
            path=self.root/(sid+'.json');JsonStateStore(path).save(signal,expected_generation=0)
            rows.append(dict(strategy_id=sid,state_file=str(path)))
        original=(self.root/'account.json').read_bytes()
        changed=deepcopy(observed);changed['bases'][str(self.p)][1]+=1
        transport=Mock(broker_id='9999',investor_id='fixture',account_id='fixture')
        transport.connect.return_value=NS(trading_day='20261014')
        with patch.object(repair,'observe_positions',side_effect=[observed,changed]), \
                self.assertRaisesRegex(RuntimeError,'核验期间发生变化'):
            repair.reconcile(state_file=self.root/'account.json',rows=rows,
                expected={str(self.rb):Decimal(0),str(self.p):Decimal(2)},transport=transport)
        self.assertEqual((self.root/'account.json').read_bytes(),original)
        self.assertFalse(list(self.root.glob('*.bak')))


if __name__=='__main__':
    unittest.main()
