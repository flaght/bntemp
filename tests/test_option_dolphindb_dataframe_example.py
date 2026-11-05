"""真实Provider夹具→业务DataFrame组合→转换/导出；不联网、不报单。"""
from importlib import import_module
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from decimal import Decimal

from bomber.framework.dataprep.features import DolphinDbRawFeatureProvider, FeatureRequest, load_feature_bundle
from bomber.framework.datahub.features import FeatureError
from bomber.framework.dolphindb import DolphinDbConnectionConfig
from tests.test_dolphindb_index_features import Session, definitions, row

example = import_module("demos.08_option_vega.feature.example_dolphindb")
feature = import_module("demos.08_option_vega.feature")
BASE = example.timestamp("2026-09-01T09:31:00+08:00")
M, S = 60_000_000_000, 1_000_000_000


class DolphinDataFrameExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "features"
        self.connection = DolphinDbConnectionConfig("fixture", 8848, "user", "fixture-secret")

    def generate(self, events, *, config=example.CONFIG, dataset="index.minute", availability="source-time", schema=None):
        session = Session(rows=[row(event=event, published=event+S, key="SH000852",
            close=6500+i*.2, open=6500, high=6600, low=6400) for i, event in enumerate(events)], schema=schema)
        original_create = example.FeatureProviderFactory.create_for
        def create(purpose, backend, config, registry, **options):
            if backend == "file":
                return original_create(purpose, backend, config, registry, **options)
            self.assertEqual(purpose.value, "feature_export")
            return DolphinDbRawFeatureProvider(config, registry, session_factory=lambda **kw: session,
                clock_ns=lambda: max(events, default=BASE)+10*S)
        with patch.object(example.FeatureProviderFactory, "create_for", side_effect=create):
            report = example.run_example(start_ns=BASE, end_ns=max(events, default=BASE),
                output_dir=self.output, connection=self.connection, source_key="SH000852",
                config=config, dataset=dataset, availability=availability)
        self.assertTrue(session.closed)
        self.assertEqual(sum(not sql.startswith("schema(") for sql in session.scripts), 1)
        self.assertIn('"SH000852"', session.scripts[-1])
        self.query = session.scripts[-1]
        return report

    def test_close_only_declaration_needs_no_ohlcv_projection_or_update_time(self):
        schema = definitions()
        schema = schema[schema.name.isin(("date", "minTime", "Code", "close"))]
        report = self.generate([BASE+i*M for i in range(120)],
            config=example.CONFIG.with_name("index_close.json"), dataset="index.close.minute",
            availability=None, schema=schema)
        self.assertEqual(report["feature_rows"], 61)
        self.assertEqual(report["input_availability"], "observed")
        self.assertEqual(report["file_roundtrip"], "passed")
        self.assertIn("close as p_close", self.query)
        for unused in ("p_open", "p_volume", "p_turnover", "updateTime"):
            self.assertNotIn(unused, self.query)

    def test_provider_to_custom_formula_to_standard_file(self):
        report = self.generate([BASE+i*M for i in range(120)])
        self.assertEqual(report["status"], "passed")
        self.assertFalse(report["synthetic"])
        self.assertEqual((report["input_rows"], report["feature_rows"]), (120, 61))
        self.assertEqual(report["coverage"]["warmup_skipped"], 59)
        self.assertEqual(report["file_roundtrip"], "passed")
        self.assertEqual(len(list(self.output.iterdir())), 4)
        bundle = load_feature_bundle(report["dataset_config"], None)
        with bundle.provider(report["dataset_id"]) as provider:
            records = provider.read(FeatureRequest(report["dataset_id"], 1, ("000852",),
                BASE, BASE+119*M, example.time.time_ns())).records
        self.assertAlmostEqual(records[0].payload.index_blend, 6507.9)
        self.assertEqual(records[0].source_ns, BASE+59*M)
        self.assertGreater(records[0].available_ns, BASE+119*M)
        self.assertEqual(report["source_key"], "SH000852")
        self.assertNotIn("fixture-secret", str(report))

    def test_business_formula_keeps_row_windows_across_lunch(self):
        afternoon = example.timestamp("2026-09-01T13:01:00+08:00")
        report = self.generate([BASE+i*M for i in range(60)] + [afternoon+i*M for i in range(60)])
        self.assertEqual(report["feature_rows"], 61)
        self.assertEqual(report["coverage"]["window_policy"], "rows")
        self.assertEqual(report["coverage"]["warmup_skipped"], 59)

    def test_empty_or_short_input_does_not_publish_files(self):
        for count in (0, 59):
            with self.subTest(count=count), self.assertRaises(FeatureError):
                self.generate([BASE+i*M for i in range(count)])
            self.assertFalse(self.output.exists())

    def test_cli_requires_connect_and_passes_source_configuration(self):
        argv = ["--start", "2026-09-01T09:31:00+08:00", "--end", "2026-09-01T11:30:00+08:00",
            "--output-dir", str(self.output), "--source-key", "SH000852"]
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            example.main(argv)
        with patch("dotenv.load_dotenv"), \
                patch.object(DolphinDbConnectionConfig, "from_env", return_value=self.connection), \
                patch.object(example, "run_example", return_value={"status": "passed"}) as run, \
                patch("sys.stdout", io.StringIO()) as stdout:
            example.main(["--connect", *argv])
        self.assertEqual(run.call_args.kwargs["source_key"], "SH000852")
        self.assertEqual(run.call_args.kwargs["start_ns"], BASE)
        self.assertNotIn("fixture-secret", stdout.getvalue())

    def test_connection_config_reader_returns_dataframe_and_preserves_all_source_revisions(self):
        definition = json.loads(example.CONFIG.read_text())
        definition["datasets"][0]["source"]["availability"] = "source-time"
        config = Path(self.temp.name) / "source-time.json"
        config.write_text(json.dumps(definition))
        rows = [row(event=BASE, published=BASE+1, key="SH000852"),
            row(event=BASE, published=BASE+2, key="SH000852", close=6402),
            row(event=BASE+M, published=BASE+M+3, key="SH000852")]
        for values in (rows, []):
            with self.subTest(rows=len(values)):
                session = Session(rows=values)
                def create(purpose, backend, source_config, registry, **options):
                    self.assertEqual(purpose.value, "feature_export")
                    self.assertEqual(backend, "dolphindb-raw")
                    return DolphinDbRawFeatureProvider(source_config, registry,
                        session_factory=lambda **kw: session, clock_ns=lambda: BASE+2*M)
                with patch.object(feature.FeatureProviderFactory, "create_for", side_effect=create):
                    frame = feature.read_index_dataframe(self.connection, config,
                        start_ns=BASE, end_ns=BASE+M, key="SH000852")
                self.assertTrue(session.closed)
                self.assertEqual(frame.index.name, "ts_event")
                self.assertEqual(len(frame), len(values))
                self.assertIn("close", frame.columns)
                self.assertIn("available_ns", frame.columns)
                self.assertEqual(frame.attrs["dataset_id"], "index.minute")
                if values:
                    self.assertEqual(frame.index.tolist(), [BASE, BASE, BASE+M])
                    self.assertEqual(frame["revision"].tolist(), [BASE+1, BASE+2, BASE+M+3])
                    self.assertEqual(frame["available_ns"].tolist(), frame["revision"].tolist())
                    self.assertEqual(frame["key"].tolist(), ["SH000852"]*3)
                    self.assertEqual(frame["close"].iloc[0], Decimal("6401.2"))
                    self.assertEqual(frame["received_ns"].tolist(), [None]*3)
                    self.assertTrue(all(type(value) is int for value in frame["available_ns"]))
