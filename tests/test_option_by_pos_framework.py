"""多频率策略和现金结算契约回归；原生宿主边界用记录器隔离。"""
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from enum import Enum
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace as NS
import sys
import threading
import unittest
from unittest.mock import patch

import pandas as pd
# patch.dict(sys.modules) 恢复模块表时不能卸载已向 Arrow 注册的扩展类型。
import pandas.core.arrays.arrow.extension_types  # noqa: F401
import pyarrow.parquet  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "bomber.framework.trader.execution"


def package(name, path=None, **attrs):
    item = ModuleType(name)
    item.__dict__.update(attrs)
    if path:
        item.__path__ = [str(ROOT / path)]
    return item


def load(name, path):
    spec = spec_from_file_location(name, ROOT / path)
    item = module_from_spec(spec)
    sys.modules[name] = item
    spec.loader.exec_module(item)
    return item


class Identity(str):
    @classmethod
    def from_str(cls, text): return cls(text)


class CaptureStrategy:
    def __init__(self, strategy_id):
        self.strategy_id, self.submissions, self.actual, self.working = strategy_id, [], {}, {}
    def set_targets(self, targets, ts, **kwargs):
        self.submissions.append(NS(targets=dict(targets), ts_event=ts, **kwargs))
    def account_position(self, key): return self.actual.get(key, Decimal(0))
    def working_quantity(self, key): return self.working.get(key, Decimal(0))


class Mode(str, Enum):
    REPLACE = "REPLACE"


@contextmanager
def environment():
    overrides = {
        "bomber.framework.trader": package("bomber.framework.trader", "bomber/framework/trader"),
        PREFIX: package(PREFIX, "bomber/framework/trader/execution"),
        PREFIX + ".simulation": package(PREFIX + ".simulation", "bomber/framework/trader/execution/simulation"),
        "bomber.framework.trader.contracts": package("contracts", TargetUpdateMode=Mode, ExecutionRequest=object),
        "bomber.framework.trader.template": package("template", StrategyTemplate=CaptureStrategy),
        "bomber.framework.market.basic.base": package("basic", InstrumentId=Identity),
    }
    with patch.dict(sys.modules, overrides):
        contracts = load(PREFIX + ".contracts", "bomber/framework/trader/execution/contracts.py")
        calculation = load(PREFIX + ".option_settlement", "bomber/framework/trader/execution/option_settlement.py")
        strategy = load("option_by_pos_strategy_check", "demos/12_option_by_pos/strategy.py")
        yield NS(contracts=contracts, calculation=calculation, strategy=strategy)


def ns(text): return int(pd.Timestamp(text, tz="Asia/Shanghai").value)


class PositionStore:
    """记录仓位变化，执行真实 AttributionLedger/StateMachine/Client。"""
    def __init__(self, ledger):
        self._lock = threading.RLock()
        self.account, self.owned, self.working = {}, {}, {}
        self.attribution = ledger(self)
    def account_position(self, client, key): return self.account.get((client, key), Decimal(0))
    def working_quantity(self, client, key): return self.working.get((client, key), Decimal(0))
    def position(self, strategy, key): return self.owned.get((strategy, key), Decimal(0))
    def set_strategy_position(self, strategy, key, qty): self.owned[strategy, key] = qty
    def adjust_strategy_position(self, strategy, key, qty): self.owned[strategy, key] = self.position(strategy, key) + qty
    def adjust_account_position(self, client, key, qty): self.account[client, key] = self.account_position(client, key) + qty
    def mark_recovery_required(self, client): pass


class OptionStrategyTests(unittest.TestCase):
    def setUp(self):
        self.scope = environment()
        self.env = self.scope.__enter__()
        self.addCleanup(lambda: self.scope.__exit__(None, None, None))
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def strategy(self, rows, **config):
        path = self.root / "positions.csv"
        path.write_text("timestamp,instrument_id,instrument_type,target_qty\n" + "\n".join(rows))
        obj = self.env.strategy.OptionByPosStrategy("test", path, self.env.strategy.OptionByPosConfig(**config))
        obj.set_lifecycles({key: ns("2026-06-19 15:00") for key in obj.get_insts()})
        return obj

    def feed(self, strategy, symbol, stamp):
        strategy.on_bar(symbol, NS(bar_type=NS(instrument_id=Identity(symbol + ".CFFEX")),
                                  ts_event=ns(stamp), ts_init=ns(stamp)))

    def test_mixed_bar_arrival_order_emits_once_after_required_bars(self):
        rows = ["2026/5/6 13:55,IM2606,FUTURE,1", "2026/5/6 13:55,MO2606-C-7000,OPTION,-2"]
        for first, second in (("IM2606", "MO2606-C-7000"), ("MO2606-C-7000", "IM2606")):
            obj = self.strategy(rows)
            self.feed(obj, first, "2026-05-06 13:55")
            self.assertFalse(obj.submissions)
            self.feed(obj, second, "2026-05-06 13:55")
            self.feed(obj, second, "2026-05-06 13:55")
            self.assertEqual(len(obj.submissions), 1)
            self.assertEqual(obj.submissions[0].targets, {"IM2606": Decimal(1), "MO2606-C-7000": Decimal(-2)})
            self.assertEqual(obj.verify_signals(), [])

    def test_futures_m1_update_keeps_omitted_option_and_option_update_closes_omitted_future(self):
        obj = self.strategy(["2026/5/6 09:55,IM2606,FUTURE,1", "2026/5/6 09:55,MO2606-C-7000,OPTION,-2",
            "2026/5/6 10:00,IM2606,FUTURE,2", "2026/5/6 10:05,MO2606-C-7000,OPTION,0"])
        for key in obj.get_insts(): self.feed(obj, key, "2026-05-06 09:55")
        self.feed(obj, "IM2606", "2026-05-06 10:00")
        self.assertEqual(obj.submissions[-1].targets, {"IM2606": Decimal(2), "MO2606-C-7000": Decimal(-2)})
        self.feed(obj, "MO2606-C-7000", "2026-05-06 10:05")
        self.assertEqual(obj.submissions[-1].targets, {"IM2606": Decimal(0), "MO2606-C-7000": Decimal(0)})

    def test_missing_m5_does_not_silently_trade_on_m1_clock(self):
        obj = self.strategy(["2026/5/6 13:55,IM2606,FUTURE,1", "2026/5/6 13:55,MO2606-C-7000,OPTION,-2"])
        self.feed(obj, "IM2606", "2026-05-06 13:55")
        with self.assertRaisesRegex(RuntimeError, "缺少"):
            self.feed(obj, "IM2606", "2026-05-06 13:56")
        self.assertFalse(obj.submissions)

    def test_expired_option_waits_for_settlement_instead_of_generating_close_order(self):
        obj = self.strategy(["2026/5/6 13:55,IM2606,FUTURE,1", "2026/5/6 13:55,MO2606-C-7000,OPTION,-2",
                             "2026/5/6 15:00,MO2606-C-7000,OPTION,0"])
        obj.last_trade_ns["MO2606-C-7000"] = ns("2026-05-06 15:00")
        for key in obj.get_insts(): self.feed(obj, key, "2026-05-06 13:55")
        obj.actual["MO2606-C-7000"] = Decimal(-2)
        self.feed(obj, "IM2606", "2026-05-06 15:00")
        self.assertEqual(obj.submissions[-1].targets["MO2606-C-7000"], Decimal(-2))
        obj.on_fill(NS(identity=NS(instrument_id="MO2606-C-7000.CFFEX"), ts_event=ns("2026-05-06 16:00"),
                       quantity=2, price=0, metadata={"execution_origin": "option_expiry"}))
        self.assertEqual(obj.goals["MO2606-C-7000"], 0)

    def test_legacy_early_close_is_explicit(self):
        rows = ["2026/5/6 13:55,MO2606-C-7000,OPTION,-2", "2026/5/6 15:00,MO2606-C-7000,OPTION,0"]
        normal, early = self.strategy(rows), self.strategy(rows, legacy_close_early=True)
        for obj in (normal, early):
            self.feed(obj, "MO2606-C-7000", "2026-05-06 13:55")
            self.feed(obj, "IM2606", "2026-05-06 14:58")
        self.assertEqual(len(normal.submissions), 1)
        self.assertEqual(len(early.submissions), 2)
        self.assertEqual(early.submissions[-1].targets["MO2606-C-7000"], 0)

    def test_expiry_boundary_preserves_order_waiting_for_final_m5(self):
        obj = self.strategy(["2026/5/6 14:55,MO2606-C-7000,OPTION,-2",
                             "2026/5/6 15:00,MO2606-C-7000,OPTION,0"])
        obj.last_trade_ns["MO2606-C-7000"] = ns("2026-05-06 15:00")
        self.feed(obj, "MO2606-C-7000", "2026-05-06 14:55")
        obj.working["MO2606-C-7000"] = Decimal(-2)
        self.feed(obj, "IM2606", "2026-05-06 15:00")
        self.assertEqual(obj.submissions[-1].targets["MO2606-C-7000"], Decimal(-2))

    def test_duplicate_or_noninteger_targets_rejected(self):
        for rows in (["2026/5/6 13:55,MO2606-C-7000,OPTION,-2.5"],
                     ["2026/5/6 13:55,IM2606,FUTURE,1"] * 2):
            with self.assertRaises(ValueError): self.strategy(rows)


class SettlementValueTests(unittest.TestCase):
    def test_call_put_zero_terminal_value_and_short_pnl(self):
        with environment() as env:
            calc = env.calculation
            self.assertEqual(calc.option_settlement_price("C", 7000, "7120.13"), Decimal("120.13"))
            self.assertEqual(calc.option_settlement_price("P", 7000, 7100), 0)
            self.assertEqual(calc.settlement_pnl(-1, 50, 120, 100), -7000)
            self.assertEqual(calc.settlement_pnl(2, 50, 0, 100, 1), -10002)
            with self.assertRaises(ValueError): calc.OptionSettlement("MO.CFFEX", 20, 19, 0)

    def test_cash_settlement_updates_account_and_strategy_once_with_zero_price(self):
        with environment() as env:
            package_overrides = {
                PREFIX + ".ports": package("ports", OrderPlannerPort=object, SimExecutionBackendPort=object),
                PREFIX + ".risk": package("risk", KillSwitchMode=NS(REDUCE_ONLY=NS(value="REDUCE_ONLY")),
                                           PreTradeRiskManager=object),
                "bomber.framework.trader.portfolio": package("portfolio", PositionManager=object),
            }
            with patch.dict(sys.modules, package_overrides):
                attribution = load(PREFIX + ".attribution", "bomber/framework/trader/execution/attribution.py")
                load(PREFIX + ".order_state", "bomber/framework/trader/execution/order_state.py")
                load(PREFIX + ".events", "bomber/framework/trader/execution/events.py")
                client_mod = load(PREFIX + ".simulation.client", "bomber/framework/trader/execution/simulation/client.py")
                positions = PositionStore(attribution.PositionAttributionLedger)
                identity = Identity("MO2606-C-7000.CFFEX")
                positions.account["sim", identity] = Decimal(-2)
                positions.attribution.adopt("sim", identity, "strategy", "MO2606-C-7000", Decimal(-2))
                backend = NS(backend_id="sim", register_report_handler=lambda fn: None)
                client = client_mod.SimulationExecutionClient("sim", None, backend, positions)
                events = []
                client.register_execution_event_handler(events.append)
                c = env.contracts
                report = c.ExecutionReport("sim", "settlement-1", identity, "FILLED", 100,
                    2, 0, "BUY", 2, "CLOSE", report_id="settlement-1", sequence=1,
                    metadata={"execution_origin": "option_expiry", "trade_id": "settlement-1",
                              "commission": "0", "commission_currency": "CNY"})
                client._on_report(report)
                client._on_report(report)
                self.assertEqual(client.report_errors, ())
                self.assertEqual(positions.account_position("sim", identity), 0)
                self.assertEqual(positions.position("strategy", "MO2606-C-7000"), 0)
                self.assertEqual(positions.working_quantity("sim", identity), 0)
                self.assertEqual(len(events), 2)
                self.assertEqual(events[-1].price, 0)
                self.assertEqual(events[-1].metadata["attributed_fills"][0]["quantity"], "2")
                with self.assertRaises(ValueError):
                    c.ExecutionReport("sim", "ordinary", identity, "FILLED", 101, 1, 0, "BUY", 1)


class SettlementInputTests(unittest.TestCase):
    def test_old_eod_filters_daily_settlement_and_preserves_zero_final_price(self):
        with environment():
            from bomber.framework.dataprep.option_settlements import load_option_settlements
            with TemporaryDirectory() as root:
                path = Path(root) / "opt_eod.parquet"
                pd.DataFrame(dict(tradeID=["MO2606-C-7000"] * 2,
                    TradeDate=pd.to_datetime(["2026-06-18", "2026-06-19"]),
                    lastTradingDate=pd.to_datetime(["2026-06-19"] * 2), settlePrice=[12, 0])).to_parquet(path)
                result = load_option_settlements(path, {"MO2606-C-7000": NS(last_trade=date(2026, 6, 19))},
                    start_day=date(2026, 6, 1), end_day=date(2026, 6, 30))
                self.assertEqual(result["MO2606-C-7000"].price, 0)
                self.assertEqual(result["MO2606-C-7000"].available_ns, ns("2026-06-19 16:00"))


if __name__ == "__main__": unittest.main()
