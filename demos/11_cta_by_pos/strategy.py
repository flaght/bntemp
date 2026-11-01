"""目标文件驱动 CTA；读取目标、判断信号和提交仓位均由策略负责。"""

from __future__ import annotations

import calendar
import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from bomber.framework.trader.contracts import TargetUpdateMode
from bomber.framework.trader.template import StrategyTemplate


@dataclass(frozen=True)
class CtaByPosConfig:
    signal_time: str = "09:40"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.signal_time):
            raise ValueError("signal_time 须为 HH:MM")


class CtaByPosStrategy(StrategyTemplate):
    def __init__(self, strategy_id: str, positions_path: Path | str,
                 config: CtaByPosConfig | None = None, *,
                 start_day: date | None = None, end_day: date | None = None) -> None:
        super().__init__(strategy_id)
        if start_day and end_day and start_day > end_day:
            raise ValueError("日期范围无效")
        self.config = config or CtaByPosConfig()
        self._tz = ZoneInfo("Asia/Shanghai")
        self._positions_map: dict[date, dict[str, Decimal]] = {}
        self._last_timestamp = -1
        self._handled_days: set[date] = set()
        self.target_records: list[dict] = []
        self.skipped_expired: list[dict] = []
        self._load_positions(Path(positions_path), start_day, end_day)

    def _load_positions(self, path: Path, start_day: date | None, end_day: date | None) -> None:
        """按日加载目标；同日同合约最后一行覆盖，文件内时间不作调度。"""
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            required = {"timestamp", "instrument_id", "target_qty"}
            if len(fields) != len(set(fields)) or required - set(fields):
                raise ValueError("持仓文件须包含不重复的 timestamp,instrument_id,target_qty 列")
            for number, row in enumerate(reader, 2):
                try:
                    day = datetime.fromisoformat(row["timestamp"]).date()
                    symbol = row["instrument_id"].strip()
                    quantity = Decimal(row["target_qty"])
                except (ValueError, InvalidOperation, TypeError, AttributeError) as exc:
                    raise ValueError(f"持仓文件第 {number} 行无效") from exc
                if (start_day and day < start_day) or (end_day and day > end_day):
                    continue
                if (not re.fullmatch(r"(?:IC|IF|IH|IM)\d{4}", symbol)
                        or row.get("instrument_type", "FUTURE") != "FUTURE"):
                    raise ValueError(f"第 {number} 行须为 IC/IF/IH/IM 股指期货")
                if not quantity.is_finite() or quantity != quantity.to_integral_value():
                    raise ValueError(f"第 {number} 行目标手数须为有限整数")
                date(2000 + int(symbol[2:4]), int(symbol[4:6]), 1)
                self._positions_map.setdefault(day, {})[symbol] = quantity
        self._all_insts = sorted({symbol for targets in self._positions_map.values() for symbol in targets})
        if not self._all_insts:
            raise ValueError("请求区间没有目标持仓记录")

    def get_insts(self) -> list[str]:
        """运行入口据此注册真实合约和订阅行情。"""
        return list(self._all_insts)

    @property
    def max_target_quantity(self) -> Decimal:
        return max(abs(qty) for targets in self._positions_map.values() for qty in targets.values())

    @property
    def target_dates(self) -> tuple[date, ...]:
        return tuple(sorted(self._positions_map))

    @property
    def handled_days(self) -> frozenset[date]:
        return frozenset(self._handled_days)

    def _is_contract_expired(self, symbol: str, timestamp_ns: int) -> bool:
        """保留原策略的第三周五 15:00 过滤规则。"""
        year, month = 2000 + int(symbol[2:4]), int(symbol[4:6])
        fridays = [week[4] for week in calendar.monthcalendar(year, month) if week[4]]
        expiry = datetime(year, month, fridays[2], 15, tzinfo=self._tz)
        return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, self._tz) > expiry

    def on_bar(self, data_key: str, bar) -> None:
        del data_key
        timestamp = int(bar.ts_event)
        if timestamp < self._last_timestamp:
            raise ValueError("Bar 时间不能倒退")
        if timestamp == self._last_timestamp:
            return
        self._last_timestamp = timestamp
        stamp = datetime.fromtimestamp(timestamp / 1_000_000_000, self._tz)
        day = stamp.date()
        if (stamp.strftime("%H:%M") != self.config.signal_time or day in self._handled_days
                or day not in self._positions_map):
            return
        self._handled_days.add(day)
        targets, expired = {}, []
        for symbol, quantity in self._positions_map[day].items():
            if quantity != 0 and self._is_contract_expired(symbol, timestamp):
                expired.append(symbol)
                self.skipped_expired.append({"ts_event": timestamp, "instrument_id": symbol})
            else:
                targets[symbol] = quantity
        if not targets:
            return
        self.set_targets(
            targets, timestamp, update_mode=TargetUpdateMode.REPLACE,
            metadata={"signal_source": "cta_by_pos", "trading_day": str(day),
                      "skipped_expired": tuple(expired)},
        )
        self.target_records.extend({"ts_event": timestamp,
            "trading_day": str(day), "instrument_id": symbol, "target_qty": quantity}
            for symbol, quantity in targets.items())
