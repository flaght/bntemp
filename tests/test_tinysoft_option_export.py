"""无需连接 DolphinDB，验证 Tinysoft 字段口径和真实 Feather 导出。"""

from datetime import date
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from examples.opts2.export_tinysoft_options import (
    export_day, fill_previous_close, fill_previous_prices, prepare_bars,
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
        minute_queries = [q for q in session.queries if '"opt_5min_patch"' in q]
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
            self.assertIn('loadTable("dfs://JC_MKT_MIN", "opt_5min_patch")',
                          session.sql)
            self.assertIn("where TradeDate = 2026.01.05", session.sql)
            root = Path(directory) / "20260105"
            for symbol, group in source.groupby("Code"):
                path = root / f"{symbol.strip()}_20260105.feather"
                actual = pd.read_feather(path)
                pd.testing.assert_frame_equal(actual, prepare_bars(group, day=self.day))
            with self.assertRaises(FileExistsError):
                export_day(session, day=self.day, output_dir=directory)
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


if __name__ == "__main__":
    unittest.main()
