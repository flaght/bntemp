"""验证失败分钟诊断使用公共时间标签和真实文件身份，不启动交易引擎。"""
from datetime import date
from importlib import import_module
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from bomber.framework.dataprep.contracts import BarFileKey

check = import_module("demos.08_option_vega.check_backtest_data")


class BacktestDataTests(unittest.TestCase):
    def inspect(self, times, **kwargs):
        frame = pd.DataFrame({"datetime": pd.to_datetime(times),
            "open": 6500, "high": 6500, "low": 6500, "close": 6500, "volume": 0})
        key = BarFileKey("future", "IM2609", date(2026, 9, 10))
        with patch("pandas.read_feather", return_value=frame):
            return check.inspect_file(Path("IM2609_20260910.feather"), key,
                at="09:37", **kwargs)

    def test_late_first_bar_distinguished_from_missing_file(self):
        report = self.inspect(["2026-09-10 13:58", "2026-09-10 15:01"])
        self.assertEqual(report["status"], "NO_BAR_BY_DECISION")
        self.assertTrue(report["has_1358"])
        self.assertEqual(report["rows_by_decision"], 0)
        self.assertEqual(report["first_after_decision"], "2026-09-10T13:58:00+08:00")

    def test_zero_volume_bar_preserved_at_timeout_boundary(self):
        report = self.inspect(["2026-09-10 09:32"])
        self.assertEqual(report["status"], "FRESH_AT_DECISION")
        self.assertEqual(report["age_seconds"], 300)
        self.assertEqual(report["nearby_rows"][0]["volume"], "0")

    def test_six_minute_gap_is_stale(self):
        report = self.inspect(["2026-09-10 09:31"])
        self.assertEqual(report["status"], "STALE_AT_DECISION")
        self.assertEqual(report["age_seconds"], 360)

    def test_start_label_applied_once_before_decision_check(self):
        report = self.inspect(["2026-09-10 09:37"], timestamp_label="start")
        self.assertEqual(report["source_first"], "2026-09-10T09:37:00+08:00")
        self.assertEqual(report["completed_first"], "2026-09-10T09:38:00+08:00")
        self.assertEqual(report["status"], "NO_BAR_BY_DECISION")

    def test_wrong_trading_day_rejected_by_public_reader(self):
        with self.assertRaisesRegex(ValueError, "Day-session date differs"):
            self.inspect(["2026-09-09 15:01"])

    def test_missing_day_reports_previous_file_and_handles_lowercase(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "im2609_20260909.feather").touch()
            report = check.inspect_market(root, "future", "IM2609", date(2026, 9, 10), at="09:37")
        self.assertEqual(report["status"], "FILE_MISSING")
        self.assertTrue(report["previous_available_file"].endswith("im2609_20260909.feather"))


if __name__ == "__main__":
    unittest.main()
