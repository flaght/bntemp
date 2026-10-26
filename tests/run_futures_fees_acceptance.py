"""服务器手续费集中验收；原生依赖、源码摘要、分组日志与失败复测。"""
from __future__ import annotations

import argparse
import hashlib
from importlib import import_module
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "doc/evidence/checksums/FUTURES_FEES_20261026_SHA256.txt"
GROUPS = {
    "environment": ("tests.run_futures_fees_acceptance", "--probe"),
    "rules": ("unittest", "-v", "tests.test_futures_fees.FeeContractTests"),
    "native": ("unittest", "-v", "tests.test_futures_fees_native"),
    "references": ("unittest", "-v", "tests.test_basic_alignment", "tests.test_reference_sources"),
    "ctp": ("tests.run_ctp_high_fidelity",),
    "regression": ("tests.run_option_vega_offline",),
}


def probe():
    """仅在独立进程导入SDK，缺依赖直接失败，不跳过、不安装替身。"""
    modules = {}
    for name in ("pandas", "pyarrow", "dotenv", "bomber.backtest.engine",
                 "bomber.model.objects", "bomber.model.data", "bomber.framework.fees",
                 "bomber.framework.dataprep.metadata",
                 "bomber.framework.trader.execution.simulation.fees"):
        module = import_module(name)
        path = Path(module.__file__).resolve()
        modules[name] = {"path": str(path), "version": getattr(module, "__version__", None)}
        if name.startswith("bomber.framework.") and not path.is_relative_to(ROOT):
            raise RuntimeError(f"模块没有加载本次pro源码: {name}={path}")
        if name in {"bomber.backtest.engine", "bomber.model.objects", "bomber.model.data"}:
            if path.suffix not in {".so", ".pyd"}:
                raise RuntimeError(f"没有加载Bomber原生扩展: {name}={path}")
    print(json.dumps({"python": sys.executable, "version": sys.version,
        "project": str(ROOT), "platform": platform.platform(), "modules": modules},
        ensure_ascii=False, indent=2), flush=True)


def source_hashes():
    hashes = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        expected, relative = line.split(maxsplit=1)
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT):
            raise RuntimeError(f"清单路径超出项目: {relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        hashes[relative] = actual
        if actual != expected:
            raise RuntimeError(f"源码未同步: {relative} expected={expected} actual={actual}")
    if not hashes:
        raise RuntimeError("手续费部署清单为空")
    return hashes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", choices=tuple(GROUPS), help="只复测指定组，可重复")
    parser.add_argument("--timeout", type=float, default=180, help="每组超时秒数")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "tests/results")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.probe:
        probe()
        return 0
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout须为有限正数")
    selected = tuple(dict.fromkeys(("environment", *(args.only or GROUPS))))
    if args.list:
        for name in selected:
            print(name, sys.executable, "-u -m", *GROUPS[name])
        return 0
    destination = args.report_dir.resolve() / f"futures-fees-acceptance-{time.time_ns()}"
    destination.mkdir(parents=True, exist_ok=False)
    report = {"python": sys.executable, "version": sys.version, "project": str(ROOT),
        "platform": platform.platform(), "selected": selected, "status": "running", "groups": []}

    def save():
        (destination / "summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"手续费远端验收 Python={sys.executable} project={ROOT} report={destination}", flush=True)
    try:
        report["sources"] = source_hashes()
        report["manifest_sha256"] = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    except (OSError, ValueError, RuntimeError) as error:
        report.update(status="failed", deployment_error=str(error))
        save()
        print(f"部署核对失败: {error}", flush=True)
        return 2
    save()
    for name in selected:
        command = [sys.executable, "-u", "-m", *GROUPS[name]]
        print(f"开始: {name}", flush=True)
        started = time.monotonic()
        env = dict(os.environ, FUTURES_FEES_ARTIFACT_DIR=str(destination / "native-artifacts"))
        try:
            process = subprocess.run(command, cwd=ROOT, env=env, timeout=args.timeout,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            code, output = process.returncode, process.stdout
        except subprocess.TimeoutExpired as error:
            raw = error.stdout or ""
            output = (raw.decode(errors="replace") if isinstance(raw, bytes) else raw) + "\nTIMEOUT\n"
            code = 124
        log = destination / f"{name}.log"
        log.write_text(output, encoding="utf-8")
        counts = re.findall(r"Ran (\d+) tests? in", output)
        report["groups"].append({"group": name, "command": command, "exit_code": code,
            "seconds": time.monotonic() - started, "test_count": int(counts[-1]) if counts else None,
            "log": str(log)})
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
        print(f"结束: {name} exit={code}", flush=True)
        if code:
            report["status"] = "failed"
            save()
            print(f"停止。修复后仅复测: python -u -m tests.run_futures_fees_acceptance --only {name}", flush=True)
            return 1
        save()
    report["status"] = "passed"
    save()
    print(f"全部选定手续费验收组通过: {destination}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
