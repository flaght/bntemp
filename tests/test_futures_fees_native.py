"""服务器原生验收：真实Feather、Bomber扩展和撮合账户，不安装SDK替身。"""
from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from bomber.backtest.config import BacktestEngineConfig
from bomber.backtest.models import FeeModel
from bomber.config import LoggingConfig
from bomber.model import Venue
from bomber.model.currencies import CNY
from bomber.model.identifiers import InstrumentId, TraderId
from bomber.model.instruments import FuturesContract
from bomber.framework.datahub.future_basic import FutureBasic
from bomber.framework.dataprep.futures import make_instrument
from bomber.framework.dataprep.session import InputSession, read_feather
from bomber.framework.market.basic.base import make_bar
from bomber.framework.trader import (
    CtpCommissionRule, CtpExecutionAccounting, CtpFuturesBasicProfile, CtpPositionLedger,
    ExecutionReport, ExecutionReportType, NautilusSimExecutionBackend, OrderIntent,
    OrderSide, PositionEffect,
)


def basic_row(symbol="rb2704", *, value=2.5, unit="元/手"):
    return {"symbol": symbol, "code": symbol[:2].upper(), "exchangeCD": "XSGE",
        "currencyCD": "CNY", "listDate": "2026-01-01", "lastTradeDate": "2027-04-30",
        "contMultNum": 10, "minChgPriceNum": 1, "tradeCommiNum": value,
        "tradeCommiUnit": unit, "deliCommiNum": 999.0, "deliCommiUnit": "元/手"}


class NativeFuturesFeeTests(unittest.TestCase):
    def setUp(self):
        destination = os.environ.get("FUTURES_FEES_ARTIFACT_DIR")
        if destination:
            self.output = Path(destination) / self._testMethodName
            self.output.mkdir(parents=True, exist_ok=False)
        else:
            temporary = TemporaryDirectory(prefix="futures-fees-native-")
            self.addCleanup(temporary.cleanup)
            self.output = Path(temporary.name)

    def read_basics(self, rows):
        path = self.output / "fut_basic.feather"
        pd.DataFrame(rows).to_feather(path)
        with InputSession() as session:
            frame = read_feather(path)
            read_feather(path)
            self.assertEqual(session.counters["feather_reads"], 1)
        return frame.to_dict("records")

    def test_feather_roundtrip_and_native_instruments(self):
        rows = self.read_basics([basic_row(), basic_row("hc2704", value=0.0045, unit="%")])
        profile = CtpFuturesBasicProfile(commission_source="metadata")
        for row in rows:
            terms = FutureBasic.from_mapping(row)
            contract, _, multiplier = make_instrument(row, profile)
            self.assertIsInstance(contract, FuturesContract)
            self.assertEqual(multiplier, 10)
            self.assertEqual(contract.info["tradeCommiUnit"], row["tradeCommiUnit"])
            if row["tradeCommiUnit"] == "%":
                self.assertEqual(terms.trade_fee_rate, Decimal("0.000045"))
                self.assertEqual(Decimal(contract.info["trade_fee_rate"]), Decimal("0.000045"))
            else:
                self.assertEqual(terms.trade_fee_per_contract, Decimal("2.5"))
            self.assertEqual(terms.delivery_fee.per_contract, 999)

    def roundtrip(self, rows, expected_per_fill, **profile_options):
        profile = CtpFuturesBasicProfile(starting_balance="1000000",
            commission_per_contract="2.5", **profile_options)
        self.assertIsInstance(profile.fee_model, FeeModel)
        backend = NautilusSimExecutionBackend("futures-fees-native", BacktestEngineConfig(
            trader_id=TraderId("FEE-TESTER"), logging=LoggingConfig(log_level="ERROR"), run_analysis=False))
        self.addCleanup(backend.stop)
        backend.add_profile(profile)
        instruments = {}
        for row in self.read_basics(rows):
            contract, meta, _ = make_instrument(row, profile)
            backend.add_instrument(contract)
            instruments[str(contract.id)] = (contract, meta)
        start = int(datetime.fromisoformat("2026-10-26T09:00:00+08:00").timestamp()) * 1_000_000_000

        def bar(contract, meta, minute):
            return make_bar(contract.id, open=4000, high=4000, low=4000, close=4000,
                volume=100, ts_event=start + minute * 60_000_000_000, meta=meta)

        for contract, meta in instruments.values():
            backend.process_market_event(bar(contract, meta, 0))
        fills = []
        # 固定量价使手续费、现金扣费有独立的确定预期；不是行情真实性验收。
        for minute, side, effect in ((1, OrderSide.BUY, PositionEffect.OPEN),
                                     (2, OrderSide.SELL, PositionEffect.CLOSE)):
            for key, (contract, meta) in instruments.items():
                before = len(backend.reports)
                backend.submit_order(OrderIntent(strategy_id="native-fee-test",
                    backend_id=backend.backend_id, instrument_id=contract.id,
                    side=side, quantity=2, position_effect=effect))
                backend.process_market_event(bar(contract, meta, minute))
                reports = [report for report in backend.reports[before:]
                    if report.report_type in {ExecutionReportType.PARTIALLY_FILLED, ExecutionReportType.FILLED}]
                self.assertEqual(len(reports), 1, f"{key}: 应有一次完整原生成交，实际为{reports}")
                report = reports[0]
                self.assertEqual(report.filled_quantity, 2)
                self.assertEqual(report.fill_price, 4000)
                commission = Decimal(str(report.metadata["commission"]).replace("_", "").split()[0])
                self.assertEqual(commission, expected_per_fill[key])
                fills.append({"instrument_id": key, "effect": effect.value,
                    "price": str(report.fill_price), "quantity": str(report.filled_quantity),
                    "commission": str(commission)})
        expected_total = sum(expected_per_fill.values(), Decimal(0)) * 2
        for contract, _ in instruments.values():
            self.assertEqual(Decimal(str(backend.engine.portfolio.net_position(contract.id))), 0)
        account = backend.engine.portfolio.account(profile.venue)
        commission = account.commissions().get(CNY)
        actual_total = Decimal(0) if commission is None else commission.as_decimal()
        self.assertEqual(actual_total, expected_total)
        self.assertEqual(account.balance_total(CNY).as_decimal(), Decimal("1000000") - expected_total)
        self.assertEqual(len(profile.fee_model.records), len(fills))
        backend.write_commission_report(self.output)
        with (self.output / "commissions.csv").open(encoding="utf-8") as stream:
            audit = list(csv.DictReader(stream))
        self.assertEqual(len(audit), len(fills))
        self.assertEqual(sum((Decimal(row["actual_commission"]) for row in audit), Decimal(0)), expected_total)
        backend.finish()
        (self.output / "native_result.json").write_text(json.dumps({"fills": fills,
            "expected_commission": str(expected_total), "actual_commission": str(actual_total),
            "balance": str(account.balance_total(CNY).as_decimal()), "final_positions_zero": True},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_native_mixed_fees_overrides_and_cash_deduction(self):
        # rb: 2*2.5*1.2=6；hc: 4000*10*2*0.0045%*1.8=6.48。
        self.roundtrip([basic_row(), basic_row("hc2704", value=0.0045, unit="%")],
            {"rb2704.SHFE": Decimal("6.00"), "hc2704.SHFE": Decimal("6.48")},
            commission_source="metadata", commission_multiplier="1.2",
            commission_product_multipliers={"HC": "1.5"},
            commission_contract_multipliers={"hc2704.SHFE": "1.8"}, delivery_fee_multiplier=9)

    def test_native_zero_fee_is_explicit(self):
        self.roundtrip([basic_row(value=0, unit="%")], {"rb2704.SHFE": Decimal(0)},
            commission_source="metadata", commission_multiplier="1.2")

    def test_native_fixed_override(self):
        self.roundtrip([basic_row(value=0.0045, unit="%")], {"rb2704.SHFE": Decimal("6.00")},
            commission_source="fixed", commission_multiplier="1.2")

    def test_native_legacy_fallback(self):
        row = basic_row()
        del row["tradeCommiNum"], row["tradeCommiUnit"]
        self.roundtrip([row], {"rb2704.SHFE": Decimal("6.00")},
            commission_source="auto", commission_multiplier="1.2")

    def test_native_metadata_missing_rejected_before_fills(self):
        row = basic_row()
        del row["tradeCommiNum"], row["tradeCommiUnit"]
        profile = CtpFuturesBasicProfile(commission_source="metadata")
        contract, _, _ = make_instrument(row, profile)
        backend = NautilusSimExecutionBackend("missing-fees", BacktestEngineConfig(
            trader_id=TraderId("FEE-MISSING"), logging=LoggingConfig(log_level="ERROR")))
        self.addCleanup(backend.stop)
        backend.add_profile(profile)
        with self.assertRaisesRegex(ValueError, "缺少交易手续费"):
            backend.add_instrument(contract)
        self.assertEqual(backend.reports, ())

    def test_ctp_effect_rules_with_native_identity(self):
        identity = InstrumentId.from_str("rb2704.SHFE")
        ledger = CtpPositionLedger("20261026")
        rule = CtpCommissionRule(open_per_contract=1, close_today_per_contract=5,
            open_rate="0.000045", close_today_rate="0.000135", commission_multiplier="1.2")
        accounting = CtpExecutionAccounting(ledger, {identity: 10}, {identity: rule})
        for index, side, effect in ((1, OrderSide.BUY, PositionEffect.OPEN),
                                    (2, OrderSide.SELL, PositionEffect.CLOSE_TODAY)):
            accounting.on_report(ExecutionReport(backend_id="native-rule", client_order_id=f"O-{index}",
                instrument_id=identity, report_type=ExecutionReportType.FILLED,
                ts_event=index, filled_quantity=2, fill_price=4000,
                order_side=side, order_quantity=2, position_effect=effect))
        self.assertEqual(accounting.commissions, [Decimal("6.72"), Decimal("24.96")])
        self.assertEqual(accounting.total_commission, Decimal("31.68"))
        self.assertEqual(ledger.snapshot(identity).net_position, 0)


if __name__ == "__main__":
    unittest.main()
