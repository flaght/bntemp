"""08目录内双文件特征例子：真实Provider和标准文件，不联网、不发单。"""
from decimal import Decimal, localcontext
from importlib import import_module
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from bomber.framework.dataprep.features import DolphinDbRawFeatureProvider, FeatureRequest, load_feature_bundle
from bomber.framework.dolphindb import DolphinDbConnectionConfig
from bomber.framework.datahub.features import ExternalDataRecord, FeatureError, dataset_from_definition
from tests.test_dolphindb_index_features import Session, row

example = import_module("demos.08_option_vega.feature.generate")
features = import_module("demos.08_option_vega.feature")
M, S = 60_000_000_000, 1_000_000_000
BASE = example.timestamp("2026-09-01T09:31:00+08:00")


class DataFrameCalculationTests(unittest.TestCase):
    def setUp(self):
        self.spec = dataset_from_definition({"dataset_id": "fixture.index.minute", "schema_version": 1,
            "fields": [{"name": "close", "kind": "decimal", "minimum": "0", "exclusive_minimum": True}]})

    def records(self):
        return tuple(ExternalDataRecord(self.spec.dataset_id, 1, "000852",
            self.spec.payload_type(close=Decimal(value)), BASE+i*M, BASE+i*M+S//10,
            BASE+i*M, 0, "dataframe-fixture", quality_flags=2 if i == 0 else 0)
            for i, value in enumerate((1, 2, 3, 4, 5)))

    def test_pure_numpy_decimal_calculation_keeps_precision_segments_and_input(self):
        import pandas as pd
        frame = pd.DataFrame({"close": [1, 2, 2, 4, 5, 6], "segment_id": [0, 0, 0, 1, 1, 1]})
        original = frame.copy(deep=True)
        result = example.calculate_moving_average(frame, 3)
        pd.testing.assert_frame_equal(frame, original)
        self.assertEqual(result.index.tolist(), [2, 5])
        with localcontext() as context:
            context.prec = 34
            expected = Decimal(5) / Decimal(3)
        self.assertEqual(result.loc[2, "ma3"], expected)
        self.assertEqual(result.loc[5, "ma3"], Decimal(5))
        self.assertEqual(result.columns.tolist(), ["close", "ma3", "window"])

    def test_pandas_composite_formula_uses_same_record_adapter_and_source_times(self):
        import pandas as pd
        output = dataset_from_definition({"dataset_id": "fixture.index.blend", "schema_version": 1,
            "computation_version": "pandas-float64-ma2-ma3-half-v1",
            "fields": [{"name": name, "kind": "float", "minimum": "0", "exclusive_minimum": True}
                for name in ("close", "ma2", "ma3", "index_blend")]
                + [{"name": "window", "kind": "int", "minimum": "3", "maximum": "3"}]})
        def calculate(frame, window):
            close = frame["close"].astype(float)
            groups = close.groupby(frame["segment_id"])
            ma2 = groups.transform(lambda values: values.rolling(2, min_periods=2).mean())
            ma3 = groups.transform(lambda values: values.rolling(3, min_periods=3).mean())
            return pd.DataFrame({"close": close, "ma2": ma2, "ma3": ma3,
                "index_blend": (ma2+ma3)/2}, index=frame.index).assign(window=3).dropna()
        records, coverage = example.moving_average_records(self.records(), self.spec, output,
            "000852", 3, clock_ns=lambda: BASE+10*M, calculator=calculate)
        self.assertEqual([record.payload.index_blend for record in records], [2.25, 3.25, 4.25])
        self.assertEqual(records[0].ts_event, BASE+2*M)
        self.assertEqual(records[0].source_ns, BASE+2*M)
        self.assertEqual(records[0].available_ns, BASE+10*M)
        self.assertEqual(records[0].received_ns, BASE+10*M)
        self.assertEqual(records[0].payload.window, 3)
        self.assertEqual([record.quality_flags for record in records], [2, 0, 0])
        self.assertEqual(coverage["warmup_skipped"], 2)
        self.assertTrue(records[0].source.startswith("derived:fixture.index.blend:"))

    def test_calculator_cannot_return_unknown_minutes_or_emit_before_warmup(self):
        output = dataset_from_definition(example.moving_average_definition(3))
        def unknown_minute(frame, window):
            result = example.calculate_moving_average(frame, window)
            result.index = result.index+100*M
            return result
        def early(frame, window):
            return frame[["close"]].assign(ma3=Decimal(1), window=3)
        for calculator, message in ((unknown_minute, "原始业务时间"), (early, "预热前")):
            with self.subTest(calculator=calculator.__name__), self.assertRaisesRegex(ValueError, message):
                example.moving_average_records(self.records(), self.spec, output, "000852", 3,
                    clock_ns=lambda: BASE+10*M, calculator=calculator)


class CombinedFeatureExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "features"
        self.connection = DolphinDbConnectionConfig("fixture", 8848, "user", "fixture-secret")

    def generate(self, *, count=20, source_key=None, availability="source-time", index_only=False, suffix=".csv"):
        rows = [row(event=BASE+i*M, published=BASE+i*M+S//10,
            key=source_key or "000852", close=6400+i, high=6500) for i in range(count)]
        self.session = Session(rows=rows)
        original_create = example.FeatureProviderFactory.create_for
        def create(purpose, backend, config, registry, **options):
            if backend == "file":
                return original_create(purpose, backend, config, registry, **options)
            self.assertEqual(purpose.value, "feature_export")
            self.assertEqual(backend, "dolphindb-raw")
            return DolphinDbRawFeatureProvider(config, registry,
                session_factory=lambda **kw: self.session, clock_ns=lambda: BASE+30*M)
        with patch.object(example.FeatureProviderFactory, "create_for", side_effect=create) as factory:
            if index_only:
                args = NS(output=self.output / ("index"+suffix), index_feature_config=example.CONFIG,
                    index_dataset="index.minute", index_schema_version=1, index_code="000852",
                    source_key=source_key, index_availability=availability, max_rows=1_000_000)
                result = example.generate_index_features(args, BASE, BASE+19*M, connection=self.connection)
            else:
                result = example.generate_features(start_ns=BASE, end_ns=BASE+19*M,
                    output_dir=self.output, connection=self.connection, source_key=source_key,
                    availability=availability)
            self.assertEqual(sum(call.args[1] == "dolphindb-raw" for call in factory.call_args_list), 1)
        self.assertTrue(self.session.closed)
        # 一次原表数据查询；Schema查询是同一Provider打开时的校验。
        self.assertEqual(sum(not script.startswith("schema(") for script in self.session.scripts), 1)
        return result

    def read(self, part):
        name = "index" if part == "index" else "index-ma20"
        bundle = load_feature_bundle(self.output / (name + ".csv.dataset.json"), None)
        dataset = "index.minute" if part == "index" else "research.index.ma20.minute"
        with bundle.provider(dataset) as provider:
            records = provider.read(FeatureRequest(dataset, 1, ("000852",), BASE,
                BASE+19*M, example.time.time_ns())).records
        return bundle, records

    def test_one_query_generates_six_files_and_both_load_into_08(self):
        report = self.generate(source_key="SH000852")
        self.assertEqual(report["status"], "passed")
        self.assertEqual((report["index"]["rows"], report["ma"]["rows"]), (20, 1))
        self.assertEqual(report["coverage"]["warmup_skipped"], 19)
        self.assertEqual(report["source_key"], "SH000852")
        self.assertEqual(len(list(self.output.iterdir())), 7)  # 两套包加summary。
        raw_bundle, raw = self.read("index")
        ma_bundle, ma = self.read("ma")
        self.assertEqual({record.key for record in raw + ma}, {"000852"})
        self.assertEqual(raw[0].available_ns, BASE+S//10)
        self.assertIsNone(raw[0].received_ns)
        self.assertEqual(ma[0].payload.ma20, Decimal("6409.5"))
        self.assertGreater(ma[0].visible_ns, raw[-1].visible_ns)
        self.assertEqual(raw_bundle.binding("index.minute").spec.fingerprint,
            load_feature_bundle(example.CONFIG, self.connection).binding("index.minute").spec.fingerprint)
        self.assertIsNone(ma_bundle.binding("research.index.ma20.minute").bar_projection)
        for name, dataset, field in (("index", "index.minute", None),
                                     ("index-ma20", "research.index.ma20.minute", "ma20")):
            args = NS(index_feature_file=self.output / (name+".csv"),
                index_feature_config=self.output / (name+".csv.dataset.json"),
                index_dataset=dataset, index_schema_version=1, index_code="000852", index_value_field=field)
            _, _, records, identity = features.read_index_features(args, BASE, BASE+19*M)
            self.assertEqual(identity["value_field"], field or "close")
            self.assertTrue(records)
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("fixture-secret", text)
        self.assertEqual(json.loads((self.output / "summary.json").read_text()), report)

    def test_observed_raw_and_ma_keep_current_visibility_not_historical_backfill(self):
        report = self.generate(availability="observed")
        _, raw = self.read("index")
        _, ma = self.read("ma")
        self.assertEqual(raw[0].available_ns, BASE+30*M)
        self.assertGreater(ma[0].available_ns, BASE+30*M)
        self.assertEqual(report["index_availability"], "observed")
        self.assertIn("actual_calculation", report["ma_availability"])

    def test_single_and_combined_preserve_identical_raw_records_manifests_and_report_schema(self):
        for availability in ("source-time", "observed"):
            with self.subTest(availability=availability):
                self.output = Path(self.temp.name) / availability / "combined"
                combined = self.generate(source_key="SH000852", availability=availability)
                _, combined_records = self.read("index")
                combined_bytes = (self.output / "index.csv").read_bytes()
                self.output = Path(self.temp.name) / availability / "single"
                with patch.object(example, "moving_average_records", side_effect=AssertionError("不应计算MA")):
                    single = self.generate(source_key="SH000852", availability=availability, index_only=True)
                _, single_records = self.read("index")
                self.assertEqual(single_records, combined_records)
                self.assertEqual((self.output / "index.csv").read_bytes(), combined_bytes)
                self.assertEqual(single["input_records_sha256"], combined["input_records_sha256"])
                self.assertEqual(single["input_config_sha256"], combined["input_config_sha256"])
                self.assertEqual(set(single), set(combined))
                self.assertEqual(set(single["index"]), set(combined["index"]))
                for key in set(single["index"]) - {"output", "manifest", "dataset_config"}:
                    self.assertEqual(single["index"][key], combined["index"][key])
                self.assertIsNone(single["ma"])
                self.assertIsNone(single["coverage"])
                self.assertIsNone(single["window"])
                self.assertIsNone(single["ma_availability"])
                self.assertEqual(len(list(self.output.iterdir())), 4)

    def test_single_index_does_not_require_ma_warmup_and_parquet_uses_same_readback(self):
        for suffix in (".csv", ".parquet"):
            with self.subTest(suffix=suffix):
                self.output = Path(self.temp.name) / suffix[1:]
                report = self.generate(count=1, index_only=True, suffix=suffix)
                self.assertEqual(report["index"]["rows"], 1)
                self.assertEqual(report["index"]["file_roundtrip"], "passed")
                self.assertTrue((self.output / ("index"+suffix+".dataset.json")).is_file())
                self.assertIsNone(report["ma"])
                self.assertIsNone(report["coverage"])

    def test_no_data_or_insufficient_warmup_writes_neither_package(self):
        for count, message in ((0, "没有可见记录"), (19, "连续20根")):
            with self.subTest(count=count), self.assertRaisesRegex(FeatureError, message):
                self.generate(count=count)
            self.assertFalse(self.output.exists())

    def test_existing_ma_sidecar_rejects_before_query_or_raw_export(self):
        self.output.mkdir()
        sidecar = self.output / "index-ma20.csv.dataset.json"
        sidecar.write_text("preserve")
        with patch.object(example.FeatureProviderFactory, "create_for") as create:
            with self.assertRaisesRegex(FeatureError, "输出已存在"):
                example.generate_features(start_ns=BASE, end_ns=BASE+19*M,
                    output_dir=self.output, connection=self.connection)
            create.assert_not_called()
        self.assertEqual(sidecar.read_text(), "preserve")
        self.assertFalse((self.output / "index.csv").exists())

    def test_cli_requires_connect_and_passes_actual_parameters(self):
        argv = ["--start", "2026-09-01T09:31:00+08:00", "--end", "2026-09-01T15:00:00+08:00",
            "--output-dir", str(self.output), "--source-key", "SH000852"]
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            example.main(argv)
        with patch("dotenv.load_dotenv"), \
                patch.object(DolphinDbConnectionConfig, "from_env", return_value=self.connection), \
                patch.object(example, "generate_features", return_value={"status": "passed"}) as run, \
                patch("sys.stdout", io.StringIO()) as stdout:
            example.main(["--connect", *argv])
        self.assertEqual(run.call_args.kwargs["window"], 20)
        self.assertEqual(run.call_args.kwargs["source_key"], "SH000852")
        self.assertEqual(run.call_args.kwargs["start_ns"], BASE)
        self.assertNotIn("fixture-secret", stdout.getvalue())

    def test_timestamp_preserves_microseconds_and_requires_timezone(self):
        self.assertEqual(example.timestamp("2026-09-01T09:31:00.123456+08:00"), BASE+123456000)
        with self.assertRaises(ValueError):
            example.timestamp("2026-09-01T09:31:00")
