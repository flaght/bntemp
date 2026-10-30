"""旧六手批次的独立恢复；只用本地SDK替身，不连接柜台、不发送订单。"""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from decimal import Decimal
from importlib import import_module
from io import StringIO
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import json
import unittest

from tests import test_hybrid_continuous as fixture
from tests.run_hybrid_live_offline import live, Upstream, InstrumentId
from tests.test_demo_hybrid_live import ns
from bomber.framework.trader.contracts import TargetPortfolio
from bomber.framework.trader.execution.contracts import OrderSide, PositionEffect
from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.portfolio import AccountTargetKey, PortfolioCoordinatorState
from bomber.framework.trader.runtime.live.local import strategy_state_lock

recovery = import_module('demos.10_hybrid_transformer.recover_expired')


class OldExpiryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ContinuousTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        base, _, _ = self.f.session()
        self.inputs = NS(references=base.references, calendar=self.f.cal, upstream=Upstream('fixture'))
        self.driver = fixture.CtpNativeTraderDriver(live.CLIENT_ID, 'fixture', fixture.Transport('20261028'),
            enable_test_orders=True, disconnect_handler=lambda _: None)
        self.config = live.HybridTransformerConfig('RB', 'SHFE', '1301', 10, 5, 1, Decimal(10),
            target_key='rb_main', record_signals=True)
        self.old = self.assemble('simnow')
        strategy = self.old.strategy
        for minute in (1, 2, 3):
            stamp = ns(f'2026-10-28 09:0{minute}') + live.MINUTE - 1
            strategy.holdings.advance('rb2701', -1, fixture.dt(f'2026-10-28 09:0{minute}'), timestamp_ns=stamp)
        strategy.restore_target_revision(1)
        strategy.bars_used = 3
        strategy.last_processed_ns = stamp
        strategy.last_main = 'rb2701'
        strategy.last_targets = {'rb2701': Decimal(-6)}
        instrument = self.old.references.instrument_id
        positions = self.old.runner.position_manager
        positions.set_account_position(live.CLIENT_ID, instrument, -6)
        positions.attribution.adopt(live.CLIENT_ID, instrument, live.CLIENT_ID, 'rb_main', -6)
        self.old.ledger.apply_fill(instrument, 'SELL', 'OPEN', 6, 100, 10)
        self.old.runner.target_store.apply(TargetPortfolio(live.CLIENT_ID, 1, stamp, {'rb_main': Decimal(-6)}))
        key = AccountTargetKey(live.CLIENT_ID, instrument)
        self.old.runner.portfolio_coordinator.restore(PortfolioCoordinatorState(1,
            {live.CLIENT_ID: {key: Decimal(-6)}}, {live.CLIENT_ID: 1}, frozenset({key})))
        self.driver.start(lambda _: None)
        self.old.manager.save()
        self.driver.stop()
        # 模拟新增max_entry_contracts字段之前的真实旧检查点。
        store = JsonStateStore(self.f.path)
        saved = store.load()
        payload = deepcopy(saved.payload)
        payload['component_states']['hybrid']['holdings'].pop('max_entry_contracts')
        self.saved = store.save(payload, expected_generation=saved.generation)
        self.before = self.f.path.read_bytes()

    def assemble(self, mode):
        if mode == 'simnow':
            self.driver = fixture.CtpNativeTraderDriver(live.CLIENT_ID, 'fixture', fixture.Transport('20261028'),
                enable_test_orders=True, disconnect_handler=lambda _: None)
        strategy = live.HybridTransformerStrategy(live.CLIENT_ID, self.inputs.references,
            recovery.NoEntryPredictor('1301'), self.config, calendar=self.f.cal)
        return live.assemble(self.inputs, NS(driver=self.driver if mode == 'simnow' else None),
            product='RB', release_path=self.f.release, directions_file=self.f.directions,
            signal_config=self.f.signal, market_config=live.MarketSourceConfig(), max_contracts=10,
            mode=mode, state_file=self.f.path, execution_policy=fixture.ExecutionPolicy('off', 'fills'),
            strategy=strategy, bar_directory=self.f.root/'market')

    def test_joint_restore_retains_all_six_old_contracts_and_original_deadlines(self):
        restored = self.assemble('simnow')
        restored.manager.restore()
        self.assertEqual(restored.strategy.last_targets, {'rb2701': Decimal(-6)})
        self.assertEqual([b.quantity for b in restored.strategy.holdings.active], [-2, -2, -2])
        self.assertEqual([b.expires_at_ns for b in restored.strategy.holdings.active],
            [b.expires_at_ns for b in self.old.strategy.holdings.active])
        self.assertEqual(restored.runner.position_manager.position(live.CLIENT_ID, 'rb_main'), -6)
        self.assertEqual(restored.ledger.snapshot(restored.references.instrument_id).short_total, 6)
        self.assertEqual(self.f.path.read_bytes(), self.before)

    def test_only_expired_batches_leave_and_no_predictor_call_can_create_a_batch(self):
        recording = self.assemble('recording')
        live.HybridCheckpoint(recording.strategy).restore_state(
            self.saved.payload['component_states']['hybrid'])
        recording.runner.target_store.restore(self.old.runner.target_store.all())
        recording.runner.hold_restored_target(live.CLIENT_ID, 1)
        recording.runner._session_check = lambda: True
        recording.runner.instrument_health = None
        recording.runner._clock_ns = lambda: ns('2026-10-28 09:07:02')
        recording.runner.start()
        try:
            recording.runner.accept_bars = True
            recording.runner.first_complete_bar_ns = 0
            recording.runner.expire_due()
            self.assertEqual(recording.strategy.last_targets, {'rb2701': Decimal(-4)})
            self.assertEqual(len(recording.strategy.holdings.active), 2)
            recording.runner._clock_ns = lambda: ns('2026-10-28 09:10:02')
            recording.runner.expire_due()
            self.assertEqual(recording.strategy.last_targets, {'rb2701': Decimal(0)})
            self.assertFalse(recording.strategy.holdings.active)
            self.assertIsNone(recording.strategy.predictor.on_bar({'anything': 'ignored'}))
            self.assertEqual(recording.strategy.config.max_contracts, 10)
        finally:
            recording.runner.stop()

    def test_ctp_planner_closes_six_short_contracts_and_rejects_all_open_legs(self):
        session = self.assemble('simnow')
        session.manager.restore()
        instrument = session.references.instrument_id
        session.execution.prices.update(instrument, 100, ns('2026-10-28 09:10'))
        planner = recovery.CloseOnlyPlanner(session.client.planner)
        def request(target):
            return NS(targets={instrument: Decimal(target)}, client_id=live.CLIENT_ID,
                strategy_id=live.CLIENT_ID, revision=2, metadata={})
        orders = planner.plan(request(0))
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].side, OrderSide.BUY)
        self.assertEqual(orders[0].quantity, 6)
        self.assertEqual(orders[0].position_effect, PositionEffect.CLOSE_TODAY)
        self.assertTrue(orders[0].reduce_only)
        with self.assertRaisesRegex(RuntimeError, '禁止开仓'):
            planner.plan(request(-7))
        self.assertEqual(self.f.path.read_bytes(), self.before)

    def test_broker_total_and_bucket_checks_are_mandatory_even_with_off_policy(self):
        session = self.assemble('simnow')
        session.manager.restore()
        transport = Mock()
        transport.query_gross_positions.return_value = {'rb2701.SHFE': (Decimal(0), Decimal(6))}
        transport.query_position_details.return_value = [dict(InstrumentID='rb2701', ExchangeID='SHFE',
            Position=6, PosiDirection='3', PositionDate='1', HedgeFlag='1', PositionCost=6000)]
        self.assertEqual(recovery.validate_broker_positions(session, transport), (0, 6))
        transport.query_position_details.return_value[0]['PositionDate'] = '2'
        with self.assertRaisesRegex(RuntimeError, '分桶不一致'):
            recovery.validate_broker_positions(session, transport)
        transport.query_gross_positions.return_value = {}
        with self.assertRaisesRegex(RuntimeError, '总仓不一致'):
            recovery.validate_broker_positions(session, transport)

    def test_backup_and_process_locks_do_not_rewrite_the_checkpoint(self):
        lifecycle = object.__new__(recovery.RecoveryLifecycle)
        lifecycle.state_file, lifecycle.saved = self.f.path, self.saved
        with ExitStack() as resources, redirect_stdout(StringIO()):
            lifecycle.lock_state(resources)
            backups = list(self.f.root.glob('*.before-expired-recovery-*.bak'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), self.before)
            with ExitStack() as other, self.assertRaisesRegex(RuntimeError, '已有进程'):
                lifecycle.lock_state(other)
        self.assertEqual(self.f.path.read_bytes(), self.before)

    def test_recovery_runtime_assembly_uses_saved_ten_hand_config_without_loading_a_model(self):
        transport=fixture.Transport('20261028')
        original=fixture.CtpNativeTraderDriver
        def make_driver(*args, **kwargs):
            kwargs['enable_test_orders']=True
            kwargs['enable_simnow_orders']=False
            return original(*args, **kwargs)
        with patch.object(live,'build_simnow_transport',return_value=transport), \
                patch.dict('os.environ',{'CTP_MD_ADDRESS':'tcp://fixture:30011'}), \
                patch('bomber.framework.trader.execution.ctp.CtpNativeTraderDriver',side_effect=make_driver):
            runtime=recovery.build_runtime(state_file=self.f.path, release_path=self.f.release,
                directions_file=self.f.directions, signal_config=self.f.signal,
                market_config=live.MarketSourceConfig(), expected_trading_day='20261028',
                expected_source_day='20261027')
            session=runtime.assemble_session(self.inputs,NS(trading_day='20261028'))
        self.assertIsInstance(session.strategy.predictor,recovery.NoEntryPredictor)
        self.assertIsInstance(session.client.planner,recovery.CloseOnlyPlanner)
        self.assertEqual(session.strategy.config.max_contracts,10)
        self.assertIsNone(session.strategy.config.max_entry_contracts)
        session.manager.restore()
        self.assertEqual(session.strategy.last_targets,{'rb2701':Decimal(-6)})
        self.assertEqual(len(session.strategy.holdings.active),3)
        self.assertEqual(self.f.path.read_bytes(),self.before)
        self.assertFalse(session.runner.accept_bars)
        instrument=session.references.instrument_id
        stamp=ns('2026-10-28 09:10')-1
        session.bar_feed._emit_bar(fixture.base.make_bar(instrument_id=instrument,meta=self.inputs.references.instrument_meta(),
            ts_event=stamp,ts_init=stamp+1,open=100,high=100,low=100,close=100,volume=2))
        self.assertEqual(session.execution.prices.get(instrument).price,100)
        self.assertFalse(session.runner.accept_bars)
        self.assertEqual(session.strategy.last_targets,{'rb2701':Decimal(-6)})
        self.assertEqual(self.f.path.read_bytes(),self.before)

    def test_recovery_does_not_report_success_before_broker_confirms_flat_and_no_active_orders(self):
        lifecycle=object.__new__(recovery.RecoveryLifecycle)
        lifecycle.confirmed_flat=False
        lifecycle.transport=Mock()
        lifecycle.driver=NS(reconcile_active_orders=Mock(return_value=NS(orders=())))
        session=NS(strategy=NS(holdings=NS(active=()),_targets_pending=False,last_targets={'rb2701':Decimal(0)}),
            ledger=NS(snapshot=lambda _:NS(long_total=0,short_total=0)),references=self.inputs.references,
            client=NS(order_state_machine=NS(states=lambda:()),report_errors=()),
            runner=NS(failure=None,position_manager=NS(snapshot=lambda:NS(working_quantities={}))))
        with patch.object(live.HybridSessionLifecycle,'poll',return_value=True), \
                patch.object(recovery,'validate_broker_positions',side_effect=RuntimeError('broker not flat')):
            with self.assertRaisesRegex(RuntimeError,'broker not flat'):
                lifecycle.poll(session)
        self.assertFalse(lifecycle.confirmed_flat)
        with self.assertRaisesRegex(RuntimeError,'尚未全部成交'):
            lifecycle.verify(session)
        with patch.object(live.HybridSessionLifecycle,'poll',return_value=True), \
                patch.object(recovery,'validate_broker_positions',return_value=(0,0)),redirect_stdout(StringIO()):
            self.assertFalse(lifecycle.poll(session))
        self.assertTrue(lifecycle.confirmed_flat)
        lifecycle.verify(session)

    def test_normal_live_entry_still_rejects_ten_contracts(self):
        with self.assertRaisesRegex(ValueError, '1至5手'):
            live.build_runtime(product='RB', release_path=self.f.release, directions_file=self.f.directions,
                signal_config=self.f.signal, max_contracts=10,
                expected_trading_day='20261028', expected_source_day='20261027')


if __name__ == '__main__':
    unittest.main()
