"""08特征文件、Live声明与模拟可见时间的离线回归，无数据库/柜台连接。"""
from dataclasses import replace
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

from bomber.framework.datahub.features import ExternalDataRecord, FeatureError
from bomber.framework.dataprep.features import DEFAULT_FEATURE_CONFIG, export_features, load_feature_bundle
from bomber.framework.dataprep.sources.dolphindb import DolphinDbReferenceConfig

features = import_module("demos.08_option_vega.features")
replay = import_module("demos.08_option_vega.backtest_runtime")
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
        runner = replay.OptionFeatureBacktestRunner(positions=None, feed=None,
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
