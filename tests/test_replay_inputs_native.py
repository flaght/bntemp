"""Native preparation-only mode and uncached/certified full-report equivalence."""
from copy import deepcopy
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from bomber.framework.dataprep.prepared import PreparedSource, build_prepared_dataset
from tests import test_option_vega_native as native_fixture
from tests.test_prepared_dataset_native import comparable_report


@unittest.skipIf(native_fixture.NATIVE_ERROR is not None, f"需要Bomber编译扩展: {native_fixture.NATIVE_ERROR}")
class ReplayInputNativeTests(unittest.TestCase):
    def test_preparation_never_replays_and_certified_reports_equal_uncached_native_batch(self):
        from bomber.framework.market.replay.native_batch import NativeBarBatchParser, require_native_batch
        require_native_batch()
        fixture = native_fixture.VegaNativeSettlementTests()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            module, args = fixture.fixture(root, Decimal("120.13"), option_bar_minutes=5,
                execution_timing="next_market", future_hedge=True)
            specs = module.backtest_bar_specs(args)
            snapshot = root/"prepared/v1"
            build_prepared_dataset((PreparedSource("future", root/"kline/fut", "IM", specs["future"]),
                PreparedSource("option", root/"kline/opt", "MO", specs["option"])), snapshot,
                start_day=args.start_day, end_day=args.end_day)
            args.data_mode, args.bar_parser, args.prepared_root = "prepared", "native_batch", snapshot
            args.prepared_verify = "full"
            args.report_dir = root/"cold"
            cold = module.run_case(deepcopy(args))
            preparation = deepcopy(args)
            preparation.prepare_replay_inputs = root/"replay/v1"
            with patch("bomber.framework.trader.execution.simulation.backend.NautilusSimExecutionBackend.process_market_event",
                       side_effect=AssertionError("preparation advanced the engine")):
                output = module.run_case(preparation)
            self.assertEqual(output, preparation.prepare_replay_inputs)
            # Warm runs need the parent index/provenance, not its OHLCV files.
            shutil.rmtree(snapshot/"fut")
            shutil.rmtree(snapshot/"opt")
            shutil.rmtree(root/"kline")
            warm = deepcopy(args)
            warm.replay_inputs, warm.report_dir = output, root/"warm"
            frames = import_module("demos.08_option_vega.feature.backtest")
            with patch.object(NativeBarBatchParser, "prepare_arrays", side_effect=AssertionError("data rechecked")), \
                    patch.object(frames, "prepare_feature_frames", side_effect=AssertionError("frames rebuilt")):
                hot = module.run_case(warm)
            for path in (cold, hot):
                summary = json.loads((path/"summary.json").read_text())
                self.assertGreater(summary["fills"], 0)
                self.assertEqual(summary["settlements"], 1)
                self.assertEqual(summary["future_settlement_count"], 1)
                self.assertEqual(summary["remaining_positions_or_working"], {})
                self.assertEqual(summary["report_errors"], [])
            stats = json.loads((hot/"performance.json").read_text())
            summary = json.loads((hot/"summary.json").read_text())
            self.assertEqual(stats["counters"]["native_ready_rows"], summary["bars"])
            self.assertEqual(stats["counters"]["prepared_decision_frames"], summary["frames"])
            self.assertNotIn("native_batch_rounding", stats["timings"])
            self.assertNotIn("native_batch_array_prepare", stats["timings"])
            self.assertEqual(json.loads((cold/"feature_frames.json").read_text()),
                             json.loads((hot/"feature_frames.json").read_text()))
            for name in ("orders.csv", "fills.csv", "positions.csv", "account.csv", "signals.csv",
                         "settlements.csv", "future_settlements.csv"):
                with self.subTest(report=name):
                    reports = [comparable_report(pd.read_csv(path/name, dtype=str, keep_default_na=False))
                               for path in (cold, hot)]
                    pd.testing.assert_index_equal(reports[0].columns, reports[1].columns)
                    for column in reports[0].columns:
                        with self.subTest(report=name, column=column):
                            pd.testing.assert_frame_equal(reports[0][[column]], reports[1][[column]], obj=name)


if __name__ == "__main__":
    unittest.main()
