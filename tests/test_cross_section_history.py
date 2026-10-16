"""实际横截面策略/历史服务/日历的同步复权预热，无数据库或柜台连接。"""
from datetime import date
from decimal import Decimal
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from threading import RLock
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest

from tests import test_execution_composition as fixtures


class Instrument(fixtures.Instrument):
    @property
    def symbol(self): return self.value.split(".")[0]
    @property
    def venue(self): return self.value.split(".")[1]


class CrossHistoryTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExecutionCompositionTests.setUp(self)
        self.api = import_module("bomber.framework.dataprep.history")
        self.module = import_module("demos.04_cross_section.history")
        self.strategy_module = import_module("demos.04_cross_section.strategy")
        from tests.session_fixtures import declared_day_sessions
        from bomber.framework.calendars.sessions import datetime_ns
        self.calendars = {p: declared_day_sessions(p, date(2026, 10, 16)) for p in ("IM", "IF")}
        end = datetime_ns(self.calendars["IM"].windows(date(2026, 10, 16))[0].end)
        self.stamps = self.calendars["IM"].last_minutes(end, 5)
        self.ids = {"IM": Instrument("IM2611.CFFEX"), "IF": Instrument("IF2611.CFFEX")}
        snapshot = NS(instrument=lambda p, role: self.ids[p].symbol, factor=lambda p, role: Decimal(2))
        self.strategy = self.strategy_module.MainCrossSectionMomentumStrategy("cross", products=("IM", "IF"),
            roles=NS(snapshot=lambda stamp: snapshot), instruments={i.symbol.lower(): i for i in self.ids.values()},
            multipliers={i: Decimal(200) for i in self.ids.values()}, lookback=2, rebalance_interval=1,
            target_notional=Decimal(10000000), group_fraction=Decimal("0.5"))
        self.strategy.set_targets = Mock()
        owner = self
        self.rows = {str(i): [self.api.HistoryBar(str(i), s, close=100 + n if p == "IM" else 100 - n)
            for n, s in enumerate(self.stamps)] for p, i in self.ids.items()}
        class Provider:
            def open(self): pass
            def close(self): pass
            def read(self, req): return tuple(owner.rows[req.instrument_id])
        class Factory:
            @staticmethod
            def create_for(*args): return Provider()
        self.services = {p: self.api.HistoryService(database_config={}, factory=Factory, missing_policy="allow",
            price_adjuster=lambda rows: tuple(replace(b, adjusted_close=b.close * 2) for b in rows)) for p in self.ids}
        self.history = self.module.CrossSectionHistory(self.services, self.calendars, 3)
        self.runner = NS(strategy=self.strategy, instruments=self.ids, fixed_ids=frozenset(self.ids.values()),
            _submit_lock=RLock(), history_buffer=[], first_complete_bar_ns=self.stamps[0], history_frame_count=0)

    def event(self, product, stamp, price):
        return NS(bar_type=NS(instrument_id=self.ids[product]), ts_event=stamp,
                  close=NS(as_decimal=lambda: Decimal(price)))

    def warm(self):
        self.history.warm(self.runner, self.stamps[2] + 1)

    def test_full_adjusted_window_does_not_publish_historical_targets(self):
        self.warm()
        self.assertEqual(tuple(self.strategy.signal.history["IM"]), (Decimal(200), Decimal(202), Decimal(204)))
        self.assertEqual(self.strategy.synchronized_frames, 3)
        self.assertEqual(self.strategy._revision, 0)
        self.strategy.set_targets.assert_not_called()
        self.assertEqual(self.history.last_audit["loaded"], 3)

    def test_four_index_products_warm_21_frames_then_rank_two_long_two_short(self):
        from tests.session_fixtures import declared_day_sessions
        from bomber.framework.calendars.sessions import datetime_ns
        products = ("IM", "IH", "IC", "IF")
        calendars = {p: declared_day_sessions(p, date(2026, 10, 16), start="09:00", end="09:30") for p in products}
        end = datetime_ns(calendars["IM"].windows(date(2026, 10, 16))[0].end)
        stamps = calendars["IM"].last_minutes(end, 25)
        ids = {p: Instrument(p+"2610.CFFEX") for p in products}
        snapshot = NS(instrument=lambda p, role: ids[p].symbol, factor=lambda p, role: Decimal(2))
        strategy = self.strategy_module.MainCrossSectionMomentumStrategy("four", products=products,
            roles=NS(snapshot=lambda _: snapshot), instruments={i.symbol.lower(): i for i in ids.values()},
            multipliers={i: Decimal(300 if p in {"IF", "IH"} else 200) for p, i in ids.items()},
            lookback=20, rebalance_interval=5, group_fraction=Decimal("0.5"), target_notional=Decimal(3000000),
            target_quantity_cap=1)
        strategy.set_targets = Mock()
        slopes = {"IM": 3, "IH": 1, "IC": -1, "IF": -3}
        rows = {str(i): tuple(self.api.HistoryBar(str(i), s, close=100+n*slopes[p]) for n, s in enumerate(stamps[:21]))
            for p, i in ids.items()}
        class Provider:
            def open(self): pass
            def close(self): pass
            def read(self, req): return rows[req.instrument_id]
        class Factory:
            @staticmethod
            def create_for(*_): return Provider()
        services = {p: self.api.HistoryService(database_config={}, factory=Factory, missing_policy="allow",
            price_adjuster=lambda bars: tuple(replace(b, adjusted_close=b.close*2) for b in bars)) for p in products}
        history = self.module.CrossSectionHistory(services, calendars, 21)
        runner = NS(strategy=strategy, instruments=ids, _submit_lock=RLock(), history_buffer=[],
            first_complete_bar_ns=stamps[0], history_frame_count=0)
        history.warm(runner, stamps[20]+1)
        self.assertEqual(history.last_audit["loaded"], 21)
        self.assertEqual(set(history.last_audit["products"]), set(products))
        strategy.set_targets.assert_not_called()
        for n, stamp in enumerate(stamps[21:], 21):
            for p in products:
                strategy.on_bar(str(ids[p]), NS(bar_type=NS(instrument_id=ids[p]), ts_event=stamp,
                    close=NS(as_decimal=lambda p=p, n=n: Decimal(100+n*slopes[p]))))
        strategy.set_targets.assert_called_once()
        self.assertEqual(set(strategy.last_signal.long_products), {"IM", "IH"})
        self.assertEqual(set(strategy.last_signal.short_products), {"IC", "IF"})
        self.assertEqual(dict(strategy.last_targets), {str(ids[p]): Decimal(1 if p in {"IM", "IH"} else -1) for p in products})

    def test_next_live_frame_uses_same_price_scale_and_original_ranking(self):
        self.warm()
        for p, price in (("IM", 104), ("IF", 96)):
            self.strategy.on_bar(str(self.ids[p]), self.event(p, self.stamps[3], price))
        self.strategy.set_targets.assert_called_once()
        self.assertEqual(self.strategy.last_signal.long_products, ("IM",))
        self.assertEqual(self.strategy.last_signal.short_products, ("IF",))

    def test_missing_product_strict_fails_without_partial_window(self):
        self.rows[str(self.ids["IF"])] = []
        with self.assertRaises(self.api.HistoryUnavailable): self.warm()
        self.assertEqual(self.strategy.synchronized_frames, 0)
        self.assertEqual(self.history.last_audit["products"]["IM"]["loaded"], 3)
        self.assertEqual(self.history.last_audit["products"]["IF"]["loaded"], 0)

    def test_allow_uses_only_complete_same_minute_frames(self):
        self.history.missing_policy = "allow"
        self.rows[str(self.ids["IF"])] = [self.rows[str(self.ids["IF"])][1]]
        self.warm()
        self.assertEqual(self.strategy.synchronized_frames, 1)
        self.assertEqual(self.history.last_audit["missing"], 2)

    def test_live_buffer_completes_gap_without_duplicate_price_point(self):
        self.rows[str(self.ids["IF"])] = self.rows[str(self.ids["IF"])][:2]
        self.runner.history_buffer = [self.event("IF", self.stamps[2], 98)] * 2
        self.warm()
        self.assertEqual(self.strategy.synchronized_frames, 3)

    def test_flush_waits_for_complete_set_then_advances_once(self):
        self.warm()
        self.runner.history_buffer = [self.event("IM", self.stamps[3], 103)]
        self.history.flush(self.runner)
        self.assertEqual(self.strategy.synchronized_frames, 3)
        self.runner.history_buffer.append(self.event("IF", self.stamps[3], 97))
        self.history.flush(self.runner)
        self.history.flush(self.runner)
        self.assertEqual(self.strategy.synchronized_frames, 4)
        self.strategy.set_targets.assert_not_called()

    def test_restore_revision_is_preserved(self):
        self.strategy.restore_target_revision(8)
        self.warm()
        self.assertEqual(self.strategy._revision, 8)
        self.strategy.set_targets.assert_not_called()

    def test_invalid_adjusted_price_leaves_all_windows_unchanged(self):
        self.services["IF"].price_adjuster = lambda rows: tuple(replace(b, adjusted_close=None) for b in rows)
        with self.assertRaises(self.api.HistoryError): self.warm()
        self.assertEqual(self.strategy.synchronized_frames, 0)

    def test_database_outage_is_not_hidden_by_allow(self):
        self.history.missing_policy = "allow"
        self.services["IF"]._read = Mock(side_effect=self.api.HistoryUnavailable("offline"))
        with self.assertRaises(self.api.HistoryUnavailable): self.warm()

    def test_inconsistent_historical_source_days_fail_before_strategy_update(self):
        class Adjust:
            def __init__(self, source):
                self.last_audit = [{"factor_date_basis": "source", "trading_day": "2026-10-16", "source_day": source}]
            def __call__(self, rows): return tuple(replace(b, adjusted_close=b.close * 2) for b in rows)
        self.services["IM"].price_adjuster = Adjust("2026-10-15")
        self.services["IF"].price_adjuster = Adjust("2026-10-14")
        with self.assertRaisesRegex(self.api.HistoryError, "来源日不一致"): self.warm()
        self.assertEqual(self.strategy.synchronized_frames, 0)

    def test_common_calendar_expands_search_for_overlapping_minutes(self):
        from tests.session_fixtures import declared_day_sessions
        from bomber.framework.calendars.sessions import datetime_ns
        calendars = [declared_day_sessions("IM", date(2026, 10, 16), start="09:00", end="09:05"),
                     declared_day_sessions("IF", date(2026, 10, 16), start="09:01", end="09:06")]
        before = datetime_ns(calendars[1].windows(date(2026, 10, 16))[0].end)
        result = self.module.CommonCalendar(calendars).last_minutes(before, 2)
        self.assertEqual(result, calendars[0].last_minutes(before, 2))

    def test_builder_uses_per_product_calendar_and_automatic_factors(self):
        args = NS(history_config=Path(__file__).resolve().parents[1] / "demos/04_cross_section/history_cffex.json",
            products=("IM", "IF"), reference_database="dfs://bomber_daily", reference_timeout=15,
            reference_max_source_age_days=14, factor_date_basis="source", history_file=None,
            history_stage="recovery", history_minutes=0, lookback=20, history_missing_policy="fail",
            _session_calendars=self.calendars)
        from bomber.framework.dataprep.sources import DolphinDbReferenceConfig
        cfg = DolphinDbReferenceConfig("fake", 8848, "fake", "fake")
        with patch.object(DolphinDbReferenceConfig, "from_env", return_value=cfg):
            history = self.module.build_history(args, NS(instrument_ids=self.ids))
        self.assertEqual(history.minutes, 21)
        self.assertTrue(all(isinstance(s.price_adjuster, self.api.SessionReferenceFactorAdjuster)
                            for s in history.services.values()))

    def test_cffex_portfolio_is_opt_in_and_symbol_case_matches_database(self):
        import sys
        sys.modules["bomber.framework.market.basic.base"].InstrumentMeta = NS
        module = import_module("bomber.framework.dataprep.live_portfolio")
        from bomber.framework.datahub.sector_roles import SectorRoleAssignment
        assignment = SectorRoleAssignment(date(2026, 10, 16), date(2026, 10, 15), 0, 0,
            {p: {"main": i.symbol} for p, i in self.ids.items()},
            {p: {"main": Decimal(2)} for p in self.ids})
        service = NS(products=("IM", "IF"), trading_day=date(2026, 10, 16), started_ns=0,
            factor_date_basis="source", refresh=lambda: None, snapshot=lambda ns: assignment,
            manifest={"factor_date": "2026-10-15", "ready": True},
            instrument_specs={p: {"main": NS(symbol=i.symbol, product=p, venue="CFFEX", currency="CNY")}
                              for p, i in self.ids.items()})
        with self.assertRaises(ValueError): module.PortfolioRoleReferences(service)
        refs = module.PortfolioRoleReferences(service, allowed_venues=module.CTP_PORTFOLIO_VENUES | {"CFFEX"})
        self.assertEqual(str(refs.instrument_ids["IM"]), "IM2611.CFFEX")
        self.assertEqual(module.ctp_futures_symbol("im2611", "CFFEX"), "IM2611")
        self.assertEqual(module.ctp_futures_symbol("ih2610", "CFFEX"), "IH2610")
        self.assertEqual(module.ctp_futures_symbol("ic2610", "CFFEX"), "IC2610")
        with self.assertRaises(ValueError): module.ctp_futures_symbol("T2610", "CFFEX")

    def test_im_calendar_recording_reference_can_select_two_real_cffex_contracts(self):
        import sys
        sys.modules["bomber.framework.market.basic.base"].InstrumentMeta = NS
        module = import_module("demos.03_calendar_spread.live_references")
        from bomber.framework.datahub.sector_roles import SectorRoleAssignment
        assignment = SectorRoleAssignment(date(2026, 10, 16), date(2026, 10, 15), 0, 0,
            {"IM": {"secondary": "IM2611", "far": "IM2612"}}, {"IM": {}})
        service = NS(products=("IM",), roles={"IM": ("secondary", "far")}, factor_roles={"IM": ()},
            trading_day=date(2026, 10, 16), refresh=lambda: None, snapshot=lambda ns: assignment,
            manifest={"fingerprint": "test"}, instrument_specs={"IM": {r: NS(symbol=s, product="IM",
                venue="CFFEX", currency="CNY") for r, s in (("secondary", "IM2611"), ("far", "IM2612"))}})
        with self.assertRaises(ValueError): module.CalendarReferences(service, "IM", ("secondary", "far"))
        refs = module.CalendarReferences(service, "IM", ("secondary", "far"),
            allowed_venues={"CFFEX"})
        self.assertEqual([str(i) for i in refs.instrument_ids.values()], ["IM2611.CFFEX", "IM2612.CFFEX"])
        service.instrument_specs["IM"]["far"].symbol = "IM2611"
        service.snapshot = lambda ns: SectorRoleAssignment(date(2026, 10, 16), date(2026, 10, 15), 0, 0,
            {"IM": {"secondary": "IM2611", "far": "IM2611"}}, {"IM": {}})
        with self.assertRaisesRegex(ValueError,
                "角色指向相同合约.*secondary.*IM2611.CFFEX.*far.*IM2611.CFFEX"):
            module.CalendarReferences(service, "IM", ("secondary", "far"), allowed_venues={"CFFEX"})


if __name__ == "__main__":
    unittest.main()
