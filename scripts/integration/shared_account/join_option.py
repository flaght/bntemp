"""Hybrid与原08账本的只读合并预检；不连接柜台、不写文件、不开放交易。"""
import argparse
from decimal import Decimal
import json
from pathlib import Path

from .config import read_config


def inspect_join(config, account, option, signals, *, strategy_limits_only=False):
    """检查归属来源及原账户限额；物理仓位仍须停机后的柜台权威核验。"""
    base, incoming = account.payload, option.payload
    identity = base["component_states"]["session_identity"]["identity"]
    declarations = {row["strategy_id"]: row for row in config["strategies"]}
    if (not declarations or any(row["kind"] != "hybrid_transformer" for row in declarations.values())
            or config.get("execution_mode") != "independent"
            or config.get("existing_position_policy") != "preserve_external"):
        raise ValueError("源配置须为独立订单、保留外部基线的Hybrid共享会话")
    if identity["account"] != config["account"] or set(identity["strategies"]) != set(declarations):
        raise ValueError("Hybrid配置与账户账本的身份、限额或策略集合不一致")
    if set(signals) != set(declarations):
        raise ValueError("须保留全部Hybrid策略信号文件，包含当前零仓策略")
    for sid, saved in signals.items():
        declared = identity["strategies"][sid]
        if declared["kind"] != declarations[sid]["kind"] or declared["argv"] != declarations[sid]["argv"]:
            raise ValueError("Hybrid参数与账本不一致，不能在合并时隐式改动")
        if saved.payload.get("identity") != declared or "signal" not in saved.payload:
            raise ValueError(f"Hybrid信号文件身份不符或缺少批次状态: {sid}")

    old = incoming["component_states"]["session_identity"]["identity"]
    if "strategies" in old:
        rows = old["strategies"]
        if len(rows) != 1 or next(iter(rows.values()))["kind"] != "option_vega":
            raise ValueError("原08检查点须唯一声明一个option_vega策略")
        sid = next(iter(rows))
        option_day = old["trading_day"]
        option_format = "shared_account"
    else:
        if old.get("mode") != "simnow":
            raise ValueError("原08检查点须为SimNow成交归属，不能导入模型或Recording")
        sid, option_day = old["strategy_id"], old["td_day"]
        option_format = "native_option"
    if sid in declarations:
        raise ValueError("08策略ID与Hybrid重复")

    def owned(payload, allowed):
        rows = payload["positions"]["strategy_positions"]
        result = []
        for row in rows:
            quantity = Decimal(row["quantity"])
            if not quantity.is_finite():
                raise ValueError("归属数量不是有限数值")
            if row["strategy_id"] not in allowed:
                raise ValueError("检查点包含未声明的策略归属，不能丢弃")
            if quantity:
                result.append(dict(row))
        return result

    base_owned, option_owned = owned(base, declarations), owned(incoming, {sid})
    # 按策略归属累计绝对数量；不同策略同合约、相反方向均不能净掉预算。
    quantity = sum((abs(Decimal(row["quantity"])) for row in base_owned + option_owned), Decimal(0))
    planned_limits = dict(config["account"])
    if strategy_limits_only:
        planned_limits.update(max_gross_quantity=None, max_gross_notional=None)
    limit = (None if planned_limits["max_gross_quantity"] is None else
             Decimal(str(planned_limits["max_gross_quantity"])))
    blockers = []
    if limit is not None and quantity > limit:
        blockers.append("OWNED_QUANTITY_EXCEEDS_UNCHANGED_ACCOUNT_LIMIT")
    shared = base["component_states"]["shared_account"]
    if shared.get("failed") or shared.get("pending"):
        blockers.append("HYBRID_EXECUTION_REQUIRES_RECONCILIATION")
    incoming_shared = incoming["component_states"].get("shared_account", {})
    if incoming_shared.get("failed") or incoming_shared.get("pending"):
        blockers.append("OPTION_EXECUTION_REQUIRES_RECONCILIATION")
    frozen = any(Decimal(row["quantity"]) != 0 for payload in (base, incoming)
                 for row in payload["positions"].get("working_quantities", []))
    if frozen:
        blockers.append("WORKING_ORDERS_PRESENT")
    external = base["component_states"].get("external_positions", {})
    return {
        "scope": "readonly_join_inspection", "can_start_orders": False,
        "state_modified": False, "broker_current_verified": False,
        "blockers": blockers, "source_account_limits": dict(config["account"]),
        "account_limits": planned_limits, "risk_scope": "strategy" if limit is None else "account_and_strategy",
        "owned_gross_quantity": str(quantity), "remaining_quantity": None if limit is None else str(limit-quantity),
        "hybrid": {"generation": account.generation, "trading_day": identity["trading_day"],
            "positions": base_owned, "signal_generations": {sid: row.generation for sid, row in signals.items()},
            "signal_revisions": {sid: row.payload.get("revision") for sid, row in signals.items()},
            "account_source_revisions": {sid: value["revision"] for sid, value in shared["sources"].items()},
            "order_budget": shared["shared"].get("order_budget"),
            "suspended": shared["shared"].get("suspended", []),
            "draining": shared["shared"].get("draining", [])},
        "option": {"generation": option.generation, "trading_day": option_day,
            "format": option_format, "strategy_id": sid, "positions": option_owned,
            "identity_fields": sorted(old), "state_components": sorted(incoming["component_states"]),
            "failed": incoming_shared.get("failed"), "pending": incoming_shared.get("pending"),
            "draining": incoming_shared.get("shared", {}).get("draining", []),
            "account_source_revision": incoming_shared.get("sources", {}).get(sid, {}).get("revision"),
            "embedded_signal_present": "vega_signal" in incoming["component_states"]},
        "option_in_saved_external_baseline": {row["target_key"]: external.get("gross", {}).get(row["target_key"])
            for row in option_owned},
        "note": "仅检查已保存状态；运行中的generation会继续变化，正式迁移须正常停机后重新核验柜台及全部源文件。",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hybrid-config", type=Path, required=True)
    parser.add_argument("--option-state", type=Path, required=True)
    parser.add_argument("--option-signal", type=Path,
        help="旧08共享信号文件；省略检查原账户文件名追加.vega-signal.json")
    parser.add_argument("--strategy-limits-only", action="store_true",
        help="预检取消联合手数及名义金额限额的计划；保留策略限额和TD报单额度，不写原配置")
    args = parser.parse_args(argv)
    # 导入公共带校验和读取器；不导入或创建任何TD连接、执行宿主或策略算法。
    from bomber.framework.trader.persistence import JsonStateStore, StatePersistenceError
    config = read_config(args.hybrid_config)
    def load(path):
        saved = JsonStateStore(path).load()
        if saved is None:
            raise ValueError(f"原检查点不存在: {path}")
        return saved
    try:
        report = inspect_join(config, load(config["account_state_file"]), load(args.option_state),
            {row["strategy_id"]: load(row["state_file"]) for row in config["strategies"]},
            strategy_limits_only=args.strategy_limits_only)
        signal_path = args.option_signal or args.option_state.with_name(args.option_state.name + ".vega-signal.json")
        signal = JsonStateStore(signal_path).load()
        if args.option_signal and signal is None:
            raise ValueError(f"指定的原08信号文件不存在: {signal_path}")
        report["option_signal"] = {"path": str(signal_path), "exists": signal is not None,
            "generation": signal.generation if signal else None,
            "revision": signal.payload.get("revision") if signal else None,
            "signal_fields": sorted(signal.payload.get("signal", {})) if signal else []}
    except (ValueError, KeyError, OSError, StatePersistenceError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
