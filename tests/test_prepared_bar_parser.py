"""Prepared parsing vs the actual generic factory/feed, with SDK entity fixtures."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import pandas as pd

from bomber.framework.dataprep import BarReadSpec, InputSession
from bomber.framework.dataprep.bars import add_prepared_bar_source, read_bar_frame
from tests.test_bar_parsers import Identity, load_framework


class EntityBarType:
    """Like the SDK BarType, an object rather than the old fixture's str subtype."""
    calls = 0
    def __init__(self, text):
        self.text = text
        self.instrument_id = Identity(text.rsplit("-", 4)[0])
    @classmethod
    def from_str(cls, text):
        cls.calls += 1
        return cls(text)
    def __str__(self):
        return self.text


def fingerprint(event):
    bar = event.payload
    return (event.data_type.name, str(event.instrument_id), str(bar.bar_type), bar.ts_event, bar.ts_init,
        tuple(str(getattr(bar, key)) for key in ("open", "high", "low", "close", "volume")))


class PreparedBarParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fw = load_framework()

    def setUp(self):
        for target in (self.fw.basic, self.fw.fixed):
            mock = patch.object(target, "BarType", EntityBarType)
            mock.start()
            self.addCleanup(mock.stop)
        EntityBarType.calls = 0
        self.instrument = Identity("MO2609-C-6600.CFFEX")
        self.meta = self.fw.basic.InstrumentMeta(self.instrument, price_precision=2, size_precision=0)
        self.context = self.fw.context.ParserContext(Path("/bars/fixture.feather"), 7,
            lambda identity: self.meta if identity == self.instrument else None)
        self.stamp = pd.Timestamp("2026-08-13T14:00:00+08:00")

    def row(self, **changes):
        return dict(datetime=self.stamp, event_ns=self.stamp.value, available_ns=self.stamp.value+123,
            open="100.125", high="102.987", low="99.876", close="101.005", volume="20.5", **changes)

    def generic(self, period="5-MINUTE"):
        return self.fw.fixed.FixedInstrumentBarParser(self.instrument, bar_spec=period,
            available_nanoseconds="available_ns")

    def prepared(self, period="5-MINUTE"):
        return self.fw.fixed.PreparedBarParser(self.instrument, bar_spec=period)

    def test_prices_quantity_rounding_and_both_clocks_equal_generic(self):
        for period in ("1-MINUTE", "5-MINUTE"):
            for precision in (0, 1, 2, 4):
                for size_precision in (0, 1, 2):
                    with self.subTest(period=period, price=precision, size=size_precision):
                        self.meta.price_precision, self.meta.size_precision = precision, size_precision
                        row = self.row()
                        before = dict(row)
                        legacy = [fingerprint(e) for e in self.generic(period).parse(row, self.context)]
                        prepared = [fingerprint(e) for e in self.prepared(period).parse(row, self.context)]
                        self.assertEqual(legacy, prepared)
                        self.assertEqual(row, before)

    def test_metadata_and_bar_type_bind_once_and_reset_for_next_load(self):
        lookups = []
        context = self.fw.context.ParserContext(self.context.path, 1,
            lambda key: lookups.append(key) or self.meta)
        parser = self.prepared()
        for _ in range(100):
            list(parser.parse(self.row(), context))
        self.assertEqual(lookups, [self.instrument])
        self.assertEqual(EntityBarType.calls, 1)
        parser.reset()
        self.meta = self.fw.basic.InstrumentMeta(self.instrument, price_precision=4, size_precision=1)
        event = next(iter(parser.parse(self.row(), context)))
        self.assertEqual((len(lookups), EntityBarType.calls), (2, 2))
        self.assertEqual(str(event.payload.close), "101.0050")
        self.assertEqual(str(event.payload.volume), "20.5")

    def test_no_iso_parse_identity_enrichment_or_per_row_metadata_lookup(self):
        parser = self.prepared()
        row = {key: value for key, value in self.row().items() if key != "datetime"}
        with patch.object(self.fw.mapped, "parse_iso_timestamp", side_effect=AssertionError("ISO parse")), \
                patch.object(self.fw.fixed.InstrumentId, "from_str", side_effect=AssertionError("identity parse")):
            first = next(iter(parser.parse(row, self.context)))
            second = next(iter(parser.parse(row, self.context)))
        self.assertEqual(fingerprint(first), fingerprint(second))
        self.assertEqual(EntityBarType.calls, 1)

    def test_malformed_values_keep_generic_file_line_errors(self):
        for changes in ({"open": 0}, {"high": 10}, {"low": 150}, {"close": float("nan")},
                {"volume": -1}, {"volume": "bad"}, {"available_ns": "1.2"},
                {"available_ns": None}, {"available_ns": self.stamp.value-1}, {"available_ns": True}):
            row = self.row()
            row.update(changes)
            errors = []
            for parser in (self.generic(), self.prepared()):
                with self.assertRaises(self.fw.context.DataLoadError) as failure:
                    list(parser.parse(row, self.context))
                errors.append(str(failure.exception))
            self.assertEqual(*errors)
            self.assertIn("fixture.feather:7:", errors[0])

    def test_unknown_instrument_and_bad_event_clock_fail_with_context(self):
        missing = self.fw.context.ParserContext(self.context.path, 9, lambda key: None)
        errors = []
        for parser in (self.generic(), self.prepared()):
            with self.assertRaises(self.fw.context.DataLoadError) as failure:
                list(parser.parse(self.row(), missing))
            errors.append(str(failure.exception))
        self.assertEqual(*errors)
        for clock in (None, True, 1.5, "NaN"):
            row = self.row(); row["event_ns"] = clock
            with self.assertRaisesRegex(self.fw.context.DataLoadError, "fixture.feather:7:.*integer nanoseconds"):
                list(self.prepared().parse(row, self.context))

    def test_integer_strings_and_decimal_clocks_keep_nanoseconds(self):
        row = self.row()
        row["event_ns"], row["available_ns"] = str(self.stamp.value), Decimal(self.stamp.value+123)
        self.assertEqual([fingerprint(e) for e in self.generic().parse(row, self.context)],
            [fingerprint(e) for e in self.prepared().parse(row, self.context)])

    def source(self, root, period, submicro=False):
        path = root/f"{self.instrument.symbol.value}_20260813.feather"
        frame = pd.DataFrame(dict(datetime=pd.date_range(self.stamp, periods=3, freq=f"{period}min"),
            open=100.125, high=102.987, low=99.876, close=101.005, volume=2))
        if submicro:
            frame["datetime"] += pd.Timedelta(nanoseconds=123)
        frame["published"] = frame.datetime+pd.Timedelta(nanoseconds=999)
        frame.to_feather(path)
        return read_bar_frame(path, BarReadSpec(interval_seconds=period*60, available_column="published",
            require_minute_alignment=not submicro))

    def load_source(self, result, fast):
        with patch.dict("sys.modules", {"bomber.framework.market.replay.parsers.bar": NS(
                FixedInstrumentBarParser=self.fw.fixed.FixedInstrumentBarParser),
                "bomber.framework.market.replay.parsers.bar.fixed": self.fw.fixed}):
            feed = self.fw.feed.FileReplayFeed()
            feed.register_instrument(self.meta)
            feed.subscribe(self.instrument, self.fw.basic.DataType.BAR, bar_spec=result.spec.bar_spec)
            add_prepared_bar_source(feed, result, self.instrument, fast_parse=fast)
            feed.connect()
            return feed

    def test_prepared_frame_projection_and_submicro_fallback_keep_events_identical(self):
        for period in (1, 5):
            for submicro in (False, True):
                with self.subTest(period=period, submicro=submicro), TemporaryDirectory() as directory:
                    result = self.source(Path(directory), period, submicro)
                    old = self.load_source(result, False)
                    with InputSession() as session:
                        new = self.load_source(result, True)
                    left, right = old.load_events(), new.load_events()
                    signature = lambda e: (str(e.bar_type), e.ts_event, e.ts_init,
                        *(str(getattr(e, key)) for key in ("open", "high", "low", "close", "volume")))
                    self.assertEqual(list(map(signature, left)), list(map(signature, right)))
                    self.assertEqual(len(right), 3)
                    counter = "prepared_parser_fallback_rows" if submicro else "prepared_parser_rows"
                    self.assertEqual(session.performance.counters[counter], 3)
                    reader = new._sources[0].reader
                    row = next(iter(reader.read(result.path)))[1]
                    self.assertEqual("datetime" in row, submicro)
                    self.assertNotIn("source_timestamp", row if not submicro else {})

    def test_force_reload_rebinds_replaced_metadata(self):
        with TemporaryDirectory() as directory:
            result = self.source(Path(directory), 5)
            feed = self.load_source(result, True)
            first = feed.load_events()
            self.assertEqual(str(first[0].close), "101.00")
            feed.register_instrument(self.fw.basic.InstrumentMeta(self.instrument, price_precision=4, size_precision=1))
            second = feed.load_events(force_reload=True)
            self.assertEqual(str(second[0].close), "101.0050")
            self.assertEqual(str(second[0].volume), "2.0")

    def test_start_labeled_m5_frame_is_shifted_once_before_either_parser(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/f"{self.instrument.symbol.value}_20260813.feather"
            pd.DataFrame(dict(datetime=[self.stamp], published=[self.stamp+pd.Timedelta(minutes=5, nanoseconds=999)],
                open=[100.125], high=[102.987], low=[99.876], close=[101.005], volume=[2])).to_feather(path)
            result = read_bar_frame(path, BarReadSpec(interval_seconds=300, timestamp_label="start",
                available_column="published"))
            before, after = self.load_source(result, False).load_events(), self.load_source(result, True).load_events()
            self.assertEqual([(e.ts_event, e.ts_init, str(e.close)) for e in before],
                [(e.ts_event, e.ts_init, str(e.close)) for e in after])
            self.assertEqual(after[0].ts_event, self.stamp.value+300_000_000_000)


if __name__ == "__main__":
    unittest.main()
