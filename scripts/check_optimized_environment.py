#!/usr/bin/env python3
"""Read-only deployment audit for the 08 backtest optimization stack.

Copy this file alone to the target project's scripts directory. Reference hashes
are embedded; the target does not need Git or a second reference file.
Imports locate the active packages but never instantiate an engine or run a
strategy. Only an explicitly requested, new JSON report is written.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
from importlib import import_module
from importlib.util import find_spec
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
REFERENCE_COMMIT = "e6c2ac4b395871803d11767b1d0b0f3436e50179"
SOURCE_CHECKS = (
    ("配置文件解析", "bomber.framework.cli",
     "f3b5a74aea689ab707eca70c3dd9b09725180a624545383908f19e17456634aa", ["class ConfigArgumentParser","add_json_config_argument"]),
    ("性能统计", "bomber.framework.performance",
     "3805779cec25005330bbbce4a06a0b323d07c7b86bb87a61071e835f0b778651", ["enable_engine_run_profiling","class PerformanceStats"]),
    ("行情目录索引", "bomber.framework.dataprep.catalog",
     "23dc2f2445a0f5cf623362eb622b5566c104f8a9983d3f1cc46409b4dec549e2", ["directory_scans"]),
    ("向量化行情准备/Prepared解析", "bomber.framework.dataprep.bars",
     "2619b00c2820853db3b92a93097536e18c86e3effdc5bab9c28722f201fd9627", ["PreparedFrameReader","add_prepared_bar_source"]),
    ("Prepared快照", "bomber.framework.dataprep.prepared",
     "d0424dfbeda24b4c8e9d40eb0dce3c767a1859dfa0f7de9535344c80ed68d229", ["class PreparedDataset","build_prepared_dataset"]),
    ("Arrow行情视图", "bomber.framework.dataprep.columnar",
     "39d9102c9d16372abd4c113479405bd77522bc345c1cbc022d0a6e2aa5f626f2", ["class ArrowBarFrame","class CertifiedArrowFrame"]),
    ("认证输入和帧产物", "bomber.framework.dataprep.replay_inputs",
     "5a7992db912818f9fca58f36a6c0e87ee84b4742e1b4b6746881fcc0bf3d61f3", ["class ReplayInputs","native_identity"]),
    ("期权链Prepared读取", "bomber.framework.dataprep.scenarios.option_chain",
     "998566a7dbe30db56fa98ff37976cd34f595c065f78e9ce5f01da96184802247", ["prepared_dataset","frame_format"]),
    ("同刻回放分组", "bomber.framework.market.replay.base",
     "e6ff9158fa6482bbf1ff7604925f14fd430cf303a760e1d4e92976032b5fe1aa", ["set_replay_batch_handler","groupby"]),
    ("原生批量Bar转换", "bomber.framework.market.replay.native_batch",
     "612d371436d58d28171c757be040e599751a3303b01399d96c726d75fb327f26", ["from_raw_arrays_to_list","class NativeBarBatchParser"]),
    ("标准化表快速解析", "bomber.framework.market.replay.parsers.bar.fixed",
     "1e317326b054b6564a0005483efee94e9efd3bbd3c74b31ebea8c454cb7147b7", ["class PreparedBarParser"]),
    ("策略批量仓位接口", "bomber.framework.trader.template",
     "d985dfbf1309c13480c3652c14dde068efa2c580e5fc90b7caa577f0d3098cdc", ["def position_quantities"]),
    ("公共Runner批量仓位", "bomber.framework.trader.runner",
     "af15dc5e2ed01132b7072e873cd7ba4b161935a97e258b2440c4da6ce1ea4050", ["def position_quantities"]),
    ("账本批量仓位", "bomber.framework.trader.portfolio",
     "9f8eb59c4f44e1758739c60be4d5e35fe12dde79d63eefbfa8796afc33254e80", ["def strategy_quantities"]),
    ("活动订单批量在途", "bomber.framework.trader.execution.attribution",
     "c73e28cc89f343c2b43e29b25f0a7ad7f280b6ae4c63e58a0cd12edf0e79a3ac", ["def working_quantities"]),
    ("批量模拟Backend", "bomber.framework.trader.execution.simulation.backend",
     "3f03e9acda57e8444583a3cbf23fdcfa017a5a764b000ada0cdfbd28c0f81cb9", ["def process_market_batch","set_data_callback"]),
    ("现金结算空闲判断", "bomber.framework.trader.execution.simulation.option_expiry",
     "602d55f654aea1f8a0fee1feba4ed630185a44c59595c26eecc1599c1ec3fa9b", ["len(self.expired) == len(self.settled)"]),
    ("行情绑定索引/逐事件发布", "bomber.framework.trader.runtime.market_adapter",
     "ac659a196d3bc5f21261a3bef14ef7289313889ff060b44a58d8a93e106e1efc", ["_binding_specs","process_market_batch"]),
    ("合批Runtime装配", "bomber.framework.trader.runtime.historical",
     "e6600c25b8b599fd4504f8cb70c59fbe341e0a72d8f8e7a9be9e9d9266f66a64", ["batch_same_timestamp"]),
    ("增量特征存储/业务槽索引", "bomber.framework.datahub.features.store",
     "7a32514047d60c5731318150b75101f80c05d6db5a6c76210f51e6777aa31800", ["capture_for","self._rows.update(staged)"]),
    ("特征按槽对齐", "bomber.framework.datahub.features.alignment",
     "ff16e6f0e780a8fb318179e16c002e2958e3562ac16eb2b1b91c30a89e18166b", ["capture_for"]),
    ("可复用分钟帧编解码", "bomber.framework.datahub.features.prepared_frames",
     "05549123715248e3567b40573e99490c3009cb650f71031f39a6d3e169dc157e", ["decode_frames","restore_market_observations"]),
    ("公共Cython帧恢复选择", "bomber.framework.datahub.features.restoration",
     "3f3994e4d9ac8c4a0823c185ccd4802ea80b57ade7a8b25d5e8f17624deb3b73", ["native_restoration_module","class ColumnarObservations"]),
    ("08回测入口", "demos.08_option_vega.run_backtest",
     "5ba04bf319b7d379fc7516e209c90c429ef5195a0fdca87b9b778be9f7b3c66d", ["--data-mode","--replay-batch-mode","--profile-engine-run"]),
    ("08策略查询及静态缓存", "demos.08_option_vega.strategy",
     "0e82a3c6040df42ee788e5cd328d37b5cde227303cbfdc6fc8aa771f3d3aef88", ["self.position_quantities(","strategy_static_identity_builds","scheduled_hedge_months_calculated"]),
    ("策略JSON参数接入", "demos.08_option_vega.vega_signal",
     "08201b405e2081a7e76885989fcdb1d7ea15125764fe6e177203c8a9250f10db", ["add_json_config_argument","max_notional"]),
    ("08分钟帧复用", "demos.08_option_vega.feature.backtest",
     "1e793b8b4a7283f8f8da4c58dc135f2ab30db285ba903c74dd79ff3e9dabada5", ["replay_inputs","load_frames"]),
)
CLI_REQUIREMENTS = {
    "--data-mode": {"prepared"},
    "--prepared-root": set(),
    "--prepared-verify": {"metadata", "full"},
    "--bar-parser": {"native_batch"},
    "--replay-batch-mode": {"same_init"},
    "--profile-engine-run": set(),
    "--replay-inputs": set(),
    "--prepare-replay-inputs": set(),
}


def row(name, status, detail, **extra):
    return dict(name=name, status=status, detail=detail, **extra)


def check_source(label, module, expected, markers):
    try:
        spec = find_spec(module)
        if spec is None or not spec.origin:
            raise ImportError(f"未找到模块: {module}")
        path = Path(spec.origin).resolve()
        data = path.read_bytes()
        text = data.decode("utf-8")
    except Exception as exc:
        return row(label, "FAIL", f"定位/读取模块失败: {type(exc).__name__}: {exc}", module=module)
    actual = hashlib.sha256(data).hexdigest()
    missing = [marker for marker in markers if marker not in text]
    if missing:
        status, detail = "FAIL", "缺少优化标记: " + ", ".join(missing)
    elif actual != expected:
        status, detail = "REVIEW", "关键标记存在，但全文SHA256不同；不能直接判为旧版或已验证版本"
    else:
        status, detail = "PASS", "与参考优化源码一致"
    return row(label, status, detail, module=module, path=str(path),
               sha256=actual, expected_sha256=expected)


def check_entry_cli(path):
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        declared = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                continue
            options = [arg.value for arg in node.args
                       if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            choices = set()
            for keyword in node.keywords:
                if keyword.arg == "choices":
                    try:
                        choices = set(ast.literal_eval(keyword.value))
                    except (ValueError, TypeError, SyntaxError):
                        pass
            for option in options:
                declared[option] = choices
        missing = [option for option, choices in CLI_REQUIREMENTS.items()
                   if option not in declared or not choices.issubset(declared[option])]
    except Exception as exc:
        return row("08入口参数注册", "FAIL", f"无法检查入口: {type(exc).__name__}: {exc}")
    return row("08入口参数注册", "FAIL" if missing else "PASS",
               "缺少参数/选项: " + ", ".join(missing) if missing else "prepared、原生批量、合批及计时参数均已注册",
               path=str(path))


def check_native_sdk():
    results = []
    try:
        core = import_module("bomber.backtest.engine")
        path = getattr(core, "__file__", None)
        for name, expected in (
            ("STREAMING_VALIDATION_REVISION", "l1-indexed-validation-v1"),
            ("RUN_PROFILING_REVISION", "run-phase-timing-v1"),
            ("STREAMING_EXECUTION_REVISION", "lazy-logging-same-init-v1"),
        ):
            actual = getattr(core, name, None)
            results.append(row("SDK." + name, "PASS" if actual == expected else "FAIL",
                               f"实际={actual!r}; 预期={expected!r}", path=path))
        for name in ("set_data_callback", "set_run_profiling", "get_run_profile"):
            found = callable(getattr(core.BacktestEngine, name, None))
            results.append(row("SDK." + name, "PASS" if found else "FAIL",
                               "接口可用" if found else "缺少编译接口", path=path))
    except Exception as exc:
        results.append(row("SDK.回测内核", "FAIL", f"{type(exc).__name__}: {exc}"))
    for module_name, member, label in (
        ("bomber.model.data", ("Bar", "from_raw_arrays_to_list"), "SDK.原生批量Bar构造"),
        ("bomber.common.component", ("is_logging_enabled",), "SDK.日志级别判断"),
    ):
        try:
            module = import_module(module_name)
            value = module
            for name in member:
                value = getattr(value, name, None)
            found = callable(value)
            results.append(row(label, "PASS" if found else "FAIL",
                               "接口可用" if found else "缺少编译接口",
                               path=getattr(module, "__file__", None)))
        except Exception as exc:
            results.append(row(label, "FAIL", f"{type(exc).__name__}: {exc}"))
    return results


def check_optional_cython():
    try:
        module = import_module("bomber.framework.datahub.features._restoration_native")
        found = callable(getattr(module, "build_market", None))
        return row("可选.Cython分钟帧恢复", "PASS" if found else "WARN",
                   "扩展可用；仅复用replay输入时使用" if found else "扩展缺少build_market",
                   path=getattr(module, "__file__", None))
    except Exception as exc:
        return row("可选.Cython分钟帧恢复", "WARN",
                   f"当前Python中扩展不可用；replay帧恢复加速未就绪: {type(exc).__name__}: {exc}")


def resolve_path(root, value):
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def check_config(root, filename, config_id):
    if filename is None:
        return [row("配置启用状态", "SKIP", "未指定YAML；仅检查代码及SDK能力")], {}
    try:
        yaml = import_module("yaml")
        path = resolve_path(root, filename)
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = document["experiment_setups"][config_id]
        if not isinstance(config, dict):
            raise ValueError("配置ID对应内容须是映射")
    except Exception as exc:
        return [row("配置读取", "FAIL", f"{type(exc).__name__}: {exc}")], {}
    results = [row("配置读取", "PASS", f"config_id={config_id}", path=str(path))]
    for name, expected in (("data_mode", "prepared"), ("bar_parser", "native_batch"),
                           ("replay_batch_mode", "same_init")):
        value = config.get(name)
        results.append(row("配置." + name, "PASS" if value == expected else "WARN",
                           f"YAML={value!r}; 加速路径={expected!r}；此检查不含命令行覆盖"))
    if config.get("replay_inputs"):
        results.append(row("配置.replay_inputs", "PASS",
                           "已声明复用；本脚本不核验产物与行情/特征/SDK的完整匹配",
                           path=str(resolve_path(root, config["replay_inputs"]))))
    else:
        results.append(row("配置.replay_inputs", "SKIP",
                           "未声明认证数组/分钟帧复用；当前prepared+native_batch仍会现场准备数组和帧"))
    return results, config


def check_prepared(root):
    root = Path(root).resolve()
    missing = [name for name in ("manifest.json", "index.feather", "issues.feather")
               if not (root / name).is_file()]
    if missing:
        return row("Prepared目录结构", "FAIL", "缺少: " + ", ".join(missing), path=str(root))
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != "bomber-prepared-bars-v1":
            raise ValueError(f"未知format: {manifest.get('format')!r}")
        return row("Prepared目录结构", "PASS", "仅检查结构，未核验分区SHA256或原始数据等价",
                   path=str(root), snapshot_id=manifest.get("snapshot_id"),
                   requested=manifest.get("requested"))
    except Exception as exc:
        return row("Prepared目录结构", "FAIL", f"{type(exc).__name__}: {exc}", path=str(root))


def overall_status(checks):
    statuses = {item["status"] for item in checks}
    if "FAIL" in statuses:
        return "failed"
    if "REVIEW" in statuses:
        return "needs_review"
    return "passed_required_checks"


def save_report(report, output, prepared_root=None):
    output = Path(output).expanduser().resolve()
    if prepared_root is not None and output.is_relative_to(Path(prepared_root).resolve()):
        raise ValueError("检查报告不能写入只读prepared目录")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Never replace source, config, prior evidence, or input files.
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config-file", type=Path)
    parser.add_argument("--config-id", default="m5_index_blend")
    parser.add_argument("--prepared-root", type=Path)
    parser.add_argument("--output", type=Path, help="可选的新JSON报告；禁止覆盖已有文件")
    args = parser.parse_args(argv)
    root = args.project_root.expanduser().resolve()
    # Match run_backtest.py's local-first import behavior.
    sys.path.insert(0, str(root))
    print(f"Python: {sys.executable}\n版本: {sys.version.split()[0]}\n项目: {root}")
    print(f"源码参考提交: {REFERENCE_COMMIT}")
    checks = [check_source(*item) for item in SOURCE_CHECKS]
    entry = next(item for item in checks if item.get("module") == "demos.08_option_vega.run_backtest")
    checks.append(check_entry_cli(entry["path"]) if entry.get("path") else
                  row("08入口参数注册", "FAIL", "入口无法定位，不能确认参数能力"))
    checks.extend(check_native_sdk())
    checks.append(check_optional_cython())
    config_checks, config = check_config(root, args.config_file, args.config_id)
    checks.extend(config_checks)
    prepared = args.prepared_root or config.get("prepared_root")
    prepared = resolve_path(root, prepared) if prepared is not None else None
    checks.append(check_prepared(prepared) if prepared else
                  row("Prepared目录结构", "SKIP", "未指定prepared根目录"))
    status = overall_status(checks)
    report = dict(status=status, reference_commit=REFERENCE_COMMIT,
                  python=sys.executable, python_version=sys.version.split()[0],
                  project_root=str(root), counts=dict(Counter(item["status"] for item in checks)),
                  checks=checks, scope="静态源码/编译接口/配置声明/数据结构检查；不启动引擎、行情或策略；不证明性能和金融等价")
    for item in checks:
        print(f"[{item['status']}] {item['name']}: {item['detail']}")
        if item.get("path"):
            print(f"       {item['path']}")
    print("\n汇总: " + json.dumps(report["counts"], ensure_ascii=False))
    print("状态: " + status)
    print("PASS=源码匹配/能力可用；FAIL=缺失或不可用；REVIEW=源码不同待核对；WARN=配置/可选加速注意项；SKIP=未声明。")
    print("此脚本不证明回测金融结果等价；版本标记也不等于整个二进制的源码证明。")
    if args.output:
        try:
            save_report(report, args.output, prepared)
            print("JSON报告: " + str(args.output.resolve()))
        except (OSError, ValueError) as exc:
            print(f"报告保存失败: {exc}", file=sys.stderr)
            return 1
    return 0 if status == "passed_required_checks" else 1


if __name__ == "__main__":
    raise SystemExit(main())

