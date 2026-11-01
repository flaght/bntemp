"""真实 Parquet/Feather 往返，验证拆分、时间口径及不丢失源数据。"""

from datetime import date
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from scripts.convert_option_parquet import OUTPUT_COLUMNS, convert, main, prepare_bars
from bomber.framework.dataprep.bars import read_bar_frame
from bomber.framework.dataprep.catalog import scan_bar_files
from bomber.framework.dataprep.contracts import BarFileKey, BarReadSpec


class OptionParquetConversionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "MO.parquet"
        self.output = self.root / "opt"

    def source_frame(self):
        return pd.DataFrame({
            "trade_date": pd.to_datetime(["2026-08-20"] * 3 + ["2026-08-21"]),
            "act_time": pd.to_datetime(["2026-08-20 09:40", "2026-08-20 09:35",
                                        "2026-08-20 09:35", "2026-08-21 15:00"]),
            "code": ["MO2610-C-4100", "MO2610-C-4100", "MO2610-P-4100", "MO2610-C-4100"],
            "open": [12.4, 11.2, 9.0, 0.0], "high": [13.2, 12.0, 9.2, 0.0],
            "low": [12.0, 10.6, 8.8, 0.0], "close": [12.8, 11.6, 9.0, 0.0],
            "volume": [1, 0, 2, 0], "open_interest": [34, 33, 0, 0],
            "timestamp": pd.to_datetime(["2026-08-20 09:40", "2026-08-20 09:35",
                                         "2026-08-20 09:35", "2026-08-21 15:00"]).tz_localize("Asia/Shanghai"),
        })

    def write_source(self, frame=None):
        (self.source_frame() if frame is None else frame).to_parquet(self.source, index=False)

    def test_real_files_preserve_rows_and_values_and_match_current_inventory(self):
        self.write_source()
        summary = convert(self.source, self.output)
        self.assertEqual((summary["files"], summary["output_rows"], summary["zero_volume_rows"]), (3, 4, 2))
        paths = sorted(self.output.rglob("*.feather"))
        self.assertEqual(len(scan_bar_files(self.output, "option")), 3)
        self.assertTrue((self.output / "20260820/MO2610-C-4100_20260820.feather").is_file())
        actual = pd.concat([pd.read_feather(path) for path in paths], ignore_index=True)
        actual = actual.sort_values(["trade_date", "symbol", "datetime"]).reset_index(drop=True)
        pd.testing.assert_frame_equal(actual, prepare_bars(self.source_frame()))
        self.assertEqual(list(actual), list(OUTPUT_COLUMNS))
        self.assertEqual(str(actual.datetime.dtype), "datetime64[ns, Asia/Shanghai]")
        self.assertEqual(str(actual.volume.dtype), "int64")
        # 使用现有统一读取器读取 M5；此处只验证输入，不调用尚限制 M1 的注册 helper。
        path = self.output / "20260820/MO2610-C-4100_20260820.feather"
        result = read_bar_frame(path, BarReadSpec(interval_seconds=300),
                               BarFileKey("option", "MO2610-C-4100", date(2026, 8, 20)))
        self.assertEqual(len(result.frame), 2)
        self.assertEqual(result.frame.close.tolist(), [11.6, 12.8])

    def test_utc_and_naive_times_preserve_shanghai_wall_clock_without_mutating_source(self):
        frame = self.source_frame()
        original = frame.copy(deep=True)
        expected = prepare_bars(frame)
        pd.testing.assert_frame_equal(frame, original)
        frame["timestamp"] = frame.timestamp.dt.tz_convert("UTC")
        pd.testing.assert_frame_equal(prepare_bars(frame), expected)
        frame["timestamp"] = frame.timestamp.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
        pd.testing.assert_frame_equal(prepare_bars(frame), expected)

    def test_explicit_start_labels_shift_by_five_minutes_only_once(self):
        frame = self.source_frame().iloc[:3].copy()
        expected = prepare_bars(frame)
        actual = prepare_bars(frame, timestamp_label="start")
        self.assertTrue(actual.datetime.eq(expected.datetime + pd.Timedelta(minutes=5)).all())
        self.assertEqual(actual.close.tolist(), expected.close.tolist())

    def test_filter_by_day_and_contract(self):
        self.write_source()
        summary = convert(self.source, self.output, start_day=date(2026, 8, 20),
                          end_day=date(2026, 8, 20), symbols=("mo2610-c-4100.CFFEX",))
        self.assertEqual((summary["input_rows"], summary["output_rows"], summary["files"]), (4, 2, 1))

    def test_command_arguments_and_input_file_preserved(self):
        self.write_source()
        original = self.source.read_bytes()
        main(["--input", str(self.source), "--output-dir", str(self.output),
              "--start-day", "2026-08-21", "--end-day", "2026-08-21"])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(len(list(self.output.rglob("*.feather"))), 1)

    def test_open_interest_negative_missing_and_infinite_values_survive_real_file_roundtrip(self):
        frame = self.source_frame()
        frame["open_interest"] = [-3.0, float("nan"), float("inf"), float("-inf")]
        self.write_source(frame)
        output = StringIO()
        with redirect_stdout(output):
            summary = convert(self.source, self.output)
        self.assertEqual(summary["output_rows"], len(frame))
        self.assertEqual(summary["negative_open_interest_rows"], 1)
        self.assertEqual(summary["missing_open_interest_rows"], 1)
        self.assertEqual(summary["infinite_open_interest_rows"], 2)
        self.assertIn("样本", output.getvalue())
        self.assertIn("MO2610-C-4100", output.getvalue())
        restored = pd.concat([pd.read_feather(path) for path in self.output.rglob("*.feather")])
        restored = restored.sort_values(["trade_date", "symbol", "datetime"]).reset_index(drop=True)
        pd.testing.assert_frame_equal(restored, prepare_bars(frame))

    def test_nullable_integer_open_interest_preserves_missing_value_and_numeric_dtype(self):
        frame = self.source_frame()
        frame["open_interest"] = pd.Series([-3, pd.NA, 0, 7], dtype="Int64")
        self.write_source(frame)
        summary = convert(self.source, self.output)
        self.assertEqual(summary["negative_open_interest_rows"], 1)
        self.assertEqual(summary["missing_open_interest_rows"], 1)
        restored = pd.concat([pd.read_feather(path) for path in self.output.rglob("*.feather")])
        restored = restored.sort_values(["trade_date", "symbol", "datetime"]).reset_index(drop=True)
        pd.testing.assert_frame_equal(restored, prepare_bars(frame))
        self.assertEqual(str(restored.open_interest.dtype), "Int64")

    def invalid_ohlc_source(self):
        frame = self.source_frame()
        # 用户实际报错的三个价格组合：open < low，以及 close < low。
        frame.loc[0, ["open", "high", "low", "close"]] = [191.2, 219.0, 191.6, 191.6]
        frame.loc[1, ["open", "high", "low", "close"]] = [1.4, 1.4, 1.4, 1.2]
        frame.loc[2, ["open", "high", "low", "close"]] = [60.0, 60.0, 60.0, 51.8]
        return frame

    def test_reported_ohlc_anomalies_are_counted_and_preserved_without_repair_or_row_loss(self):
        frame = self.invalid_ohlc_source()
        self.write_source(frame)
        output = StringIO()
        with redirect_stdout(output):
            summary = convert(self.source, self.output)
        self.assertEqual(summary["invalid_ohlc_rows"], 3)
        self.assertEqual(summary["output_rows"], len(frame))
        self.assertIn("OHLC 区间异常 3 行", output.getvalue())
        restored = pd.concat([pd.read_feather(path) for path in self.output.rglob("*.feather")])
        keys, values = ["symbol", "datetime"], ["open", "high", "low", "close", "volume", "open_interest"]
        expected = frame.rename(columns={"code": "symbol", "timestamp": "datetime"})
        expected["symbol"] = expected.symbol.astype("string")
        expected["datetime"] = expected.datetime.astype("datetime64[ns, Asia/Shanghai]")
        pd.testing.assert_frame_equal(restored.set_index(keys)[values].sort_index(),
                                      expected.set_index(keys)[values].sort_index())

    def test_strict_ohlc_cli_rejects_reported_samples_before_writing_any_file(self):
        self.write_source(self.invalid_ohlc_source())
        with self.assertRaisesRegex(ValueError, "OHLC 大小关系"):
            main(["--input", str(self.source), "--output-dir", str(self.output), "--strict-ohlc"])
        self.assertFalse(self.output.exists())

    def test_existing_destination_fails_before_writing_other_files_and_overwrite_is_explicit(self):
        self.write_source()
        collision = self.output / "20260821/MO2610-C-4100_20260821.feather"
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            convert(self.source, self.output)
        self.assertEqual(collision.read_bytes(), b"original")
        self.assertEqual(list(self.output.rglob("*.feather")), [collision])
        convert(self.source, self.output, overwrite=True)
        self.assertEqual(len(pd.read_feather(collision)), 1)
        self.assertEqual(list(self.output.rglob("*.tmp")), [])

    def test_invalid_or_duplicate_rows_fail_before_creating_output(self):
        for field, value in (("code", "../bad"), ("volume", 0.5), ("volume", -1),
                             ("close", float("nan")), ("high", -1),
                             ("trade_date", pd.Timestamp("2026-08-19")),
                             ("act_time", pd.Timestamp("2026-08-20 09:45"))):
            with self.subTest(field=field):
                frame = self.source_frame()
                if field == "volume" and isinstance(value, float):
                    frame["volume"] = frame.volume.astype(float)
                frame.loc[0, field] = value
                self.write_source(frame)
                with self.assertRaises(ValueError):
                    convert(self.source, self.output)
                self.assertFalse(self.output.exists())
        frame = self.source_frame()
        self.write_source(pd.concat([frame, frame.iloc[:1]], ignore_index=True))
        with self.assertRaisesRegex(ValueError, "重复"):
            convert(self.source, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
