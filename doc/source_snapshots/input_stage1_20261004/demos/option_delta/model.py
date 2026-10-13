from dataclasses import dataclass
from datetime import date, datetime, time
from math import isfinite


@dataclass(frozen=True)
class Option:
    symbol: str
    kind: str
    strike: float
    listed: date
    expiry: date
    tick: float
    multiplier: float
    month: str


@dataclass(frozen=True)
class Config:
    product: str = "MO"
    index_code: str = "000852"
    model: str = "bs"
    future_product: str = "IM"
    sides: tuple = ("C", "P")
    delta_min: float = .25
    delta_max: float = .30
    target_delta: float = .275
    fallback_min: float = .225
    fallback_max: float = .325
    allow_fallback: bool = True
    dte_min: int = 20
    dte_max: int = 45
    target_dte: float = 32.5
    min_remaining_days: int = 7
    expiry_month: str | None = None
    select_slots: tuple = ("13:58",)
    rate: float = .02
    dividend_yield: float = 0
    max_quote_age_seconds: int = 60
    min_open_interest: float = 1
    min_volume: float = 0

    def __post_init__(self):
        numbers=(self.delta_min,self.delta_max,self.target_delta,self.fallback_min,self.fallback_max,
                 self.target_dte,self.rate,self.dividend_yield,
                 self.min_open_interest,self.min_volume,self.dte_min,self.dte_max,
                 self.min_remaining_days,self.max_quote_age_seconds)
        if not all(isfinite(n) for n in numbers): raise ValueError("参数须为有限数值")
        if not 0 < self.fallback_min <= self.delta_min <= self.target_delta <= self.delta_max <= self.fallback_max < 1:
            raise ValueError("Delta目标、核心区间与回退区间无效")
        if not self.min_remaining_days > 0 or not self.min_remaining_days <= self.dte_min <= self.target_dte <= self.dte_max:
            raise ValueError("剩余自然日参数无效")
        if self.max_quote_age_seconds <= 0 or min(self.min_open_interest,self.min_volume)<0:
            raise ValueError("帧迟到限制或流动性参数无效")
        if self.model not in ("bs","black76") or not self.sides or len(set(self.sides))!=len(self.sides) or set(self.sides)-{"C","P"}:
            raise ValueError("模型或Call/Put类型无效")
        if self.expiry_month is not None:
            if len(self.expiry_month)!=6 or not self.expiry_month.isdigit(): raise ValueError("月份须为YYYYMM")
            datetime.strptime(self.expiry_month,"%Y%m")
        if not self.select_slots or len(set(self.select_slots))!=len(self.select_slots): raise ValueError("选约时点为空或重复")
        for slot in self.select_slots:
            if time.fromisoformat(slot).strftime("%H:%M")!=slot: raise ValueError("选约时点须为HH:MM")


def years_to_expiry(now, expiry):
    return max(0,(datetime.combine(expiry,time(15),tzinfo=now.tzinfo)-now).total_seconds()/(365*86400))
