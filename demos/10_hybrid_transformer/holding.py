from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite

from bomber.framework.calendars.hours import MINUTE_NS

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
    opened_at_ns: int
    expires_at_ns: int


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


def completed_minute_ns(timestamp_ns: int) -> int:
    """统一历史分钟结束边界与实时结束边界减 1ns；禁止猜测开始标签。"""
    if type(timestamp_ns) is not int or timestamp_ns < 0:
        raise ValueError("行情时间须为非负整数纳秒")
    remainder = timestamp_ns % MINUTE_NS
    if remainder == MINUTE_NS - 1:
        return timestamp_ns + 1
    if remainder == 0:
        return timestamp_ns
    raise ValueError("行情须使用已完成分钟的结束边界或结束边界减1ns")


class PeriodHoldings:
    """按日历交易分钟轮换预算；到期由绝对事件时间决定。

    bar_index/opened_bar/expires_bar 仅用于核对交易分钟位置。断线漏收时按
    日历补计经过的分钟，不创建历史开仓；批次 ID 使用建立时的结束时间戳。
    """
    def __init__(self, holding_bars: int, max_contracts: int, *, calendar,
                 max_entry_contracts: int | None = None) -> None:
        if type(holding_bars) is not int or holding_bars < 1:
            raise ValueError("持仓周期须为正整数")
        if type(max_contracts) is not int or max_contracts < 1:
            raise ValueError("总手数上限须为正整数")
        if max_entry_contracts is not None and (type(max_entry_contracts) is not int
                or not 1 <= max_entry_contracts <= max_contracts):
            raise ValueError("每批开仓手数上限须为不超过总上限的正整数")
        self.calendar = calendar
        self.holding_bars = holding_bars
        self.max_contracts = max_contracts
        self.max_entry_contracts = max_entry_contracts
        base, extra = divmod(max_contracts, holding_bars)
        budgets = tuple(base + int(slot < extra) for slot in range(holding_bars))
        self.capacities = (budgets if max_entry_contracts is None else
                           tuple(min(budget, max_entry_contracts) for budget in budgets))
        self.bar_index = -1
        self.last_bar_ns = -1
        self.last_check_ns = -1
        self.active: tuple[HoldingBatch, ...] = ()

    def advance(self, instrument: str, signal: float | None, signal_time: object,
                *, timestamp_ns: int) -> HoldingUpdate:
        if not instrument:
            raise ValueError("批次必须绑定实际合约")
        stamp = completed_minute_ns(timestamp_ns)
        if stamp <= self.last_bar_ns:
            raise ValueError("持仓行情时间重复或倒退")
        # 校验当前戳确为一个完整交易分钟；不能把休市戳视作普通 bar。
        if self.calendar.last_minutes(stamp, 1)[0] != stamp - 1:
            raise ValueError("行情时间不是日历中的已完成交易分钟")
        steps = (1 if self.last_bar_ns < 0 else
                 len(self.calendar.missing_minutes(self.last_bar_ns - 1, stamp)))
        index = self.bar_index + steps
        slot = index % self.holding_bars
        capacity = self.capacities[slot]
        quantity = Decimal(0) if signal is None else signal_to_contracts(signal, capacity)
        # 时钟已前进的情况下，迟到历史 bar 只能用于模型预热，不能补下历史开仓。
        if stamp < self.last_check_ns:
            quantity = Decimal(0)
        check_ns = max(stamp, self.last_check_ns)
        expired = tuple(batch for batch in self.active if batch.expires_at_ns <= check_ns)
        active = tuple(batch for batch in self.active if batch.expires_at_ns > check_ns)
        opened = None
        if quantity:
            expires_ns = self.calendar.next_minutes(stamp - 1, self.holding_bars)[-1] + 1
            opened = HoldingBatch(
                batch_id=stamp, slot=slot, instrument=instrument,
                opened_bar=index, expires_bar=index + self.holding_bars,
                quantity=quantity, signal=float(signal), signal_time=signal_time,
                opened_at_ns=stamp, expires_at_ns=expires_ns)
            active += (opened,)
        update = HoldingUpdate(index, slot, capacity, opened, expired, active)
        if update.gross_contracts > self.max_contracts or len({b.slot for b in active}) != len(active):
            raise RuntimeError("批次预算超限或槽位被重复占用")
        self.bar_index, self.last_bar_ns, self.last_check_ns = index, stamp, check_ns
        self.active = active
        return update

    def expire(self, timestamp_ns: int) -> HoldingUpdate:
        """独立时钟只处理到期；不创建新批次，不依赖模型或成交量。"""
        if type(timestamp_ns) is not int or timestamp_ns < 0:
            raise ValueError("时钟须为非负整数纳秒")
        # 同一分钟稍后送达的完整 bar 仍可在该分钟边界建立新批次。
        check_ns = max(self.last_check_ns, timestamp_ns // MINUTE_NS * MINUTE_NS)
        expired = tuple(b for b in self.active if b.expires_at_ns <= check_ns)
        self.active = tuple(b for b in self.active if b.expires_at_ns > check_ns)
        self.last_check_ns = check_ns
        slot = max(self.bar_index, 0) % self.holding_bars
        return HoldingUpdate(self.bar_index, slot, self.capacities[slot], None, expired, self.active)

    def snapshot_state(self) -> dict:
        batches = []
        for batch in self.active:
            row = asdict(batch)
            row["quantity"] = str(batch.quantity)
            value = batch.signal_time
            row["signal_time"] = value.isoformat() if hasattr(value, "isoformat") else str(value)
            batches.append(row)
        return {"version": 1, "holding_bars": self.holding_bars,
                "max_contracts": self.max_contracts, "max_entry_contracts": self.max_entry_contracts,
                "calendar": self.calendar.provenance["sha256"],
                "bar_index": self.bar_index, "last_bar_ns": self.last_bar_ns,
                "last_check_ns": self.last_check_ns, "active": batches}

    def restore_state(self, state: dict) -> None:
        """先完整校验再赋值；过期批次保留原期限，恢复后的时钟负责清理。"""
        expected = {"version": 1, "holding_bars": self.holding_bars,
                    "max_contracts": self.max_contracts, "max_entry_contracts": self.max_entry_contracts,
                    "calendar": self.calendar.provenance["sha256"]}
        if not isinstance(state, dict) or any(state.get(k) != v for k, v in expected.items()):
            raise ValueError("持仓检查点的版本、预算、周期或交易日历不一致")
        index, last, checked = (state.get(k) for k in ("bar_index", "last_bar_ns", "last_check_ns"))
        if (any(type(v) is not int or v < -1 for v in (index, last, checked))
                or (index == -1) != (last == -1) or checked < last
                or (last >= 0 and (completed_minute_ns(last) != last
                    or self.calendar.last_minutes(last, 1)[0] != last - 1))
                or (checked >= 0 and checked % MINUTE_NS)):
            raise ValueError("持仓检查点计时位置无效")
        rows = state.get("active")
        if not isinstance(rows, list):
            raise ValueError("持仓检查点批次须为列表")
        active = []
        try:
            for row in rows:
                batch = HoldingBatch(**{**row, "quantity": Decimal(row["quantity"])})
                if (any(type(getattr(batch, k)) is not int for k in
                        ("batch_id", "slot", "opened_bar", "expires_bar", "opened_at_ns", "expires_at_ns"))
                        or not 0 <= batch.slot < self.holding_bars
                        or not 0 <= batch.opened_bar <= index
                        or batch.slot != batch.opened_bar % self.holding_bars
                        or batch.expires_bar != batch.opened_bar + self.holding_bars
                        or not 0 <= batch.opened_at_ns <= last
                        or batch.batch_id != batch.opened_at_ns
                        or batch.expires_at_ns <= checked
                        or not isinstance(batch.instrument, str) or not batch.instrument
                        or not isinstance(batch.signal_time, str)
                        or not batch.quantity.is_finite() or batch.quantity == 0
                        or batch.quantity != batch.quantity.to_integral_value()
                        or batch.quantity != signal_to_contracts(batch.signal, self.capacities[batch.slot])
                        or self.calendar.last_minutes(batch.opened_at_ns, 1)[0] != batch.opened_at_ns - 1
                        or self.calendar.next_minutes(batch.opened_at_ns - 1, self.holding_bars)[-1] + 1
                           != batch.expires_at_ns):
                    raise ValueError("批次字段与预算或期限不一致")
                active.append(batch)
            if (len({b.slot for b in active}) != len(active)
                    or len({b.batch_id for b in active}) != len(active)
                    or sum((abs(b.quantity) for b in active), Decimal(0)) > self.max_contracts):
                raise ValueError("批次重复或预算超限")
        except (TypeError, KeyError, ArithmeticError) as exc:
            raise ValueError("持仓检查点批次格式无效") from exc
        self.bar_index, self.last_bar_ns, self.last_check_ns = index, last, checked
        self.active = tuple(active)
