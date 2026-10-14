"""08 SimNow 迁移前只读核对：原检查点、柜台总仓/今昨仓、资金及订单。"""
from argparse import ArgumentParser
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[3]
CLIENT = "option-vega-live"
BUCKETS = ("long_today", "long_yesterday", "short_today", "short_yesterday")


def quantity(value):
    value = Decimal(str(value))
    if not value.is_finite() or value != value.to_integral_value():
        raise ValueError("持仓须为有限整数")
    return value


def is_managed(key):
    symbol, venue = str(key).rsplit(".", 1)
    return venue == "CFFEX" and symbol.startswith(("MO", "IM"))


def checkpoint_summary(saved):
    """只提取迁移所需事实；不恢复组件、重写身份或制造策略归属。"""
    from bomber.framework.trader.execution.order_state import OrderLifecycleStatus
    payload = saved.payload
    components = payload["component_states"]
    identity = components["session_identity"]["identity"]
    if identity["strategy_id"] != CLIENT or identity["mode"] != "simnow":
        raise ValueError("原状态不是08 SimNow会话")
    ledgers, drivers = payload["ctp_ledgers"], payload["ctp_drivers"]
    if set(ledgers) != {CLIENT} or set(drivers) != {CLIENT}:
        raise ValueError("原状态不是08单客户端CTP账本")
    positions = payload["positions"]
    if any(quantity(row["quantity"]) for row in positions["working_quantities"]):
        raise ValueError("原状态仍有在途数量")
    if any(not OrderLifecycleStatus(row["status"]).is_terminal
           for rows in payload["orders"].values() for row in rows) or drivers[CLIENT]["orders"]:
        raise ValueError("原状态仍有活动订单；不能自动迁移")
    gross, buckets = {}, {}
    for row in ledgers[CLIENT]["positions"]:
        key = row["instrument_id"]
        values = tuple(quantity(row[name]) for name in BUCKETS)
        if any(value < 0 for value in values) or not is_managed(key) or key in buckets:
            raise ValueError("原CTP持仓分桶或合约无效")
        buckets[key] = values
        if any(values):
            gross[key] = (sum(values[:2]), sum(values[2:]))
    owned = {}
    for row in positions["strategy_positions"]:
        value = quantity(row["quantity"])
        if not value:
            continue
        key = row["target_key"]
        key = key if "." in key else key + ".CFFEX"
        if row["strategy_id"] != CLIENT or not is_managed(key) or key in owned:
            raise ValueError("原状态存在其他策略或重复持仓归属")
        owned[key] = value
    account = {row["key"]["instrument_id"]: quantity(row["quantity"])
        for row in positions["account_positions"] if row["key"]["client_id"] == CLIENT
        and is_managed(row["key"]["instrument_id"]) and quantity(row["quantity"])}
    net = {key: long-short for key, (long, short) in gross.items() if long != short}
    if owned != net or account != net:
        raise ValueError("原状态策略归属、账户净仓与CTP账本不一致")
    if any(key not in owned for key in gross):
        raise ValueError("原状态存在净仓为零的双向总仓，不能凭净仓推断归属")
    return {"generation": saved.generation, "saved_at_ns": saved.saved_at_ns,
        "identity": {key: identity[key] for key in ("valuation_day", "td_day", "mode", "environment",
            "config", "sources", "labels", "policy")},
        "ledger_trading_day": ledgers[CLIENT]["trading_day"], "saved_managed_gross": gross,
        "saved_buckets": {key: value for key, value in buckets.items() if any(value)},
        "owned_positions": owned, "signal": components["vega_signal"]}


def inspect_counterparty(driver, expected_day, saved, report):
    from bomber.framework.trader.runtime.ctp_account import position_buckets
    transport = driver.transport
    checkpoint = saved.payload["ctp_drivers"][CLIENT]
    report["account_identity_matches"] = all(str(checkpoint[name]) == str(getattr(transport, name))
        for name in ("account_id", "broker_id", "investor_id"))
    if str(transport.broker_id) != "9999" or not report["account_identity_matches"]:
        raise ValueError("须使用原状态的同一SimNow账户；未连接")
    try:
        driver.start(lambda event: None)
        report["observed_td_trading_day"] = driver.trading_day
        # 只查询，不调用submit_order/cancel_order，不安装策略或执行客户端。
        account = driver.reconcile_account_state()
        orders = driver.reconcile_active_orders()
        gross = {str(key): tuple(map(quantity, value)) for key, value in transport.query_gross_positions().items()
            if any(value)}
        managed = {key: value for key, value in gross.items() if is_managed(key)}
        details = [row for row in transport.query_position_details()
            if is_managed(str(row["InstrumentID"])+"."+str(row["ExchangeID"]))]
        buckets = position_buckets(details, managed, require_cost=True)
        report.update(active_orders=len(orders.orders), managed_gross=managed,
            external_gross={key: value for key, value in gross.items() if not is_managed(key)},
            bucket_order=BUCKETS, authoritative_buckets={key: {"quantities": value.quantities,
                "position_costs": value.costs} for key, value in buckets.items()},
            cny_available_positive="CNY" in account.balances and account.balances["CNY"].available > 0)
        bucket_gross = {key: (sum(value.quantities[:2]), sum(value.quantities[2:])) for key, value in buckets.items()}
        report["gross_matches_original_state"] = managed == report["checkpoint"]["saved_managed_gross"]
        if driver.trading_day != expected_day:
            raise ValueError("当前TD交易日与声明不一致")
        if report["active_orders"] or not report["cny_available_positive"]:
            raise ValueError("柜台存在活动订单或缺可用CNY资金")
        if bucket_gross != managed:
            raise ValueError("权威今昨仓明细与总仓不一致")
        if not report["gross_matches_original_state"]:
            raise ValueError("当前MO/IM总仓与原状态不一致，需核对其他交易或持仓变化")
    finally:
        try:
            driver.stop()
        except Exception as error:
            report["cleanup_errors"].append(str(error))
            raise


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--expected-trading-day", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--report-dir", type=Path, default=ROOT/"tests/results/option-vega-simnow-preflight")
    args = parser.parse_args(argv)
    if not args.connect:
        parser.error("须显式--connect；本探针只读，不报单、不撤单、不改状态")
    try:
        if datetime.strptime(args.expected_trading_day, "%Y%m%d").strftime("%Y%m%d") != args.expected_trading_day:
            raise ValueError("交易日须为YYYYMMDD")
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("timeout须为有限正数")
    except ValueError as error:
        parser.error(str(error))
    path = args.report_dir/f"sample-{time.time_ns()}"
    path.mkdir(parents=True, exist_ok=False)
    report = {"scope": "readonly_migration_preflight", "status": "failed", "failure": None,
        "cleanup_errors": [], "state_file": str(args.state_file.resolve()),
        "expected_trading_day": args.expected_trading_day, "orders_enabled": False,
        "can_start_orders": False, "state_modified": False}
    before = None
    try:
        from bomber.framework.trader.persistence import JsonStateStore
        from bomber.framework.trader.runtime.live.local import strategy_state_lock
        from bomber.framework.trader.runtime.live.channels.ctp import account_lock
        from scripts.integration.ctp.td_readonly import build_readonly_driver
        with strategy_state_lock(args.state_file):
            before = hashlib.sha256(args.state_file.read_bytes()).hexdigest()
            saved = JsonStateStore(args.state_file).load()
            if saved is None:
                raise ValueError("原状态不存在")
            report["state_sha256"] = before
            report["checkpoint"] = checkpoint_summary(saved)
            driver = build_readonly_driver(timeout=args.timeout)
            with account_lock(driver.transport.account_id):
                inspect_counterparty(driver, args.expected_trading_day, saved, report)
            report["migration_required"] = report["checkpoint"]["identity"]["td_day"] != args.expected_trading_day or \
                report["checkpoint"]["identity"]["sources"].get("market", {}).get("channel") != "dolphindb"
            report["next_step"] = "验证原归属后准备独立迁移文件；保持交易闭闸，不直接改原身份或新建空仓"
            report["status"] = "completed"
    except Exception as error:
        report["failure"] = str(error)
    finally:
        if before is not None:
            try:
                report["state_modified"] = hashlib.sha256(args.state_file.read_bytes()).hexdigest() != before
                if report["state_modified"]:
                    report.update(status="failed", failure="预检期间原状态发生变化；本探针未写原文件")
            except OSError as error:
                report.update(status="failed", failure=str(error), state_modified=None)
        (path/"summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)+"\n")
        print("OPTION_VEGA_SIMNOW_PREFLIGHT:", json.dumps(report, ensure_ascii=False, default=str), flush=True)
        print("报告:", path/"summary.json", flush=True)
    return int(report["status"] != "completed")


if __name__ == "__main__":
    raise SystemExit(main())
