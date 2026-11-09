"""真实Provider夹具→业务DataFrame组合→转换/导出；不联网、不报单。"""
from importlib import import_module
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from decimal import Decimal
from types import SimpleNamespace as NS

import pandas as pd

from bomber.framework.dataprep.features import DolphinDbRawFeatureProvider, FeatureRequest, load_feature_bundle
from bomber.framework.datahub.features import FeatureError
from bomber.framework.dolphindb import DolphinDbConnectionConfig
from tests.test_dolphindb_index_features import Session, definitions, row

example = import_module("demos.08_option_vega.feature.example_dolphindb")
feature = import_module("demos.08_option_vega.feature")
replay = import_module("demos.08_option_vega.feature.backtest")
BASE = example.timestamp("2026-09-01T09:31:00+08:00")
M, S = 60_000_000_000, 1_000_000_000


class DolphinDataFrameExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "features"
        self.connection = DolphinDbConnectionConfig("fixture", 8848, "user", "fixture-secret")

    def generate(self, events, *, config=example.CONFIG, dataset="index.minute", availability="source-time", schema=None,
                 output_time_mode="snapshot", assumed_delay_seconds=None, last_close=None):
        rows = [row(event=event, published=event+S, key="SH000852",
            close=6500+i*.2, open=6500, high=6600, low=6400) for i, event in enumerate(events)]
        if last_close is not None:
            rows[-1]["close"] = last_close
        session = Session(rows=rows, schema=schema)
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
                config=config, dataset=dataset, availability=availability,
                output_time_mode=output_time_mode, assumed_delay_seconds=assumed_delay_seconds)
        self.assertTrue(session.closed)
        self.assertEqual(sum(not sql.startswith("schema(") for sql in session.scripts), 1)
        self.assertIn('"SH000852"', session.scripts[-1])
        self.query = session.scripts[-1]
        return report

    def read_output(self, report):
        bundle = load_feature_bundle(report["dataset_config"], None)
        with bundle.provider(report["dataset_id"]) as provider:
            records = provider.read(FeatureRequest(report["dataset_id"], 1, ("000852",),
                BASE, BASE+120*M, example.time.time_ns())).records
        return bundle, bundle.binding(report["dataset_id"]), records

    def test_tinysoft_historical_research_roundtrip_and_frame_deadline(self):
        schema = pd.DataFrame({"name": ["TradeDate", "ActTime", "stockID", "closePrice"],
            "typeString": ["DATE", "TIMESTAMP", "SYMBOL", "DOUBLE"]})
        report = self.generate([BASE+i*M for i in range(120)],
            config=example.CONFIG.with_name("index_tinysoft.json"), dataset="index.close.minute",
            availability=None, schema=schema, output_time_mode="historical-research", assumed_delay_seconds="1.000000001")
        bundle, binding, records = self.read_output(report)
        self.assertIn('loadTable("dfs://JC_MKT_MIN","idx_1min_tiny")', self.query)
        self.assertIn("closePrice as p_close", self.query)
        self.assertIn("nanotimestamp(ActTime)", self.query)
        self.assertEqual(report["assumed_delay_ns"], S+1)
        self.assertEqual(report["output_time_mode"], "historical-research")
        self.assertEqual(report["input_availability"], "observed")
        self.assertFalse(report["historical_publication_verified"])
        self.assertTrue(all(record.available_ns == record.received_ns == record.ts_event+S+1 for record in records))
        self.assertGreater(report["generated_at_ns"], records[-1].visible_ns)
        self.assertGreater(report["input_visible_range_ns"][0], records[-1].visible_ns)
        self.assertIn("historical-research:delay-ns-1000000001", binding.spec.computation_version)
        self.assertTrue(all(record.source.startswith("research-assumption:") for record in records))
        self.assertTrue(all(record.quality_flags == 2 for record in records))
        bars = [NS(ts_event=BASE+i*M, ts_init=BASE+i*M,
            bar_type=NS(instrument_id=NS(symbol="IM2609")), close=NS(as_decimal=lambda: Decimal(6500)))
            for i in range(121)]
        outcomes, _ = replay.prepare_feature_frames(bars, bundle, binding, records, "000852", 5*S)
        self.assertEqual([result.status for result in outcomes], ["SKIPPED"]*59+["READY"]*61)
        # 延迟超过接收期限时仍拒绝；研究模式没有绕过公共可见性门槛。
        outcomes, _ = replay.prepare_feature_frames(bars, bundle, binding, records, "000852", S)
        self.assertTrue(all(result.status == "SKIPPED" for result in outcomes))

    def test_historical_research_formula_never_uses_later_rows(self):
        options = dict(config=example.CONFIG.with_name("index_close.json"), dataset="index.close.minute",
            availability=None, output_time_mode="historical-research", assumed_delay_seconds="1")
        report = self.generate([BASE+i*M for i in range(120)], **options)
        _, _, original = self.read_output(report)
        self.output = Path(self.temp.name) / "changed"
        changed_report = self.generate([BASE+i*M for i in range(120)], last_close=6550, **options)
        _, _, changed = self.read_output(changed_report)
        self.assertEqual([record.payload for record in original[:-1]], [record.payload for record in changed[:-1]])
        self.assertNotEqual(original[-1].payload.index_blend, changed[-1].payload.index_blend)
        self.assertNotEqual(report["input_records_sha256"], changed_report["input_records_sha256"])

    def test_research_assumption_is_explicit_and_does_not_overwrite_source_time(self):
        for mode, delay in (("historical-research", None), ("historical-research", "-1"),
                ("historical-research", "NaN"), ("historical-research", "Infinity"),
                ("historical-research", "0.0000000001"), ("historical-research", "1.0000000000000000000000000001"),
                ("snapshot", "1"), ("unknown", "1")):
            with self.subTest(mode=mode, delay=delay), patch.object(example.FeatureProviderFactory, "create_for") as create:
                with self.assertRaises(FeatureError):
                    example.run_example(start_ns=BASE, end_ns=BASE+119*M, output_dir=self.output,
                        connection=self.connection, output_time_mode=mode, assumed_delay_seconds=delay)
                create.assert_not_called()
        with self.assertRaisesRegex(FeatureError, "observed"), patch.object(example.FeatureProviderFactory, "create_for") as create:
            example.run_example(start_ns=BASE, end_ns=BASE+119*M, output_dir=self.output,
                connection=self.connection, availability="source-time",
                output_time_mode="historical-research", assumed_delay_seconds="1")
        create.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_zero_delay_is_explicit_and_delay_changes_schema_identity(self):
        fingerprints = []
        for delay in ("0", "2"):
            self.output = Path(self.temp.name) / f"delay-{delay}"
            report = self.generate([BASE+i*M for i in range(60)],
                config=example.CONFIG.with_name("index_close.json"), dataset="index.close.minute", availability=None,
                output_time_mode="historical-research", assumed_delay_seconds=delay)
            _, binding, records = self.read_output(report)
            self.assertEqual(records[0].visible_ns, BASE+59*M+int(delay)*S)
            fingerprints.append(binding.spec.fingerprint)
        self.assertNotEqual(*fingerprints)

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
        self.assertEqual(report["output_time_mode"], "snapshot")
        self.assertIsNone(report["assumed_delay_ns"])
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
            example.main(["--connect", *argv, "--output-time-mode", "historical-research", "--assumed-delay-seconds", "1"])
        self.assertEqual(run.call_args.kwargs["source_key"], "SH000852")
        self.assertEqual(run.call_args.kwargs["start_ns"], BASE)
        self.assertEqual(run.call_args.kwargs["output_time_mode"], "historical-research")
        self.assertEqual(run.call_args.kwargs["assumed_delay_seconds"], "1")
        self.assertNotIn("fixture-secret", stdout.getvalue())

    def test_cli_missing_research_delay_fails_before_loading_connection(self):
        with patch("sys.stderr", io.StringIO()), patch.object(DolphinDbConnectionConfig, "from_env") as connect:
            with self.assertRaises(SystemExit):
                example.main(["--connect", "--start", "2026-09-01T09:31:00+08:00",
                    "--end", "2026-09-01T11:30:00+08:00", "--output-dir", str(self.output),
                    "--output-time-mode", "historical-research"])
            connect.assert_not_called()

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
