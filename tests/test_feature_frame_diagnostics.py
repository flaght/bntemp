"""Real Feather/public custom-feature diagnostics without strategy or native SDK."""
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pyarrow as pa

from bomber.framework.datahub.features import ExternalDataRecord, FeatureQuery
from bomber.framework.datahub.features.alignment import FeatureSnapshot
from bomber.framework.datahub.features.frames import FrameOutcome, StrategyFrame
from bomber.framework.datahub.features.prepared_frames import decode_frames, encode_frames
from bomber.framework.dataprep.feature_diagnostics import profile_prepared_frames
from bomber.framework.dataprep.features import FeatureRequest, bundle_from_definition, export_features
from bomber.framework.dataprep.replay_inputs import ReplayInputs, publish_replay_inputs
from bomber.framework.market.frames import FrameObservation
from bomber.framework.performance import PerformanceStats

S = 1_000_000_000


class FrameDiagnosticTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.file, self.config = self.root/"alpha.csv", self.root/"alpha.dataset.json"
        self.definition = {"format_version": 1, "datasets": [{
            "schema": {"dataset_id": "research.custom.alpha", "schema_version": 1,
                "computation_version": "alpha-v1", "alignment": "EXACT",
                "fields": [{"name": "alpha", "kind": "decimal"}]},
            "source": {"backend": "file", "path": str(self.file)}}]}
        self.config.write_text(json.dumps(self.definition))
        bundle = bundle_from_definition(self.definition, None)
        self.binding = bundle.binding("research.custom.alpha", 1)
        first = int(datetime(2026, 1, 5, 9, 31, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())*S
        stamps = [first+minute*60*S for minute in range(3)]
        records = [ExternalDataRecord(self.binding.spec.dataset_id, 1, "CUSTOM", self.binding.spec.payload_type(
            alpha=Decimal("1.2300")), stamp, stamp+S, stamp, 0, "fixture") for stamp in stamps[:2]]
        records.append(ExternalDataRecord(self.binding.spec.dataset_id, 1, "CUSTOM",
            self.binding.spec.payload_type(alpha=Decimal("9.99")), first, first+10*S, first, 1, "fixture"))
        export_features(self.file, bundle.registry, records)
        request = FeatureRequest(self.binding.spec.dataset_id, 1, ("CUSTOM",), first, stamps[-1]+10*S,
                                 2**63-1, max_rows=1_000_000)
        with bundle.provider(self.binding.spec.dataset_id, 1) as provider:
            batch = provider.read(request)
        self.records = batch.records
        self.outcomes, self.deadlines = [], {stamp: stamp+2*S for stamp in stamps}
        for stamp in stamps[:2]:
            record = next(row for row in self.records if row.ts_event == stamp and row.revision == 0)
            snapshot = FeatureSnapshot(FeatureQuery(stamp, stamp+S), 1, {"index": (record,)},
                                       {"index": self.binding.spec.fingerprint})
            market = {"CONTRACT-A": FrameObservation(Decimal("12.3400"), "market", stamp, stamp),
                      "CONTRACT-B": FrameObservation(Decimal("56.780"), "market", stamp, stamp)}
            self.outcomes.append(FrameOutcome(str(stamp), stamp, "READY",
                StrategyFrame(str(stamp), stamp, stamp+S, market, snapshot)))
        self.outcomes.append(FrameOutcome(str(stamps[-1]), stamps[-1], "SKIPPED", reason="MISSING"))
        self.outcomes = tuple(self.outcomes)
        self.context = {"source_count": 1, "requested": ["2026-01-05", "2026-01-05"],
            "frame_rules": {"index_key": "CUSTOM"}, "implementation": "previous-code-v1",
            "feature_input": {"dataset_id": self.binding.spec.dataset_id, "schema_version": 1,
                "schema_fingerprint": self.binding.spec.fingerprint, "path": str(self.file),
                "source": batch.source_identity, "records": len(self.records)}}
        self.output = self.root/"replay/v1"
        # Diagnostic fixture: native partitions are indexed but are never read or replayed.
        with publish_replay_inputs(self.output, self.context, parent_root=self.root/"parent") as writer:
            writer.write_table("native/one.feather", pa.table({"close": [1.]}))
            writer.native_rows = [{"source_key": "fixture", "file": "native/one.feather", "offset": 0, "rows": 1}]
            writer.write_table("native-index.feather", pa.Table.from_pylist(writer.native_rows))
            writer.write_frames(self.outcomes, self.deadlines, self.records, self.binding)

    def test_measured_restore_preserves_custom_payload_revisions_decimal_and_skips(self):
        frames, observations = encode_frames(self.outcomes, self.deadlines, self.records, self.binding)
        clock = iter(index/1000 for index in range(1000)).__next__
        stats = PerformanceStats(clock=clock)
        measured = decode_frames(frames, observations, self.records, self.binding, performance=stats)
        self.assertEqual(measured, (self.outcomes, self.deadlines))
        self.assertEqual(measured, decode_frames(frames, observations, self.records, self.binding))
        self.assertEqual(measured[0][0].frame.features.latest("index").revision, 0)
        self.assertEqual(measured[0][0].frame.market["CONTRACT-A"].price.as_tuple(), Decimal("12.3400").as_tuple())
        self.assertEqual(stats.counters, {"prepared_frame_rows": 3, "prepared_observation_rows": 4,
            "prepared_observations_restored": 4, "prepared_frames_ready": 2, "prepared_frames_skipped": 1})
        for name in ("prepared_frames_to_python", "prepared_observations_to_python", "prepared_frame_reference_checks",
                     "prepared_observation_decimal", "prepared_observation_objects", "prepared_frame_objects"):
            self.assertEqual(stats.timings[name]["calls"], 1)
            self.assertGreater(stats.timings[name]["seconds"], 0.)

    def test_diagnostic_profiles_old_artifact_without_native_replay_or_file_mutation(self):
        def hashes():
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self.output.rglob("*") if path.is_file()}
        before = hashes()
        with patch.object(ReplayInputs, "load_selected", side_effect=AssertionError("market data loaded")), \
                patch("bomber.framework.dataprep.replay_inputs.native_identity", side_effect=AssertionError("SDK accessed")):
            result = profile_prepared_frames(self.output, self.config, verify="full", repeat=2)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["diagnostic_only"])
        self.assertFalse(result["current_implementation_matches_artifact"])
        self.assertEqual(len(result["runs"]), 2)
        self.assertEqual(before, hashes())
        for run in result["runs"]:
            self.assertEqual(run["frames"], 3)
            self.assertEqual(run["counters"]["prepared_observation_rows"], 4)
            for name in ("prepared_frames_table_read", "prepared_observations_table_read",
                         "prepared_frames_decode", "prepared_frames_load"):
                self.assertIn(name, run["timings"])
        with self.assertRaisesRegex(ValueError, "version changed"):
            ReplayInputs(self.output, {**self.context, "implementation": "new-code"})

    def test_changed_feature_schema_and_content_are_rejected(self):
        self.definition["datasets"][0]["schema"]["computation_version"] = "alpha-v2"
        self.config.write_text(json.dumps(self.definition))
        with self.assertRaisesRegex(ValueError, "Schema"):
            profile_prepared_frames(self.output, self.config)
        self.definition["datasets"][0]["schema"]["computation_version"] = "alpha-v1"
        self.config.write_text(json.dumps(self.definition))
        bundle = bundle_from_definition(self.definition, None)
        self.file.unlink()
        Path(str(self.file)+".manifest.json").unlink()
        export_features(self.file, bundle.registry, self.records[:1])
        with self.assertRaisesRegex(ValueError, "内容/映射/记录数"):
            profile_prepared_frames(self.output, self.config)

    def test_full_profile_rejects_corrupt_frame_file(self):
        path = self.output/"frames.feather"
        contents = bytearray(path.read_bytes())
        contents[len(contents)//2] ^= 1
        path.write_bytes(contents)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            profile_prepared_frames(self.output, self.config, verify="full")

    def test_standalone_cli_does_not_require_strategy_sdk_and_protects_artifact(self):
        command = [sys.executable, "scripts/profile_prepared_frames.py", "--replay-inputs", str(self.output),
                   "--feature-config", str(self.config), "--verify", "full"]
        process = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["runs"][0]["frames"], 3)
        process = subprocess.run([*command, "--output", str(self.output/"report.json")],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse((self.output/"report.json").exists())
        for path in (self.config, self.file):
            original = path.read_bytes()
            process = subprocess.run([*command, "--output", str(path)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
