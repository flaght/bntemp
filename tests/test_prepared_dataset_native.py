"""Server acceptance using real native matching, next_market and expiry settlement."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from tests import test_option_vega_native as native_fixture
from bomber.framework.dataprep.prepared import PreparedSource, build_prepared_dataset


def comparable_report(frame):
    """Exclude generated identities, retaining deterministic native trade IDs."""
    generated = {"client_order_id", "venue_order_id", "order_list_id", "position_id",
                 "trade_id", "opening_order_id", "closing_order_id", "event_id", "run_id"}
    result = frame.drop(columns=[column for column in frame
        if column in generated or column.startswith("Unnamed:")])
    if "last_trade_id" in result:
        # CashSettlementCoordinator generates OS/FS + uuid4.hex for settlements.
        # Keep the settlement kind and every ordinary matching trade ID intact.
        result["last_trade_id"] = result["last_trade_id"].str.replace(
            r"^(OS|FS)-[0-9a-f]{32}$", r"\1-<settlement>", regex=True)
    return result


class PreparedReportComparisonTests(unittest.TestCase):
    def test_settlement_uuid_suffixes_are_ignored_and_native_trade_ids_preserved(self):
        frame = pd.DataFrame({
            "last_trade_id": ["FS-"+"1"*32, "T-e1be3e77e5705ec8-003", "OS-"+"2"*32, ""],
            "price": ["6900.13", "50.0", "120.13", ""],
        })
        other = frame.copy()
        other.loc[0, "last_trade_id"] = "FS-"+"3"*32
        other.loc[2, "last_trade_id"] = "OS-"+"4"*32
        pd.testing.assert_frame_equal(comparable_report(frame), comparable_report(other))
        self.assertEqual(comparable_report(frame).last_trade_id.iloc[1], "T-e1be3e77e5705ec8-003")
        self.assertEqual(frame.last_trade_id.iloc[0], "FS-"+"1"*32)

    def test_financial_time_status_trade_identity_and_row_order_differences_still_fail(self):
        frame = pd.DataFrame({"last_trade_id": ["FS-"+"1"*32, "T-001"],
            "price": ["6900.13", "50"], "quantity": ["1", "2"],
            "commission": ["0.5", "1.2"], "ts_event": ["100", "200"],
            "status": ["FILLED", "FILLED"]})
        for column, value in (("last_trade_id", "OS-"+"1"*32),
                ("last_trade_id", "FS-not-a-uuid"), ("price", "6900.14"),
                ("quantity", "3"), ("commission", "0.6"),
                ("ts_event", "101"), ("status", "CANCELED")):
            other = frame.copy()
            other.loc[0, column] = value
            with self.subTest(column=column, value=value), self.assertRaises(AssertionError):
                pd.testing.assert_frame_equal(comparable_report(frame), comparable_report(other))
        other = frame.copy()
        other.loc[1, "last_trade_id"] = "T-002"
        with self.assertRaises(AssertionError):
            pd.testing.assert_frame_equal(comparable_report(frame), comparable_report(other))
        with self.assertRaises(AssertionError):
            pd.testing.assert_frame_equal(comparable_report(frame), comparable_report(frame.iloc[::-1].reset_index(drop=True)))


@unittest.skipIf(native_fixture.NATIVE_ERROR is not None, f"需要Bomber编译扩展: {native_fixture.NATIVE_ERROR}")
class PreparedDatasetNativeTests(unittest.TestCase):
    def test_raw_and_prepared_matching_signals_fees_settlement_and_accounts_equal(self):
        fixture = native_fixture.VegaNativeSettlementTests()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            module, args = fixture.fixture(root, Decimal("120.13"), option_bar_minutes=5,
                execution_timing="next_market", future_hedge=True)
            specs = module.backtest_bar_specs(args)
            snapshot = root / "prepared/v1"
            build_prepared_dataset((
                PreparedSource("future", root/"kline/fut", "IM", specs["future"]),
                PreparedSource("option", root/"kline/opt", "MO", specs["option"])),
                snapshot, start_day=args.start_day, end_day=args.end_day)
            raw_args, prepared_args = deepcopy(args), deepcopy(args)
            raw_args.report_dir = root/"raw-results"
            raw = module.run_case(raw_args)
            # Prove the prepared runtime does not need the original minute files.
            shutil.rmtree(root/"kline")
            prepared_args.data_mode, prepared_args.prepared_root = "prepared", snapshot
            prepared_args.prepared_verify = "full"
            prepared_args.report_dir = root/"prepared-results"
            prepared = module.run_case(prepared_args)
            for output in (raw, prepared):
                summary = json.loads((output/"summary.json").read_text())
                self.assertGreater(summary["fills"], 0)
                self.assertEqual(summary["settlements"], 1)
                self.assertEqual(summary["future_settlement_count"], 1)
                self.assertEqual(summary["remaining_positions_or_working"], {})
                self.assertEqual(summary["report_errors"], [])
            # Native UUID identities differ per engine run. Keep every financial
            # value, status and timestamp; exclude only generated record identities.
            for name in ("orders.csv", "fills.csv", "positions.csv", "account.csv", "signals.csv",
                         "settlements.csv", "future_settlements.csv"):
                frames = []
                for output in (raw, prepared):
                    frame = pd.read_csv(output/name, dtype=str, keep_default_na=False)
                    frames.append(comparable_report(frame))
                pd.testing.assert_frame_equal(*frames, obj=name)


if __name__ == "__main__":
    unittest.main()
