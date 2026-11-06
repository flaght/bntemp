"""08启动依赖声明；JSON仅提供策略和风险参数，不改变执行授权或状态路径。"""
from dataclasses import fields

from .vega_signal import OptionVegaConfig


def add_strategy_config_argument(parser):
    parser.add_json_config_argument("--strategy-config",
        fields={field.name for field in fields(OptionVegaConfig)} | {"max_notional", "limit_offset_ticks"},
        sequences=("delta_targets", "hedge_slots"), help="策略与风险参数JSON；显式CLI优先")


def effective_parameters(args, config):
    return {**vars(config), **{name: getattr(args, name) for name in
        ("max_notional", "limit_offset_ticks") if hasattr(args, name)}}
