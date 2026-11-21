"""Run diagnostics on/off through real matching, fees and both settlement kinds."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from tests import test_option_vega_native as native_fixture
from tests.test_prepared_dataset_native import comparable_report


@unittest.skipIf(native_fixture.NATIVE_ERROR is not None, f"需要Bomber编译扩展: {native_fixture.NATIVE_ERROR}")
class EngineRunProfileNativeTests(unittest.TestCase):
    def test_diagnostics_preserve_signals_orders_fills_accounts_and_settlements(self):
        fixture = native_fixture.VegaNativeSettlementTests()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            module, args = fixture.fixture(root, Decimal("120.13"), option_bar_minutes=5,
                                          execution_timing="next_market", future_hedge=True)
            outputs = []
            for enabled in (False, True):
                run_args = deepcopy(args)
                run_args.profile_engine_run = enabled
                run_args.report_dir = root / ("profile-on" if enabled else "profile-off")
                output = module.run_case(run_args)
                outputs.append(output)
                performance = json.loads((output / "performance.json").read_text())
                summary = json.loads((output / "summary.json").read_text())
                self.assertGreater(summary["fills"], 0)
                self.assertEqual(summary["settlements"], 1)
                self.assertEqual(summary["future_settlement_count"], 1)
                self.assertEqual(summary["remaining_positions_or_working"], {})
                self.assertEqual("engine_run_profile" in performance, enabled)
                if enabled:
                    profile = performance["engine_run_profile"]
                    self.assertEqual(profile["revision"], "run-phase-timing-v1")
                    self.assertTrue(profile["enabled"])
                    self.assertGreater(profile["counters"]["run_calls"], 0)
                    self.assertGreater(profile["counters"]["data_events"], 0)
                    timings = profile["timings"]
                    children = sum(row["seconds"] for name, row in timings.items() if name != "engine_run_total")
                    self.assertGreaterEqual(timings["engine_run_total"]["seconds"] + 1e-9, children)
            for name in ("orders.csv", "fills.csv", "positions.csv", "account.csv", "signals.csv",
                         "settlements.csv", "future_settlements.csv"):
                with self.subTest(report=name):
                    frames = [comparable_report(pd.read_csv(output / name, dtype=str, keep_default_na=False))
                              for output in outputs]
                    pd.testing.assert_frame_equal(*frames, obj=name)


if __name__ == "__main__":
    unittest.main()
