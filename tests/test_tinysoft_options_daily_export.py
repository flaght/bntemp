"""验证期权日线字段、交易所、简称、昨收日期及真实 Feather 读写。"""

from datetime import date
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from examples.opts2.export_tinysoft_options_daily import (
    OUTPUT_COLUMNS, export_day, infer_exchange, load_names, prepare_daily_row,
)


class DailySession:
    def __init__(self, source, previous=None):
        self.source = source
        self.previous = pd.DataFrame() if previous is None else previous
        self.queries = []

    def run(self, sql):
        self.queries.append(sql)
        if "select *" in sql:
            return self.source
        if "max(TradeDate)" in sql:
            day = pd.NaT if self.previous.empty else pd.Timestamp("2026-11-04")
            return pd.DataFrame({"previous_trade_date": [day]})
        return self.previous


class OptionsDailyExportTests(unittest.TestCase):
    day = date(2026, 11, 5)

    def source(self):
        return pd.DataFrame([dict(
            Code="HO2611-C-2500", tradeID="HO2611-C-2500",
            instrumentName="上证50指数2611购2500", underlyerID="SH000016",
            TradeDate=pd.Timestamp(self.day), preSettlePrice=297.6,
            openPrice=296.6, highPrice=296.6, lowPrice=289.8, closePrice=292.6,
            settlePrice=304.6, volume=3, turnover=87900, openInterestAccumulate=70,
        )])

    def previous(self):
        return pd.DataFrame([dict(Code="HO2611-C-2500", TradeDate=pd.Timestamp("2026-11-04"),
                                  closePrice=286.2, settlePrice=297.6)])

    def test_exact_row_mapping_and_short_name(self):
        result = prepare_daily_row(self.source().iloc[0], day=self.day,
                                    previous=self.previous().iloc[0])
        self.assertEqual(list(result), list(OUTPUT_COLUMNS))
        self.assertEqual(result["code"], "HO2611-C-2500")
        self.assertEqual(result["secShortName"], "上证50购11月2500")
        self.assertEqual(result["exchangeCD"], "CCFX")
        self.assertEqual(result["preClosePrice"], 286.2)
        self.assertEqual(result["preSettlePrice"], 297.6)
        self.assertEqual(result["settlePrice"], 304.6)
        self.assertEqual(result["highestPrice"], 296.6)
        self.assertEqual(result["lowestPrice"], 289.8)
        self.assertEqual(result["turnoverVol"], 3.)
        self.assertEqual(result["turnoverValue"], 87900.)
        self.assertEqual(result["openInt"], 70)

    def test_etf_venues_and_original_op_identity(self):
        for underlyer, expected in (("SH510050", "XSHG"), ("SZ159922", "XSHE")):
            with self.subTest(underlyer=underlyer):
                row = self.source().assign(Code="OP90008430", tradeID="159922P2706M002900",
                                            instrumentName="中证500ETF沽6月2900",
                                            underlyerID=underlyer).iloc[0]
                result = prepare_daily_row(row, day=self.day)
                self.assertEqual(result["code"], "OP90008430")
                self.assertEqual(result["secShortName"], "中证500ETF沽6月2900")
                self.assertEqual(result["exchangeCD"], expected)

    def test_unknown_or_conflicting_exchange_rejected(self):
        row = self.source().iloc[0]
        with self.assertRaisesRegex(ValueError, "唯一确定交易所"):
            infer_exchange(row, {"exchangeCD": "XSHG"})
        with self.assertRaisesRegex(ValueError, "唯一确定交易所"):
            infer_exchange(self.source().assign(Code="OP100001", underlyerID="UNKNOWN").iloc[0])

    def test_template_name_override_and_alias_exchange(self):
        result = prepare_daily_row(self.source().iloc[0], day=self.day,
                                    name_reference={"secShortName": "已有简称", "exchangeCD": "CFFEX"})
        self.assertEqual(result["secShortName"], "已有简称")
        self.assertEqual(result["exchangeCD"], "CCFX")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "names.feather"
            pd.DataFrame(dict(code=["HO2611-C-2500"], secShortName=["已有简称"],
                              exchangeCD=["CCFX"])).to_feather(path)
            self.assertEqual(load_names(path)["HO2611-C-2500"]["secShortName"], "已有简称")

    def test_new_contract_missing_close_stays_nan_and_settlement_is_not_close(self):
        result = prepare_daily_row(self.source().iloc[0], day=self.day)
        self.assertTrue(pd.isna(result["preClosePrice"]))
        self.assertEqual(result["preSettlePrice"], 297.6)
        self.assertEqual(result["settlePrice"], 304.6)
        source = self.source().assign(preSettlePrice=float("nan"))
        result = prepare_daily_row(source.iloc[0], day=self.day, previous=self.previous().iloc[0])
        self.assertEqual(result["preSettlePrice"], 297.6)

    def test_future_reference_and_mismatched_contract_rejected(self):
        with self.assertRaisesRegex(ValueError, "必须早于"):
            prepare_daily_row(self.source().iloc[0], day=self.day,
                              previous=self.previous().assign(TradeDate=pd.Timestamp(self.day)).iloc[0])
        with self.assertRaisesRegex(ValueError, "Code"):
            prepare_daily_row(self.source().iloc[0], day=self.day,
                              previous=self.previous().assign(Code="HO2611-P-2500").iloc[0])

    def test_fractional_volume_or_interest_rejected(self):
        for column in ("volume", "openInterestAccumulate"):
            with self.subTest(column=column):
                with self.assertRaisesRegex(ValueError, "不是整数"):
                    prepare_daily_row(self.source().assign(**{column: 1.5}).iloc[0], day=self.day)

    def test_roundtrip_all_options_one_day_file_types_and_previous_query(self):
        source = pd.concat([
            self.source(), self.source().assign(Code="OP100001", underlyerID="SH510050",
                                                instrumentName="ETF购11月2500"),
            self.source().assign(Code="OP90008430", underlyerID="SZ159922",
                                 tradeID="159922P2706M002900", instrumentName="中证500ETF沽6月2900"),
        ], ignore_index=True)
        session = DailySession(source, self.previous())
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(session, day=self.day, output_dir=directory), (1, 3))
            path = Path(directory) / "2026-11-05.feather"
            bars = pd.read_feather(path)
            self.assertEqual(list(bars.columns), list(OUTPUT_COLUMNS))
            self.assertEqual(str(bars.trade_date.dtype), "datetime64[ns]")
            self.assertEqual(str(bars.turnoverVol.dtype), "float64")
            self.assertEqual(str(bars.openInt.dtype), "int64")
            self.assertEqual(set(bars.exchangeCD), {"CCFX", "XSHG", "XSHE"})
            self.assertEqual(len(session.queries), 3)
            self.assertIn('"opt_eod_patch"', session.queries[0])
            self.assertIn("where TradeDate = 2026.11.04", session.queries[2])
            self.assertEqual(export_day(session, day=self.day, output_dir=directory), (0, 0))
            self.assertEqual(export_day(session, day=self.day, output_dir=directory,
                                        overwrite=True), (1, 3))

    def test_bad_and_duplicate_contracts_skipped_without_losing_healthy_row(self):
        source = pd.concat([
            self.source(), self.source().assign(Code="OP100001", underlyerID="UNKNOWN"),
            self.source().assign(Code="OP100002", underlyerID="SH510050"),
            self.source().assign(Code="OP100002", underlyerID="SH510050"),
        ], ignore_index=True)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(DailySession(source), day=self.day,
                                        output_dir=directory), (1, 1))
            errors = pd.read_csv(Path(directory) / "2026-11-05.errors.csv")
            self.assertEqual(set(errors.Code), {"OP100001", "OP100002"})
            bars = pd.read_feather(Path(directory) / "2026-11-05.feather")
            self.assertEqual(bars.code.tolist(), ["HO2611-C-2500"])

    def test_empty_day_creates_no_files(self):
        session = DailySession(pd.DataFrame())
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(session, day=self.day, output_dir=directory), (0, 0))
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(len(session.queries), 1)


if __name__ == "__main__":
    unittest.main()
