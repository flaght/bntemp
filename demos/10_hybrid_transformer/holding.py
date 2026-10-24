from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite

@dataclass(frozen=True)
class HoldingBatch:
    batch_id: int
    slot: int
    instrument: str
    opened_bar: int
    expires_bar: int
    quantity: Decimal
    signal: float
    signal_time: object


@dataclass(frozen=True)
class HoldingUpdate:
    bar_index: int
    slot: int
    capacity: int
    opened: HoldingBatch | None
    expired: tuple[HoldingBatch, ...]
    active: tuple[HoldingBatch, ...]

    @property
    def targets(self) -> dict[str, Decimal]:
        result: dict[str, Decimal] = {}
        for batch in self.active:
            result[batch.instrument] = result.get(batch.instrument, Decimal(0)) + batch.quantity
        return result

    @property
    def gross_contracts(self) -> Decimal:
        return sum((abs(batch.quantity) for batch in self.active), Decimal(0))


## 信号结合资金换算成手数
def signal_to_contracts(signal: float, max_contracts: int) -> Decimal:
    """按当前槽位的整数手预算量化；零预算槽位不建立仓位。"""
    value = float(signal)
    if not isfinite(value) or abs(value) > 1.0:
        raise ValueError("信号必须是 [-1, 1] 内的有限值")
    if type(max_contracts) is not int or max_contracts < 0:
        raise ValueError("槽位手数预算须为非负整数")
    magnitude = int((Decimal(str(abs(value))) * max_contracts).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))
    return Decimal(magnitude if value >= 0 else -magnitude)


class PeriodHoldings:
    """每个有效主力分钟推进一次；休市/缺失分钟不制造 bar。

    五个槽位固定轮换，整数预算之和为 max_contracts。余数依次分配给
    前几个槽位，例如 11 -> (3,2,2,2,2)，1 -> (1,0,0,0,0)。每批在
    opened_bar + 5 到期。signal=None 仅停止新开仓，不阻止旧批次到期。
    """
    def __init__(self, holding_bars: int, max_contracts: int) -> None:
        if type(max_contracts) is not int or max_contracts < 1:
            raise ValueError("总手数上限须为正整数")
        self.holding_bars = holding_bars
        self.max_contracts = max_contracts
        base, extra = divmod(max_contracts, holding_bars)
        self.capacities = tuple(base + int(slot < extra) for slot in range(holding_bars))
        self.bar_index = -1
        self.active: tuple[HoldingBatch, ...] = ()

    def advance(self, instrument: str, signal: float, signal_time: object) -> HoldingUpdate:
        if not instrument:
            raise ValueError("批次必须绑定实际合约")
        
        index = self.bar_index + 1
        slot = index % self.holding_bars
        capacity = self.capacities[slot]

        # 先校验新信号，避免错误输入导致旧批次被部分移除。
        quantity = Decimal(0) if signal is None else signal_to_contracts(signal, capacity)
        expired = tuple(batch for batch in self.active if batch.expires_bar <= index)
        active = tuple(batch for batch in self.active if batch.expires_bar > index)

        opened = None
        if quantity:
            opened = HoldingBatch(index, slot, instrument, index, index + self.holding_bars,
                                  quantity, float(signal), signal_time)
            active += (opened,)
        update = HoldingUpdate(index, slot, capacity, opened, expired, active)
        if update.gross_contracts > self.max_contracts:
            raise RuntimeError("批次绝对手数之和超过总手数上限")
        self.bar_index, self.active = index, active
        return update
