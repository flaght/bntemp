"""08特征文件、Live声明与模拟可见时间的离线回归，无数据库/柜台连接。"""
from dataclasses import replace
from datetime import date
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

features = import_module("demos.08_option_vega.feature")
replay = import_module("demos.08_option_vega.feature.backtest")
exporter = import_module("demos.08_option_vega.feature.generate")
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
        outcomes, _ = replay.prepare_feature_frames([self.bar(), self.bar(END+M)],
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

    def test_all_invisible_features_fail_before_native_replay(self):
        for records in ([self.record(visible=END+100*M)],
                [self.record(received=END+100*M)], [self.record(event=END-M)]):
            with self.subTest(records=records):
                runner = replay.build_backtest_runner(positions=None,
                    feed=NS(load_events=lambda: [self.bar(), self.bar(END+M)]), backend=None,
                    bundle=self.bundle, binding=self.binding, records=records, index_key="000852", wait_seconds=2)
                with patch.object(type(runner).__mro__[1], "run_replay") as native:
                    with self.assertRaisesRegex(FeatureError, "全部历史分钟帧.*available_ns/received_ns"):
                        runner.run_replay()
                    native.assert_not_called()

    def test_partial_missing_features_keep_warmup_skips_and_ready_frames(self):
        runner = replay.build_backtest_runner(positions=None,
            feed=NS(load_events=lambda: [self.bar(), self.bar(END+M), self.bar(END+2*M)]), backend=None,
            bundle=self.bundle, binding=self.binding, records=[self.record(event=END+M)],
            index_key="000852", wait_seconds=2)
        def native_replay():
            runner.frame_audit = [dict(status=row.status) for row in runner.outcomes.values()]
            return "completed"
        with patch.object(type(runner).__mro__[1], "run_replay", side_effect=native_replay) as native:
            self.assertEqual(runner.run_replay(), "completed")
            native.assert_called_once()
        self.assertEqual([row["status"] for row in runner.frame_audit], ["SKIPPED", "READY"])

    def test_diagnostics_count_decisions_without_changing_feature_frames_or_timer_boundaries(self):
        from bomber.framework.performance import PerformanceStats
        stats, delivered = PerformanceStats(), []
        runner = replay.build_backtest_runner(positions=None,
            feed=NS(load_events=lambda: [self.bar(), self.bar(END+M), self.bar(END+2*M)]), backend=None,
            bundle=self.bundle, binding=self.binding, records=[self.record(event=END+M)],
            index_key="000852", wait_seconds=2, performance=stats)
        runner.strategy = NS(on_market_frame=delivered.append)
        def native_replay():
            for stamp in runner.outcomes:
                runner._close_frame(stamp, runner.deadlines[stamp]+1)
            return "completed"
        with patch.object(type(runner).__mro__[1], "run_replay", side_effect=native_replay):
            self.assertEqual(runner.run_replay(), "completed")
        self.assertEqual([row["status"] for row in runner.frame_audit], ["SKIPPED", "READY"])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].request_ns, END+M+2*S+1)
        self.assertEqual(stats.counters, {"index_feature_records": 1,
            "decision_frames": 2, "ready_frames": 1, "skipped_frames": 1})
        for name in ("feature_alignment", "strategy_decision", "replay_dispatch_including_engine"):
            self.assertEqual(stats.timings[name]["calls"], 1)

    def test_replay_progress_distinguishes_loading_alignment_and_native_simulation(self):
        from contextlib import redirect_stdout
        from io import StringIO
        runner = replay.build_backtest_runner(positions=None,
            feed=NS(load_events=lambda: [self.bar(), self.bar(END+M)]), backend=None,
            bundle=self.bundle, binding=self.binding, records=[self.record()], index_key='000852', wait_seconds=2)
        def native_replay():
            runner.frame_audit = [dict(status=row.status) for row in runner.outcomes.values()]
            return 'completed'
        output = StringIO()
        with redirect_stdout(output), patch.object(type(runner).__mro__[1], 'run_replay', side_effect=native_replay):
            self.assertEqual(runner.run_replay(), 'completed')
        messages = output.getvalue()
        self.assertLess(messages.index('开始转换与排序'), messages.index('开始指数特征对齐'))
        self.assertLess(messages.index('特征对齐完成'), messages.index('开始推进模拟时间'))
        self.assertIn('events=2', messages)
        self.assertIn('frames=1 ready=1 skipped=0', messages)


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
        original_create = exporter.FeatureProviderFactory.create_for
        def create(purpose, backend, config, registry, **options):
            if backend == "file":
                return original_create(purpose, backend, config, registry, **options)
            self.assertEqual(purpose.value, "feature_export")
            self.assertEqual(backend, "dolphindb-raw")
            return DolphinDbRawFeatureProvider(config, registry,
                session_factory=lambda **kwargs: session, clock_ns=lambda: END+10*S)
        with patch.object(exporter.FeatureProviderFactory, "create_for", side_effect=create), \
                patch.object(exporter.time, "time_ns", return_value=END+10*S):
            report = exporter.generate_index_features(self.args, END, END, connection=self.connection)
        self.assertTrue(session.closed)
        return report

    def read(self):
        args = NS(**vars(self.args), index_feature_file=self.path)
        return features.read_index_features(args, END, END+M)

    def test_dolphindb_to_file_to_original_frame_keeps_historical_source_time(self):
        report = self.generate(availability="source-time")
        bundle, binding, records, identity = self.read()
        self.assertEqual(report["index"]["file_roundtrip"], "passed")
        self.assertEqual(report["index"]["rows"], 1)
        self.assertEqual(report["index_availability"], "source-time")
        self.assertEqual(records[0].available_ns, END+100)
        self.assertEqual(records[0].payload.close, Decimal("6401.2"))
        self.assertEqual(identity["schema_fingerprint"], report["index"]["schema_fingerprint"])
        bars = [NS(ts_event=t, ts_init=t, bar_type=NS(instrument_id=NS(symbol="IM2610")),
            close=NS(as_decimal=lambda: Decimal(6500))) for t in (END, END+M)]
        outcomes, _ = replay.prepare_feature_frames(bars, bundle, binding, records, "000852", 2*S)
        self.assertEqual(outcomes[0].status, "READY")
        self.assertEqual(outcomes[0].frame.features.latest("index").payload.close, Decimal("6401.2"))
        self.assertNotIn("fixture-secret", str(report))

    def test_observed_export_keeps_observation_time_and_cannot_enter_earlier_frame(self):
        report = self.generate()
        bundle, binding, records, _ = self.read()
        self.assertEqual(report["index_availability"], "observed")
        self.assertEqual(records[0].available_ns, END+10*S)
        self.assertEqual(report["index"]["max_visibility_delay_seconds"], 10)
        bars = [NS(ts_event=t, ts_init=t, bar_type=NS(instrument_id=NS(symbol="IM2610")),
            close=NS(as_decimal=lambda: Decimal(6500))) for t in (END, END+M)]
        outcomes, _ = replay.prepare_feature_frames(bars, bundle, binding, records, "000852", 2*S)
        self.assertEqual(outcomes[0].status, "SKIPPED")

    def test_empty_source_does_not_create_file_or_manifest(self):
        with self.assertRaisesRegex(FeatureError, "没有可见记录"):
            self.generate(rows=[])
        self.assertFalse(self.path.exists())
        self.assertFalse(Path(str(self.path)+".manifest.json").exists())

    def test_existing_output_or_manifest_fails_before_query(self):
        for existing in (self.path, Path(str(self.path)+".manifest.json"),
                         Path(str(self.path)+".dataset.json"), self.path.parent / "summary.json"):
            with self.subTest(existing=existing):
                existing.write_text("preserved")
                try:
                    with patch.object(exporter.FeatureProviderFactory, "create_for") as create:
                        with self.assertRaisesRegex(FeatureError, "已存在"):
                            exporter.generate_index_features(self.args, END, END, connection=self.connection)
                        create.assert_not_called()
                    self.assertEqual(existing.read_text(), "preserved")
                finally:
                    existing.unlink()

    def test_source_schema_failure_does_not_create_file(self):
        from tests.test_dolphindb_index_features import row
        with self.assertRaises(FeatureError):
            self.generate(rows=[row(close=-1)])
        self.assertFalse(self.path.exists())

    def test_cli_requires_explicit_connection_and_valid_window_before_credentials(self):
        from contextlib import redirect_stderr
        from io import StringIO
        base = ["--start", "2026-09-01T00:00:00+08:00", "--end", "2026-09-11T23:59:59+08:00",
            "--output-dir", str(self.path.parent / "generated")]
        for extra in ([], ["--connect", "--window", "0"]):
            with redirect_stderr(StringIO()), patch(
                    "bomber.framework.dolphindb.DolphinDbConnectionConfig.from_env") as env:
                with self.assertRaises(SystemExit):
                    exporter.main(base+extra)
                env.assert_not_called()

    def test_cli_selects_range_and_dolphindb_purpose_without_exposing_credentials(self):
        from contextlib import redirect_stdout
        from io import StringIO
        stdout = StringIO()
        with redirect_stdout(stdout), patch("dotenv.load_dotenv"), patch(
                "bomber.framework.dolphindb.DolphinDbConnectionConfig.from_env",
                return_value=self.connection) as env, patch.object(
                exporter, "generate_features", return_value={"status": "passed"}) as generate:
            exporter.main(["--connect", "--start", "2026-09-01T00:00:00+08:00",
                "--end", "2026-09-11T23:59:59+08:00", "--availability", "source-time",
                "--output-dir", str(self.path.parent / "generated")])
        env.assert_called_once_with(purpose="market", read_timeout_seconds=30)
        args = generate.call_args.kwargs
        from tests.test_demo_option_vega import ns
        self.assertEqual(args["start_ns"], ns("2026-09-01 00:00"))
        self.assertEqual(args["end_ns"], ns("2026-09-12 00:00")-S)
        self.assertEqual(args["availability"], "source-time")
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
        outcomes, deadlines = replay.prepare_feature_frames(bars, bundle, binding, records, "000852", 2*S)
        timers = []
        runner = replay.build_backtest_runner(positions=None, feed=None,
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


class BacktestCalendarTests(unittest.TestCase):
    def setUp(self):
        self.entry = import_module("demos.08_option_vega.run_backtest")
        self.args = NS(start_day=date(2026, 8, 13), end_day=date(2026, 9, 30),
            calendar=None, calendar_id="china.cffex", fut_dir=Path("fut"), opt_dir=Path("opt"))

    def test_native_calendar_retains_future_days_for_expiry_without_future_market_data(self):
        from bomber.framework.calendars import CalendarSnapshot
        # 日期仅为测试夹具；实际日期规则由公共Provider提供。
        days = (self.args.start_day, date(2026, 9, 23), date(2026, 9, 24),
            date(2026, 9, 28), date(2026, 9, 29), self.args.end_day,
            date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13),
            date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16))
        snapshot = CalendarSnapshot("china.cffex", "fixture-v1", self.args.start_day,
            date(2026, 12, 31), days, "compiled")
        with patch.object(self.entry, "NativeCalendarProvider") as provider, \
                patch.object(self.entry, "load_calendar") as file_load, \
                patch.object(self.entry, "infer_market_calendar") as infer:
            provider.return_value.snapshot.return_value = snapshot
            calendar, source = self.entry.load_backtest_calendar(self.args)
        provider.assert_called_once_with("china.cffex")
        provider.return_value.snapshot.assert_called_once_with(self.args.start_day)
        file_load.assert_not_called()
        infer.assert_not_called()
        self.assertEqual(calendar, days)
        self.assertIn(snapshot.fingerprint, source)
        self.assertIn("fixture-v1", source)
        self.assertEqual(self.entry.remaining_trading_days(
            calendar, self.args.end_day, date(2026, 10, 16), 5), 6)
        self.assertEqual([day for day in calendar if day <= self.args.end_day], list(days[:6]))

    def test_native_coverage_and_extension_failures_do_not_fall_back_to_inference(self):
        from bomber.framework.calendars import CalendarSnapshot
        snapshot = CalendarSnapshot("china.cffex", "fixture-v1", self.args.start_day,
            date(2026, 12, 31), (self.args.start_day,), "compiled")
        self.args.end_day = date(2027, 1, 4)
        with patch.object(self.entry, "NativeCalendarProvider") as provider, \
                patch.object(self.entry, "infer_market_calendar") as infer:
            provider.return_value.snapshot.return_value = snapshot
            with self.assertRaisesRegex(ValueError, "覆盖不足"):
                self.entry.load_backtest_calendar(self.args)
            provider.return_value.snapshot.side_effect = RuntimeError("日历扩展不可用")
            with self.assertRaisesRegex(RuntimeError, "日历扩展不可用"):
                self.entry.load_backtest_calendar(self.args)
            infer.assert_not_called()

    def test_existing_file_and_inferred_calendar_modes_are_preserved(self):
        self.args.calendar_id = None
        self.args.calendar = Path("calendar.csv")
        days = (self.args.start_day, self.args.end_day)
        with patch.object(self.entry, "load_calendar", return_value=days) as file_load:
            self.assertEqual(self.entry.load_backtest_calendar(self.args), (days, "calendar.csv"))
            file_load.assert_called_once_with(self.args.calendar, self.args.start_day, self.args.end_day)
        self.args.calendar = None
        with patch.object(self.entry, "infer_market_calendar", return_value=days) as infer:
            self.assertEqual(self.entry.load_backtest_calendar(self.args), (days, "MARKET_DATES"))
            infer.assert_called_once_with((self.args.fut_dir, self.args.opt_dir),
                self.args.start_day, self.args.end_day)

    def test_cli_selects_native_calendar_and_rejects_conflicting_file(self):
        from contextlib import redirect_stderr
        import io
        cli = ["run_backtest.py", "--start-day", "2026-08-13", "--end-day", "2026-09-30",
            "--index-feature-file", "index.csv", "--calendar-id", "china.cffex"]
        with patch.object(self.entry, "run_case") as run, patch("sys.argv", cli):
            self.entry.main()
        self.assertEqual(run.call_args.args[0].calendar_id, "china.cffex")
        self.assertIsNone(run.call_args.args[0].calendar)
        with patch.object(self.entry, "run_case") as run, \
                patch("sys.argv", [*cli, "--calendar", "calendar.csv"]), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            self.entry.main()
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()


class MultiPeriodBacktestTests(unittest.TestCase):
    def setUp(self):
        self.entry = import_module("demos.08_option_vega.run_backtest")

    def test_option_ohlc_repair_is_explicit_audited_and_keeps_raw_file(self):
        import pandas as pd
        from bomber.framework.dataprep.bars import read_bar_frame
        from bomber.framework.dataprep.contracts import BarFileKey, InputError
        args = NS(option_bar_minutes=5, option_slot="14:00", bar_timestamp="end")
        strict = self.entry.backtest_bar_specs(args)
        args.option_ohlc_policy = "expand_range"
        repair = self.entry.backtest_bar_specs(args)
        self.assertEqual(strict["option"].ohlc_policy, "reject")
        self.assertEqual(repair["future"].ohlc_policy, "reject")
        key = BarFileKey("option", "MO2606-C-6000", date(2026, 6, 22))
        raw = pd.DataFrame(dict(datetime=pd.to_datetime(["2026-06-22 09:35"]),
            open=[2761.6], high=[2752.0], low=[2752.0], close=[2752.0], volume=[0]))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "MO2606-C-6000_20260622.feather"
            raw.to_feather(path)
            original = path.read_bytes()
            with self.assertRaisesRegex(InputError, "INVALID_OHLC"):
                read_bar_frame(path, strict["option"], key)
            result = read_bar_frame(path, repair["option"], key)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(result.frame), 1)
            row = result.frame.iloc[0]
            self.assertEqual((row.open, row.high, row.low, row.close, row.volume),
                (2761.6, 2761.6, 2752.0, 2752.0, 0))
            self.assertEqual((row.source_high, row.source_low), (2752.0, 2752.0))
            self.assertEqual([issue.code for issue in result.issues], ["OHLC_RANGE_EXPANDED"])
            raw.loc[0, "close"] = 0
            raw.to_feather(path)
            with self.assertRaisesRegex(InputError, "INVALID_BAR_FIELDS"):
                read_bar_frame(path, repair["option"], key)

    def test_ohlc_policy_loads_from_yaml_and_cli_can_override(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "launch.yaml"
            path.write_text(json.dumps({"experiment_setups": {"research": {
                "start_day": "2026-06-01", "end_day": "2026-06-22",
                "index_feature_file": "index.csv", "option_ohlc_policy": "expand_range"}}}))
            cli = ["run_backtest.py", "--config-file", str(path), "--config-id", "research"]
            for override, expected in (([], "expand_range"),
                    (["--option-ohlc-policy", "reject"], "reject")):
                with patch.object(self.entry, "run_case") as run, patch("sys.argv", cli + override):
                    self.entry.main()
                specs = self.entry.backtest_bar_specs(run.call_args.args[0])
                self.assertEqual(specs["option"].ohlc_policy, expected)
                self.assertEqual(specs["future"].ohlc_policy, "reject")

    def test_real_m5_input_keeps_futures_m1_and_shifts_only_start_labeled_options(self):
        import pandas as pd
        from bomber.framework.dataprep.bars import read_bar_frame
        from bomber.framework.dataprep.contracts import BarFileKey, InputError
        args = NS(option_bar_minutes=5, option_slot="14:00", bar_timestamp="end",
            options_bar_timestamp="start")
        specs = self.entry.backtest_bar_specs(args)
        self.assertEqual(specs["option"].bar_spec, "5-MINUTE")
        self.assertEqual(specs["future"].bar_spec, "1-MINUTE")
        with TemporaryDirectory() as directory:
            for kind, symbol, minutes in (("option", "MO2609-C-6600", ("13:55", "14:00")),
                                          ("future", "IM2609", ("14:00", "14:01"))):
                path = Path(directory) / f"{symbol}_20260813.feather"
                frame = pd.DataFrame(dict(datetime=pd.to_datetime([f"2026-08-13 {m}" for m in minutes]),
                    open=100, high=100, low=100, close=100, volume=0))
                frame.to_feather(path)
                key = BarFileKey(kind, symbol, date(2026, 8, 13))
                prepared = read_bar_frame(path, specs[kind], key).frame
                self.assertEqual(len(prepared), 2)  # 不扩展或填充M5。
                expected = ("14:00", "14:05") if kind == "option" else ("14:00", "14:01")
                self.assertEqual(tuple(prepared.datetime.dt.strftime("%H:%M")), expected)
                if kind == "option":
                    frame.loc[0, "datetime"] = pd.Timestamp("2026-08-13 13:58")
                    frame.to_feather(path)
                    with self.assertRaisesRegex(InputError, "INVALID_TIMESTAMP"):
                        read_bar_frame(path, specs[kind], key)

    def test_cli_requires_explicit_aligned_slot_and_preserves_m1_defaults(self):
        from contextlib import redirect_stderr
        import io
        cli = ["run_backtest.py", "--start-day", "2026-08-13", "--end-day", "2026-09-30",
            "--index-feature-file", "index.csv"]
        with patch.object(self.entry, "run_case") as run, patch("sys.argv", cli):
            self.entry.main()
        args = run.call_args.args[0]
        self.assertEqual((args.option_bar_minutes, args.option_slot, args.execution_timing),
            (1, "13:58", "native"))
        for extra in (("--option-bar-minutes", "5"), ("--option-bar-minutes", "2")):
            with patch.object(self.entry, "run_case") as run, patch("sys.argv", [*cli, *extra]), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.entry.main()
            run.assert_not_called()
        with patch.object(self.entry, "run_case") as run, patch("sys.argv", [*cli,
                "--option-bar-minutes", "5", "--option-slot", "14:00",
                "--options-bar-timestamp", "start", "--execution-timing", "next_market"]):
            self.entry.main()
        args = run.call_args.args[0]
        self.assertEqual((args.option_bar_minutes, args.option_slot, args.execution_timing),
            (5, "14:00", "next_market"))
        self.assertEqual(args.options_bar_timestamp, "start")

    def test_m5_entry_then_m1_hedge_preserves_option_timestamp_and_never_reads_future_m5(self):
        from tests.test_demo_option_vega import StrategyTests, ns
        from bomber.framework.market.frames import FrameObservation, MinuteFrame
        from datetime import datetime
        from zoneinfo import ZoneInfo
        fixture = StrategyTests()
        fixture.setUp()
        strategy, signal = fixture.strategy, import_module("demos.08_option_vega.vega_signal")
        strategy.config = replace(strategy.config, option_slot="14:00", hedge_slots=("14:01",))
        stamp = ns("2026-08-13 14:00")
        now = datetime.fromtimestamp(stamp // S, ZoneInfo("Asia/Shanghai"))
        premium = signal.call_price(6500, 6600,
            signal.source_years(now, strategy._remaining_days(now.date(), fixture.info.last_day)), .02, .25)
        def publish(stamp, *, option=False):
            values = {"IM2609": 6500, "000852": 6400}
            if option:
                values[fixture.info.symbol] = premium
            strategy.on_market_frame(MinuteFrame(stamp, stamp + S, {
                key: FrameObservation(Decimal(str(price)), "fixture", stamp, stamp)
                for key, price in values.items()}))
        publish(stamp - M)  # 没有提前看到14:00期权。
        self.assertNotIn(fixture.info.symbol, strategy.latest)
        self.assertEqual(strategy.entry_decisions, 0)
        publish(stamp, option=True)
        self.assertEqual(strategy.entry_decisions, 1)
        self.assertLess(strategy.option_goals[fixture.info.symbol], 0)
        fixture.ctx.actual[fixture.ids[fixture.info.symbol]] = Decimal(-2)
        publish(stamp + M)  # 只有M1期货/指数也能触发14:01对冲。
        self.assertEqual(strategy.last_decision["hedge"], "EVALUATED")
        self.assertEqual(strategy.latest[fixture.info.symbol][0], stamp)
        self.assertEqual(strategy.latest["IM2609"][0], stamp + M)

    def test_feature_frames_keep_m1_clock_without_filling_m5_or_using_future_bar(self):
        from tests.test_demo_option_vega import ns
        stamp = ns("2026-08-13 14:00")
        bundle, binding = features.index_bundle(DEFAULT_FEATURE_CONFIG, "index.minute", 1,
            file_path=Path("fixture-index.csv"))
        payload = binding.spec.payload_type(open=Decimal(6400), high=Decimal(6400),
            low=Decimal(6400), close=Decimal(6400), volume=Decimal(0), turnover=Decimal(0),
            prev_close=None, pct_change=None)
        records = [ExternalDataRecord("index.minute", 1, "000852", payload,
            stamp + i*M, stamp + i*M, stamp + i*M, 0, "fixture") for i in range(7)]
        def bar(key, timestamp):
            return NS(ts_event=timestamp, ts_init=timestamp,
                bar_type=NS(instrument_id=NS(symbol=key)),
                close=NS(as_decimal=lambda: Decimal(100)))
        events = [bar("IM2609", stamp + i*M) for i in range(7)]
        events += [bar("MO2609-C-6600", stamp + i*M) for i in (0, 5)]
        outcomes, _ = replay.prepare_feature_frames(events, bundle, binding, records, "000852", 2*S)
        self.assertEqual([row.event_ns for row in outcomes], [stamp + i*M for i in range(6)])
        self.assertTrue(all(row.status == "READY" for row in outcomes))
        for i, row in enumerate(outcomes):
            self.assertEqual("MO2609-C-6600" in row.frame.market, i in (0, 5))


class BacktestYamlConfigTests(unittest.TestCase):
    def setUp(self):
        self.entry = import_module("demos.08_option_vega.run_backtest")
        self.config = Path(__file__).resolve().parents[1] / "demos/08_option_vega/config/strategy.yaml"

    def test_example_yaml_starts_same_m5_case_without_long_cli_or_source_connections(self):
        with patch.object(self.entry, "run_case") as run, patch("sys.argv", ["run_backtest.py",
                "--config-file", str(self.config), "--config-id", "m5_index_blend"]):
            self.entry.main()
        args = run.call_args.args[0]
        self.assertEqual(args.start_day, date(2026, 8, 13))
        self.assertEqual(args.end_day, date(2026, 9, 30))
        self.assertEqual(args.starting_balance, Decimal("40000000"))
        self.assertEqual(args.index_code, "000852")
        self.assertEqual(args.index_value_field, "index_blend")
        self.assertEqual(args.index_feature_file, Path("demos/feature/index/index-blend.csv"))
        self.assertEqual(args.opt_dir, Path("temp/data/to/opt"))
        self.assertEqual((args.option_bar_minutes, args.option_slot, args.execution_timing),
            (5, "14:00", "next_market"))
        self.assertTrue(args.no_tearsheet)
        self.assertTrue(args.require_fills)

    def test_cli_overrides_yaml_field_and_domain_validation_still_runs(self):
        from contextlib import redirect_stderr
        import io
        cli = ["run_backtest.py", "--config-file", str(self.config), "--config-id", "m5_index_blend"]
        with patch.object(self.entry, "run_case") as run, patch("sys.argv", [*cli,
                "--index-value-field", "ma20", "--opt-dir", "other-options"]):
            self.entry.main()
        self.assertEqual(run.call_args.args[0].index_value_field, "ma20")
        self.assertEqual(run.call_args.args[0].opt_dir, Path("other-options"))
        with patch.object(self.entry, "run_case") as run, patch("sys.argv", [*cli,
                "--option-slot", "13:58"]), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as error:
            self.entry.main()
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_startup_yaml_keeps_strategy_fields_in_json_dependencies(self):
        from dataclasses import fields
        from bomber.framework.cli import load_config
        names = {field.name for field in fields(self.entry.OptionVegaConfig)} | {"max_notional", "limit_offset_ticks"}
        for config_id in ("m5_index_blend", "live_recording", "live_model", "live_simnow"):
            selected = load_config(self.config, config_id)
            self.assertFalse(names & selected.keys())
            parameters = json.loads(Path(selected["strategy_config"]).read_text())
            self.assertIn("option_slot", parameters)
            self.assertEqual(parameters["option_slot"], "14:00")
            self.assertIsInstance(parameters["delta_targets"], list)
            self.assertIsInstance(parameters["hedge_slots"], list)


class BacktestOptionPathTests(unittest.TestCase):
    def setUp(self):
        self.entry = import_module("demos.08_option_vega.run_backtest")
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name in ("kline/opt", "kline/fut", "role", "m5"):
            (self.root / name).mkdir(parents=True)
        # 这里只验证公共路径选择及文件存在性，不读取条款内容。
        for name in ("opt_basic.feather", "fut_basic.feather"):
            (self.root / "role" / name).touch()

    def test_cli_switch_to_m1_ignores_yaml_m5_directory_and_uses_default_root(self):
        config = Path(__file__).resolve().parents[1] / "demos/08_option_vega/config/strategy.yaml"
        with patch.object(self.entry, "run_case") as run, patch("sys.argv", ["run_backtest.py",
                "--config-file", str(config), "--config-id", "m5_index_blend",
                "--option-bar-minutes", "1", "--data-root", str(self.root)]):
            self.entry.main()
        args = self.entry.resolve_backtest_paths(run.call_args.args[0])
        self.assertEqual(args.opt_dir, self.root / "kline/opt")
        self.assertEqual(args.fut_dir, self.root / "kline/fut")
        self.assertEqual(args.option_slot, "14:00")

    def test_m5_requires_custom_directory_and_never_falls_back_to_m1(self):
        args = NS(data_root=self.root, option_bar_minutes=5, opt_dir=None)
        with self.assertRaisesRegex(ValueError, "M5须.*指定真实M5目录"):
            self.entry.resolve_backtest_paths(args)
        args.opt_dir = self.root / "m5"
        resolved = self.entry.resolve_backtest_paths(args)
        self.assertEqual(resolved.opt_dir, self.root / "m5")
        self.assertEqual(resolved.fut_dir, self.root / "kline/fut")

    def test_m1_requires_root_even_if_old_custom_option_path_is_supplied(self):
        with self.assertRaisesRegex(ValueError, "M1使用默认路径.*data-root"):
            self.entry.resolve_backtest_paths(NS(data_root=None, option_bar_minutes=1,
                opt_dir=self.root / "m5"))


class BacktestDayCoverageTests(unittest.TestCase):
    def setUp(self):
        self.entry = import_module("demos.08_option_vega.run_backtest")
        self.day = date(2026, 9, 10)
        self.key = "MO2610-C-6600"
        self.contracts = {self.key: NS(list_day=date(2026, 6, 1), last_day=date(2026, 10, 16))}
        self.args = NS(option_product="MO", index_code="000852", opt_dir=Path("m5-options"),
            opt_basic=Path("role/opt_basic.feather"), index_feature_file=Path("index-blend.csv"),
            report_dir=Path("reports"))

    def test_missing_option_chain_is_distinct_from_present_index(self):
        from bomber.framework.dataprep.session import InputSession
        with InputSession() as session, self.assertRaises(FileNotFoundError) as error:
            self.entry.check_day_coverage(self.args, (self.day,), {}, self.contracts, {self.day: {1}})
        self.assertEqual([issue.code for issue in session.issues], ["MISSING_OPTION_CHAIN"])
        self.assertIn('"index_minutes": 1', str(error.exception))
        self.assertIn("m5-options", str(error.exception))
        self.assertIn("opt_basic.feather", str(error.exception))

    def test_missing_index_is_distinct_from_present_listed_option(self):
        from bomber.framework.dataprep.session import InputSession
        paths = {(self.day, self.key): Path("option.feather")}
        with InputSession() as session, self.assertRaises(FileNotFoundError) as error:
            self.entry.check_day_coverage(self.args, (self.day,), paths, self.contracts, {})
        self.assertEqual([issue.code for issue in session.issues], ["MISSING_INDEX_FEATURE"])
        self.assertEqual(session.issues[0].source, "index-blend.csv")
        self.assertIn('"listed_option_candidates": 1', str(error.exception))

    def test_all_requested_missing_days_are_recorded_without_truncating_calendar(self):
        from datetime import timedelta
        from bomber.framework.dataprep.session import InputSession
        days = tuple(self.day + timedelta(days=i) for i in range(12))
        with InputSession() as session, self.assertRaises(FileNotFoundError) as error:
            self.entry.check_day_coverage(self.args, days, {}, self.contracts, {})
        self.assertEqual(len(session.issues), 24)
        self.assertEqual({issue.trading_day for issue in session.issues}, set(days))
        self.assertIn("缺失交易日=12", str(error.exception))
        self.assertIn("input_issues.json", str(error.exception))

    def test_existing_but_unlisted_contract_is_not_reported_as_a_missing_file(self):
        from bomber.framework.dataprep.session import InputSession
        self.contracts[self.key].list_day = date(2026, 9, 11)
        with InputSession() as session, self.assertRaises(FileNotFoundError) as error:
            self.entry.check_day_coverage(self.args, (self.day,),
                {(self.day, self.key): Path("option.feather")}, self.contracts, {self.day: {1}})
        self.assertIn("认购文件=1", session.issues[0].message)
        self.assertIn('"listed_option_candidates": 0', str(error.exception))

    def test_complete_inputs_keep_same_candidates_and_do_not_fill_missing_minutes(self):
        from bomber.framework.dataprep.session import InputSession
        paths, minutes = {(self.day, self.key): Path("option.feather")}, {self.day: {1}}
        with InputSession() as session:
            selected = self.entry.check_day_coverage(self.args, (self.day,), paths, self.contracts, minutes)
        self.assertEqual(selected, {self.day: (self.key,)})
        self.assertEqual(minutes, {self.day: {1}})
        self.assertEqual(session.issues, [])
