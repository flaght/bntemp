"""CTP高保真组合Profile中的双向持仓与手续费参数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from bomber.backtest.models import StandardMarginModel
from bomber.model import Money, Venue
from bomber.model.currencies import CNY
from bomber.model.enums import AccountType, OmsType

from bomber.framework.trader.execution.contracts import PositionEffect
from bomber.framework.trader.execution.simulation.profile import GenericVenueProfile


def _decimal(value: Decimal | int | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class CtpCommissionRule:
    """分别配置开仓、平昨和平今的每手费用、成交金额费率及手续费系数。"""

    open_per_contract: Decimal | int | float | str = Decimal(0)
    close_per_contract: Decimal | int | float | str = Decimal(0)
    close_today_per_contract: Decimal | int | float | str = Decimal(0)
    open_rate: Decimal | int | float | str = Decimal(0)
    close_rate: Decimal | int | float | str = Decimal(0)
    close_today_rate: Decimal | int | float | str = Decimal(0)
    commission_multiplier: Decimal | int | float | str = Decimal(1)

    def __post_init__(self) -> None:
        for name in (
            "open_per_contract",
            "close_per_contract",
            "close_today_per_contract",
            "open_rate", "close_rate", "close_today_rate", "commission_multiplier",
        ):
            value = _decimal(getattr(self, name))
            if not value.is_finite() or value < 0 or (name == "commission_multiplier" and value == 0):
                raise ValueError("手续费须为有限非负数，手续费系数须为有限正数")
            object.__setattr__(self, name, value)

    def calculate(
        self,
        effect: PositionEffect | str,
        quantity: Decimal | int | float | str,
        price=None,
        contract_multiplier=None,
        *,
        commission_multiplier=None,
    ) -> Decimal:
        return self.calculate_details(effect, quantity, price, contract_multiplier,
            commission_multiplier=commission_multiplier).actual_commission

    def calculate_details(self, effect, quantity, price=None, contract_multiplier=None,
                          *, commission_multiplier=None):
        from bomber.framework.fees import FuturesFee
        effect = PositionEffect(effect)
        if effect is PositionEffect.OPEN:
            fixed, rate = self.open_per_contract, self.open_rate
        elif effect is PositionEffect.CLOSE_TODAY:
            fixed, rate = self.close_today_per_contract, self.close_today_rate
        elif effect in {PositionEffect.CLOSE, PositionEffect.CLOSE_YESTERDAY}:
            fixed, rate = self.close_per_contract, self.close_rate
        else:
            raise ValueError("手续费核算需要明确的开平标志")
        return FuturesFee(fixed, rate).charge(quantity, price, contract_multiplier,
            commission_multiplier=self.commission_multiplier if commission_multiplier is None else commission_multiplier)

    @classmethod
    def from_mapping(cls, row, *, commission_multiplier=1):
        """源表只有通用费率时三种开平使用相同标准；不推断平今优惠。"""
        from bomber.framework.fees import FuturesFee
        fee = FuturesFee.from_mapping(row)
        if fee is None:
            raise ValueError("缺少交易手续费数值和单位")
        return cls(fee.per_contract, fee.per_contract, fee.per_contract,
            fee.rate, fee.rate, fee.rate, commission_multiplier)


@dataclass(frozen=True)
class CtpFuturesHedgingProfile:
    """CTP双向持仓Profile。

    Nautilus Venue只承担HEDGING撮合基础；今昨仓、平仓标志和逐日结算由Ledger、
    Planner和Accounting组合实现，避免假装原生引擎已经理解CTP全部柜台语义。
    """

    profile_id: str = "ctp-futures-hedging"
    starting_balance: Decimal | int | float | str = Decimal("1000000")
    venue: Venue = field(default_factory=lambda: Venue("SHFE"))
    commission_rule: CtpCommissionRule = field(default_factory=CtpCommissionRule)

    def __post_init__(self) -> None:
        balance = _decimal(self.starting_balance)
        if not self.profile_id.strip() or balance <= 0:
            raise ValueError("profile_id不能为空且starting_balance必须大于零")
        object.__setattr__(self, "starting_balance", balance)

    def build_backend_config(self) -> Mapping[str, Any]:
        # 不配置原生统一费率，避免与外部今昨仓差异化手续费重复扣费；真实开/平今
        # 差异由CtpExecutionAccounting按ExecutionReport.position_effect核算。
        return GenericVenueProfile(
            profile_id=self.profile_id,
            venue=self.venue,
            oms_type=OmsType.HEDGING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money(self.starting_balance, CNY)],
            base_currency=CNY,
            default_leverage=Decimal(1),
            margin_model=StandardMarginModel(),
            fee_model=None,
            use_position_ids=True,
            use_reduce_only=True,
            use_market_order_acks=True,
            bar_execution=True,
            trade_execution=True,
        ).build_backend_config()
