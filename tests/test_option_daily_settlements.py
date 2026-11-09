"""按日原始期权 Feather → 到期终值；使用真实文件，不创建分钟 Bar。"""
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import pandas as pd

from bomber.framework.dataprep import option_settlements as inputs
from bomber.framework.dataprep.session import InputSession
from tests.test_option_by_pos_framework import environment, ns


class OptionDailySettlementTests(unittest.TestCase):
    def setUp(self):
        scope = environment()
        scope.__enter__()
        self.addCleanup(lambda: scope.__exit__(None, None, None))
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.day = date(2026, 6, 19)
        self.call, self.put = "MO2606-C-7000", "MO2606-P-7000"
        self.options = {symbol: NS(last_trade=self.day) for symbol in (self.call, self.put)}

    def frame(self):
        # 与用户日行情相同的14列，以及不连续的DataFrame索引。
        return pd.DataFrame(dict(trade_date=pd.to_datetime([self.day] * 3),
            code=["HO2606-C-3000", self.call, self.put], secShortName=["上证50购6月3000", "中证1000购", "中证1000沽"],
            exchangeCD=["CCFX"] * 3, preSettlePrice=[30.0, 40.0, 50.0],
            preClosePrice=[31.0, 41.0, 51.0], openPrice=[32.0, 42.0, 52.0],
            highestPrice=[35.0, 45.0, 55.0], lowestPrice=[30.0, 40.0, 50.0],
            closePrice=[34.0, 44.0, 54.0], settlePrice=[99.0, 120.13, 0.0],
            turnoverVol=[4.0, 0.0, 0.0], turnoverValue=[169720.0, 0.0, 0.0], openInt=[27, 2, 2]),
            index=[0, 10, 20])

    def write(self, frame=None, day=None):
        path = self.root / f"{(day or self.day).isoformat()}.feather"
        (self.frame() if frame is None else frame).to_feather(path)
        return path

    def read(self, **kwargs):
        return inputs.load_option_settlements(self.root, self.options,
            start_day=date(2026, 1, 1), end_day=date(2026, 7, 9), **kwargs)

    def test_reads_only_required_expiry_days_preserves_zero_and_ignores_other_products(self):
        path = self.write()
        ordinary = self.root / "2026-06-18.feather"
        ordinary.write_text("ordinary-day file must not be opened")
        self.options["MO2609-C-7000"] = NS(last_trade=date(2026, 9, 18))
        with InputSession() as session, patch.object(inputs, "read_feather", wraps=inputs.read_feather) as read:
            result = self.read()
            self.assertEqual(read.call_count, 1)
            self.assertEqual(read.call_args.args[0], path)
            self.assertEqual(set(session.manifest), {str(path)})
            self.assertEqual(session.issues, [])
        self.assertEqual(set(result), {self.call, self.put})
        self.assertEqual(result[self.call].price, Decimal("120.13"))
        self.assertEqual(result[self.put].price, 0)
        self.assertEqual(result[self.call].available_ns, ns("2026-06-19 16:00"))
        self.assertEqual(result[self.call].source, str(path))

    def test_explicit_publication_time_and_fee_are_preserved(self):
        frame = self.frame()
        frame["available_at"] = "2026-06-19T17:30:00+08:00"
        frame["fee_per_contract"] = 0.75
        self.write(frame)
        item = self.read(fee_per_contract=1)[self.call]
        self.assertEqual(item.available_ns, ns("2026-06-19 17:30"))
        self.assertEqual(item.fee_per_contract, Decimal("0.75"))

    def test_missing_day_is_diagnosed_and_left_to_actual_expiry_position_check(self):
        with InputSession() as session:
            self.assertEqual(self.read(), {})
            self.assertEqual([issue.code for issue in session.issues], ["OPTION_SETTLEMENT_DAY_MISSING"])
            self.assertEqual(session.issues[0].trading_day, self.day)

    def test_missing_contract_is_diagnosed_without_substituting_other_contract_price(self):
        self.write(self.frame().loc[[0, 10]])
        with InputSession() as session:
            result = self.read()
            self.assertEqual(set(result), {self.call})
            self.assertEqual(session.issues[0].code, "OPTION_SETTLEMENT_CONTRACT_MISSING")
            self.assertEqual(session.issues[0].symbol, self.put)

    def test_daily_filename_and_trade_date_must_match(self):
        frame = self.frame()
        frame.loc[10, "trade_date"] = pd.Timestamp("2026-06-18")
        self.write(frame)
        with self.assertRaisesRegex(ValueError, "trade_date differs"):
            self.read()

    def test_selected_contract_exchange_must_match(self):
        frame = self.frame()
        frame.loc[10, "exchangeCD"] = "XSHG"
        self.write(frame)
        with self.assertRaisesRegex(ValueError, "CFFEX"):
            self.read()

    def test_invalid_final_prices_are_rejected_instead_of_using_close_price(self):
        for value in (-1.0, float("nan"), float("inf")):
            with self.subTest(value=value):
                frame = self.frame()
                frame.loc[10, "settlePrice"] = value
                self.write(frame)
                with self.assertRaisesRegex(ValueError, self.call):
                    self.read()

    def test_missing_settlement_column_cannot_fall_back_to_close(self):
        self.write(self.frame().drop(columns="settlePrice"))
        with self.assertRaisesRegex(ValueError, "settlePrice"):
            self.read()

    def test_correct_settle_price_is_required_and_misspelling_is_ignored(self):
        frame = self.frame()
        frame["settlPrice"] = 1.0
        self.write(frame)
        self.assertEqual(self.read()[self.call].price, Decimal("120.13"))
        self.assertEqual(self.read()[self.put].price, Decimal(0))
        self.write(frame.drop(columns="settlePrice"))
        with self.assertRaisesRegex(ValueError, "settlePrice"):
            self.read()

    def test_duplicate_identical_prices_are_accepted_and_conflicts_are_rejected(self):
        frame = pd.concat([self.frame(), self.frame().loc[[10]]])
        self.write(frame)
        self.assertEqual(self.read()[self.call].price, Decimal("120.13"))
        frame.iloc[-1, frame.columns.get_loc("settlePrice")] = 125.0
        self.write(frame)
        with self.assertRaisesRegex(ValueError, "Conflicting final settlements"):
            self.read()


if __name__ == "__main__": unittest.main()
