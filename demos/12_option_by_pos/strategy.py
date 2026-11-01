"""按文件内分钟时点回放期货/期权目标，执行与结算交给公共框架。"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from bomber.framework.trader.contracts import TargetUpdateMode
from bomber.framework.trader.template import StrategyTemplate

FUTURE = re.compile(r"(?:IC|IF|IH|IM)\d{2}(?:0[1-9]|1[0-2])")
OPTION = re.compile(r"(?:MO|IO|HO)\d{2}(?:0[1-9]|1[0-2])-[CP]-[1-9]\d*")
ZONE = ZoneInfo("Asia/Shanghai")


def timestamp_ns(value):
    text = str(value).strip()
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        stamp = datetime.strptime(text, "%Y/%m/%d %H:%M")
    stamp = stamp.replace(tzinfo=ZONE) if stamp.tzinfo is None else stamp.astimezone(ZONE)
    if stamp.second or stamp.microsecond:
        raise ValueError("目标时间须对齐整分钟")
    delta = stamp.astimezone(ZoneInfo("UTC")) - datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000


@dataclass(frozen=True)
class OptionByPosConfig:
    legacy_close_early: bool = False
    missing_signal_policy: str = "error"

    def __post_init__(self):
        if self.missing_signal_policy not in {"error", "skip"}:
            raise ValueError("missing_signal_policy must be error or skip")


class OptionByPosStrategy(StrategyTemplate):
    def __init__(self, strategy_id, positions_path, config=None, *, start_day=None, end_day=None):
        super().__init__(strategy_id)
        self.config = config or OptionByPosConfig()
        self.positions_path = Path(positions_path)
        self.schedule = {}
        self.instrument_types = {}
        self.legacy_slots = set()
        if start_day and end_day and start_day > end_day:
            raise ValueError("日期范围无效")
        with self.positions_path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or {"timestamp", "instrument_id", "target_qty"} - set(fields):
                raise ValueError("目标文件必须包含不重复的 timestamp,instrument_id,target_qty 列")
            for number, row in enumerate(reader, 2):
                try:
                    ts = timestamp_ns(row["timestamp"])
                    stamp = datetime.fromtimestamp(ts / 1e9, ZONE)
                    symbol = row["instrument_id"].strip().upper().removesuffix(".CFFEX")
                    quantity = Decimal(row["target_qty"])
                except (ValueError, TypeError, ArithmeticError, AttributeError) as exc:
                    raise ValueError(f"目标文件第 {number} 行无效") from exc
                if (start_day and stamp.date() < start_day) or (end_day and stamp.date() > end_day):
                    continue
                kind = "OPTION" if OPTION.fullmatch(symbol) else "FUTURE" if FUTURE.fullmatch(symbol) else None
                if kind is None or row.get("instrument_type", kind) != kind:
                    raise ValueError(f"第 {number} 行合约类型无效: {symbol}")
                if not quantity.is_finite() or quantity != quantity.to_integral_value():
                    raise ValueError(f"第 {number} 行目标手数必须是有限整数")
                if self.config.legacy_close_early and stamp.strftime("%H:%M") == "15:00":
                    ts -= 2 * 60 * 1_000_000_000
                    self.legacy_slots.add(ts)
                targets = self.schedule.setdefault(ts, {})
                if symbol in targets:
                    raise ValueError(f"第 {number} 行目标时间和合约重复: {symbol}")
                targets[symbol] = quantity
                self.instrument_types[symbol] = kind
        if not self.schedule:
            raise ValueError("请求区间没有目标记录")
        active = {symbol for targets in self.schedule.values() for symbol, qty in targets.items() if qty}
        if not active:
            raise ValueError("请求区间没有非零目标（本回测从空仓开始）")
        self.instrument_types = {key: value for key, value in self.instrument_types.items() if key in active}
        self.goals = {symbol: Decimal(0) for symbol in active}
        self.last_trade_ns = {}
        self.latest = {}
        self._last_event_ns = -1
        self._pending = None
        self.handled_slots = set()
        self.target_records = []
        self.skipped_slots = []
        self.settlement_records = []

    def get_insts(self):
        return sorted(self.instrument_types)

    @property
    def max_target_quantity(self):
        return max(abs(qty) for targets in self.schedule.values() for qty in targets.values())

    def set_lifecycles(self, last_trade_ns):
        if set(self.get_insts()) - set(last_trade_ns):
            raise ValueError("缺少合约最后交易时刻")
        self.last_trade_ns = dict(last_trade_ns)

    def _missed(self, signal_ns):
        targets = self.schedule[signal_ns]
        missing = [key for key in targets if key in self.goals
                   and signal_ns < self.last_trade_ns.get(key, 2**63 - 1)
                   and self.latest.get(key) != signal_ns]
        detail = dict(signal_ns=signal_ns, missing=missing, reason="missing_signal_bars")
        self.skipped_slots.append(detail)
        self.handled_slots.add(signal_ns)
        self._pending = None
        if self.config.missing_signal_policy == "error":
            raise RuntimeError(f"目标时点缺少对应合约行情: {detail}")

    def on_bar(self, data_key, bar):
        symbol = str(bar.bar_type.instrument_id).removesuffix(".CFFEX")
        signal_ns = int(bar.ts_event)
        observed_ns = int(bar.ts_init)
        if observed_ns < self._last_event_ns:
            raise ValueError("Bar 可用时间不能倒退")
        self._last_event_ns = observed_ns
        if self._pending is not None and signal_ns > self._pending:
            self._missed(self._pending)
        self.latest[symbol] = signal_ns
        if signal_ns not in self.schedule or signal_ns in self.handled_slots:
            return
        self._pending = signal_ns
        targets = {key: qty for key, qty in self.schedule[signal_ns].items() if key in self.goals}
        needed = {key for key in targets if signal_ns < self.last_trade_ns.get(key, 2**63 - 1)}
        if signal_ns in self.legacy_slots:
            # 旧规则允许在期货M1时点用最后已知期权报价提交意图；执行层仍等新M5。
            ready = all(key in self.latest for key in needed)
        else:
            ready = all(self.latest.get(key) == signal_ns for key in needed)
        if not ready:
            return
        for key, kind in self.instrument_types.items():
            if kind == "FUTURE":
                self.goals[key] = targets.get(key, Decimal(0))
            elif key in targets:
                self.goals[key] = targets[key]
            if signal_ns >= self.last_trade_ns.get(key, 2**63 - 1):
                if targets.get(key, 0):
                    raise ValueError(f"到期后存在非零目标: {key}")
                # 已停交易但尚未现金结算的仓位不得再生成普通平仓单。
                self.goals[key] = self.account_position(key) + self.working_quantity(key)
        self.set_targets(dict(self.goals), observed_ns, update_mode=TargetUpdateMode.REPLACE,
            metadata={"signal_source": "option_by_pos", "signal_ns": signal_ns})
        self.target_records.extend(dict(signal_ns=signal_ns, request_ns=observed_ns,
            instrument_id=key, target_qty=qty) for key, qty in sorted(self.goals.items()))
        self.handled_slots.add(signal_ns)
        self._pending = None

    def on_fill(self, event):
        if event.metadata.get("execution_origin") == "option_expiry":
            symbol = event.identity.instrument_id.removesuffix(".CFFEX")
            if symbol in self.goals:
                self.goals[symbol] = Decimal(0)
            self.settlement_records.append(dict(instrument_id=symbol, ts_event=event.ts_event,
                price=str(event.price), quantity=str(event.quantity)))

    def verify_signals(self):
        missing = sorted(set(self.schedule) - self.handled_slots)
        if missing and self.config.missing_signal_policy == "error":
            raise RuntimeError(f"目标时点未完成提交: {missing[:5]}")
        return missing
