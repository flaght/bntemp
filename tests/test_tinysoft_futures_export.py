"""验证期货累计口径、夜盘时间、合约映射及真实 Feather 读写。"""

from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from examples.opts2.export_tinysoft_futures import (
    OUTPUT_COLUMNS, build_specs, export_day, prepare_bars,
)


class TinysoftFuturesTests(unittest.TestCase):
    day = date(2026, 1, 5)
    spec = dict(symbol="a2601", exchange="XDCE", multiplier=10.0)

    def frame(self, code="A2601"):
        return pd.DataFrame({
            "Code": [code] * 3,
            "TradeDate": pd.to_datetime(["2026-01-05"] * 3),
            "ActTime": pd.to_datetime(["2026-01-02 21:00", "2026-01-05 09:01",
                                       "2026-01-05 09:02"]),
            "openPrice": [4207.] * 3, "highPrice": [4207.] * 3,
            "lowPrice": [4207.] * 3, "closePrice": [4207.] * 3,
            "volume": [0, 2, 5], "volumeAccumulate": [0, 2, 5],
            "turnover": [0, 84140, 210350], "turnoverAccumulate": [0, 84140, 210350],
            "openInterest": [-6454, 0, -1], "openInterestAccumulate": [6454, 6454, 6453],
        })

    def session(self, source, info):
        class Session:
            def run(self, sql):
                return info if "fut_instrument_base_info" in sql else source
        return Session()

    def test_schema_increments_positions_and_actual_night_date(self):
        bars = prepare_bars(self.frame().iloc[::-1], day=self.day, spec=self.spec)
        self.assertEqual(tuple(bars.columns), OUTPUT_COLUMNS)
        self.assertEqual(bars.volume.tolist(), [0., 2., 3.])
        self.assertEqual(bars.value.tolist(), [0., 84140., 126210.])
        self.assertEqual(bars.open_interest.tolist(), [6454., 6454., 6453.])
        self.assertEqual(bars.date.tolist(), ["2026-01-02", "2026-01-05", "2026-01-05"])
        self.assertEqual(bars.time.tolist(), ["21:00:00", "09:01:00", "09:02:00"])
        self.assertIsNone(bars.datetime.dt.tz)
        self.assertEqual(str(bars.datetime.dtype), "datetime64[ns]")
        self.assertEqual(bars.vt_symbol.tolist(), ["a2601.XDCE"] * 3)
        self.assertEqual(bars.vwap.tolist(), [4207.] * 3)
        for name in ("open", "high", "low", "close", "volume", "value", "open_interest", "vwap"):
            self.assertEqual(str(bars[name].dtype), "float64")

    def test_exchange_case_and_czce_month_mapping(self):
        codes = ["RB2703", "A2601", "SA2601", "IF2601", "SC2601", "SI2601"]
        info = pd.DataFrame({"Code": codes, "TradeDate": [pd.Timestamp(self.day)] * 6,
                             "exchange": ["上海期货交易所", "大连商品交易所", "郑州商品交易所",
                                          "中国金融期货交易所", "上海国际能源交易中心", "广州期货交易所"],
                             "multiplier": [10., 10., 20., 300., 1000., 5.]})
        specs = build_specs(info, codes, day=self.day)
        self.assertEqual([s["symbol"] for s in specs.values()],
                         ["rb2703", "a2601", "SA601", "IF2601", "sc2601", "si2601"])
        self.assertEqual([s["exchange"] for s in specs.values()],
                         ["XSGE", "XDCE", "XZCE", "CCFX", "XSIE", "XGFE"])
        with self.assertRaisesRegex(ValueError, "之后"):
            build_specs(info.assign(TradeDate=pd.Timestamp("2026-10-29")), codes, day=self.day)
        with self.assertRaisesRegex(ValueError, "缺少"):
            build_specs(info.iloc[:1], codes, day=self.day)

    def test_first_row_requires_explicit_baseline(self):
        source = self.frame().iloc[1:]
        with self.assertRaisesRegex(ValueError, "首条累计值"):
            prepare_bars(source, day=self.day, spec=self.spec)
        bars = prepare_bars(source, day=self.day, spec=self.spec,
                            first_row_policy="zero-baseline")
        self.assertEqual(bars.volume.tolist(), [2., 3.])

    def test_missing_contract_uses_latest_consistent_product_metadata(self):
        info = pd.DataFrame({
            "Code": ["B2509", "B2603", "B2605"],
            "TradeDate": pd.to_datetime(["2025-12-30", "2026-01-05", "2026-01-05"]),
            "exchange": ["DCE", "大连商品交易所", "XDCE"],
            "multiplier": [20., 10., 10.],
        })
        spec = build_specs(info, ["B2610"], day=self.day)["B2610"]
        self.assertEqual(spec["symbol"], "b2610")
        self.assertEqual(spec["exchange"], "XDCE")
        self.assertEqual(spec["multiplier"], 10.)
        self.assertEqual(spec["metadata_source"], "product")
        with self.assertRaisesRegex(ValueError, "缺少"):
            build_specs(info, ["B2610"], day=self.day, metadata_fallback="error")

    def test_product_conflicts_and_different_prefix_rejected(self):
        info = pd.DataFrame({"Code": ["B2603", "B2605"],
                             "TradeDate": [pd.Timestamp(self.day)] * 2,
                             "exchange": ["DCE", "DCE"], "multiplier": [10., 20.]})
        with self.assertRaisesRegex(ValueError, "不一致"):
            build_specs(info, ["B2610"], day=self.day)
        info["multiplier"] = 10.
        info["exchange"] = ["DCE", "SHFE"]
        with self.assertRaisesRegex(ValueError, "不一致"):
            build_specs(info, ["B2610"], day=self.day)
        info["Code"] = ["BB2603", "BB2605"]
        with self.assertRaisesRegex(ValueError, "也无历史资料"):
            build_specs(info, ["B2610"], day=self.day)

    def test_exact_contract_metadata_takes_priority(self):
        info = pd.DataFrame({"Code": ["B2610", "B2605"],
                             "TradeDate": [pd.Timestamp(self.day)] * 2,
                             "exchange": ["DCE", "DCE"], "multiplier": [12., 10.]})
        spec = build_specs(info, ["B2610"], day=self.day)["B2610"]
        self.assertEqual(spec["multiplier"], 12.)
        self.assertEqual(spec["metadata_source"], "contract")

    def test_suffixed_contracts_preserve_identity_and_case(self):
        codes = ["L2611", "L2611F", "V2611F", "PP2611F"]
        info = pd.DataFrame({"Code": codes,
                             "TradeDate": [pd.Timestamp(self.day)] * len(codes),
                             "exchange": ["DCE"] * len(codes),
                             "multiplier": [5.] * len(codes)})
        specs = build_specs(info, codes, day=self.day)
        self.assertEqual([specs[c]["symbol"] for c in codes],
                         ["l2611", "l2611F", "v2611F", "pp2611F"])
        for code in codes:
            self.assertEqual(specs[code]["metadata_source"], "contract")

    def test_suffix_fallback_requires_matching_family(self):
        info = pd.DataFrame({"Code": ["L2610", "L2610F"],
                             "TradeDate": [pd.Timestamp(self.day)] * 2,
                             "exchange": ["DCE", "DCE"], "multiplier": [10., 5.]})
        specs = build_specs(info, ["L2611", "L2611F"], day=self.day)
        self.assertEqual(specs["L2611"]["multiplier"], 10.)
        self.assertEqual(specs["L2611F"]["multiplier"], 5.)
        with self.assertRaisesRegex(ValueError, "也无历史资料"):
            build_specs(info.iloc[:1], ["L2611F"], day=self.day)

    def test_rollback_duplicates_and_fractional_volume_rejected(self):
        for frame, message in [
            (self.frame().assign(volumeAccumulate=[0, 2, 1]), "回退"),
            (pd.concat([self.frame(), self.frame().iloc[:1]]), "重复时间"),
            (self.frame().assign(volumeAccumulate=[0, 2.5, 5]), "不是整数"),
        ]:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    prepare_bars(frame, day=self.day, spec=self.spec)

    def test_interval_vwap_and_utc_input(self):
        source = self.frame()
        source["ActTime"] = source.ActTime.dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC")
        bars = prepare_bars(source, day=self.day, spec=self.spec, vwap_mode="interval")
        self.assertEqual(bars.vwap.tolist(), [4207.] * 3)
        self.assertEqual(bars.datetime.iloc[0], pd.Timestamp("2026-01-02 21:00"))

    def test_export_paths_roundtrip_and_overwrite(self):
        source = pd.concat([self.frame(), self.frame("RB2601"), self.frame("B2610")],
                           ignore_index=True)

        class Session:
            def __init__(self):
                self.queries = []

            def run(self, sql):
                self.queries.append(sql)
                if "fut_instrument_base_info" in sql:
                    return pd.DataFrame({"Code": ["A2601", "RB2601", "B2605"],
                                         "TradeDate": [pd.Timestamp("2026-01-05")] * 3,
                                         "exchange": ["大连商品交易所", "上海期货交易所", "DCE"],
                                         "multiplier": [10., 10., 10.]})
                return source

        session = Session()
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(session, day=self.day, output_dir=directory), (3, 9))
            self.assertIn('"fut_1min_patch"', session.queries[0])
            self.assertIn("where TradeDate <= 2026.01.05", session.queries[1])
            for symbol in ("a2601", "rb2601", "b2610"):
                path = Path(directory) / "20260105" / f"{symbol}_20260105.feather"
                bars = pd.read_feather(path)
                self.assertEqual(tuple(bars.columns), OUTPUT_COLUMNS)
                self.assertEqual(bars.volume.tolist(), [0., 2., 3.])
                self.assertEqual(bars.symbol.tolist(), [symbol] * 3)
            self.assertEqual(export_day(session, day=self.day, output_dir=directory), (0, 0))
            self.assertEqual(export_day(session, day=self.day, output_dir=directory,
                                        overwrite=True), (3, 9))
            self.assertEqual(list(Path(directory).rglob("*.partial")), [])

    def test_empty_query_does_not_load_metadata(self):
        class Session:
            def run(self, sql):
                if "fut_instrument_base_info" in sql:
                    raise AssertionError("空行情不应查询资料")
                return pd.DataFrame()

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(Session(), day=self.day, output_dir=directory), (0, 0))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_suffixed_and_regular_contracts_write_separate_files(self):
        source = pd.concat([self.frame("L2611"), self.frame("L2611F")], ignore_index=True)

        class Session:
            def run(self, sql):
                if "fut_instrument_base_info" in sql:
                    return pd.DataFrame({"Code": ["L2611", "L2611F"],
                                         "TradeDate": [pd.Timestamp("2026-01-05")] * 2,
                                         "exchange": ["DCE", "DCE"], "multiplier": [5., 5.]})
                return source

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(Session(), day=self.day, output_dir=directory), (2, 6))
            for symbol in ("l2611", "l2611F"):
                path = Path(directory) / "20260105" / f"{symbol}_20260105.feather"
                bars = pd.read_feather(path)
                self.assertEqual(bars.vt_symbol.tolist(), [symbol + ".XDCE"] * 3)
                self.assertEqual(tuple(bars.columns), OUTPUT_COLUMNS)

    def test_bad_contracts_logged_while_healthy_contract_exports(self):
        source = pd.concat([
            self.frame("A2611").assign(volumeAccumulate=[0, 2, 1]),
            self.frame("RB2611"),
            self.frame("A2612").iloc[1:],
            self.frame("ZZZ2611"),
        ], ignore_index=True)
        info = pd.DataFrame({"Code": ["A2611", "RB2611", "A2612"],
                             "TradeDate": [pd.Timestamp(self.day)] * 3,
                             "exchange": ["DCE", "SHFE", "DCE"], "multiplier": [10.] * 3})
        with tempfile.TemporaryDirectory() as directory:
            result = export_day(self.session(source, info), day=self.day, output_dir=directory)
            self.assertEqual(result, (1, 3))
            root = Path(directory) / "20260105"
            self.assertEqual([p.name for p in root.glob("*.feather")], ["rb2611_20260105.feather"])
            errors = pd.read_csv(root / "export_errors.csv")
            self.assertEqual(set(errors.Code), {"A2611", "A2612", "ZZZ2611"})
            self.assertEqual(set(errors.stage), {"metadata", "conversion"})
            self.assertTrue(errors.error.str.contains("累计值回退").any())

    def test_ambiguous_aliases_skipped_without_overwriting_each_other(self):
        source = pd.concat([self.frame(c) for c in ["SA2601", "SA601", "A2601"]],
                           ignore_index=True)
        info = pd.DataFrame({"Code": ["SA2601", "SA601", "A2601"],
                             "TradeDate": [pd.Timestamp(self.day)] * 3,
                             "exchange": ["CZCE", "CZCE", "DCE"], "multiplier": [20., 20., 10.]})
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(export_day(self.session(source, info), day=self.day,
                                        output_dir=directory), (1, 3))
            root = Path(directory) / "20260105"
            self.assertTrue((root / "a2601_20260105.feather").exists())
            self.assertFalse((root / "SA601_20260105.feather").exists())
            self.assertEqual(set(pd.read_csv(root / "export_errors.csv").Code), {"SA2601", "SA601"})

    def test_failed_write_cleans_partial_and_continues(self):
        source = pd.concat([self.frame(), self.frame("RB2601")], ignore_index=True)
        info = pd.DataFrame({"Code": ["A2601", "RB2601"],
                             "TradeDate": [pd.Timestamp(self.day)] * 2,
                             "exchange": ["DCE", "SHFE"], "multiplier": [10., 10.]})
        real_write = pd.DataFrame.to_feather

        def write(frame, path, **kwargs):
            if frame.symbol.iloc[0] == "a2601":
                Path(path).write_bytes(b"partial")
                raise OSError("模拟单合约写入失败")
            return real_write(frame, path, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(pd.DataFrame, "to_feather", write):
                result = export_day(self.session(source, info), day=self.day, output_dir=directory)
            self.assertEqual(result, (1, 3))
            root = Path(directory) / "20260105"
            self.assertEqual(list(root.glob("*.partial")), [])
            self.assertFalse((root / "a2601_20260105.feather").exists())
            self.assertTrue((root / "rb2601_20260105.feather").exists())
            self.assertEqual(pd.read_csv(root / "export_errors.csv").stage.tolist(), ["write"])


if __name__ == "__main__":
    unittest.main()
