"""Real Feather and actual DolphinDB adapter with a fake SDK; no network or trading engine."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
import io
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from bomber.framework.calendars import CalendarSnapshot
from bomber.framework.calendars.overrides import OVERRIDE_COLUMNS
from bomber.framework.dataprep.sources import (
    DolphinDbReferenceConfig, ReferenceSourceFactory, ReferenceSourceError)
from scripts.integration.reference_data import trading_sessions as readonly
from .test_trading_hours import frame


class FakeSdk:
    def __init__(self, hours, overrides=None):
        self.tables = {"fut_trading_session": hours,
                       "session_overrides": overrides}
        self.scripts = []
        self.closed = False
        self.fail = False
        self.on_read = None

    def connect(self, *args, **kwargs):
        return True

    def run(self, script):
        self.scripts.append(script)
        if self.fail:
            raise RuntimeError("never-export-this-secret")
        table = re.search(r'loadTable\("[^"]+", "([^"]+)"\)', script)[1]
        if self.on_read:
            self.on_read(table)
        return self.tables[table].copy()

    def close(self):
        self.closed = True


class SessionReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "fut_trading_session.feather"
        frame("CU").iloc[::-1].to_feather(self.file)
        self.sdk = FakeSdk(frame("CU").rename(columns={"code": "Code"}))
        self.created = []
        owner = self

        class Factory:
            @staticmethod
            def create_for(purpose, backend, config):
                owner.created.append(backend)
                kwargs = {"session_factory": lambda **_: owner.sdk} if backend == "dolphindb" else {}
                return ReferenceSourceFactory.create_for(purpose, backend, config, **kwargs)

        self.factory = Factory
        self.config = DolphinDbReferenceConfig("fake", 8848, "user", "never-export-this-secret",
            database="dfs://bomber_static", hours_table="fut_trading_session", overrides_table="session_overrides")
        self.dates = CalendarSnapshot("china.sse", "fixture", date(2026, 10, 8), date(2026, 10, 16),
            tuple(date(2026, 10, d) for d in (8, 9, 12, 13, 14, 15, 16)), "declared")
        dates = self.dates

        class Provider:
            def snapshot(self):
                return dates

        self.provider = Provider()
        self.environment = patch.dict("os.environ", {"ROLE_DIR": str(self.root)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def args(self, *extra):
        return readonly.parse_args(["--connect", "--product", "CU", *extra])

    def session_args(self, *extra):
        return self.args("--calendar-id", "china.sse", "--exchange", "SHFE",
                         "--trading-day", "20261012", *extra)

    def probe(self, args):
        return readonly.probe(args, self.config, factory=self.factory, calendar_provider=self.provider)

    def rejected(self, *extra):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.args(*extra)
        self.assertEqual(self.created, [])

    def test_actual_names_role_dir_and_hours_only_do_not_claim_sessions_checked(self):
        args = self.args()
        self.assertEqual(args.database, "dfs://bomber_static")
        self.assertEqual(args.trading_hours_table, "fut_trading_session")
        self.assertEqual(args.trading_hours_file, self.file.resolve())
        result = self.probe(args)
        self.assertEqual((result["status"], result["rows"]), ("passed", 5))
        self.assertEqual(result["sessions"], {"status": "not_requested"})
        self.assertEqual(result["overrides"]["evidence"], "not_checked")
        self.assertEqual(set(result["hours_records"][0]), set(frame().columns))
        self.assertNotIn("Code", result["hours_records"][0])
        self.assertEqual(len(self.sdk.scripts), 2)
        self.assertIn('loadTable("dfs://bomber_static", "fut_trading_session")', self.sdk.scripts[0])
        self.assertIn("string(start_time)", self.sdk.scripts[0])
        self.assertIn('upper(Code) in ["CU"]', self.sdk.scripts[0])
        self.assertTrue(self.sdk.closed)
        self.assertFalse(result["ctp_connected"])
        self.assertEqual(result["orders_submitted"], 0)
        self.assertFalse(Path(str(self.file) + ".manifest.json").exists())
        self.assertNotIn("never-export-this-secret", json.dumps(result, default=str))

    def test_windows_weekend_midnight_and_minutes_match(self):
        result = self.probe(self.session_args("--no-session-overrides"))
        sessions = result["sessions"]
        self.assertEqual(sessions["status"], "passed")
        self.assertEqual(sessions["calendar"]["coverage_start"], "2026-10-08")
        self.assertEqual(sessions["history_minutes"]["count"], 600)
        self.assertEqual(sessions["gap_minutes"]["count"], 465)
        self.assertEqual(sessions["windows"][0]["start"], "2026-10-09T21:00:00+08:00")
        self.assertEqual(sessions["windows"][1]["start"], "2026-10-10T00:00:00+08:00")
        self.assertEqual(sessions["windows"][1]["trading_day"], "20261012")
        self.assertEqual(result["overrides"]["evidence"], "declared_empty")

    def exception_files(self, rows):
        local = pd.DataFrame(rows, columns=OVERRIDE_COLUMNS)
        path = self.root / "session_overrides.feather"
        local.to_feather(path)
        self.sdk.tables["session_overrides"] = local.rename(columns={"code": "Code"})
        return path

    def test_date_cancellation_is_compared_and_midnight_tail_is_removed(self):
        path = self.exception_files([dict(code="CU", override_id="night-cancel", date_basis="NIGHT_START",
            date=date(2026, 10, 9), action="CANCEL", target="NIGHT", segments="[]")])
        result = self.probe(self.session_args("--session-overrides-table", "session_overrides",
                          "--session-overrides-file", str(path)))
        self.assertEqual(result["overrides"]["evidence"], "compared_sources")
        self.assertEqual(result["overrides"]["rows"], 1)
        self.assertEqual(len(result["sessions"]["windows"]), 3)
        self.assertEqual(result["sessions"]["gap_minutes"]["count"], 225)
        self.assertEqual(len(self.sdk.scripts), 4)

    def test_empty_real_exception_table_is_compared_without_inventing_rows(self):
        path = self.exception_files([])
        result = self.probe(self.session_args("--session-overrides-table", "session_overrides",
                          "--session-overrides-file", str(path)))
        self.assertEqual(result["overrides"]["rows"], 0)
        self.assertEqual(result["overrides"]["evidence"], "compared_sources")
        self.assertEqual(len(result["sessions"]["windows"]), 5)

    def test_changed_hours_fail_and_source_is_closed(self):
        self.sdk.tables["fut_trading_session"].loc[0, "start_time"] = "09:01"
        with self.assertRaisesRegex(ValueError, "时段内容不一致"):
            self.probe(self.args())
        self.assertTrue(self.sdk.closed)

    def test_changed_exceptions_fail_even_when_hours_match(self):
        path = self.exception_files([dict(code="CU", override_id="night-cancel", date_basis="NIGHT_START",
            date=date(2026, 10, 9), action="CANCEL", target="NIGHT", segments="[]")])
        self.sdk.tables["session_overrides"].loc[0, "date"] = date(2026, 10, 12)
        with self.assertRaisesRegex(ValueError, "日期例外内容不一致"):
            self.probe(self.session_args("--session-overrides-table", "session_overrides",
                                       "--session-overrides-file", str(path)))
        self.assertTrue(self.sdk.closed)

    def test_database_failure_is_not_a_file_fallback(self):
        self.sdk.fail = True
        with self.assertRaises(ReferenceSourceError) as error:
            self.probe(self.args())
        self.assertNotIn("never-export-this-secret", str(error.exception))
        self.assertEqual(self.created, ["dolphindb"])
        self.assertTrue(self.sdk.closed)

    def test_rule_change_during_comparison_invalidates_result(self):
        def update(table):
            if len(self.sdk.scripts) == 2:
                self.sdk.tables[table].loc[0, "start_time"] = "09:01"
        self.sdk.on_read = update
        with self.assertRaisesRegex(ValueError, "核验期间数据库规则发生变化"):
            self.probe(self.args())
        self.assertTrue(self.sdk.closed)

    def test_file_Code_is_rejected_rather_than_renamed(self):
        frame("CU").rename(columns={"code": "Code"}).to_feather(self.file)
        with self.assertRaisesRegex(ValueError, "字段不符"):
            self.probe(self.args())
        self.assertTrue(self.sdk.closed)

    def test_session_checks_require_known_exceptions_and_paired_sources(self):
        self.rejected("--trading-day", "20261012", "--calendar-id", "china.sse")
        self.rejected("--session-overrides-table", "session_overrides")
        path = self.exception_files([])
        self.rejected("--no-session-overrides", "--session-overrides-file", str(path))

    def test_calendar_coverage_failure_happens_before_database_construction(self):
        with self.assertRaisesRegex(ValueError, "日历覆盖不足"):
            self.probe(self.args("--calendar-id", "china.sse", "--trading-day", "20270104",
                                 "--no-session-overrides"))
        self.assertEqual(self.created, [])

    def test_nontrading_day_failure_happens_before_database_construction(self):
        with self.assertRaisesRegex(ValueError, "必须是日历中的交易日"):
            self.probe(self.args("--trading-day", "20261010", "--calendar-id", "china.sse",
                                 "--no-session-overrides"))
        self.assertEqual(self.created, [])

    def test_main_writes_new_report_and_refuses_to_overwrite_evidence(self):
        report = self.root / "report.json"
        real_probe = readonly.probe
        def injected(args, config):
            self.assertEqual(config.database, "dfs://bomber_static")
            self.assertEqual(config.hours_table, "fut_trading_session")
            return real_probe(args, config, factory=self.factory)
        with patch.dict("os.environ", {"DDB_HOST": "fake", "DDB_USERNAME": "user",
                                     "DDB_PASSWORD": "never-export-this-secret"}), \
                patch.object(readonly, "load_dotenv"), patch.object(readonly, "probe", side_effect=injected), \
                redirect_stdout(io.StringIO()):
            readonly.main(["--connect", "--product", "CU", "--report", str(report)])
        self.assertEqual(json.loads(report.read_text())["status"], "passed")
        original = report.read_bytes()
        self.created.clear()
        self.rejected("--report", str(report))
        self.assertEqual(report.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
