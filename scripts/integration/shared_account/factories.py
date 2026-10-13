"""应用层策略工厂；公共框架不反向导入demo。复用原策略和原Runner。"""

from importlib import import_module


ENTRIES = {"main_ema": "demos.01_main_ema.run_live", "cross_section": "demos.04_cross_section.run_live"}


def strategy_arguments(row, config, *, resume):
    entry = import_module(ENTRIES[row["kind"]])
    argv = list(row["argv"]) + ["--connect", "--mode", "simnow", "--enable-orders", "--confirm-simnow",
        "--state-file", row["state_file"], "--expected-trading-day", config["trading_day"],
        "--seconds", str(config["seconds"]), "--simnow-environment", config["environment"]]
    if config.get("replay_md_trading_day"):
        argv += ["--replay-md-trading-day", config["replay_md_trading_day"]]
    if resume:
        argv += ["--resume"]
    args = entry.parse_args(argv)
    if getattr(args, "run_forever", False) or getattr(args, "history_minutes", 0):
        raise ValueError("首期共享入口采用各策略实时重新预热")
    return entry, args


def instrument_specs(kind, references):
    if kind == "main_ema":
        return {references.instrument_id: references.spec}
    return {references.instrument_ids[p]: spec for p, spec in references.specs.items()}


def assemble_strategy_session(entry, args, inputs, *, service, driver, strategy_id):
    return entry.assemble(args, inputs.references, driver, inputs.upstream,
                          shared_service=service, strategy_id=strategy_id)
