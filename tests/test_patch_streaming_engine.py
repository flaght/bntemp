"""Real git-apply checks outside a Git repository; no SDK or wheel required."""
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.integration.options.patch_streaming_engine import (DEFAULT_PATCH, REVISION,
    git_apply, logic_patch, update_engine)


class EnginePatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "bomber/backtest/engine.pyx"
        self.target.parent.mkdir(parents=True)
        # Old sides of the reviewed hunks, with deliberately different imports.
        # git-apply must verify every actual algorithm context before any write.
        hunks = re.split(r"(?m)(?=^@@ )", DEFAULT_PATCH.read_text())[1:]
        fragments = ["".join(line[1:] for line in hunk.splitlines(keepends=True)[1:]
            if line.startswith((" ", "-"))) for hunk in hunks]
        fragments[0] = fragments[0].replace("from bomber.trading.strategy cimport Strategy",
            "from bomber.trading.strategy cimport Strategy  # different release layout")
        self.before = ("# Different release/import layout\n" + "\n# gap\n".join(fragments)).encode()
        self.target.write_bytes(self.before)

    def test_check_is_read_only_and_apply_works_without_git_metadata(self):
        self.assertNotEqual(git_apply(self.root, DEFAULT_PATCH, check=True).returncode, 0)
        report = update_engine(self.root)
        self.assertEqual(report["status"], "check_passed")
        self.assertFalse((self.root / ".git").exists())
        self.assertEqual(self.target.read_bytes(), self.before)
        self.assertEqual(list(self.target.parent.glob("*.backup-*")), [])
        report = update_engine(self.root, apply=True)
        self.assertEqual(Path(report["backup"]).read_bytes(), self.before)
        after = self.target.read_text()
        self.assertIn("cache.instrument(first.instrument_id) is not None", after)
        self.assertIn("cache.instrument(first.bar_type.instrument_id) is not None", after)
        self.assertIn("if exchange.book_type <= BookType.L1_MBP:", after)
        self.assertTrue(after.endswith(f'STREAMING_VALIDATION_REVISION = "{REVISION}"\n'))
        self.assertEqual(update_engine(self.root, apply=True)["status"], "already_applied")
        self.assertEqual(len(list(self.target.parent.glob("*.backup-*"))), 1)

    def test_algorithm_drift_refuses_changes_and_does_not_create_backup(self):
        drifted = self.before.replace(b"first.instrument_id in self._kernel.cache.instrument_ids()",
            b"first.instrument_id in other_cache")
        self.target.write_bytes(drifted)
        with self.assertRaisesRegex(ValueError, "算法补丁也不匹配"):
            update_engine(self.root, apply=True)
        self.assertEqual(self.target.read_bytes(), drifted)
        self.assertEqual(list(self.target.parent.glob("*.backup-*")), [])

    def test_missing_marker_on_already_patched_logic_is_added_without_reapplying(self):
        self.target.write_bytes(self.before)
        applied = update_engine(self.root, apply=True)
        content = self.target.read_text()
        self.target.write_text(content[:content.index("# Compiled extension deployment evidence.")])
        report = update_engine(self.root, apply=True)
        self.assertEqual(report["status"], "applied_source_only")
        self.assertIn(REVISION, self.target.read_text())
        self.assertNotEqual(report["backup"], applied["backup"])

    def test_partial_apply_failure_restores_original_source(self):
        def failing(root, patch_path, *, check=False, reverse=False):
            if check:
                return git_apply(root, patch_path, check=check, reverse=reverse)
            self.target.write_text("partially written")
            raise OSError("fixture apply failure")
        with patch("scripts.integration.options.patch_streaming_engine.git_apply", side_effect=failing):
            with self.assertRaisesRegex(OSError, "fixture apply failure"):
                update_engine(self.root, apply=True)
        self.assertEqual(self.target.read_bytes(), self.before)

    def test_other_revision_is_not_overwritten(self):
        self.target.write_bytes(self.before + b'\nSTREAMING_VALIDATION_REVISION = "other"\n')
        before = self.target.read_bytes()
        with self.assertRaisesRegex(ValueError, "其他版本"):
            update_engine(self.root, apply=True)
        self.assertEqual(self.target.read_bytes(), before)

    def test_unexpected_patch_layout_is_rejected(self):
        with self.assertRaises(ValueError):
            logic_patch("arbitrary patch")


if __name__ == "__main__":
    unittest.main()
