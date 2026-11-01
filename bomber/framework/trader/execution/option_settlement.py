"""现金结算的值契约；不依赖原生内核或行情文件格式。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

SETTLEMENT_ORIGIN = "option_expiry"


def decimal_value(value, name, *, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def option_settlement_price(kind, strike, index_settlement):
    """由官方指数交割结算价计算每点期权终值；不使用最后一根 Bar。"""
    strike = decimal_value(strike, "strike", positive=True)
    index = decimal_value(index_settlement, "index_settlement", positive=True)
    if kind not in {"C", "P"}:
        raise ValueError("option kind must be C or P")
    return max(Decimal(0), index - strike if kind == "C" else strike - index)


@dataclass(frozen=True)
class OptionSettlement:
    instrument_id: str
    last_trade_ns: int
    available_ns: int
    price: Decimal
    fee_per_contract: Decimal = Decimal(0)
    source: str = ""

    def __post_init__(self):
        if not self.instrument_id.strip() or "." not in self.instrument_id:
            raise ValueError("settlement requires a qualified instrument ID")
        for name in ("last_trade_ns", "available_ns"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be nonnegative integer nanoseconds")
        if self.available_ns < self.last_trade_ns:
            raise ValueError("settlement cannot be available before last trading time")
        object.__setattr__(self, "price", decimal_value(self.price, "settlement price"))
        object.__setattr__(self, "fee_per_contract", decimal_value(self.fee_per_contract, "settlement fee"))


def settlement_pnl(quantity, entry_price, settlement_price, multiplier, fee_per_contract=0):
    """有符号持仓的全生命周期盈亏，供核对；正式账户由原生成交事件更新。"""
    quantity = Decimal(str(quantity))
    if not quantity.is_finite():
        raise ValueError("quantity must be finite")
    entry = decimal_value(entry_price, "entry price")
    price = decimal_value(settlement_price, "settlement price")
    multiplier = decimal_value(multiplier, "multiplier", positive=True)
    fee = decimal_value(fee_per_contract, "settlement fee")
    return quantity * (price - entry) * multiplier - abs(quantity) * fee
