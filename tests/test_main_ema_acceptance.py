"""集中离线验收的失败汇总；子进程执行结果受控，不连接外部服务。"""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import run_main_ema_acceptance as acceptance


class AcceptanceTests(unittest.TestCase):
    def run_cases(self, outcomes, *flags):
        with TemporaryDirectory() as directory:
            output = StringIO()
            cases = {key: (f"tests.{key}",) for key in ("first", "second", "third")}
            with patch.object(acceptance, "CASES", cases), patch.object(acceptance, "SOURCES", ()), \
                    patch.object(sys, "argv", ["acceptance", "--report-dir", directory, *flags]), \
                    patch.object(acceptance.subprocess, "run", side_effect=outcomes) as run, \
                    redirect_stdout(output):
                code = acceptance.main()
            report = next(Path(directory).glob("main-ema-acceptance-*"))
            summary = json.loads((report / "summary.json").read_text())
            logs = {p.stem: p.read_text() for p in report.glob("*.log")}
            return code, summary, run.call_count, output.getvalue(), logs

    def completed(self, code):
        return SimpleNamespace(returncode=code, stdout=f"exit={code}\n")

    def test_default_still_stops_at_first_failure(self):
        code, summary, count, _, logs = self.run_cases([self.completed(1)])
        self.assertEqual((code, summary["status"], count), (1, "failed", 1))
        self.assertEqual(set(logs), {"first"})

    def test_keep_going_reports_every_failure_even_when_last_case_passes(self):
        code, summary, count, output, logs = self.run_cases(
            [self.completed(1), self.completed(2), self.completed(0)], "--keep-going")
        self.assertEqual((code, summary["status"], count), (1, "failed", 3))
        self.assertEqual([row["exit_code"] for row in summary["modules"]], [1, 2, 0])
        self.assertEqual(set(logs), {"first", "second", "third"})
        self.assertIn("--only first --only second", output)
        self.assertNotIn("全部选定离线模块通过", output)

    def test_keep_going_timeout_is_failure_and_next_case_runs(self):
        timeout = subprocess.TimeoutExpired(["test"], 180, output=b"partial log")
        code, summary, count, _, logs = self.run_cases(
            [timeout, self.completed(0), self.completed(0)], "--keep-going")
        self.assertEqual((code, summary["status"], count), (1, "failed", 3))
        self.assertEqual(summary["modules"][0]["exit_code"], 124)
        self.assertIn("partial log", logs["first"])

    def test_all_selected_cases_pass(self):
        code, summary, count, _, logs = self.run_cases(
            [self.completed(0)], "--keep-going", "--only", "second")
        self.assertEqual((code, summary["status"], count), (0, "passed", 1))
        self.assertEqual(set(logs), {"second"})


if __name__ == "__main__":
    unittest.main()
