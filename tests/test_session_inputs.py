"""Direct Provider/default-calendar assembly and special-date source parity; no network or engine."""
import argparse
import ast
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from bomber.framework.calendars import (CalendarSnapshot, NativeCalendarProvider,
    ExportedCalendarProvider, export_calendar)
from bomber.framework.calendars.overrides import OVERRIDE_COLUMNS, normalize_overrides
from bomber.framework.calendars.trading import RULES_START, RULES_END
from bomber.framework.dataprep.session_inputs import (SessionInputs, load_session_inputs,
    add_session_arguments, validate_session_arguments, validate_session_inputs, build_session_calendar)
from bomber.framework.dataprep.sources import (ReferenceDataset, ReferenceSourceFactory, DataSourcePurpose)
from tests.test_trading_hours import frame, moment, ns
from tests.session_fixtures import declared_day_sessions


def overrides_frame(code="ZN"):
    return pd.DataFrame([(code, "holiday-night", "NIGHT_START", date(2026, 9, 30),
                          "CANCEL", "NIGHT", None)], columns=OVERRIDE_COLUMNS)


class RuleSession:
    def __init__(self):
        self.scripts, self.closed = [], False
        self.tables = {"hours": frame().rename(columns={"code": "Code"}),
                       "exceptions": overrides_frame().rename(columns={"code": "Code"})}
    def connect(self, *args, **kwargs): return True
    def close(self): self.closed = True
    def run(self, script):
        self.scripts.append(script)
        table = re.search(r'loadTable\("[^\"]+", "([^\"]+)"\)', script)[1]
        return self.tables[table].copy()


class SessionInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hours = self.root / "trading_hours.feather"
        self.exceptions = self.root / "session_overrides.feather"
        frame().to_feather(self.hours)
        overrides_frame().to_feather(self.exceptions)
        self.inputs = SessionInputs("ZN", "china.sse", backend="file", hours_file=self.hours,
                                    overrides_file=self.exceptions, exchange="SHFE")
        self.sdk = RuleSession()

    def test_file_and_dolphindb_use_same_dates_hours_and_exceptions_without_calendar_json(self):
        offline = load_session_inputs(self.inputs)
        live_input = replace(self.inputs, backend="dolphindb", hours_file=None, overrides_file=None,
                             database="dfs://rules", hours_table="hours", overrides_table="exceptions")
        with patch.dict("os.environ", {"DDB_HOST": "test", "DDB_USERNAME": "user", "DDB_PASSWORD": "secret"}), \
                patch("bomber.framework.dataprep.sources.dolphindb._session_factory", return_value=self.sdk):
            live = load_session_inputs(live_input)
        self.assertEqual((offline.start, offline.end), (RULES_START, RULES_END))
        self.assertEqual(offline.provenance["sha256"], live.provenance["sha256"])
        self.assertEqual(offline.last_minutes(ns("2026-10-13T15:00"), 600),
                         live.last_minutes(ns("2026-10-13T15:00"), 600))
        self.assertIsNone(live.window(moment("2026-09-30T21:00")))
        self.assertIsNone(live.window(moment("2026-10-01T00:30")))
        self.assertEqual(live.window(moment("2026-10-10T00:30")).trading_day, "20261012")
        self.assertTrue(self.sdk.closed)
        self.assertEqual(len(self.sdk.scripts), 2)
        self.assertEqual(list(self.root.glob("*.json")), [])

    def test_empty_exception_table_is_explicit_empty_and_query_failure_is_not_empty(self):
        empty = pd.DataFrame(columns=OVERRIDE_COLUMNS)
        empty.to_feather(self.exceptions)
        calendar = load_session_inputs(self.inputs)
        self.assertEqual(calendar.overrides, ())
        self.assertIsNotNone(calendar.window(moment("2026-09-30T21:00")))
        with self.assertRaises(ValueError): load_session_inputs(replace(self.inputs, overrides_file=None))

    def test_explicit_no_overrides_and_option_binding_need_no_extra_file(self):
        inputs = replace(self.inputs, product="OPTION_SERIES", hours_code="ZN", asset_type="OPTION",
                         overrides_file=None, no_overrides=True)
        calendar = load_session_inputs(inputs)
        self.assertEqual(calendar.product, "OPTION_SERIES")
        self.assertEqual(len(calendar.windows(date(2026, 10, 13))), 5)
        with self.assertRaises(ValueError): load_session_inputs(replace(inputs, overrides_file=self.exceptions))

    def test_exported_calendar_declares_full_coverage_without_requested_dates(self):
        snapshot = NativeCalendarProvider("china.sse").snapshot(date(2026, 10, 8), date(2026, 10, 16))
        path = self.root / "calendar.csv"
        export_calendar(snapshot, path)
        provider = ExportedCalendarProvider(path)
        restored = provider.snapshot()
        self.assertEqual(restored.fingerprint, snapshot.fingerprint)
        calendar = load_session_inputs(self.inputs, calendar_provider=provider)
        self.assertEqual((calendar.start, calendar.end), (snapshot.start, snapshot.end))
        self.assertEqual(calendar.overrides, ())  # September exception is outside this calendar scope.

    def test_cli_preflight_uses_no_json_no_credentials_and_does_not_connect(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--product")
        add_session_arguments(parser)
        args = parser.parse_args(["--product", "ZN", "--calendar-id", "china.sse", "--no-session-overrides"])
        self.assertIsNone(args.trading_calendar)
        with patch("bomber.framework.dataprep.sources.dolphindb._session_factory") as connect, \
                patch("bomber.framework.dataprep.sources.dolphindb.DolphinDbReferenceConfig.from_env") as creds:
            validate_session_arguments(args, purpose=DataSourcePurpose.LIVE_REFERENCE)
            connect.assert_not_called()
            creds.assert_not_called()
        with self.assertRaises(ValueError):
            validate_session_arguments(parser.parse_args(["--product", "ZN"]))

    def test_policy_and_unsafe_names_fail_before_any_source_is_created(self):
        for change in ({"calendar_id": "china.shfe"}, {"no_overrides": True},
                       {"backend": "mongodb"}, {"hours_table": "x;dropTable"}):
            with self.subTest(change=change), patch.object(ReferenceSourceFactory, "create_for") as create:
                with self.assertRaises(ValueError): load_session_inputs(replace(self.inputs, **change))
                create.assert_not_called()
        with self.assertRaises(ValueError):
            validate_session_inputs(self.inputs, purpose=DataSourcePurpose.LIVE_REFERENCE)

    def test_replace_records_and_alias_mapping_have_same_semantics(self):
        parts = [{"segment_id": "special", "date_anchor": "TRADING_DAY", "start_day_offset": 0,
                  "start_time": "10:00", "end_day_offset": 0, "end_time": "10:02"}]
        raw = pd.DataFrame([("ZN", "special-day", "TRADING_DAY", "2026-10-13", "REPLACE", "ALL",
                             json.dumps(parts))], columns=OVERRIDE_COLUMNS)
        raw.to_feather(self.exceptions)
        offline = load_session_inputs(self.inputs)
        live_input = replace(self.inputs, backend="dolphindb", hours_file=None, overrides_file=None,
                             hours_table="hours", overrides_table="exceptions")
        self.sdk.tables["exceptions"] = raw.rename(columns={"code": "Code"})
        with patch.dict("os.environ", {"DDB_HOST": "test", "DDB_USERNAME": "u", "DDB_PASSWORD": "p"}), \
                patch("bomber.framework.dataprep.sources.dolphindb._session_factory", return_value=self.sdk):
            live = load_session_inputs(live_input)
        self.assertEqual(live.provenance["sha256"], offline.provenance["sha256"])
        self.assertEqual(len(live.windows(date(2026, 10, 13))), 1)
        self.assertEqual(live.missing_minutes(ns("2026-10-13T00:00"), ns("2026-10-13T15:00")),
                         (ns("2026-10-13T10:01")-1, ns("2026-10-13T10:02")-1))

    def test_database_error_releases_connection_and_never_infers_empty_exceptions(self):
        del self.sdk.tables["exceptions"]
        inputs = replace(self.inputs, backend="dolphindb", hours_file=None, overrides_file=None,
                         hours_table="hours", overrides_table="exceptions")
        with patch.dict("os.environ", {"DDB_HOST": "test", "DDB_USERNAME": "u", "DDB_PASSWORD": "p"}), \
                patch("bomber.framework.dataprep.sources.dolphindb._session_factory", return_value=self.sdk):
            with self.assertRaises(RuntimeError): load_session_inputs(inputs)
        self.assertTrue(self.sdk.closed)

    def test_override_schema_duplicate_records_and_unknown_dates_are_rejected(self):
        for raw in (pd.concat([overrides_frame(), overrides_frame()]),
                    overrides_frame().rename(columns={"code": "Code"}),
                    overrides_frame().assign(date="2026-09-30T21:00"),
                    overrides_frame().assign(action="GUESS")):
            with self.subTest(columns=list(raw)):
                with self.assertRaises(ValueError): normalize_overrides(raw)

    def test_direct_builder_uses_provider_coverage_and_requires_known_exceptions(self):
        from bomber.framework.calendars import SessionBinding
        with ReferenceSourceFactory.create("file", {ReferenceDataset.TRADING_HOURS: self.hours}) as source:
            calendar = build_session_calendar(source, SessionBinding("ZN", "ZN", "china.sse", "Asia/Shanghai"),
                                              overrides=())
            self.assertEqual((calendar.start, calendar.end), (RULES_START, RULES_END))
            with self.assertRaises(ValueError):
                build_session_calendar(source, calendar.binding, overrides=None)

    def test_memory_fixture_matches_the_buffered_history_window(self):
        calendar = declared_day_sessions("RB", date(2026, 9, 22))
        before = ns("2026-09-22T09:02")
        self.assertEqual(calendar.last_minutes(before, 2), (before-60_000_000_000-1, before-1))

    def test_actual_runtime_helper_defaults_to_provider_and_checks_injected_identity(self):
        # Isolate the actual assembly helper from the eager engine imports in run_live.
        path = Path(__file__).resolve().parents[1] / "demos/01_main_ema/run_live.py"
        tree = ast.parse(path.read_text())
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"runtime_sessions", "session_source_purpose"}]
        namespace = {}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
        args = SimpleNamespace(product="ZN", calendar_id="china.sse", trading_calendar=None,
                               trading_hours_table="hours", session_overrides_table="exceptions")
        with patch.dict("os.environ", {"DDB_HOST": "test", "DDB_USERNAME": "u", "DDB_PASSWORD": "p"}), \
                patch("bomber.framework.dataprep.sources.dolphindb._session_factory", return_value=self.sdk):
            calendar = namespace["runtime_sessions"](args, exchange="SHFE")
        self.assertTrue(calendar.requires_rules_checkpoint)
        self.assertEqual(calendar.binding.exchange, "SHFE")
        args._session_calendar = calendar
        with patch.object(ReferenceSourceFactory, "create_for") as create:
            self.assertIs(namespace["runtime_sessions"](args, exchange="SHFE"), calendar)
            create.assert_not_called()
            with self.assertRaises(ValueError): namespace["runtime_sessions"](args, exchange="DCE")
            args.product = "RB"
            with self.assertRaises(ValueError): namespace["runtime_sessions"](args)


if __name__ == "__main__": unittest.main(verbosity=2)
