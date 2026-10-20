"""直接提取用户原文件计算方法作为独立对照，不加载其旧交易适配器。"""
import ast
import contextlib
import io
import math
from dataclasses import replace
from datetime import date
from decimal import Decimal
from importlib import import_module
from pathlib import Path
import unittest

import numpy as np
import pandas as pd
from tests import test_demo_option_vega as fixture

signal = import_module("demos.08_option_vega.vega_signal")
checkpoint = import_module("demos.08_option_vega.checkpoint")


def original():
    tree = ast.parse((Path(__file__).parent / "fixtures/option_vega/strategy1.py").read_text())
    methods = {"_calc_option_positions", "_evaluate_close", "_calc_remaining_minutes"}
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    cls.bases = []
    cls.body = [n for n in cls.body if isinstance(n, ast.Assign)
                or isinstance(n, ast.FunctionDef) and n.name in methods]
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    env = dict(np=np, pd=pd, math=math, _erf_vec=np.vectorize(math.erf))
    exec(compile(ast.Module(functions + [cls], []), "<user strategy1 methods>", "exec"), env)
    obj = env[cls.name]()
    obj._nav, obj._iv_cache = 40000000, {}
    return obj, env


class SourceFormulaTests(unittest.TestCase):
    def test_iv_and_allocations_match_original_across_strikes(self):
        source, env = original()
        rows, candidates = [], []
        for strike in (6600, 6700, 6800, 6900):
            key = f"MO2609-C-{strike}"
            price = signal.call_price(6500, strike, 26/252, .02, .25)
            iv = signal.source_iv(6500, strike, 26/252, .02, price)
            self.assertAlmostEqual(iv, env["_implied_iv"](6500, strike, 26/252, .02, price), places=13)
            rows.append(dict(Code=key, strike=strike, closePrice=price, futClosePrice=6500,
                Shibor=.02, lastTradingDate=pd.Timestamp("2026-09-18"), **{"剩余交易日": 26}))
            g = signal.source_greeks(6500, strike, 26/252, .02, iv, 36/365)
            candidates.append((key, g, Decimal(100)))
        with contextlib.redirect_stdout(io.StringIO()):
            expected = source._calc_option_positions(pd.DataFrame(rows), "2026-08-13")
        expected = {key: int(value) for key, value in expected.items() if value}
        self.assertEqual(signal.allocate_short(candidates, source.DELTA_TARGETS, 500000, 1000, 1000), expected)

    def test_entry_and_hedge_time_match_original(self):
        source, _ = original()
        for clock in ("09:00", "10:00", "11:00", "12:00", "13:30", "14:55", "15:00"):
            now = pd.Timestamp(f"2026-08-13 {clock}")
            self.assertEqual(signal.source_years(now, 26), 26/252)
            self.assertAlmostEqual(signal.source_years(now, 26, hedge=True),
                26/252 + source._calc_remaining_minutes(now)/(240*252))

    def test_delta_tie_retains_input_order_and_rounds_after_grouping(self):
        g = signal.Greeks(.2, .25, .001, 100)
        self.assertEqual(signal.allocate_short([("z",g,100),("a",g,100)], (.25,.25), 15000,100,100), {"z": -2})


class SourceLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.StrategyTests.setUpClass()

    def setUp(self):
        self.f = fixture.StrategyTests()
        self.f.setUp()
        self.s, self.ctx = self.f.strategy, self.f.ctx

    def frame(self, clock, premium=None):
        now = pd.Timestamp(clock, tz="Asia/Shanghai")
        price = premium if premium is not None else signal.call_price(6500,6600,
            signal.source_years(now, self.s._remaining_days(now.date(), self.f.info.last_day)), .02,.25)
        self.s.latest = {"000852": (fixture.ns(clock),6400), "IM2609": (fixture.ns(clock),6500),
            self.f.info.symbol: (fixture.ns(clock),price)}
        self.s._frame(fixture.ns(clock),fixture.ns(clock)+20_000_000_000)

    def test_two_days_add_and_no_automatic_hedge_between_slots(self):
        self.frame("2026-08-13 13:58")
        first = self.s.option_goals[self.f.info.symbol]
        self.ctx.actual[self.f.ids[self.f.info.symbol]] = first
        self.frame("2026-08-13 13:59")
        self.assertEqual(self.s.last_decision["hedge"], "NOT_DUE")
        self.frame("2026-08-14 13:58")
        self.assertLess(self.s.option_goals[self.f.info.symbol], first)

    def test_cap_rejects_increment_keeps_owned_position(self):
        self.s.config = replace(self.s.config, max_option_lots=1, max_total_option_lots=2)
        self.s.option_goals[self.f.info.symbol] = Decimal(-1)
        self.ctx.actual[self.f.ids[self.f.info.symbol]] = Decimal(-1)
        self.frame("2026-08-13 13:58")
        self.assertEqual(self.s.entry_audit[-1]["outcome"], "RISK_LIMIT")
        self.assertEqual(self.s.option_goals[self.f.info.symbol], -1)
        self.assertFalse(self.ctx.intents)

    def test_new_selection_keeps_other_contract_old_goal(self):
        new = replace(self.f.info, symbol="MO2609-C-6700", strike=6700)
        self.s.contracts[new.symbol] = new
        self.s.instruments[new.symbol] = new.symbol+".CFFEX"
        self.s.option_goals.update({self.f.info.symbol: Decimal(-1), new.symbol: Decimal(0)})
        self.ctx.actual[self.f.ids[self.f.info.symbol]] = Decimal(-1)
        self.s.options_by_day[date(2026,8,13)] = (new.symbol,)
        stamp = fixture.ns("2026-08-13 13:58")
        self.s.latest = {"000852": (stamp,6400), "IM2609": (stamp,6500),
            self.f.info.symbol: (stamp,100), new.symbol: (stamp,80)}
        self.s._frame(stamp,stamp+20_000_000_000)
        self.assertEqual(self.s.option_goals[self.f.info.symbol], -1)
        self.assertLess(self.s.option_goals[new.symbol], 0)

    def test_missing_iv_keeps_existing_protective_future(self):
        self.ctx.actual[self.f.ids[self.f.info.symbol]] = Decimal(-2)
        self.s.option_goals[self.f.info.symbol] = Decimal(-2)
        self.s.future_goals["IM2609"] = Decimal(1)
        self.ctx.actual[self.f.ids["IM2609"]] = Decimal(1)
        self.frame("2026-08-13 10:00", 10000)
        self.assertEqual(self.s.future_goals["IM2609"], 1)
        self.assertEqual(self.s.last_decision["hedge_missing_iv"], [self.f.info.symbol])
        self.assertFalse(self.ctx.intents)

    def test_exit_only_option_slot_uses_same_month_future(self):
        self.s.option_goals[self.f.info.symbol] = Decimal(-1)
        self.ctx.actual[self.f.ids[self.f.info.symbol]] = Decimal(-1)
        self.frame("2026-08-13 13:57", 1)
        self.assertEqual(self.s.last_decision["exit_checks"], [])
        # ITM相对期货：时间价值=1-(6700-6600)；指数保持6400不应代替期货。
        stamp = fixture.ns("2026-08-13 13:58")
        self.s.latest = {"000852": (stamp,6400), "IM2609": (stamp,6700), self.f.info.symbol: (stamp,1)}
        self.s._frame(stamp,stamp+20_000_000_000)
        self.assertEqual(self.s.last_decision["exit_checks"][0]["time_value"], -99)
        self.assertEqual(self.s.option_goals[self.f.info.symbol], 0)

    def test_strategy_entry_greeks_use_source_trading_years(self):
        self.frame("2026-08-13 13:58")
        now = pd.Timestamp("2026-08-13 13:58", tz="Asia/Shanghai")
        g = self.s._greeks(self.f.info.symbol, fixture.ns(str(now.tz_localize(None))), now)
        years = self.s._remaining_days(now.date(), self.f.info.last_day)/252
        expected = signal.source_greeks(6500,6600,years,.02,.25,36/365)
        self.assertAlmostEqual(g.iv, .25, places=12)
        self.assertAlmostEqual(g.delta, expected.delta, places=12)
        self.assertAlmostEqual(g.vega, expected.vega, places=9)

    def test_nearest_month_without_iv_does_not_fall_through_to_far_month(self):
        far = replace(self.f.info, symbol="MO2610-C-6600", future="IM2610",
                      last_day=date(2026,10,16), expiry_day=date(2026,10,16))
        self.s.contracts[far.symbol] = far
        self.s.futures[far.future] = {"contMultNum": Decimal(200)}
        self.s.instruments.update({far.symbol: far.symbol+".CFFEX", far.future: far.future+".CFFEX"})
        self.s.option_goals[far.symbol] = Decimal(0)
        self.s.future_goals[far.future] = Decimal(0)
        self.s.options_by_day[date(2026,8,13)] = (self.f.info.symbol,far.symbol)
        stamp = fixture.ns("2026-08-13 13:58")
        self.s.latest = {"000852": (stamp,6400), "IM2609": (stamp,6500),
            self.f.info.symbol: (stamp,10000), far.symbol: (stamp,200), far.future: (stamp,6500)}
        self.s._frame(stamp,stamp+20_000_000_000)
        self.assertEqual(self.s.entry_audit[-1]["outcome"], "NO_VALID_ENTRY")
        self.assertFalse(self.ctx.intents)

    def test_iv_rejects_incomplete_calendar_instead_of_using_lower_bound(self):
        self.s.trading_days = self.s.trading_days[:10]
        with self.assertRaisesRegex(ValueError, "日历须覆盖最后交易日"):
            self.frame("2026-08-13 13:58")

    def test_hedge_once_and_checkpoint_backward_compatibility(self):
        self.ctx.actual[self.f.ids[self.f.info.symbol]] = Decimal(-2)
        self.frame("2026-08-13 10:00")
        cp = checkpoint.OptionVegaCheckpoint(self.s)
        state = cp.snapshot_state()
        cp.restore_state(state)
        self.frame("2026-08-13 10:00")
        self.assertEqual(self.s.last_decision["hedge"], "NOT_DUE")
        legacy = {k:v for k,v in state.items() if k not in ("logic_version", "hedge_sent")}
        cp.restore_state(legacy)
        self.assertEqual(self.s.hedge_sent, set())
        self.assertEqual(self.s.option_goals, {self.f.info.symbol: Decimal(0)})


if __name__ == "__main__":
    unittest.main()
