"""Certified input and frame artifact behavior; native entities use explicit fixtures."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import pandas as pd

from bomber.framework.dataprep import InputPlan, InputSession
from bomber.framework.dataprep.prepared import PreparedDataset
from bomber.framework.dataprep.replay_inputs import (ReplayInputs, artifact_context, publish_replay_inputs)
from tests import test_native_batch as native_fixture
from tests import test_option_feature_backtest as feature_fixture
from tests.test_bar_parsers import Scalar, Identity


class ReplayInputTests(unittest.TestCase):
    def setUp(self):
        self.native = native_fixture.NativeBatchTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.fixture = self.native.fixture
        for path in self.fixture.paths.values():
            frame = pd.read_feather(path)
            frame["volume"] = 2
            frame.to_feather(path)
        self.fixture.output = self.fixture.root/"prepared/ordinary"
        self.fixture.build()
        self.dataset = PreparedDataset(self.fixture.output, verify="full")
        self.sources = self.native.bundle()
        self.output = self.fixture.root/"replay/v1"
        # Native signatures are tested separately against the real SDK on XY.
        for name, value in (("raw", property(lambda self: int(self*Decimal(10)**16))),
                            ("precision", property(lambda self: max(0, -self.as_tuple().exponent))),
                            ("as_decimal", lambda self: Decimal(self))):
            p = patch.object(Scalar, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict("sys.modules", {
            "bomber.framework.market.basic.base": self.native.fw.basic,
            "bomber.framework.market.replay.native_batch": self.native.batch,
            "bomber.framework.market.replay.parsers.bar.fixed": self.native.fw.fixed,
            "bomber.framework.market.replay.parsers.base": self.native.fw.context})
        p.start()
        self.addCleanup(p.stop)
        p = patch("bomber.framework.dataprep.replay_inputs.native_identity", return_value={"fixture": "native-v1"})
        p.start()
        self.addCleanup(p.stop)
        self.features = feature_fixture.FeatureBacktestTests()
        self.features.setUp()
        self.addCleanup(self.features.doCleanups)
        self.meta = {}
        self.instruments = {}
        for source in self.sources.sources:
            identity = Identity(source.result.key.symbol+".CFFEX")
            self.instruments[source.result.key.symbol] = identity
            self.meta[identity] = self.native.fw.basic.InstrumentMeta(identity, price_precision=2, size_precision=0)
        timestamps = sorted({int(ns) for source in self.sources.sources for ns in source.result.frame.array("event_ns")})
        self.records = tuple(self.features.record(event=stamp, visible=stamp+feature_fixture.S,
                            close="6400.00") for stamp in timestamps)
        # A future revision must not replace the first visible revision.
        self.records += (self.features.record(event=timestamps[0], visible=timestamps[0]+3*feature_fixture.S,
                                             revision=1, close="9999.00"),)
        # The next business minute has data, but its first revision misses deadline.
        self.records = tuple(replace(row, available_ns=row.ts_event+2*feature_fixture.S+1)
            if row.ts_event == timestamps[1] else row for row in self.records)
        self.rules = {"wait_ns": 2*feature_fixture.S, "max_age_ns": 300*feature_fixture.S, "index_key": "000852"}
        self.context = artifact_context(self.dataset, self.fixture.plan(), self.fixture.specs,
            {"source": "feature-fixture-content-v1", "schema": self.features.binding.spec.fingerprint},
            self.rules, feature_fixture.replay.frame_preparation_identity())

    def publish(self, output=None):
        with publish_replay_inputs(output or self.output, self.context, parent_root=self.dataset.root) as writer:
            events = writer.write_native(self.sources.sources, self.instruments, self.meta.get)
            self.outcomes, self.deadlines = feature_fixture.replay.prepare_feature_frames(events,
                self.features.bundle, self.features.binding, self.records, "000852", self.rules["wait_ns"],
                self.rules["max_age_ns"])
            writer.write_frames(self.outcomes, self.deadlines, self.records, self.features.binding)
        return events

    def load(self, cache, plan=None):
        return self.dataset.load(plan or self.fixture.plan(), specs=self.fixture.specs, products=self.fixture.products,
            copy_frames=False, frame_format="arrow", replay_inputs=cache)

    def test_certified_arrays_skip_rounding_checks_and_keep_exact_native_events(self):
        events = self.publish()
        cache = ReplayInputs(self.output, self.context, verify="full")
        with InputSession() as session:
            bundle = self.load(cache)
        self.assertNotIn("prepared_partition_read", session.performance.timings)
        self.assertEqual(session.performance.counters["native_ready_files_read"], 6)
        feed = self.native.feed(bundle, True)
        with patch.object(self.native.batch.NativeBarBatchParser, "prepare_arrays", side_effect=AssertionError("checks repeated")), \
                patch.object(self.native.batch, "legacy_rounded_array", side_effect=AssertionError("rounding repeated")):
            actual = feed.load_events()
        self.assertEqual(self.native.signature(events), self.native.signature(actual))
        self.assertEqual(feed.performance.counters["native_ready_rows"], 36)
        self.assertNotIn("native_batch_rounding", feed.performance.timings)
        self.assertNotIn("native_batch_array_prepare", feed.performance.timings)

    def test_frames_preserve_revisions_deadlines_skips_decimal_and_decision_clocks(self):
        self.publish()
        cache = ReplayInputs(self.output, self.context, verify="full")
        with patch.object(feature_fixture.replay, "prepare_feature_frames", side_effect=AssertionError("frames rebuilt")):
            outcomes, deadlines = cache.load_frames(self.records, self.features.binding)
        self.assertEqual(outcomes, self.outcomes)
        self.assertEqual(deadlines, self.deadlines)
        self.assertEqual(outcomes[0].frame.features.latest("index").revision, 0)
        self.assertTrue(any(row.status == "SKIPPED" for row in outcomes))
        for row, old in zip(outcomes, self.outcomes):
            if row.frame:
                for key in row.frame.market:
                    self.assertEqual(row.frame.market[key].price.as_tuple(), old.frame.market[key].price.as_tuple())

    def test_changed_features_wait_parent_or_implementation_are_rejected(self):
        self.publish()
        for field, value in (("frame_rules", {**self.rules, "wait_ns": 3*feature_fixture.S}),
                ("feature_input", {"source": "changed"}), ("parent_manifest_sha256", "changed"),
                ("implementation", "changed"), ("native", {"fixture": "changed"})):
            changed = deepcopy(self.context)
            changed[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "version changed"):
                ReplayInputs(self.output, changed)

    def test_changed_source_selection_and_precision_rejected(self):
        self.publish()
        cache = ReplayInputs(self.output, self.context)
        with self.assertRaisesRegex(ValueError, "source universe"):
            self.load(cache, InputPlan(tuple(self.fixture.paths)[1:], requested=self.fixture.plan().requested))
        feed = self.native.feed(self.load(cache), True, price=4)
        with self.assertRaisesRegex(self.native.fw.context.DataLoadError, "precision/identity changed"):
            feed.load_events()

    def test_no_overwrite_and_failed_certification_does_not_publish(self):
        self.publish()
        original = (self.output/"manifest.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.publish()
        bad = self.output.parent/"bad"
        with patch.object(self.native.batch.Bar, "from_raw_arrays_to_list", return_value=[]), \
                self.assertRaisesRegex(ValueError, "row count differs"):
            self.publish(bad)
        self.assertFalse(bad.exists())
        self.assertFalse((bad.parent/"bad.prepare.lock").exists())
        self.assertEqual((self.output/"manifest.json").read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.publish(self.dataset.root/"nested")

    def test_native_value_difference_fails_certification_before_publication(self):
        constructor = self.native.batch.Bar.from_raw_arrays_to_list
        def wrong_values(*args):
            bars = constructor(*args)
            if bars:
                bars[0].close = Scalar("999.00")
            return bars
        with patch.object(self.native.batch.Bar, "from_raw_arrays_to_list", side_effect=wrong_values), \
                self.assertRaisesRegex(ValueError, "conversion differs"):
            self.publish()
        self.assertFalse(self.output.exists())
        self.assertFalse((self.output.parent/"v1.prepare.lock").exists())

    def test_incomplete_package_and_corrupt_files_fail(self):
        with self.assertRaisesRegex(ValueError, "both be prepared"):
            with publish_replay_inputs(self.output, self.context, parent_root=self.dataset.root):
                pass
        self.assertFalse(self.output.exists())
        self.publish()
        cache = ReplayInputs(self.output, self.context, verify="full")
        path = self.output/"frames.feather"
        data = bytearray(path.read_bytes())
        data[len(data)//2] ^= 1
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            cache.load_frames(self.records, self.features.binding)

    def test_preparation_wrapper_forwards_same_yaml_and_explicit_dates(self):
        from scripts.prepare_replay_inputs import main
        import sys
        previous = sys.argv
        entry = import_module("demos.08_option_vega.run_backtest")
        captured = []
        with patch.object(entry, "main", side_effect=lambda: captured.append(list(sys.argv))):
            main(["--config-file", "strategy.yaml", "--config-id", "m5_index_blend",
                  "--start-day", "2026-01-01", "--output", str(self.output)])
        self.assertIs(sys.argv, previous)
        self.assertEqual(captured[0][-6:], ["--data-mode", "prepared", "--bar-parser", "native_batch",
                                          "--prepare-replay-inputs", str(self.output)])

    def test_integrity_cli_works_without_native_sdk(self):
        self.publish()
        result = subprocess.run([sys.executable, "scripts/check_replay_inputs.py", "--replay-inputs", str(self.output)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "passed")

    def test_backtest_cli_accepts_preparation_and_reuse_without_changing_budget(self):
        entry = import_module("demos.08_option_vega.run_backtest")
        for flag in ("--replay-inputs", "--prepare-replay-inputs"):
            argv = ["run_backtest.py", "--start-day", "2026-01-01", "--end-day", "2026-10-21",
                    "--index-feature-file", "index.csv", "--data-mode", "prepared",
                    "--prepared-root", str(self.fixture.output), "--bar-parser", "native_batch",
                    flag, str(self.output), "--max-notional", "1000000000"]
            with self.subTest(flag=flag), patch.object(entry, "run_case") as capture, patch.object(sys, "argv", argv):
                entry.main()
            args = capture.call_args.args[0]
            self.assertEqual(getattr(args, flag[2:].replace("-", "_")), self.output)
            self.assertEqual(args.max_notional, Decimal("1000000000"))


if __name__ == "__main__":
    unittest.main()
