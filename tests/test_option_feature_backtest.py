"""08特征文件、Live声明与模拟可见时间的离线回归，无数据库/柜台连接。"""
from dataclasses import replace
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from bomber.framework.datahub.features import ExternalDataRecord, FeatureError
from bomber.framework.dataprep.features import DEFAULT_FEATURE_CONFIG, export_features, load_feature_bundle
from bomber.framework.dataprep.sources.dolphindb import DolphinDbReferenceConfig

features = import_module("demos.08_option_vega.features")
replay = import_module("demos.08_option_vega.features")
M, S = 60_000_000_000, 1_000_000_000
END = 1_791_767_460_000_000_000 // M * M


class FeatureBacktestTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "index.csv"
        self.bundle, self.binding = features.index_bundle(DEFAULT_FEATURE_CONFIG,
            "index.minute", 1, file_path=self.path)

    def record(self, *, event=END, visible=None, received=None, revision=0, close="6400"):
        payload = self.binding.spec.payload_type(**{name: Decimal(close) for name in
            ("open", "high", "low", "close")}, volume=Decimal(0), turnover=Decimal(0),
            prev_close=None, pct_change=None)
        return ExternalDataRecord("index.minute", 1, "000852", payload, event,
            event if visible is None else visible, event, revision, "fixture", received_ns=received)

    def bar(self, event=END, received=None, key="IM2610"):
        return NS(ts_event=event, ts_init=event if received is None else received,
            bar_type=NS(instrument_id=NS(symbol=key)), close=NS(as_decimal=lambda: Decimal(6500)))

    def prepare(self, records, bars=None):
        return replay.prepare_feature_frames(bars or [self.bar(), self.bar(END+M)],
            self.bundle, self.binding, records, "000852", 2*S)

    def test_file_and_live_share_schema_and_computation_version(self):
        live = load_feature_bundle(DEFAULT_FEATURE_CONFIG,
            DolphinDbReferenceConfig("fixture", 8848, "user", "secret"))
        self.assertEqual(self.binding.spec.fingerprint, live.binding("index.minute").spec.fingerprint)
        export_features(self.path, self.bundle.registry, [self.record(received=END+S)])
        args = NS(index_feature_config=DEFAULT_FEATURE_CONFIG, index_dataset="index.minute",
            index_schema_version=1, index_code="000852", index_feature_file=self.path)
        _, _, records, identity = features.read_index_features(args, END, END+M)
        self.assertEqual(records[0].received_ns, END+S)
        self.assertEqual(identity["computation_version"], self.binding.spec.computation_version)
        self.assertIn(str(self.path.resolve()), identity["source"])

    def test_incompatible_computation_version_fails_manifest_validation(self):
        export_features(self.path, self.bundle.registry, [self.record()])
        definition = json.loads(DEFAULT_FEATURE_CONFIG.read_text())
        definition["datasets"][0]["schema"]["computation_version"] = "other-calculation"
        config = Path(self.temp.name) / "features.json"
        config.write_text(json.dumps(definition))
        args = NS(index_feature_config=config, index_dataset="index.minute",
            index_schema_version=1, index_code="000852", index_feature_file=self.path)
        with self.assertRaisesRegex(FeatureError, "计算版本"):
            features.read_index_features(args, END, END+M)

    def test_custom_dataset_version_roundtrips_and_drives_same_feature_frame(self):
        definition = json.loads(DEFAULT_FEATURE_CONFIG.read_text())
        schema = definition["datasets"][0]["schema"]
        schema.update(dataset_id="research.custom_index", schema_version=2,
                      computation_version="custom-index-v1")
        config = Path(self.temp.name) / "custom-features.json"
        config.write_text(json.dumps(definition))
        bundle, binding = features.index_bundle(config, "research.custom_index", 2, file_path=self.path)
        payload = binding.spec.payload_type(open=Decimal(7777), high=Decimal(7777),
            low=Decimal(7777), close=Decimal(7777), volume=Decimal(0), turnover=Decimal(0),
            prev_close=None, pct_change=None)
        record = ExternalDataRecord("research.custom_index", 2, "000852", payload,
            END, END, END, 0, "custom-fixture", received_ns=END+S)
        export_features(self.path, bundle.registry, [record])
        args = NS(index_feature_config=config, index_dataset="research.custom_index",
            index_schema_version=2, index_code="000852", index_feature_file=self.path)
        loaded, selected, records, identity = features.read_index_features(args, END, END+M)
        outcomes, _ = features.prepare_feature_frames([self.bar(), self.bar(END+M)],
            loaded, selected, records, "000852", 2*S)
        self.assertEqual(identity["computation_version"], binding.spec.computation_version)
        self.assertTrue(identity["computation_version"].startswith("custom-index-v1:declaration:"))
        self.assertEqual(identity["dataset_id"], "research.custom_index")
        self.assertEqual(outcomes[0].status, "READY")
        self.assertEqual(outcomes[0].frame.features.latest("index").payload.close, Decimal(7777))
        self.assertNotEqual(selected.spec.fingerprint, self.binding.spec.fingerprint)

    def test_deadline_inclusive_and_received_time_controls_visibility(self):
        for delay, status in ((2*S, "READY"), (2*S+1, "SKIPPED")):
            with self.subTest(delay=delay):
                rows, deadlines = self.prepare([self.record(received=END+delay)])
                self.assertEqual(rows[0].status, status)
                self.assertEqual(deadlines[END], END+2*S)

    def test_future_revision_does_not_change_already_selected_minute(self):
        rows, _ = self.prepare([self.record(visible=END+S),
            self.record(visible=END+3*S, revision=1, close="9999")])
        record = rows[0].frame.features.latest("index")
        self.assertEqual(record.revision, 0)
        self.assertEqual(record.payload.close, Decimal(6400))

    def test_first_visible_revision_is_frozen_like_live_projection(self):
        rows, _ = self.prepare([self.record(visible=END+S),
            self.record(visible=END+2*S, revision=1, close="6500")])
        self.assertEqual(rows[0].frame.features.latest("index").revision, 0)

    def test_missing_same_minute_is_skipped_without_asof_fill(self):
        rows, _ = self.prepare([self.record(event=END-M)])
        self.assertEqual(rows[0].status, "SKIPPED")
        self.assertIn("MISSING", rows[0].reason)

    def test_late_market_leg_does_not_enter_closed_frame(self):
        rows, _ = self.prepare([self.record()], [self.bar(),
            self.bar(received=END+2*S+1, key="MO2610-C-7000"), self.bar(END+M)])
        self.assertNotIn("MO2610-C-7000", rows[0].frame.market)

    def test_market_leg_at_deadline_is_included(self):
        rows, _ = self.prepare([self.record()], [self.bar(),
            self.bar(received=END+2*S, key="MO2610-C-7000"), self.bar(END+M)])
        self.assertIn("MO2610-C-7000", rows[0].frame.market)

    def test_quality_failure_is_not_treated_as_missing_index(self):
        with self.assertRaisesRegex(FeatureError, "质量"):
            self.prepare([replace(self.record(), quality_flags=1)])

    def test_original_strategy_receives_frame_at_native_clock_deadline(self):
        outcomes, deadlines = self.prepare([self.record(received=END+S)])
        timers, delivered = [], []
        runner = replay.build_backtest_runner(positions=None, feed=None,
            backend=NS(engine=NS(kernel=NS(clock=NS(set_time_alert_ns=lambda **kw: timers.append(kw))))),
            bundle=self.bundle, binding=self.binding, records=(), index_key="000852", wait_seconds=2)
        runner.outcomes, runner.deadlines = {r.event_ns: r for r in outcomes}, deadlines
        runner.strategy = NS(on_market_frame=delivered.append)
        runner.publish("option-bars", self.bar())
        runner.publish("option-bars", self.bar(key="MO2610-C-7000"))
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0]["alert_time_ns"], END+2*S+1)
        self.assertEqual(delivered, [])
        timers[0]["callback"](NS(ts_event=END+2*S+1))
        self.assertEqual(delivered[0].request_ns, END+2*S+1)
        self.assertEqual(delivered[0].observations["000852"].received_ns, END+S)


class FeatureGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "index.csv"
        self.connection = DolphinDbReferenceConfig("fixture", 8848, "user", "fixture-secret")
        self.args = NS(output=self.path, index_feature_config=DEFAULT_FEATURE_CONFIG,
            index_dataset="index.minute", index_schema_version=1, index_code="000852",
            index_availability=None, max_rows=100)

    def generate(self, *, rows=None, availability=None):
        from tests.test_dolphindb_index_features import Session
        from bomber.framework.dataprep.features import DolphinDbRawFeatureProvider
        session = Session(rows=rows)
        self.args.index_availability = availability
        def create(purpose, backend, config, registry, **options):
            self.assertEqual(purpose.value, "feature_export")
            self.assertEqual(backend, "dolphindb-raw")
            return DolphinDbRawFeatureProvider(config, registry,
                session_factory=lambda **kwargs: session, clock_ns=lambda: END+10*S)
        with patch.object(features.FeatureProviderFactory, "create_for", side_effect=create), \
                patch.object(features.time, "time_ns", return_value=END+10*S):
            report = features.generate_index_features(self.args, END, END, connection=self.connection)
        self.assertTrue(session.closed)
        return report

    def read(self):
        args = NS(**vars(self.args), index_feature_file=self.path)
        return features.read_index_features(args, END, END+M)

    def test_dolphindb_to_file_to_original_frame_keeps_historical_source_time(self):
        report = self.generate(availability="source-time")
        bundle, binding, records, identity = self.read()
        self.assertEqual(report["file_roundtrip"], "passed")
        self.assertEqual(report["records"], 1)
        self.assertEqual(report["availability"], "source-time")
        self.assertEqual(records[0].available_ns, END+100)
        self.assertEqual(records[0].payload.close, Decimal("6401.2"))
        self.assertEqual(identity["schema_fingerprint"], report["export_manifest"]["schema_fingerprint"])
        bars = [NS(ts_event=t, ts_init=t, bar_type=NS(instrument_id=NS(symbol="IM2610")),
            close=NS(as_decimal=lambda: Decimal(6500))) for t in (END, END+M)]
        outcomes, _ = features.prepare_feature_frames(bars, bundle, binding, records, "000852", 2*S)
        self.assertEqual(outcomes[0].status, "READY")
        self.assertEqual(outcomes[0].frame.features.latest("index").payload.close, Decimal("6401.2"))
        self.assertNotIn("fixture-secret", str(report))

    def test_observed_export_keeps_observation_time_and_cannot_enter_earlier_frame(self):
        report = self.generate()
        bundle, binding, records, _ = self.read()
        self.assertEqual(report["availability"], "observed")
        self.assertEqual(records[0].available_ns, END+10*S)
        self.assertEqual(report["max_visibility_delay_seconds"], 10)
        bars = [NS(ts_event=t, ts_init=t, bar_type=NS(instrument_id=NS(symbol="IM2610")),
            close=NS(as_decimal=lambda: Decimal(6500))) for t in (END, END+M)]
        outcomes, _ = features.prepare_feature_frames(bars, bundle, binding, records, "000852", 2*S)
        self.assertEqual(outcomes[0].status, "SKIPPED")

    def test_empty_source_does_not_create_file_or_manifest(self):
        with self.assertRaisesRegex(FeatureError, "没有可见记录"):
            self.generate(rows=[])
        self.assertFalse(self.path.exists())
        self.assertFalse(Path(str(self.path)+".manifest.json").exists())

    def test_existing_output_or_manifest_fails_before_query(self):
        for existing in (self.path, Path(str(self.path)+".manifest.json")):
            with self.subTest(existing=existing):
                existing.write_text("preserved")
                try:
                    with patch.object(features.FeatureProviderFactory, "create_for") as create:
                        with self.assertRaisesRegex(FeatureError, "已存在"):
                            features.generate_index_features(self.args, END, END, connection=self.connection)
                        create.assert_not_called()
                    self.assertEqual(existing.read_text(), "preserved")
                finally:
                    existing.unlink()

    def test_source_schema_failure_does_not_create_file(self):
        from tests.test_dolphindb_index_features import row
        with self.assertRaises(FeatureError):
            self.generate(rows=[row(close=-1)])
        self.assertFalse(self.path.exists())

    def test_cli_requires_explicit_connection_and_valid_identity_before_credentials(self):
        from contextlib import redirect_stderr
        from io import StringIO
        base = ["--start-day", "2026-09-01", "--end-day", "2026-09-11", "--output", str(self.path)]
        for extra in ([], ["--connect", "--index-code", "bad"]):
            with redirect_stderr(StringIO()), patch(
                    "bomber.framework.dolphindb.DolphinDbConnectionConfig.from_env") as env:
                with self.assertRaises(SystemExit):
                    features.main(base+extra)
                env.assert_not_called()

    def test_cli_selects_range_and_dolphindb_purpose_without_exposing_credentials(self):
        from contextlib import redirect_stdout
        from io import StringIO
        stdout = StringIO()
        with redirect_stdout(stdout), patch("dotenv.load_dotenv"), patch(
                "bomber.framework.dolphindb.DolphinDbConnectionConfig.from_env",
                return_value=self.connection) as env, patch.object(
                features, "generate_index_features", return_value={"status": "passed"}) as generate:
            features.main(["--connect", "--start-day", "2026-09-01", "--end-day", "2026-09-11",
                "--index-availability", "source-time", "--output", str(self.path)])
        env.assert_called_once_with(purpose="market", read_timeout_seconds=30)
        args, start, end = generate.call_args.args
        from tests.test_demo_option_vega import ns
        self.assertEqual(start, ns("2026-09-01 00:00"))
        self.assertEqual(end, ns("2026-09-12 00:00")-1)
        self.assertEqual(args.index_availability, "source-time")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "passed")
        self.assertNotIn("fixture-secret", stdout.getvalue())


class Ma20BacktestTests(unittest.TestCase):
    """真实20条输入→标准MA包→字段选择→原策略选约；不模拟交易所成交。"""
    def setUp(self):
        from tests import test_demo_option_vega as fixture
        from bomber.framework.datahub.features import dataset_from_definition, DatasetRegistry
        moving_average_definition = import_module("demos.08_option_vega.feature.generate").moving_average_definition
        self.fixture = fixture
        fixture.StrategyTests.setUpClass()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ma20.csv"
        self.config = Path(str(self.path) + ".dataset.json")
        self.stamp = fixture.ns("2026-08-13 13:58")
        self.input = dataset_from_definition({"dataset_id": "fixture.index.minute", "schema_version": 1,
            "fields": [{"name": "close", "kind": "decimal", "minimum": "0", "exclusive_minimum": True}]})
        self.definition = moving_average_definition(20)
        self.output = dataset_from_definition(self.definition)
        self.registry = DatasetRegistry((self.output,))
        self.config.write_text(json.dumps({"format_version": 1, "datasets": [{
            "schema": self.definition, "source": {"backend": "file", "path": self.path.name}}]}))

    def inputs(self, count=20):
        return tuple(ExternalDataRecord(self.input.dataset_id, 1, "000852",
            self.input.payload_type(close=Decimal(6700 if i == 19 else 6490)),
            self.stamp-(19-i)*M, self.stamp-(19-i)*M+S//10,
            self.stamp-(19-i)*M, 0, "minute-fixture") for i in range(count))

    def export(self, delay=S//2):
        moving_average_records = import_module("demos.08_option_vega.feature.generate").moving_average_records
        records, coverage = moving_average_records(self.inputs(), self.input, self.output,
            "000852", 20, clock_ns=lambda: self.stamp+delay)
        export_features(self.path, self.registry, records)
        return records, coverage

    def args(self, field):
        return NS(index_feature_file=self.path, index_feature_config=self.config,
            index_dataset=self.output.dataset_id, index_schema_version=1, index_code="000852",
            index_value_field=field)

    def decide(self, field):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        bundle, binding, records, identity = features.read_index_features(self.args(field), self.stamp, self.stamp)
        f = self.fixture.StrategyTests()
        f.setUp()
        strategy = f.strategy
        now = datetime.fromtimestamp(self.stamp/1e9, ZoneInfo("Asia/Shanghai"))
        signal = import_module("demos.08_option_vega.vega_signal")
        premium = signal.call_price(6500, 6600,
            signal.source_years(now, strategy._remaining_days(now.date(), f.info.last_day)), .02, .25)
        def bar(key, stamp, value):
            return NS(ts_event=stamp, ts_init=stamp, bar_type=NS(instrument_id=NS(symbol=key)),
                close=NS(as_decimal=lambda: Decimal(str(value))))
        bars = [bar("IM2609", self.stamp, 6500), bar(f.info.symbol, self.stamp, premium),
                bar("IM2609", self.stamp+M, 6500)]
        outcomes, deadlines = features.prepare_feature_frames(bars, bundle, binding, records, "000852", 2*S)
        timers = []
        runner = features.build_backtest_runner(positions=None, feed=None,
            backend=NS(engine=NS(kernel=NS(clock=NS(set_time_alert_ns=lambda **kw: timers.append(kw))))),
            bundle=bundle, binding=binding, records=records, index_key="000852", wait_seconds=2,
            index_value_field=field)
        runner.strategy = strategy
        runner.outcomes, runner.deadlines = {r.event_ns: r for r in outcomes}, deadlines
        runner.publish("option-bars", bars[0])
        runner.publish("option-bars", bars[1])
        timers[0]["callback"](NS(ts_event=self.stamp+2*S+1))
        return f, runner, identity, now

    def test_raw_close_vs_ma20_changes_original_candidate_filter_not_pricing_inputs(self):
        records, coverage = self.export()
        self.assertEqual(records[0].payload.ma20, Decimal("6500.5"))
        self.assertEqual(coverage["warmup_skipped"], 19)
        raw, raw_runner, raw_identity, now = self.decide("close")
        ma, ma_runner, ma_identity, _ = self.decide("ma20")
        self.assertEqual(raw.strategy.latest["000852"][1], 6700)
        self.assertEqual(ma.strategy.latest["000852"][1], 6500.5)
        self.assertEqual(raw.strategy.entry_audit[-1]["candidate_count"], 0)
        self.assertEqual(ma.strategy.entry_audit[-1]["candidate_count"], 1)
        self.assertEqual(raw.ctx.intents, [])
        self.assertTrue(ma.ctx.intents)
        for key in (raw.info.symbol, "IM2609"):
            self.assertEqual(raw.strategy.latest[key], ma.strategy.latest[key])
        self.assertEqual(raw.strategy._greeks(raw.info.symbol, self.stamp, now),
                         ma.strategy._greeks(ma.info.symbol, self.stamp, now))
        self.assertEqual(raw_identity["value_field"], "close")
        self.assertEqual(ma_identity["value_field"], "ma20")
        self.assertEqual(ma_runner.frame_audit[0]["value_field"], "ma20")
        self.assertNotIn("bar_projection", json.loads(self.config.read_text())["datasets"][0])

    def test_late_ma20_is_skipped_without_backdating_calculation(self):
        records, _ = self.export(delay=3*S)
        f, runner, _, _ = self.decide("ma20")
        self.assertEqual(records[0].available_ns, self.stamp+3*S)
        self.assertEqual(runner.frame_audit[0]["status"], "SKIPPED")
        self.assertEqual(f.ctx.intents, [])

    def test_warmup_and_gap_require_twenty_complete_consecutive_minutes(self):
        moving_average_records = import_module("demos.08_option_vega.feature.generate").moving_average_records
        records, _ = moving_average_records(self.inputs(19), self.input, self.output,
            "000852", 20, clock_ns=lambda: self.stamp+S)
        self.assertEqual(records, ())
        inputs = self.inputs(19) + (replace(self.inputs()[-1], ts_event=self.stamp+M,
            available_ns=self.stamp+M+S//10, source_ns=self.stamp+M),)
        records, coverage = moving_average_records(inputs, self.input, self.output,
            "000852", 20, clock_ns=lambda: self.stamp+M+S)
        self.assertEqual(records, ())
        self.assertEqual(coverage["window_resets"], 1)

    def test_scalar_selection_is_explicit_and_must_name_a_numeric_field(self):
        self.export()
        for field in (None, "missing"):
            with self.subTest(field=field), self.assertRaises(FeatureError):
                features.read_index_features(self.args(field), self.stamp, self.stamp)
        with self.assertRaisesRegex(ValueError, "尚未可见"):
            moving_average_records = import_module("demos.08_option_vega.feature.generate").moving_average_records
            moving_average_records(self.inputs(), self.input, self.output,
                "000852", 20, clock_ns=lambda: self.stamp)

    def test_backtest_cli_passes_ma20_field_without_connecting_to_sources(self):
        entry = import_module("demos.08_option_vega.run_backtest")
        with patch.object(entry, "run_case") as run, patch("sys.argv", ["run_backtest.py",
                "--start-day", "2026-08-13", "--end-day", "2026-08-13",
                "--index-feature-file", str(self.path), "--index-feature-config", str(self.config),
                "--index-dataset", self.output.dataset_id, "--index-value-field", "ma20"]):
            entry.main()
        self.assertEqual(run.call_args.args[0].index_value_field, "ma20")
