"""公共 Live 历史加载与衔接边界；仅 SDK 实体沿用离线夹具。"""
from datetime import datetime
from decimal import Decimal
from threading import Event, RLock, Thread
from types import SimpleNamespace as NS
from unittest.mock import Mock
import unittest

from tests.run_hybrid_live_offline import base
from tests.test_demo_hybrid_live import calendar, ns, ZONE
from bomber.framework.dataprep.history import HistoryBar, HistoryService, HistoryError, HistoryUnavailable
from bomber.framework.trader.runtime.live.continuous import LiveWindowContext
from bomber.framework.trader.runtime.live.history import (
    HistoryFrameLoader, history_phase, history_stamps, first_complete_minute,
    buffer_history_bar, complete_buffer_boundary, start_after_history)


def service(rows, *, fail=False):
    class Provider:
        def open(self):
            pass
        def close(self):
            pass
        def read(self, request):
            if fail:
                raise HistoryUnavailable("source unavailable")
            return rows
    return HistoryService(database_config={}, missing_policy="allow",
        factory=NS(create_for=lambda *_: Provider()))


class LiveHistoryTests(unittest.TestCase):
    def setUp(self):
        self.calendar = calendar()
        self.stamps = history_stamps(self.calendar, ns("2026-10-28 09:03"), 3)
        self.sources = {key: (service(tuple(HistoryBar(instrument, stamp, close=100)
            for stamp in self.stamps)), instrument)
            for key, instrument in (("near", "rb2701.SHFE"), ("far", "rb2705.SHFE"))}

    def runner(self):
        return NS(_submit_lock=RLock(), history_buffer=[], history_buffering=True,
            accept_bars=False, market_feed_id="minutes", instruments={"main": "rb2701.SHFE"},
            _clock_ns=lambda: ns("2026-10-28 09:05"), publish=Mock())

    def bar(self, stamp=None, instrument="rb2701.SHFE"):
        return NS(ts_event=self.stamps[0] if stamp is None else stamp,
            bar_type=NS(instrument_id=instrument))

    def test_windows_use_completed_trading_minutes_and_skip_lunch(self):
        result = history_stamps(self.calendar, ns("2026-10-28 13:32"), 4)
        self.assertEqual(result, tuple(ns(stamp)-1 for stamp in (
            "2026-10-28 11:29", "2026-10-28 11:30", "2026-10-28 13:31", "2026-10-28 13:32")))
        self.assertEqual(history_stamps(self.calendar, ns("2026-10-28 13:32"), 100,
            after_ns=result[1]), result[2:])
        for before in (result[0], result[0]-1):
            with self.assertRaises(HistoryError):
                history_stamps(self.calendar, before, 3, after_ns=result[0])

    def test_auto_phase_requires_all_calendars_open_and_explicit_phase_wins(self):
        active = datetime(2026, 10, 28, 10, tzinfo=ZONE)
        closed = NS(zone=ZONE, window=lambda _: None)
        self.assertEqual(history_phase("auto", (self.calendar,), now=active), "recovery")
        self.assertEqual(history_phase("auto", (self.calendar, closed), now=active), "preopen")
        self.assertEqual(history_phase("preopen", (self.calendar,), now=active), "preopen")

    def test_partial_start_minute_is_excluded_at_exact_and_partial_boundaries(self):
        self.assertEqual(first_complete_minute(ns("2026-10-28 09:00")), ns("2026-10-28 09:01")-1)
        self.assertEqual(first_complete_minute(ns("2026-10-28 09:00:01")), ns("2026-10-28 09:02")-1)

    def test_strict_coverage_reports_all_sources_before_returning_any_frame(self):
        self.sources["far"] = (service(()), "rb2705.SHFE")
        loader = HistoryFrameLoader()
        with self.assertRaises(HistoryUnavailable):
            loader.load_frames(self.sources, self.stamps, phase="recovery")
        self.assertEqual(loader.last_audit["loaded"], 0)
        self.assertEqual(loader.last_audit["instruments"]["near"]["loaded"], 3)
        self.assertEqual(loader.last_audit["instruments"]["far"]["missing"], 3)

    def test_allow_keeps_same_minute_intersection_without_filling_other_legs(self):
        self.sources["far"] = (service((HistoryBar("rb2705.SHFE", self.stamps[1], close=99),)), "rb2705.SHFE")
        loader = HistoryFrameLoader(missing_policy="allow")
        frames = loader.load_frames(self.sources, self.stamps, phase="recovery")
        self.assertEqual([stamp for stamp, _ in frames], [self.stamps[1]])
        self.assertEqual(set(frames[0][1]), {"near", "far"})
        self.assertEqual(loader.last_audit["missing"], 2)

    def test_buffer_completes_gap_and_exact_duplicates_do_not_add_frames(self):
        self.sources["far"] = (service(()), "rb2705.SHFE")
        bars = tuple(HistoryBar("rb2705.SHFE", stamp, close=99) for stamp in self.stamps)
        frames = HistoryFrameLoader().load_frames(self.sources, self.stamps, phase="recovery",
            live_bars={"far": bars + bars})
        self.assertEqual([stamp for stamp, _ in frames], list(self.stamps))

    def test_source_outage_is_fatal_even_with_allow(self):
        self.sources["far"] = (service((), fail=True), "rb2705.SHFE")
        loader = HistoryFrameLoader(missing_policy="allow")
        with self.assertRaises(HistoryUnavailable):
            loader.load_frames(self.sources, self.stamps, phase="recovery")
        self.assertEqual(loader.last_audit["instruments"]["far"]["unavailable_sources"], ("dolphindb",))

    def test_conflicting_buffer_duplicates_fail_instead_of_selecting_a_price(self):
        conflicting = (HistoryBar("rb2705.SHFE", self.stamps[0], close=99),
                       HistoryBar("rb2705.SHFE", self.stamps[0], close=101))
        with self.assertRaises(HistoryError):
            HistoryFrameLoader().load_frames(self.sources, self.stamps, phase="recovery",
                live_bars={"far": conflicting})

    def test_buffer_rejects_wrong_source_instrument_future_and_overflow(self):
        for feed, event in (("other", self.bar()), ("minutes", self.bar(instrument="other.SHFE")),
                            ("minutes", self.bar(stamp=ns("2026-10-28 09:06")))):
            runner = self.runner()
            with self.assertRaises(HistoryError):
                buffer_history_bar(runner, feed, event)
            self.assertEqual(runner.history_buffer, [])
        runner = self.runner()
        runner.history_buffer = [self.bar()] * 10000
        with self.assertRaises(HistoryError):
            buffer_history_bar(runner, "minutes", self.bar())

    def test_buffer_boundary_waits_for_all_legs_and_ignores_processed_minutes(self):
        events = [self.bar(), self.bar(self.stamps[1])]
        ids = {"rb2701.SHFE", "rb2705.SHFE"}
        self.assertIsNone(complete_buffer_boundary(events, ids, first_ns=self.stamps[0], after_ns=-1))
        events.append(self.bar(self.stamps[0], "rb2705.SHFE"))
        self.assertEqual(complete_buffer_boundary(events, ids, first_ns=self.stamps[0], after_ns=-1),
            self.stamps[0]+1)
        self.assertIsNone(complete_buffer_boundary(events, ids, first_ns=self.stamps[0], after_ns=self.stamps[0]))

    def test_failed_load_or_readiness_never_opens_decisions_or_discards_buffer(self):
        for failing in ("load", "readiness"):
            runner = self.runner()
            event = self.bar()
            runner.history_buffer = [event]
            flush, validate = Mock(), Mock()
            (flush if failing == "load" else validate).side_effect = HistoryUnavailable(failing)
            with self.assertRaises(HistoryUnavailable):
                start_after_history(runner, flush=flush, validate=validate, replay_buffer=True)
            self.assertFalse(runner.accept_bars)
            self.assertTrue(runner.history_buffering)
            self.assertEqual(runner.history_buffer, [event])
            runner.publish.assert_not_called()

    def test_stop_during_history_does_not_check_health_or_reopen_runner(self):
        runner, stop = self.runner(), Event()
        now = datetime(2026, 10, 28, 10, tzinfo=ZONE)
        runner.window_context = LiveWindowContext(self.calendar, self.calendar.window(now), stop,
            "20261027", clock=lambda: now)
        runner.client = Mock()
        validate = Mock()
        start_after_history(runner, flush=stop.set, validate=validate, replay_buffer=True)
        self.assertFalse(runner.accept_bars)
        validate.assert_not_called()
        runner.publish.assert_not_called()
        runner.client.disarm.assert_called_once()

    def test_buffer_replay_is_ordered_and_single_series_can_consume_without_replay(self):
        for replay in (True, False):
            runner = self.runner()
            events = [self.bar(self.stamps[2]), self.bar(self.stamps[0])]
            runner.history_buffer = events
            start_after_history(runner, flush=Mock(), validate=Mock(), replay_buffer=replay)
            self.assertTrue(runner.accept_bars)
            self.assertFalse(runner.history_buffering)
            self.assertEqual(runner.history_buffer, [])
            self.assertEqual([args[1].ts_event for args, _ in runner.publish.call_args_list],
                [self.stamps[0], self.stamps[2]] if replay else [])

    def test_stop_during_final_validation_still_prevents_opening(self):
        runner, stop = self.runner(), Event()
        now = datetime(2026, 10, 28, 10, tzinfo=ZONE)
        runner.window_context = LiveWindowContext(self.calendar, self.calendar.window(now), stop,
            "20261027", clock=lambda: now)
        runner.client = Mock()
        start_after_history(runner, flush=Mock(), validate=stop.set, replay_buffer=True)
        self.assertFalse(runner.accept_bars)
        self.assertTrue(runner.history_buffering)
        runner.publish.assert_not_called()

    def test_feed_arriving_during_handoff_is_delivered_after_gate_opens(self):
        runner, launch, attempted, delivered = self.runner(), Event(), Event(), Event()
        event, outcome = self.bar(), []
        def producer():
            if not launch.wait(1):
                return
            attempted.set()
            buffered = buffer_history_bar(runner, "minutes", event)
            outcome.append(buffered)
            if not buffered:
                runner.publish("minutes", event)
            delivered.set()
        thread = Thread(target=producer, daemon=True)
        thread.start()
        def flush():
            launch.set()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(delivered.is_set())
        start_after_history(runner, flush=flush, validate=Mock())
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, [False])
        runner.publish.assert_called_once_with("minutes", event)
        self.assertEqual(runner.history_buffer, [])


class EmaHistoryIntegrationTests(unittest.TestCase):
    """实际 EMA Runner/历史服务接线；只记录预热调用，不代替原生 EMA 运算。"""
    def setUp(self):
        from tests import run_live_continuous_offline as ema_fixtures
        self.history = ema_fixtures.ema_history
        self.stamps = history_stamps(calendar(), ns("2026-10-28 09:03"), 2)
        runner = object.__new__(self.history.CheckpointedRunner)
        runner._submit_lock = RLock()
        runner._role_binding = NS(instrument_id="rb2701.SHFE")
        runner._clients = {"fixture": Mock(_submit_lock=RLock())}
        runner.market_feed_id = "minutes"
        runner._clock_ns = lambda: ns("2026-10-28 09:05")
        runner.first_complete_bar_ns = self.stamps[0]
        runner.history_buffer, runner.history_buffering = [], True
        runner.accept_bars, runner.failure = False, None
        runner.session_ready = lambda: True
        runner.calendar, runner.history_minutes = calendar(), 2
        runner.references = NS(snapshot=lambda _: NS(instrument=lambda *_: "rb2701", factor=lambda *_: Decimal(2)))
        strategy = NS(bars_used=0, last_processed_ns=-1, config=NS(product="RB"),
            fast=NS(value=0), slow=NS(value=0, initialized=False))
        def warm(stamp, price):
            strategy.bars_used += 1
            strategy.last_processed_ns = stamp
        runner.ema_checkpoint = NS(instrument="rb2701.SHFE", strategy=strategy, warm_closed=Mock(side_effect=warm))
        runner.history_service = service(())
        runner.manager = Mock()
        self.runner = runner

    def test_buffer_warms_once_then_opens_without_replaying_historical_targets(self):
        for stamp in self.stamps:
            bar = base.Bar(ts_event=stamp, bar_type=NS(instrument_id="rb2701.SHFE"),
                close=NS(as_decimal=lambda: Decimal(100)))
            self.runner.publish("minutes", bar)
        self.runner.begin_bars()
        self.assertTrue(self.runner.accept_bars)
        self.assertFalse(self.runner.history_buffering)
        self.assertEqual(self.runner.history_buffer, [])
        self.assertEqual([call.args for call in self.runner.ema_checkpoint.warm_closed.call_args_list],
            [(stamp, Decimal(200)) for stamp in self.stamps])
        self.runner.manager.save.assert_not_called()
        self.runner._clients["fixture"].submit_targets.assert_not_called()

    def test_invalid_buffer_source_disarms_and_keeps_decisions_closed(self):
        bar = base.Bar(ts_event=self.stamps[0], bar_type=NS(instrument_id="rb2701.SHFE"))
        with self.assertRaises(HistoryError):
            self.runner.publish("other", bar)
        self.assertFalse(self.runner.accept_bars)
        self.assertTrue(self.runner.failure)
        self.assertEqual(self.runner.history_buffer, [])
        self.runner._clients["fixture"].disarm.assert_called_once_with("history_buffer_failure")
        self.runner.ema_checkpoint.warm_closed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
