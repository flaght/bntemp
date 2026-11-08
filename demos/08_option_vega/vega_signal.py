"""期权条款、配置、数值算法与可独立恢复的策略信号状态。"""
from bisect import bisect_right
from dataclasses import dataclass, fields
from datetime import date, datetime, time
from decimal import Decimal
from math import erf, exp, isfinite, log, pi, sqrt


@dataclass(frozen=True)
class Contract:
    """认购期权静态条款与对应同月对冲期货，不负责读取文件。"""
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
    """源策略风险预算与时点；close_remaining_days仅保留旧检查点参数兼容。"""
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
    max_market_age_seconds: int = 300
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
        """每日新增配仓的现金Vega尺度，口径为波动率sigma变化1.0。

        nav=40,000,000、vega_budget=1.25时返回500,000；对应sigma变化
        0.01的线性敏感度尺度为5,000。vega_budget不是本金的1.25倍或
        保证金比例；此值用于allocate_short的手数归一化，不直接生成订单金额。
        累计旧仓的Vega不会从这个B中扣除，累计手数另由策略外层限额检查。
        """
        # 数值函数的敏感度按波动率变化1计算，预算对应规模基准乘预算参数再除以100。
        # 波动率变化一个百分点时的现金预算还需除以100，即规模基准乘参数除以10000。
        return float(self.nav) * self.vega_budget / 100


def add_strategy_config_argument(parser):
    """回测与Live共享JSON参数范围；解析与CLI优先级由公共框架负责。"""
    parser.add_json_config_argument("--strategy-config",
        fields={field.name for field in fields(OptionVegaConfig)} | {"max_notional", "limit_offset_ticks"},
        sequences=("delta_targets", "hedge_slots"), help="策略与风险参数JSON；显式CLI优先")


def effective_parameters(args, config):
    return {**vars(config), **{name: getattr(args, name) for name in
        ("max_notional", "limit_offset_ticks") if hasattr(args, name)}}


def risk_limits(config, contracts, instruments, multipliers, max_notional):
    """将策略手数及金额配置转换为公共执行端的逐合约限额。"""
    from bomber.framework.trader.execution.risk import RiskLimits
    return {instrument: RiskLimits(
        max_order_quantity=(config.max_option_lots if key in contracts else config.max_future_lots) * 2,
        max_abs_position=config.max_option_lots if key in contracts else config.max_future_lots,
        max_order_notional=max_notional, max_abs_position_notional=max_notional,
        max_market_age_ns=config.max_market_age_seconds * 1_000_000_000,
        contract_multiplier=multipliers[instrument]) for key, instrument in instruments.items()}


def tau(now: datetime, last_day: date) -> float:
    # 保留旧自然年化辅助函数供历史夹具使用；当前策略使用source_years。
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


def cdf(x):
    """标准正态分布的累计概率。"""
    return (1 + erf(x / sqrt(2))) / 2


def call_price(forward, strike, years, rate, volatility):
    """以同月期货价格为标的，计算认购期权理论价格。"""
    if not all(isfinite(v) for v in (forward, strike, years, rate, volatility)) or min(forward, strike, years, volatility) <= 0:
        raise ValueError("Black76输入无效")
    root = volatility * sqrt(years)
    d1 = log(forward / strike) / root + root / 2
    return exp(-rate * years) * (forward * cdf(d1) - strike * cdf(d1 - root))


@dataclass(frozen=True)
class Greeks:
    """保存隐含波动率及价格一阶、二阶和波动率敏感度。"""
    iv: float
    delta: float
    gamma: float
    vega: float


def implied_greeks(forward, strike, years, rate, premium):
    """校验价格边界并二分反解隐含波动率，失败返回空值。"""
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


def source_iv(forward, strike, years, rate, premium):
    """用市场认购价格C反解Black76隐含波动率sigma，保持源代码计算口径。

    F=同月期货价，K=执行价，T=source_years，r=年化利率：
        C(sigma)=exp(-r*T)*(F*N(d1)-K*N(d2))
        d1=(ln(F/K)+sigma**2*T/2)/(sigma*sqrt(T)), d2=d1-sigma*sqrt(T)
    N是标准正态累计分布；理论价格随sigma增加而增加，适合二分查找。
    非有限值、非正F/K/T/C、C低于折现内在价值或达到折现F上界时返回None。
    搜索区间[0.0001, 5.0]，执行64轮；保留源代码行为，不额外做区间端点
    可达性或最终重定价误差检查，故返回数值不代表所有输入都能精确重定价。
    """
    if not all(isfinite(v) for v in (forward, strike, years, rate, premium)) or min(forward, strike, years, premium) <= 0:
        return None
    discount = exp(-rate*years)
    if premium < discount*max(forward-strike, 0) or premium >= discount*forward:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(64):
        mid = (lo+hi)/2
        if call_price(forward, strike, years, rate, mid) > premium:
            hi = mid
        else:
            lo = mid
    return (lo+hi)/2


def source_years(now, remaining_days, *, hedge=False):
    """源代码的交易年期限：N不含当日、包含最后交易日。

    开仓T=N/252；对冲T=N/252 + R/(240*252)，R为当日剩余交易分钟。
    R按09:30–11:30、13:00–15:00及全天240分钟计算：09:30为240，
    午休为120，14:00为60，15:00及以后为0。这里只按分钟数，不计秒。
    这只是保留的源定价公式，不是常驻进程的开收盘规则；MO的14:57等
    运行边界仍由外部交易日历负责，不能用这个公式决定连接何时结束。
    """
    minute = now.hour * 60 + now.minute
    remaining = (240 if minute < 570 else 240-(minute-570) if minute < 690 else
                 120 if minute < 780 else 120-(minute-780) if minute < 900 else 0)
    return remaining_days/252 + (remaining/(240*252) if hedge else 0)


def source_greeks(forward, strike, years, rate, iv, discount_years):
    """按源口径返回认购Greeks，期权乘数在配仓和敞口汇总时另乘。

    T为交易年期限、sigma为IV、Tc为到最后交易日的自然日差/365：
        d1=(ln(F/K)+sigma**2*T/2)/(sigma*sqrt(T))
        Delta=N(d1)
        Gamma=phi(d1)/(F*sigma*sqrt(T))
        Vega=phi(d1)*F*sqrt(T)*exp(-r*Tc)
    phi是标准正态密度。Vega对sigma变化1.0计量，按1个百分点需再乘0.01。
    Delta/Gamma没有折现项，Vega用Tc折现；这与所有Greeks都用同一个T和
    同一折现因子的定义不同，当前策略沿用源代码，不能混接另一组Greeks。
    返回的是多头认购敏感度，卖出后的方向由负的实际手数/增量体现。
    """
    if min(forward, strike, years, iv) <= 0:
        return None
    root = sqrt(years)
    d1 = (log(forward/strike) + iv*iv*years/2)/(iv*root)
    pdf = exp(-d1*d1/2)/sqrt(2*pi)
    return Greeks(iv, cdf(d1), pdf/(forward*iv*root),
                  pdf*forward*root*exp(-rate*discount_years))


def allocate_short(candidates, delta_targets, cash_vega_budget, max_lots, max_total):
    """将每日现金Vega预算B分配到各Delta档，返回symbol→负的新增手数。

    输入每项为(symbol, Greeks, multiplier)。依次对每个目标Delta取距离
    最小候选，距离相同时沿用输入中先出现的候选，不保证各档选不同合约。

    对选中档j：m_j为期权乘数，Gamma_j/Vega_j为多头认购敏感度：
        w_j=1/(Gamma_j*m_j)
        D=sum(w_j*Vega_j*m_j)
        a_j=B*w_j/D
    各档分担同一个B；未取整/截限时sum(a_j*Vega_j*m_j)=B。
    这种权重让各档的a_j*Gamma_j*m_j相等，不等于每档手数或Vega相等。
    同一symbol命中多个档时先累加a_j，故不会因为重复档而多用一份预算。

    执行顺序不能互换：按symbol合并→Python round到整手→单合约max_lots
    截限→总手数超max_total时按比例缩放并int向下取整→丢弃0手→取负数。
    round为半偶舍入，如round(2.5)=2；缩放后不足总额度的余数不会再分配。
    例如原始手数2.4/1.6先变2/2，若总上限3则分别int(2*3/4)=1，
    最终只卖1/1手，总数2而不是补齐到3。返回空字典时没有新增目标。

    本函数只控制当天增量手数，不扣除旧仓，也不检查增量叠加后的累计手数、
    实际资金或委托状态；这些检查分别由策略_frame及公共执行风控完成。
    """
    if not candidates:
        return {}
    choices = [min(candidates, key=lambda x: abs(x[1].delta - target)) for target in delta_targets]
    weights = [1 / (item[1].gamma * float(item[2])) for item in choices]
    denominator = sum(w * item[1].vega * float(item[2]) for w, item in zip(weights, choices))
    raw = {}
    for weight, item in zip(weights, choices):
        raw[item[0]] = raw.get(item[0], 0) + cash_vega_budget * weight / denominator
    quantities = {key: min(round(value), max_lots) for key, value in raw.items()}
    total = sum(quantities.values())
    if total > max_total:
        quantities = {key: int(value * max_total / total) for key, value in quantities.items()}
    return {key: -value for key, value in quantities.items() if value > 0}


def filter_hedgeable_options(candidates, contracts, future_rows, day):
    """剔除同月期货缺条款、未上市或已到期的认购候选。"""
    eligible, skipped = [], set()
    for key in candidates:
        future = contracts[key].future
        if future not in future_rows:
            skipped.add(future)
            continue
        row = future_rows[future]
        if not row["listDate"] <= day <= row["lastTradeDate"]:
            skipped.add(future)
            continue
        eligible.append(key)
    return tuple(eligible), tuple(sorted(skipped))


class OptionVegaCheckpoint:
    def __init__(self, strategy):
        self.strategy = strategy

    def snapshot_state(self):
        s = self.strategy
        return {"version": 1,
            "option_goals": {key: str(value) for key, value in s.option_goals.items()},
            "future_goals": {key: str(value) for key, value in s.future_goals.items()},
            "sent_days": sorted(str(day) for day in s.sent_days),
            "logic_version": "strategy1-noddb-v1", "hedge_sent": sorted(s.hedge_sent),
            "submissions": s.submissions, "entry_decisions": s.entry_decisions,
            "flatten_requested": s.flatten_requested}

    def restore_state(self, state):
        if state.get("version") != 1:
            raise ValueError("不支持的Vega策略检查点")
        s = self.strategy
        if state.get("logic_version", "strategy1-noddb-v1") != "strategy1-noddb-v1":
            raise ValueError("不支持的Vega信号逻辑版本")
        hedge_sent = set(state.get("hedge_sent", ()))
        for token in hedge_sent:
            day, slot = token.split("_", 1)
            if date.fromisoformat(day) not in s.trading_days or slot not in s.config.hedge_slots:
                raise ValueError("恢复对冲时点无效")
        restored = {}
        for name, expected, cap in (("option_goals", s.contracts, s.config.max_option_lots),
                                    ("future_goals", s.futures, s.config.max_future_lots)):
            values = state[name]
            if set(values) != set(expected):
                raise ValueError("恢复时Vega合约集合不一致")
            values = {key: Decimal(value) for key, value in values.items()}
            if any(not value.is_finite() or value != value.to_integral_value() or abs(value) > cap
                   or name == "option_goals" and value > 0 for value in values.values()):
                raise ValueError("恢复目标数量无效或超过配置上限")
            restored[name] = values
        if sum(abs(v) for v in restored["option_goals"].values()) > s.config.max_total_option_lots:
            raise ValueError("恢复期权总手数超过上限")
        days = {date.fromisoformat(value) for value in state["sent_days"]}
        if not days <= set(s.trading_days):
            raise ValueError("恢复选约日期不在日历内")
        for name in ("submissions", "entry_decisions"):
            if type(state[name]) is not int or state[name] < 0:
                raise ValueError("恢复计数无效")
        if type(state["flatten_requested"]) is not bool:
            raise ValueError("恢复清仓状态无效")
        s.option_goals, s.future_goals = restored["option_goals"], restored["future_goals"]
        s.sent_days = days
        s.hedge_sent = hedge_sent
        s.submissions, s.entry_decisions = state["submissions"], state["entry_decisions"]
        s.flatten_requested = state["flatten_requested"]
        s.latest.clear()
        s.iv_cache.clear()
        s.pending_ts = s.resume_ns = None
        s.last_event_ns = -1
        s.restored = True
