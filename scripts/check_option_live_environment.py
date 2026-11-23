#!/usr/bin/env python3
"""只读核验08直连部署；不加载env、不创建会话、不连接DDB/CTP或改写状态。

代码摘要对应2026-11-24的M1 Live、无限手数预算、成交回调恢复及算法撤单接入。不同摘要只表示与
该参考不一致，不能单凭摘要认定代码较旧。磁盘/检查点均不证明进程内存已更新。
"""
from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True

SOURCE_HASHES = {
    "demos.08_option_vega.run_direct": "00e3af069de4f55036058898861223d8101722f8ea1c2f4c8c945e4057468a54",
    "demos.direct_ctp": "31481f9360975385976ddf504544234aa1e33a727fe9807fdf264911e4cece83",
    "demos.08_option_vega.run_live": "fe186695519e79a1c96eb52a01b2788ffee0d0abf1e0f93d6986786fd6c04bc1",
    "demos.08_option_vega.live": "1513f81c3a964d935832fdd4534b846af40bd31b16c3c16ec71b1a793069bf65",
    "demos.08_option_vega.strategy": "08b6aa721688fd3015e0dfbd8331011a667d0bc4abce3b9ef18e88426768524e",
    "demos.08_option_vega.vega_signal": "08201b405e2081a7e76885989fcdb1d7ea15125764fe6e177203c8a9250f10db",
    "demos.08_option_vega.recovery": "bd4ab7b1447a8d416471c95389273a1b7a179abe948b75088f45defb6453a00a",
    "bomber.framework.trader.execution.shared": "b70d13faf439627644c28c0bef6a1921ca26f14636ccb60e510f49e030673219",
    "bomber.framework.trader.runtime.live.shared": "9c2269873e05e5d27ffd585813002a909d7ede4ac60a0790b407e6156c26ed94",
    "bomber.framework.trader.execution.ctp.td_transport": "33ab58252a7d0a37d08a5277fb502668fc4af8b46ed0567a6f6cf5de24d39da6",
    "bomber.framework.market.sources": "13ac277c068d22a0793c18dd7ecf5fcede471a774b76abef990ac6ff7efebecd",
    "bomber.framework.trader.execution.nt_algo": "814b38c9ba9f79b90943bdff6a87be4a64805cf1aca7b9e0ab75d566d6f658a0",
    "bomber.framework.trader.execution.ctp.nt_algo": "9851e981b4815775a9bdd3d1dee1fc2cc009427db29d16e26d72a14a46fcb8e1",
}
EXPECTED = {
    "nav": "40000000", "vega_budget": 1.25,
    "delta_targets": [0.2, 0.25, 0.3, 0.35], "option_slot": "13:58",
    "entry_quote_policy": "latest", "index_signal_minutes": 1,
    "hedge_slots": ["10:00", "11:00", "13:30", "14:00", "14:55"],
    "flatten_slot": "14:50", "min_remaining_days": 5, "close_remaining_days": 2,
    "rate": 0.02, "min_time_value": 5.0, "max_market_age_seconds": 300,
    "max_iv_age_seconds": 300, "max_option_lots": 0, "max_total_option_lots": 0,
    "max_future_lots": 0, "max_notional": "1000000000", "limit_offset_ticks": 1,
}


def normalized(value):
    return json.loads(json.dumps(value, default=lambda v: str(v) if isinstance(v, Decimal) else v))


def differences(actual, expected):
    actual = normalized(actual)
    return {key: {"actual": actual.get(key), "expected": value}
            for key, value in expected.items() if actual.get(key) != value}


def checkpoint_parameters(document, strategy_id):
    identity = document["payload"]["component_states"]["session_identity"]["identity"]
    reference = identity["strategies"][strategy_id]["reference"]
    return {**reference["config"], **{key: reference.get(key)
            for key in ("max_notional", "limit_offset_ticks")}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", type=Path, default=Path("demos/08_option_vega/config/strategy.yaml"))
    parser.add_argument("--config-id", default="live_simnow")
    parser.add_argument("--state-file", type=Path, help="只读检查当前使用的账户state.json，不是signal.json")
    parser.add_argument("--strategy-id", default="option-vega-direct")
    options = parser.parse_args(argv)
    failures = []

    def check(name, passed, detail):
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}", flush=True)
        if not passed:
            failures.append(name)

    print(f"Python: {sys.executable}\n版本: {sys.version.split()[0]}\n运行目录: {Path.cwd()}")
    modules = {}
    for name, expected in SOURCE_HASHES.items():
        try:
            module = import_module(name)
            modules[name] = module
            path = Path(module.__file__).resolve()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            check(name, digest == expected, f"{path}\n  sha256={digest}")
        except Exception as error:
            check(name, False, f"{type(error).__name__}（模块无法导入或读取）")

    try:
        entry = modules["demos.08_option_vega.run_live"]
        # 只调用参数解析；--connect等标志在这里不创建连接。不存在的假状态路径
        # 用于通过“新会话”参数校验，不创建文件，也不读取/恢复用户的signal检查点。
        unused = Path.cwd() / f".option-live-audit-unused-{os.getpid()}.json"
        if unused.exists():
            raise ValueError("核验用假状态路径已存在")
        today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        args = entry.shared_arguments(dict(state_file=str(unused), argv=["--config-file",
            str(options.config_file), "--config-id", options.config_id]),
            dict(trading_day=today, seconds=86400), resume=False)
        effective = {**vars(args.config), "max_notional": args.max_notional,
            "limit_offset_ticks": args.limit_offset_ticks}
        print("策略JSON:", args.strategy_config.resolve())
        delta = differences(effective, EXPECTED)
        check("Live策略参数", not delta, json.dumps(delta or normalized(effective), ensure_ascii=False))
        source = entry.source_config(args)
        check("市场分钟源", source.source == "dolphindb" and source.table == "stream_cffex_1min"
            and source.timestamp_label == "end" and source.max_age_seconds == 300,
            f"source={source.source} table={source.table} label={source.timestamp_label} max_age={source.max_age_seconds}")
        check("分钟帧等待", args.frame_wait_seconds == 20, args.frame_wait_seconds)
        bundle = json.loads(args.index_feature_config.read_text())
        dataset, = [row for row in bundle["datasets"] if row["schema"]["dataset_id"] == args.index_dataset
                    and row["schema"]["schema_version"] == args.index_schema_version]
        check("原始指数M1", args.index_source == "dolphindb-dfs" and args.index_dataset == "index.minute"
            and dataset["schema"]["frequency"]["period_ns"] == 60_000_000_000
            and dataset["bar_projection"]["bar_spec"] == "1-MINUTE"
            and dataset["source"]["table"] == "index_1min",
            f"{args.index_feature_config.resolve()} dataset={args.index_dataset}")
        budget = modules["bomber.framework.trader.execution.shared"].SharedRiskBudget(None, args.max_notional)
        check("不限手数且金额受限的预算", budget.max_gross_quantity is None and not budget.is_unlimited,
            budget.state())
    except (Exception, SystemExit) as error:
        check("Live参数/预算核验", False, f"{type(error).__name__}（请查看上方解析错误或模块检查结果）")

    if options.state_file is not None:
        if not options.state_file.is_file():
            print(f"WARN 检查点尚不存在: {options.state_file}；无法核验已保存会话参数")
        else:
            try:
                document = json.loads(options.state_file.read_text())
                delta = differences(checkpoint_parameters(document, options.strategy_id), EXPECTED)
                saved = document.get("saved_at_ns")
                saved_time = (datetime.fromtimestamp(saved / 1e9, ZoneInfo("Asia/Shanghai")).isoformat()
                              if saved is not None else "未记录")
                check("检查点已保存参数", not delta,
                    f"{options.state_file.resolve()} generation={document.get('generation')} saved_at={saved_time} "
                    + json.dumps(delta or {"matched": True}, ensure_ascii=False))
            except Exception as error:
                check("检查点已保存参数", False, f"{type(error).__name__}（文件结构不匹配或无法读取）")

    print("说明：这是新解释器的只读核验，不证明旧进程内存已更新。请同时核对当前进程命令与启动后的日志。")
    print("结果: " + (f"有{len(failures)}项不一致，先核对再重启" if failures else "所检查的代码和参数与本次参考一致"))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
