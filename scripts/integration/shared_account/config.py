"""共享SimNow入口的声明式配置；读取与校验不加载SDK、不连接网络。"""

from datetime import datetime
from decimal import Decimal
import json
import math
import re
from copy import deepcopy
from pathlib import Path


PROTECTED = frozenset(("--connect", "--mode", "--enable-orders", "--confirm-simnow", "--resume",
    "--state-file", "--expected-trading-day", "--seconds", "--account-reconciliation",
    "--strategy-position-source", "--run-forever", "--adopt-existing-position", "--expected-position",
    "--history-minutes", "--history-file", "--history-config", "--simnow-environment",
    "--replay-md-trading-day"))


def read_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    allowed = {"version", "trading_day", "environment", "replay_md_trading_day", "seconds",
        "account_state_file", "report_dir", "account", "strategies", "position_scope", "execution_mode"}
    if set(config) - allowed or config.get("version") != 1:
        raise ValueError("共享配置版本无效或存在未知字段")
    if config.get("position_scope", "account") not in {"account", "strategy_instruments"}:
        raise ValueError("position_scope须为account或strategy_instruments")
    if config.get("execution_mode", "netted") not in {"netted", "independent"}:
        raise ValueError("execution_mode须为netted或independent")
    day = config["trading_day"]
    if datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d") != day:
        raise ValueError("trading_day须为YYYYMMDD")
    environment = config.get("environment", "realtime")
    if environment not in {"realtime", "replay"}:
        raise ValueError("environment须为realtime或replay")
    replay = config.get("replay_md_trading_day")
    if environment == "replay":
        if not replay or datetime.strptime(replay, "%Y%m%d").strftime("%Y%m%d") != replay:
            raise ValueError("replay须声明原始MD交易日")
    elif replay:
        raise ValueError("原始MD交易日仅用于replay")
    seconds = float(config.get("seconds", 600))
    if not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise ValueError("共享会话时长须为(0,86400]秒")
    account = config["account"]
    if set(account) != {"client_id", "max_gross_quantity", "max_gross_notional", "max_session_orders", "limit_offset_ticks"}:
        raise ValueError("账户配置须完整声明身份、总量、名义预算及订单上限")
    if not isinstance(account["client_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account["client_id"]):
        raise ValueError("账户client_id不能为空")
    for key in ("max_gross_quantity", "max_gross_notional"):
        value = Decimal(str(account[key]))
        if not value.is_finite() or value <= 0:
            raise ValueError("账户预算须为有限正数")
    for key, minimum in (("max_session_orders", 1), ("limit_offset_ticks", 0)):
        if isinstance(account[key], bool) or not isinstance(account[key], int) or account[key] < minimum:
            raise ValueError("账户订单上限/跳价偏移无效")
    rows = config["strategies"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("至少配置一个策略")
    ids, paths = set(), {str((path.parent / config["account_state_file"]).resolve())}
    for row in rows:
        if not {"strategy_id", "kind", "state_file", "argv"} <= set(row) or set(row) - {
                "strategy_id", "kind", "state_file", "argv", "stop_after_seconds"}:
            raise ValueError("每个策略须声明ID、kind、信号文件和参数")
        sid = row["strategy_id"]
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid) or sid in ids:
            raise ValueError("策略ID为空或重复")
        ids.add(sid)
        stop_after = row.get("stop_after_seconds")
        if stop_after is not None and (not math.isfinite(float(stop_after)) or not 0 < float(stop_after) < seconds):
            raise ValueError("策略独立停止时间须在会话时长以内")
        if row["kind"] not in {"main_ema", "cross_section", "calendar_spread", "option_vega"}:
            raise ValueError("策略工厂支持main_ema、cross_section、calendar_spread和option_vega")
        if not isinstance(row["argv"], list) or any(not isinstance(arg, str) for arg in row["argv"]):
            raise ValueError("策略argv须为字符串数组")
        if any(arg.split("=", 1)[0] in PROTECTED for arg in row["argv"]):
            raise ValueError("策略参数不能覆盖宿主执行、日期、授权或恢复政策")
        state_file = str((path.parent / row["state_file"]).resolve())
        if state_file in paths:
            raise ValueError("账户与各策略检查点文件必须不同")
        paths.add(state_file)
        row["state_file"] = state_file
    config.update(environment=environment, seconds=seconds,
        account_state_file=str((path.parent / config["account_state_file"]).resolve()),
        report_dir=str((path.parent / config.get("report_dir", "results")).resolve()))
    return config


def validate_budget_increase(original, updated):
    """显式迁移只允许账户总名义限额上调，其他身份/路径/参数必须相同。"""
    candidate = deepcopy(updated)
    candidate["account"]["max_gross_notional"] = original["account"]["max_gross_notional"]
    if candidate != original or Decimal(str(updated["account"]["max_gross_notional"])) <= Decimal(
            str(original["account"]["max_gross_notional"])):
        raise ValueError("预算迁移仅允许上调账户max_gross_notional，其他配置须保持一致")
    return dict(updated["account"])
