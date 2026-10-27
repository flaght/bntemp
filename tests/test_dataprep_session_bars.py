"""回放入口的交易时段过滤；使用声明式日历和真实 Feather 读写。"""
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import pandas as pd

from bomber.framework.calendars import (CalendarSnapshot, SessionBinding, SessionCalendar,
    SessionOverride, SessionSegment, TradingHoursSnapshot)
from bomber.framework.dataprep import BarReadSpec, InputError, InputSession
from bomber.framework.dataprep.bars import PreparedFrameReader, filter_session_bars, read_bar_frame
from bomber.framework.market.futures import futures_bar_values

DAY = date(2026, 1, 5)
ZONE = "Asia/Shanghai"


def sessions(*, night=False, overrides=()):
    dates = CalendarSnapshot("test.sessions", "fixture-v1", date(2026, 1, 1), date(2026, 1, 7),
        (date(2026, 1, 2), DAY, date(2026, 1, 6), date(2026, 1, 7)), "declared")
    parts = [SessionSegment(name, "TRADING_DAY", 0, begin, 0, end)
        for name, begin, end in (("day1", "09:00", "10:15"),
            ("day2", "10:30", "11:30"), ("day3", "13:30", "15:00"))]
    if night:
        parts.append(SessionSegment("night", "NIGHT_START", 0, "21:00", 1, "02:30"))
    hours = TradingHoursSnapshot(tuple(("P", part) for part in parts), "declared-test")
    return SessionCalendar(hours, dates, SessionBinding("P", "P", dates.calendar_id, ZONE),
        overrides=overrides)


class SessionBarFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "p2605_20260105.feather"

    def prepared(self, labels, **spec):
        pd.DataFrame([dict(datetime=label if " " in label else f"{DAY} {label}",
            open=100, high=102, low=99, close=101, volume=2, value=2020, openint=20)
            for label in labels]).to_feather(self.path)
        return read_bar_frame(self.path, BarReadSpec(**spec))

    def labels(self, result):
        return result.frame.source_timestamp.dt.strftime("%H:%M").tolist()

    def test_end_labels_keep_closing_bar_and_remove_breaks_and_1517(self):
        original = self.prepared(["09:00", "09:01", "10:15", "10:16", "10:30",
            "10:31", "11:30", "11:31", "13:30", "13:31", "15:00", "15:17"])
        before = original.frame.copy(deep=True)
        result = filter_session_bars(original, sessions())
        self.assertEqual(self.labels(result), ["09:01", "10:15", "10:31", "11:30", "13:31", "15:00"])
        pd.testing.assert_frame_equal(original.frame, before)
        self.assertEqual(result.issues[-1].code, "OUTSIDE_TRADING_SESSION")
        rows = [row for _, row in PreparedFrameReader(result.frame).read(self.path)]
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["source_timestamp"].strftime("%H:%M") != "15:17" for row in rows))

    def test_start_labels_shift_once_and_preserve_original_model_time(self):
        original = self.prepared(["09:00", "10:14", "10:15", "10:30", "11:29", "11:30",
            "14:59", "15:00", "15:17"], timestamp_label="start")
        result = filter_session_bars(original, sessions())
        self.assertEqual(self.labels(result), ["09:00", "10:14", "10:30", "11:29", "14:59"])
        self.assertEqual(result.frame.datetime.dt.strftime("%H:%M").tolist(),
                         ["09:01", "10:15", "10:31", "11:30", "15:00"])
        self.assertTrue(result.frame.event_ns.eq(result.frame.available_ns).all())

    def test_friday_night_and_saturday_after_midnight_belong_to_monday(self):
        original = self.prepared(["2026-01-02 21:00", "2026-01-02 21:01", "2026-01-03 00:00",
            "2026-01-03 02:30", "2026-01-03 02:31", "2026-01-05 15:00"])
        result = filter_session_bars(original, sessions(night=True))
        self.assertEqual(self.labels(result), ["21:01", "00:00", "02:30", "15:00"])
        self.assertTrue(result.frame.trading_day.eq(DAY).all())

    def test_cancelled_night_is_excluded_using_existing_override(self):
        original = self.prepared(["2026-01-02 21:01", "2026-01-03 02:30", "2026-01-05 09:01"])
        override = SessionOverride("NIGHT_START", date(2026, 1, 2), "CANCEL", "NIGHT")
        result = filter_session_bars(original, sessions(night=True, overrides=(override,)))
        self.assertEqual(self.labels(result), ["09:01"])

    def test_late_availability_does_not_remove_legal_closing_bar(self):
        self.prepared(["15:00"])
        raw = pd.read_feather(self.path)
        raw["published"] = f"{DAY} 15:17"
        raw.to_feather(self.path)
        original = read_bar_frame(self.path, BarReadSpec(available_column="published"))
        result = filter_session_bars(original, sessions())
        self.assertEqual(self.labels(result), ["15:00"])
        self.assertEqual(result.frame.available_datetime.iloc[0].strftime("%H:%M"), "15:17")

    def test_input_reports_record_counts_and_calendar_and_raw_cache_is_unchanged(self):
        with InputSession() as session:
            original = self.prepared(["09:01", "15:00", "15:17"])
            calendar = sessions()
            filter_session_bars(original, calendar)
            report = session.coverage.files[str(self.path)]["session_filters"][0]
            self.assertEqual((report["input_rows"], report["kept_rows"], report["dropped_rows"]), (3, 2, 1))
            self.assertEqual(report["calendar_sha256"], calendar.provenance["sha256"])
            self.assertEqual(session.issues[-1].code, "OUTSIDE_TRADING_SESSION")
            self.assertIn("15:17", session.issues[-1].message)
            self.assertEqual(len(read_bar_frame(self.path).frame), 3)

    def test_all_outside_and_missing_calendar_coverage_fail_explicitly(self):
        with self.assertRaisesRegex(InputError, "NO_SESSION_BARS"):
            filter_session_bars(self.prepared(["15:17"]), sessions())
        with self.assertRaisesRegex(ValueError, "日历覆盖不足"):
            filter_session_bars(self.prepared(["2025-12-31 15:17"]), sessions())

    def test_in_session_bad_turnover_is_kept_for_numeric_validation(self):
        original = self.prepared(["09:01", "15:17"])
        original.frame["value"] = 0
        result = filter_session_bars(original, sessions())
        self.assertEqual(self.labels(result), ["09:01"])
        event = SimpleNamespace(bar=object(), factors=result.frame.iloc[0].to_dict())
        with self.assertRaisesRegex(ValueError, "无效 VWAP"):
            futures_bar_values(event, contract_multiplier=10)


if __name__ == "__main__":
    unittest.main()
