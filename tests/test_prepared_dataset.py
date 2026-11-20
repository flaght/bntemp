"""Real Feather snapshot round trips, policies, incremental publication and replay order."""
from dataclasses import replace
from datetime import date
from importlib import import_module
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from bomber.framework.dataprep.bars import read_bar_frame
from bomber.framework.dataprep.catalog import scan_bar_files
from bomber.framework.dataprep.contracts import BarFileKey, BarReadSpec, InputError, InputPlan
from bomber.framework.dataprep.prepared import PreparedDataset, PreparedSource, build_prepared_dataset
from bomber.framework.dataprep.session import InputSession


class PreparedDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "prepared/v1"
        self.days = tuple(date(year, 8, 13) for year in (2024, 2025, 2026))
        self.specs = {"future": BarReadSpec(trading_day_policy="day_session"),
                      "option": BarReadSpec(interval_seconds=300, trading_day_policy="day_session",
                                             ohlc_policy="expand_range")}
        self.products = {"future": "IM", "option": "MO"}
        self.paths = {}
        for day in self.days:
            for kind, codes in (("future", ("IM2609", "IM2612")),
                                ("option", ("MO2609-C-6600", "MO2609-C-6700"))):
                for code in codes:
                    directory = self.root / "kline" / ("fut" if kind == "future" else "opt") / f"{day:%Y%m%d}"
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / f"{code}_{day:%Y%m%d}.feather"
                    frame = pd.DataFrame(dict(datetime=pd.date_range(f"{day} 14:00", periods=3,
                        freq="min" if kind == "future" else "5min"),
                        open=[100., 101., 102.], high=[104., 104., 104.], low=[99., 99., 99.],
                        close=[101., 102., 103.], volume=pd.Series([2**54+1, 2**54+3, 0], dtype="uint64")))
                    if code.endswith("6700"):
                        frame.loc[0, "high"] = 90.
                    frame.to_feather(path)
                    self.paths[BarFileKey(kind, code, day)] = path
        self.sources = [PreparedSource(kind, self.root/"kline"/folder, self.products[kind], self.specs[kind])
                        for kind, folder in (("future", "fut"), ("option", "opt"))]

    def build(self, output=None, **kwargs):
        return build_prepared_dataset(self.sources, output or self.output,
            start_day=self.days[0], end_day=self.days[-1], **kwargs)

    def plan(self, keys=None, **kwargs):
        return InputPlan(tuple(self.paths if keys is None else keys), requested=(self.days[0], self.days[-1]), **kwargs)

    def load(self, dataset, plan=None, **kwargs):
        return dataset.load(plan or self.plan(), specs=self.specs, products=self.products, **kwargs)

    def test_all_years_exact_frames_large_integer_volume_and_repair_diagnostics(self):
        manifest = self.build()
        self.assertEqual((manifest["source_files"], manifest["rows"], manifest["partitions"]), (12, 36, 6))
        dataset = PreparedDataset(self.output, verify="full")
        bundle = self.load(dataset)
        self.assertEqual(tuple(s.result.key for s in bundle.sources), tuple(sorted(self.paths)))
        for source in bundle.sources:
            result = source.result
            expected = read_bar_frame(self.paths[result.key], self.specs[result.key.asset_kind], result.key)
            pd.testing.assert_frame_equal(result.frame, expected.frame)
            self.assertEqual(result.issues, expected.issues)
            self.assertEqual(int(result.frame.volume.iloc[0]), 2**54+1)
            self.assertTrue(result.path.is_file())
        dataset.verify_all()

    def test_reads_without_raw_files_or_normalizer_and_opens_each_partition_once(self):
        self.build()
        shutil.rmtree(self.root / "kline")
        dataset = PreparedDataset(self.output)
        with InputSession() as session, patch("bomber.framework.dataprep.prepared.read_bar_frame",
                side_effect=AssertionError("normalized again")):
            session.coverage.requested = (self.days[0], self.days[-1])
            bundle = self.load(dataset, copy_frames=False)
            self.assertEqual(session.performance.counters["prepared_sources_loaded"], 12)
            self.assertEqual(session.performance.counters["prepared_rows_loaded"], 36)
            self.assertLess(session.performance.counters["prepared_partition_files_read"], 12)
            self.assertEqual(len(session.issues), 3)
            self.assertEqual(session.coverage.actual_days, self.days)
            self.assertEqual(len(bundle.sources), 12)

    def test_date_contract_filter_missing_required_and_optional_sources(self):
        self.build()
        dataset = PreparedDataset(self.output)
        inventory = dataset.inventory("future", "IM", self.specs["future"], self.days[1], self.days[1],
                                      lambda symbol: symbol == "IM2609")
        self.assertEqual(tuple(inventory), ((self.days[1], "IM2609"),))
        key = BarFileKey("future", "IM2609", self.days[1])
        missing = BarFileKey("future", "IM2703", self.days[1])
        bundle = self.load(dataset, self.plan((key,), optional=(missing,)))
        self.assertEqual(bundle.coverage.optional_missing, (missing,))
        self.assertEqual(len(bundle.sources), 1)
        with self.assertRaisesRegex(InputError, "MISSING_REQUIRED_BAR"):
            self.load(dataset, self.plan((missing,)))

    def test_every_policy_mismatch_rejected_before_partition_read(self):
        self.build()
        dataset = PreparedDataset(self.output)
        for changes in (dict(timestamp_label="start"), dict(interval_seconds=60),
                        dict(ohlc_policy="reject"), dict(timezone="UTC"),
                        dict(available_column="published"), dict(zero_price_policy="drop"),
                        dict(field_mapping=(("close", "last"),))):
            with self.subTest(changes=changes), self.assertRaisesRegex(InputError, "PREPARED_POLICY_MISMATCH"):
                dataset.validate_spec("option", "MO", replace(self.specs["option"], **changes))

    def test_start_label_and_available_time_round_trip_without_second_shift(self):
        source = self.sources[0]
        path = next(path for key, path in self.paths.items() if key.asset_kind == "future")
        frame = pd.read_feather(path)
        frame["published"] = frame.datetime + pd.Timedelta(seconds=75)
        frame.to_feather(path)
        # All files need the explicitly declared availability column.
        for key, file in self.paths.items():
            if key.asset_kind == "future" and file != path:
                frame = pd.read_feather(file)
                frame["published"] = frame.datetime + pd.Timedelta(seconds=75)
                frame.to_feather(file)
        spec = replace(source.spec, timestamp_label="start", available_column="published")
        build_prepared_dataset((replace(source, spec=spec),), self.output,
            start_day=self.days[0], end_day=self.days[-1])
        keys = tuple(key for key in self.paths if key.asset_kind == "future")
        bundle = PreparedDataset(self.output).load(self.plan(keys), specs={"future": spec},
                                                products={"future": "IM"})
        for item in bundle.sources:
            expected = read_bar_frame(self.paths[item.result.key], spec, item.result.key)
            pd.testing.assert_frame_equal(item.result.frame, expected.frame)

    def test_schema_groups_do_not_promote_integer_prices_to_float(self):
        key = next(key for key in self.paths if key.asset_kind == "future")
        frame = pd.read_feather(self.paths[key])
        for name, value in (("open", 2**63+1), ("high", 2**63+4), ("low", 2**63), ("close", 2**63+3)):
            frame[name] = pd.Series([value]*len(frame), dtype="uint64")
        frame.to_feather(self.paths[key])
        self.build()
        result = self.load(PreparedDataset(self.output), self.plan((key,))).sources[0].result
        expected = read_bar_frame(self.paths[key], self.specs["future"], key)
        pd.testing.assert_frame_equal(result.frame, expected.frame)

    def test_incremental_reuses_unchanged_days_and_rebuilds_changed_day(self):
        self.build()
        output2 = self.root / "prepared/v2"
        manifest = self.build(output2, base_snapshot=self.output)
        self.assertEqual(manifest["reused_partitions"], 6)
        key = next(key for key in self.paths if key.asset_kind == "future")
        frame = pd.read_feather(self.paths[key])
        frame.loc[0, "close"] = 102.
        frame.to_feather(self.paths[key])
        output3 = self.root / "prepared/v3"
        manifest = self.build(output3, base_snapshot=output2)
        self.assertEqual(manifest["reused_partitions"], 5)
        for source in self.load(PreparedDataset(output3, verify="full")).sources:
            pd.testing.assert_frame_equal(source.result.frame,
                read_bar_frame(self.paths[source.result.key], self.specs[source.result.key.asset_kind], source.result.key).frame)
        PreparedDataset(output2).verify_all()

    def test_no_overwrite_and_failed_build_does_not_publish_or_leave_lock(self):
        self.build()
        before = (self.output / "manifest.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.build()
        path = next(iter(self.paths.values()))
        frame = pd.read_feather(path)
        frame.loc[0, "close"] = -1.
        frame.to_feather(path)
        bad = self.root / "prepared/bad"
        with self.assertRaisesRegex(InputError, "INVALID_BAR_FIELDS"):
            self.build(bad)
        self.assertFalse(bad.exists())
        self.assertFalse((bad.parent / "bad.prepare.lock").exists())
        self.assertEqual((self.output / "manifest.json").read_bytes(), before)

    def test_corrupt_index_partition_and_normalizer_version_rejected(self):
        self.build()
        index_path = self.output / "index.feather"
        original = index_path.read_bytes()
        index_path.write_bytes(original[:-1])
        with self.assertRaisesRegex(InputError, "PREPARED_INTEGRITY"):
            PreparedDataset(self.output)
        index_path.write_bytes(original)
        dataset = PreparedDataset(self.output)
        name = dataset.rows[0]["data_file"]
        path = self.output / name
        content = path.read_bytes()
        path.write_bytes(content[:-1])
        with self.assertRaisesRegex(InputError, "PREPARED_INTEGRITY"):
            self.load(dataset)
        path.write_bytes(content)
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["normalizer"] = "different-version"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(InputError, "PREPARED_VERSION_MISMATCH"):
            PreparedDataset(self.output)

    def test_selected_diagnostics_only_and_detached_frame_results(self):
        self.build()
        dataset = PreparedDataset(self.output)
        key = BarFileKey("option", "MO2609-C-6700", self.days[0])
        with InputSession() as session:
            a = self.load(dataset, self.plan((key,))).sources[0].result
            self.assertEqual(len(session.issues), 1)
            self.assertEqual(session.issues[0].source, str(self.paths[key].resolve()))
            self.assertEqual(a.issues[0].code, "OHLC_RANGE_EXPANDED")
        a.frame.loc[0, "close"] = 999.
        b = self.load(dataset, self.plan((key,))).sources[0].result
        self.assertEqual(b.frame.close.iloc[0], 101.)

    def test_empty_sources_after_explicit_zero_drop_keep_no_fabricated_bars(self):
        source = self.sources[0]
        for key, path in self.paths.items():
            if key.asset_kind == "future":
                frame = pd.read_feather(path)
                frame[["open", "high", "low", "close"]] = 0.
                frame.to_feather(path)
        spec = replace(source.spec, zero_price_policy="drop")
        build_prepared_dataset((replace(source, spec=spec),), self.output,
            start_day=self.days[0], end_day=self.days[-1])
        keys = tuple(key for key in self.paths if key.asset_kind == "future")
        bundle = PreparedDataset(self.output).load(self.plan(keys), specs={"future": spec},
                                                 products={"future": "IM"})
        self.assertTrue(all(s.result.frame.empty for s in bundle.sources))
        self.assertTrue(all(s.result.issues[0].code == "ZERO_PRICE_BAR_DROPPED" for s in bundle.sources))

    def test_full_hash_detects_same_size_content_changes(self):
        self.build()
        dataset = PreparedDataset(self.output, verify="full")
        path = self.output / dataset.rows[0]["data_file"]
        content = bytearray(path.read_bytes())
        content[len(content)//2] ^= 1
        path.write_bytes(content)
        with self.assertRaisesRegex(InputError, "PREPARED_INTEGRITY"):
            self.load(dataset)

    def test_prepared_paths_require_only_terms_not_original_market_directories(self):
        from types import SimpleNamespace as NS
        entry = import_module("demos.08_option_vega.run_backtest")
        role = self.root / "role"
        role.mkdir()
        for file in ("opt_basic.feather", "fut_basic.feather"):
            (role/file).touch()
        self.build()
        shutil.rmtree(self.root / "kline")
        args = NS(data_root=self.root, data_mode="prepared", prepared_root=self.output,
                  option_bar_minutes=5, calendar=None)
        result = entry.resolve_backtest_paths(args)
        self.assertEqual(result.opt_basic, (role/"opt_basic.feather").resolve())
        self.assertEqual(result.prepared_root, self.output.resolve())
        with self.assertRaisesRegex(ValueError, "prepared-root"):
            entry.resolve_backtest_paths(NS(data_mode="prepared", prepared_root=None))

    def test_cli_registers_modes_and_preserves_strategy_configuration(self):
        entry = import_module("demos.08_option_vega.run_backtest")
        with patch.object(entry, "run_case") as run, patch("sys.argv", ["run_backtest.py",
                "--start-day", "2024-01-01", "--end-day", "2026-10-21",
                "--index-feature-file", "index.csv", "--data-mode", "prepared",
                "--prepared-root", str(self.output), "--option-bar-minutes", "5",
                "--option-slot", "14:00", "--prepared-verify", "full"]):
            entry.main()
        args = run.call_args.args[0]
        self.assertEqual((args.data_mode, args.prepared_root, args.prepared_verify),
                         ("prepared", self.output, "full"))
        self.assertEqual((args.option_slot, args.execution_timing), ("14:00", "native"))

    def test_two_periods_of_same_contract_keep_separate_diagnostics(self):
        option_source = self.sources[1]
        minute = replace(option_source, spec=replace(option_source.spec, interval_seconds=60))
        build_prepared_dataset((option_source, minute), self.output,
            start_day=self.days[0], end_day=self.days[-1])
        dataset = PreparedDataset(self.output)
        keys = tuple(key for key in self.paths if key.asset_kind == "option")
        for spec in (option_source.spec, minute.spec):
            bundle = dataset.load(self.plan(keys), specs={"option": spec}, products={"option": "MO"})
            self.assertEqual(sum(len(s.result.issues) for s in bundle.sources), 3)
            for source in bundle.sources:
                expected = read_bar_frame(self.paths[source.result.key], spec, source.result.key)
                self.assertEqual(source.result.issues, expected.issues)

    def test_cli_without_native_engine_and_full_verification(self):
        command = [sys.executable, "scripts/prepare_market_data.py", "--data-root", str(self.root),
            "--start-day", str(self.days[0]), "--end-day", str(self.days[-1]), "--output", str(self.output),
            "--option-ohlc-policy", "expand_range"]
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(command, cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([sys.executable, "scripts/prepare_market_data.py", "--verify", str(self.output)],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"status": "passed"', result.stdout)

    def test_preparation_cli_uses_backtest_yaml_and_explicit_date_override(self):
        config = self.root/"strategy.yaml"
        config.write_text("experiment_setups:\n  fixture:\n"
            f"    data-root: {self.root}\n    start_day: '2025-08-13'\n    end_day: '2026-08-13'\n"
            "    option_bar_minutes: 5\n    option-ohlc-policy: expand_range\n"
            "    option_product: MO\n    future_product: IM\n    bar_timestamp: end\n")
        command = [sys.executable, "scripts/prepare_market_data.py", "--config-file", str(config),
            "--config-id", "fixture", "--start-day", "2024-08-13", "--output", str(self.output)]
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(PreparedDataset(self.output).manifest["source_files"], 12)

    def test_standalone_comparison_detects_source_updates_and_uses_exact_fields(self):
        self.build()
        from scripts.check_prepared_market_data import compare
        dataset = PreparedDataset(self.output, verify="full")
        self.assertEqual(compare(dataset), dict(source_files_compared=12, rows_compared=36, equal=True))
        path = next(iter(self.paths.values()))
        frame = pd.read_feather(path)
        frame.loc[0, "close"] = 102.
        frame.to_feather(path)
        with self.assertRaisesRegex(ValueError, "Raw source changed"):
            compare(dataset)

    def test_public_option_chain_entry_preserves_context(self):
        from bomber.framework.dataprep.contracts import InputContext
        from bomber.framework.dataprep.scenarios.option_chain import prepare_option_chain
        self.build()
        context = InputContext(references={"fixture": "same-context"})
        dataset = PreparedDataset(self.output)
        with patch("bomber.framework.dataprep.scenarios.option_chain.scan_bar_files",
                   side_effect=AssertionError("scanned originals")):
            result = prepare_option_chain({}, self.plan(), specs=self.specs, context=context,
                prepared_dataset=dataset, products=self.products, copy_frames=False)
        self.assertIs(result.context, context)
        self.assertEqual(len(result.sources), 12)

    def test_same_timestamp_native_event_sequence_matches_raw_path(self):
        from types import SimpleNamespace as NS
        from tests.test_prepared_bar_parser import PreparedBarParserTests, EntityBarType
        from tests.test_bar_parsers import Identity
        from bomber.framework.dataprep.bars import add_prepared_bar_source
        PreparedBarParserTests.setUpClass()
        fixture = PreparedBarParserTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fw = fixture.fw
        self.build()
        raw = [read_bar_frame(path, self.specs[key.asset_kind], key) for key, path in sorted(self.paths.items())]
        prepared = [s.result for s in self.load(PreparedDataset(self.output)).sources]
        signatures = []
        with patch.dict("sys.modules", {"bomber.framework.market.replay.parsers.bar":
                NS(FixedInstrumentBarParser=fw.fixed.FixedInstrumentBarParser),
                "bomber.framework.market.replay.parsers.bar.fixed": fw.fixed}):
            for results in (raw, prepared):
                feed = fw.feed.FileReplayFeed()
                # Same source-registration order as the 08 runner: date then symbol.
                for result in sorted(results, key=lambda r: (r.key.trading_day, r.key.symbol)):
                    identity = Identity(result.key.symbol + ".CFFEX")
                    feed.register_instrument(fw.basic.InstrumentMeta(identity, price_precision=2, size_precision=0))
                    feed.subscribe(identity, fw.basic.DataType.BAR, bar_spec=result.spec.bar_spec)
                    add_prepared_bar_source(feed, result, identity, copy_frame=False, fast_parse=True)
                feed.connect()
                events = feed.load_events()
                signatures.append([(str(e.bar_type), e.ts_event, e.ts_init,
                    *(str(getattr(e, n)) for n in ("open", "high", "low", "close", "volume"))) for e in events])
        self.assertEqual(signatures[0], signatures[1])
        self.assertEqual(len(signatures[1]), 36)


if __name__ == "__main__":
    unittest.main()
