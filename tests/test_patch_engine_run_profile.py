"""Patch drift, rollback and idempotency without native SDK dependencies."""
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bomber.framework.performance import enable_engine_run_profiling
from scripts.integration.options.patch_engine_run_profile import DEFAULT_PATCH, REVISION, split_patch, update_engine
from scripts.integration.options.patch_streaming_engine import git_apply


class EngineRunProfilePatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        logic, _ = split_patch(DEFAULT_PATCH.read_text())
        self.originals = {}
        for file in re.split(r"(?m)(?=^--- a/)", logic):
            if not file:
                continue
            name = re.search(r"^--- a/(\S+)", file).group(1)
            fragments = []
            for hunk in re.split(r"(?m)(?=^@@ )", file)[1:]:
                fragments.append("".join(line[1:] for line in hunk.splitlines(keepends=True)[1:]
                                         if line.startswith((" ", "-"))))
            target = self.root / "bomber" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            self.originals[target] = ("# fixture import layout\n" + "\n# gap\n".join(fragments)).encode()
            target.write_bytes(self.originals[target])

    def test_check_apply_and_repeat_without_git_repository(self):
        self.assertEqual(update_engine(self.root)["status"], "check_passed")
        for target, original in self.originals.items():
            self.assertEqual(target.read_bytes(), original)
        self.assertEqual(update_engine(self.root, apply=True)["status"], "applied_source_only")
        self.assertEqual(update_engine(self.root, apply=True)["status"], "already_applied")
        for target, original in self.originals.items():
            backups = list(target.parent.glob(target.name + ".backup-run-profile-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)

    def test_pxd_drift_refuses_both_files_before_backup(self):
        target = self.root / "bomber/backtest/engine.pxd"
        target.write_text(target.read_text().replace("_response_data", "_changed_response"))
        before = {p: p.read_bytes() for p in self.originals}
        with self.assertRaisesRegex(ValueError, "does not match both"):
            update_engine(self.root, apply=True)
        self.assertEqual({p: p.read_bytes() for p in self.originals}, before)
        self.assertEqual(list(target.parent.glob("*.backup-run-profile-*")), [])

    def test_partial_apply_failure_restores_both_files(self):
        def fail(root, patch_path, *, check=False, reverse=False):
            if check:
                return git_apply(root, patch_path, check=check, reverse=reverse)
            for target in self.originals:
                target.write_text("partial write")
            raise OSError("fixture apply failure")
        with patch("scripts.integration.options.patch_engine_run_profile.git_apply", side_effect=fail):
            with self.assertRaisesRegex(OSError, "fixture apply failure"):
                update_engine(self.root, apply=True)
        self.assertEqual({p: p.read_bytes() for p in self.originals}, self.originals)

    def test_other_revision_and_unexpected_patch_are_refused(self):
        target = self.root / "bomber/backtest/engine.pyx"
        target.write_text(target.read_text() + '\nRUN_PROFILING_REVISION = "other"\n')
        with self.assertRaisesRegex(ValueError, "Other/duplicate"):
            update_engine(self.root, apply=True)
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            split_patch("arbitrary patch")

    def test_old_kernel_fails_explicitly_and_new_kernel_is_enabled(self):
        with self.assertRaisesRegex(RuntimeError, "重新编译"):
            enable_engine_run_profiling(object())
        class Engine:
            def set_run_profiling(self, value):
                self.enabled = value
            def get_run_profile(self):
                return {}
        engine = Engine()
        enable_engine_run_profiling(engine)
        self.assertTrue(engine.enabled)


if __name__ == "__main__":
    unittest.main()
