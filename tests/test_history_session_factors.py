"""完成分钟标准化与按交易会话自动复权；真实日历/查询构造，受控DDB传输。"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from threading import RLock
from unittest.mock import Mock, patch
from importlib import import_module
import unittest

from bomber.framework.calendars import (
    CalendarSnapshot, SessionBinding, SessionCalendar, SessionSegment, TradingHoursSnapshot)
from bomber.framework.calendars.sessions import datetime_ns
from bomber.framework.dataprep.history import (
    HistoryBar, HistoryError, HistoryRequest, DolphinDbHistoryConfig,
    DolphinDbHistoryProvider, SessionReferenceFactorAdjuster, HistoryService, HistoryUnavailable)
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

    def factor_gap_service(self, policy="allow", *, market_gap=False):
        bars = (self.bar("2026-10-16T11:01:00"), self.bar("2026-10-16T11:02:00"),
                self.bar("2026-10-19T10:01:00"))
        def factors(query):
            batch = self.factors(query)
            if query.start_date == date(2026, 10, 15):
                return ReferenceBatch(batch.dataset, (), batch.columns, "missing-previous-factor")
            return batch
        self.source.adjustment_factors.side_effect = factors
        class Provider:
            def open(self): pass
            def close(self): pass
            def read(self, request): return bars
        factory = SimpleNamespace(create_for=lambda *args: Provider())
        service = HistoryService(database_config={}, factory=factory, missing_policy=policy,
                                 price_adjuster=self.adjuster)
        stamps = tuple(b.ts_event for b in bars)
        if market_gap:
            stamps += (self.bar("2026-10-19T10:02:00").ts_event,)
        return service, bars, stamps

    def test_allow_skips_missing_historical_factor_without_borrowing_current_factor(self):
        service, bars, stamps = self.factor_gap_service(market_gap=True)
        result = service.load(bars[0].instrument_id, stamps, phase="recovery")
        self.assertEqual([b.ts_event for b in result.bars], [bars[-1].ts_event])
        self.assertEqual(result.bars[0].adjusted_close, Decimal(160))
        self.assertEqual(set(result.missing), {bars[0].ts_event, bars[1].ts_event, stamps[-1]})
        audit = service.last_audit
        self.assertEqual((audit["required"], audit["loaded"], audit["missing"]), (4, 1, 3))
        self.assertEqual(audit["missing_market_bars"], 1)
        self.assertEqual(audit["missing_factor_bars"], 2)
        self.assertEqual(audit["missing_factors"][0]["source_day"], "2026-10-15")
        self.assertEqual(audit["missing_factors"][0]["bars"], 2)
        self.assertTrue(all(b.adjusted_close is None for b in bars))

    def test_fail_still_rejects_missing_historical_factor(self):
        service, bars, stamps = self.factor_gap_service(policy="fail")
        with self.assertRaisesRegex(HistoryError, "唯一累计因子"):
            service.load(bars[0].instrument_id, stamps, phase="recovery")

    def test_allow_does_not_hide_duplicate_invalid_or_future_factor(self):
        for failure in ("duplicate", "invalid", "future"):
            with self.subTest(failure=failure):
                service, bars, stamps = self.factor_gap_service()
                def factors(query):
                    batch = self.factors(query)
                    rows = batch.rows * 2 if failure == "duplicate" else (
                        dict(batch.rows[0], **({"pcr_cumfactor": "0"} if failure == "invalid" else
                             {"available_ns": bars[-1].ts_event + 1})),)
                    return ReferenceBatch(batch.dataset, rows, batch.columns, failure)
                self.source.adjustment_factors.side_effect = factors
                with self.assertRaises(HistoryError):
                    service.load(bars[0].instrument_id, stamps, phase="recovery")

    def test_allow_does_not_hide_factor_source_failure(self):
        service, bars, stamps = self.factor_gap_service()
        self.source.adjustment_factors.side_effect = RuntimeError("factor source unavailable")
        with self.assertRaisesRegex(RuntimeError, "factor source unavailable"):
            service.load(bars[0].instrument_id, stamps, phase="recovery")

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


class StartupHistoryDelayTests(unittest.TestCase):
    def setUp(self):
        self.module = import_module("demos.01_main_ema.history")
        self.stamps = (1792118519999999999, 1792118579999999999)
        self.before = self.stamps[-1] + 4_000_000_001
        self.rows = [HistoryBar("IM2610.CFFEX", stamp, adjusted_close=100) for stamp in self.stamps]
        self.queries, self.warmed = [], []
        self.elapsed = 0
        owner = self

        class Provider:
            def open(self): pass
            def close(self): pass
            def read(self, request):
                owner.queries.append(request)
                return tuple(owner.rows)

        factory = SimpleNamespace(create_for=lambda *args: Provider())
        strategy = SimpleNamespace(bars_used=0, last_processed_ns=-1,
            fast=SimpleNamespace(value=100), slow=SimpleNamespace(value=100, initialized=True))

        def warm(stamp, price):
            self.warmed.append((stamp, price))
            strategy.bars_used += 1
            strategy.last_processed_ns = stamp

        self.runner = SimpleNamespace(ema_checkpoint=SimpleNamespace(strategy=strategy,
            instrument="IM2610.CFFEX", warm_closed=warm),
            history_service=HistoryService(database_config={}, factory=factory), history_minutes=2,
            calendar=SimpleNamespace(last_minutes=lambda *args: self.stamps),
            accept_bars=False, references=SimpleNamespace(refresh=Mock()),
            session_ready=lambda: True, reference_is_current=lambda: True,
            _submit_lock=RLock(), _clients={"recording": SimpleNamespace(_submit_lock=RLock())})
        self.sleep_action = lambda: None

        def sleep(seconds):
            self.assertEqual(self.warmed, [])
            self.assertFalse(self.runner.accept_bars)
            self.elapsed += seconds
            self.sleep_action()

        for target, replacement in (("monotonic", lambda: self.elapsed), ("sleep", sleep)):
            patched = patch.object(self.module.time, target, side_effect=replacement)
            patched.start()
            self.addCleanup(patched.stop)

    def warm(self):
        return self.module.warm_history(self.runner, self.before, phase="recovery")

    def test_delayed_tail_retries_fixed_window_and_warms_exactly_once(self):
        tail = self.rows.pop()
        self.sleep_action = lambda: self.rows.append(tail)
        result = self.warm()
        self.assertEqual(result.missing, ())
        self.assertEqual(len(self.queries), 2)
        self.assertEqual(self.queries[0], self.queries[1])
        self.assertEqual([stamp for stamp, price in self.warmed], list(self.stamps))
        self.assertFalse(self.runner.accept_bars)

    def test_persistent_tail_gap_times_out_without_partial_indicator_state(self):
        self.rows.pop()
        with self.assertRaises(HistoryUnavailable): self.warm()
        self.assertEqual(self.elapsed, 20)
        self.assertEqual(self.warmed, [])

    def test_older_gap_does_not_wait(self):
        self.rows.pop(0)
        with self.assertRaises(HistoryUnavailable): self.warm()
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(self.elapsed, 0)

    def test_old_tail_outside_settlement_window_does_not_wait(self):
        self.rows.pop()
        self.before += 60_000_000_000
        with self.assertRaises(HistoryUnavailable): self.warm()
        self.assertEqual(len(self.queries), 1)

    def test_market_loss_while_waiting_keeps_gate_closed(self):
        self.rows.pop()
        self.sleep_action = lambda: setattr(self.runner, "session_ready", lambda: False)
        with self.assertRaisesRegex(HistoryUnavailable, "行情或参考失效"): self.warm()
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(self.warmed, [])

    def test_database_failure_is_not_treated_as_settlement_delay(self):
        def unavailable(*args):
            raise HistoryUnavailable("database connection unavailable")
        self.runner.history_service.factory.create_for = unavailable
        with self.assertRaises(HistoryUnavailable): self.warm()
        self.assertEqual(self.elapsed, 0)


if __name__ == "__main__":
    unittest.main()
