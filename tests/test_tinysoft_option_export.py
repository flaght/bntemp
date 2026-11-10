"""无需连接 DolphinDB，验证 Tinysoft 字段口径和真实 Feather 导出。"""

from datetime import date
from pathlib import Path
import tempfile
import unittest
import os
from unittest.mock import patch

import pandas as pd

from examples.opts2.export_tinysoft_options_min import (
    connect_session, export_day, fill_previous_close, fill_previous_prices, prepare_bars,
)


class DailyReferenceSession:
    def __init__(self, current, previous, *, previous_day="2025-12-31",
                 minute_frame=None, minute_reference=None):
        self.current = current
        self.previous = previous
        self.previous_day = previous_day
        self.minute_frame = minute_frame
        self.minute_reference = minute_reference
        self.queries = []

    def run(self, sql):
        self.queries.append(sql)
        if '"opt_eod_patch"' in sql:
            if "max(TradeDate)" in sql:
                day = pd.NaT if self.previous_day is None else pd.Timestamp(self.previous_day)
                return pd.DataFrame({"previous_trade_date": [day]})
            if "preSettlePrice" in sql:
                return self.current
            return self.previous
        if "atImax" in sql:
            return self.minute_reference if self.minute_reference is not None else pd.DataFrame()
        if "max(TradeDate)" in sql:
            return pd.DataFrame({"previous_trade_date": [pd.NaT]})
        if self.minute_frame is None:
            raise AssertionError(f"不应执行的查询：{sql}")
        return self.minute_frame


class TinysoftOptionExportTests(unittest.TestCase):
    day = date(2026, 1, 5)

    def frame(self):
        return pd.DataFrame({
            "Code": [" HO2601-C-2650 "] * 4,
            "TradeDate": pd.to_datetime(["2026-01-05"] * 4),
            "ActTime": pd.to_datetime([
                "2026-01-05 09:35", "2026-01-05 09:50",
                "2026-01-05 10:20", "2026-01-05 10:25",
            ]),
            "openPrice": [373.6, 419.2, 424.2, 429.6],
            "highPrice": [373.6, 419.2, 424.2, 429.6],
            "lowPrice": [373.6, 419.2, 424.2, 429.6],
            "closePrice": [373.6, 419.2, 424.2, 429.6],
            "volume": [0, 1, 2, 1],
            "volumeAccumulate": [0, 1, 3, 4],
            "turnover": [0, 41920, 84840, 42960],
            "turnoverAccumulate": [0, 41920, 126760, 169720],
            "openInterest": [0, 1, 0, 1],
            "openInterestAccumulate": [25, 26, 26, 27],
            "refilled": [0] * 4,
            "bidPrice1": [400.0] * 4,
        })

    def test_exact_schema_values_timezone_and_dtypes(self):
        source = self.frame().iloc[::-1]
        original = source.copy(deep=True)
        bars = prepare_bars(source, day=self.day)
        self.assertEqual(list(bars.columns), [
            "symbol", "trade_date", "datetime", "open", "high", "low", "close",
            "volume", "turnover", "volume_accumulate", "turnover_accumulate",
            "open_interest", "prev_close", "prev_settlement",
        ])
        self.assertEqual(bars.volume.tolist(), [0, 1, 2, 1])
        self.assertEqual(bars.volume_accumulate.tolist(), [0, 1, 3, 4])
        self.assertEqual(bars.turnover.tolist(), [0.0, 41920.0, 84840.0, 42960.0])
        self.assertEqual(bars.turnover_accumulate.tolist(),
                         [0.0, 41920.0, 126760.0, 169720.0])
        self.assertEqual(bars.open_interest.tolist(), [25.0, 26.0, 26.0, 27.0])
        self.assertEqual(bars.symbol.tolist(), ["HO2601-C-2650"] * 4)
        self.assertEqual(bars.trade_date.tolist(), ["2026-01-05"] * 4)
        self.assertEqual(bars.datetime.iloc[0],
                         pd.Timestamp("2026-01-05 09:35", tz="Asia/Shanghai"))
        self.assertEqual(str(bars.datetime.dtype), "datetime64[ns, Asia/Shanghai]")
        for name in ("volume", "volume_accumulate"):
            self.assertEqual(str(bars[name].dtype), "int64")
        for name in ("open", "high", "low", "close", "turnover",
                     "turnover_accumulate", "open_interest", "prev_close",
                     "prev_settlement"):
            self.assertEqual(str(bars[name].dtype), "float64")
        self.assertTrue(bars[["prev_close", "prev_settlement"]].isna().all().all())
        pd.testing.assert_frame_equal(source, original)

    def test_utc_input_preserves_beijing_wall_clock(self):
        source = self.frame()
        source["ActTime"] = source.ActTime.dt.tz_localize(
            "Asia/Shanghai",
        ).dt.tz_convert("UTC")
        bars = prepare_bars(source, day=self.day)
        self.assertEqual(bars.datetime.dt.hour.tolist(), [9, 9, 10, 10])
        self.assertEqual(bars.datetime.dt.minute.tolist(), [35, 50, 20, 25])

    def test_op_codes_preserved_alongside_other_options(self):
        source = pd.concat([
            self.frame(),
            self.frame().assign(Code=" OP10009633 "),
            self.frame().assign(Code="OP10009634"),
        ], ignore_index=True)
        bars = prepare_bars(source, day=self.day)
        self.assertEqual(len(bars), 12)
        self.assertEqual(set(bars.symbol),
                         {"HO2601-C-2650", "OP10009633", "OP10009634"})
        self.assertEqual(bars.groupby("symbol").size().tolist(), [4, 4, 4])
        for symbol, group in bars.groupby("symbol"):
            self.assertEqual(group.volume.tolist(), [0, 1, 2, 1])
            self.assertEqual(group.open_interest.tolist(), [25.0, 26.0, 26.0, 27.0])

    def test_real_previous_prices_preserved_and_position_may_decrease(self):
        source = self.frame()
        source["prev_close"] = 370.0
        source["prev_settlement"] = 371.2
        source.loc[3, "openInterest"] = -1
        source.loc[3, "openInterestAccumulate"] = 25
        bars = prepare_bars(source, day=self.day)
        self.assertEqual(bars.prev_close.tolist(), [370.0] * 4)
        self.assertEqual(bars.prev_settlement.tolist(), [371.2] * 4)
        self.assertEqual(bars.open_interest.tolist(), [25.0, 26.0, 26.0, 25.0])

    def test_known_previous_close_fills_same_contract_without_history(self):
        source = self.frame()
        source["prev_close"] = [370.0, float("nan"), float("nan"), float("nan")]
        source["prev_settlement"] = 371.2
        bars = prepare_bars(source, day=self.day)

        class Session:
            def run(self, sql):
                raise AssertionError("已有昨收不应查询历史数据")

        filled = fill_previous_close(Session(), bars, day=self.day)
        self.assertEqual(filled.prev_close.tolist(), [370.0] * 4)
        self.assertEqual(filled.prev_settlement.tolist(), [371.2] * 4)
        self.assertTrue(bars.prev_close.iloc[1:].isna().all())

    def test_previous_available_day_used_without_older_contract_fallback(self):
        source = pd.concat([
            self.frame(), self.frame().assign(Code="OP10009633"),
        ], ignore_index=True)
        bars = prepare_bars(source, day=self.day)

        class Session:
            def __init__(self):
                self.queries = []

            def run(self, sql):
                self.queries.append(sql)
                if "max(TradeDate)" in sql:
                    return pd.DataFrame({
                        "previous_trade_date": [pd.Timestamp("2025-12-31")],
                    })
                return pd.DataFrame({
                    "Code": [" HO2601-C-2650 "], "prev_close": [360.2],
                })

        session = Session()
        filled = fill_previous_close(session, bars, day=self.day)
        self.assertEqual(filled.loc[filled.symbol == "HO2601-C-2650",
                                    "prev_close"].tolist(), [360.2] * 4)
        self.assertTrue(filled.loc[filled.symbol == "OP10009633",
                                   "prev_close"].isna().all())
        self.assertTrue(filled.prev_settlement.isna().all())
        self.assertEqual(len(session.queries), 2)
        self.assertIn("where TradeDate < 2026.01.05", session.queries[0])
        self.assertIn("where TradeDate = 2025.12.31", session.queries[1])
        self.assertIn("atImax(ActTime, closePrice)", session.queries[1])
        self.assertTrue(bars.prev_close.isna().all())

    def test_no_earlier_day_keeps_previous_close_missing(self):
        class Session:
            def run(self, sql):
                return pd.DataFrame({"previous_trade_date": [pd.NaT]})

        bars = prepare_bars(self.frame(), day=self.day)
        filled = fill_previous_close(Session(), bars, day=self.day)
        self.assertTrue(filled.prev_close.isna().all())
        self.assertTrue(filled.prev_settlement.isna().all())

    def daily_references(self):
        current = pd.DataFrame({
            "Code": [" HO2601-C-2650 "],
            "TradeDate": [pd.Timestamp("2026-01-05")],
            "prev_settlement": [371.2],
        })
        previous = pd.DataFrame({
            "Code": ["HO2601-C-2650"],
            "TradeDate": [pd.Timestamp("2025-12-31")],
            "prev_close": [360.2],
            "prev_settlement": [359.5],
        })
        return current, previous

    def test_daily_close_and_current_pre_settlement_take_priority(self):
        current, previous = self.daily_references()
        session = DailyReferenceSession(current, previous)
        bars = prepare_bars(self.frame(), day=self.day)
        filled = fill_previous_prices(session, bars, day=self.day)
        self.assertEqual(filled.prev_close.tolist(), [360.2] * 4)
        self.assertEqual(filled.prev_settlement.tolist(), [371.2] * 4)
        self.assertTrue(bars[["prev_close", "prev_settlement"]].isna().all().all())
        self.assertEqual(len(session.queries), 3)
        self.assertIn("preSettlePrice as prev_settlement", session.queries[0])
        self.assertIn("where TradeDate = 2026.01.05", session.queries[0])
        self.assertNotIn("closePrice", session.queries[0])
        self.assertNotIn("settlePrice as", session.queries[0])
        self.assertIn("where TradeDate < 2026.01.05", session.queries[1])
        self.assertIn("where TradeDate = 2025.12.31", session.queries[2])

    def test_previous_settlement_fallback_and_existing_close_preserved(self):
        current, previous = self.daily_references()
        current["prev_settlement"] = float("nan")
        bars = prepare_bars(self.frame().assign(prev_close=370.0), day=self.day)
        session = DailyReferenceSession(current, previous)
        filled = fill_previous_prices(session, bars, day=self.day)
        self.assertEqual(filled.prev_close.tolist(), [370.0] * 4)
        self.assertEqual(filled.prev_settlement.tolist(), [359.5] * 4)

    def test_daily_missing_close_uses_same_day_minute_fallback(self):
        current, previous = self.daily_references()
        previous["prev_close"] = float("nan")
        minute = pd.DataFrame({"Code": ["HO2601-C-2650"], "prev_close": [358.0]})
        session = DailyReferenceSession(current, previous, minute_reference=minute)
        filled = fill_previous_prices(session, prepare_bars(self.frame(), day=self.day),
                                      day=self.day)
        self.assertEqual(filled.prev_close.tolist(), [358.0] * 4)
        self.assertEqual(filled.prev_settlement.tolist(), [371.2] * 4)
        minute_queries = [q for q in session.queries if '"opt_5min_tiny"' in q]
        self.assertEqual(len(minute_queries), 1)
        self.assertIn("where TradeDate = 2025.12.31", minute_queries[0])

    def test_current_pre_settlement_available_without_any_prior_history(self):
        current, _ = self.daily_references()
        session = DailyReferenceSession(current, pd.DataFrame(), previous_day=None)
        filled = fill_previous_prices(session, prepare_bars(self.frame(), day=self.day),
                                      day=self.day)
        self.assertTrue(filled.prev_close.isna().all())
        self.assertEqual(filled.prev_settlement.tolist(), [371.2] * 4)

    def test_missing_daily_contract_remains_missing(self):
        current, previous = self.daily_references()
        source = pd.concat([self.frame(), self.frame().assign(Code="OP10009633")],
                           ignore_index=True)
        session = DailyReferenceSession(current, previous)
        filled = fill_previous_prices(session, prepare_bars(source, day=self.day),
                                      day=self.day)
        op = filled.loc[filled.symbol == "OP10009633"]
        self.assertEqual(len(filled), 8)
        self.assertTrue(op[["prev_close", "prev_settlement"]].isna().all().all())

    def test_duplicate_or_wrong_date_daily_rows_rejected(self):
        current, previous = self.daily_references()
        for invalid, message in [
            (pd.concat([current, current]), "重复 Code"),
            (current.assign(TradeDate=pd.Timestamp("2026-01-06")), "错误 TradeDate"),
        ]:
            with self.subTest(message=message):
                session = DailyReferenceSession(invalid, previous)
                with self.assertRaisesRegex(ValueError, message):
                    fill_previous_prices(session, prepare_bars(self.frame(), day=self.day),
                                         day=self.day)

    def test_invalid_records_rejected_before_export(self):
        cases = [
            (pd.concat([self.frame(), self.frame().iloc[:1]]), "重复合约时间"),
            (self.frame().assign(TradeDate=pd.Timestamp("2026-01-06")), "其他交易日"),
            (self.frame().assign(Code="not-an-option"), "非预期合约"),
            (self.frame().assign(volume=0.5), "不是整数"),
            (self.frame().drop(columns="turnoverAccumulate"), "缺少源字段"),
            (self.frame().assign(ActTime=pd.NaT), "ActTime 存在空值"),
        ]
        for source, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    prepare_bars(source, day=self.day)

    def test_feather_roundtrip_paths_and_overwrite(self):
        source = pd.concat([
            self.frame(), self.frame().assign(Code="MO2601-P-8500"),
            self.frame().assign(Code="OP10009633"),
        ], ignore_index=True)

        class Session:
            def run(self, sql):
                if "max(TradeDate)" in sql:
                    return pd.DataFrame({"previous_trade_date": [pd.NaT]})
                if '"opt_eod_patch"' in sql:
                    return pd.DataFrame()
                self.sql = sql
                return source

        session = Session()
        with tempfile.TemporaryDirectory() as directory:
            result = export_day(session, day=self.day, output_dir=directory)
            self.assertEqual(result, (3, 12))
            self.assertIn('loadTable("dfs://JC_MKT_MIN", "opt_5min_tiny")',
                          session.sql)
            self.assertIn("where TradeDate = 2026.01.05", session.sql)
            root = Path(directory) / "20260105"
            for symbol, group in source.groupby("Code"):
                path = root / f"{symbol.strip()}_20260105.feather"
                actual = pd.read_feather(path)
                pd.testing.assert_frame_equal(actual, prepare_bars(group, day=self.day))
            self.assertEqual(export_day(session, day=self.day, output_dir=directory), (0, 0))
            self.assertEqual(export_day(session, day=self.day, output_dir=directory,
                                        overwrite=True), (3, 12))
            self.assertEqual(list(root.glob("*.partial")), [])

    def test_export_fills_previous_close_before_feather_write(self):
        source = self.frame()

        class Session:
            def run(self, sql):
                if '"opt_eod_patch"' in sql:
                    if "max(TradeDate)" in sql:
                        return pd.DataFrame({"previous_trade_date": [pd.NaT]})
                    return pd.DataFrame()
                if "max(TradeDate)" in sql:
                    return pd.DataFrame({
                        "previous_trade_date": [pd.Timestamp("2025-12-31")],
                    })
                if "atImax" in sql:
                    return pd.DataFrame({
                        "Code": ["HO2601-C-2650"], "prev_close": [360.2],
                    })
                return source

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(Session(), day=self.day, output_dir=directory),
                             (1, 4))
            path = Path(directory) / "20260105" / "HO2601-C-2650_20260105.feather"
            bars = pd.read_feather(path)
            self.assertEqual(bars.prev_close.tolist(), [360.2] * 4)
            self.assertTrue(bars.prev_settlement.isna().all())
            self.assertEqual(len(bars.columns), 14)

    def test_daily_reference_feather_roundtrip(self):
        current, previous = self.daily_references()
        session = DailyReferenceSession(current, previous, minute_frame=self.frame())
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(session, day=self.day, output_dir=directory),
                             (1, 4))
            path = Path(directory) / "20260105" / "HO2601-C-2650_20260105.feather"
            bars = pd.read_feather(path)
            self.assertEqual(bars.prev_close.tolist(), [360.2] * 4)
            self.assertEqual(bars.prev_settlement.tolist(), [371.2] * 4)
            self.assertEqual(str(bars.prev_close.dtype), "float64")
            self.assertEqual(str(bars.prev_settlement.dtype), "float64")
            self.assertEqual(len(bars.columns), 14)

    def test_empty_query_creates_no_directory(self):
        class Session:
            def run(self, sql):
                return pd.DataFrame()

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "export"
            self.assertEqual(export_day(Session(), day=self.day, output_dir=output),
                             (0, 0))
            self.assertFalse(output.exists())

    def test_bad_contracts_skipped_and_daily_references_still_batched(self):
        source = pd.concat([
            self.frame().assign(volume=-1),
            self.frame().assign(Code="OP10009633"),
            self.frame().assign(Code="invalid-code"),
            self.frame().assign(Code=None),
        ], ignore_index=True)
        session = DailyReferenceSession(pd.DataFrame(), pd.DataFrame(),
                                         previous_day=None, minute_frame=source)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(session, day=self.day, output_dir=directory), (1, 4))
            root = Path(directory) / "20260105"
            self.assertEqual([p.name for p in root.glob("*.feather")],
                             ["OP10009633_20260105.feather"])
            errors = pd.read_csv(root / "export_errors.csv", keep_default_na=False)
            self.assertEqual(set(errors.Code), {"HO2601-C-2650", "invalid-code", "<NA>"})
            self.assertEqual(set(errors.stage), {"conversion"})
            queries = [q for q in session.queries if "preSettlePrice" in q]
            self.assertEqual(len(queries), 1)

    def test_failed_option_write_cleans_partial_and_other_contract_continues(self):
        source = pd.concat([self.frame(), self.frame().assign(Code="OP10009633")],
                           ignore_index=True)
        session = DailyReferenceSession(pd.DataFrame(), pd.DataFrame(),
                                         previous_day=None, minute_frame=source)
        real_write = pd.DataFrame.to_feather

        def write(frame, path, **kwargs):
            if frame.symbol.iloc[0] == "HO2601-C-2650":
                Path(path).write_bytes(b"partial")
                raise OSError("模拟期权写入失败")
            return real_write(frame, path, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(pd.DataFrame, "to_feather", write):
                result = export_day(session, day=self.day, output_dir=directory)
            self.assertEqual(result, (1, 4))
            root = Path(directory) / "20260105"
            self.assertEqual(list(root.glob("*.partial")), [])
            self.assertFalse((root / "HO2601-C-2650_20260105.feather").exists())
            self.assertTrue((root / "OP10009633_20260105.feather").exists())
            self.assertEqual(pd.read_csv(root / "export_errors.csv").stage.tolist(), ["write"])

    def test_reference_and_legacy_connections_check_result_once(self):
        class Session:
            connected = True

            def connect(self, **kwargs):
                self.config = kwargs
                return self.connected

        session = Session()
        reference = dict(DDB_REFERENCE_HOST="reference.example", DDB_REFERENCE_PORT="12345",
                         DDB_REFERENCE_USERNAME="reference_user", DDB_REFERENCE_PASSWORD="test-only",
                         DDB_HOST="legacy.example", DDB_PASSWORD="legacy-test")
        with patch.dict(os.environ, reference, clear=True):
            connect_session(session)
        self.assertEqual(session.config, dict(host="reference.example", port=12345,
                                              userid="reference_user", password="test-only"))
        legacy = dict(DDB_HOST="legacy.example", DDB_PORT="9999", DDB_USER="legacy_user",
                      DDB_PASSWORD="test-only")
        session.connected = False
        with patch.dict(os.environ, legacy, clear=True):
            with self.assertRaisesRegex(ConnectionError, "legacy.example:9999") as caught:
                connect_session(session)
        self.assertNotIn("test-only", str(caught.exception))

    def test_reported_zero_volume_open_above_high_rejected_with_details(self):
        source = self.frame().iloc[:1].assign(
            Code="MO2606-C-6000", TradeDate=pd.Timestamp("2026-06-22"),
            ActTime=pd.Timestamp("2026-06-22 09:35"),
            openPrice=2761.6, highPrice=2752., lowPrice=2752., closePrice=2752., volume=0,
        )
        issues = []
        with self.assertRaisesRegex(ValueError, "OHLC.*1 行"):
            prepare_bars(source, day=date(2026, 6, 22), validation_issues=issues)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["open"], 2761.6)
        self.assertEqual(issues[0]["high"], 2752.)
        self.assertEqual(issues[0]["volume"], 0)
        self.assertEqual(issues[0]["issue"], "HIGH_BELOW_OPEN_OR_CLOSE")

    def test_tiny_source_example_preserves_interval_zero_and_nonzero_cumulative(self):
        source = self.frame().iloc[:1].drop(columns="refilled").assign(
            Code="MO2706-C-6000", TradeDate=pd.Timestamp("2026-11-06"),
            ActTime=pd.Timestamp("2026-11-06 10:55"),
            openPrice=669.6, highPrice=669.6, lowPrice=669.6, closePrice=669.6,
            volume=0, volumeAccumulate=19, turnover=0, turnoverAccumulate=1264440,
            openInterest=0, openInterestAccumulate=527,
        )
        bars = prepare_bars(source, day=date(2026, 11, 6))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars.volume.iloc[0], 0)
        self.assertEqual(bars.volume_accumulate.iloc[0], 19)
        self.assertEqual(bars.turnover.iloc[0], 0.)
        self.assertEqual(bars.turnover_accumulate.iloc[0], 1264440.)
        self.assertEqual(bars.open_interest.iloc[0], 527.)
        self.assertEqual(len(bars.columns), 14)

    def test_warn_preserves_bad_prices_and_default_rejects_zero_prices(self):
        source = self.frame().iloc[:1].assign(openPrice=400.)
        original = source.copy(deep=True)
        issues = []
        bars = prepare_bars(source, day=self.day, ohlc_policy="warn", validation_issues=issues)
        self.assertEqual(bars.open.iloc[0], 400.)
        self.assertEqual(bars.high.iloc[0], 373.6)
        self.assertEqual(issues[0]["action"], "warn_preserve")
        pd.testing.assert_frame_equal(source, original)
        zero = self.frame().iloc[:1].assign(openPrice=0., highPrice=0., lowPrice=0., closePrice=0.)
        with self.assertRaisesRegex(ValueError, "ZERO_PRICE"):
            prepare_bars(zero, day=self.day)

    def test_price_validation_report_and_new_source_for_both_policies(self):
        bad = self.frame().copy()
        bad.loc[0, "openPrice"] = 400.
        source = pd.concat([bad, self.frame().assign(Code="OP10009633")], ignore_index=True)
        with tempfile.TemporaryDirectory() as directory:
            for policy, expected in (("reject", (1, 4)), ("warn", (2, 8))):
                session = DailyReferenceSession(pd.DataFrame(), pd.DataFrame(),
                                                 previous_day=None, minute_frame=source)
                output = Path(directory) / policy
                result = export_day(session, day=self.day, output_dir=output, ohlc_policy=policy)
                self.assertEqual(result, expected)
                root = output / "20260105"
                report = pd.read_csv(root / "bar_validation.csv")
                self.assertEqual(report.Code.tolist(), ["HO2601-C-2650"])
                self.assertEqual(report.open.tolist(), [400.])
                self.assertEqual(report.high.tolist(), [373.6])
                self.assertEqual(report.exported_this_run.tolist(), [policy == "warn"])
                self.assertTrue((root / "OP10009633_20260105.feather").exists())
                self.assertEqual((root / "HO2601-C-2650_20260105.feather").exists(), policy == "warn")
                minute_queries = [q for q in session.queries if "opt_5min" in q]
                self.assertTrue(minute_queries)
                self.assertTrue(all('"opt_5min_tiny"' in q for q in minute_queries))
                self.assertFalse(any("opt_5min_patch" in q for q in session.queries))
                self.assertIn("where TradeDate = 2026.01.05", session.queries[0])

    def test_rejected_existing_file_is_preserved_and_marked_in_report(self):
        source = self.frame().assign(openPrice=500.)
        session = DailyReferenceSession(pd.DataFrame(), pd.DataFrame(),
                                         previous_day=None, minute_frame=source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "20260105"
            root.mkdir()
            original = root / "HO2601-C-2650_20260105.feather"
            original.write_bytes(b"original output")
            self.assertEqual(export_day(session, day=self.day, output_dir=directory,
                                        overwrite=True), (0, 0))
            self.assertEqual(original.read_bytes(), b"original output")
            report = pd.read_csv(root / "bar_validation.csv")
            self.assertTrue(report.existing_file_kept.all())
            self.assertFalse(report.exported_this_run.any())


if __name__ == "__main__":
    unittest.main()
