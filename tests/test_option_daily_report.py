"""08交易日报告使用公共追加接口；测试不连接数据库/柜台。"""
from contextlib import redirect_stdout
from importlib import import_module
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch


class OptionDailyReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.Report = import_module("demos.08_option_vega.reports").OptionLiveReport
        self.session = NS(strategy=NS(strategy_id="vega", signals=[],
            option_goals={"MO2611-C-7100": "-1"}, future_goals={}, last_decision={"status": "NO_TARGET_CHANGE"}),
            runner=NS(frame_audit=[], position_manager=NS(position=lambda sid, instrument: -1)),
            references=NS(instrument_ids={"MO2611-C-7100": "MO2611-C-7100.CFFEX"}))

    def report(self, *, stamp=1, day="20261106", mode="recording"):
        report = self.Report(self.root / "MO", prefix=f"option-vega-{mode}-MO",
            trading_day=day, metadata={"strategy_parameters": {"nav": "40000000"}},
            clock_ns=lambda: stamp)
        with redirect_stdout(io.StringIO()):
            report.begin()
        return report

    def write(self, report, *, session=None, events=(), status="passed"):
        with redirect_stdout(io.StringIO()):
            return report.write(status=status, fields={}, session=session, events=events)

    def test_same_trade_day_restarts_append_heterogeneous_signals_and_events(self):
        for stamp in (1, 2):
            report = self.report(stamp=stamp)
            self.session.strategy.signals = [dict(action="NO_VALID_ENTRY", signal_ns=stamp)]
            report.record(self.session, [f"accepted-{stamp}"])
            report.record(self.session, [f"accepted-{stamp}"])
            self.session.strategy.signals.append(dict(action="REBALANCE", targets={"MO": "-1"}))
            self.write(report, session=self.session, events=[f"accepted-{stamp}", f"filled-{stamp}"])
        path = self.root / "MO/option-vega-recording-MO-20261106"
        rows = [json.loads(line) for line in (path / "signals.jsonl").read_text().splitlines()]
        self.assertEqual([row["action"] for row in rows], ["NO_VALID_ENTRY", "REBALANCE"] * 2)
        self.assertEqual([row["run_id"] for row in rows], ["1", "1", "2", "2"])
        self.assertEqual({row["trading_day"] for row in rows}, {"20261106"})
        self.assertEqual(len((path / "events.txt").read_text().splitlines()), 4)
        self.assertEqual(len((path / "sessions.jsonl").read_text().splitlines()), 2)
        self.assertEqual(json.loads((path / "summary.json").read_text())["run_id"], "2")

    def test_snapshot_and_effective_parameters_are_separate_from_recovery_state(self):
        state = self.root / "existing-state.json"
        state.write_text("original checkpoint")
        report = self.report()
        self.session.runner.frame_audit = [{"valuation_ns": 1, "status": "CONSUMED"}]
        self.write(report, session=self.session)
        snapshot = json.loads((report.path / "strategy_state.json").read_text())
        self.assertEqual(snapshot["positions"], {"MO2611-C-7100": "-1"})
        self.assertEqual(snapshot["scope"], "report_snapshot_not_recovery_checkpoint")
        self.assertEqual(json.loads((report.path / "strategy_parameters.json").read_text())["nav"], "40000000")
        self.assertEqual(json.loads((report.path / "feature_frames.json").read_text())["scope"], "recent_frames")
        self.assertEqual(state.read_text(), "original checkpoint")

    def test_next_trade_day_and_modes_use_separate_directories(self):
        reports = (self.report(), self.report(stamp=2), self.report(day="20261109"), self.report(mode="model"))
        self.assertEqual(reports[0].path, reports[1].path)
        self.assertEqual(len({report.path for report in reports}), 3)

    def test_failed_restart_preserves_previous_signals_and_position_snapshot(self):
        first = self.report()
        self.session.strategy.signals.append({"action": "REBALANCE"})
        self.write(first, session=self.session, events=["old fill"])
        snapshot = (first.path / "strategy_state.json").read_bytes()
        signals = (first.path / "signals.jsonl").read_bytes()
        failed = self.report(stamp=2)
        self.write(failed, status="failed")
        self.assertEqual((failed.path / "signals.jsonl").read_bytes(), signals)
        self.assertEqual((failed.path / "strategy_state.json").read_bytes(), snapshot)
        self.assertEqual((failed.path / "events.txt").read_text(), "old fill\n")

    def test_live_progress_records_signals_and_late_events_without_database_calls(self):
        entry = import_module("demos.08_option_vega.run_live")
        config = Path(__file__).resolve().parents[1] / "demos/08_option_vega/config/strategy.yaml"
        args = entry.parse_args(["--config-file", str(config), "--config-id", "live_recording",
            "--expected-trading-day", "20261106", "--report-dir", str(self.root)])
        runtime = entry.build_runtime(args)
        with redirect_stdout(io.StringIO()):
            runtime.report.begin()
        self.session.strategy.signals.append({"action": "NO_VALID_ENTRY"})
        events = ["accepted"]
        with patch.object(runtime.controller, "events", side_effect=lambda session: tuple(events)):
            runtime.progress(self.session)
            events.append("late fill")
            runtime.progress(self.session)
        self.assertEqual((runtime.report.path / "events.txt").read_text(), "accepted\nlate fill\n")
        self.assertEqual(len((runtime.report.path / "signals.jsonl").read_text().splitlines()), 1)
        self.assertEqual(runtime.report.path, self.root / "MO/option-vega-recording-MO-20261106")
