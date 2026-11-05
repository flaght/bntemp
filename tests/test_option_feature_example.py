"""08目录内双文件特征例子：真实Provider和标准文件，不联网、不发单。"""
from decimal import Decimal
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
from bomber.framework.datahub.features import FeatureError
from tests.test_dolphindb_index_features import Session, row

example = import_module("demos.08_option_vega.feature.generate")
features = import_module("demos.08_option_vega.features")
M, S = 60_000_000_000, 1_000_000_000
BASE = example.timestamp("2026-09-01T09:31:00+08:00")


class CombinedFeatureExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "features"
        self.connection = DolphinDbConnectionConfig("fixture", 8848, "user", "fixture-secret")

    def generate(self, *, count=20, source_key=None, availability="source-time"):
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
        self.assertEqual(report["warmup_skipped"], 19)
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

    def test_no_data_or_insufficient_warmup_writes_neither_package(self):
        for count, message in ((0, "没有可见指数"), (19, "连续20根")):
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
