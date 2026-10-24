"""交易分钟期限、程序断线与检查点恢复；日历为显式离线夹具。"""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from bomber.framework.calendars import (CalendarSnapshot, SessionBinding, SessionCalendar,
    SessionOverride, SessionSegment, TradingHoursSnapshot)
from tests.test_demo_hybrid_holdings import strategy_module

holding = import_module('demos.10_hybrid_transformer.holding')
HybridCheckpoint = import_module('demos.10_hybrid_transformer.checkpoint').HybridCheckpoint
ZONE = ZoneInfo('Asia/Shanghai')
MINUTE = 60_000_000_000


def ns(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=ZONE).timestamp()) * 10**9


def sessions(product='RB', *, no_night=False, night_end='23:00', offset=0):
    dates = CalendarSnapshot('fixture', 'v1', date(2026, 10, 9), date(2026, 10, 13),
        (date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)), 'explicit-test')
    parts = ([('am', '09:30', '11:30'), ('pm', '13:00', '15:00')] if product == 'IF' else
             [('am1', '09:00', '10:15'), ('am2', '10:30', '11:30'), ('pm', '13:30', '15:00')])
    rows = [(product, SessionSegment(key, 'TRADING_DAY', 0, start, 0, end))
            for key, start, end in parts]
    if product != 'IF':
        rows.append((product, SessionSegment('night', 'NIGHT_START', 0, '21:00', offset, night_end)))
    overrides = ((SessionOverride('NIGHT_START', date(2026, 10, 9), 'CANCEL', 'NIGHT'),)
                 if no_night else ())
    return SessionCalendar(TradingHoursSnapshot(tuple(rows), 'fixture'), dates,
        SessionBinding(product, product, dates.calendar_id, 'Asia/Shanghai'), overrides=overrides)


class CalendarMinuteTests(unittest.TestCase):
    def test_break_and_night_and_index_day_are_skipped(self):
        cases = [('RB', '2026-10-09 10:12', '2026-10-09 10:32'),
                 ('RB', '2026-10-09 14:57', '2026-10-09 21:02'),
                 ('RB', '2026-10-09 14:58', '2026-10-09 21:03'),
                 ('IF', '2026-10-09 14:57', '2026-10-12 09:32'),
                 ('IF', '2026-10-09 14:58', '2026-10-12 09:33')]
        for product, start, end in cases:
            with self.subTest(product=product, start=start):
                stamps = sessions(product).next_minutes(ns(start) - 1, 5)
                self.assertEqual(stamps[-1] + 1, ns(end))
                self.assertEqual(len(stamps), 5)

    def test_holiday_night_cancellation_and_midnight(self):
        self.assertEqual(sessions(no_night=True).next_minutes(ns('2026-10-09 14:58') - 1, 5)[-1] + 1,
                         ns('2026-10-12 09:03'))
        overnight = sessions(night_end='02:30', offset=1)
        self.assertEqual(overnight.next_minutes(ns('2026-10-09 23:58') - 1, 5)[-1] + 1,
                         ns('2026-10-10 00:03'))

    def test_start_end_boundary_and_unknown_future(self):
        calendar = sessions()
        self.assertEqual(calendar.next_minutes(ns('2026-10-09 10:15') - 1, 1),
                         (ns('2026-10-09 10:31') - 1,))
        self.assertEqual(calendar.next_minutes(ns('2026-10-09 21:00'), 1),
                         (ns('2026-10-09 21:01') - 1,))
        with self.assertRaises(RuntimeError):
            calendar.next_minutes(ns('2026-10-13 14:58') - 1, 5)
        for count in (-1, True, 1.5, 10001):
            with self.subTest(count=count), self.assertRaises(ValueError):
                calendar.next_minutes(ns('2026-10-09 10:15'), count)


class HoldingClockTests(unittest.TestCase):
    def ledger(self, **options):
        return holding.PeriodHoldings(5, 10, calendar=sessions(**options))

    def open(self, ledger, text='2026-10-09 14:58', signal=1):
        return ledger.advance('rb2701', signal, 'source-label', timestamp_ns=ns(text)).opened

    def test_absolute_expiry_survives_lunch_without_intermediate_bars(self):
        ledger = self.ledger()
        batch = self.open(ledger, '2026-10-09 10:12')
        self.assertEqual(batch.expires_at_ns, ns('2026-10-09 10:32'))
        self.assertFalse(ledger.expire(ns('2026-10-09 10:20')).expired)
        self.assertEqual(ledger.expire(ns('2026-10-09 10:32')).expired, (batch,))

    def test_disconnect_advances_slots_by_calendar_without_fictional_orders(self):
        ledger = self.ledger()
        batch = self.open(ledger, '2026-10-09 09:01')
        update = ledger.advance('rb2701', 1, 'live', timestamp_ns=ns('2026-10-09 09:07'))
        self.assertEqual(update.bar_index, 6)
        self.assertEqual(update.slot, 1)
        self.assertEqual(update.expired, (batch,))
        self.assertEqual(len(update.active), 1)
        self.assertEqual(update.opened.opened_at_ns, ns('2026-10-09 09:07'))

    def test_json_restore_keeps_identity_budget_deadline_and_duplicate_guard(self):
        first = self.ledger()
        batch = self.open(first)
        state = json.loads(json.dumps(first.snapshot_state()))
        restored = self.ledger()
        restored.restore_state(state)
        self.assertEqual(restored.snapshot_state(), state)
        self.assertFalse(restored.expire(ns('2026-10-09 21:02')).expired)
        self.assertEqual(restored.expire(ns('2026-10-09 21:03')).expired[0].batch_id, batch.batch_id)
        update = restored.advance('rb2701', 1, 'live', timestamp_ns=ns('2026-10-09 21:04'))
        self.assertNotEqual(update.opened.batch_id, batch.batch_id)
        self.assertEqual(update.bar_index, 6)
        with self.assertRaises(ValueError):
            restored.advance('rb2701', 1, 'duplicate', timestamp_ns=ns('2026-10-09 21:04') - 1)

    def test_recovery_clock_expires_overdue_batch_and_late_bars_cannot_open(self):
        ledger = self.ledger()
        self.open(ledger)
        ledger.expire(ns('2026-10-09 21:05'))
        update = ledger.advance('rb2701', 1, 'old-history', timestamp_ns=ns('2026-10-09 21:01'))
        self.assertIsNone(update.opened)
        self.assertFalse(update.active)

    def test_restore_rejects_changed_rules_and_corrupted_deadline_without_mutation(self):
        ledger = self.ledger()
        self.open(ledger)
        state = ledger.snapshot_state()
        with self.assertRaises(ValueError):
            self.ledger(no_night=True).restore_state(state)
        for field, value in [('expires_at_ns', ns('2026-10-09 21:04')), ('slot', 3),
                             ('quantity', '11'), ('quantity', 'NaN'), ('batch_id', 0)]:
            changed = deepcopy(state)
            changed['active'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                ledger.restore_state(changed)
            self.assertEqual(ledger.snapshot_state(), state)

    def test_label_variants_same_deadline_and_nontrading_bar_is_rejected(self):
        left, right = self.ledger(), self.ledger()
        a = left.advance('rb2701', 1, '14:57', timestamp_ns=ns('2026-10-09 14:58'))
        b = right.advance('rb2701', 1, '14:57', timestamp_ns=ns('2026-10-09 14:58') - 1)
        self.assertEqual(a.opened.expires_at_ns, b.opened.expires_at_ns)
        with self.assertRaises(ValueError):
            self.open(self.ledger(), '2026-10-09 10:17')


class StrategyRecoveryTests(unittest.TestCase):
    def strategy(self):
        predictor = NS(on_bar=Mock(return_value={'signal': 1, 'predicted_z': .1}))
        config = strategy_module.HybridTransformerConfig('RB', 'SHFE', 'test', 10, 5, 1, Decimal(10), record_signals=True)
        strategy = strategy_module.HybridTransformerStrategy('hybrid-test',
            NS(snapshot=lambda ns: NS(instrument=lambda *args: 'rb2701')),
            predictor, config, calendar=sessions())
        return strategy

    def send(self, strategy, text):
        event = NS(ts_event=ns(text),
            bar=NS(bar_type=NS(instrument_id=NS(symbol='rb2701', venue='SHFE'))),
            factors=dict(open=100, high=101, low=99, close=100, volume=2, value=2000,
                         open_interest=20, trade_time=datetime.fromisoformat(text)))
        strategy.on_custom_bar(str(event.bar.bar_type.instrument_id), event)
        return event

    def test_restart_with_cold_model_expires_by_clock_and_rejects_mismatched_targets(self):
        first = self.strategy()
        self.send(first, '2026-10-09 14:58')
        saved = json.loads(json.dumps(HybridCheckpoint(first).snapshot_state()))
        second = self.strategy()
        checkpoint = HybridCheckpoint(second)
        checkpoint.restore_state(saved)
        checkpoint.validate_targets(NS(get=lambda key: NS(revision=first._revision, targets={'rb_main': Decimal(2)})))
        with self.assertRaises(ValueError):
            checkpoint.validate_targets(NS(get=lambda key: NS(revision=first._revision, targets={'rb_main': Decimal(0)})))
        second.on_time(ns('2026-10-09 21:04'))
        second.predictor.on_bar.assert_not_called()
        self.assertEqual(second.last_targets, {'rb2701': Decimal(0)})
        self.assertFalse(second.holdings.active)
        self.assertGreater(second._revision, first._revision)

    def test_bad_model_output_still_expires_positions(self):
        strategy = self.strategy()
        self.send(strategy, '2026-10-09 14:58')
        strategy.predictor.on_bar.side_effect = RuntimeError('model-warming-failed')
        with self.assertRaisesRegex(RuntimeError, 'model-warming'):
            self.send(strategy, '2026-10-09 21:03')
        self.assertEqual(strategy.last_targets, {'rb2701': Decimal(0)})
        self.assertFalse(strategy.holdings.active)

    def test_unknown_new_deadline_cannot_prevent_existing_batch_expiry(self):
        strategy = self.strategy()
        self.send(strategy, '2026-10-13 14:53')
        with self.assertRaises(RuntimeError):
            self.send(strategy, '2026-10-13 14:58')
        self.assertFalse(strategy.holdings.active)
        self.assertEqual(strategy.last_targets, {'rb2701': Decimal(0)})

    def test_expiry_and_renewal_on_same_bar_keep_net_target(self):
        strategy = self.strategy()
        self.send(strategy, '2026-10-09 14:58')
        self.send(strategy, '2026-10-09 21:03')
        self.assertEqual(len(strategy.targets), 1)
        self.assertEqual(len(strategy.holdings.active), 1)
        self.assertEqual(strategy.holdings.active[0].expires_at_ns, ns('2026-10-09 21:08'))

    def test_unchanged_target_renewal_still_saves_new_batch_state(self):
        strategy = self.strategy()
        saved = []
        strategy.bind_checkpoint(lambda: saved.append(HybridCheckpoint(strategy).snapshot_state()))
        self.send(strategy, '2026-10-09 14:58')
        self.send(strategy, '2026-10-09 21:03')
        self.assertEqual(len(strategy.targets), 1)
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[-1]['holdings']['active'][0]['expires_at_ns'], ns('2026-10-09 21:08'))
        self.assertNotEqual(saved[0]['holdings']['active'][0]['batch_id'], saved[1]['holdings']['active'][0]['batch_id'])
        with self.assertRaises(ValueError):
            strategy.bind_checkpoint(lambda: None)

    def test_minute_end_minus_one_and_boundary_are_deduplicated(self):
        strategy = self.strategy()
        event = self.send(strategy, '2026-10-09 14:58')
        event.ts_event -= 1
        strategy.on_custom_bar(str(event.bar.bar_type.instrument_id), event)
        self.assertEqual(strategy.bars_used, 1)

    def test_failed_target_publication_is_retried_without_opening_a_second_batch(self):
        strategy = self.strategy()
        publish = strategy.set_targets
        def fail(*args, **kwargs):
            publish(*args, **kwargs)
            raise RuntimeError('transport failure')
        strategy.set_targets = fail
        with self.assertRaises(RuntimeError):
            self.send(strategy, '2026-10-09 14:58')
        self.assertTrue(strategy._targets_pending)
        strategy.set_targets = publish
        strategy.on_time(ns('2026-10-09 14:58'))
        self.assertFalse(strategy._targets_pending)
        self.assertEqual(len(strategy.holdings.active), 1)
        self.assertEqual(strategy.last_targets, {'rb2701': Decimal(2)})


if __name__ == '__main__':
    unittest.main()
