"""原生验收：真实 Feather → M1/M5 回放 → 到期现金结算及账户对账。"""
from decimal import Decimal
import gc
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

try:
    import bomber.backtest.engine  # noqa: F401
except ImportError as error:
    NATIVE_ERROR = str(error)
else:
    NATIVE_ERROR = None


@unittest.skipIf(NATIVE_ERROR is not None, f"需要 Bomber 编译扩展: {NATIVE_ERROR}")
class NativeOptionByPosTests(unittest.TestCase):
    def test_mixed_frequencies_and_terminal_values_reconcile_actual_ledger(self):
        module = importlib.import_module("demos.12_option_by_pos.run_backtest")
        for terminal, signal, entry, fut_fill, opt_fill in (
            (Decimal("0"), "13:55", 60, "13:56", "14:00"),
            (Decimal("120.13"), "13:55", 60, "13:56", "14:00"),
            (Decimal("0"), "14:55", 80, "15:00", "15:00"),
            (None, "13:55", 60, "13:56", "14:00")):
            with self.subTest(terminal=terminal, signal=signal), TemporaryDirectory() as directory:
                root = Path(directory)
                role = root / "role"
                role.mkdir()
                pd.DataFrame([dict(symbol="IM2607", code="IM", exchangeCD="CCFX", currencyCD="CNY",
                    contMultNum=200, minChgPriceNum=0.2, listDate="2026-01-01", lastTradeDate="2026-07-17",
                    trade_fee_per_contract=0, trade_fee_rate=0)]).to_feather(role / "fut_basic.feather")
                option = "MO2606-C-7000"
                pd.DataFrame([dict(symbol=option, contractType="CO", strikePrice=7000, contMultNum=100,
                    varTicker="000852", exchangeCD="CCFX", currencyCD="CNY", listDate="2026-01-01",
                    lastTradeDate="2026-06-19", expDate="2026-06-19", tickNum=0.2)]).to_feather(
                        role / "opt_basic.feather")
                for kind, symbol, stamps, values in (
                    ("fut", "IM2607", ["13:55", "13:56", "14:00", "14:55", "15:00"],
                     [7000, 7010, 7020, 7030, 7040]),
                    ("opt", option, ["13:55", "14:00", "14:55", "15:00"], [50, 60, 70, 80])):
                    destination = root / "kline" / kind / "20260619"
                    destination.mkdir(parents=True)
                    pd.DataFrame(dict(symbol=symbol, trade_date="2026-06-19",
                        datetime=pd.to_datetime(["2026-06-19 " + stamp for stamp in stamps]),
                        open=values, high=values, low=values, close=values, volume=100)).to_feather(
                            destination / f"{symbol}_20260619.feather")
                positions = root / "targets.csv"
                positions.write_text("timestamp,instrument_id,instrument_type,target_qty\n"
                    f"2026/6/19 {signal},IM2607,FUTURE,1\n"
                    f"2026/6/19 {signal},{option},OPTION,-2\n" + (
                    "2026/6/19 14:55,IM2607,FUTURE,1\n"
                    f"2026/6/19 14:55,{option},OPTION,0\n" if terminal is None else ""))
                settlements = root / "settlements.csv"
                if terminal == Decimal("120.13"):
                    settlements = root / "daily" / "opt"
                    settlements.mkdir(parents=True)
                    pd.DataFrame(dict(trade_date=pd.to_datetime(["2026-06-19"]), code=[option],
                        exchangeCD=["CCFX"], settlPrice=[float(terminal)], closePrice=[80.0])).to_feather(
                            settlements / "2026-06-19.feather")
                elif terminal is not None:
                    settlements.write_text("symbol,last_trade_date,settlement_price,available_at\n"
                        f"{option},2026-06-19,{terminal},2026-06-19T16:00:00+08:00\n")
                args = module.parse_args(["--positions", str(positions), "--start-day", "2026-06-19",
                    "--end-day", "2026-06-19", "--data-root", str(root),
                    "--option-commission-per-contract", "0", "--commission-per-contract", "0",
                    "--report-dir", str(root / "reports"), "--log-level", "ERROR", "--require-fills"] + (
                    ["--option-settlements", str(settlements)] if terminal is not None else []))
                output = module.run_case(args)
                # 提前回收已释放的引擎，验证下一组不会重新注册全局日志器。
                gc.collect()
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["report_errors"], [])
                self.assertEqual(summary["untriggered_slots"], [])
                self.assertEqual(summary["settlement_prices_loaded"], 0 if terminal is None else 1)
                self.assertEqual(summary["intervals"], {"IM2607": "1-MINUTE", option: "5-MINUTE"})
                self.assertEqual(Decimal(summary["framework_positions"][option]), 0)
                self.assertEqual(Decimal(summary["strategy_positions"][option]), 0)
                self.assertEqual(Decimal(summary["framework_positions"]["IM2607"]), 1)
                fills = pd.read_csv(output / "fills_output.csv")
                fut = fills.loc[fills.instrument_id.eq("IM2607.CFFEX")]
                opt = fills.loc[fills.instrument_id.eq(option + ".CFFEX")]
                self.assertEqual(len(fut), 1)
                self.assertEqual(fut.timestamp.iloc[0], "2026-06-19 " + fut_fill + ":00")
                self.assertEqual(len(opt), 2)
                close_time = "15:00" if terminal is None else "16:00"
                close_price = Decimal(80) if terminal is None else terminal
                self.assertEqual(list(opt.timestamp), ["2026-06-19 " + opt_fill + ":00", "2026-06-19 " + close_time + ":00"])
                self.assertEqual(Decimal(str(opt.price.iloc[0])), entry)
                self.assertEqual(Decimal(str(opt.price.iloc[1])), close_price)
                actual = (Decimal(str(opt.price.iloc[0])) - Decimal(str(opt.price.iloc[1]))) * 2 * 100
                self.assertEqual(actual, (Decimal(entry) - close_price) * 200)
                self.assertEqual(summary["settlements"], 0 if terminal is None else 1)
                if terminal is not None:
                    settled = pd.read_csv(output / "settlements.csv")
                    self.assertEqual(len(settled), 1)
                native_positions = pd.read_csv(output / "positions.csv")
                row = native_positions.loc[native_positions.instrument_id.eq(option + ".CFFEX")].iloc[0]
                self.assertEqual(str(row["side"]), "FLAT")
                self.assertEqual(Decimal(str(row["realized_pnl"]).split()[0].replace(",", "")), actual)
                account = pd.read_csv(output / "account.csv")
                # 期货尚未平仓，浮盈不计入已实现现金；期权已按终值完整入账。
                self.assertEqual(Decimal(str(account["total"].iloc[-1]).replace(",", "")),
                                 Decimal("40000000") + actual)


if __name__ == "__main__":
    unittest.main()
