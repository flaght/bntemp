"""Source parity, midnight attribution and recovery bucket contracts, without network/engine."""
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import runpy
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

from bomber.framework.calendars import (CalendarSnapshot, SessionBinding, SessionCalendar,
    SessionOverride, SessionSegment, parse_trading_hours, NativeCalendarProvider, SessionRulesCheckpoint)
from bomber.framework.calendars.sessions import datetime_ns
from bomber.framework.dataprep.sources import (ReferenceSourceFactory, ReferenceDataset,
    ReferenceQuery, DolphinDbReferenceConfig, DataSourcePurpose)
from bomber.framework.dataprep.trading_hours import read_trading_hours, load_session_config

ZONE = ZoneInfo("Asia/Shanghai")
MINUTE = 60_000_000_000


def moment(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZONE)


def ns(value):
    return datetime_ns(moment(value))


def frame(code="ZN"):
    return pd.DataFrame([
        (code, "day_1", "TRADING_DAY", 0, "09:00:00.000", 0, "10:15:00.000"),
        (code, "day_2", "TRADING_DAY", 0, "10:30:00.000", 0, "11:30:00.000"),
        (code, "day_3", "TRADING_DAY", 0, "13:30:00.000", 0, "15:00:00.000"),
        (code, "night_1", "NIGHT_START", 0, "21:00:00.000", 1, "00:00:00.000"),
        (code, "night_2", "NIGHT_START", 1, "00:00:00.000", 1, "01:00:00.000"),
    ], columns=("code", "segment_id", "date_anchor", "start_day_offset", "start_time",
                "end_day_offset", "end_time"))


class FakeSession:
    def __init__(self, table):
        self.table, self.scripts, self.closed = table, [], False
    def connect(self, *args, **kwargs): return True
    def close(self): self.closed = True
    def run(self, script):
        self.scripts.append(script)
        return self.table.copy()


class TradingHoursTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calendar = CalendarSnapshot("test.exchange", "dates-v1", date(2026, 10, 8), date(2026, 10, 16),
            tuple(date(2026, 10, d) for d in (8, 9, 12, 13, 14, 15, 16)), "declared")
        self.hours = parse_trading_hours(frame(), source="test")
        self.binding = SessionBinding("ZN", "ZN", self.calendar.calendar_id, "Asia/Shanghai", "SHFE")

    def service(self, **kwargs):
        return SessionCalendar(self.hours, self.calendar, self.binding, **kwargs)

    def test_dolphindb_and_real_feather_share_content_and_minutes_without_manifest(self):
        path = self.root / "trading_hours.feather"
        file_rows = frame().iloc[::-1].copy()
        file_rows["start_time"] = [time.fromisoformat(v) for v in file_rows.start_time]
        file_rows["end_time"] = [time.fromisoformat(v) for v in file_rows.end_time]
        file_rows.to_feather(path)
        sdk = FakeSession(frame().rename(columns={"code": "Code"}))
        config = DolphinDbReferenceConfig("test", 8848, "test", "test", hours_table="hours_test")
        with ReferenceSourceFactory.create("dolphindb", config, session_factory=lambda **_: sdk) as source:
            live = read_trading_hours(source, ("ZN",))
        with ReferenceSourceFactory.create("file", {ReferenceDataset.TRADING_HOURS: path}) as source:
            offline = read_trading_hours(source, ("ZN",))
        self.assertFalse(Path(str(path)+".manifest.json").exists())
        self.assertEqual(live.fingerprint, offline.fingerprint)
        self.assertTrue(sdk.closed)
        self.assertIn('"hours_test"', sdk.scripts[0])
        self.assertIn('upper(Code)', sdk.scripts[0])
        self.assertNotIn("date >=", sdk.scripts[0])
        a = SessionCalendar(live, self.calendar, self.binding)
        b = SessionCalendar(offline, self.calendar, self.binding)
        self.assertEqual(a.last_minutes(ns("2026-10-13T15:00"), 600),
                         b.last_minutes(ns("2026-10-13T15:00"), 600))
        self.assertEqual(a.provenance["sha256"], b.provenance["sha256"])

    def test_only_dolphindb_accepts_Code_and_alias_conflicts_fail(self):
        path = self.root / "bad.feather"
        frame().rename(columns={"code": "Code"}).to_feather(path)
        with ReferenceSourceFactory.create("file", {ReferenceDataset.TRADING_HOURS: path}) as source:
            with self.assertRaises(ValueError): read_trading_hours(source, ("ZN",))
        sdk = FakeSession(frame().assign(Code="ZC"))
        with ReferenceSourceFactory.create("dolphindb", DolphinDbReferenceConfig("t", 8848, "u", "p"),
                                           session_factory=lambda **_: sdk) as source:
            with self.assertRaisesRegex(ValueError, "冲突"):
                read_trading_hours(source, ("ZN",))
        sdk = FakeSession(frame().assign(Code="zn"))
        with ReferenceSourceFactory.create("dolphindb", DolphinDbReferenceConfig("t", 8848, "u", "p"),
                                           session_factory=lambda **_: sdk) as source:
            self.assertEqual(read_trading_hours(source, ("ZN",)).fingerprint, self.hours.fingerprint)

    def test_clock_types_hash_the_same_but_epoch_numbers_and_timestamps_fail(self):
        converted = frame()
        converted["start_time"] = [np.timedelta64(int(v[:2])*60+int(v[3:5]), "m") for v in converted.start_time]
        converted["end_time"] = [pd.Timedelta(hours=int(v[:2]), minutes=int(v[3:5])) for v in converted.end_time]
        self.assertEqual(parse_trading_hours(converted, source="duration").fingerprint, self.hours.fingerprint)
        for value in (9, float("nan"), datetime(2026, 10, 13, 9), "09:00:01", "24:00", True,
                      "09:00:00.000000001"):
            with self.subTest(value=value):
                invalid = frame().astype(object)
                invalid.loc[0, "start_time"] = value
                with self.assertRaises(ValueError): parse_trading_hours(invalid, source="bad")
        precise = frame().copy()
        precise["start_time"] = precise.start_time.str.replace(".000", ".000000000", regex=False)
        self.assertEqual(parse_trading_hours(precise, source="nanotime").fingerprint, self.hours.fingerprint)

    def test_midnight_segments_belong_to_monday_and_have_no_duplicate_minutes(self):
        service = self.service()
        self.assertEqual(service.window(moment("2026-10-09T21:00")).trading_day, "20261012")
        midnight = service.window(moment("2026-10-10T00:00"))
        self.assertEqual((midnight.trading_day, midnight.segment_id), ("20261012", "night_2"))
        self.assertIsNone(service.window(moment("2026-10-10T01:00")))
        self.assertIsNone(service.window(moment("2026-10-11T21:00")))
        intervals = service.windows(date(2026, 10, 12))
        self.assertEqual(len(intervals), 5)
        self.assertEqual(intervals[0].start, moment("2026-10-09T21:00"))
        stamps = service.missing_minutes(ns("2026-10-09T21:00")-1, ns("2026-10-10T01:00"))
        self.assertEqual(len(stamps), 240)
        self.assertEqual(len(set(stamps)), 240)
        self.assertEqual(stamps[-1], ns("2026-10-10T01:00")-1)
        self.assertEqual(service.last_minutes(ns("2026-10-10T00:00:30"), 1),
                         (ns("2026-10-10T00:00")-1,))

    def test_incremental_recovery_excludes_pause_and_unfinished_minute(self):
        service = self.service()
        self.assertEqual(service.missing_minutes(ns("2026-10-13T10:15")-1, ns("2026-10-13T10:31")),
                         (ns("2026-10-13T10:31")-1,))
        self.assertEqual(service.missing_minutes(ns("2026-10-13T10:15")-1, ns("2026-10-13T10:30:59")), ())
        self.assertEqual(len(service.last_minutes(ns("2026-10-13T15:00"), 600)), 600)
        self.assertEqual(service.last_minutes(ns("2026-10-13T15:00"), 0), ())

    def test_cancel_whole_night_by_anchor_or_owner_leaves_no_midnight_tail(self):
        for basis, day in (("NIGHT_START", date(2026, 10, 9)), ("TRADING_DAY", date(2026, 10, 12))):
            with self.subTest(basis=basis):
                service = self.service(overrides=(SessionOverride(basis, day, "CANCEL", "NIGHT"),))
                self.assertIsNone(service.window(moment("2026-10-09T21:00")))
                self.assertIsNone(service.window(moment("2026-10-10T00:30")))
                self.assertEqual(len(service.windows(date(2026, 10, 12))), 3)

    def test_date_replacement_and_conflicts(self):
        day = date(2026, 10, 13)
        replacement = SessionSegment("special", "TRADING_DAY", 0, "10:00", 0, "10:02")
        service = self.service(overrides=(SessionOverride("TRADING_DAY", day, "REPLACE", "ALL", (replacement,)),))
        self.assertEqual(len(service.windows(day)), 1)
        self.assertEqual(len(service.missing_minutes(ns("2026-10-13T00:00"), ns("2026-10-13T15:00"))), 2)
        rules = (SessionOverride("NIGHT_START", date(2026, 10, 9), "CANCEL", "NIGHT"),
                 SessionOverride("TRADING_DAY", date(2026, 10, 12), "CANCEL", "NIGHT"))
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.service(overrides=rules).windows(date(2026, 10, 12))

    def test_new_product_option_binding_only_changes_data(self):
        for product, kind in (("NEW", "FUTURE"), ("MO_SERIES", "OPTION")):
            binding = SessionBinding(product, "ZN", self.calendar.calendar_id, "Asia/Shanghai", asset_type=kind)
            service = SessionCalendar(self.hours, self.calendar, binding)
            self.assertEqual(len(service.windows(date(2026, 10, 13))), 5)
        with self.assertRaises(ValueError):
            SessionCalendar(self.hours, self.calendar, SessionBinding("X", "MISSING", self.calendar.calendar_id, "UTC"))

    def test_missing_coverage_identity_rules_and_duplicates_fail(self):
        service = self.service()
        with self.assertRaises(RuntimeError): service.windows(date(2026, 10, 8))
        with self.assertRaises(RuntimeError): service.window(moment("2026-10-17T09:00"))
        with self.assertRaises(RuntimeError): service.window(moment("2026-10-16T21:00"))
        with self.assertRaises(RuntimeError): service.last_minutes(ns("2026-10-08T09:00"), 1)
        with self.assertRaises(ValueError): service.window(datetime(2026, 10, 13, 9))
        with self.assertRaises(ValueError):
            SessionCalendar(self.hours, self.calendar, SessionBinding("ZN", "ZN", "OTHER", "UTC"))
        invalids = (pd.concat([frame(), frame().iloc[:1]]), frame().drop(columns="code"),
                    frame().assign(start_day_offset=0.5), frame().assign(date_anchor="GUESS"))
        for invalid in invalids:
            with self.assertRaises(ValueError): parse_trading_hours(invalid, source="bad")

    def test_snapshot_changes_and_does_not_follow_mutable_source_frame(self):
        raw = frame()
        snapshot = parse_trading_hours(raw, source="test")
        raw.loc[0, "start_time"] = "09:01"
        self.assertEqual(snapshot.fingerprint, self.hours.fingerprint)
        self.assertNotEqual(parse_trading_hours(raw, source="test").fingerprint, snapshot.fingerprint)

    def test_static_hours_query_rejects_dates_and_bad_source_policy_before_read(self):
        sdk = FakeSession(frame())
        with ReferenceSourceFactory.create("dolphindb", DolphinDbReferenceConfig("t", 8848, "u", "p"),
                                           session_factory=lambda **_: sdk) as source:
            with self.assertRaises(ValueError):
                source.trading_hours(ReferenceQuery(products=("ZN",), start_date=date(2026, 1, 1)))
        self.assertEqual(sdk.scripts, [])
        with self.assertRaises(ValueError):
            ReferenceSourceFactory.create_for(DataSourcePurpose.LIVE_REFERENCE, "file", {})

    def test_v2_config_feather_native_calendar_and_explicit_empty_exceptions(self):
        path = self.root / "trading_hours.feather"
        frame().to_feather(path)
        config = {"schema_version": 2, "product": "ZN", "calendar": {
            "backend": "native", "calendar_id": "china.sse", "start": "2026-10-08", "end": "2026-10-16"},
            "binding": {"hours_code": "ZN", "timezone": "Asia/Shanghai", "exchange": "SHFE"},
            "hours": {"backend": "file", "path": path.name}, "overrides": []}
        service = load_session_config(config, self.root/"assembly.json", product="ZN",
                                      purpose=DataSourcePurpose.OFFLINE_REFERENCE)
        self.assertEqual(len(service.windows(date(2026, 10, 13))), 5)
        with self.assertRaises(ValueError):
            load_session_config(config, self.root/"assembly.json", product="OTHER")
        with self.assertRaises(ValueError):
            load_session_config(config, self.root/"assembly.json", purpose=DataSourcePurpose.LIVE_REFERENCE)

    def test_runtime_v2_loader_preflight_is_lazy_and_legacy_uses_same_service(self):
        loader = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                  "bomber/framework/trader/runtime/trading_sessions.py"))
        config = {"schema_version": 2, "product": "ZN", "calendar": {
            "backend": "native", "calendar_id": "china.sse", "start": "2026-10-08", "end": "2026-10-16"},
            "binding": {"hours_code": "ZN", "timezone": "Asia/Shanghai", "exchange": "SHFE"},
            "hours": {"backend": "dolphindb", "database": "dfs://test", "table": "hours_test"}, "overrides": []}
        path = self.root / "session.json"
        path.write_text(json.dumps(config))
        with patch("bomber.framework.dataprep.sources.dolphindb._session_factory") as connect:
            loader["validate_sessions"](path, "ZN", purpose=DataSourcePurpose.LIVE_REFERENCE)
            connect.assert_not_called()
        frame().to_feather(self.root / "trading_hours.feather")
        config["hours"] = {"backend": "file", "path": "trading_hours.feather"}
        path.write_text(json.dumps(config))
        wrapper = loader["load_sessions"](path, "ZN", purpose=DataSourcePurpose.OFFLINE_REFERENCE)
        self.assertIsInstance(wrapper._service, SessionCalendar)
        self.assertEqual(len(wrapper.windows(date(2026, 10, 13))), 5)
        # A compatibility unit test must own its versioned input, rather than
        # depend on a deployment's independently maintained live calendar file.
        legacy_path = self.root / "legacy_sessions.json"
        legacy_path.write_text(json.dumps({
            "schema_version": 1, "product": "CU", "timezone": "Asia/Shanghai",
            "start": "2026-09-30", "end": "2026-10-13",
            "trading_days": ["2026-09-30", "2026-10-08", "2026-10-09",
                             "2026-10-12", "2026-10-13"],
            "session_segments": [
                {"start": "09:00", "end": "10:15", "trading_day_rule": "same_day"},
                {"start": "21:00", "end": "01:00", "end_day_offset": 1,
                 "trading_day_rule": "next_trading_day"}],
            "no_night": ["2026-09-30"],
        }))
        legacy = loader["load_sessions"](legacy_path, "CU")
        self.assertIsInstance(legacy._service, SessionCalendar)
        self.assertEqual(legacy.window(moment("2026-10-10T00:30")).trading_day, "20261012")
        self.assertIsNone(legacy.window(moment("2026-09-30T21:00")))

    def test_history_probe_delegates_and_rules_checkpoint_detects_content_change(self):
        from bomber.framework.dataprep.history import ImDayWindow
        window = ImDayWindow(date(2026, 10, 13))
        self.assertEqual(len(window.stamps), 240)
        self.assertEqual(window.last_minutes(window.end_ns, 240), window.stamps)
        self.assertIsInstance(window._service, SessionCalendar)
        checkpoint = SessionRulesCheckpoint(self.service())
        restored = SessionRulesCheckpoint(self.service())
        restored.restore_state(checkpoint.snapshot_state())
        modified = frame()
        modified.loc[0, "start_time"] = "09:01"
        different = SessionCalendar(parse_trading_hours(modified, source="changed"), self.calendar, self.binding)
        with self.assertRaisesRegex(ValueError, "变化"):
            SessionRulesCheckpoint(different).restore_state(checkpoint.snapshot_state())

    def test_replacement_overlap_and_opposite_group_rejected(self):
        with self.assertRaises(ValueError):
            SessionOverride("NIGHT_START", date(2026, 10, 9), "REPLACE", "NIGHT",
                            (SessionSegment("day", "TRADING_DAY", 0, "09:00", 0, "10:00"),))
        overlapping = SessionSegment("extra", "TRADING_DAY", 0, "09:30", 0, "11:00")
        service = self.service(overrides=(SessionOverride("TRADING_DAY", date(2026, 10, 13), "REPLACE", "DAY",
            (overlapping, SessionSegment("other", "TRADING_DAY", 0, "10:00", 0, "11:30"))),))
        with self.assertRaisesRegex(ValueError, "重叠"):
            service.windows(date(2026, 10, 13))

    def test_exception_can_add_night_to_day_only_template(self):
        day_hours = parse_trading_hours(frame().loc[lambda f: f.date_anchor.eq("TRADING_DAY")], source="day-only")
        override = SessionOverride("TRADING_DAY", date(2026, 10, 13), "REPLACE", "NIGHT",
            (SessionSegment("special_night", "NIGHT_START", 0, "21:00", 0, "23:00"),))
        service = SessionCalendar(day_hours, self.calendar, self.binding, overrides=(override,))
        windows = service.windows(date(2026, 10, 13))
        self.assertEqual(len(windows), 4)
        self.assertEqual(windows[0].start, moment("2026-10-12T21:00"))
        self.assertEqual(service.window(moment("2026-10-12T22:00")).trading_day, "20261013")


if __name__ == "__main__": unittest.main()
