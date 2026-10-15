"""跨期预热使用实际策略/历史服务/日历，仅行情标识和数据库传输为受控边界。"""
from datetime import date
from decimal import Decimal
from importlib import import_module
from threading import RLock
from types import SimpleNamespace as NS
from unittest.mock import Mock
import unittest

from tests import test_execution_composition as fixtures


class Instrument(fixtures.Instrument):
    @property
    def symbol(self): return self.value.split(".")[0]


class CalendarHistoryTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExecutionCompositionTests.setUp(self)
        self.strategy_module = import_module("demos.03_calendar_spread.strategy")
        self.history = import_module("demos.03_calendar_spread.history")
        self.api = import_module("bomber.framework.dataprep.history")
        from tests.session_fixtures import declared_day_sessions
        from bomber.framework.calendars.sessions import datetime_ns
        self.calendar = declared_day_sessions("AU", date(2026, 10, 15))
        end = datetime_ns(self.calendar.windows(date(2026, 10, 15))[0].end)
        self.stamps = self.calendar.last_minutes(end, 5)
        self.first, self.second = Instrument("au2612.SHFE"), Instrument("au2702.SHFE")
        self.strategy = self.strategy_module.CalendarSpreadStrategy("calendar", self.strategy_module.CalendarSpreadConfig(
            lookback=2, entry_z=1, exit_z=0.25), (self.strategy_module.CalendarSelection(
                date(2026, 10, 15), 0, self.first.symbol, self.second.symbol),),
            {i.symbol: i for i in (self.first, self.second)})
        self.strategy.set_targets = Mock()
        owner = self
        self.rows = {str(self.first): [self.bar(self.first, s, 100 + n) for n, s in enumerate(self.stamps)],
                     str(self.second): [self.bar(self.second, s, 90) for s in self.stamps]}
        class Provider:
            def open(self): pass
            def close(self): pass
            def read(self, request): return tuple(owner.rows[request.instrument_id])
        class Factory:
            @staticmethod
            def create_for(*args): return Provider()
        service = self.api.HistoryService(database_config={}, factory=Factory, missing_policy="allow")
        self.coordinator = self.history.CalendarHistory(service, self.calendar, 2)
        self.runner = NS(strategy=self.strategy, instruments={"secondary": self.first, "far": self.second},
            fixed_ids=frozenset((self.first, self.second)), _submit_lock=RLock(), history_buffer=[],
            first_complete_bar_ns=self.stamps[0])

    def bar(self, instrument, stamp, price):
        return self.api.HistoryBar(str(instrument), stamp, close=price)

    def event(self, instrument, stamp, price):
        return NS(ts_event=stamp, bar_type=NS(instrument_id=instrument),
                  close=NS(as_decimal=lambda: Decimal(price)))

    def warm(self, count=2):
        self.coordinator.warm(self.runner, self.stamps[count - 1] + 1)

    def test_history_warms_raw_spread_without_targets_revision_or_direction(self):
        self.warm()
        self.assertEqual(tuple(self.strategy.signal.history), (10.0, 11.0))
        self.assertEqual(self.strategy.complete_frames, 2)
        self.assertEqual(self.strategy.direction, 0)
        self.assertEqual(self.strategy._revision, 0)
        self.strategy.set_targets.assert_not_called()
        self.assertEqual(self.coordinator.last_audit["loaded"], 2)
        self.assertEqual(len(self.coordinator.last_audit["legs"]), 2)

    def test_first_complete_live_frame_can_generate_target_after_history(self):
        self.warm()
        stamp = self.stamps[2]
        self.strategy.on_bar(str(self.first), self.event(self.first, stamp, 90))
        self.strategy.set_targets.assert_not_called()
        self.strategy.on_bar(str(self.second), self.event(self.second, stamp, 90))
        self.strategy.set_targets.assert_called_once()
        self.assertEqual(self.strategy.direction, 1)
        self.assertEqual(self.strategy.complete_frames, 3)

    def test_missing_leg_fail_reports_both_legs_before_mutating_strategy(self):
        self.rows[str(self.second)] = []
        with self.assertRaises(self.api.HistoryUnavailable): self.warm()
        self.assertEqual(self.strategy.complete_frames, 0)
        self.assertEqual(self.coordinator.last_audit["legs"][str(self.first)]["loaded"], 2)
        self.assertEqual(self.coordinator.last_audit["legs"][str(self.second)]["loaded"], 0)
        self.strategy.set_targets.assert_not_called()

    def test_allow_uses_only_intersection_and_does_not_cross_join_minutes(self):
        self.coordinator.missing_policy = "allow"
        self.rows[str(self.first)] = [self.rows[str(self.first)][0]]
        self.rows[str(self.second)] = [self.rows[str(self.second)][1]]
        self.warm()
        self.assertEqual(self.strategy.complete_frames, 0)
        self.assertEqual(self.coordinator.last_audit["missing"], 2)

    def test_allow_retains_real_complete_frames_without_fabricating_missing_leg(self):
        self.coordinator.missing_policy = "allow"
        self.rows[str(self.second)] = [self.rows[str(self.second)][0]]
        self.warm()
        self.assertEqual(self.strategy.complete_frames, 1)
        self.assertEqual(tuple(self.strategy.signal.history), (10.0,))

    def test_buffer_fills_missing_database_minute_and_deduplicates(self):
        self.rows[str(self.second)] = [self.rows[str(self.second)][0]]
        self.runner.history_buffer = [self.event(self.second, self.stamps[1], 90)] * 2
        self.warm()
        self.assertEqual(self.strategy.complete_frames, 2)
        self.assertIn("live-buffer", self.coordinator.last_audit["legs"][str(self.second)]["sources"])

    def test_flush_waits_for_both_legs_then_advances_only_once(self):
        self.warm()
        self.runner.history_buffer = [self.event(self.first, self.stamps[2], 102)]
        self.coordinator.flush(self.runner)
        self.assertEqual(self.strategy.complete_frames, 2)
        self.runner.history_buffer.append(self.event(self.second, self.stamps[2], 90))
        self.coordinator.flush(self.runner)
        self.coordinator.flush(self.runner)
        self.assertEqual(self.strategy.complete_frames, 3)
        self.strategy.set_targets.assert_not_called()

    def test_restore_direction_and_target_revision_are_not_replayed(self):
        self.strategy.restore_target_revision(7)
        self.strategy.rewarm_after_restore(-1)
        self.warm()
        self.assertEqual((self.strategy.direction, self.strategy._revision), (-1, 7))
        self.assertTrue(self.strategy._force_target)
        self.strategy.set_targets.assert_not_called()

    def test_duplicate_or_changed_pair_warmup_is_rejected(self):
        self.warm()
        pair = (self.first.symbol, self.second.symbol)
        with self.assertRaises(ValueError):
            self.strategy.warm_closed(self.stamps[1], 100, 90, pair)
        with self.assertRaises(ValueError):
            self.strategy.warm_closed(self.stamps[2], 100, 90, tuple(reversed(pair)))
        self.assertEqual(self.strategy.complete_frames, 2)

    def test_invalid_later_raw_frame_does_not_partially_mutate_window(self):
        self.rows[str(self.second)][1] = self.api.HistoryBar(str(self.second), self.stamps[1], adjusted_close=90)
        with self.assertRaises(self.api.HistoryError): self.warm()
        self.assertEqual(self.strategy.complete_frames, 0)
        self.assertEqual(tuple(self.strategy.signal.history), ())

    def test_database_outage_cannot_be_hidden_by_allow_missing(self):
        self.coordinator.missing_policy = "allow"
        self.coordinator.service._read = Mock(side_effect=self.api.HistoryUnavailable("outage"))
        with self.assertRaises(self.api.HistoryUnavailable): self.warm()
        self.assertEqual(self.strategy.complete_frames, 0)
        self.assertTrue(all(row["unavailable_sources"] for row in self.coordinator.last_audit["legs"].values()))

    def test_conflicting_live_and_database_price_is_fatal(self):
        self.runner.history_buffer = [self.event(self.first, self.stamps[0], 900)]
        with self.assertRaises(self.api.HistoryError): self.warm()
        self.assertEqual(self.strategy.complete_frames, 0)


if __name__ == "__main__":
    unittest.main()
