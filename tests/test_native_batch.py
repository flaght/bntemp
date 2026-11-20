"""Arrow loading and real Python replay path with only SDK entities substituted."""
from dataclasses import replace
from decimal import Decimal
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from bomber.framework.dataprep.columnar import ArrowBarFrame
from bomber.framework.dataprep.prepared import PreparedDataset
from bomber.framework.performance import PerformanceStats
from tests import test_prepared_dataset as dataset_fixture
from tests.test_bar_parsers import load_framework, load_module, Scalar, Identity
from tests.test_prepared_bar_parser import EntityBarType


class NativeBatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dataset_fixture.PreparedDatasetTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.build()
        self.fw = load_framework()
        with patch.dict("sys.modules", {"bomber.framework.market.basic.base": self.fw.basic,
                "bomber.framework.market.replay.parsers.base": self.fw.context,
                "bomber.framework.market.replay.parsers.bar.fixed": self.fw.fixed}):
            self.batch = load_module("bomber.framework.market.replay.native_batch",
                                     "bomber/framework/market/replay/native_batch.py")
        for module in (self.fw.basic, self.fw.fixed, self.batch):
            p = patch.object(module, "BarType", EntityBarType)
            p.start()
            self.addCleanup(p.stop)
        self.calls = []

        def native_double_constructor(cls, bar_type, price_precision, size_precision,
                                      opens, highs, lows, closes, volumes, events, available):
            self.calls.append(len(events))
            # Emulate the native float constructor's positive half-away rounding,
            # deliberately different from Python formatting on half-way inputs.
            def scalar(value, precision):
                units = int(np.floor(value * 10**precision + .5))
                return Scalar(format(Decimal(units).scaleb(-precision), f".{precision}f"))
            return [cls(bar_type=bar_type, ts_event=int(events[i]), ts_init=int(available[i]),
                **{name: scalar(values[i], size_precision if name == "volume" else price_precision)
                   for name, values in zip(("open", "high", "low", "close", "volume"),
                                          (opens, highs, lows, closes, volumes))}) for i in range(len(events))]
        p = patch.object(self.batch.Bar, "from_raw_arrays_to_list", classmethod(native_double_constructor), create=True)
        p.start()
        self.addCleanup(p.stop)

    def bundle(self, form="arrow"):
        return PreparedDataset(self.fixture.output).load(self.fixture.plan(), specs=self.fixture.specs,
            products=self.fixture.products, copy_frames=False, frame_format=form)

    @staticmethod
    def signature(events):
        return [(str(e.bar_type), e.ts_event, e.ts_init,
                 *(str(getattr(e, n)) for n in ("open", "high", "low", "close", "volume"))) for e in events]

    def feed(self, bundle, batch, *, price=2, size=0):
        from bomber.framework.dataprep.bars import add_prepared_bar_source
        from types import SimpleNamespace as NS
        feed = self.fw.feed.FileReplayFeed()
        feed.performance = PerformanceStats()
        with patch.dict("sys.modules", {
                "bomber.framework.market.replay.parsers.bar": NS(FixedInstrumentBarParser=self.fw.fixed.FixedInstrumentBarParser),
                "bomber.framework.market.replay.parsers.bar.fixed": self.fw.fixed}):
            for source in sorted(bundle.sources, key=lambda s: (s.result.key.trading_day, s.result.key.symbol)):
                result = source.result
                identity = Identity(result.key.symbol+".CFFEX")
                feed.register_instrument(self.fw.basic.InstrumentMeta(identity, price_precision=price, size_precision=size))
                feed.subscribe(identity, self.fw.basic.DataType.BAR, bar_spec=result.spec.bar_spec)
                if batch:
                    self.batch.add_arrow_bar_source(feed, result, identity)
                else:
                    add_prepared_bar_source(feed, result, identity, copy_frame=False, fast_parse=True)
        feed.connect()
        return feed

    def test_arrow_sources_exact_round_trip_and_entry_clock_without_dataframe_restore(self):
        from bomber.framework.dataprep.session import InputSession
        with InputSession() as session:
            arrow = self.bundle()
        pandas = self.bundle("pandas")
        self.assertNotIn("prepared_frame_restore", session.performance.timings)
        self.assertEqual(session.performance.counters["prepared_arrow_sources"], 12)
        for a, p in zip(arrow.sources, pandas.sources):
            pd.testing.assert_frame_equal(a.result.frame.to_pandas(), p.result.frame, check_exact=True)
            self.assertEqual((a.result.first_ns, a.result.last_ns), (p.result.first_ns, p.result.last_ns))
            with patch.object(ArrowBarFrame, "to_pandas", side_effect=AssertionError("whole frame restored")):
                pd.testing.assert_series_equal(a.result.frame.datetime.rename("datetime"), p.result.frame.datetime)

    def test_batch_sequence_rounding_and_clocks_equal_existing_feed(self):
        # Ordinary fixture volumes are above 2**54: their explicit fallback must
        # preserve legacy float conversion, not silently use unsafe native input.
        old, new = self.feed(self.bundle("pandas"), False), self.feed(self.bundle(), True)
        self.assertEqual(self.signature(old.load_events()), self.signature(new.load_events()))
        self.assertEqual(new.performance.counters["native_batch_fallback_rows"], 36)

    def test_regular_batch_skips_pandas_and_row_parser_and_keeps_source_order(self):
        arrow, pandas = self.bundle(), self.bundle("pandas")
        import pyarrow as pa
        for source in arrow.sources:
            frame = source.result.frame
            index = frame.table.column_names.index("volume")
            frame.table = frame.table.set_column(index, "volume", pa.array([2]*len(frame)))
        for source in pandas.sources:
            source.result.frame["volume"] = 2
        old, new = self.feed(pandas, False), self.feed(arrow, True)
        expected = self.signature(old.load_events())
        with patch.object(ArrowBarFrame, "to_pandas", side_effect=AssertionError("whole frame restored")), \
                patch.object(self.fw.fixed.PreparedBarParser, "parse", side_effect=AssertionError("row parser")):
            self.assertEqual(expected, self.signature(new.load_events()))
        self.assertEqual(new.performance.counters["native_batch_rows"], 36)
        self.assertEqual(self.calls, [3]*12)
        self.assertEqual(new.performance.counters["replay_source_rows"], 36)
        self.assertIs(new.load_events(), new.load_events())
        for identity, meta in list(new._instruments.items()):
            new.register_instrument(replace(meta, price_precision=4))
        self.assertEqual(len(new.load_events(force_reload=True)), 36)

    def test_legacy_rounding_precedes_native_half_away_rounding(self):
        for precision in (0, 1, 2, 4, 8):
            values = np.array([.125, 1.005, 2.675, 100.125, 101.005, 101.125, 101.55555555, 20.5])
            rounded = self.batch.legacy_rounded_array(values, precision)
            for original, new in zip(values, rounded):
                units = int(np.floor(new*10**precision+.5))
                self.assertEqual(Decimal(units).scaleb(-precision), Decimal(f"{original:.{precision}f}"))
        self.assertIsNone(self.batch.legacy_rounded_array(np.array([2**54]), 0))
        self.assertIsNone(self.batch.legacy_rounded_array(np.array([1.]), 9))

    def test_submicrosecond_event_clock_keeps_legacy_generic_fallback(self):
        import pyarrow as pa
        pandas = self.bundle("pandas")
        for source in pandas.sources:
            frame = source.result.frame
            frame["datetime"] += pd.Timedelta(nanoseconds=123)
            frame["event_ns"] += 123
            frame["available_ns"] += 123
            frame["volume"] = 2
        arrow_sources = tuple(replace(source, result=replace(source.result,
            frame=ArrowBarFrame(pa.Table.from_pandas(source.result.frame, preserve_index=False))))
            for source in pandas.sources)
        arrow = replace(pandas, sources=arrow_sources)
        old, new = self.feed(pandas, False), self.feed(arrow, True)
        self.assertEqual(self.signature(old.load_events()), self.signature(new.load_events()))
        self.assertEqual(new.performance.counters["native_batch_fallback_submicro_clock"], 36)
        self.assertEqual(self.calls, [])

    def test_empty_sources_do_not_fabricate_bars_and_subscriptions_filter_batches(self):
        bundle = self.bundle()
        for source in bundle.sources:
            source.result.frame.table = source.result.frame.table.slice(0, 0)
        feed = self.feed(bundle, True)
        self.assertEqual(feed.load_events(), ())
        self.assertEqual(feed.performance.counters["native_batch_rows"], 0)
        # Loading an unsubscribed source must not leak its events into replay.
        feed = self.feed(self.bundle(), True)
        feed._subscriptions.clear()
        self.assertEqual(feed.load_events(), ())

    def test_invalid_numeric_and_clocks_are_rejected_with_source_context(self):
        import pyarrow as pa
        for field, values in (("high", [1., 104., 104.]), ("close", [float("nan"), 102., 103.]),
                              ("volume", [-1., 2., 2.]), ("available_ns", [0, 0, 0])):
            bundle = self.bundle()
            result = bundle.sources[0].result
            table = result.frame.table
            result.frame.table = table.set_column(table.column_names.index(field), field, pa.array(values))
            feed = self.feed(bundle, True)
            with self.subTest(field=field), self.assertRaisesRegex(self.fw.context.DataLoadError, "feather:1:"):
                feed.load_events()

    def test_missing_native_api_is_an_explicit_failure(self):
        with patch.object(self.batch.Bar, "from_raw_arrays_to_list", None):
            with self.assertRaisesRegex(RuntimeError, "from_raw_arrays_to_list"):
                self.batch.require_native_batch()

    def test_backtest_cli_accepts_opt_in_batch_and_retains_risk_override(self):
        from importlib import import_module
        entry = import_module("demos.08_option_vega.run_backtest")
        with patch.object(entry, "run_case") as run, patch("sys.argv", ["run_backtest.py",
                "--start-day", "2026-01-01", "--end-day", "2026-10-21", "--index-feature-file", "index.csv",
                "--data-mode", "prepared", "--prepared-root", str(self.fixture.output),
                "--bar-parser", "native_batch", "--max-notional", "1000000000"]):
            entry.main()
        args = run.call_args.args[0]
        self.assertEqual(args.bar_parser, "native_batch")
        self.assertEqual(args.max_notional, Decimal("1000000000"))


if __name__ == "__main__":
    unittest.main()
