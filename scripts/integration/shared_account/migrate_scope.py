"""同日两品种调试账本迁移到四品种独立会话；柜台只读核验，不报单。"""

import argparse
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from .config import read_config


def algorithm_arguments(argv):
    result = list(argv)
    if "--ctp-future-buffer-ms" in result:
        index = result.index("--ctp-future-buffer-ms")
        if index + 1 >= len(result) or int(result[index + 1]) not in range(101):
            raise ValueError("CTP缓冲声明无效")
        del result[index:index + 2]
    return result


def validate_scope_configs(previous, updated):
    if (previous["trading_day"] != updated["trading_day"] or previous["environment"] != updated["environment"]
            or updated["environment"] != "realtime" or previous.get("execution_mode") != "independent"
            or updated.get("execution_mode") != "independent" or updated.get("existing_position_policy") != "preserve_external"
            or previous.get("position_scope") != "strategy_instruments" or updated.get("position_scope") != "strategy_instruments"):
        raise ValueError("范围迁移须为同日实时独立账户及外部基线隔离")
    old, new = previous["account"], updated["account"]
    limits = {"max_gross_quantity", "max_gross_notional"}
    if ({k: v for k, v in old.items() if k not in limits} != {k: v for k, v in new.items() if k not in limits}
            or any(Decimal(str(new[k])) < Decimal(str(old[k])) for k in limits)):
        raise ValueError("迁移必须保留账户身份、订单上限、跳价规则；预算不能减少")
    before = {row["kind"]: row for row in previous["strategies"]}
    after = {row["kind"]: row for row in updated["strategies"]}
    if (set(before) != {"main_ema", "cross_section", "calendar_spread"} or set(after) != set(before)
            or len(previous["strategies"]) != 3 or len(updated["strategies"]) != 3):
        raise ValueError("首期范围迁移仅支持01/03/04三个原策略")
    for kind, old_row in before.items():
        new_row = after[kind]
        a, b = algorithm_arguments(old_row["argv"]), algorithm_arguments(new_row["argv"])
        if kind == "cross_section":
            ai, bi = a.index("--products") + 1, b.index("--products") + 1
            if a[ai] != "CU,AL" or b[bi] != "CU,AL,AG,AU" or old_row["strategy_id"] == new_row["strategy_id"]:
                raise ValueError("范围迁移须从CU/AL扩展到CU/AL/AG/AU并使用新横截面ID")
            b[bi] = a[ai]
        elif old_row["strategy_id"] != new_row["strategy_id"]:
            raise ValueError("EMA和跨期的原策略ID必须保留")
        if a != b or old_row.get("stop_after_seconds") != new_row.get("stop_after_seconds"):
            raise ValueError("范围迁移不能改变策略算法参数")
    old_files = {previous["account_state_file"], *(row["state_file"] for row in previous["strategies"])}
    new_files = {updated["account_state_file"], *(row["state_file"] for row in updated["strategies"])}
    if not old_files.isdisjoint(new_files):
        raise ValueError("范围迁移必须输出到独立新文件，保留全部原账本")
    return before, after


def transform_scope_payload(payload, previous, updated, identity, declaration):
    before, after = validate_scope_configs(previous, updated)
    result = deepcopy(payload)
    components = result["component_states"]
    old_identity = components["session_identity"]["identity"]
    if (old_identity.get("account") != previous["account"] or old_identity.get("trading_day") != previous["trading_day"]
            or old_identity.get("environment") != previous["environment"]):
        raise ValueError("原配置与账户检查点身份不符")
    for kind, row in before.items():
        saved = old_identity["strategies"][row["strategy_id"]]
        if saved["argv"] != row["argv"] or saved["kind"] != kind:
            raise ValueError("原策略参数与账户检查点不符")
        if kind != "cross_section":
            new = identity["strategies"][row["strategy_id"]]
            if saved["routes"] != new["routes"] or saved.get("reference") != new.get("reference"):
                raise ValueError("原EMA/跨期路由或固定参考变化，不能迁移")
    shared = components["shared_account"]
    nested = shared["shared"]
    cross = before["cross_section"]["strategy_id"]
    new_cross = after["cross_section"]["strategy_id"]
    if shared.get("failed") or shared.get("pending") or nested.get("draining"):
        raise ValueError("原账户发送未决或停机未完成，须先对账")
    if (any(row["status"] not in {"FILLED", "CANCELED", "REJECTED"} for rows in result["orders"].values() for row in rows)
            or any(Decimal(row["quantity"]) for row in result["positions"]["working_quantities"])
            or any(driver["orders"] for driver in result["ctp_drivers"].values())):
        raise ValueError("范围迁移要求所有原委托已终结")
    if shared["sources"].get(cross) or shared["strategy_revisions"].get(cross, 0):
        raise ValueError("旧横截面已经产生目标，不能按未交易会话扩展品种")
    positions = result["positions"]
    if any(Decimal(row["quantity"]) for row in positions["strategy_positions"] if row["strategy_id"] == cross):
        raise ValueError("旧横截面已有归属持仓，不能重新初始化")
    journal = nested.get("event_journal")
    if journal:
        if (journal["events"][cross] or journal["ack"][cross]
                or any(key.startswith(cross + ":") for key in journal["requests"])
                or any(row["status"] != "completed" for row in journal["requests"].values())):
            raise ValueError("旧横截面有执行历史或账户有未决IPC请求，不能丢弃回报")
        journal["events"].pop(cross); journal["ack"].pop(cross)
        journal["events"][new_cross], journal["ack"][new_cross] = [], 0
    attribution = positions["attribution"]
    if any(Decimal(row["quantity"]) for row in attribution["owned"] if row["strategy_id"] == cross):
        raise ValueError("旧横截面有真实合约归属，不能丢弃")
    if nested.get("bootstrap_signals", {}).get(cross) is not None:
        raise ValueError("旧横截面有恢复信号，不能丢弃")
    if any(any(leg["strategy_id"] == cross for leg in order["legs"]) for order in attribution["orders"].values()):
        raise ValueError("旧横截面有订单分配历史，不能丢弃")
    baseline = components["external_positions"]
    if baseline.get("version") not in {1, 2}:
        raise ValueError("原外部基线版本无效")
    if baseline["version"] == 1:
        # strict账本含范围外物理净仓；observe执行账本只保留原系统成交净仓。
        for row in positions["account_positions"]:
            key = row["key"]["instrument_id"]
            long, short = (Decimal(q) for q in baseline["gross"].get(key, (0, 0)))
            row["quantity"] = str(Decimal(row["quantity"]) - (long - short))
    baseline.update(version=2, managed_instruments=identity["managed_instruments"])
    for name in ("strategy_positions", "strategy_revisions"):
        positions[name] = [row for row in positions[name] if row["strategy_id"] != cross]
    attribution["owned"] = [row for row in attribution["owned"] if row["strategy_id"] != cross]
    shared["strategy_revisions"].pop(cross, None)
    portfolio = result["portfolio"]
    portfolio["strategy_revisions"].pop(cross, None)
    portfolio["contributions"].pop(cross, None)
    nested.update(routes=declaration["shared"]["routes"], budget=declaration["shared"]["budget"],
        managed_instruments=identity["managed_instruments"])
    nested["suspended"] = [sid for sid in nested["suspended"] if sid != cross]
    instruments = sorted({i for port in nested["routes"].values() for i in port["routes"].values()})
    shared["known_instruments"] = instruments
    portfolio["known_keys"] = [{"client_id": updated["account"]["client_id"], "instrument_id": i} for i in instruments]
    components["session_identity"]["identity"] = deepcopy(identity)
    return result


def worker_identity(row, config):
    return {"strategy_id": row["strategy_id"], "kind": row["kind"], "argv": row["argv"],
        "trading_day": config["trading_day"], "environment": config["environment"],
        "replay_md_trading_day": config.get("replay_md_trading_day"), "execution_mode": "independent"}


def write_exclusive(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-config", type=Path, required=True)
    parser.add_argument("--to-config", type=Path, required=True)
    parser.add_argument("--connect", action="store_true")
    args = parser.parse_args(argv)
    previous, updated = read_config(args.from_config), read_config(args.to_config)
    before, after = validate_scope_configs(previous, updated)
    if not args.connect:
        print("范围迁移配置通过；未连接、未写检查点")
        return
    from dotenv import load_dotenv
    from .run import build_host, PROJECT_ROOT
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    from bomber.framework.trader.execution.ipc import ExecutionEventJournal
    from bomber.framework.trader.runtime.reports import LiveRunReport
    load_dotenv(PROJECT_ROOT / ".env")
    report = LiveRunReport(updated["report_dir"], prefix="four-scope-migration")
    report.begin()
    host, status, failure, fields = None, "failed", None, {"orders_submitted": 0}
    with ExitStack() as locks:
        try:
            for path in sorted({previous["account_state_file"], *(r["state_file"] for r in previous["strategies"]),
                                *(r["state_file"] for r in updated["strategies"])}):
                locks.enter_context(strategy_state_lock(Path(path)))
            destinations = [updated["account_state_file"], *(r["state_file"] for r in updated["strategies"])]
            if any(Path(path).exists() for path in destinations):
                raise ValueError("新检查点已存在，禁止重复迁移或覆盖")
            source = JsonStateStore(previous["account_state_file"])
            persisted = source.load()
            if persisted is None:
                raise ValueError("原账户检查点不存在")
            original_bytes = source.path.read_bytes()
            signals = {}
            for kind, row in before.items():
                saved = JsonStateStore(row["state_file"]).load()
                if saved:
                    if saved.payload["identity"] != worker_identity(row, previous):
                        raise ValueError("原worker检查点身份不符")
                    if kind == "cross_section":
                        if saved.payload["revision"] or saved.payload.get("cursor", 0) or saved.payload.get("fills_received", 0):
                            raise ValueError("旧横截面worker已有目标/回报，不允许重置")
                    else:
                        signals[kind] = deepcopy(saved.payload)
                        signals[kind]["identity"] = worker_identity(after[kind], updated)
                elif kind != "cross_section" and persisted.payload["component_states"]["shared_account"]["sources"].get(row["strategy_id"]):
                    raise ValueError("原已交易策略缺少worker检查点，不能丢失消费游标")
            host = build_host(updated, resume=True, read_only=True, strategy_processes=True)
            host.service.event_journal = ExecutionEventJournal(host.strategies)
            identity = host.account.manager.state_components["session_identity"].identity
            declaration = host.service.snapshot_state()
            host.account.manager.repository = source
            transform = lambda payload: transform_scope_payload(payload, previous, updated, identity, declaration)
            host.account.start(resume=True, payload_transform=transform, commit_restored=False)
            if source.path.read_bytes() != original_bytes:
                raise RuntimeError("原账户检查点在核验期间变化，拒绝提交")
            if host.account.driver.submitted_orders or host.account.client.is_armed:
                raise RuntimeError("范围迁移意外取得报单权限")
            # 全部权威核验通过后才创建新账户文件，保留原代并由现有提交恢复机制写入新代。
            destination = Path(updated["account_state_file"])
            fields["created_files"] = []
            write_exclusive(destination, original_bytes)
            fields["created_files"].append(str(destination))
            host.account.manager.repository = JsonStateStore(destination)
            saved_account = host.account.manager.commit_restore()
            for kind, payload in signals.items():
                JsonStateStore(after[kind]["state_file"]).save(payload, expected_generation=0)
                fields["created_files"].append(after[kind]["state_file"])
            fields.update(original_sha256=hashlib.sha256(original_bytes).hexdigest(),
                original_state_unchanged=source.path.read_bytes() == original_bytes,
                account_state_file=str(destination), saved_generation=saved_account.generation,
                preserved_worker_states=list(signals), new_cross_strategy=after["cross_section"]["strategy_id"],
                external_baseline=host.account.scope_diagnostics(),
                resume_workers=[after[kind]["strategy_id"] for kind in signals])
            status = "migrated"
        except BaseException as error:
            failure = str(error) or type(error).__name__
            raise
        finally:
            shutdown_error = None
            try:
                if host:
                    # 未决/不完整输出不自动重试，原文件始终保留。迁移不清算、不启动算法。
                    host.account.abort_preparation()
                    try:
                        host.account.stop()
                    finally:
                        host.resources.close()
            except Exception as error:
                shutdown_error, status = error, "failed"
            finally:
                report.write(status=status, fields={**fields, "failure": failure,
                    "shutdown_failure": str(shutdown_error) if shutdown_error else None,
                    "verification": "readonly_scope_migration_no_orders"}, session=None, events=())
            if shutdown_error is not None and failure is None:
                raise RuntimeError("迁移连接关闭失败，查看停机结果") from shutdown_error


if __name__ == "__main__":
    main()
