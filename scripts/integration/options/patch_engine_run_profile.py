"""Strict, backed-up application of optional native run diagnostics.

Default is read-only. Keep the module support block separate so deployment does
not depend on a particular release's import order or end-of-file layout.
"""
from argparse import ArgumentParser
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory

from scripts.integration.options.patch_streaming_engine import git_apply


REVISION = "run-phase-timing-v1"
DEFAULT_PATCH = (Path(__file__).resolve().parents[3]
    / "doc/evidence/patches/ENGINE_RUN_PROFILE_20261121_BOMBER.patch")


def split_patch(text):
    support = None
    output = []
    for file in re.split(r"(?m)(?=^--- a/)", text):
        if not file:
            continue
        sections = re.split(r"(?m)(?=^@@ )", file)
        output.append(sections[0])
        for section in sections[1:]:
            if f'+RUN_PROFILING_REVISION = "{REVISION}"' in section:
                if support is not None or any(line.startswith("-") for line in section.splitlines()[1:]):
                    raise ValueError("Unexpected profiling support hunk")
                support = "".join(line[1:] for line in section.splitlines(keepends=True)[1:]
                                  if line.startswith("+"))
            else:
                output.append(section)
    logic = "".join(output)
    if (support is None or "_RUN_PROFILE_NAMES = (" not in support
            or "+++ b/backtest/engine.pyx\n" not in logic
            or "+++ b/backtest/engine.pxd\n" not in logic
            or "+    def get_run_profile(self):" not in logic
            or "+    cdef double _run_profile_seconds[15]" not in logic):
        raise ValueError("Unexpected profiling patch layout")
    return logic, support


def update_engine(source_root, patch_path=DEFAULT_PATCH, *, apply=False):
    root = Path(source_root).expanduser().resolve()
    targets = [root / "bomber/backtest" / name for name in ("engine.pyx", "engine.pxd")]
    originals = {path: path.read_bytes() for path in targets}
    source = originals[targets[0]].decode()
    markers = re.findall(r'(?m)^RUN_PROFILING_REVISION\s*=\s*["\']([^"\']+)["\']\s*$', source)
    if markers and markers != [REVISION]:
        raise ValueError("Other/duplicate RUN_PROFILING_REVISION; no source changes")
    logic, support = split_patch(Path(patch_path).read_text())
    with TemporaryDirectory(prefix="engine-run-profile-") as directory:
        patch = Path(directory) / "logic.patch"
        patch.write_text(logic)
        forward = git_apply(root, patch, check=True)
        if forward.returncode:
            already = git_apply(root, patch, check=True, reverse=True).returncode == 0
            if already and markers and source.endswith(support):
                return {"status": "already_applied", "revision": REVISION}
            raise ValueError("Profiling patch does not match both engine.pyx/engine.pxd; no source changes.\n"
                             + forward.stderr.strip())
        if markers:
            raise ValueError("Marker exists without matching profiling implementation; no source changes")
        if not apply:
            return {"status": "check_passed", "revision": REVISION,
                    "sources": [str(path) for path in targets]}
        if any(path.read_bytes() != originals[path] for path in targets):
            raise ValueError("Source changed during validation; no source changes")
        suffix = ".backup-run-profile-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backups = {path: path.with_name(path.name + suffix) for path in targets}
        for path, backup in backups.items():
            shutil.copy2(path, backup)
        try:
            result = git_apply(root, patch)
            if result.returncode:
                raise ValueError(result.stderr.strip())
            with targets[0].open("a") as stream:
                stream.write(support)
        except BaseException:
            for path, backup in backups.items():
                shutil.copy2(backup, path)
            raise
    return {"status": "applied_source_only", "revision": REVISION,
            "backups": [str(path) for path in backups.values()],
            "next": "Rebuild the complete Bomber wheel (engine.pxd changes class layout), then install into the backtest Python environment"}


def main():
    parser = ArgumentParser(description="原生run内部计时补丁：默认只读检查，--apply备份后应用")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = update_engine(args.source_root, apply=args.apply)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
