"""DataFrame通用转换：类型、业务键、纳秒时间与标准文件往返；不联网。"""
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
import pandas as pd

from bomber.framework.datahub.features import DatasetRegistry, FeatureError, dataset_from_definition
from bomber.framework.dataprep.features import export_features, FileFeatureProvider, FeatureRequest

convert = import_module("demos.08_option_vega.feature.dataframe").dataframe_to_records
BASE = int(pd.Timestamp("2026-09-01T09:31:00+08:00").value)
M, S = 60_000_000_000, 1_000_000_000


class DataFrameFeatureTests(unittest.TestCase):
    def setUp(self):
        self.spec = dataset_from_definition({"dataset_id": "research.custom.minute", "schema_version": 2,
            "computation_version": "business-frame-v1",
            "fields": [{"name": "signal", "kind": "float"}, {"name": "count", "kind": "int"},
                {"name": "exact", "kind": "decimal"}, {"name": "enabled", "kind": "bool"},
                {"name": "label", "kind": "str"}, {"name": "optional", "kind": "float", "nullable": True}]})
        self.frame = pd.DataFrame({"signal": np.array([-.02, .03]), "count": np.array([20, 60], dtype=np.int64),
            "exact": [Decimal("6500.12345678901234567890123456789"), Decimal("6501.1")],
            "enabled": [True, False], "label": ["ma20", "blend"], "optional": [np.nan, .5],
            "auxiliary": [100, 200]}, index=pd.DatetimeIndex([
                "2026-09-01T09:31:00+08:00", "2026-09-01T09:32:00+08:00"]))

    def convert(self, frame=None, **overrides):
        args = dict(key="000852", source="business:frame-v1", available_ns=BASE+2*M+123456789,
            source_ns=pd.Series([BASE, BASE+M], index=self.frame.index, dtype="Int64"),
            received_ns=BASE+2*M+987654321)
        args.update(overrides)
        return convert(self.frame if frame is None else frame, self.spec, **args)

    def test_datetime_frame_preserves_types_keys_nanoseconds_and_only_declared_columns(self):
        original = self.frame.copy(deep=True)
        rows = self.convert()
        pd.testing.assert_frame_equal(self.frame, original)
        self.assertEqual([r.ts_event for r in rows], [BASE, BASE+M])
        self.assertEqual(rows[0].available_ns, BASE+2*M+123456789)
        self.assertEqual(rows[0].received_ns, BASE+2*M+987654321)
        self.assertEqual(rows[0].revision, rows[0].available_ns)
        self.assertEqual(rows[0].key, "000852")
        self.assertEqual(rows[0].payload.exact, self.frame.exact.iloc[0])
        self.assertEqual(rows[0].payload.count, 20)
        self.assertIs(type(rows[0].payload.count), int)
        self.assertEqual(rows[0].payload.signal, -.02)  # 通用特征允许负值，不套指数价格限制。
        self.assertIsNone(rows[0].payload.optional)
        self.assertFalse(hasattr(rows[0].payload, "auxiliary"))

    def test_integer_index_and_aligned_metadata_series_preserve_nullable_receipts(self):
        frame = self.frame.copy()
        frame.index = pd.Index([BASE, BASE+M], name="ts_event")
        frame["optional"] = pd.Series([pd.NA, .5], index=frame.index, dtype="Float64")
        rows = self.convert(frame, key=pd.Series(["000852", "000300"], index=frame.index),
            available_ns=pd.Series([BASE+1, BASE+M+7], index=frame.index, dtype="Int64"),
            source_ns=pd.Series([BASE, BASE+M], index=frame.index, dtype="Int64"),
            received_ns=pd.Series([pd.NA, BASE+M+9], index=frame.index, dtype="Int64"),
            revision=0, quality_flags=pd.Series([0, 2], index=frame.index))
        self.assertEqual([r.key for r in rows], ["000852", "000300"])
        self.assertIsNone(rows[0].received_ns)
        self.assertEqual(rows[1].received_ns, BASE+M+9)
        self.assertEqual(rows[1].quality_flags, 2)
        self.assertEqual([r.revision for r in rows], [0, 0])

    def test_naive_float_and_default_indices_are_rejected_without_guessing_time(self):
        for index in (self.frame.index.tz_localize(None), pd.Index([float(BASE), float(BASE+M)], name="ts_event"),
                      pd.RangeIndex(2)):
            with self.subTest(index=index), self.assertRaises(FeatureError):
                frame = self.frame.copy()
                frame.index = index
                self.convert(frame, source_ns=BASE)
        with self.assertRaises(FeatureError):
            self.convert(available_ns=float(BASE+2*M))

    def test_metadata_series_must_align_and_payload_must_match_schema(self):
        with self.assertRaisesRegex(FeatureError, "索引须与DataFrame完全一致"):
            self.convert(source_ns=pd.Series([BASE, BASE+M]))
        with self.assertRaisesRegex(FeatureError, "缺少声明字段"):
            self.convert(self.frame.drop(columns="signal"))
        with self.assertRaisesRegex(FeatureError, "列名唯一"):
            self.convert(pd.concat([self.frame, self.frame[["signal"]]], axis=1))

    def test_schema_finiteness_frequency_and_availability_are_enforced(self):
        bad = self.frame.copy()
        bad["signal"] = np.nan
        with self.assertRaises(FeatureError):
            self.convert(bad)
        bad.index = pd.to_datetime([BASE+1, BASE+M+1], utc=True)
        bad["signal"] = [.1, .2]
        with self.assertRaisesRegex(FeatureError, "频率"):
            self.convert(bad, source_ns=BASE)
        with self.assertRaises(FeatureError):
            self.convert(available_ns=BASE-1)

    def test_same_minute_supports_multiple_keys_or_revisions_but_not_duplicate_identity(self):
        frame = self.frame.copy()
        frame.index = pd.Index([BASE, BASE], name="ts_event")
        with self.assertRaisesRegex(FeatureError, "重复"):
            self.convert(frame, source_ns=BASE)
        rows = self.convert(frame, key=pd.Series(["000852", "000300"], index=frame.index), source_ns=BASE)
        self.assertEqual(len(rows), 2)
        rows = self.convert(frame, revision=pd.Series([0, 1], index=frame.index), source_ns=BASE)
        self.assertEqual([r.revision for r in rows], [0, 1])

    def test_standard_csv_and_parquet_manifest_roundtrip(self):
        rows = self.convert()
        registry = DatasetRegistry((self.spec,))
        with TemporaryDirectory() as directory:
            for suffix in (".csv", ".parquet"):
                with self.subTest(suffix=suffix):
                    path = Path(directory) / ("custom"+suffix)
                    export_features(path, registry, rows)
                    self.assertTrue(Path(str(path)+".manifest.json").is_file())
                    with FileFeatureProvider(path, registry) as provider:
                        loaded = provider.read(FeatureRequest(self.spec.dataset_id, 2, ("000852",),
                            BASE, BASE+M, BASE+3*M)).records
                    self.assertEqual(loaded, rows)

    def test_empty_dataframe_returns_no_records(self):
        self.assertEqual(self.convert(self.frame.iloc[:0], source_ns=BASE), ())
