"""完成分钟标准化与按交易会话自动复权；真实日历/查询构造，受控DDB传输。"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch
from importlib import import_module
import unittest

from bomber.framework.calendars import (
    CalendarSnapshot, SessionBinding, SessionCalendar, SessionSegment, TradingHoursSnapshot)
from bomber.framework.calendars.sessions import datetime_ns
from bomber.framework.dataprep.history import (
    HistoryBar, HistoryError, HistoryRequest, DolphinDbHistoryConfig,
    DolphinDbHistoryProvider, SessionReferenceFactorAdjuster)
from bomber.framework.dataprep.sources import (
    DolphinDbReferenceConfig, ReferenceBatch, ReferenceDataset)


class SessionFactorTests(unittest.TestCase):
    def setUp(self):
        dates = CalendarSnapshot("test.cu", "v1", date(2026, 10, 14), date(2026, 10, 19),
            (date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19)), "declared")
        hours = TradingHoursSnapshot((
            ("CU", SessionSegment("day", "TRADING_DAY", 0, "09:00", 0, "15:00")),
            ("CU", SessionSegment("night", "NIGHT_START", 0, "21:00", 1, "01:00"))), "test")
        self.calendar = SessionCalendar(hours, dates, SessionBinding("CU", "CU", "test.cu", "Asia/Shanghai"))
        self.source, self.factory = Mock(), Mock()
        self.factory.create_for.return_value = self.source
        self.source.contract_structure.side_effect = self.roles
        self.source.adjustment_factors.side_effect = self.factors
        self.adjuster = SessionReferenceFactorAdjuster("config", self.calendar, factory=self.factory)

    def roles(self, query):
        rows = tuple({"date": d, "code": "CU", "main": "cu2611"}
            for d in (date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)) if d <= query.end_date)
        return ReferenceBatch(ReferenceDataset.CONTRACT_STRUCTURE, rows, ("date", "code", "main"), "roles")

    def factors(self, query):
        row = {"trade_date": query.start_date, "code": "CU", "symbol": "CU2611",
               "pcr_cumfactor": {14: "1.4", 15: "1.5", 16: "1.6"}[query.start_date.day]}
        return ReferenceBatch(ReferenceDataset.ADJUSTMENT_FACTORS, (row,), tuple(row), "factors")

    def bar(self, clock):
        from datetime import datetime
        stamp = datetime_ns(datetime.fromisoformat(clock + "+08:00")) - 1
        return HistoryBar("cu2611.SHFE", stamp, close="100")

    def test_day_night_and_midnight_use_session_trading_day(self):
        bars = tuple(self.bar(t) for t in ("2026-10-15T11:02:00", "2026-10-15T21:01:00",
                                          "2026-10-16T00:01:00"))
        result = self.adjuster(bars)
        self.assertEqual([b.adjusted_close for b in result], [Decimal("140"), Decimal("150"), Decimal("150")])
        self.assertTrue(all(b.adjusted_close is None for b in bars))
        self.assertEqual([(a["trading_day"], a["source_day"]) for a in self.adjuster.last_audit],
                         [("2026-10-15", "2026-10-14"), ("2026-10-16", "2026-10-15")])

    def test_friday_night_and_saturday_belong_to_monday(self):
        self.adjuster((self.bar("2026-10-16T21:01:00"), self.bar("2026-10-17T00:01:00")))
        self.assertEqual(self.adjuster.last_audit[0]["trading_day"], "2026-10-19")
        self.assertEqual(self.adjuster.last_audit[0]["source_day"], "2026-10-16")

    def test_explicit_future_publication_is_not_bypassed(self):
        bar = self.bar("2026-10-15T21:01:00")
        batch = self.factors(SimpleNamespace(start_date=date(2026, 10, 15)))
        row = dict(batch.rows[0], available_ns=bar.ts_event + 1)
        self.source.adjustment_factors.side_effect = None
        self.source.adjustment_factors.return_value = ReferenceBatch(batch.dataset, (row,), tuple(row), "future")
        with self.assertRaisesRegex(HistoryError, "尚不可用"):
            self.adjuster((bar,))
        self.source.close.assert_called_once()

    def test_missing_or_duplicate_factor_is_fatal(self):
        batch = self.factors(SimpleNamespace(start_date=date(2026, 10, 15)))
        for rows in ((), batch.rows * 2):
            self.source.adjustment_factors.side_effect = None
            self.source.adjustment_factors.return_value = ReferenceBatch(batch.dataset, rows, batch.columns, "bad")
            with self.assertRaises(HistoryError):
                self.adjuster((self.bar("2026-10-15T21:01:00"),))

    def test_outside_session_fails_before_database_access(self):
        with self.assertRaises(HistoryError):
            self.adjuster((self.bar("2026-10-15T20:00:00"),))
        self.factory.create_for.assert_not_called()

    def test_changing_factor_version_is_rejected(self):
        batch = self.factors(SimpleNamespace(start_date=date(2026, 10, 15)))
        row = dict(batch.rows[0], pcr_cumfactor="2.0")
        self.source.adjustment_factors.side_effect = [batch,
            ReferenceBatch(batch.dataset, (row,), tuple(row), "changed")]
        with self.assertRaisesRegex(HistoryError, "读取期间变化"):
            self.adjuster((self.bar("2026-10-15T21:01:00"),))

    def test_missing_selected_day_does_not_use_latest_other_day(self):
        batch = self.factors(SimpleNamespace(start_date=date(2026, 10, 16)))
        self.source.adjustment_factors.side_effect = None
        self.source.adjustment_factors.return_value = batch
        with self.assertRaisesRegex(HistoryError, "请求不一致"):
            self.adjuster((self.bar("2026-10-15T21:01:00"),))

    def test_completed_minute_query_matches_exact_framework_timestamp(self):
        connection = DolphinDbReferenceConfig("fake", 8848, "fake", "fake")
        config = DolphinDbHistoryConfig(connection, "dfs://min_bar", "shfe_1min",
            columns={"instrument_id": "Code", "close": "close"},
            date_column="date", time_column="minTime", source_timezone="Asia/Shanghai",
            time_label="end", instrument_format="symbol", venue="SHFE")
        bar = self.bar("2026-10-15T21:01:00")
        provider = DolphinDbHistoryProvider(config)
        query = provider._script(HistoryRequest(bar.instrument_id, bar.ts_event, bar.ts_event + 1))
        self.assertIn("-1l)", query)
        self.assertNotIn("+59999999999", query)
        self.assertIn('Code="cu2611"', query)
        self.assertEqual(self.calendar.last_minutes(bar.ts_event + 1, 1), (bar.ts_event,))
        midnight = self.bar("2026-10-16T00:00:00")
        query = provider._script(HistoryRequest(midnight.instrument_id, midnight.ts_event, midnight.ts_event + 1))
        self.assertIn("date>=2026.10.16,date<=2026.10.16", query)

    def test_raw_ema_configuration_automatically_wires_factor_adjuster(self):
        from pathlib import Path
        module = import_module("demos.01_main_ema.history")
        args = SimpleNamespace(history_config=Path(__file__).resolve().parents[1] / "demos/01_main_ema/history_cu.json",
            history_db="dolphindb", reference_timeout=15, history_file=None, history_missing_policy="fail")
        connection = DolphinDbReferenceConfig("fake", 8848, "fake", "fake")
        with patch.object(module.DolphinDbReferenceConfig, "from_env", return_value=connection):
            service = module.build_history(args, calendar=self.calendar)
        self.assertIsInstance(service.price_adjuster, SessionReferenceFactorAdjuster)
        self.assertEqual(service.config.time_label, "end")
        self.assertNotIn("adjusted_close", service.config.columns)


if __name__ == "__main__":
    unittest.main()
