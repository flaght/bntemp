"""策略配置与条款；不依赖行情源、DataHub或交易客户端。"""
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from math import isfinite


@dataclass(frozen=True)
class Contract:
    symbol: str
    future: str
    strike: float
    multiplier: Decimal
    list_day: date
    last_day: date
    expiry_day: date
    tick: Decimal


@dataclass(frozen=True)
class OptionVegaConfig:
    nav: Decimal = Decimal("40000000")
    vega_budget: float = 1.25
    delta_targets: tuple[float, ...] = (0.2, 0.25, 0.3, 0.35)
    option_slot: str = "13:58"
    hedge_slots: tuple[str, ...] = ("10:00", "11:00", "13:30", "14:00", "14:55")
    flatten_slot: str = "14:50"
    min_remaining_days: int = 5
    close_remaining_days: int = 2
    rate: float = 0.02
    min_time_value: float = 5.0
    max_market_age_seconds: int = 120
    max_iv_age_seconds: int = 300
    max_option_lots: int = 100
    max_future_lots: int = 100
    max_total_option_lots: int = 200

    def __post_init__(self):
        nav = Decimal(str(self.nav))
        if not nav.is_finite() or nav <= 0:
            raise ValueError("nav必须为有限正数")
        object.__setattr__(self, "nav", nav)
        if not isfinite(self.vega_budget) or self.vega_budget <= 0:
            raise ValueError("vega_budget必须为有限正数")
        if not self.delta_targets or any(not isfinite(v) or not 0 < v < 1 for v in self.delta_targets):
            raise ValueError("delta_targets须在(0,1)范围内")
        if not isfinite(self.rate) or not isfinite(self.min_time_value) or self.min_time_value < 0:
            raise ValueError("利率或最小时间价值无效")
        for name in ("max_market_age_seconds", "max_iv_age_seconds", "max_option_lots", "max_future_lots", "max_total_option_lots"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name}必须为正整数")
        if not 0 <= self.close_remaining_days < self.min_remaining_days:
            raise ValueError("需满足0 <= close_remaining_days < min_remaining_days")
        for slot in (self.option_slot, self.flatten_slot, *self.hedge_slots):
            if time.fromisoformat(slot).strftime("%H:%M") != slot:
                raise ValueError("时点必须为HH:MM")
        if self.option_slot >= self.flatten_slot:
            raise ValueError("开仓时点必须早于最终清仓时点")

    @property
    def vega_cash_per_unit(self):
        # 与原策略单次预算口径一致：每1个百分点IV变动的现金Vega预算=nav*budget/10000。
        return float(self.nav) * self.vega_budget / 100


def tau(now: datetime, last_day: date) -> float:
    # 所有IV和Greeks统一使用自然年化剩余时间，而不混用252/365。
    expiry = datetime.combine(last_day, time(15), tzinfo=now.tzinfo)
    return max((expiry - now).total_seconds() / (365 * 86400), 0.0)


def remaining_trading_days(trading_days, day, last_day, threshold):
    """统计不含当日、含最后交易日的日期；远期只需足以判断策略阈值。

    若到期超过已知日期末端，计数是下界。下界达到threshold时可以确定
    尚未进入对应开仓/清仓限制；否则不把缺少未来日期当成临近到期。
    """
    if not trading_days:
        raise ValueError("没有可用交易日")
    if last_day <= trading_days[-1] and last_day not in trading_days:
        raise ValueError(f"合约最后交易日{last_day}不在已知交易日中")
    count = bisect_right(trading_days, last_day) - bisect_right(trading_days, day)
    if last_day > trading_days[-1] and count < threshold:
        raise ValueError(
            f"{day}: 合约最后交易日{last_day}超出已知日期末端{trading_days[-1]}，"
            f"之后仅有{count}个已知交易日，无法判断{threshold}日阈值；"
            "请让行情日期覆盖回测结束后的足够交易日，或提供--calendar"
        )
    return count
