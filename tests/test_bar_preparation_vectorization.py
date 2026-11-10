"""Vectorized preparation must retain timestamps, diagnostics and price precision."""
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from bomber.framework.dataprep import BarReadSpec, InputError, InputSession
from bomber.framework.dataprep.bars import read_bar_frame


class BarPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "MO2609-C-6600_20260813.feather"
        self.path.touch()

    def frame(self, count=3):
        return pd.DataFrame(dict(datetime=pd.date_range("2026-08-13 10:00", periods=count, freq="min"),
            open=100., high=102., low=99., close=101., volume=2))

    def read(self, frame, **spec):
        with patch("pandas.read_feather", return_value=frame.copy(deep=True)):
            return read_bar_frame(self.path, BarReadSpec(**spec))

    def test_seconds_milliseconds_microseconds_nanoseconds_and_timezone_preserve_both_clocks(self):
        for unit in ("s", "ms", "us", "ns"):
            for zone in (None, "UTC", "Asia/Shanghai"):
                with self.subTest(unit=unit, zone=zone):
                    frame = self.frame()
                    times = pd.Series(pd.date_range("2026-08-13 10:00:00.123456789", periods=3,
                        freq="min", tz="Asia/Shanghai"))
                    if zone is None:
                        times = times.dt.tz_localize(None)
                    else:
                        times = times.dt.tz_convert(zone)
                    frame["datetime"] = times.dt.as_unit(unit)
                    frame["published"] = frame.datetime + pd.Timedelta(seconds=2)
                    result = self.read(frame, available_column="published", require_minute_alignment=False)
                    localized = (frame.datetime.dt.tz_localize("Asia/Shanghai") if zone is None else frame.datetime)
                    self.assertEqual(result.frame.event_ns.tolist(), [stamp.value for stamp in localized])
                    self.assertEqual(result.frame.available_ns.tolist(),
                        [stamp.value + 2_000_000_000 for stamp in localized])
                    self.assertEqual(str(result.frame.datetime.dt.tz), "Asia/Shanghai")

    def test_standard_sorted_numeric_file_needs_no_row_reduction_or_sort(self):
        frame = self.frame()
        with patch.object(pd.DataFrame, "max", side_effect=AssertionError("row-wise max")), \
                patch.object(pd.DataFrame, "min", side_effect=AssertionError("row-wise min")), \
                patch.object(pd.DataFrame, "sort_values", side_effect=AssertionError("redundant sort")):
            result = self.read(frame)
        pd.testing.assert_series_equal(result.frame.close, frame.close)

    def test_unsigned_integer_prices_and_quantities_above_float_precision_remain_exact(self):
        frame = self.frame()
        base = 2**63 + 123
        for key, offset in (("open", 1), ("high", 3), ("low", 0), ("close", 2)):
            frame[key] = pd.Series([base + offset] * 3, dtype="uint64")
        frame["volume"] = pd.Series([2**54 + 1, 2**54 + 3, 2**54 + 5], dtype="uint64")
        result = self.read(frame)
        for key in ("open", "high", "low", "close", "volume"):
            pd.testing.assert_series_equal(result.frame[key], frame[key])

    def test_mixed_decimal_and_integer_prices_keep_pandas_promotion(self):
        frame = self.frame()
        frame["open"] = [Decimal("100.123"), Decimal("100.456"), Decimal("100.789")]
        frame["high"] = pd.Series([102, 102, 102], dtype="int64")
        result = self.read(frame)
        self.assertEqual(result.frame.open.tolist(), [100.123, 100.456, 100.789])
        self.assertEqual(result.frame.high.dtype, np.dtype("int64"))

    def test_repair_uses_updated_high_before_computing_low_and_retains_originals(self):
        frame = self.frame(2)
        frame.loc[0, "high"] = 90.  # Original high must not become the repaired low.
        frame.loc[1, "low"] = 110.
        result = self.read(frame, ohlc_policy="expand_range")
        self.assertEqual(result.frame.high.tolist(), [101., 110.])
        self.assertEqual(result.frame.low.tolist(), [99., 100.])
        self.assertEqual(result.frame.source_high.tolist(), [90., 102.])
        self.assertEqual(result.frame.source_low.tolist(), [99., 110.])
        self.assertEqual(result.issues[0].code, "OHLC_RANGE_EXPANDED")
        self.assertIn("Expanded 2", result.issues[0].message)

    def test_strict_ohlc_reports_first_original_row(self):
        frame = self.frame()
        frame.loc[1, "high"] = 90.
        with self.assertRaises(InputError) as failure:
            self.read(frame)
        self.assertEqual((failure.exception.issue.code, failure.exception.issue.row), ("INVALID_OHLC", 2))

    def test_missing_infinite_and_invalid_values_keep_audited_flags_and_field_order(self):
        frame = self.frame()
        frame["close"] = pd.Series([101., None, float("inf")], dtype="Float64")
        frame["volume"] = pd.Series([1, None, -1], dtype="Int64")
        result = self.read(frame, required_fields=("close", "volume"), value_policy="research_audited")
        self.assertEqual([(issue.field, issue.row) for issue in result.issues],
            [("close", 2), ("close", 3), ("volume", 2), ("volume", 3)])
        self.assertEqual(result.frame.input_quality.tolist(), ["", "INVALID_BAR_FIELDS", "INVALID_BAR_FIELDS"])

    def test_zero_drop_and_audit_preserve_original_row_number_and_reset_result_index(self):
        frame = self.frame()
        frame.loc[0, "open"] = 0.
        frame.loc[2, "volume"] = -1
        result = self.read(frame, zero_price_policy="drop", value_policy="research_audited")
        self.assertEqual([issue.code for issue in result.issues], ["ZERO_PRICE_BAR_DROPPED", "INVALID_BAR_FIELDS"])
        self.assertEqual(result.issues[1].row, 3)
        self.assertEqual(result.frame.index.tolist(), [0, 1])
        self.assertEqual(result.frame.input_quality.tolist(), ["", "INVALID_BAR_FIELDS"])

    def test_out_of_order_file_is_sorted_by_availability_then_event_with_row_values_intact(self):
        frame = self.frame()
        frame["volume"] = [1, 2, 3]
        frame = frame.iloc[[2, 0, 1]]
        result = self.read(frame)
        self.assertEqual(result.frame.volume.tolist(), [1, 2, 3])
        self.assertTrue(result.frame.event_ns.is_monotonic_increasing)

    def test_availability_that_reverses_event_order_is_still_rejected(self):
        frame = self.frame(2)
        frame["published"] = pd.to_datetime(["2026-08-13 10:03", "2026-08-13 10:02"])
        with self.assertRaisesRegex(InputError, "UNSUPPORTED_CAPABILITY"):
            self.read(frame, available_column="published")

    def test_named_or_nondefault_index_is_reset_and_alias_identity_checks_still_apply(self):
        frame = self.frame()
        frame.index = pd.RangeIndex(0, 3, name="source_index")
        frame = frame.rename(columns={"close": "last"})
        result = self.read(frame, field_mapping=(("close", "last"),))
        self.assertIsNone(result.frame.index.name)
        self.assertEqual(result.frame.close.tolist(), [101.] * 3)
        self.assertEqual(result.frame.trading_day.tolist(), [date(2026, 8, 13)] * 3)

    def test_cached_frames_keep_independent_public_results(self):
        frame = self.frame()
        with InputSession(), patch("pandas.read_feather", return_value=frame):
            first = read_bar_frame(self.path)
            first.frame.loc[0, "close"] = 999
            second = read_bar_frame(self.path)
        self.assertEqual(second.frame.close.iloc[0], 101.)
        self.assertEqual(frame.columns.tolist(), ["datetime", "open", "high", "low", "close", "volume"])


if __name__ == "__main__":
    unittest.main()
