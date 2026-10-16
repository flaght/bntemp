"""生成新三策略会话，不修改已有状态；日期必须由操作者只读核实后声明。"""

import argparse
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import re


CFFEX_PROFILES = {"im-if": ("IM", "IF"), "cffex-four": ("IM", "IH", "IC", "IF")}


def profile_cross_products(profile, products):
    expected = CFFEX_PROFILES.get(profile)
    if expected is not None and products is not None and products != expected:
        raise ValueError(f"{profile}横截面须为{','.join(expected)}，不能静默忽略cross-products")
    return expected or products or ("CU", "AL")


def parse_cross_products(value):
    products = tuple(item.strip().upper() for item in value.split(","))
    if (len(products) < 2 or len(set(products)) != len(products)
            or any(not re.fullmatch(r"[A-Z]+", item) for item in products)
            or len("cross-" + "-".join(products) + "-three") > 64):
        raise argparse.ArgumentTypeError("横截面须声明至少两个不同的字母品种，策略ID长度不超过64")
    return products


def observed_budget(contracts, specs, quotes, products, *, product="CU", roles=("secondary", "far"), strategy_contracts=None):
    """每策略每腿最多1手的保守归属上界；重复合约按各策略分别计入。"""
    selected = strategy_contracts or {kind: contracts for kind in ("main_ema", "cross_section", "calendar_spread")}
    legs = [("ema", selected["main_ema"][product]["main"]),
        *(("cross", selected["cross_section"][p]["main"]) for p in products),
        *(("calendar", selected["calendar_spread"][product][role]) for role in roles)]
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


def apply_cffex_profile(config, observed, output, *, profile, products):
    """本次固定联合验收；沿用三个Recording已验证的指标和历史参数。"""
    import os
    root = Path(__file__).resolve().parents[3]
    four = profile == "cffex-four"
    config["account"].update(client_id="three-cffex-four-independent" if four else "three-im-if-independent",
        max_session_orders=200)
    common = ["--calendar-id", "china.cffex", "--trading-hours-database", "dfs://bomber_static",
        "--trading-hours-table", "fut_trading_session", "--no-session-overrides",
        "--history-stage", "recovery", "--history-missing-policy", "fail"]
    definitions = [("ema-im-four" if four else "ema-im-three", "01_main_ema", "history_im_raw.json", 240),
        ("cross-im-ih-ic-if-four" if four else "cross-im-if-three", "04_cross_section", "history_cffex.json", 21),
        ("calendar-im-four" if four else "calendar-im-three", "03_calendar_spread", "history_im.json", 120)]
    for row, (sid, demo, history, minutes) in zip(config["strategies"], definitions):
        contracts = observed["strategy_contracts"][row["kind"]]
        row["strategy_id"] = sid
        argv = row["argv"]
        if "--product" in argv:
            argv[argv.index("--product") + 1] = "IM"
        argv[argv.index("--max-notional") + 1] = "3000000"
        if row["kind"] == "main_ema":
            argv += ["--expected-instrument", contracts["IM"]["main"]]
        elif row["kind"] == "calendar_spread":
            argv[argv.index("--leg1-role") + 1], argv[argv.index("--leg2-role") + 1] = "main", "secondary"
            argv[argv.index("--lookback") + 1] = "120"
            argv[argv.index("--rebalance-interval") + 1] = "5"
            argv[argv.index("--entry-z") + 1], argv[argv.index("--exit-z") + 1] = "2", "0.5"
            if contracts["IM"]["main"] == contracts["IM"]["secondary"]:
                raise ValueError("跨期必须是两个不同的IM真实合约")
            for role in ("main", "secondary"):
                argv += ["--expected-instrument", f"{role}={contracts['IM'][role]}"]
        else:
            argv[argv.index("--lookback") + 1] = "20"
            argv[argv.index("--rebalance-interval") + 1] = "5"
            side_notional = Decimal(3000000)
            if four:
                budget = observed_budget(observed["contracts"], observed["instrument_specs"], observed["quotes"], products,
                    product="IM", roles=("main", "secondary"), strategy_contracts=observed["strategy_contracts"])
                # 原策略每侧等分预算；四品种各选两名，须覆盖最贵入选合约的一手。
                largest = max(Decimal(leg["notional"]) for leg in budget["legs"] if leg["strategy"] == "cross")
                side_notional = max(side_notional,
                    (largest * 2 / Decimal(100000)).to_integral_value(rounding=ROUND_CEILING) * Decimal(100000))
            argv[argv.index("--target-notional") + 1] = str(side_notional)
            for product in products:
                argv += ["--expected-instrument", f"{product}={contracts[product]['main']}"]
        argv += [*common, "--history-db", "dolphindb", "--history-minutes", str(minutes),
            "--history-config", os.path.relpath(root / "demos" / demo / history, output.resolve().parent)]


def preserve_cffex_identities(config, previous):
    """准备同日扩品种迁移配置；新文件承载原身份，原参数和账本保持。"""
    if (previous["trading_day"] != config["trading_day"] or previous["environment"] != "realtime"
            or previous.get("execution_mode") != "independent"
            or previous.get("position_scope") != "strategy_instruments"
            or previous.get("existing_position_policy", "require_flat") != "require_flat"):
        raise ValueError("股指扩品种须原同日实时独立会话及strict策略合约范围")
    rows = {row["kind"]: row for row in previous["strategies"]}
    if set(rows) != {"main_ema", "cross_section", "calendar_spread"} or len(previous["strategies"]) != 3:
        raise ValueError("股指扩品种须原01/03/04三策略")
    old_cross = rows["cross_section"]["argv"]
    if old_cross[old_cross.index("--products") + 1] != "IM,IF":
        raise ValueError("本次股指扩品种仅支持IM/IF到IM/IH/IC/IF")
    budget = config["account"]
    config["account"] = {**previous["account"], **{key: str(max(Decimal(str(budget[key])),
        Decimal(str(previous["account"][key])))) for key in ("max_gross_quantity", "max_gross_notional")}}
    for row in config["strategies"]:
        old = rows[row["kind"]]
        row["strategy_id"] = old["strategy_id"]
        if old.get("stop_after_seconds") is not None:
            row["stop_after_seconds"] = old["stop_after_seconds"]
        if row["kind"] != "cross_section":
            row["argv"] = list(old["argv"])
            continue
        generated = row["argv"]
        # 仅增加品种、相应预期合约和必要的等权侧预算，其他算法参数保留。
        argv, index = [], 0
        while index < len(old_cross):
            if old_cross[index] == "--expected-instrument":
                index += 2
            else:
                argv.append(old_cross[index]); index += 1
        argv[argv.index("--products") + 1] = generated[generated.index("--products") + 1]
        argv[argv.index("--target-notional") + 1] = str(max(
            Decimal(argv[argv.index("--target-notional") + 1]),
            Decimal(generated[generated.index("--target-notional") + 1])))
        for index, arg in enumerate(generated):
            if arg == "--expected-instrument":
                argv += [arg, generated[index + 1]]
        row["argv"] = argv


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment", choices=("replay", "realtime"), required=True)
    parser.add_argument("--execution-mode", choices=("netted", "independent"), required=True)
    parser.add_argument("--profile", choices=("commodity", *CFFEX_PROFILES), default="commodity")
    parser.add_argument("--from-config", type=Path, help="cffex-four同日扩品种，保留原策略/账户身份，另写迁移目标配置")
    parser.add_argument("--trading-day")
    parser.add_argument("--source-day")
    parser.add_argument("--observations", type=Path, help="本次discover只读摘要；不得与手工日期混用")
    parser.add_argument("--replay-md-trading-day")
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--cross-products", type=parse_cross_products,
                        help="横截面完整品种列表，如CU,AL,AG,AU；旧两品种配置不自动改写")
    parser.add_argument("--existing-position-policy", choices=("require_flat", "preserve_external"), default="require_flat")
    parser.add_argument("--ctp-future-buffer-ms", type=int, default=0)
    parser.add_argument("--use-observed-budget", action="store_true", help="按本次只读盘口的逐腿上界计算账户限额")
    args = parser.parse_args(argv)
    if args.from_config is not None and args.profile != "cffex-four":
        parser.error("from-config仅用于cffex-four同日迁移准备")
    try:
        args.cross_products = profile_cross_products(args.profile, args.cross_products)
    except ValueError as error:
        parser.error(str(error))
    product, roles = ("IM", ("main", "secondary")) if args.profile in CFFEX_PROFILES else ("CU", ("secondary", "far"))
    if args.profile in CFFEX_PROFILES:
        if (args.environment != "realtime" or args.execution_mode != "independent"
                or args.existing_position_policy != "require_flat"
                or not args.observations or not args.use_observed_budget):
            parser.error("股指联合配置须realtime、independent、管理合约空仓及本次同profile实际盘口观察预算")
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
        if args.profile != observed.get("profile", "commodity"):
            parser.error("观察摘要的联合profile与配置不符")
        if args.profile in CFFEX_PROFILES:
            expected = {"main_ema": {"IM": ("main",)}, "cross_section": {p: ("main",) for p in args.cross_products},
                "calendar_spread": {"IM": ("main", "secondary")}}
            try:
                for kind, products in expected.items():
                    for p, role_names in products.items():
                        for role in role_names:
                            instrument = observed["strategy_contracts"][kind][p][role]
                            if (not re.fullmatch(p + r"\d{4}\.CFFEX", instrument)
                                    or instrument not in observed["instrument_specs"] or instrument not in observed["quotes"]):
                                raise ValueError("逐策略合约或盘口不完整")
            except (KeyError, TypeError, ValueError):
                parser.error("须重新运行同profile discover：未核验完整逐策略因子合约及其盘口")
        dates = observed["session_arguments"]
        if args.cross_products != ("CU", "AL"):
            contracts = observed.get("contracts", {})
            required = {p: {"main"} for p in args.cross_products}
            required.setdefault(product, set()).update(("main", *roles))
            if any(not roles <= set(contracts.get(product, {})) for product, roles in required.items()):
                parser.error("只读观察未覆盖横截面品种及跨期合约；须用同样的profile/cross-products重新观察")
        args.trading_day, args.source_day = dates["trading_day"], dates["source_day"]
        args.replay_md_trading_day = dates["replay_md_trading_day"]
    if not args.trading_day or not args.source_day:
        parser.error("须提供已核实的TD/资料日或本次只读观察摘要")
    for day in (args.trading_day, args.source_day, args.replay_md_trading_day):
        if day and datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d") != day:
            parser.error("日期须为YYYYMMDD")
    if args.source_day >= args.trading_day or bool(args.replay_md_trading_day) != (args.environment == "replay"):
        parser.error("来源日须早于TD日；replay须且仅replay可声明原始MD日")
    if args.output.exists():
        parser.error(f"目标配置已存在，禁止覆盖: {args.output}")
    if not 0 < args.seconds <= 3600:
        parser.error("运行时长须在(0,3600]秒")
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
        budget = observed_budget(observed["contracts"], observed["instrument_specs"], observed["quotes"], args.cross_products,
            product=product, roles=roles, strategy_contracts=observed.get("strategy_contracts"))
        config["account"].update(max_gross_quantity=str(max(5, budget["quantity"])),
            max_gross_notional=str(max(Decimal(3000000), Decimal(budget["rounded_notional_limit"]))))
    if args.profile in CFFEX_PROFILES:
        apply_cffex_profile(config, observed, args.output, profile=args.profile, products=args.cross_products)
    if args.from_config is not None:
        from .config import read_config
        try:
            preserve_cffex_identities(config, read_config(args.from_config))
        except (ValueError, KeyError) as error:
            parser.error(str(error))
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
