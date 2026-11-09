"""股指期货最终结算资料；只读临时文件，不连接柜台。"""
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

import pandas as pd

from bomber.framework.dataprep.future_settlements import load_future_settlements
from bomber.framework.dataprep.session import InputSession
from tests.test_option_by_pos_framework import environment, ns


class FutureSettlementInputTests(unittest.TestCase):
    def setUp(self):
        scope = environment()
        scope.__enter__()
        self.addCleanup(lambda: scope.__exit__(None, None, None))
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.day = date(2026, 5, 15)
        self.options = {"IM2605": NS(last_trade=self.day)}

    def frame(self):
        # 与quant.market_fut的固定14列一致；读取器只消费日期、代码、settlePrice等结算字段。
        return pd.DataFrame({"trade_date": [self.day]*2, "code": ["IM2605", "IM2606"],
            "secShortName": ["中证1000股指2605", "中证1000股指2606"], "exchangeCD": ["CCFX"]*2,
            "preSettlePrice": [6800., 6790.], "preClosePrice": [6799., 6789.],
            "openPrice": [6850., 6840.], "highestPrice": [6950., 6940.], "lowestPrice": [6800., 6790.],
            "closePrice": [6899., 6880.], "settlePrice": [6900.13, 6888.8],
            "turnoverVol": [10, 20], "turnoverValue": [100000., 200000.], "openInt": [30, 40]})

    def read(self, path=None, **kwargs):
        return load_future_settlements(path or self.root, self.options,
            start_day=date(2026, 5, 2), end_day=date(2026, 6, 21), **kwargs)

    def test_expiry_day_only_and_official_price_not_close(self):
        path = self.root / "2026-05-15.feather"
        self.frame().to_feather(path)
        (self.root / "2026-05-14.feather").write_text("not read")
        result = self.read()
        self.assertEqual(set(result), {"IM2605"})
        item = result["IM2605"]
        self.assertEqual(item.price, Decimal("6900.13"))
        self.assertEqual(item.available_ns, ns("2026-05-15 16:00"))
        self.assertEqual(item.last_trade_ns, ns("2026-05-15 15:00"))

    def test_fixed_daily_schema_ignores_other_products_and_unused_nullable_prices(self):
        frame = self.frame()
        commodity = frame.iloc[[0]].assign(code="zn2710", exchangeCD="XSGE", openPrice=None, highestPrice=None)
        frame = pd.concat([frame, commodity], ignore_index=True)
        frame.to_feather(self.root / "2026-05-15.feather")
        result = self.read()
        self.assertEqual(set(result), {"IM2605"})
        self.assertEqual(result["IM2605"].price, Decimal("6900.13"))

    def test_misspelled_price_column_is_never_used_as_fallback(self):
        frame = self.frame()
        frame["settlPrice"] = 1.0  # 历史拼写错误，不是固定格式中的settlePrice。
        path = self.root / "2026-05-15.feather"
        frame.to_feather(path)
        self.assertEqual(self.read()["IM2605"].price, Decimal("6900.13"))
        frame.drop(columns="settlePrice").to_feather(path)
        with self.assertRaisesRegex(ValueError, "settlePrice"):
            self.read()

    def test_standard_csv_preserves_publication_fee_and_expiry(self):
        path = self.root / "final.csv"
        path.write_text("symbol,last_trade_date,settlement_price,available_at,fee_per_contract\n"
            "IM2605,2026-05-15,6900.13,2026-05-15T17:30:00+08:00,1.25\n")
        item = self.read(path)["IM2605"]
        self.assertEqual(item.available_ns, ns("2026-05-15 17:30"))
        self.assertEqual(item.fee_per_contract, Decimal("1.25"))

    def test_missing_files_and_contracts_are_diagnosed_without_fabrication(self):
        with InputSession() as session:
            self.assertEqual(self.read(), {})
            self.assertEqual(session.issues[0].code, "FUTURE_SETTLEMENT_DAY_MISSING")
        self.frame().iloc[[1]].to_feather(self.root / "2026-05-15.feather")
        with InputSession() as session:
            self.assertEqual(self.read(), {})
            self.assertEqual(session.issues[0].code, "FUTURE_SETTLEMENT_CONTRACT_MISSING")

    def test_invalid_final_value_and_ordinary_close_are_rejected(self):
        for price in (0, -1, float("nan"), float("inf")):
            frame = self.frame()
            frame.loc[0, "settlePrice"] = price
            frame.to_feather(self.root / "2026-05-15.feather")
            with self.subTest(price=price), self.assertRaisesRegex(ValueError, "IM2605"):
                self.read()
        self.frame().drop(columns="settlePrice").to_feather(self.root / "2026-05-15.feather")
        with self.assertRaisesRegex(ValueError, "settlePrice"):
            self.read()

    def test_date_mismatch_and_duplicate_conflicts_rejected(self):
        frame = self.frame()
        frame["trade_date"] = date(2026, 5, 14)
        frame.to_feather(self.root / "2026-05-15.feather")
        with self.assertRaisesRegex(ValueError, "trade_date"):
            self.read()
        frame = pd.concat([self.frame(), self.frame().iloc[[0]].assign(settlePrice=7000)])
        frame.to_feather(self.root / "2026-05-15.feather")
        with self.assertRaisesRegex(ValueError, "Conflicting final"):
            self.read()

    def test_expiry_date_must_match_fut_basic(self):
        path = self.root / "final.csv"
        path.write_text("symbol,last_trade_date,settlement_price\nIM2605,2026-05-14,6900\n")
        with self.assertRaisesRegex(ValueError, "fut_basic"):
            self.read(path)

    def test_physical_delivery_product_is_not_implicitly_cash_settled(self):
        self.options = {"rb2610": NS(last_trade=self.day)}
        with self.assertRaisesRegex(ValueError, "IF/IH/IM"):
            self.read()


if __name__ == "__main__":
    unittest.main()
