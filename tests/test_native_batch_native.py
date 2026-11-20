"""XY acceptance of the installed native batch constructor and complete reports."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

import numpy as np
import pandas as pd
import pyarrow as pa

from tests import test_option_vega_native as native_fixture
from tests.test_prepared_dataset_native import comparable_report
from bomber.framework.dataprep.columnar import ArrowBarFrame
from bomber.framework.dataprep.prepared import PreparedSource, build_prepared_dataset


@unittest.skipIf(native_fixture.NATIVE_ERROR is not None, f"需要Bomber编译扩展: {native_fixture.NATIVE_ERROR}")
class NativeBatchAcceptanceTests(unittest.TestCase):
    def test_native_values_precision_raw_integers_and_clocks_equal_row_parser(self):
        from bomber.framework.market.replay.native_batch import (
            ArrowBatchReader, NativeBarBatchParser, require_native_batch)
        from bomber.framework.market.replay.parsers.bar.fixed import PreparedBarParser
        from bomber.framework.market.replay.parsers.base import ParserContext
        from bomber.framework.market.basic.base import InstrumentId, InstrumentMeta
        from bomber.framework.dataprep.bars import PreparedFrameReader
        require_native_batch()  # Missing batch API is a deployment failure, not a skip.
        identity = InstrumentId.from_str("MO2609-C-6600.CFFEX")
        def signature(bars):
            return [(str(bar.bar_type), bar.ts_event, bar.ts_init,
                *((str(getattr(bar, name)), getattr(bar, name).precision, getattr(bar, name).raw)
                  for name in ("open", "high", "low", "close", "volume"))) for bar in bars]
        for price in (0, 1, 2, 4, 8, 9):
            for size in (0, 1, 2):
                for volume in (20.5, float(2**54)):
                    with self.subTest(price=price, size=size, volume=volume):
                        frame = pd.DataFrame(dict(event_ns=np.array([1_786_600_800_000_000_000+i*300_000_000_000
                            for i in range(7)], dtype=np.int64),
                            open=100.125, high=103., low=.125,
                            close=[101.005, 100.125, 101.125, 2.675, 1.005, .125, 101.55555555], volume=volume))
                        frame["available_ns"] = frame.event_ns+123
                        meta = InstrumentMeta(identity, price_precision=price, size_precision=size)
                        context = ParserContext(Path("/native/batch.feather"), 1, lambda _: meta)
                        old = PreparedBarParser(identity, bar_spec="5-MINUTE")
                        try:
                            expected = [event.payload for line, row in PreparedFrameReader(frame, copy_frame=False).read(context.path)
                                for event in old.parse(row, ParserContext(context.path, line, context.get_meta))]
                        except ValueError as error:
                            # Standard-precision SDK builds can have a lower
                            # Quantity maximum than the exceptional-volume case.
                            with self.assertRaises(type(error)):
                                NativeBarBatchParser(identity, "5-MINUTE").load_bar_batch(
                                    ArrowBatchReader(ArrowBarFrame(pa.Table.from_pandas(frame, preserve_index=False))), context)
                            continue
                        actual = NativeBarBatchParser(identity, "5-MINUTE").load_bar_batch(
                            ArrowBatchReader(ArrowBarFrame(pa.Table.from_pandas(frame, preserve_index=False))), context)
                        self.assertEqual(signature(expected), signature(actual))

    def test_complete_prepared_row_and_batch_reports_equal_without_raw_files(self):
        fixture = native_fixture.VegaNativeSettlementTests()
        from bomber.framework.market.replay.native_batch import require_native_batch
        require_native_batch()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            module, args = fixture.fixture(root, Decimal("120.13"), option_bar_minutes=5,
                execution_timing="next_market", future_hedge=True)
            specs = module.backtest_bar_specs(args)
            snapshot = root/"prepared/v1"
            build_prepared_dataset((
                PreparedSource("future", root/"kline/fut", "IM", specs["future"]),
                PreparedSource("option", root/"kline/opt", "MO", specs["option"])),
                snapshot, start_day=args.start_day, end_day=args.end_day)
            shutil.rmtree(root/"kline")
            outputs = []
            for parser in ("prepared", "native_batch"):
                selected = deepcopy(args)
                selected.data_mode, selected.prepared_root = "prepared", snapshot
                selected.prepared_verify, selected.bar_parser = "full", parser
                selected.report_dir = root/parser
                outputs.append(module.run_case(selected))
                summary = json.loads((outputs[-1]/"summary.json").read_text())
                self.assertGreater(summary["fills"], 0)
                self.assertEqual(summary["settlements"], 1)
                self.assertEqual(summary["future_settlement_count"], 1)
                self.assertEqual(summary["remaining_positions_or_working"], {})
                self.assertEqual(summary["report_errors"], [])
            for name in ("orders.csv", "fills.csv", "positions.csv", "account.csv", "signals.csv",
                         "settlements.csv", "future_settlements.csv"):
                with self.subTest(report=name):
                    frames = [comparable_report(pd.read_csv(output/name, dtype=str, keep_default_na=False))
                              for output in outputs]
                    pd.testing.assert_index_equal(frames[0].columns, frames[1].columns)
                    for column in frames[0].columns:
                        with self.subTest(report=name, column=column):
                            pd.testing.assert_frame_equal(frames[0][[column]], frames[1][[column]], obj=name)


if __name__ == "__main__":
    unittest.main()
