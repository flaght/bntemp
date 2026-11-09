"""08完整原生链路验收；需安装Bomber扩展，使用临时真实Feather和特征包。"""
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

try:
    import bomber.backtest.engine  # noqa: F401
except ImportError as error:
    NATIVE_ERROR = str(error)
else:
    NATIVE_ERROR = None


@unittest.skipIf(NATIVE_ERROR is not None, f"需要Bomber编译扩展: {NATIVE_ERROR}")
class VegaNativeSettlementTests(unittest.TestCase):
    def fixture(self, root, terminal, *, option_bar_minutes=1, execution_timing="native",
                future_hedge=False, future_terminal=Decimal("6900.13")):
        module = import_module("demos.08_option_vega.run_backtest")
        features = import_module("demos.08_option_vega.feature")
        from bomber.framework.datahub.features import ExternalDataRecord
        from bomber.framework.dataprep.features import DEFAULT_FEATURE_CONFIG, export_features
        role = root / "role"
        role.mkdir()
        options = ("MO2609-C-7000", "MO2610-C-7000")
        expiries = ("2026-09-18", "2026-10-16")
        pd.DataFrame([dict(symbol="IM"+code[2:6], code="IM", exchangeCD="CCFX", currencyCD="CNY",
            contMultNum=200, minChgPriceNum=.2, listDate="2026-01-01", lastTradeDate=expiry,
            trade_fee_per_contract=0, trade_fee_rate=0) for code, expiry in zip(options, expiries)]
        ).to_feather(role / "fut_basic.feather")
        pd.DataFrame([dict(symbol=code, contractType="CO", strikePrice=7000, contMultNum=100,
            varTicker="000852", exchangeCD="CCFX", currencyCD="CNY", listDate="2026-01-01",
            lastTradeDate=expiry, expDate=expiry, tickNum=.2) for code, expiry in zip(options, expiries)]
        ).to_feather(role / "opt_basic.feather")
        calendar = root / "calendar.csv"
        pd.DataFrame({"date": pd.bdate_range("2026-09-17", "2026-10-16"),
            "is_trading_day": True}).to_csv(calendar, index=False)
        days = ("2026-09-17", "2026-09-18", "2026-09-21")
        clocks = (("14:00", "14:01", "14:05", "14:06", "14:50", "14:51", "14:55", "15:00")
            if option_bar_minutes == 5 else ("13:58", "13:59", "14:50", "14:51", "14:52"))
        index_path = root / "index.csv"
        bundle, binding = features.index_bundle(DEFAULT_FEATURE_CONFIG, "index.minute", 1, file_path=index_path)
        records = []
        for day in days:
            stamps = pd.to_datetime([f"{day} {clock}" for clock in clocks])
            for code, expiry in zip(options, expiries):
                if day > expiry:
                    continue
                for kind, symbol, price in (("opt", code, 50), ("fut", "IM"+code[2:6], 6900)):
                    destination = root / "kline" / kind / day.replace("-", "")
                    destination.mkdir(parents=True, exist_ok=True)
                    market_stamps = (stamps[stamps.minute % 5 == 0]
                        if option_bar_minutes == 5 and kind == "opt" else stamps)
                    pd.DataFrame(dict(symbol=symbol, trade_date=day, datetime=market_stamps,
                        open=price, high=price, low=price, close=price, volume=100)).to_feather(
                            destination / f"{symbol}_{day.replace('-', '')}.feather")
            for stamp in stamps:
                event = int(stamp.tz_localize("Asia/Shanghai").value)
                payload = binding.spec.payload_type(open=Decimal(6800), high=Decimal(6800),
                    low=Decimal(6800), close=Decimal(6800), volume=Decimal(0), turnover=Decimal(0),
                    prev_close=None, pct_change=None)
                records.append(ExternalDataRecord("index.minute", 1, "000852", payload,
                    event, event, event, 0, "fixture", received_ns=event))
        export_features(index_path, bundle.registry, records)
        settlements = root / "settlements.csv"
        if terminal is not None:
            settlements.write_text("symbol,last_trade_date,settlement_price,available_at,fee_per_contract\n"
                f"{options[0]},2026-09-18,{terminal},2026-09-18T16:00:00+08:00,1.2\n")
        future_settlements = root / "future-settlements.csv"
        if future_hedge and future_terminal is not None:
            future_settlements.write_text("symbol,last_trade_date,settlement_price,available_at,fee_per_contract\n"
                f"IM2609,2026-09-18,{future_terminal},2026-09-18T16:00:00+08:00,0.5\n")
        cli = ["run_backtest.py", "--data-root", str(root), "--calendar", str(calendar),
            "--index-feature-file", str(index_path), "--start-day", days[0], "--end-day", days[-1],
            "--min-remaining-days", "1", "--close-remaining-days", "0", "--nav", "2000000",
            "--delta-targets", "0.25", "--max-option-lots", "1", "--max-total-option-lots", "2",
            "--hedge-slots", "09:00", "--report-dir", str(root / "reports"), "--no-tearsheet",
            "--log-level", "ERROR", "--require-fills"]
        cli.extend(["--option-bar-minutes", str(option_bar_minutes),
            "--execution-timing", execution_timing])
        if option_bar_minutes == 5:
            cli.extend(["--option-slot", "14:00", "--opt-dir", str(root / "kline/opt")])
        if terminal is not None:
            cli.extend(["--option-settlements", str(settlements)])
        if future_hedge:
            cli.extend(["--nav", "40000000", "--max-option-lots", "10", "--max-total-option-lots", "20",
                "--hedge-slots", "14:50"])
            if future_terminal is not None:
                cli.extend(["--future-settlements", str(future_settlements)])
        with patch.object(module, "run_case") as capture, patch("sys.argv", cli):
            module.main()
        return module, capture.call_args.args[0]

    def test_zero_and_nonzero_expiry_settlement_then_continue_to_next_day(self):
        for terminal in (Decimal(0), Decimal("120.13")):
            with self.subTest(terminal=terminal), TemporaryDirectory() as directory:
                module, args = self.fixture(Path(directory), terminal)
                output = module.run_case(args)
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["settlements"], 1)
                self.assertEqual(summary["remaining_positions_or_working"], {})
                self.assertEqual(summary["report_errors"], [])
                settled = pd.read_csv(output / "settlements.csv")
                self.assertEqual(Decimal(str(settled.price.iloc[0])), terminal)
                self.assertEqual(Decimal(str(settled.commission.iloc[0])), Decimal("1.2"))
                self.assertEqual(int(settled.ts_event.iloc[0]),
                    int(pd.Timestamp("2026-09-18 16:00", tz="Asia/Shanghai").value))
                signals = pd.read_csv(output / "signals.csv")
                self.assertEqual(sum(signals.action.eq("OPTION_CASH_SETTLEMENT")), 1)

    def test_missing_final_value_rejects_actual_expiry_residual(self):
        with TemporaryDirectory() as directory:
            module, args = self.fixture(Path(directory), None)
            with self.assertRaisesRegex(RuntimeError, "Missing final settlement.*MO2609-C-7000"):
                module.run_case(args)

    def test_m5_options_m1_futures_next_market_and_cash_settlement(self):
        with TemporaryDirectory() as directory:
            module, args = self.fixture(Path(directory), Decimal(0),
                option_bar_minutes=5, execution_timing="next_market")
            from bomber.framework.trader.assembly import assemble_strategy
            with patch("bomber.framework.trader.assembly.assemble_strategy", wraps=assemble_strategy) as assemble:
                output = module.run_case(args)
            assemble.assert_called_once()
            bindings = assemble.call_args.kwargs["bindings"]
            self.assertEqual({binding.data_key: binding.bar_spec for binding in bindings.data}, {
                "MO2609-C-7000": "5-MINUTE", "MO2610-C-7000": "5-MINUTE",
                "IM2609": "1-MINUTE", "IM2610": "1-MINUTE", "000852": "1-MINUTE"})
            self.assertEqual({route.target_key for route in bindings.execution}, {
                "MO2609-C-7000.CFFEX", "MO2610-C-7000.CFFEX", "IM2609.CFFEX", "IM2610.CFFEX"})
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["bar_specs"]["option"]["bar_spec"], "5-MINUTE")
            self.assertEqual(summary["bar_specs"]["future"]["bar_spec"], "1-MINUTE")
            self.assertEqual(summary["execution_timing"], "next_market")
            self.assertEqual(summary["settlements"], 1)
            self.assertGreater(summary["fills"], 0)
            self.assertEqual(summary["remaining_positions_or_working"], {})
            fills = pd.read_csv(output / "fills.csv")
            option_fills = fills.loc[fills.instrument_id.str.contains("-C-")]
            times = pd.to_datetime(option_fills.ts_event, utc=True).dt.tz_convert("Asia/Shanghai")
            self.assertTrue((times.dt.minute % 5 == 0).all())
            self.assertFalse(times.dt.strftime("%H:%M").eq("14:00").any())

    def test_filled_future_hedge_cash_settles_and_next_day_runs_without_expired_quotes(self):
        with TemporaryDirectory() as directory:
            module, args = self.fixture(Path(directory), Decimal(0), option_bar_minutes=5,
                execution_timing="next_market", future_hedge=True)
            output = module.run_case(args)
            summary = json.loads((output / "summary.json").read_text())
            settled = pd.read_csv(output / "future_settlements.csv")
            self.assertEqual(summary["future_settlement_count"], 1)
            self.assertEqual(settled.instrument_id.tolist(), ["IM2609.CFFEX"])
            self.assertGreater(Decimal(str(settled.quantity.iloc[0])), 0)
            self.assertEqual(settled.side.iloc[0], "SELL")
            self.assertEqual(Decimal(str(settled.price.iloc[0])), Decimal("6900.13"))
            self.assertEqual(int(settled.ts_event.iloc[0]), int(pd.Timestamp("2026-09-18T16:00:00+08:00").value))
            self.assertEqual(summary["remaining_positions_or_working"], {})
            self.assertEqual(summary["report_errors"], [])
            signals = pd.read_csv(output / "signals.csv")
            self.assertEqual(sum(signals.action.eq("FUTURE_CASH_SETTLEMENT")), 1)

    def test_filled_future_hedge_requires_official_final_price(self):
        with TemporaryDirectory() as directory:
            module, args = self.fixture(Path(directory), Decimal(0), option_bar_minutes=5,
                execution_timing="next_market", future_hedge=True, future_terminal=None)
            with self.assertRaisesRegex(RuntimeError, "Missing final settlement for held future IM2609"):
                module.run_case(args)


if __name__ == "__main__":
    unittest.main()
