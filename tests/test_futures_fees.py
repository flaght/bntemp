"""费用契约及真实Python适配器验证；SDK实体替身不代表原生撮合验收。"""
from argparse import ArgumentParser
from dataclasses import asdict
from decimal import Decimal
from importlib import import_module
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch
from unittest.mock import Mock
import pandas as pd

from bomber.framework.fees import CommissionConfig, FuturesFee
from bomber.framework.datahub import AsOfQuery, FutureBasicProvider, ReferenceRecord
from bomber.framework.datahub.future_basic import FutureBasic
from bomber.framework.dataprep.commissions import add_commission_arguments, commission_config_from_args
from bomber.framework.dataprep.metadata import parse_future_terms, future_spec

ROOT = Path(__file__).resolve().parents[1]


def basic_row(**kwargs):
    return dict(symbol="rb2704", code="RB", exchangeCD="XSGE", currencyCD="CNY",
        listDate="2026-01-01", lastTradeDate="2027-04-30", contMultNum=10,
        minChgPriceNum=1, **kwargs)


class FeeContractTests(unittest.TestCase):
    def test_fixed_and_percentage_with_multiplier(self):
        fixed = FuturesFee.from_source("2.5000", "元/手")
        ratio = FuturesFee.from_source("0.0045", "%")
        self.assertEqual(fixed.charge(2, commission_multiplier="1.2").actual_commission, Decimal("6.00"))
        self.assertEqual(ratio.rate, Decimal("0.000045"))
        charge = ratio.charge(2, 4000, 10, commission_multiplier="1.2")
        self.assertEqual(charge.standard_commission, Decimal("3.600000"))
        self.assertEqual(charge.actual_commission, Decimal("4.32"))
        self.assertEqual((ratio.source_value, ratio.source_unit), (Decimal("0.0045"), "%"))

    def test_coefficient_precedes_rounding_and_can_be_below_one(self):
        charge = FuturesFee(rate="0.000045").charge(1, 3500, 10, commission_multiplier="1.2")
        self.assertEqual(charge.standard_commission, Decimal("1.575000"))
        self.assertEqual(charge.actual_commission, Decimal("1.89"))
        self.assertEqual(FuturesFee(per_contract=2).charge(1, commission_multiplier="0.8").actual_commission,
                         Decimal("1.60"))

    def test_combined_fees_and_zero_fee(self):
        self.assertEqual(FuturesFee("2.5", "0.000045").charge(2, 4000, 10,
            commission_multiplier="1.2").actual_commission, Decimal("10.32"))
        self.assertEqual(FuturesFee.from_source(0, "%").charge(2).actual_commission, 0)

    def test_reject_incomplete_unknown_and_conflicting_units(self):
        for row in ({"tradeCommiNum": 2}, {"tradeCommiUnit": "%"},
                    {"tradeCommiNum": 2, "tradeCommiUnit": "元"},
                    {"trade_fee_rate": "0.01"},
                    {"tradeCommiNum": 2, "tradeCommiUnit": "元/手",
                     "trade_fee_per_contract": 3, "trade_fee_rate": 0}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                FuturesFee.from_mapping(row)
        self.assertIsNone(FuturesFee.from_mapping({}))
        self.assertIsNone(FuturesFee.from_mapping({"tradeCommiNum": None, "tradeCommiUnit": None}))

    def test_reject_invalid_numbers_and_missing_notional_inputs(self):
        for value in ("NaN", "Infinity", "-1", "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                FuturesFee.from_source(value, "%")
        for value in (0, -1, "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CommissionConfig(commission_multiplier=value)
        for price, multiplier in ((None, 10), (4000, None), (0, 10), (4000, -1)):
            with self.subTest(price=price, multiplier=multiplier), self.assertRaises(ValueError):
                FuturesFee(rate="0.001").charge(1, price, multiplier)

    def test_override_priority_and_immutable_config(self):
        overrides = {"rb2704.SHFE": "1.8", "rb2705": "1.6"}
        config = CommissionConfig("1.2", {"rb": "1.5"}, overrides, delivery_fee_multiplier="2")
        overrides["rb2704.SHFE"] = "9"
        self.assertEqual(config.resolve("rb2704.SHFE"), Decimal("1.8"))
        self.assertEqual(config.resolve("RB2705.SHFE"), Decimal("1.6"))
        self.assertEqual(config.resolve("rb2706.SHFE"), Decimal("1.5"))
        self.assertEqual(config.resolve("cu2704.SHFE"), Decimal("1.2"))
        with self.assertRaises(TypeError):
            config.commission_product_multipliers["RB"] = 2
        for kwargs in ({"commission_contract_multipliers": {"RB": 1}},
                       {"commission_product_multipliers": {"RB": 1, "rb": 2}},
                       {"delivery_fee_multiplier": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CommissionConfig(**kwargs)

    def test_cli_configuration_and_duplicate_rejection(self):
        parser = ArgumentParser()
        add_commission_arguments(parser)
        args = parser.parse_args(["--commission-multiplier", "1.2",
            "--commission-product-multiplier", "RB=1.5",
            "--commission-contract-multiplier", "rb2704.SHFE=1.8",
            "--commission-source", "metadata"])
        config = commission_config_from_args(args)
        self.assertEqual(config.resolve("rb2704.SHFE"), Decimal("1.8"))
        self.assertEqual(config.commission_source, "metadata")
        with self.assertRaises(ValueError):
            commission_config_from_args(parser.parse_args([
                "--commission-product-multiplier", "RB=1", "--commission-product-multiplier", "rb=2"]))

    def test_basic_shared_parsing_delivery_separation_and_round_trip(self):
        row = basic_row(tradeCommiNum="0.0045", tradeCommiUnit="%",
                        deliCommiNum="2.5", deliCommiUnit="元/手")
        direct = FutureBasic.from_mapping(row)
        terms = parse_future_terms(row)
        self.assertEqual(direct.trade_fee, terms.trade_fee)
        self.assertEqual(terms.trade_fee_rate, Decimal("0.000045"))
        self.assertEqual(terms.delivery_fee.per_contract, Decimal("2.5"))
        self.assertEqual(future_spec(row).trade_fee, terms.trade_fee)
        self.assertEqual(FutureBasic.from_mapping(asdict(terms)), terms)
        self.assertIsNone(FutureBasic.from_mapping(basic_row()).trade_fee)

    def test_fee_versions_keep_existing_asof_gate(self):
        first = FutureBasic.from_mapping(basic_row(tradeCommiNum=2, tradeCommiUnit="元/手"))
        second = FutureBasic.from_mapping(basic_row(tradeCommiNum=3, tradeCommiUnit="元/手"))
        provider = FutureBasicProvider((ReferenceRecord("future_basic", first.symbol, first, 10, 10, 10),
                                      ReferenceRecord("future_basic", second.symbol, second, 20, 20, 20)))
        self.assertEqual(provider.basic_at(first.symbol, AsOfQuery(15)).trade_fee_per_contract, 2)
        self.assertEqual(provider.basic_at(first.symbol, AsOfQuery(25)).trade_fee_per_contract, 3)

    def test_reference_export_preserves_raw_fee_fields(self):
        from bomber.framework.dataprep.sources.base import ReferenceDataset, ReferenceQuery
        from bomber.framework.dataprep.sources.normalize import normalize_frame
        row = basic_row(tradeCommiNum="0.0045", tradeCommiUnit="%")
        batch = normalize_frame(pd.DataFrame([row]), ReferenceDataset.FUTURES_BASIC,
            ReferenceQuery(products=("RB",)), source="test")
        self.assertEqual(batch.rows[0]["tradeCommiNum"], "0.0045")
        self.assertEqual(batch.rows[0]["tradeCommiUnit"], "%")
        with self.assertRaises(ValueError):
            normalize_frame(pd.DataFrame([{**row, "tradeCommiUnit": "元"}]),
                ReferenceDataset.FUTURES_BASIC, ReferenceQuery(products=("RB",)), source="test")


class Scalar(Decimal):
    @classmethod
    def from_str(cls, value):
        return cls(value)

    @classmethod
    def from_int(cls, value):
        return cls(value)

    def as_decimal(self):
        return Decimal(self)


class Identity(str):
    @classmethod
    def from_str(cls, value):
        return cls(value)


class Entity:
    def __init__(self, *args, **kwargs):
        if args:
            kwargs["instrument_id"] = args[0]
        self.__dict__.update(kwargs)
        if "instrument_id" in kwargs:
            self.id = kwargs["instrument_id"]


class Money:
    def __init__(self, amount, currency):
        self.amount, self.currency = amount, currency


def sdk_fixture():
    """仅替换SDK类型和未使用网关，导入实际Profile、费用模型和CTP账本。"""
    modules = {}
    for name in ("bomber.framework.trader", "bomber.framework.trader.execution",
                 "bomber.framework.trader.execution.ctp", "bomber.framework.trader.execution.simulation"):
        module = ModuleType(name)
        module.__path__ = [str(ROOT / name.replace(".", "/"))]
        modules[name] = module
    for name, values in {
        "bomber.backtest.models": {"FeeModel": type("FeeModel", (), {}), "StandardMarginModel": object},
        "bomber.backtest.config": {"BacktestEngineConfig": Entity},
        "bomber.backtest.engine": {"BacktestEngine": Entity},
        "bomber.model": {"Money": Money, "Price": Scalar, "Quantity": Scalar, "Symbol": str,
                         "Venue": str, "Currency": str},
        "bomber.model.currencies": {"CNY": "CNY"},
        "bomber.model.identifiers": {"InstrumentId": Identity},
        "bomber.model.instruments": {"FuturesContract": type("FuturesContract", (Entity,), {})},
        "bomber.model.enums": {"AccountType": NS(MARGIN="MARGIN"), "AssetClass": NS(COMMODITY="COMMODITY"),
            "OmsType": NS(NETTING="NETTING", HEDGING="HEDGING"), "BookType": NS(L1_MBP="L1_MBP")},
        "bomber.core.rust.model": {"OtoTriggerMode": NS(PARTIAL="PARTIAL")},
        "bomber.framework.market.basic.base": {"InstrumentId": Identity, "InstrumentMeta": Entity,
            "DataType": NS(BAR="BAR", CUSTOM_BAR="CUSTOM_BAR", TRADE_TICK="TRADE_TICK", QUOTE_TICK="QUOTE_TICK")},
        "bomber.framework.trader.execution.simulation.gateway": {"NautilusOrderGateway": Entity},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        modules[name] = module
    return modules


class FeeAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = patch.dict(sys.modules, sdk_fixture())
        cls.fixture.start()
        cls.addClassCleanup(cls.fixture.stop)
        cls.profile = import_module("bomber.framework.trader.execution.simulation.ctp")
        cls.rules = import_module("bomber.framework.trader.execution.ctp.profile")
        cls.ledger = import_module("bomber.framework.trader.execution.ctp.ledger")
        cls.accounting = import_module("bomber.framework.trader.execution.ctp.accounting")
        cls.contracts = import_module("bomber.framework.trader.execution.contracts")
        cls.backend = import_module("bomber.framework.trader.execution.simulation.backend")

    def instrument(self, row, profile):
        from bomber.framework.dataprep.futures import make_instrument
        return make_instrument(row, profile)[0]

    def test_shared_venue_mixed_fees_and_contract_override(self):
        profile = self.profile.CtpFuturesBasicProfile(commission_multiplier="1.2",
            commission_contract_multipliers={"rb2704.SHFE": "1.5"})
        ratio = self.instrument(basic_row(tradeCommiNum="0.0045", tradeCommiUnit="%"), profile)
        fixed = self.instrument({**basic_row(tradeCommiNum="2.5", tradeCommiUnit="元/手"),
                                 "symbol": "rb2705"}, profile)
        model = profile.build_backend_config()["fee_model"]
        order = NS(client_order_id="O-1")
        self.assertEqual(model.get_commission(order, Scalar(2), Scalar(4000), ratio).amount, Decimal("5.40"))
        self.assertEqual(model.get_commission(order, Scalar(2), Scalar(4000), fixed).amount, Decimal("6.00"))
        self.assertEqual(ratio.info["tradeCommiNum"], "0.0045")
        self.assertEqual(ratio.info["trade_fee_rate"], "0.000045")
        self.assertEqual(model.records[0]["standard_commission"], Decimal("3.6"))

    def test_metadata_required_fixed_override_and_legacy_fallback(self):
        order = NS(client_order_id="O-2")
        for source, expected in (("auto", Decimal("2.40")), ("fixed", Decimal("2.40")), ("metadata", None)):
            profile = self.profile.CtpFuturesBasicProfile(commission_multiplier="1.2", commission_source=source)
            contract = self.instrument(basic_row(), profile)
            if expected is None:
                with self.assertRaisesRegex(ValueError, "缺少交易手续费"):
                    profile.fee_model.get_commission(order, Scalar(2), Scalar(4000), contract)
            else:
                self.assertEqual(profile.fee_model.get_commission(order, Scalar(2), Scalar(4000), contract).amount, expected)
        profile = self.profile.CtpFuturesBasicProfile(commission_source="fixed")
        contract = self.instrument(basic_row(tradeCommiNum=5, tradeCommiUnit="元/手"), profile)
        self.assertEqual(profile.fee_model.get_commission(order, Scalar(2), Scalar(4000), contract).amount, 2)

    def test_delivery_coefficient_does_not_change_trade_fee_and_parts_use_fill_quantity(self):
        profile = self.profile.CtpFuturesBasicProfile(commission_multiplier="1.2", delivery_fee_multiplier=9)
        contract = self.instrument(basic_row(tradeCommiNum="0.0045", tradeCommiUnit="%",
            deliCommiNum=999, deliCommiUnit="元/手"), profile)
        order = NS(client_order_id="split")
        fees = [profile.fee_model.get_commission(order, Scalar(1), Scalar(price), contract).amount
                for price in (4000, 4200)]
        self.assertEqual(fees, [Decimal("2.16"), Decimal("2.27")])
        self.assertEqual(contract.info["deliCommiNum"], "999")

    def test_high_fidelity_uses_effect_price_multiplier_and_one_coefficient(self):
        effect = self.contracts.PositionEffect
        rule = self.rules.CtpCommissionRule(open_per_contract=1, close_per_contract=2,
            close_today_per_contract=5, open_rate="0.000045", close_rate="0.00009",
            close_today_rate="0.000135", commission_multiplier="1.2")
        self.assertEqual(rule.calculate(effect.OPEN, 2, 4000, 10), Decimal("6.72"))
        self.assertEqual(rule.calculate(effect.CLOSE_YESTERDAY, 2, 4000, 10), Decimal("13.44"))
        self.assertEqual(rule.calculate(effect.CLOSE_TODAY, 2, 4000, 10), Decimal("24.96"))
        book = self.ledger.CtpPositionLedger("20261026")
        accounting = self.accounting.CtpExecutionAccounting(book, {"rb2704.SHFE": 10},
            {"rb2704.SHFE": rule}, commission_config=CommissionConfig(commission_multiplier="1.5"))
        report = self.contracts.ExecutionReport(backend_id="sim", client_order_id="O-3",
            instrument_id="rb2704.SHFE", report_type="FILLED", ts_event=1, filled_quantity=2,
            fill_price=4000, order_side="BUY", order_quantity=2, position_effect="OPEN")
        accounting.on_report(report)
        # 1.5覆盖规则中的1.2，不能相乘两次。
        self.assertEqual(accounting.total_commission, Decimal("8.40"))
        self.assertEqual(accounting.commission_records[0]["commission_multiplier"], Decimal("1.5"))
        self.assertEqual(book.snapshot("rb2704.SHFE").long_today, 2)

    def test_legacy_rule_and_source_rule(self):
        rule = self.rules.CtpCommissionRule(1, 2, 5)
        self.assertEqual(rule.calculate("OPEN", 2), 2)
        self.assertEqual(rule.calculate("CLOSE_YESTERDAY", 3), 6)
        self.assertEqual(rule.calculate("CLOSE_TODAY", 1), 5)
        from_source = self.rules.CtpCommissionRule.from_mapping(
            {"tradeCommiNum": "0.0045", "tradeCommiUnit": "%"}, commission_multiplier="1.2")
        self.assertEqual(from_source.calculate("OPEN", 2, 4000, 10), Decimal("4.32"))
        with self.assertRaises(ValueError):
            from_source.calculate("OPEN", 2)

    def test_audit_csv_contains_standard_coefficient_and_actual(self):
        import csv
        import json
        profile = self.profile.CtpFuturesBasicProfile(commission_multiplier="1.2")
        contract = self.instrument(basic_row(tradeCommiNum="2.5", tradeCommiUnit="元/手"), profile)
        profile.fee_model.get_commission(NS(client_order_id="O-4"), Scalar(2), Scalar(4000), contract)
        backend = self.backend.NautilusSimExecutionBackend("audit", engine=NS())
        backend._profiles = {"SHFE": profile}
        with TemporaryDirectory() as folder:
            backend.write_commission_report(folder)
            with (Path(folder) / "commissions.csv").open() as stream:
                record = next(csv.DictReader(stream))
            self.assertEqual(Decimal(record["standard_commission"]), 5)
            self.assertEqual(Decimal(record["commission_multiplier"]), Decimal("1.2"))
            self.assertEqual(Decimal(record["actual_commission"]), 6)
            config = json.loads((Path(folder) / "commission_config.json").read_text())
            self.assertEqual(config["SHFE"]["commission_multiplier"], "1.2")

    def test_strict_metadata_is_validated_before_engine_registration(self):
        profile = self.profile.CtpFuturesBasicProfile(commission_source="metadata")
        contract = self.instrument(basic_row(), profile)
        engine = NS(add_instrument=Mock())
        backend = self.backend.NautilusSimExecutionBackend("strict", engine=engine)
        backend._profiles = {"SHFE": profile}
        with self.assertRaisesRegex(ValueError, "缺少交易手续费"):
            backend.add_instrument(contract)
        engine.add_instrument.assert_not_called()


if __name__ == "__main__":
    unittest.main()
