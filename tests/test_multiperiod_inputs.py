"""真实 Feather 输入验证与多周期注册；不依赖编译内核。"""
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS, ModuleType
import sys
import unittest
from unittest.mock import patch

import pandas as pd

from bomber.framework.dataprep.bars import read_bar_frame, add_prepared_bar_source
from bomber.framework.dataprep.contracts import BarReadSpec, BarFileKey, InputError


class MultiPeriodInputTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "MO2609-C-7000_20260911.feather"
        self.key = BarFileKey("option", "MO2609-C-7000", date(2026, 9, 11), "CFFEX")

    def frame(self):
        return pd.DataFrame(dict(datetime=pd.to_datetime(["2026-09-11 09:35", "2026-09-11 09:40"]),
            open=[12.0, 13.0], high=[12.4, 13.4], low=[11.8, 12.8], close=[12.2, 13.2], volume=[0, 1]))

    def test_m5_end_time_and_explicit_parser_binding(self):
        self.frame().to_feather(self.path)
        result = read_bar_frame(self.path, BarReadSpec(interval_seconds=300), self.key)
        calls = []
        parser = ModuleType("bomber.framework.market.replay.parsers.bar")
        parser.FixedInstrumentBarParser = lambda *args, **kwargs: NS(**kwargs)
        identity = NS(venue="CFFEX", symbol=NS(value=self.key.symbol))
        feed = NS(add_source=lambda *args: calls.append(args))
        with patch.dict(sys.modules, {parser.__name__: parser}):
            add_prepared_bar_source(feed, result, identity)
        self.assertEqual(calls[0][2].bar_spec, "5-MINUTE")
        self.assertEqual(result.frame.datetime.iloc[0], pd.Timestamp("2026-09-11 09:35", tz="Asia/Shanghai"))
        self.assertEqual(BarReadSpec().bar_spec, "1-MINUTE")

    def test_m5_start_labels_shift_by_five_minutes(self):
        self.frame().to_feather(self.path)
        result = read_bar_frame(self.path, BarReadSpec(interval_seconds=300, timestamp_label="start"), self.key)
        self.assertEqual(result.frame.datetime.iloc[0], pd.Timestamp("2026-09-11 09:40", tz="Asia/Shanghai"))

    def test_range_expansion_is_opt_in_audited_and_keeps_source_file_unchanged(self):
        frame = self.frame()
        frame.loc[0, "open"] = 11.4
        frame.to_feather(self.path)
        original = self.path.read_bytes()
        with self.assertRaisesRegex(InputError, "INVALID_OHLC"):
            read_bar_frame(self.path, BarReadSpec(interval_seconds=300), self.key)
        result = read_bar_frame(self.path, BarReadSpec(interval_seconds=300, ohlc_policy="expand_range"), self.key)
        self.assertEqual(result.frame.low.iloc[0], 11.4)
        self.assertEqual(result.frame.source_low.iloc[0], 11.8)
        self.assertEqual(result.frame.open.iloc[0], 11.4)
        self.assertEqual(result.issues[0].code, "OHLC_RANGE_EXPANDED")
        self.assertEqual(self.path.read_bytes(), original)

    def test_zero_price_drop_is_explicit_and_empty_result_has_no_fabricated_bar(self):
        frame = self.frame()
        frame[["open", "high", "low", "close"]] = 0
        frame.to_feather(self.path)
        with self.assertRaises(InputError): read_bar_frame(self.path, BarReadSpec(interval_seconds=300), self.key)
        result = read_bar_frame(self.path, BarReadSpec(interval_seconds=300, zero_price_policy="drop"), self.key)
        self.assertTrue(result.frame.empty)
        self.assertIsNone(result.first_ns)
        self.assertEqual(result.issues[0].code, "ZERO_PRICE_BAR_DROPPED")


if __name__ == "__main__": unittest.main()
