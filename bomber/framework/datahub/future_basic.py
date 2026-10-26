"""不可变期货基础条款与按时间查询接口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping

from .basic import BasicProvider, contract_date, field, missing, positive_decimal, text
from .temporal import ReferenceRecord
from bomber.framework.fees import FuturesFee

FUTURE_BASIC_DATASET = "future_basic"


@dataclass(frozen=True)
class FutureBasic:
    """保留源合约与交易所身份；信号专用资料允许不含价位和乘数。"""

    symbol: str
    exchange: str
    currency: str
    product: str
    list_date: date
    last_trade_date: date
    multiplier: Decimal | None = None
    price_increment: Decimal | None = None
    trade_fee: FuturesFee | None = None
    delivery_fee: FuturesFee | None = None

    def __post_init__(self) -> None:
        for name in ("symbol", "exchange", "currency", "product"):
            object.__setattr__(self, name, text(getattr(self, name), name))
        for name in ("list_date", "last_trade_date"):
            object.__setattr__(self, name, contract_date(getattr(self, name), name))
        if self.list_date > self.last_trade_date:
            raise ValueError("期货上市日不能晚于最后交易日")
        for name in ("multiplier", "price_increment"):
            value = getattr(self, name)
            object.__setattr__(self, name, None if missing(value) else positive_decimal(value, name))
        for name in ("trade_fee", "delivery_fee"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, FuturesFee):
                raise ValueError(f"{name}须为FuturesFee或None")

    @property
    def trade_fee_per_contract(self):
        return None if self.trade_fee is None else self.trade_fee.per_contract

    @property
    def trade_fee_rate(self):
        return None if self.trade_fee is None else self.trade_fee.rate

    @classmethod
    def from_mapping(cls, row: Mapping[str, object]) -> FutureBasic:
        return cls(
            symbol=field(row, "symbol", "Code"),
            exchange=field(row, "exchange", "exchangeCD"),
            currency=field(row, "currency", "currencyCD"),
            product=field(row, "product", "code"),
            list_date=field(row, "list_date", "listDate"),
            last_trade_date=field(row, "last_trade_date", "lastTradeDate"),
            multiplier=field(row, "multiplier", "contMultNum"),
            price_increment=field(row, "minChgPriceNum", "tickNum", "price_increment"),
            trade_fee=FuturesFee.from_mapping(row),
            delivery_fee=FuturesFee.from_mapping(row, "delivery"),
        )


class FutureBasicProvider(BasicProvider[FutureBasic]):
    dataset = FUTURE_BASIC_DATASET
    value_type = FutureBasic

    @classmethod
    def from_feather(
        cls, path: str | Path, *,
        record_factory: Callable[[FutureBasic, Mapping[str, object]], ReferenceRecord[FutureBasic]],
    ) -> FutureBasicProvider:
        """兼容入口；文件读取委托 dataprep，时间语义必须由调用方声明。"""
        from bomber.framework.dataprep.basic import load_future_basic_provider

        return load_future_basic_provider(path, record_factory=record_factory)
