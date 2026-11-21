"""Run diagnostics on/off through real matching, fees and both settlement kinds."""
from copy import deepcopy
from decimal import Decimal
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from tests import test_option_vega_native as native_fixture
from tests.test_prepared_dataset_native import comparable_report


@unittest.skipIf(native_fixture.NATIVE_ERROR is not None, f"需要Bomber编译扩展: {native_fixture.NATIVE_ERROR}")
class EngineRunProfileNativeTests(unittest.TestCase):
    def test_log_gate_keeps_info_and_debug_file_sink_with_warning_stdout(self):
        # The Rust logger is process-global; each sink configuration needs a
        # fresh process, rather than inheriting the preceding test's guard.
        program = '''
import json, sys
from bomber.backtest.config import BacktestEngineConfig
from bomber.backtest.engine import BacktestEngine
from bomber.common.config import LoggingConfig
from bomber.common.component import is_logging_enabled, flush_logger
from bomber.common.enums import LogLevel
config = LoggingConfig(log_level=sys.argv[1], log_level_file=sys.argv[2] or None,
                       log_directory=sys.argv[3], log_file_name="sink-check")
engine = BacktestEngine(BacktestEngineConfig(logging=config, run_analysis=False))
print("SINK_PROBE " + json.dumps({"info": bool(is_logging_enabled(LogLevel.INFO)),
                               "debug": bool(is_logging_enabled(LogLevel.DEBUG))}), flush=True)
engine.run(start=1, end=2)
flush_logger()
engine.dispose()
'''
        for stdout_level, file_level, info, debug in (
            ("WARNING", "", False, False), ("INFO", "", True, False),
            ("WARNING", "DEBUG", True, True)):
            with self.subTest(stdout=stdout_level, file=file_level), TemporaryDirectory() as directory:
                environment = dict(os.environ)
                environment.pop("NAUTILUS_LOG", None)
                result = subprocess.run([sys.executable, "-c", program, stdout_level, file_level, directory],
                                        capture_output=True, text=True, env=environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                probe = next(line[len("SINK_PROBE "):] for line in result.stdout.splitlines()
                             if line.startswith("SINK_PROBE "))
                self.assertEqual(json.loads(probe), {"info": info, "debug": debug})
                self.assertEqual("BACKTEST RUN" in result.stdout, stdout_level == "INFO")
                if file_level:
                    logs = "".join(path.read_text() for path in Path(directory).glob("*.log"))
                    self.assertIn("BACKTEST RUN", logs)

    def test_diagnostics_preserve_signals_orders_fills_accounts_and_settlements(self):
        fixture = native_fixture.VegaNativeSettlementTests()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            module, args = fixture.fixture(root, Decimal("120.13"), option_bar_minutes=5,
                                          execution_timing="next_market", future_hedge=True)
            outputs = []
            profiles = {}
            for mode, enabled in (("event", False), ("event", True),
                                  ("same_init", False), ("same_init", True)):
                run_args = deepcopy(args)
                run_args.profile_engine_run = enabled
                run_args.replay_batch_mode = mode
                run_args.report_dir = root / (mode + ("-profile-on" if enabled else "-profile-off"))
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
                    profiles[mode] = profile
                    self.assertEqual(profile["revision"], "run-phase-timing-v1")
                    self.assertTrue(profile["enabled"])
                    self.assertGreater(profile["counters"]["run_calls"], 0)
                    self.assertGreater(profile["counters"]["data_events"], 0)
                    timings = profile["timings"]
                    children = sum(row["seconds"] for name, row in timings.items() if name != "engine_run_total")
                    self.assertGreaterEqual(timings["engine_run_total"]["seconds"] + 1e-9, children)
            self.assertLess(profiles["same_init"]["counters"]["run_calls"],
                            profiles["event"]["counters"]["run_calls"])
            self.assertEqual(profiles["same_init"]["counters"]["data_events"],
                             profiles["event"]["counters"]["data_events"])
            self.assertEqual(profiles["same_init"]["counters"]["run_calls"],
                             profiles["same_init"]["counters"]["data_timestamp_groups"])
            self.assertEqual(profiles["same_init"]["counters"]["clock_advances"],
                             profiles["same_init"]["counters"]["data_timestamp_groups"])
            frames = [json.loads((output / "feature_frames.json").read_text()) for output in outputs]
            for frame in frames[1:]:
                self.assertEqual(frames[0], frame)  # Every decision/request/deadline and feature revision.
            for name in ("orders.csv", "fills.csv", "positions.csv", "account.csv", "signals.csv",
                         "settlements.csv", "future_settlements.csv"):
                with self.subTest(report=name):
                    frames = [comparable_report(pd.read_csv(output / name, dtype=str, keep_default_na=False))
                              for output in outputs]
                    for frame in frames[1:]:
                        pd.testing.assert_frame_equal(frames[0], frame, obj=name)


if __name__ == "__main__":
    unittest.main()
