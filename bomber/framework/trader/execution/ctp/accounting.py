"""把统一成交回报写入CTP今昨仓账本。"""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from bomber.framework.market.basic.base import InstrumentId
from bomber.framework.trader.execution.contracts import ExecutionReport, ExecutionReportType
from bomber.framework.trader.execution.ctp.ledger import (
    CtpFillResult,
    CtpPositionLedger,
    CtpSettlementResult,
)
from bomber.framework.trader.execution.ctp.profile import CtpCommissionRule


class CtpExecutionAccounting:
    def __init__(
        self,
        ledger: CtpPositionLedger,
        multipliers: Mapping[InstrumentId, Decimal | int | float | str],
        commission_rules: Mapping[InstrumentId, CtpCommissionRule] | None = None,
        *,
        generic_close_today_first_venues=(),
        commission_config=None,
    ) -> None:
        self.ledger = ledger
        self.multipliers = {
            instrument_id: Decimal(str(value))
            for instrument_id, value in multipliers.items()
        }
        self.commission_rules = dict(commission_rules or {})
        self.commission_config = commission_config
        self.generic_close_today_first_venues = frozenset(generic_close_today_first_venues)
        self.results: list[CtpFillResult] = []
        self.commissions: list[Decimal] = []
        self.commission_records: list[dict] = []
        self.settlements: list[CtpSettlementResult] = []

    @property
    def total_commission(self) -> Decimal:
        return sum(self.commissions, Decimal(0))

    @property
    def total_realized_pnl(self) -> Decimal:
        return sum((result.realized_pnl for result in self.results), Decimal(0))

    @property
    def total_variation_margin(self) -> Decimal:
        return sum(
            (result.variation_margin for result in self.settlements),
            Decimal(0),
        )

    @property
    def net_cash_change(self) -> Decimal:
        return (
            self.total_realized_pnl
            + self.total_variation_margin
            - self.total_commission
        )

    def settle(
        self,
        trading_day: str,
        settlement_prices: Mapping[InstrumentId, Decimal | int | float | str],
    ) -> CtpSettlementResult:
        result = self.ledger.settle(
            trading_day,
            settlement_prices,
            self.multipliers,
        )
        self.settlements.append(result)
        return result

    def on_report(self, report: ExecutionReport) -> None:
        if report.report_type not in {
            ExecutionReportType.PARTIALLY_FILLED,
            ExecutionReportType.FILLED,
        }:
            return
        if (
            report.order_side is None
            or report.position_effect is None
            or report.fill_price is None
        ):
            raise ValueError("CTP成交回报缺少side、position_effect或fill_price")
        multiplier = self.multipliers.get(report.instrument_id)
        if multiplier is None:
            raise ValueError(f"缺少CTP合约乘数: {report.instrument_id}")
        rule = self.commission_rules.get(report.instrument_id)
        # 在变更账本之前完成费用校验，避免无效费率导致账本只更新一半。
        charge = None if rule is None else rule.calculate_details(
            report.position_effect, report.filled_quantity, report.fill_price, multiplier,
            commission_multiplier=None if self.commission_config is None
                else self.commission_config.resolve(report.instrument_id))
        self.results.append(
            self.ledger.apply_fill(
                report.instrument_id,
                report.order_side,
                report.position_effect,
                report.filled_quantity,
                report.fill_price,
                multiplier,
                close_today_first=str(report.instrument_id).rsplit(".", 1)[-1]
                    in self.generic_close_today_first_venues,
            ),
        )
        self.commissions.append(
            Decimal(0)
            if charge is None
            else charge.actual_commission,
        )
        self.commission_records.append({"instrument_id": str(report.instrument_id),
            "client_order_id": report.client_order_id, "position_effect": report.position_effect.value,
            "quantity": report.filled_quantity, "fill_price": report.fill_price,
            "contract_multiplier": multiplier,
            "source": "unconfigured" if charge is None else "commission_rule",
            "standard_commission": Decimal(0) if charge is None else charge.standard_commission,
            "commission_multiplier": None if charge is None else charge.commission_multiplier,
            "actual_commission": self.commissions[-1]})
