"""Deployment checker regressions; no installed SDK, backtest, or network required."""
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "scripts/check_optimized_environment.py"
spec = spec_from_file_location("optimized_environment_check", path)
checker = module_from_spec(spec)
spec.loader.exec_module(checker)


class OptimizedEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def source(self, text, expected=None):
        source = self.root / "module.py"
        source.write_text(text, encoding="utf-8")
        digest = expected or hashlib.sha256(source.read_bytes()).hexdigest()
        with patch.object(checker, "find_spec", return_value=NS(origin=str(source))):
            return checker.check_source("example", "example.module", digest, ["optimized_api"])

    def test_exact_reference_reports_active_origin_and_hash(self):
        result = self.source("def optimized_api(): pass\n")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["path"], str((self.root / "module.py").resolve()))
        self.assertEqual(result["sha256"], result["expected_sha256"])

    def test_old_source_fails_even_when_file_can_be_imported(self):
        result = self.source("def old_api(): pass\n")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("optimized_api", result["detail"])

    def test_different_source_with_markers_is_review_not_verified_or_old(self):
        result = self.source("def optimized_api(): return 2\n", expected="0" * 64)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(checker.overall_status([result]), "needs_review")

    def test_broken_parent_import_does_not_abort_the_audit(self):
        with patch.object(checker, "find_spec", side_effect=ImportError("missing dependency")):
            result = checker.check_source("example", "example.module", "unused", [])
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("missing dependency", result["detail"])

    def test_old_entry_rejects_prepared_and_native_batch_capability(self):
        source = self.root / "run.py"
        source.write_text("parser.add_argument('--bar-parser', choices=('generic', 'prepared'))\n")
        result = checker.check_entry_cli(source)
        self.assertEqual(result["status"], "FAIL")
        for name in ("--data-mode", "--bar-parser", "--replay-batch-mode"):
            self.assertIn(name, result["detail"])

    def test_cli_requires_registration_instead_of_flags_in_comments(self):
        source = self.root / "run.py"
        source.write_text("\n".join("# " + name for name in checker.CLI_REQUIREMENTS))
        self.assertEqual(checker.check_entry_cli(source)["status"], "FAIL")
        source.write_text("\n".join(
            f"parser.add_argument({name!r}, choices={sorted(choices)!r})"
            for name, choices in checker.CLI_REQUIREMENTS.items()))
        self.assertEqual(checker.check_entry_cli(source)["status"], "PASS")

    def sdk(self, *, callback=True):
        core = NS(__file__="/sdk/engine.so",
            STREAMING_VALIDATION_REVISION="l1-indexed-validation-v1",
            RUN_PROFILING_REVISION="run-phase-timing-v1",
            STREAMING_EXECUTION_REVISION="lazy-logging-same-init-v1",
            BacktestEngine=NS(set_run_profiling=lambda: None, get_run_profile=lambda: None))
        if callback:
            core.BacktestEngine.set_data_callback = lambda: None
        return {
            "bomber.backtest.engine": core,
            "bomber.model.data": NS(__file__="/sdk/data.so", Bar=NS(from_raw_arrays_to_list=lambda: None)),
            "bomber.common.component": NS(__file__="/sdk/component.so", is_logging_enabled=lambda: None),
        }

    def test_sdk_marker_alone_cannot_hide_a_missing_compiled_interface(self):
        with patch.object(checker, "import_module", side_effect=self.sdk(callback=False).__getitem__):
            results = checker.check_native_sdk()
        self.assertEqual(checker.overall_status(results), "failed")
        callback = next(row for row in results if row["name"] == "SDK.set_data_callback")
        self.assertEqual(callback["status"], "FAIL")

    def test_complete_sdk_capabilities_and_optional_missing_extension(self):
        with patch.object(checker, "import_module", side_effect=self.sdk().__getitem__):
            results = checker.check_native_sdk()
        self.assertEqual(checker.overall_status(results), "passed_required_checks")
        with patch.object(checker, "import_module", side_effect=ModuleNotFoundError("not built for this Python")):
            optional = checker.check_optional_cython()
        self.assertEqual(optional["status"], "WARN")
        self.assertEqual(checker.overall_status([*results, optional]), "passed_required_checks")

    def test_config_warns_when_code_exists_but_modes_are_not_enabled(self):
        config = self.root / "strategy.yaml"
        config.write_text(json.dumps({"experiment_setups": {"m5_index_blend": {"data_mode": "raw"}}}))
        with patch.object(checker, "import_module", return_value=NS(safe_load=json.loads)):
            rows, selected = checker.check_config(self.root, config, "m5_index_blend")
        self.assertEqual(selected["data_mode"], "raw")
        self.assertEqual(next(row for row in rows if row["name"] == "配置.data_mode")["status"], "WARN")
        self.assertEqual(next(row for row in rows if row["name"] == "配置.replay_inputs")["status"], "SKIP")

    def test_prepared_structure_is_not_misreported_as_full_integrity(self):
        self.assertEqual(checker.check_prepared(self.root)["status"], "FAIL")
        (self.root / "manifest.json").write_text(json.dumps({"format": "bomber-prepared-bars-v1"}))
        for name in ("index.feather", "issues.feather"):
            (self.root / name).write_bytes(b"structure test only, not a real Feather table")
        result = checker.check_prepared(self.root)
        self.assertEqual(result["status"], "PASS")
        self.assertIn("仅检查结构", result["detail"])
        self.assertIn("未核验", result["detail"])

    def test_report_cannot_overwrite_input_or_existing_evidence(self):
        existing = self.root / "report.json"
        existing.write_text("keep me")
        with self.assertRaises(FileExistsError):
            checker.save_report({}, existing)
        self.assertEqual(existing.read_text(), "keep me")
        with self.assertRaisesRegex(ValueError, "prepared"):
            checker.save_report({}, self.root / "new.json", prepared_root=self.root)
        self.assertFalse((self.root / "new.json").exists())
        output = self.root / "evidence" / "new.json"
        checker.save_report({"status": "failed"}, output)
        self.assertEqual(json.loads(output.read_text()), {"status": "failed"})


if __name__ == "__main__":
    unittest.main()
