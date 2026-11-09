"""08现金结算接线：真实策略、归属与执行客户端；不连接行情或柜台。"""
from datetime import date
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace as NS
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests import test_demo_option_vega as fixture


class VegaSettlementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.StrategyTests.setUpClass()

    def setUp(self):
        self.f = fixture.StrategyTests()
        self.f.setUp()
        self.s, self.ctx = self.f.strategy, self.f.ctx
        self.key = self.f.info.symbol
        self.instrument = self.f.ids[self.key]
        self.cutoff = fixture.ns("2026-09-18 15:00")
        self.s.option_last_trade_times = {self.key: self.cutoff}
        self.s.final_day = None
        self.ctx.actual[self.instrument] = Decimal(-1)
        self.s.option_goals[self.key] = Decimal(-1)

    def frame(self, clock):
        stamp = fixture.ns(clock)
        self.s.latest = {"000852": (stamp, 6400), "IM2609": (stamp, 6500)}
        self.s._frame(stamp, stamp + 20_000_000_000)

    def fill(self, **kwargs):
        return NS(metadata={"execution_origin": "option_expiry"},
            identity=NS(instrument_id=self.instrument), ts_event=self.cutoff+3600_000_000_000,
            quantity=Decimal(1), price=Decimal(0), **kwargs)

    def test_waits_for_authoritative_settlement_without_expired_quotes_or_false_flat(self):
        self.s.resume_ns = fixture.ns("2026-09-21 09:31")
        self.frame("2026-09-21 09:31")
        self.assertEqual(self.s.last_decision["awaiting_settlement"], [self.key])
        self.assertNotEqual(self.s.last_decision["status"], "RESUME_WAIT")
        self.assertEqual(self.s.option_goals[self.key], -1)
        self.assertEqual(self.s.position(self.instrument), -1)
        self.assertEqual(self.ctx.intents, [])

    def test_final_flat_holds_expired_residual_for_settlement_and_closes_other_legs(self):
        self.s.final_day = date(2026, 9, 18)
        self.ctx.actual[self.f.ids["IM2609"]] = Decimal(1)
        self.s.future_goals["IM2609"] = Decimal(1)
        self.frame("2026-09-18 15:01")
        self.assertTrue(self.s.flatten_requested)
        target = self.ctx.intents[-1].targets
        self.assertEqual(target[self.instrument], -1)
        self.assertEqual(target[self.f.ids["IM2609"]], 0)
        self.assertEqual(self.s.position(self.instrument), -1)

    def test_final_flat_only_expired_residual_does_not_resubmit_unchanged_targets(self):
        self.s.final_day = date(2026, 9, 18)
        self.frame("2026-09-18 15:01")
        self.frame("2026-09-18 15:02")
        self.assertTrue(self.s.flatten_requested)
        self.assertEqual(self.s.option_goals[self.key], -1)
        self.assertEqual(self.ctx.intents, [])

    def test_zero_final_fill_clears_only_settled_goal_without_reentrant_orders(self):
        self.ctx.actual[self.instrument] = Decimal(0)  # 归属/账户在回调前已按结算更新。
        self.s.latest[self.key] = (self.cutoff, 1)
        self.s.iv_cache[self.key] = (self.cutoff, .25)
        self.s.future_goals["IM2609"] = Decimal(1)
        self.s.on_fill(self.fill())
        self.assertEqual(self.s.option_goals[self.key], 0)
        self.assertEqual(self.s.future_goals["IM2609"], 1)
        self.assertNotIn(self.key, self.s.latest)
        self.assertNotIn(self.key, self.s.iv_cache)
        self.assertEqual(self.ctx.intents, [])
        self.assertEqual(self.s.signals[-1]["action"], "OPTION_CASH_SETTLEMENT")
        self.assertEqual(self.s.signals[-1]["price"], "0")

    def test_unconfirmed_residual_or_early_settlement_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "权威了结"):
            self.s.on_fill(self.fill())
        self.assertEqual(self.s.option_goals[self.key], -1)
        self.ctx.actual[self.instrument] = Decimal(0)
        event = self.fill()
        event.ts_event = self.cutoff-1
        with self.assertRaisesRegex(RuntimeError, "权威了结"):
            self.s.on_fill(event)
        self.assertEqual(self.s.option_goals[self.key], -1)

    def test_live_without_cash_policy_keeps_expired_position_protection(self):
        self.s.option_last_trade_times.clear()
        self.s.latest[self.key] = (fixture.ns("2026-09-21 09:31"), 1)
        with self.assertRaisesRegex(RuntimeError, "已过最后交易日"):
            self.frame("2026-09-21 09:31")
        self.assertEqual(self.s.position(self.instrument), -1)

    def test_settled_goal_stays_zero_on_next_day_rebalance(self):
        self.ctx.actual[self.instrument] = Decimal(0)
        self.s.on_fill(self.fill())
        self.frame("2026-09-21 09:31")
        self.assertEqual(self.s.option_goals[self.key], 0)
        self.assertNotIn(self.instrument, self.s.last_decision["positions"])
        self.assertEqual(self.ctx.intents, [])

    def test_lifecycle_requires_complete_valid_option_binding(self):
        for times in ({}, {self.key: True}, {self.key: -1}, {"OTHER": self.cutoff}):
            with self.subTest(times=times), self.assertRaisesRegex(ValueError, "生命周期"):
                type(self.s)("bad", self.s.config, self.s.contracts, self.s.futures,
                    self.s.instruments, "000852", self.s.trading_days, option_last_trade_times=times)

    def test_both_backtests_share_settlement_cli_without_opening_sources(self):
        replay = import_module("demos.08_option_vega.run_backtest")
        options = ["--start-day", "2026-09-01", "--end-day", "2026-09-18",
            "--option-settlements", "/fixture/daily", "--settlement-available-time", "17:30",
            "--exercise-fee-per-contract", "0.75"]
        with patch.object(replay, "run_case") as run, patch("sys.argv", [
                "run_backtest.py", *options, "--index-feature-file", "/fixture/index.csv"]):
            replay.main()
        first = run.call_args.args[0]
        second = import_module("demos.12_option_by_pos.run_backtest").parse_args(options)
        for key in ("option_settlements", "settlement_available_time", "exercise_fee_per_contract"):
            self.assertEqual(getattr(first, key), getattr(second, key))
        self.assertEqual(first.exercise_fee_per_contract, Decimal("0.75"))

    def test_yaml_can_configure_settlement_directories_without_column_parameters(self):
        replay = import_module("demos.08_option_vega.run_backtest")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "startup.yaml"
            path.write_text("experiment_setups:\n  run:\n"
                "    start_day: '2026-05-02'\n    end_day: '2026-06-21'\n"
                "    index_feature_file: /fixture/index.csv\n"
                "    option_settlements: /fixture/daily/opt\n    future_settlements: /fixture/daily/fut\n"
                "    settlement_available_time: '16:30'\n"
                "    future_settlement_fee_per_contract: '1.25'\n")
            with patch.object(replay, "run_case") as run, patch("sys.argv", ["run_backtest.py",
                    "--config-file", str(path), "--config-id", "run"]):
                replay.main()
            args = run.call_args.args[0]
            self.assertEqual(args.future_settlements, Path("/fixture/daily/fut"))
            self.assertEqual(args.option_settlements, Path("/fixture/daily/opt"))
            self.assertEqual(args.settlement_available_time, "16:30")
            self.assertFalse(hasattr(args, "future_settlement_symbol_column"))
            self.assertFalse(hasattr(args, "future_settlement_date_column"))
            self.assertFalse(hasattr(args, "future_settlement_price_column"))
            self.assertEqual(args.future_settlement_fee_per_contract, Decimal("1.25"))

    def test_only_expiry_cancellations_are_expected_rejections_still_fail(self):
        def event(status, stamp):
            return NS(status=status, ts_event=stamp, identity=NS(instrument_id=self.instrument), reason="fixture")
        for status in ("CANCELED", "EXPIRED"):
            with self.assertRaisesRegex(RuntimeError, status):
                self.s.on_order_update(event(status, self.cutoff-1))
            self.s.on_order_update(event(status, self.cutoff+1))
        with self.assertRaisesRegex(RuntimeError, "REJECTED"):
            self.s.on_order_update(event("REJECTED", self.cutoff+1))

    def test_public_client_applies_settlement_to_account_attribution_and_strategy_once(self):
        self.assert_public_settlement("option")

    def test_public_client_applies_future_settlement_to_several_owners_once(self):
        self.assert_public_settlement("future")

    def assert_public_settlement(self, asset):
        from bomber.framework.market.basic.base import InstrumentId
        from bomber.framework.trader.contracts import ExecutionRoute, RuntimeMode
        from bomber.framework.trader.portfolio import PositionManager
        from bomber.framework.trader.runner import UnifiedStrategyRunner
        from bomber.framework.trader.execution.simulation.client import SimulationExecutionClient
        from bomber.framework.trader.execution.planner import NetTargetOrderPlanner
        from bomber.framework.trader.execution.contracts import (ExecutionReport, ExecutionReportType,
            OrderSide, PositionEffect)
        from tests.test_position_attribution import Backend, Observer

        positions, backend = PositionManager(), Backend()
        client = SimulationExecutionClient(backend.backend_id, NetTargetOrderPlanner(positions), backend, positions)
        runner = UnifiedStrategyRunner(RuntimeMode.HISTORICAL, position_manager=positions)
        runner.add_execution_client(client)
        key = self.key if asset == "option" else "IM2609"
        instrument = InstrumentId.from_str(self.f.ids[key])
        strategy = type(self.s)("vega", self.s.config, self.s.contracts, self.s.futures,
            self.s.instruments, "000852", self.s.trading_days,
            option_last_trade_times={self.key: self.cutoff}, future_last_trade_times={"IM2609": self.cutoff})
        runner.add_strategy(strategy, data_bindings=(), execution_routes=(
            ExecutionRoute(str(instrument), backend.backend_id, instrument),))
        other = Observer("other")
        runner.add_strategy(other, data_bindings=(), execution_routes=(
            ExecutionRoute("leg", backend.backend_id, instrument),))
        runner.start()
        self.addCleanup(runner.stop)
        goals = strategy.option_goals if asset == "option" else strategy.future_goals
        goals[key] = Decimal(-2)
        strategy.set_target(str(instrument), -2, self.cutoff-60_000_000_000)
        backend.emit(0, ExecutionReportType.FILLED, 2)
        other.set_target("leg", -1, self.cutoff-30_000_000_000)
        backend.emit(1, ExecutionReportType.FILLED, 1)
        self.assertEqual(strategy.position(str(instrument)), -2)
        report = ExecutionReport(backend.backend_id, "settlement", instrument,
            ExecutionReportType.FILLED, self.cutoff+3600_000_000_000, Decimal(3), Decimal(0 if asset == "option" else 6900),
            OrderSide.BUY, Decimal(3), PositionEffect.CLOSE, report_id="settlement", sequence=1,
            metadata={"execution_origin": f"{asset}_expiry", "trade_id": "settlement"})
        backend.handler(report)
        backend.handler(report)
        self.assertEqual(client.report_errors, ())
        self.assertEqual(positions.account_position(backend.backend_id, instrument), 0)
        self.assertEqual(strategy.position(str(instrument)), 0)
        self.assertEqual(other.position("leg"), 0)
        self.assertEqual(goals[key], 0)
        self.assertEqual(sum(row["action"] == f"{asset.upper()}_CASH_SETTLEMENT" for row in strategy.signals), 1)
        self.assertEqual(len(backend.orders), 2)  # 结算不是新发普通委托。

    def test_future_settlement_clears_goal_and_next_session_needs_no_expired_quote(self):
        instrument = self.f.ids["IM2609"]
        self.s.future_last_trade_times = {"IM2609": self.cutoff}
        self.s.option_goals[self.key] = Decimal(0)
        self.ctx.actual[self.instrument] = Decimal(0)
        self.ctx.actual[instrument] = Decimal(7)
        self.s.future_goals["IM2609"] = Decimal(7)
        event = NS(metadata={"execution_origin": "future_expiry"}, identity=NS(instrument_id=instrument),
            ts_event=self.cutoff+3600_000_000_000, quantity=Decimal(7), price=Decimal("6900.13"))
        with self.assertRaisesRegex(RuntimeError, "权威了结"):
            self.s.on_fill(event)
        self.assertEqual(self.s.future_goals["IM2609"], 7)
        self.ctx.actual[instrument] = Decimal(0)  # 公共归属先于业务回调记账。
        self.s.on_fill(event)
        self.assertEqual(self.s.future_goals["IM2609"], 0)
        stamp = fixture.ns("2026-09-21 09:37")
        self.s.latest = {"000852": (stamp, 6400)}
        self.s.resume_ns = fixture.ns("2026-09-21 09:31")
        self.s._frame(stamp, stamp+20_000_000_000)
        self.assertNotEqual(self.s.last_decision["status"], "RESUME_WAIT")
        self.assertEqual(self.s.last_decision["awaiting_settlement"], [])
        self.assertEqual(self.ctx.intents, [])

    def test_expired_future_is_retained_while_waiting_for_authoritative_settlement(self):
        instrument = self.f.ids["IM2609"]
        self.s.future_last_trade_times = {"IM2609": self.cutoff}
        self.s.option_goals[self.key] = Decimal(0)
        self.ctx.actual[self.instrument] = Decimal(0)
        self.ctx.actual[instrument] = Decimal(7)
        self.s.future_goals["IM2609"] = Decimal(7)
        stamp = fixture.ns("2026-09-18 15:01")
        self.s.latest = {"000852": (stamp, 6400)}
        for final_day in (None, date(2026, 9, 18)):
            self.s.final_day = final_day
            self.s._frame(stamp, stamp+20_000_000_000)
            self.assertEqual(self.s.last_decision["awaiting_settlement"], ["IM2609"])
            self.assertEqual(self.s.future_goals["IM2609"], 7)
            self.assertEqual(self.s.position(instrument), 7)
            self.assertEqual(self.ctx.intents, [])


if __name__ == "__main__":
    unittest.main()
