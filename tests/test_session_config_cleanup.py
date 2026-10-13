"""Preserve migrated CU night exceptions and remove executable legacy demo dependencies."""
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from bomber.framework.calendars import NativeCalendarProvider
from bomber.framework.calendars.overrides import OVERRIDE_COLUMNS, normalize_overrides
from bomber.framework.dataprep.session_inputs import SessionInputs, load_session_inputs
from bomber.framework.dataprep.sources import ReferenceDataset, ReferenceQuery, ReferenceSourceFactory
from scripts.integration.reference_data.session_overrides_export import export_overrides
from tests.test_trading_hours import frame

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "scripts/integration/reference_data/configs/session_overrides_cu_2026.csv"
ZONE = ZoneInfo("Asia/Shanghai")
EXPECTED_DATES = tuple(date.fromisoformat(value) for value in (
    "2025-12-31", "2026-02-13", "2026-04-03", "2026-04-30", "2026-06-18", "2026-09-24", "2026-09-30"))


class SessionConfigCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_all_seven_legacy_night_cancellations_survive_standard_feather_export(self):
        raw = pd.read_csv(SEED, dtype="string", keep_default_na=False)
        self.assertEqual(tuple(raw.columns), OVERRIDE_COLUMNS)
        normalized = normalize_overrides(raw)
        self.assertEqual(tuple(row["date"] for row in normalized), EXPECTED_DATES)
        self.assertTrue(all((row["code"], row["date_basis"], row["action"], row["target"], row["segments"])
                           == ("CU", "NIGHT_START", "CANCEL", "NIGHT", "[]") for row in normalized))
        output = self.root / "cu-session-overrides.feather"
        result = export_overrides(SEED, output)
        with ReferenceSourceFactory.create("file", {ReferenceDataset.SESSION_OVERRIDES: output}) as source:
            batch = source.session_overrides(ReferenceQuery(products=("CU",)))
        self.assertEqual(tuple(dict(row) for row in batch.rows), normalized)
        self.assertEqual(result["sha256"], batch.fingerprint)
        self.assertEqual(result["rows"], 7)
        self.assertFalse(Path(str(output) + ".manifest.json").exists())

    def test_migrated_rules_cancel_both_midnight_pieces_but_keep_day_sessions(self):
        hours, overrides = self.root / "hours.feather", self.root / "overrides.feather"
        frame("CU").to_feather(hours)
        export_overrides(SEED, overrides)
        calendar = load_session_inputs(SessionInputs("CU", "china.sse", exchange="SHFE",
            backend="file", hours_file=hours, overrides_file=overrides))
        for day in EXPECTED_DATES:
            with self.subTest(day=day):
                self.assertIsNone(calendar.window(datetime.combine(day, datetime.min.time(), ZONE)
                                                 + timedelta(hours=21)))
                self.assertIsNone(calendar.window(datetime.combine(day + timedelta(days=1),
                    datetime.min.time(), ZONE) + timedelta(minutes=30)))
                self.assertIsNotNone(calendar.window(datetime.combine(day, datetime.min.time(), ZONE)
                                                     + timedelta(hours=9)))
                owner = next(candidate for candidate in calendar.calendar.days if candidate > day)
                self.assertEqual(len(calendar.windows(owner)), 3)
        # The verified ordinary Friday/Monday split night remains enabled.
        self.assertEqual(calendar.window(datetime(2026, 10, 10, 0, 30, tzinfo=ZONE)).trading_day, "20261012")

    def test_export_refuses_overwrite_and_validates_before_creating_output(self):
        output = self.root / "overrides.feather"
        export_overrides(SEED, output)
        original = output.read_bytes()
        with self.assertRaises(ValueError): export_overrides(SEED, output)
        self.assertEqual(output.read_bytes(), original)
        invalid = self.root / "invalid.csv"
        raw = pd.read_csv(SEED, dtype="string")
        raw.loc[0, "date_basis"] = "GUESS"
        raw.to_csv(invalid, index=False)
        destination = self.root / "invalid.feather"
        with self.assertRaises(ValueError): export_overrides(invalid, destination)
        self.assertFalse(destination.exists())

    def test_acceptance_lists_and_memory_fixtures_do_not_require_demo_calendar_json(self):
        from tests.run_main_ema_acceptance import SOURCES as ema_sources
        from tests.run_cross_section_acceptance import SOURCES as cross_sources
        from tests.session_fixtures import index_sessions
        removed = {"demos/01_main_ema/" + name for name in (
            "shfe_cu_2026.json", "cffex_im_20260921_22.json", "cffex_if_20261009_13.json")}
        self.assertFalse(removed.intersection(ema_sources))
        self.assertFalse(removed.intersection(cross_sources))
        for path in removed:
            self.assertFalse((ROOT / path).exists(), f"退役日历仍存在，请清理旧部署文件: {path}")
        self.assertTrue(all((ROOT / path).is_file() for path in ema_sources))
        for product in ("IM", "IF"):
            calendar = index_sessions(product)
            self.assertEqual(len(calendar.windows(date(2026, 9, 22))), 2)
            self.assertEqual(calendar.previous_trading_day("20260922"), "20260921")
        # Native Provider scope replaces the manually listed demo dates.
        self.assertTrue(NativeCalendarProvider("china.cffex").snapshot().is_trading_day(date(2026, 10, 14)))


if __name__ == "__main__":
    unittest.main()
