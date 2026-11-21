"""Public replay/adapter/backend batch contracts with native boundaries stubbed."""
from importlib.util import module_from_spec, spec_from_file_location
from itertools import groupby
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from bomber.framework.performance import PerformanceStats
from tests.test_bar_parsers import Identity, load_framework
from tests.test_simulation_market_timing import Bar, EngineHost, components


class BatchEngine(EngineHost):
    def __init__(self):
        super().__init__()
        self.callback = None
        self.run_calls = self.clear_calls = 0
    def set_data_callback(self, callback=None):
        self.callback = callback
    def run(self, streaming):
        self.run_calls += 1
        events = self.events
        for event in events:
            self.events = [event]
            super().run(streaming)
            if self.callback is not None:
                self.callback(event)
        self.events = events
    def clear_data(self):
        self.clear_calls += 1
        super().clear_data()
    def dispose(self):
        self.disposed = True


class BatchBackendTests(unittest.TestCase):
    def test_reports_created_by_publication_are_not_charged_to_next_event(self):
        for batched in (False, True):
            with components() as sdk:
                engine = BatchEngine()
                backend = sdk.backend.NautilusSimExecutionBackend("sim", engine=engine)
                received = []
                def publish(event, reports):
                    received.append(len(reports))
                    backend._reports.append(object())  # synchronous report during a strategy callback
                bars = [Bar("A", 1), Bar("B", 1)]
                if batched:
                    backend.process_market_batch(bars, publish)
                else:
                    for event in bars:
                        publish(event, backend.process_market_event(event))
                self.assertEqual(received, [0, 0])

    def test_same_time_signal_waits_for_later_own_bar_in_both_paths(self):
        for delay in (1, 2):
            observations = []
            for batched in (False, True):
                with components() as sdk:
                    engine = BatchEngine()
                    backend = sdk.backend.NautilusSimExecutionBackend("sim", engine=engine,
                        execution_timing="next_market", execution_delay_bars=delay)
                    bars = [Bar(key, stamp) for key, stamp in (
                        ("A", 100), ("B", 100), ("B", 100), ("A", 101),
                        ("B", 101), ("B", 101), ("B", 102))]
                    published = []
                    def publish(event, reports):
                        published.append((event.bar_type.instrument_id, event.ts_event,
                                          len(engine.gateway.submissions)))
                        if len(published) == 1:
                            backend.submit_order(sdk.contracts.OrderIntent(
                                backend_id="sim", strategy_id="strategy", instrument_id="B",
                                side=sdk.contracts.OrderSide.BUY, quantity=1,
                                metadata={"request_ts": 100}))
                    for _, group in groupby(bars, key=lambda row: row.ts_init):
                        group = tuple(group)
                        if batched:
                            backend.process_market_batch(group, publish)
                        else:
                            for event in group:
                                publish(event, backend.process_market_event(event))
                    observations.append((published, [(stamp, order.instrument_id)
                        for stamp, order in engine.gateway.submissions]))
                    self.assertEqual(engine.run_calls, 3 if batched else 7)
                    self.assertIsNone(engine.callback)
            self.assertEqual(*observations)
            self.assertEqual(observations[0][1], [(101 if delay == 1 else 102, "B")])

    def test_publication_failure_stops_without_running_unconsumed_data_on_cleanup(self):
        with components() as sdk:
            engine = BatchEngine()
            backend = sdk.backend.NautilusSimExecutionBackend("sim", engine=engine)
            published = []
            def fail(event, reports):
                published.append(event)
                raise ValueError("publication fixture")
            with self.assertRaisesRegex(ValueError, "publication fixture"):
                backend.process_market_batch([Bar("A", 1), Bar("B", 1)], fail)
            backend.stop()
            self.assertEqual(len(published), 1)
            self.assertEqual(engine.run_calls, 1)
            self.assertEqual(engine.clear_calls, 0)
            self.assertIsNone(engine.callback)
            self.assertTrue(engine.disposed)
            with self.assertRaisesRegex(RuntimeError, "failed batch"):
                backend.process_market_event(Bar("B", 2))

    def test_batch_rejects_mixed_receipt_times_and_old_kernel(self):
        with components() as sdk:
            engine = BatchEngine()
            backend = sdk.backend.NautilusSimExecutionBackend("sim", engine=engine)
            with self.assertRaisesRegex(ValueError, "one nondecreasing"):
                backend.process_market_batch([Bar("A", 1), Bar("B", 2)], lambda *args: None)
            self.assertEqual(engine.run_calls, 0)
            old = sdk.backend.NautilusSimExecutionBackend("old", engine=EngineHost())
            with self.assertRaisesRegex(RuntimeError, "重新编译"):
                old.require_batch_support()

    def test_callback_cannot_reenter_backend_finalization(self):
        with components() as sdk:
            engine = BatchEngine()
            backend = sdk.backend.NautilusSimExecutionBackend("sim", engine=engine)
            with self.assertRaisesRegex(RuntimeError, "active batch callback"):
                backend.process_market_batch([Bar("A", 1), Bar("B", 1)],
                                             lambda *args: backend.finish())
            backend.stop()
            self.assertEqual(engine.run_calls, 1)
            self.assertTrue(engine.disposed)


class FeedBatchTests(unittest.TestCase):
    def setUp(self):
        self.fw = load_framework()
        path = Path(__file__).resolve().parents[1]/"bomber/framework/trader/runtime/market_adapter.py"
        spec = spec_from_file_location("_batch_market_adapter", path)
        self.adapter = module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: self.adapter,
            "bomber.framework.market.basic.base": self.fw.basic,
            "bomber.framework.trader.execution.ports": NS(SimExecutionBackendPort=object)}):
            spec.loader.exec_module(self.adapter)

    def bar(self, key, event, received):
        class BarType:
            instrument_id = Identity(key)
            def __str__(self):
                return key+"-1-MINUTE-LAST-EXTERNAL"
        return self.fw.basic.Bar(bar_type=BarType(), ts_event=event, ts_init=received)

    def test_matching_and_publication_stay_interleaved_without_future_prices(self):
        histories = []
        for batched in (False, True):
            feed = self.fw.feed.FileReplayFeed()
            events = (self.bar("A", 1, 2), self.bar("B", 1, 2), self.bar("A", 2, 3),
                      self.bar("C", 3, 4), self.bar("A", 3, 4))
            feed.load_events = lambda: events
            latest, trace = {}, []
            class Backend:
                def require_batch_support(self): pass
                def process_market_event(self, event):
                    trace.append(("match", event.bar_type.instrument_id, event.ts_init))
                    return ()
                def process_market_batch(self, rows, publish):
                    for event in rows:
                        publish(event, self.process_market_event(event))
            kind = self.fw.basic.DataType.BAR
            bindings = tuple(self.adapter.MarketStreamBinding(Identity(key), kind, "1-MINUTE")
                             for key in ("A", "B"))
            adapter = self.adapter.NautilusMarketFeedAdapter("test", feed, Backend(), bindings,
                                                           performance=PerformanceStats())
            if batched:
                adapter.enable_same_timestamp_batches()
            adapter._started = True
            adapter._attach_handlers()
            def observe(event):
                latest[str(event.bar_type.instrument_id)] = event.ts_event
                trace.append(("publish", dict(latest)))
            feed.register_bar_handler(observe)
            summary = feed.replay()
            self.assertEqual(summary.bars, 5)
            self.assertEqual(adapter.events_processed, 4)
            histories.append(trace)
        self.assertEqual(*histories)
        self.assertEqual(histories[0][1], ("publish", {"A": 1}))

    def test_grouping_uses_receipt_clock_not_event_clock_and_keeps_step_mode(self):
        feed = self.fw.feed.FileReplayFeed()
        rows = (self.bar("A", 1, 2), self.bar("B", 0, 2), self.bar("A", 1, 3))
        feed.load_events = lambda: rows
        groups, seen = [], []
        def batch(events, emit):
            groups.append(events)
            for event in events: emit(event)
        feed.set_replay_batch_handler(batch)
        feed.register_bar_handler(seen.append)
        self.assertEqual(feed.replay().bars, 3)
        self.assertEqual([len(group) for group in groups], [2, 1])
        self.assertEqual(seen, list(rows))
        groups.clear()
        self.assertEqual(list(feed.replay_step()), list(rows))
        self.assertEqual(groups, [])


if __name__ == "__main__":
    unittest.main()
