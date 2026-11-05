"""真实Provider夹具→业务DataFrame组合→转换/导出；不联网、不报单。"""
from importlib import import_module
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bomber.framework.dataprep.features import DolphinDbRawFeatureProvider, FeatureRequest, load_feature_bundle
from bomber.framework.datahub.features import FeatureError
from bomber.framework.dolphindb import DolphinDbConnectionConfig
from tests.test_dolphindb_index_features import Session, row

example = import_module("demos.08_option_vega.feature.example_dolphindb")
BASE = example.timestamp("2026-09-01T09:31:00+08:00")
M, S = 60_000_000_000, 1_000_000_000


class DolphinDataFrameExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "features"
        self.connection = DolphinDbConnectionConfig("fixture", 8848, "user", "fixture-secret")

    def generate(self, events):
        session = Session(rows=[row(event=event, published=event+S, key="SH000852",
            close=6500+i*.2, open=6500, high=6600, low=6400) for i, event in enumerate(events)])
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
                availability="source-time")
        self.assertTrue(session.closed)
        self.assertEqual(sum(not sql.startswith("schema(") for sql in session.scripts), 1)
        self.assertIn('"SH000852"', session.scripts[-1])
        return report

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

    def test_lunch_gap_resets_both_windows(self):
        afternoon = example.timestamp("2026-09-01T13:01:00+08:00")
        report = self.generate([BASE+i*M for i in range(60)] + [afternoon+i*M for i in range(60)])
        self.assertEqual(report["feature_rows"], 2)
        self.assertEqual(report["coverage"]["window_resets"], 1)
        self.assertEqual(report["coverage"]["warmup_skipped"], 118)

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
