"""指数MA导出例子：窗口缺口、真实可见性及文件清单往返；不连接数据库。"""
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bomber.framework.datahub.features import (
    DatasetRegistry, ExternalDataRecord, FeatureHub, FeatureQuery,
    FeatureRequirement, FeatureUnavailable, dataset_from_definition,
)
from bomber.framework.dataprep.features import FeatureRequest, export_features, load_feature_bundle
from scripts.integration.features.example import moving_average_definition, moving_average_records

M = 60_000_000_000


class MovingAverageExportTests(unittest.TestCase):
    def setUp(self):
        self.input = dataset_from_definition({"dataset_id": "fixture.index", "schema_version": 1,
            "fields": [{"name": "close", "kind": "decimal"}]})
        self.definition = moving_average_definition(5)
        self.output = dataset_from_definition(self.definition)

    def rows(self, minutes):
        return tuple(ExternalDataRecord(self.input.dataset_id, 1, "000852",
            self.input.payload_type(close=Decimal(minute * 10)), minute * M,
            minute * M + 1, minute * M, 1, "fixture", received_ns=minute * M + 2)
            for minute in minutes)

    def derive(self, rows, clock=100*M):
        return moving_average_records(rows, self.input, self.output, "000852", 5, clock_ns=lambda: clock)

    def test_exported_dataset_config_loads_same_payload_and_timestamps(self):
        records, coverage = self.derive(self.rows(range(1, 7)))
        self.assertEqual([row.payload.ma5 for row in records], [Decimal(30), Decimal(40)])
        self.assertEqual(coverage["warmup_skipped"], 4)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ma.csv"
            export_features(path, DatasetRegistry((self.output,)), records)
            config = Path(str(path) + ".dataset.json")
            config.write_text(json.dumps({"format_version": 1, "datasets": [{
                "schema": self.definition, "source": {"backend": "file", "path": path.name}}]}),
                encoding="utf-8")
            bundle = load_feature_bundle(config, None)
            with bundle.provider(self.output.dataset_id) as provider:
                loaded = provider.read(FeatureRequest(self.output.dataset_id, 1, ("000852",),
                    5*M, 6*M, 100*M)).records
            self.assertEqual(loaded, records)

    def test_missing_minute_resets_window_instead_of_combining_partial_segments(self):
        records, coverage = self.derive(self.rows((1, 2, 3, 4, 6, 7, 8, 9, 10)))
        self.assertEqual([(row.ts_event, row.payload.ma5) for row in records], [(10*M, Decimal(80))])
        self.assertEqual(coverage["window_resets"], 1)
        self.assertEqual(coverage["warmup_skipped"], 8)

    def test_research_export_does_not_backdate_observed_calculation(self):
        records, _ = self.derive(self.rows(range(1, 7)))
        hub = FeatureHub(DatasetRegistry((self.output,)))
        hub.store.publish(records)
        demand = (FeatureRequirement("ma", self.output.dataset_id, 1, "000852"),)
        with self.assertRaises(FeatureUnavailable):
            hub.snapshot(demand, FeatureQuery(6*M, 6*M + 2))
        self.assertEqual(hub.snapshot(demand, FeatureQuery(6*M, 100*M)).latest("ma").payload.ma5,
                         Decimal(40))
        with self.assertRaises(ValueError):
            self.derive(self.rows(range(1, 6)), clock=5*M)

    def test_bad_latest_revision_cannot_fall_back_to_previous_good_price(self):
        rows = self.rows(range(1, 6))
        bad = replace(rows[-1], revision=2, quality_flags=1)
        with self.assertRaises(ValueError):
            self.derive((*rows, bad))


if __name__ == "__main__":
    unittest.main()
