"""应用层策略工厂；公共框架不反向导入demo。复用原策略和原Runner。"""

from importlib import import_module
from pathlib import Path


ENTRIES = {"main_ema": "demos.01_main_ema.run_live", "cross_section": "demos.04_cross_section.run_live",
           "calendar_spread": "demos.03_calendar_spread.run_live", "option_vega": "demos.08_option_vega.run_live"}


def strategy_arguments(row, config, *, resume):
    entry = import_module(ENTRIES[row["kind"]])
    argv = list(row["argv"]) + ["--connect", "--mode", "simnow", "--enable-orders", "--confirm-simnow",
        "--state-file", row["state_file"], "--expected-trading-day", config["trading_day"],
        "--seconds", str(config["seconds"]), "--simnow-environment", config["environment"]]
    if config.get("replay_md_trading_day"):
        argv += ["--replay-md-trading-day", config["replay_md_trading_day"]]
    if resume and (row["kind"] != "option_vega" or Path(row["state_file"]).is_file()):
        argv += ["--resume"]
    args = (entry.parse_args(argv, shared_execution=True) if row["kind"] in {"main_ema", "calendar_spread", "cross_section"}
            else entry.parse_args(argv))
    if "ctp_instrument_max_age_seconds" in config:
        from .config import ctp_instrument_max_age_seconds
        from bomber.framework.market.sources import source_config
        if row["kind"] not in {"main_ema", "calendar_spread", "cross_section"} or source_config(args).source != "ctp":
            raise ValueError("会话CTP逐合约时效仅用于01/03/04的CTP行情")
        # 运行监督参数独立于原argv身份；同账本恢复不改旧策略信号/限额声明。
        args.instrument_max_age_seconds = ctp_instrument_max_age_seconds(config)
    history_enabled = any(getattr(args, name, None) for name in
        ("history_minutes", "history_config", "history_file"))
    if "history_missing_policy" in config:
        if config["history_missing_policy"] not in ("fail", "allow"):
            raise ValueError("history_missing_policy须为fail或allow")
        if row["kind"] not in {"main_ema", "calendar_spread", "cross_section"}:
            raise ValueError("会话历史缺失政策仅支持01/03/04")
        args.history_missing_policy = config["history_missing_policy"]
    if getattr(args, "run_forever", False) or (history_enabled and row["kind"] not in {"main_ema", "calendar_spread", "cross_section"}):
        raise ValueError("共享入口仅支持01/03/04历史预热；持续模式及其他策略历史预热尚未接入")
    return entry, args


def instrument_specs(kind, references):
    if kind == "option_vega":
        from types import SimpleNamespace
        return {instrument: SimpleNamespace(tick=references.contracts[key].tick,
                    multiplier=references.contracts[key].multiplier) if key in references.contracts else
                SimpleNamespace(tick=references.futures[key]["minChgPriceNum"],
                    multiplier=references.futures[key]["contMultNum"])
                for key, instrument in references.instrument_ids.items()}
    if kind == "main_ema":
        return {references.instrument_id: references.spec}
    return {references.instrument_ids[p]: spec for p, spec in references.specs.items()}


def assemble_strategy_session(entry, args, inputs, *, service, driver, strategy_id,
                              prepared_reference=None):
    if hasattr(inputs, "ctp_upstream"):
        from importlib import import_module
        session = import_module("demos.08_option_vega.live_runtime").assemble(args, inputs, driver,
            shared_service=service, strategy_id=strategy_id)
        session.restore_signal = lambda state: import_module("demos.08_option_vega.rollover").restore_signal(session, state)
    else:
        refresh_prepared_references(inputs.references, prepared_reference)
        session = entry.assemble(args, inputs.references, driver, inputs.upstream,
                          shared_service=service, strategy_id=strategy_id)
    from .rollover import validate_strategy_identity
    session.validate_rollover_identity = validate_strategy_identity
    return session


def refresh_prepared_references(references, prepared_reference=None):
    """控制线程组装前刷新观测；其他策略准备耗时不能使首个缓存过期。"""
    refresh = getattr(references, "refresh", None)
    if not callable(refresh):
        return
    refresh()
    if prepared_reference is not None and references.spec != prepared_reference:
        raise RuntimeError("策略组装前参考合约或条款已变化，须重新准备会话")


def reference_identity(session):
    identity = getattr(session, "session_identity", None) or str(session.references.spec)
    market = getattr(session, "market_source_identity", None)
    return {"reference": identity, "market_config": market} if market is not None else identity
