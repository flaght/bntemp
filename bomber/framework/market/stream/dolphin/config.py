"""DolphinDB 行情流表的配置与精确字段结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Literal


StreamKind = Literal["tick", "bar"]

TICK_COLUMNS = (
    "date",
    "Code",
    "TradingDay",
    "ActionDay",
    "tradeTime",
    "OpenPrice",
    "HighestPrice",
    "LowestPrice",
    "LastPrice",
    "AveragePrice",
    "Volume",
    "Turnover",
    "BidPrice1",
    "BidVolume1",
    "AskPrice1",
    "AskVolume1",
    "PreSettlementPrice",
    "PreClosePrice",
    "PreOpenInterest",
    "OpenInterest",
    "UpperLimitPrice",
    "LowerLimitPrice",
    "UpdateTime",
    "UpdateMillisec",
    "receivedTime",
    "perPenetrationTime",
)

BAR_COLUMNS = (
    "minTime",
    "date",
    "Code",
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "prev_settlement",
    "volume",
    "turnover",
    "pct_change",
    "open_interest",
    "prev_open_interest",
    "updateTime",
)
BAR_REQUIRED_COLUMNS = ("minTime", "date", "Code", "open", "high", "low", "close", "volume")
BAR_STATE_REQUIRED_COLUMNS = ("barTime", "date", "Code", "open", "high", "low", "close",
    "volume", "turnover", "open_interest", "volume_baseline_ok", "updateTime")


@dataclass(frozen=True)
class DolphinDbStreamSpec:
    kind: StreamKind
    table_name: str
    action_name: str
    columns: tuple[str, ...]
    offset: int = -1
    resub: bool = True
    batch_size: int = 0
    throttle: float = 0.01
    filter: Any = field(default=None, compare=False, hash=False, repr=False)
    bar_spec: str = "1-MINUTE"
    auto_schema: bool = False
    volume_mode: Literal["per-bar", "cumulative"] = "per-bar"
    local_receipt_time: bool = False
    bar_mode: str = "completed"

    def __post_init__(self) -> None:
        if self.kind not in {"tick", "bar"}:
            raise ValueError(f"unsupported DolphinDB stream kind: {self.kind}")
        if not self.table_name or not self.action_name:
            raise ValueError("table_name and action_name are required")
        if re.fullmatch(r"[A-Za-z0-9_]+", self.action_name) is None:
            raise ValueError("DolphinDB action_name只能包含字母、数字和下划线")
        expected = TICK_COLUMNS if self.kind == "tick" else BAR_COLUMNS
        if self.auto_schema and (self.kind != "bar" or self.columns):
            raise ValueError("auto_schema requires a Bar stream with server-resolved columns")
        if not self.auto_schema and self.columns != expected:
            raise ValueError(
                f"{self.kind} columns do not match the exp5 schema; "
                f"expected {expected!r}",
            )
        if self.bar_mode not in {"completed", "state"}:
            raise ValueError("Bar模式须为completed/state")
        if self.bar_mode == "state" and (self.kind != "bar" or not self.auto_schema
                or self.volume_mode != "per-bar" or self.bar_spec.upper() != "1-MINUTE"):
            raise ValueError("分钟状态流须为自动字段、1-MINUTE和per-bar量额")
        if self.batch_size < 0:
            raise ValueError("batch_size must be non-negative")
        if self.throttle < 0:
            raise ValueError("throttle must be non-negative")
        if self.volume_mode not in {"per-bar", "cumulative"}:
            raise ValueError("Bar volume_mode must be per-bar or cumulative")

    @classmethod
    def tick(
        cls,
        table_name: str,
        action_name: str = "bomberTick",
        **options: Any,
    ) -> "DolphinDbStreamSpec":
        return cls("tick", table_name, action_name, TICK_COLUMNS, **options)

    @classmethod
    def bar(
        cls,
        table_name: str,
        action_name: str = "bomberBar",
        **options: Any,
    ) -> "DolphinDbStreamSpec":
        return cls("bar", table_name, action_name,
                   () if options.get("auto_schema", False) else BAR_COLUMNS, **options)


@dataclass(frozen=True)
class DolphinDbConfig:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    streams: tuple[DolphinDbStreamSpec, ...] = ()
    streaming_port: int = 0
    keep_alive_seconds: int = 60

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("DolphinDB host is required")
        if not 0 < self.port <= 65535:
            raise ValueError("DolphinDB port must be between 1 and 65535")
        if not self.streams:
            raise ValueError("at least one DolphinDB stream is required")
        topics = [(stream.table_name, stream.action_name) for stream in self.streams]
        if len(topics) != len(set(topics)):
            raise ValueError("DolphinDB table/action subscription topics must be unique")
