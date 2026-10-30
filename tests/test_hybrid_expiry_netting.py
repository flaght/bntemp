"""Live到期前先等同分钟信号，净额合并与5手/每批1手；不联网、不报单。"""
from datetime import datetime
from decimal import Decimal
from unittest.mock import Mock, patch
import json
import unittest

from tests import test_hybrid_continuous as continuous_fixture
from tests.run_hybrid_live_offline import base, live
from tests.test_demo_hybrid_live import ns


class ExpiryNettingTests(unittest.TestCase):
    def setUp(self):
        fixture=continuous_fixture.ContinuousTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture=fixture
        self.session,_,_=fixture.session()
        self.runner=self.session.runner
        self.now=ns('2026-10-28 09:01:02')
        self.runner._clock_ns=lambda:self.now
        self.runner._session_check=lambda:True
        self.runner.instrument_health=None
        self.session.strategy.predictor.on_bar=Mock(return_value={'predicted_z':.1,'signal':1})
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.runner.accept_bars=True
        self.runner.first_complete_bar_ns=0

    def send(self,label,signal=1):
        self.session.strategy.predictor.on_bar.return_value={'predicted_z':.1*signal,'signal':signal}
        instrument=self.session.references.instrument_id
        stamp=ns(label)+live.MINUTE-1
        self.now=stamp+3_000_000_001
        bar=base.make_bar(instrument_id=instrument,meta=self.session.references.instrument_meta(),
            ts_event=stamp,ts_init=self.now,open=100,high=100,low=100,close=100,volume=2)
        event=base.make_custom_bar(bar,dict(open=100,high=100,low=100,close=100,
            volume=2,value=2000,open_interest=500,
            trade_time=datetime.fromisoformat(label).replace(tzinfo=self.session.strategy.calendar.zone)))
        self.runner.publish('model-bars',event)

    def fill_five_batches(self):
        for minute in range(5):
            self.send(f'2026-10-28 09:{minute:02d}:00')
        self.assertEqual(self.session.strategy.config.max_contracts,5)
        self.assertEqual(self.session.strategy.config.max_entry_contracts,1)
        self.assertEqual(self.session.strategy.last_targets,{'rb2701':Decimal(5)})
        self.assertEqual([batch.quantity for batch in self.session.strategy.holdings.active],[1]*5)

    def wait_at_expiry(self):
        self.now=ns('2026-10-28 09:06:00')+200_000_000
        before=len(self.session.client.requests)
        self.runner.expire_due()
        self.assertEqual(len(self.session.client.requests),before)
        self.assertEqual(len(self.session.strategy.holdings.active),5)
        return before

    def test_clock_before_same_direction_bar_does_not_publish_close_then_reopen(self):
        self.fill_five_batches()
        initial=self.session.strategy.holdings.active[0]
        count=self.wait_at_expiry()
        self.send('2026-10-28 09:05:00',1)
        self.assertEqual(len(self.session.client.requests),count)
        self.assertEqual(self.session.strategy.last_targets,{'rb2701':Decimal(5)})
        active=self.session.strategy.holdings.active
        self.assertNotIn(initial,active)
        self.assertEqual(len(active),5)
        self.assertEqual(active[-1].expires_at_ns,ns('2026-10-28 09:11'))
        self.assertEqual(self.session.strategy.batch_records[-2]['event'],'expire')
        self.assertEqual(self.session.strategy.batch_records[-1]['event'],'open')

    def test_zero_at_expiry_closes_only_due_batch_and_does_not_open_opposite(self):
        self.fill_five_batches()
        previous=self.session.strategy.holdings.active
        count=self.wait_at_expiry()
        self.send('2026-10-28 09:05:00',0)
        self.assertEqual(len(self.session.client.requests),count+1)
        self.assertEqual(self.session.strategy.holdings.active,previous[1:])
        self.assertEqual(self.session.strategy.last_targets,{'rb2701':Decimal(4)})

    def test_reverse_at_expiry_publishes_one_combined_net_target(self):
        self.fill_five_batches()
        count=self.wait_at_expiry()
        self.send('2026-10-28 09:05:00',-1)
        self.assertEqual(len(self.session.client.requests),count+1)
        self.assertEqual(self.session.strategy.last_targets,{'rb2701':Decimal(3)})
        active=self.session.strategy.holdings.active
        self.assertEqual([batch.quantity for batch in active],[1,1,1,1,-1])
        self.assertEqual(sum(abs(batch.quantity) for batch in active),5)
        self.assertEqual(dict(self.session.client.requests[-1].logical_targets),{'rb_main':Decimal(3)})

    def test_missing_whole_expiry_minute_still_exits_and_late_bar_cannot_reopen_it(self):
        self.fill_five_batches()
        count=self.wait_at_expiry()
        self.now=ns('2026-10-28 09:07:00')+200_000_000
        self.runner.expire_due()
        self.assertEqual(len(self.session.client.requests),count+1)
        self.assertEqual(self.session.strategy.last_targets,{'rb2701':Decimal(3)})
        # 使用当前墙钟发布迟到的09:05分钟，量化账本不能再补开这批历史仓位。
        self.session.strategy.predictor.on_bar.return_value={'predicted_z':.1,'signal':1}
        instrument=self.session.references.instrument_id
        stamp=ns('2026-10-28 09:06')-1
        bar=base.make_bar(instrument_id=instrument,meta=self.session.references.instrument_meta(),
            ts_event=stamp,ts_init=self.now,open=100,high=100,low=100,close=100,volume=2)
        self.runner.publish('model-bars',base.make_custom_bar(bar,dict(open=100,high=100,low=100,
            close=100,volume=2,value=2000,open_interest=500,
            trade_time=datetime.fromisoformat('2026-10-28T09:05:00+08:00'))))
        self.assertEqual(len(self.session.client.requests),count+1)
        self.assertEqual(self.session.strategy.last_targets,{'rb2701':Decimal(3)})

    def test_cold_restored_model_does_not_renew_using_an_old_signal(self):
        self.fill_five_batches()
        self.session.strategy.last_prediction=None
        self.now=ns('2026-10-28 09:06:00')+200_000_000
        self.runner.expire_due()
        self.assertEqual(self.session.strategy.last_targets,{'rb2701':Decimal(4)})

    def test_live_cli_defaults_to_five_and_rejects_old_ten_hand_flag(self):
        parsed=live.parse_args(self.fixture.cli()+['--run-forever'])
        self.assertEqual(parsed.max_contracts,5)
        with patch('sys.stderr'),self.assertRaises(SystemExit):
            live.parse_args(self.fixture.cli()+['--run-forever','--max-contracts','10'])

    def test_new_sizing_round_trips_but_old_ten_hand_checkpoint_is_not_silently_rewritten(self):
        self.fill_five_batches()
        checkpoint=live.HybridCheckpoint(self.session.strategy)
        saved=json.loads(json.dumps(checkpoint.snapshot_state()))
        other,_,_=self.fixture.session()
        restored=live.HybridCheckpoint(other.strategy)
        restored.restore_state(saved)
        self.assertEqual(restored.snapshot_state(),saved)
        old=json.loads(json.dumps(saved))
        old['config']['max_contracts']=10
        old['config'].pop('max_entry_contracts')
        with self.assertRaisesRegex(ValueError,'策略配置'):
            restored.restore_state(old)
        self.assertEqual(restored.snapshot_state(),saved)

    def test_per_batch_cap_is_one_even_when_other_product_period_has_larger_slot_budget(self):
        values=json.loads(self.fixture.signal.read_text())
        values['RB']['hold_bars']=3
        self.fixture.signal.write_text(json.dumps(values))
        other,_,_=self.fixture.session()
        self.assertEqual(other.strategy.holdings.capacities,(1,1,1))
        self.assertEqual(other.strategy.holdings.max_contracts,5)
        self.assertEqual(other.strategy.holdings.max_entry_contracts,1)


class ExpiryPriceTests(unittest.TestCase):
    """实际CTP规划器/受控客户端/Driver；只替换transport发送，不连接网络。"""
    market_config = None
    def setUp(self):
        fixture = continuous_fixture.ContinuousTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        fixture.old_session(policy='off', market_config=self.market_config)
        self.session, self.driver, self.transport = fixture.session(
            mode='simnow', policy='off', market_config=self.market_config)
        self.session.manager.restore()
        self.strategy, self.runner = self.session.strategy, self.session.runner
        self.runner.hold_restored_target(self.strategy.strategy_id, self.strategy._revision)
        self.now = ns('2026-10-28 21:05:02')
        self.runner._clock_ns = lambda: self.now
        self.runner._session_check = lambda: True
        self.runner.instrument_health = None
        self.session.client._wall_clock_ns = lambda: self.now
        self.session.client._demo_environment_check = lambda: True
        self.strategy.predictor.workflow.min_raw_bars = live.MODEL_MIN_RAW_BARS
        self.strategy.predictor.on_bar = Mock(side_effect=AssertionError('到期平仓不应调用模型'))
        self.transport.send_order = Mock()
        self.driver.start(lambda _: None)
        self.addCleanup(self.driver.stop)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.session.manager.commit_restore()
        self.session.client.arm_demo(self.session.client.DEMO_CONFIRMATION)

    def emit_price_bar(self, *, stamp=None, instrument=None):
        instrument = instrument or self.session.references.instrument_id
        bar = base.make_bar(instrument_id=instrument, meta=self.session.references.instrument_meta(),
            ts_event=self.now-1_000_000_000 if stamp is None else stamp, ts_init=self.now,
            open=100, high=100, low=100, close=100, volume=2)
        self.session.bar_feed._emit_bar(bar)

    def test_start_without_price_keeps_expired_batch_and_waits_without_submitting(self):
        active = self.strategy.holdings.active
        targets = dict(self.strategy.last_targets)
        saved = self.fixture.path.read_bytes()
        self.runner.begin_bars()
        self.runner.expire_due()
        self.assertTrue(self.runner.accept_bars)
        self.assertEqual(self.strategy.holdings.active, active)
        self.assertLess(active[0].expires_at_ns, self.now)
        self.assertEqual(self.strategy.last_targets, targets)
        self.assertEqual(self.fixture.path.read_bytes(), saved)
        self.transport.send_order.assert_not_called()
        self.strategy.predictor.on_bar.assert_not_called()

    def test_closed_gate_price_is_available_for_first_expiry_order_without_model_warmup(self):
        self.assertFalse(self.runner.accept_bars)
        self.emit_price_bar()
        self.assertIsNotNone(self.session.execution.prices.get(self.session.references.instrument_id))
        self.assertEqual(self.strategy.bars_used, 1)
        self.transport.send_order.assert_not_called()
        self.runner.begin_bars()
        self.assertFalse(self.strategy.holdings.active)
        self.assertEqual(self.strategy.last_targets, {'rb2701': Decimal(0)})
        order = self.transport.send_order.call_args.args[0]
        self.assertEqual(order['Direction'], '1')
        self.assertEqual(order['CombOffsetFlag'], '3')
        self.assertEqual(order['VolumeTotalOriginal'], 1)
        self.assertEqual(order['LimitPrice'], 99)
        self.runner.expire_due()
        self.transport.send_order.assert_called_once()
        self.strategy.predictor.on_bar.assert_not_called()

    def test_stale_price_waits_then_live_handoff_minute_enables_close(self):
        active = self.strategy.holdings.active
        self.emit_price_bar(stamp=self.now-121_000_000_000)
        self.runner.begin_bars()
        self.assertEqual(self.strategy.holdings.active, active)
        self.transport.send_order.assert_not_called()
        # 仍早于首根可决策分钟；原生Bar只给执行端更新参考价。
        self.emit_price_bar()
        self.runner.expire_due()
        self.assertFalse(self.strategy.holdings.active)
        self.transport.send_order.assert_called_once()
        self.strategy.predictor.on_bar.assert_not_called()

    def test_other_instrument_price_cannot_enable_rb_close(self):
        from tests.run_hybrid_live_offline import InstrumentId
        active = self.strategy.holdings.active
        self.emit_price_bar(instrument=InstrumentId.from_str('rb2705.SHFE'))
        self.runner.begin_bars()
        self.assertEqual(self.strategy.holdings.active, active)
        self.transport.send_order.assert_not_called()

    def test_first_native_price_closes_overdue_batch_before_companion_model_bar(self):
        self.runner.begin_bars()
        self.now = ns('2026-10-28 21:07:02')
        self.emit_price_bar(stamp=ns('2026-10-28 21:07')-1)
        self.assertFalse(self.strategy.holdings.active)
        self.transport.send_order.assert_called_once()
        self.strategy.predictor.on_bar.assert_not_called()

    def test_pending_close_from_previous_attempt_also_waits_for_price(self):
        self.strategy.holdings.expire(self.now)
        self.strategy.last_targets = {'rb2701': Decimal(0)}
        self.strategy._targets_pending = True
        self.runner.begin_bars()
        self.assertTrue(self.strategy._targets_pending)
        self.transport.send_order.assert_not_called()
        self.emit_price_bar()
        self.runner.expire_due()
        self.transport.send_order.assert_called_once()
        self.assertFalse(self.strategy._targets_pending)

    def test_unrelated_planner_errors_are_not_suppressed(self):
        self.emit_price_bar()
        with patch.object(self.session.client.planner, 'plan', side_effect=RuntimeError('unexpected planner failure')):
            with self.assertRaisesRegex(RuntimeError, 'unexpected planner failure'):
                self.runner.begin_bars()
        self.transport.send_order.assert_not_called()


class DolphinDbExpiryPriceTests(ExpiryPriceTests):
    market_config = live.MarketSourceConfig(source='dolphindb', table='fixture',
        timestamp_label='start', volume_mode='per-bar')


if __name__=='__main__':unittest.main()
