"""Run-local diagnostic accounting, using deterministic clocks and real Feather I/O."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import pandas as pd

from bomber.framework.performance import PerformanceStats
from bomber.framework.dataprep.session import InputSession, read_feather
from bomber.framework.dataprep.bars import PreparedFrameReader, read_bar_frame


class PerformanceTests(unittest.TestCase):
    def test_nested_durations_and_failures_are_inclusive_and_snapshot_is_detached(self):
        stats = PerformanceStats(clock=iter((1., 2., 3., 5.)).__next__)
        with self.assertRaises(ValueError), stats.measure("outer"):
            with stats.measure("inner"):
                raise ValueError("fixture")
        stats.count("rows", 4)
        report = stats.snapshot()
        self.assertEqual(report["timings"], {
            "inner": {"seconds": 1., "calls": 1}, "outer": {"seconds": 4., "calls": 1}})
        report["timings"]["outer"]["seconds"] = 999
        self.assertEqual(stats.snapshot()["timings"]["outer"]["seconds"], 4)

    def test_input_report_counts_real_reads_separately_from_cache_hits(self):
        with TemporaryDirectory() as directory, InputSession() as session:
            path = Path(directory) / "IM2609_20260813.feather"
            pd.DataFrame(dict(datetime=["2026-08-13 14:00"], open=[100], high=[102],
                low=[99], close=[101], volume=[2])).to_feather(path)
            first = read_bar_frame(path)
            second = read_bar_frame(path)
            read_feather(path)
            pd.testing.assert_frame_equal(first.frame, second.frame)
            session.write_reports(directory)
            report = json.loads((Path(directory) / "performance.json").read_text())
            self.assertEqual(report["counters"], {"bar_cache_hits": 1,
                "bar_files_prepared": 1, "bar_rows_prepared": 1,
                "feather_cache_hits": 1, "feather_files_read": 1, "feather_rows_read": 1})
            self.assertEqual(report["timings"]["bar_read_normalize_validate"]["calls"], 2)
            self.assertEqual(report["timings"]["feather_disk_read"]["calls"], 1)

    def test_borrowing_is_opt_in_and_default_cache_and_reader_stay_isolated(self):
        with TemporaryDirectory() as directory, InputSession():
            path = Path(directory) / "IM2609_20260813.feather"
            pd.DataFrame(dict(datetime=["2026-08-13 14:00"], open=[100], high=[102],
                low=[99], close=[101], volume=[2])).to_feather(path)
            owned = read_bar_frame(path, copy_frame=False)
            borrowed = read_bar_frame(path, copy_frame=False)
            self.assertIs(borrowed.frame, owned.frame)
            independent = read_bar_frame(path)
            independent.frame.loc[0, "close"] = 999
            self.assertEqual(owned.frame.close.iloc[0], 101)
            reader = PreparedFrameReader(owned.frame)
            borrowed_reader = PreparedFrameReader(owned.frame, copy_frame=False)
            self.assertIs(borrowed_reader.frame, owned.frame)
            owned.frame.loc[0, "close"] = 103
            self.assertEqual(next(reader.read(path))[1]["close"], 101)

    def test_row_stream_preserves_nanoseconds_decimals_and_integer_precision(self):
        from decimal import Decimal
        stamp = pd.Timestamp("2026-08-13T14:00:00.123456789+08:00")
        frame = pd.DataFrame({"datetime": [stamp], "available_ns": [stamp.value + 123],
            "open": [Decimal("100.123456789")], "volume": [2**54 + 1], "close": [100.25]})
        expected = frame.to_dict("records")
        with patch.object(pd.DataFrame, "to_dict", side_effect=AssertionError("full row list")):
            rows = list(PreparedFrameReader(frame, copy_frame=False).read(Path("unused")))
        self.assertEqual(rows, [(1, expected[0])])


class AdapterPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use the existing offline native-entity fixture, executing actual Python
        # adapter/feed/parser code. No native engine, database or account involved.
        from tests.test_bar_parsers import load_framework, load_module
        cls.fw = load_framework()
        with patch.dict("sys.modules", {"bomber.framework.market.basic.base": cls.fw.basic,
                "bomber.framework.trader.execution.ports": NS(SimExecutionBackendPort=object)}):
            cls.module = load_module("_performance_market_adapter",
                "bomber/framework/trader/runtime/market_adapter.py")

    def bar(self, instrument, period):
        class BarType:
            instrument_id = instrument
            def __str__(self):
                return f"{instrument}-{period}-LAST-EXTERNAL"
        return self.fw.basic.Bar(bar_type=BarType())

    def test_index_matches_original_filter_for_all_kinds_periods_and_unbound_symbols(self):
        from tests.test_bar_parsers import Identity
        kind = self.fw.basic.DataType
        bindings = [self.module.MarketStreamBinding(Identity(f"IM{i}.CFFEX"), kind.BAR, period)
            for i in range(450) for period in ("1-MINUTE", "5-MINUTE")]
        target = Identity("IM449.CFFEX")
        bindings += [self.module.MarketStreamBinding(target, data_type)
            for data_type in (kind.TRADE_TICK, kind.QUOTE_TICK)]
        adapter = self.module.NautilusMarketFeedAdapter("test", NS(), NS(), bindings)
        def original(event):
            if isinstance(event, self.fw.basic.Bar):
                return any(b.data_type is kind.BAR and b.instrument_id == event.bar_type.instrument_id
                    and f"-{b.bar_spec}-" in str(event.bar_type).upper() for b in bindings)
            data_type = kind.TRADE_TICK if isinstance(event, self.fw.basic.TradeTick) else kind.QUOTE_TICK
            return any(b.data_type is data_type and b.instrument_id == event.instrument_id
                and b.bar_spec is None for b in bindings)
        events = [self.bar(Identity(f"IM{i}.CFFEX"), period)
            for i in (0, 200, 449, 999) for period in ("1-MINUTE", "5-MINUTE", "15-MINUTE")]
        events += [entity(instrument_id=Identity(f"IM{i}.CFFEX"))
            for i in (0, 449, 999) for entity in (self.fw.basic.QuoteTick, self.fw.basic.TradeTick)]
        expected = [original(event) for event in events]
        class NoScan(tuple):
            def __iter__(self):
                raise AssertionError("lookup scanned the universe")
        adapter.bindings = NoScan(bindings)
        self.assertEqual([adapter._matches_binding(event) for event in events], expected)
        self.assertFalse(adapter._matches_binding(object()))

    def test_forwarding_sequence_and_exception_timing_do_not_change_with_diagnostics(self):
        from tests.test_bar_parsers import Identity
        kind, identity = self.fw.basic.DataType, Identity("IM2609.CFFEX")
        event = self.bar(identity, "5-MINUTE")
        trace, stats = [], PerformanceStats()
        def process(value):
            trace.append(value)
            raise ValueError("engine fixture")
        adapter = self.module.NautilusMarketFeedAdapter("test", NS(), NS(process_market_event=process),
            (self.module.MarketStreamBinding(identity, kind.BAR, "5-MINUTE"),), performance=stats)
        adapter._started = True
        with self.assertRaisesRegex(ValueError, "engine fixture"):
            adapter._on_market_event(event)
        self.assertEqual(trace, [event])
        self.assertEqual(stats.timings["simulation_engine_including_decisions"]["calls"], 1)
        self.assertEqual(adapter.events_processed, 0)

    def test_prepared_row_stream_and_old_rows_produce_identical_sorted_bar_events(self):
        from decimal import Decimal
        from tests.test_bar_parsers import Identity
        kind, identity = self.fw.basic.DataType, Identity("IM2609.CFFEX")
        stamp = pd.Timestamp("2026-08-13T14:00:00+08:00").value
        frame = pd.DataFrame(dict(datetime=pd.to_datetime([stamp, stamp + 60_000_000_000], utc=True),
            available_ns=[stamp+123, stamp+60_000_000_456], open=["100.123", "101.123"],
            high=["102.987", "103.987"], low=["99.876", "100.876"],
            close=["100.567", "101.567"], volume=[20, 21]))
        class OldReader:
            suffixes = frozenset({".feather"})
            def read(self, path):
                return enumerate(frame.to_dict("records"), 1)
        def load(reader, path):
            feed = self.fw.feed.FileReplayFeed()
            feed.performance = PerformanceStats()
            feed.register_instrument(self.fw.basic.InstrumentMeta(identity, price_precision=2, size_precision=0))
            parser = self.fw.fixed.FixedInstrumentBarParser(identity,
                bar_spec="5-MINUTE", available_nanoseconds="available_ns")
            feed.add_source(path, reader, parser, "bar")
            feed.subscribe(identity, kind.BAR, bar_spec="5-MINUTE")
            feed.connect()
            events = feed.load_events()
            self.assertIs(feed.load_events(), events)  # cache must not inflate counts
            stats = feed.performance.snapshot()
            self.assertEqual(stats["counters"], {"replay_source_files": 1,
                "replay_source_rows": 2, "replay_events": 2})
            self.assertEqual(stats["timings"]["replay_event_sort"]["calls"], 1)
            return [(str(e.bar_type), e.ts_event, e.ts_init,
                tuple(getattr(e, k) for k in ("open", "high", "low", "close", "volume"))) for e in events]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.feather"
            path.touch()
            self.assertEqual(load(OldReader(), path),
                load(PreparedFrameReader(frame, copy_frame=False), path))


if __name__ == "__main__":
    unittest.main()
