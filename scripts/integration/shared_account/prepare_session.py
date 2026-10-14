"""生成新三策略会话，不修改已有状态；日期必须由操作者只读核实后声明。"""

import argparse
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import re


def parse_cross_products(value):
    products = tuple(item.strip().upper() for item in value.split(","))
    if (len(products) < 2 or len(set(products)) != len(products)
            or any(not re.fullmatch(r"[A-Z]+", item) for item in products)
            or len("cross-" + "-".join(products) + "-three") > 64):
        raise argparse.ArgumentTypeError("横截面须声明至少两个不同的字母品种，策略ID长度不超过64")
    return products


def observed_budget(contracts, specs, quotes, products):
    """每策略每腿最多1手的保守归属上界；CU主力在EMA和截面各计一次。"""
    legs = [("ema", contracts["CU"]["main"]),
        *(("cross", contracts[p]["main"]) for p in products),
        ("calendar", contracts["CU"]["secondary"]), ("calendar", contracts["CU"]["far"])]
    rows, total = [], Decimal(0)
    for strategy, instrument in legs:
        quote, spec = quotes[instrument], specs[instrument]
        bid, ask, tick, multiplier = (Decimal(str(v)) for v in
            (quote["bid"], quote["ask"], spec["tick"], spec["multiplier"]))
        if any(not q.is_finite() or q <= 0 for q in (bid, ask, tick, multiplier)) or ask < bid:
            raise ValueError("预算观察需要有效买卖盘口、跳价和乘数")
        price = ((ask + tick) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        notional = price * multiplier
        rows.append(dict(strategy=strategy, instrument=instrument, quantity=1,
            reference_price=str(price), multiplier=str(multiplier), notional=str(notional)))
        total += notional
    cap = (total / Decimal(100000)).to_integral_value(rounding=ROUND_CEILING) * Decimal(100000)
    return {"quantity": len(legs), "notional": str(total), "rounded_notional_limit": str(cap),
        "price_basis": "observed_ask_plus_one_tick", "legs": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment", choices=("replay", "realtime"), required=True)
    parser.add_argument("--execution-mode", choices=("netted", "independent"), required=True)
    parser.add_argument("--trading-day")
    parser.add_argument("--source-day")
    parser.add_argument("--observations", type=Path, help="本次discover只读摘要；不得与手工日期混用")
    parser.add_argument("--replay-md-trading-day")
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--cross-products", type=parse_cross_products, default="CU,AL",
                        help="横截面完整品种列表，如CU,AL,AG,AU；旧两品种配置不自动改写")
    parser.add_argument("--existing-position-policy", choices=("require_flat", "preserve_external"), default="require_flat")
    parser.add_argument("--ctp-future-buffer-ms", type=int, default=0)
    parser.add_argument("--use-observed-budget", action="store_true", help="按本次只读盘口的逐腿上界计算账户限额")
    args = parser.parse_args(argv)
    if (not 0 <= args.ctp_future_buffer_ms <= 100 or (args.ctp_future_buffer_ms and args.environment != "realtime")
            or (args.existing_position_policy == "preserve_external" and args.execution_mode != "independent")
            or (args.use_observed_budget and not args.observations)):
        parser.error("未来缓冲为实时0至100毫秒；外部基线须独立模式；预算计算须本次观察摘要")
    if args.observations:
        if args.trading_day or args.source_day or args.replay_md_trading_day:
            parser.error("观察摘要不得与手工日期混用")
        observed = json.loads(args.observations.read_text())
        if (observed.get("status") != "observed" or observed.get("orders_submitted") != 0
                or observed.get("verification") != "readonly_dates_and_contracts"
                or observed.get("environment") != args.environment):
            parser.error("只读摘要未通过或环境不一致")
        dates = observed["session_arguments"]
        if args.cross_products != ("CU", "AL"):
            contracts = observed.get("contracts", {})
            required = {p: {"main"} for p in args.cross_products}
            required["CU"] = {"main", "secondary", "far"}
            if any(not roles <= set(contracts.get(product, {})) for product, roles in required.items()):
                parser.error("只读观察未覆盖新横截面品种及CU跨期合约；须用同样的cross-products重新观察")
        args.trading_day, args.source_day = dates["trading_day"], dates["source_day"]
        args.replay_md_trading_day = dates["replay_md_trading_day"]
    if not args.trading_day or not args.source_day:
        parser.error("须提供已核实的TD/资料日或本次只读观察摘要")
    for day in (args.trading_day, args.source_day, args.replay_md_trading_day):
        if day and datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d") != day:
            parser.error("日期须为YYYYMMDD")
    if args.source_day >= args.trading_day or bool(args.replay_md_trading_day) != (args.environment == "replay"):
        parser.error("来源日须早于TD日；replay须且仅replay可声明原始MD日")
    if args.output.exists() or not 0 < args.seconds <= 3600:
        parser.error("不得覆盖已有配置；运行时长须在(0,3600]秒")
    suffix = f"{args.environment}-{args.execution_mode}-{args.trading_day}"
    cross_id = "cross-" + "-".join(p.lower() for p in args.cross_products) + "-three"
    if args.cross_products != ("CU", "AL"):
        suffix += "-" + "-".join(p.lower() for p in args.cross_products)
    reference = ["--expected-source-day", args.source_day]
    factors = ["--factor-date-basis", "source", "--factor-availability",
               "source-day-end" if args.environment == "replay" else "observed-on-read"]
    config = {"version": 1, "trading_day": args.trading_day, "environment": args.environment,
        "position_scope": "strategy_instruments", "execution_mode": args.execution_mode,
        "seconds": args.seconds, "account_state_file": f"results/three-{suffix}/account.json",
        "report_dir": f"results/three-{suffix}",
        "account": {"client_id": f"three-{args.environment}-{args.execution_mode}",
            "max_gross_quantity": "5", "max_gross_notional": "3000000", "max_session_orders": 20,
            "limit_offset_ticks": 1},
        "strategies": [
            {"strategy_id": "ema-cu-three", "kind": "main_ema",
             "state_file": f"results/three-{suffix}/ema.json",
             "argv": ["--product", "CU", *reference, *factors, "--fast", "3", "--slow", "5",
                      "--quantity", "1", "--max-notional", "1000000"]},
            {"strategy_id": cross_id, "kind": "cross_section",
             "state_file": f"results/three-{suffix}/cross.json",
             "argv": ["--products", ",".join(args.cross_products), *reference, *factors, "--lookback", "3",
                 "--rebalance-interval", "1", "--group-fraction", "0.5", "--target-notional", "1000000",
                 "--target-quantity-cap", "1", "--max-quantity", "1", "--max-notional", "1000000"]},
            {"strategy_id": "calendar-cu-three", "kind": "calendar_spread",
             "state_file": f"results/three-{suffix}/calendar.json",
             "argv": ["--product", "CU", *reference, "--leg1-role", "secondary", "--leg2-role", "far",
                 "--quantity", "1", "--lookback", "3", "--rebalance-interval", "1",
                 "--entry-z", "1", "--exit-z", "0.25", "--max-notional", "1000000"]}]}
    if args.existing_position_policy != "require_flat":
        config["existing_position_policy"] = args.existing_position_policy
    if args.ctp_future_buffer_ms:
        for row in config["strategies"]:
            row["argv"] += ["--ctp-future-buffer-ms", str(args.ctp_future_buffer_ms)]
    if args.use_observed_budget:
        budget = observed_budget(observed["contracts"], observed["instrument_specs"], observed["quotes"], args.cross_products)
        config["account"].update(max_gross_quantity=str(max(5, budget["quantity"])),
            max_gross_notional=str(max(Decimal(3000000), Decimal(budget["rounded_notional_limit"]))))
    if args.replay_md_trading_day:
        config["replay_md_trading_day"] = args.replay_md_trading_day
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(config, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(f"新配置已生成：{args.output}；未连接、未发单；账户归属总预算"
        f"{config['account']['max_gross_quantity']}手/{config['account']['max_gross_notional']}元，实际价格仍须预检")


if __name__ == "__main__":
    main()
