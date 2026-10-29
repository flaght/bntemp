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


if __name__=='__main__':unittest.main()
