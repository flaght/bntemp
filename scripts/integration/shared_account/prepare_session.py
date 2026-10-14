"""生成新三策略会话，不修改已有状态；日期必须由操作者只读核实后声明。"""

import argparse
from datetime import datetime
import json
from pathlib import Path


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
    args = parser.parse_args(argv)
    if args.observations:
        if args.trading_day or args.source_day or args.replay_md_trading_day:
            parser.error("观察摘要不得与手工日期混用")
        observed = json.loads(args.observations.read_text())
        if (observed.get("status") != "observed" or observed.get("orders_submitted") != 0
                or observed.get("verification") != "readonly_dates_and_contracts"
                or observed.get("environment") != args.environment):
            parser.error("只读摘要未通过或环境不一致")
        dates = observed["session_arguments"]
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
            {"strategy_id": "cross-cu-al-three", "kind": "cross_section",
             "state_file": f"results/three-{suffix}/cross.json",
             "argv": ["--products", "CU,AL", *reference, *factors, "--lookback", "3",
                 "--rebalance-interval", "1", "--group-fraction", "0.5", "--target-notional", "1000000",
                 "--target-quantity-cap", "1", "--max-quantity", "1", "--max-notional", "1000000"]},
            {"strategy_id": "calendar-cu-three", "kind": "calendar_spread",
             "state_file": f"results/three-{suffix}/calendar.json",
             "argv": ["--product", "CU", *reference, "--leg1-role", "secondary", "--leg2-role", "far",
                 "--quantity", "1", "--lookback", "3", "--rebalance-interval", "1",
                 "--entry-z", "1", "--exit-z", "0.25", "--max-notional", "1000000"]}]}
    if args.replay_md_trading_day:
        config["replay_md_trading_day"] = args.replay_md_trading_day
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(config, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(f"新配置已生成：{args.output}；未连接、未发单；账户归属总预算5手/300万元，实际价格仍须预检")


if __name__ == "__main__":
    main()
