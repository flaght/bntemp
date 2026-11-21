"""Shared columnar/Cython restoration equals the Python reference on real Arrow."""
import gc
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import numpy as np
import pyarrow as pa

from bomber.framework.datahub.features.prepared_frames import decode_frames, encode_frames
from bomber.framework.datahub.features.restoration import (native_restoration_module,
    prepare_observation_columns, restore_market_observations)
from bomber.framework.dataprep.feature_diagnostics import profile_prepared_frames
from bomber.framework.performance import PerformanceStats
from tests import test_feature_frame_diagnostics as fixture_module


class RestorationFallbackTests(unittest.TestCase):
    def test_missing_optional_module_uses_python_but_explicit_cython_fails(self):
        table = pa.table({"key": ["A"], "price": ["1.00"], "source_id": ["market"],
                          "source_ns": [2**53+1], "received_ns": [2**53+3]})
        with patch("bomber.framework.datahub.features.restoration.native_restoration_module", return_value=None):
            columns = prepare_observation_columns(table)
            self.assertIsInstance(columns, dict)
            self.assertEqual(restore_market_observations(columns, 0, 1)["A"].source_ns, 2**53+1)
            with self.assertRaisesRegex(RuntimeError, "扩展不可用"):
                prepare_observation_columns(table, backend="cython")


@unittest.skipIf(native_restoration_module() is None, "需要编译公共Cython恢复扩展")
class CythonRestorationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.FrameDiagnosticTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_custom_feature_frames_match_reference_without_observation_to_pydict(self):
        fixture = self.fixture
        frames, table = encode_frames(fixture.outcomes, fixture.deadlines, fixture.records, fixture.binding)
        class NoPythonTable:
            def column(self, name):
                return table.column(name)
            def __len__(self):
                return len(table)
            def to_pydict(self):
                raise AssertionError("Cython path materialized the complete observation table")
        stats = PerformanceStats()
        actual = decode_frames(frames, NoPythonTable(), fixture.records, fixture.binding,
                               performance=stats, backend="cython")
        expected = decode_frames(frames, table, fixture.records, fixture.binding, backend="python")
        self.assertEqual(actual, expected)
        self.assertEqual(actual, (fixture.outcomes, fixture.deadlines))
        self.assertEqual(stats.counters["prepared_restore_cython_runs"], 1)
        self.assertEqual(stats.counters["prepared_unique_market_keys"], 2)
        self.assertEqual(stats.counters["prepared_unique_market_sources"], 1)
        self.assertEqual(stats.counters["prepared_unique_market_prices"], 2)
        self.assertEqual(stats.timings["prepared_observations_to_python"]["seconds"], 0.)
        self.assertGreater(stats.timings["prepared_observations_columnar_prepare"]["seconds"], 0.)
        a, b = actual[0][0].frame.market["CONTRACT-A"], actual[0][1].frame.market["CONTRACT-A"]
        self.assertIsNot(a, b)
        self.assertIs(a.price, b.price)
        self.assertIs(a.source_id, b.source_id)

    def test_chunked_sliced_columns_preserve_unicode_decimal_tuple_order_and_large_clocks(self):
        prices = ["1.00", "1.0", "-0.00", "-0", "1E+3", "1000", "123456789.00000000000000000001"]
        keys = ["期权甲", "期权乙", "C", "D", "E", "F", None]
        table = pa.table({"key": keys*2, "price": prices*2, "source_id": ["行情", None, "行情", "期货", "期货", "行情", "行情"]*2,
            "source_ns": np.arange(14, dtype=np.int64)+(2**63-100),
            "received_ns": np.arange(14, dtype=np.int64)+(2**63-80)})
        table = pa.concat_tables([table.slice(0, 3), table.slice(3, 5), table.slice(8)])
        reference = prepare_observation_columns(table, backend="python")
        columns = prepare_observation_columns(table, backend="cython")
        self.assertFalse(columns.source_ns.flags.writeable)
        del table
        gc.collect()
        for offset in (0, 7):
            old = restore_market_observations(reference, offset, 7)
            new = restore_market_observations(columns, offset, 7)
            self.assertEqual(new, old)
            self.assertEqual(list(new), keys)
            for key in keys:
                self.assertEqual(new[key].price.as_tuple(), old[key].price.as_tuple())
                self.assertIs(type(new[key].source_ns), int)

    def test_empty_and_already_dictionary_encoded_columns(self):
        table = pa.table({"key": pa.array([], type=pa.string()), "price": pa.array([], type=pa.string()),
            "source_id": pa.array([], type=pa.string()), "source_ns": pa.array([], type=pa.int64()),
            "received_ns": pa.array([], type=pa.int64())})
        self.assertEqual(restore_market_observations(prepare_observation_columns(table, backend="cython"), 0, 0), {})
        table = pa.table({"key": pa.array(["A", "B"]).dictionary_encode(),
            "price": pa.array(["1.00", "2.00"]).dictionary_encode(),
            "source_id": pa.array(["source", "source"]).dictionary_encode(),
            "source_ns": [123, 124], "received_ns": [125, 126]})
        native = prepare_observation_columns(table, backend="cython")
        python = prepare_observation_columns(table, backend="python")
        self.assertEqual(restore_market_observations(native, 0, 2), restore_market_observations(python, 0, 2))

    def test_duplicate_keys_ranges_dictionary_codes_and_unsafe_clocks_fail(self):
        table = pa.table({"key": ["A", "A"], "price": ["1.00", "1.00"], "source_id": ["market", "market"],
                          "source_ns": [100, 101], "received_ns": [101, 102]})
        columns = prepare_observation_columns(table, backend="cython")
        for offset, count in ((-1, 1), (0, -1), (1, 2), (1, sys.maxsize)):
            with self.subTest(offset=offset, count=count), self.assertRaisesRegex(ValueError, "range"):
                columns.restore(offset, count)
        for backend in ("python", "cython"):
            with self.subTest(backend=backend), self.assertRaisesRegex(ValueError, "duplicated"):
                restore_market_observations(prepare_observation_columns(table, backend=backend), 0, 2)
        columns.key_codes = np.array([5, 0], dtype=np.int32)
        with self.assertRaisesRegex(ValueError, "dictionary code"):
            columns.restore(0, 1)
        table = table.set_column(3, "source_ns", pa.array([1., 2.]))
        with self.assertRaisesRegex(ValueError, "clocks"):
            prepare_observation_columns(table, backend="cython")

    def test_existing_v1_diagnostic_can_choose_either_backend_and_cli_reports_actual_backend(self):
        fixture = self.fixture
        for backend in ("python", "cython"):
            result = profile_prepared_frames(fixture.output, fixture.config, backend=backend, verify="full")
            self.assertEqual(result["runs"][0]["restore_backend"], backend)
            self.assertEqual(result["runs"][0]["counters"]["prepared_observations_restored"], 4)
            self.assertFalse(result["current_implementation_matches_artifact"])
        process = subprocess.run([sys.executable, "scripts/profile_prepared_frames.py", "--replay-inputs", str(fixture.output),
            "--feature-config", str(fixture.config), "--restore-backend", "cython"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        import json
        self.assertEqual(json.loads(process.stdout)["runs"][0]["restore_backend"], "cython")


if __name__ == "__main__":
    unittest.main()
