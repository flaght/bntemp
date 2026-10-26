"""同一Venue按真实合约选择按手/按金额费用，人民币金额仅舍入一次。"""
from __future__ import annotations

from dataclasses import asdict

from bomber.backtest.models import FeeModel
from bomber.model import Money
from bomber.model.currencies import CNY
from bomber.model.instruments import FuturesContract
from bomber.framework.fees import CommissionConfig, FuturesFee, fee_decimal


class CtpFuturesFeeModel(FeeModel):
    def __init__(self, commission_per_contract=1, *, config=None):
        self.config = config or CommissionConfig()
        self.fallback = FuturesFee(per_contract=fee_decimal(
            commission_per_contract, "commission_per_contract"))
        self.records = []

    def validate_instrument(self, instrument):
        """配置阶段可调用；资料不完整时在开始撮合前失败。"""
        info = instrument.info or {}
        fee = None if self.config.commission_source == "fixed" else FuturesFee.from_mapping(info)
        source = "fut_basic" if fee is not None else "configured_per_contract"
        if fee is None:
            if self.config.commission_source == "metadata" and isinstance(instrument, FuturesContract):
                raise ValueError(f"{instrument.id}: fut_basic缺少交易手续费数值和单位")
            fee = self.fallback
        return fee, source

    def get_commission(self, order, fill_qty, fill_px, instrument):
        fee, source = self.validate_instrument(instrument)
        quantity = fill_qty.as_decimal()
        price = fill_px.as_decimal()
        multiplier = instrument.multiplier.as_decimal()
        coefficient = self.config.resolve(instrument.id)
        charge = fee.charge(quantity, price, multiplier, commission_multiplier=coefficient)
        self.records.append({"instrument_id": str(instrument.id),
            "client_order_id": str(order.client_order_id), "quantity": quantity,
            "fill_price": price, "contract_multiplier": multiplier, "source": source,
            "source_value": fee.source_value, "source_unit": fee.source_unit,
            "per_contract": fee.per_contract, "rate": fee.rate, **asdict(charge)})
        return Money(charge.actual_commission, CNY)
