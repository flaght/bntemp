"""期货行情数值提取的精度、单位和数据质量边界。"""
from decimal import Decimal
from types import SimpleNamespace
import unittest

from bomber.framework.market.futures import futures_bar_values


class NativeNumber:
    def __init__(self, value):
        self.value = value

    def as_double(self):
        return self.value


def bar_event(**overrides):
    native = dict(open=100.0, high=102.0, low=99.0, close=101.0, volume=2.0)
    factors = {"value": 2020.0, "openint": 20.0, **overrides}
    return SimpleNamespace(bar=SimpleNamespace(**{
        key: NativeNumber(value) for key, value in native.items()}), factors=factors)


class FuturesBarValuesTests(unittest.TestCase):
    def test_native_fallback_and_new_result_without_mutating_input(self):
        event = bar_event()
        original = dict(event.factors)
        self.assertEqual(futures_bar_values(event, contract_multiplier=10), {
            "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0,
            "volume": 2.0, "value": 2020.0, "openint": 20.0, "vwap": 101.0})
        self.assertEqual(event.factors, original)

    def test_raw_precision_takes_priority_without_accessing_native_numbers(self):
        event = bar_event(open="100.123", high="102.456", low="99.789",
                          close="101.234", volume="2.5", value="2530.85")
        event.bar = object()  # 全部原始字段齐全时，无需访问原生 OHLCV。
        result = futures_bar_values(event, contract_multiplier=Decimal(10))
        self.assertEqual(result["open"], 100.123)
        self.assertEqual(result["volume"], 2.5)
        self.assertAlmostEqual(result["vwap"], 101.234)

    def test_open_interest_alias_and_canonical_priority(self):
        event = bar_event(open_interest=30)
        del event.factors["openint"]
        self.assertEqual(futures_bar_values(event, contract_multiplier=10)["openint"], 30)
        event.factors["openint"] = 40
        self.assertEqual(futures_bar_values(event, contract_multiplier=10)["openint"], 40)

    def test_multiplier_changes_units_without_any_product_configuration(self):
        for multiplier in ("2", Decimal(10), 100.0):
            with self.subTest(multiplier=multiplier):
                event = bar_event()
                event.factors["value"] = 101 * 2 * float(multiplier)
                self.assertEqual(futures_bar_values(
                    event, contract_multiplier=multiplier)["vwap"], 101)
        for multiplier in (0, -1, "NaN", "Infinity", "bad", None, True):
            with self.subTest(multiplier=multiplier), self.assertRaises(ValueError):
                futures_bar_values(bar_event(), contract_multiplier=multiplier)

    def test_missing_required_fields_and_zero_volume_are_rejected(self):
        for field in ("value", "openint"):
            event = bar_event()
            del event.factors[field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "缺少原始字段"):
                futures_bar_values(event, contract_multiplier=10)
        with self.assertRaisesRegex(ValueError, "成交量为零"):
            futures_bar_values(bar_event(volume=0), contract_multiplier=10)

    def test_invalid_raw_values_are_rejected_before_vwap_arithmetic(self):
        for field, value in (("volume", float("nan")), ("volume", float("inf")),
                             ("volume", -1), ("value", float("nan")), ("value", -1),
                             ("openint", -1), ("openint", float("inf")),
                             ("close", 0), ("open", -1)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                futures_bar_values(bar_event(**{field: value}), contract_multiplier=10)

    def test_malformed_numbers_report_field_as_value_error(self):
        fields = ("open", "high", "low", "close", "volume", "value", "openint", "vwap")
        for field in fields:
            for value in (None, "bad", float("nan"), 10 ** 1000):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(
                        ValueError, f"字段 {field} 必须为有限数值"):
                    futures_bar_values(bar_event(**{field: value}), contract_multiplier=10)
        event = bar_event(open_interest=None)
        del event.factors["openint"]
        with self.assertRaisesRegex(ValueError, "字段 open_interest"):
            futures_bar_values(event, contract_multiplier=10)

    def test_turnover_is_not_assumed_to_be_per_bar_value(self):
        event = bar_event(turnover=2020)
        del event.factors["value"]
        with self.assertRaisesRegex(ValueError, "缺少原始字段.*value"):
            futures_bar_values(event, contract_multiplier=10)

    def test_ohlc_bounds_and_supplied_vwap_agreement(self):
        for factors in (dict(value=5000), dict(open=103), dict(high=98),
                        dict(low=103), dict(vwap=100), dict(vwap=float("nan"))):
            with self.subTest(factors=factors), self.assertRaises(ValueError):
                event = bar_event()
                event.factors.update(factors)
                futures_bar_values(event, contract_multiplier=10)
        self.assertEqual(futures_bar_values(
            bar_event(vwap=101.00001), contract_multiplier=10)["vwap"], 101)


if __name__ == "__main__":
    unittest.main()
