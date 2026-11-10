"""Apply the reviewed engine logic patch without depending on import-block layout.

Default is read-only validation. The optional deployment marker is appended at
module scope; algorithm hunks still use strict git-apply checks (no 3-way/fuzz).
Works in a source directory without a .git repository.
"""
from argparse import ArgumentParser
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory


REVISION = "l1-indexed-validation-v1"
DEFAULT_PATCH = Path(__file__).resolve().parents[3] / "doc/evidence/patches/SIM_STREAMING_ENGINE_20261110_BOMBER.patch"


def logic_patch(text):
    """Keep the three reviewed algorithm hunks, drop only the marker insertion."""
    sections = re.split(r"(?m)(?=^@@ )", text)
    if (len(sections) != 5 or "--- a/backtest/engine.pyx\n" not in sections[0]
            or "+++ b/backtest/engine.pyx\n" not in sections[0]):
        raise ValueError("补丁不是本次已审核的单文件四段补丁")
    marker = [section for section in sections[1:] if "STREAMING_VALIDATION_REVISION" in section]
    if len(marker) != 1 or f'+STREAMING_VALIDATION_REVISION = "{REVISION}"' not in marker[0]:
        raise ValueError("补丁版本标记不符合预期")
    changes = [section for section in sections[1:] if section != marker[0]]
    if (not any("cache.instrument(first.instrument_id)" in section for section in changes)
            or not any("cache.instrument(first.bar_type.instrument_id)" in section for section in changes)
            or not any("exchange.book_type <= BookType.L1_MBP" in section for section in changes)):
        raise ValueError("补丁缺少本次三项算法修改")
    return sections[0] + "".join(changes)


def git_apply(root, patch, *, check=False, reverse=False):
    args = ["git", "apply"]
    if check:
        args.append("--check")
    if reverse:
        args.append("--reverse")
    return subprocess.run([*args, "--directory=bomber", str(patch)], cwd=root,
        capture_output=True, text=True)


def update_engine(source_root, patch_path=DEFAULT_PATCH, *, apply=False):
    root = Path(source_root).expanduser().resolve()
    target = root / "bomber/backtest/engine.pyx"
    before = target.read_bytes()
    source = before.decode("utf-8")
    markers = re.findall(r'''(?m)^STREAMING_VALIDATION_REVISION\s*=\s*["']([^"']+)["']\s*$''', source)
    if markers and markers != [REVISION]:
        raise ValueError("源码存在其他版本或重复的STREAMING_VALIDATION_REVISION，未修改")
    patch_text = logic_patch(Path(patch_path).read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="vega-engine-patch-") as directory:
        patch = Path(directory) / "logic.patch"
        patch.write_text(patch_text, encoding="utf-8")
        forward = git_apply(root, patch, check=True)
        already = False
        if forward.returncode:
            already = git_apply(root, patch, check=True, reverse=True).returncode == 0
            if not already:
                raise ValueError("算法补丁也不匹配，源码未修改；需要核对实际add_data/_run校验代码。\n"
                    + forward.stderr.strip())
        if already and markers:
            return {"status": "already_applied", "source": str(target), "revision": REVISION}
        if not apply:
            return {"status": "check_passed", "source": str(target), "logic_already_applied": already,
                "marker_present": bool(markers), "next": "添加--apply后备份并修改源码"}
        if target.read_bytes() != before:
            raise ValueError("检查过程中源码已变化，未修改")
        backup = target.with_name(target.name + ".backup-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        shutil.copy2(target, backup)
        try:
            if not already:
                result = git_apply(root, patch)
                if result.returncode:
                    raise ValueError(result.stderr.strip())
            if not markers:
                with target.open("ab") as stream:
                    stream.write(f'\n\n# Compiled extension deployment evidence.\nSTREAMING_VALIDATION_REVISION = "{REVISION}"\n'.encode())
        except BaseException:
            shutil.copy2(backup, target)
            raise
    return {"status": "applied_source_only", "source": str(target), "backup": str(backup),
        "revision": REVISION, "next": "重新构建并安装Bomber wheel，再核验已加载扩展的版本标记"}


def main():
    parser = ArgumentParser(description="严格校验算法补丁；--apply时备份并适配内核源码")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--patch", type=Path, default=DEFAULT_PATCH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = update_engine(args.source_root, args.patch, apply=args.apply)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
