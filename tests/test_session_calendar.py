"""配置决定会话；新增品种不修改公共算法的无网络验收。"""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from bomber.framework.trader.runtime.trading_sessions import SessionCalendar, load_sessions, SHANGHAI

MINUTE = 60_000_000_000


def ns(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=SHANGHAI).timestamp()) * 10**9


class SessionCalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "calendar.json"
        self.data = {"schema_version": 1, "product": "NEW_PRODUCT", "timezone": "Asia/Shanghai",
            "start": "2026-10-09", "end": "2026-10-13",
            "trading_days": ["2026-10-09", "2026-10-12", "2026-10-13"],
            "session_segments": [
                {"start": "09:30", "end": "11:30", "trading_day_rule": "same_day"},
                {"start": "13:00", "end": "15:00", "trading_day_rule": "same_day"}]}

    def load(self):
        self.path.write_text(json.dumps(self.data))
        return load_sessions(self.path, self.data["product"])

    def test_new_product_and_option_series_only_change_configuration(self):
        for product in ("IH", "MO_SERIES", "UNREGISTERED"):
            with self.subTest(product=product):
                self.data["product"] = product
                calendar = self.load()
                self.assertIsInstance(calendar, SessionCalendar)
                self.assertEqual(len(calendar.last_minutes(ns("2026-10-13T15:00:00"), 300)), 300)
                self.assertEqual(calendar.previous_trading_day("20261012"), "20261009")
                with self.assertRaises(ValueError): load_sessions(self.path, "OTHER")

    def test_minute_boundaries_lunch_and_partial_bar(self):
        calendar = self.load()
        after = ns("2026-10-13T11:30:00") - 1
        before = ns("2026-10-13T13:01:00")
        self.assertEqual(calendar.missing_minutes(after, before), (before - 1,))
        self.assertEqual(calendar.missing_minutes(after, before - 30 * 10**9), ())
        self.assertIsNone(calendar.window(datetime(2026, 10, 13, 12, tzinfo=SHANGHAI)))
        self.assertIsNone(calendar.window(datetime(2026, 10, 13, 15, tzinfo=SHANGHAI)))

    def test_night_crosses_midnight_and_uses_next_declared_trading_day(self):
        self.data["session_segments"] = [{"start": "21:00", "end": "02:30",
            "end_day_offset": 1, "trading_day_rule": "next_trading_day"}]
        calendar = self.load()
        self.assertEqual(calendar.window(datetime(2026, 10, 10, 2, tzinfo=SHANGHAI)).trading_day, "20261012")
        self.assertIsNone(calendar.window(datetime(2026, 10, 10, 2, 30, tzinfo=SHANGHAI)))
        self.assertEqual(len(calendar.last_minutes(ns("2026-10-10T02:30:00"), 330)), 330)
        with self.assertRaises(RuntimeError):
            calendar.window(datetime(2026, 10, 13, 21, tzinfo=SHANGHAI))

    def test_night_cancellation_and_date_replacement(self):
        self.data["session_segments"].append({"start": "21:00", "end": "23:00",
            "trading_day_rule": "next_trading_day"})
        self.data["no_night"] = ["2026-10-09"]
        self.data["overrides"] = {"2026-10-12": [
            {"start": "10:00", "end": "10:02", "trading_day_rule": "same_day"}],
            "2026-10-13": []}
        calendar = self.load()
        self.assertIsNone(calendar.window(datetime(2026, 10, 9, 21, tzinfo=SHANGHAI)))
        self.assertEqual(calendar.missing_minutes(ns("2026-10-12T00:00:00"), ns("2026-10-13T15:00:00")),
            (ns("2026-10-12T10:01:00") - 1, ns("2026-10-12T10:02:00") - 1))

    def test_unknown_coverage_and_missing_rules_fail(self):
        calendar = self.load()
        with self.assertRaises(RuntimeError): calendar.last_minutes(ns("2026-10-09T09:30:00"), 1)
        with self.assertRaises(RuntimeError): calendar.is_trading_day(datetime(2026, 10, 14).date())
        with self.assertRaises(ValueError): calendar.window(datetime(2026, 10, 13, 9, 30))
        del self.data["schema_version"]
        with self.assertRaises(ValueError): self.load()

    def test_overlapping_segments_and_duplicate_dates_fail(self):
        self.data["session_segments"].append(dict(self.data["session_segments"][0]))
        with self.assertRaises(ValueError): self.load()
        self.data["session_segments"].pop()
        self.data["trading_days"].append("2026-10-09")
        with self.assertRaises(ValueError): self.load()


if __name__ == "__main__":
    unittest.main(verbosity=2)
