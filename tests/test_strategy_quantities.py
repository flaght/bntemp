"""Batch reads preserve ownership, partial fills, cancellation and scalar fallbacks."""
from decimal import Decimal
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tests import test_position_attribution as fixture
from bomber.framework.trader import ExecutionRoute, PositionManager, RuntimeMode, StrategyTemplate
from bomber.framework.trader.execution.contracts import ExecutionReportType


class StrategyQuantitiesTests(unittest.TestCase):
    setup_runner = fixture.AttributionTests.setup_runner

    def assert_scalar_equal(self, strategy, keys=("leg",)):
        expected = ({key: strategy.position(key) for key in keys},
                    {key: strategy.strategy_working_quantity(key) for key in keys})
        actual = strategy.position_quantities(iter(keys))
        self.assertEqual(actual, expected)
        self.assertEqual(tuple(actual[0]), tuple(keys))
        self.assertEqual(tuple(actual[1]), tuple(keys))
        return actual

    def test_frozen_shares_partial_fills_duplicates_cancellation_and_restore(self):
        for live in (False, True):
            with self.subTest(live=live):
                runner, client, backend, alpha, beta = self.setup_runner(live=live, paused=True)
                self.assert_scalar_equal(alpha)
                alpha.set_target("leg", 2, 1)
                client.planner.paused = False
                beta.set_target("leg", 3, 2)
                saved = self.assert_scalar_equal(alpha)
                self.assertEqual(saved, ({"leg": 0}, {"leg": 2}))
                self.assertEqual(self.assert_scalar_equal(beta), ({"leg": 0}, {"leg": 3}))
                report = backend.emit(0, ExecutionReportType.PARTIALLY_FILLED, 3)
                backend.handler(report)
                self.assertEqual(self.assert_scalar_equal(alpha), ({"leg": 2}, {"leg": 0}))
                self.assertEqual(self.assert_scalar_equal(beta), ({"leg": 1}, {"leg": 2}))
                state = runner.position_manager.state()
                backend.emit(0, ExecutionReportType.CANCELED, sequence=2)
                self.assertEqual(self.assert_scalar_equal(beta), ({"leg": 1}, {"leg": 0}))
                runner.position_manager.restore(state)
                self.assertEqual(self.assert_scalar_equal(beta), ({"leg": 1}, {"leg": 2}))
                self.assertEqual(saved, ({"leg": 0}, {"leg": 2}))
                saved[0]["leg"] = Decimal(99)
                self.assertEqual(alpha.position("leg"), 2)

    def test_many_zero_targets_read_ledger_once_without_scalar_queries(self):
        runner, client, backend, alpha, beta = self.setup_runner(paused=True)
        alpha.set_target("leg", 2, 1)
        client.planner.paused = False
        beta.set_target("leg", 3, 2)
        routes = runner._registrations["alpha"].execution_routes
        routes.update({f"zero-{i}": ExecutionRoute(f"zero-{i}", backend.backend_id, fixture.RB)
                       for i in range(458)})
        expected = ({key: alpha.position(key) for key in routes},
                    {key: alpha.strategy_working_quantity(key) for key in routes})
        ledger = runner.position_manager.attribution
        with patch.object(runner.position_manager, "position", side_effect=AssertionError("scalar position")), \
                patch.object(ledger, "working_quantity", side_effect=AssertionError("scalar working")), \
                patch.object(ledger, "_remaining", wraps=ledger._remaining) as remaining:
            self.assertEqual(alpha.position_quantities(routes), expected)
        self.assertEqual(remaining.call_count, len(ledger._active_tokens))
        self.assertEqual(len(expected[0]), 459)

    def test_current_route_working_scope_and_logical_old_instrument_positions(self):
        runner, _, backend, alpha, _ = self.setup_runner()
        positions = runner.position_manager
        positions.set_strategy_position("alpha", "leg", Decimal("7.5"))
        orders = {}
        for token, client, instrument, owner, quantity in (
                ("current", backend.backend_id, fixture.RB, "alpha", "1.25"),
                ("old", backend.backend_id, fixture.NEXT, "alpha", "10"),
                ("other-client", "other", fixture.RB, "alpha", "20"),
                ("other-strategy", backend.backend_id, fixture.RB, "beta", "30")):
            orders[token] = dict(client_id=client, instrument_id=str(instrument),
                submitting_strategy=owner, direction=-1, quantity=quantity,
                client_order_id=token, released=False,
                legs=[dict(strategy_id=owner, target_key="leg", quantity=quantity, filled="0")])
        positions.attribution.restore(dict(version=1, orders=orders, owned=[
            dict(client_id=backend.backend_id, instrument_id=str(fixture.NEXT),
                 strategy_id="alpha", target_key="leg", quantity="7.5")]))
        self.assertEqual(self.assert_scalar_equal(alpha),
                         ({"leg": Decimal("7.5")}, {"leg": Decimal("-1.25")}))

    def test_live_and_custom_provider_keep_scalar_reads(self):
        runner, _, _, alpha, _ = self.setup_runner(live=True)
        with patch.object(runner.position_manager, "strategy_quantities",
                          side_effect=AssertionError("Live bulk ledger read")):
            self.assert_scalar_equal(alpha)
        runner.mode = RuntimeMode.HISTORICAL
        runner._position_provider = NS(position=lambda sid, key: Decimal("8.25"))
        with patch.object(runner.position_manager, "strategy_quantities",
                          side_effect=AssertionError("custom provider bypassed")):
            self.assertEqual(self.assert_scalar_equal(alpha),
                             ({"leg": Decimal("8.25")}, {"leg": 0}))

    def test_custom_position_manager_subclass_keeps_its_position_override(self):
        class CustomPositions(PositionManager):
            def position(self, strategy_id, key):
                return Decimal("9.5")

        runner, _, _, alpha, _ = self.setup_runner()
        runner._position_manager = runner._position_provider = CustomPositions()
        self.assertEqual(self.assert_scalar_equal(alpha), ({"leg": Decimal("9.5")}, {"leg": 0}))

    def test_unknown_routes_fail_and_empty_query_is_empty(self):
        runner, _, _, alpha, _ = self.setup_runner()
        self.assertEqual(alpha.position_quantities(()), ({}, {}))
        with self.assertRaisesRegex(ValueError, "未知策略目标"):
            alpha.position_quantities(("leg", "missing"))
        with self.assertRaisesRegex(ValueError, "未知策略目标"):
            runner.position_quantities("missing", ("leg",))

    def test_legacy_context_keeps_scalar_order_and_strategy_overrides(self):
        calls = []
        strategy = StrategyTemplate("legacy")
        strategy._bind(NS(position=lambda key: calls.append(("position", key)) or Decimal(1),
            strategy_working_quantity=lambda key: calls.append(("working", key)) or Decimal(-2)))
        self.assertEqual(strategy.position_quantities(iter(("a", "b"))),
                         ({"a": 1, "b": 1}, {"a": -2, "b": -2}))
        self.assertEqual(calls, [("position", "a"), ("position", "b"),
                                 ("working", "a"), ("working", "b")])
        with patch.object(strategy, "position", return_value=Decimal(3)):
            self.assertEqual(strategy.position_quantities(("a",)), ({"a": 3}, {"a": -2}))


class FrameQuantitiesTests(unittest.TestCase):
    def test_frame_matches_scalar_reads_across_fills_gaps_and_final_flat(self):
        from tests import test_demo_option_vega as vega
        from copy import deepcopy
        vega.StrategyTests.setUpClass()
        scalar, bulk = vega.StrategyTests(), vega.StrategyTests()
        scalar.setUp()
        bulk.setUp()
        calls = []

        def quantities(keys):
            calls.append(tuple(keys))
            return ({key: bulk.ctx.position(key) for key in keys},
                    {key: bulk.ctx.strategy_working_quantity(key) for key in keys})

        bulk.ctx.position_quantities = quantities
        for clock, actual, working in (
                ("2026-08-13 13:58", {}, {}),
                ("2026-08-13 13:59", {}, {}),
                ("2026-08-13 14:00", {scalar.ids[scalar.info.symbol]: -2}, {}),
                ("2026-08-13 14:01", {scalar.ids[scalar.info.symbol]: -2}, {scalar.ids["IM2609"]: 1}),
                ("2026-08-14 13:58", {scalar.ids[scalar.info.symbol]: -2, scalar.ids["IM2609"]: 1}, {}),
                ("2026-08-14 13:59", {scalar.ids[scalar.info.symbol]: -2, scalar.ids["IM2609"]: 1}, {}),
                ("2026-08-14 14:50", {scalar.ids[scalar.info.symbol]: -2, scalar.ids["IM2609"]: 1}, {}),
                ("2026-08-14 14:51", {}, {})):
            for f in (scalar, bulk):
                f.ctx.actual = {key: Decimal(value) for key, value in actual.items()}
                f.ctx.working = {key: Decimal(value) for key, value in working.items()}
                stamp = vega.ns(clock)
                f.strategy.on_market_frame(NS(valuation_ns=stamp,
                    request_ns=stamp + 20_000_000_000, observations={
                        "000852": NS(price=6400), "IM2609": NS(price=6500),
                        f.info.symbol: NS(price=100)}))
            self.assertEqual(scalar.ctx.intents, bulk.ctx.intents)
            for name in ("last_decision", "signals", "option_goals", "future_goals",
                         "sent_days", "hedge_sent", "iv_cache", "resume_ns", "frames"):
                self.assertEqual(deepcopy(getattr(scalar.strategy, name)),
                                 deepcopy(getattr(bulk.strategy, name)), (clock, name))
        self.assertEqual(len(calls), bulk.strategy.frames)
        self.assertEqual(bulk.strategy.frames, 8)
        self.assertGreater(bulk.strategy.entry_decisions, 0)
        self.assertTrue(bulk.strategy.flatten_requested)


if __name__ == "__main__":
    unittest.main()
