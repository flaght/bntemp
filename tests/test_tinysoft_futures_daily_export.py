"""日线原始量额、结算参考、代码映射及一日一文件的回归测试。"""

from datetime import date
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from examples.opts2.export_tinysoft_futures_daily import (
    OUTPUT_COLUMNS, build_daily_spec, export_day, load_names, prepare_daily_row,
)


class DailySession:
    def __init__(self, source, current, previous, info):
        self.source, self.current, self.previous, self.info = source, current, previous, info
        self.queries = []

    def run(self, sql):
        self.queries.append(sql)
        if '"sample_daily"' in sql:
            return self.source
        if "fut_instrument_base_info" in sql:
            return self.info
        if "max(TradeDate)" in sql:
            return pd.DataFrame({"previous_trade_date": [pd.Timestamp("2026-11-05")]})
        if "2026.11.06" in sql:
            return self.current
        return self.previous


class FuturesDailyExportTests(unittest.TestCase):
    day = date(2026, 11, 6)
    spec = dict(symbol="au2612", exchange="XSGE", multiplier=1000.)

    def source(self):
        return pd.DataFrame([dict(algoID=1, productID="AU", Code="AU2612",
            TradeDate=pd.Timestamp(self.day), openPrice=886.22, highPrice=888.66,
            lowPrice=867.1, closePrice=869.86, volume=346570, turnover=304717359220,
            openInterestAccumulate=149549, multiplier=0.762302)])

    def references(self):
        current = pd.DataFrame([dict(Code="AU2612", TradeDate=pd.Timestamp(self.day),
                                     preSettlePrice=885., settlePrice=871.)])
        previous = pd.DataFrame([dict(Code="AU2612", TradeDate=pd.Timestamp("2026-11-05"),
                                      closePrice=884., settlePrice=883.)])
        info = pd.DataFrame([dict(Code="AU2612", TradeDate=pd.Timestamp(self.day),
                                  exchange="上海期货交易所", multiplier=1000.)])
        return current, previous, info

    def test_daily_values_not_differenced_or_adjusted_by_source_multiplier(self):
        current, previous, _ = self.references()
        result = prepare_daily_row(self.source().iloc[0], day=self.day, spec=self.spec,
                                    daily=current.iloc[0], previous=previous.iloc[0])
        self.assertEqual(tuple(result), OUTPUT_COLUMNS)
        self.assertEqual(result["trade_date"], pd.Timestamp(self.day))
        self.assertEqual(result["code"], "au2612")
        self.assertEqual(result["contractObject"], "AU")
        self.assertEqual(result["preClosePrice"], 884.)
        self.assertEqual(result["preSettlePrice"], 885.)
        self.assertEqual(result["settlePrice"], 871.)
        self.assertEqual(result["closePrice"], 869.86)
        self.assertEqual(result["turnoverVol"], 346570)
        self.assertEqual(result["turnoverValue"], 304717359220.)
        self.assertEqual(result["openInt"], 149549.)
        self.assertEqual(result["secShortName"], "")

    def test_contract_case_month_and_suffix_mapping_without_multiplier(self):
        codes = ["PX2702", "RB2701", "IF2612", "L2611F"]
        info = pd.DataFrame(dict(Code=codes, TradeDate=[pd.Timestamp(self.day)] * 4,
                                  exchange=["CZCE", "SHFE", "CFFEX", "DCE"]))
        symbols = [build_daily_spec(info, code, day=self.day)["symbol"] for code in codes]
        self.assertEqual(symbols, ["PX702", "rb2701", "IF2612", "l2611F"])
        self.assertEqual(build_daily_spec(info, "RB2702", day=self.day)["symbol"], "rb2702")
        with self.assertRaisesRegex(ValueError, "之后"):
            build_daily_spec(info.assign(TradeDate=pd.Timestamp("2026-11-09")), "PX2702", day=self.day)

    def test_missing_reference_prices_stay_missing_and_no_current_close_fallback(self):
        result = prepare_daily_row(self.source().iloc[0], day=self.day, spec=self.spec)
        for name in ("preClosePrice", "preSettlePrice", "settlePrice"):
            self.assertTrue(pd.isna(result[name]))
        _, previous, _ = self.references()
        result = prepare_daily_row(self.source().iloc[0], day=self.day, spec=self.spec,
                                    previous=previous.iloc[0])
        self.assertEqual(result["preSettlePrice"], 883.)

    def test_bad_reference_date_and_fractional_volume_rejected(self):
        current, previous, _ = self.references()
        with self.assertRaisesRegex(ValueError, "参考日期"):
            prepare_daily_row(self.source().iloc[0], day=self.day, spec=self.spec,
                              previous=previous.assign(TradeDate=pd.Timestamp(self.day)).iloc[0])
        with self.assertRaisesRegex(ValueError, "不是整数"):
            prepare_daily_row(self.source().assign(volume=1.5).iloc[0], day=self.day, spec=self.spec)

    def test_roundtrip_one_date_one_file_and_reference_queries(self):
        current, previous, info = self.references()
        session = DailySession(self.source(), current, previous, info)
        with tempfile.TemporaryDirectory() as directory:
            result = export_day(session, day=self.day, source_table="sample_daily", algo_id=1,
                                output_dir=directory, names={"AU2612": "测试简称"})
            self.assertEqual(result, (1, 1))
            path = Path(directory) / "2026-11-06.feather"
            bars = pd.read_feather(path)
            self.assertEqual(list(bars.columns), list(OUTPUT_COLUMNS))
            self.assertEqual(str(bars.trade_date.dtype), "datetime64[ns]")
            self.assertEqual(str(bars.turnoverVol.dtype), "int64")
            self.assertEqual(str(bars.openInt.dtype), "float64")
            self.assertEqual(bars.secShortName.tolist(), ["测试简称"])
            self.assertEqual(bars.code.tolist(), ["au2612"])
            self.assertIn("algoID = 1", session.queries[0])
            self.assertTrue(any("where TradeDate <= 2026.11.06" in q for q in session.queries))
            self.assertTrue(any("where TradeDate = 2026.11.05" in q for q in session.queries))
            self.assertEqual(export_day(session, day=self.day, source_table="sample_daily",
                                        output_dir=directory), (0, 0))
            self.assertEqual(export_day(session, day=self.day, source_table="sample_daily",
                                        output_dir=directory, overwrite=True), (1, 1))

    def test_names_from_template_and_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "names.feather"
            pd.DataFrame(dict(code=["au2612"], secShortName=["测试简称"])).to_feather(path)
            self.assertEqual(load_names(path), {"AU2612": "测试简称"})
            pd.DataFrame(dict(code=["au2612", "AU2612"],
                              secShortName=["名称甲", "名称乙"])).to_feather(path)
            with self.assertRaisesRegex(ValueError, "不同"):
                load_names(path)

    def test_bad_or_duplicate_contracts_skipped_without_losing_healthy_row(self):
        source = pd.concat([self.source(), self.source().assign(Code="AU2702", volume=-1),
                            self.source().assign(Code="AU2704"),
                            self.source().assign(Code="AU2704", closePrice=870.)], ignore_index=True)
        current, previous, info = self.references()
        session = DailySession(source, current, previous, info)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(session, day=self.day, source_table="sample_daily",
                                        output_dir=directory), (1, 1))
            errors = pd.read_csv(Path(directory) / "2026-11-06.errors.csv")
            self.assertEqual(set(errors.Code), {"AU2702", "AU2704"})
            self.assertEqual(pd.read_feather(Path(directory) / "2026-11-06.feather").code.tolist(),
                             ["au2612"])

    def test_empty_day_creates_no_files(self):
        current, previous, info = self.references()
        session = DailySession(pd.DataFrame(), current, previous, info)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(session, day=self.day, source_table="sample_daily",
                                        output_dir=directory), (0, 0))
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(len(session.queries), 1)

    def test_all_algorithms_identical_contract_prices_export_once(self):
        source = pd.concat([self.source(), self.source().assign(algoID=2, multiplier=1.9)],
                           ignore_index=True)
        current, previous, info = self.references()
        session = DailySession(source, current, previous, info)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(session, day=self.day, source_table="sample_daily",
                                        output_dir=directory), (1, 1))
            self.assertNotIn("algoID =", session.queries[0])
            self.assertFalse((Path(directory) / "2026-11-06.errors.csv").exists())

    def test_default_eod_source_preserves_settlements_and_only_queries_previous_close(self):
        source = self.source().drop(columns=["algoID", "multiplier"]).assign(
            preSettlePrice=5196., openPrice=5198., highPrice=5216., lowPrice=5109.,
            closePrice=5130., settlePrice=5153., volume=270666., turnover=13948330540.,
            openInterestAccumulate=337658., Code="A2701", productID="A")
        previous = pd.DataFrame([dict(Code="A2701", TradeDate=pd.Timestamp("2026-11-05"),
                                      closePrice=5200., settlePrice=5196.)])

        class Session:
            def __init__(self):
                self.queries = []

            def run(self, sql):
                self.queries.append(sql)
                if "select *" in sql:
                    return source
                if "fut_instrument_base_info" in sql:
                    return pd.DataFrame([dict(Code="A2701", TradeDate=pd.Timestamp("2026-11-06"),
                                               exchange="DCE")])
                if "max(TradeDate)" in sql:
                    return pd.DataFrame({"previous_trade_date": [pd.Timestamp("2026-11-05")]})
                if "2026.11.06" in sql:
                    raise AssertionError("不得重复读取当天日行情")
                return previous

        session = Session()
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(session, day=self.day, output_dir=directory), (1, 1))
            bars = pd.read_feather(Path(directory) / "2026-11-06.feather")
            self.assertEqual(list(bars.columns), list(OUTPUT_COLUMNS))
            self.assertEqual(bars.code.tolist(), ["a2701"])
            self.assertEqual(bars.preClosePrice.tolist(), [5200.])
            self.assertEqual(bars.preSettlePrice.tolist(), [5196.])
            self.assertEqual(bars.settlePrice.tolist(), [5153.])
            self.assertEqual(bars.turnoverVol.tolist(), [270666])
            self.assertEqual(bars.turnoverValue.tolist(), [13948330540.])
            self.assertEqual(len(session.queries), 4)
            self.assertFalse(any("fut_eod_continuing" in query for query in session.queries))

    def test_ohlc_strict_rejects_and_warn_preserves_original_values(self):
        row = self.source().assign(openPrice=0., highPrice=0., lowPrice=0.,
                                   volume=0, turnover=0).iloc[0]
        with self.assertRaisesRegex(ValueError, "open=0.0.*volume=0"):
            prepare_daily_row(row, day=self.day, spec=self.spec)
        warnings = []
        result = prepare_daily_row(row, day=self.day, spec=self.spec,
                                    ohlc_policy="warn", ohlc_warnings=warnings)
        self.assertEqual(result["openPrice"], 0.)
        self.assertEqual(result["highestPrice"], 0.)
        self.assertEqual(result["lowestPrice"], 0.)
        self.assertEqual(result["closePrice"], 869.86)
        self.assertEqual(result["turnoverVol"], 0)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["closePrice"], 869.86)
        self.assertEqual(warnings[0]["Code"], "AU2612")

    def test_ohlc_warn_does_not_allow_invalid_numeric_values(self):
        for values in (dict(volume=-1), dict(openPrice=float("inf")),
                       dict(closePrice=float("nan")), dict(volume=1.5)):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    prepare_daily_row(self.source().assign(**values).iloc[0],
                                      day=self.day, spec=self.spec, ohlc_policy="warn")

    def test_ohlc_warn_roundtrip_overwrites_partial_day_and_saves_warning_values(self):
        source = pd.concat([self.source(), self.source().assign(
            Code="AU2702", openPrice=0., highPrice=0., lowPrice=0., volume=0, turnover=0,
        )], ignore_index=True)
        current, previous, info = self.references()
        session = DailySession(source, current, previous, info)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(session, day=self.day, source_table="sample_daily",
                                        output_dir=directory), (1, 1))
            error_path = Path(directory) / "2026-11-06.errors.csv"
            self.assertEqual(pd.read_csv(error_path).highPrice.tolist(), [0.])
            self.assertEqual(export_day(session, day=self.day, source_table="sample_daily",
                                        output_dir=directory, ohlc_policy="warn"), (0, 0))
            self.assertEqual(export_day(session, day=self.day, source_table="sample_daily",
                                        output_dir=directory, overwrite=True,
                                        ohlc_policy="warn"), (1, 2))
            bars = pd.read_feather(Path(directory) / "2026-11-06.feather")
            self.assertEqual(list(bars.columns), list(OUTPUT_COLUMNS))
            retained = bars.loc[bars.code.eq("au2702")].iloc[0]
            self.assertEqual(retained.highestPrice, 0.)
            self.assertEqual(retained.closePrice, 869.86)
            self.assertFalse(error_path.exists())
            warning_path = Path(directory) / "2026-11-06.ohlc_warnings.csv"
            warnings = pd.read_csv(warning_path)
            self.assertEqual(warnings.Code.tolist(), ["AU2702"])
            self.assertEqual(warnings.highPrice.tolist(), [0.])
            self.assertEqual(warnings.volume.tolist(), [0])
            self.assertEqual(warnings.exported.tolist(), [True])


if __name__ == "__main__":
    unittest.main()
