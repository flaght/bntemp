"""期货费用的单位、金额和系数契约；不依赖引擎或行情。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from types import MappingProxyType
from typing import Mapping


def fee_decimal(value, name, *, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name}须为有限{'正' if positive else '非负'}数") from exc
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise ValueError(f"{name}须为有限{'正' if positive else '非负'}数")
    return result


def _missing(value):
    return value is None or str(value).strip().lower() in {"", "nan", "nat", "none", "<na>"}


def _source_amounts(value, unit):
    normalized = "".join(str(unit).split()).replace("％", "%").replace("／", "/")
    if normalized == "元/手":
        return value, Decimal(0)
    if normalized == "%":
        return Decimal(0), value / Decimal(100)
    raise ValueError(f"不支持的手续费单位: {unit!r}，须明确为元/手或%")


@dataclass(frozen=True)
class CommissionCharge:
    standard_commission: Decimal
    commission_multiplier: Decimal
    actual_commission: Decimal


@dataclass(frozen=True)
class FuturesFee:
    """费率统一使用小数比例；原始数值、单位仅作溯源，不参与重复转换。"""

    per_contract: Decimal = Decimal(0)
    rate: Decimal = Decimal(0)
    source_value: Decimal | None = None
    source_unit: str | None = None

    def __post_init__(self):
        for name in ("per_contract", "rate"):
            object.__setattr__(self, name, fee_decimal(getattr(self, name), name))
        if (self.source_value is None) != (self.source_unit is None):
            raise ValueError("手续费原始数值和单位须同时提供")
        if self.source_value is not None:
            object.__setattr__(self, "source_value", fee_decimal(self.source_value, "source_value"))
            object.__setattr__(self, "source_unit", str(self.source_unit).strip())
            if _source_amounts(self.source_value, self.source_unit) != (self.per_contract, self.rate):
                raise ValueError("手续费原始字段与标准字段冲突")

    @classmethod
    def from_source(cls, value, unit):
        value = fee_decimal(value, "手续费数值")
        original_unit = str(unit).strip()
        fixed, rate = _source_amounts(value, original_unit)
        return cls(fixed, rate, source_value=value, source_unit=original_unit)

    @classmethod
    def from_mapping(cls, row, kind="trade"):
        """旧文件无费用列时返回None；半缺失或原始/标准字段冲突时拒绝。"""
        if kind not in {"trade", "delivery"}:
            raise ValueError("费用类型须为trade或delivery")
        prefix = "trade" if kind == "trade" else "deli"
        raw_names = (prefix + "CommiNum", prefix + "CommiUnit")
        names = (kind + "_fee_per_contract", kind + "_fee_rate")
        values = tuple(row.get(name) for name in raw_names)
        normalized = tuple(row.get(name) for name in names)
        raw = result = None
        if not all(_missing(value) for value in values):
            if any(_missing(value) for value in values):
                raise ValueError(f"{raw_names}数值和单位须同时提供")
            raw = cls.from_source(*values)
        if not all(_missing(value) for value in normalized):
            if any(_missing(value) for value in normalized):
                raise ValueError(f"{names}须同时提供")
            result = cls(*normalized)
        stored = row.get(kind + "_fee")
        if stored is not None and not _missing(stored):
            if isinstance(stored, Mapping):
                stored = cls(**stored)
            if not isinstance(stored, cls):
                raise ValueError(f"{kind}_fee须为FuturesFee")
            if result is not None and (stored.per_contract, stored.rate) != (result.per_contract, result.rate):
                raise ValueError("手续费标准字段冲突")
            result = stored
        if raw is not None and result is not None and (raw.per_contract, raw.rate) != (result.per_contract, result.rate):
            raise ValueError("手续费原始字段与标准字段冲突")
        return raw if raw is not None else result

    def columns(self, kind="trade"):
        return {kind + "_fee_per_contract": self.per_contract, kind + "_fee_rate": self.rate}

    def charge(self, quantity, price=None, contract_multiplier=None, *, commission_multiplier=1):
        quantity = fee_decimal(quantity, "成交手数")
        coefficient = fee_decimal(commission_multiplier, "commission_multiplier", positive=True)
        standard = quantity * self.per_contract
        if self.rate:
            price = fee_decimal(price, "成交价格", positive=True)
            multiplier = fee_decimal(contract_multiplier, "合约乘数", positive=True)
            standard += price * multiplier * quantity * self.rate
        # 先乘系数，再按人民币分舍入；不提前舍入交易所标准费用。
        return CommissionCharge(standard, coefficient,
            (standard * coefficient).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class CommissionConfig:
    commission_multiplier: Decimal = Decimal(1)
    commission_product_multipliers: Mapping[str, Decimal] = field(default_factory=dict)
    commission_contract_multipliers: Mapping[str, Decimal] = field(default_factory=dict)
    delivery_fee_multiplier: Decimal = Decimal(1)
    commission_source: str = "auto"

    def __post_init__(self):
        for name in ("commission_multiplier", "delivery_fee_multiplier"):
            object.__setattr__(self, name, fee_decimal(getattr(self, name), name, positive=True))
        if self.commission_source not in {"auto", "metadata", "fixed"}:
            raise ValueError("commission_source须为auto、metadata或fixed")
        for name, pattern in (("commission_product_multipliers", r"[A-Z]+"),
                              ("commission_contract_multipliers", r"[A-Z]+\d+(?:\.[A-Z]+)?")):
            values = {}
            for key, value in getattr(self, name).items():
                key = str(key).strip().upper()
                if not re.fullmatch(pattern, key) or key in values:
                    raise ValueError(f"{name}包含无效或重复代码: {key}")
                values[key] = fee_decimal(value, name + "/" + key, positive=True)
            object.__setattr__(self, name, MappingProxyType(values))

    def resolve(self, instrument_id, product=None):
        key = str(instrument_id).strip().upper()
        symbol = key.split(".")[0]
        if product is None:
            match = re.match(r"[A-Z]+", symbol)
            product = match[0] if match else ""
        return self.commission_contract_multipliers.get(key,
            self.commission_contract_multipliers.get(symbol,
                self.commission_product_multipliers.get(str(product).strip().upper(),
                    self.commission_multiplier)))

    def profile_kwargs(self):
        return {"commission_multiplier": self.commission_multiplier,
            "commission_product_multipliers": dict(self.commission_product_multipliers),
            "commission_contract_multipliers": dict(self.commission_contract_multipliers),
            "delivery_fee_multiplier": self.delivery_fee_multiplier,
            "commission_source": self.commission_source}
