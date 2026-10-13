"""Black-76认购IV/Greeks与受预算约束的卖方仓位，纯数值计算。"""
from dataclasses import dataclass
from math import erf, exp, floor, isfinite, log, pi, sqrt


def cdf(x):
    return (1 + erf(x / sqrt(2))) / 2


def call_price(forward, strike, years, rate, volatility):
    if not all(isfinite(v) for v in (forward, strike, years, rate, volatility)) or min(forward, strike, years, volatility) <= 0:
        raise ValueError("Black76输入无效")
    root = volatility * sqrt(years)
    d1 = log(forward / strike) / root + root / 2
    return exp(-rate * years) * (forward * cdf(d1) - strike * cdf(d1 - root))


@dataclass(frozen=True)
class Greeks:
    iv: float
    delta: float
    gamma: float
    vega: float


def implied_greeks(forward, strike, years, rate, premium):
    if not all(isfinite(v) for v in (forward, strike, years, rate, premium)) or min(forward, strike, years, premium) <= 0:
        return None
    discount = exp(-rate * years)
    if not discount * max(forward - strike, 0) < premium < discount * forward:
        return None
    lo, hi = 0.00001, 5.0
    if not call_price(forward, strike, years, rate, lo) <= premium <= call_price(forward, strike, years, rate, hi):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if call_price(forward, strike, years, rate, mid) > premium:
            hi = mid
        else:
            lo = mid
    iv = (lo + hi) / 2
    if abs(call_price(forward, strike, years, rate, iv) - premium) > max(1e-8, premium * 1e-8):
        return None
    d1 = (log(forward / strike) + iv * iv * years / 2) / (iv * sqrt(years))
    pdf = exp(-d1 * d1 / 2) / sqrt(2 * pi)
    return Greeks(iv, discount * cdf(d1), discount * pdf / (forward * iv * sqrt(years)),
                  discount * forward * pdf * sqrt(years))


def cached_greeks(forward, strike, years, rate, iv):
    if min(forward, strike, years, iv) <= 0:
        return None
    return implied_greeks(forward, strike, years, rate, call_price(forward, strike, years, rate, iv))


def allocate_short(candidates, delta_targets, cash_vega_budget, max_lots, max_total):
    """候选元组(symbol, Greeks, multiplier)；同一合约可对应多个Delta档。

    向下取整；合并重复合约。单次组合现金Vega不超过预算，不每日累加。
    """
    if not candidates:
        return {}
    choices = [min(candidates, key=lambda x: (abs(x[1].delta - target), x[0])) for target in delta_targets]
    weights = [1 / (item[1].gamma * float(item[2])) for item in choices]
    denominator = sum(w * item[1].vega * float(item[2]) for w, item in zip(weights, choices))
    raw = {}
    for weight, item in zip(weights, choices):
        raw[item[0]] = raw.get(item[0], 0) + cash_vega_budget * weight / denominator
    quantities = {key: min(floor(value + 1e-10), max_lots) for key, value in raw.items()}
    total = sum(quantities.values())
    if total > max_total:
        quantities = {key: floor(value * max_total / total) for key, value in quantities.items()}
    return {key: -value for key, value in quantities.items() if value > 0}
