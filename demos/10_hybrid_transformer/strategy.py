"""HybridTransformer 逐 bar 信号到 Bomber 目标仓位的策略层。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite
from typing import Mapping, Protocol

from bomber.framework.datahub.sector_roles import SectorDataUnavailable
from bomber.framework.market.basic.base import CustomBar
from bomber.framework.market.futures import futures_bar_values
from bomber.framework.trader.template import StrategyTemplate


class ModelSignalPort(Protocol):
    """输入完整研究口径 bar，返回 predicted_z 和已转换的 signal。"""

    def on_bar(self, bar: Mapping[str, object]) -> Mapping[str, object] | None: ...


class RoleSnapshotPort(Protocol):
    def snapshot(self, as_of_ns: int): ...

@dataclass(frozen=True)
class HybridTransformerConfig:
    product: str # 交易品种
    venue: str # 交易所表示
    signal_id: str # 信号规则编号  类似为 P → 1101、RB → 1301
    max_contracts: int # 最大目标持仓手数
    rebalance_minutes: int # 目标仓位更新间隔，单位为分钟
    contract_multiplier: Decimal # 合约乘数，即每手对应的标的数量
    target_key: str = "" # 提交目标仓位时使用的路由标识 留空时自动生成，例如 P → p_main、RB → rb_main
    record_signals: bool = False # 默认 False；开启后保存在内存中的 signal_records，方便分析，


    def __post_init__(self) -> None:
        product = self.product.strip().upper()
        venue = self.venue.strip().upper()
        signal_id = str(self.signal_id).strip()
        if not product.isalpha() or not venue or not signal_id:
            raise ValueError("品种、交易所和信号编号不能为空")
        expected_signal = {"P": "1101", "RB": "1301"}.get(product)
        if expected_signal is None or signal_id != expected_signal:
            raise ValueError("当前只固化 P/1101 与 RB/1301 两组策略")
        if (type(self.max_contracts) is not int or self.max_contracts < 1
                or type(self.rebalance_minutes) is not int
                or self.rebalance_minutes < 1 or 60 % self.rebalance_minutes):
            raise ValueError("最大手数须为正整数；调仓间隔须为 60 的正约数")
        multiplier = Decimal(str(self.contract_multiplier))
        if not multiplier.is_finite() or multiplier <= 0:
            raise ValueError("合约乘数须为有限正数，且与成交额单位一致")
        object.__setattr__(self, "product", product)
        object.__setattr__(self, "venue", venue)
        object.__setattr__(self, "signal_id", signal_id)
        object.__setattr__(self, "contract_multiplier", multiplier)
        object.__setattr__(self, "target_key", self.target_key.strip() or f"{product.lower()}_main")

## 信号结合资金换算成手数
def signal_to_contracts(signal: float, max_contracts: int) -> Decimal:
    """连续目标按最邻近整数手量化；一手时 |signal|>=0.5 才开仓。"""
    value = float(signal)
    if not isfinite(value) or abs(value) > 1.0:
        raise ValueError("信号必须是 [-1, 1] 内的有限值")
    magnitude = int((Decimal(str(abs(value))) * max_contracts).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))
    return Decimal(magnitude if value >= 0 else -magnitude)


class HybridTransformerStrategy(StrategyTemplate):
    """仅消费完整研究口径 CustomBar；执行合约与下单由 Bomber 路由。"""

    signal_source = "hybrid_transformer"

    def __init__(self, strategy_id: str, data_hub: RoleSnapshotPort,
                 predictor: ModelSignalPort, config: HybridTransformerConfig) -> None:
        super().__init__(strategy_id)
        self.data_hub = data_hub
        self.predictor = predictor
        self.config = config
        predictor_signal_id = getattr(predictor, "signal_id", None)
        if predictor_signal_id is not None and str(predictor_signal_id) != config.signal_id:
            raise ValueError("预测器信号编号与策略配置不一致")
        self.last_processed_ns = -1
        self.last_main: str | None = None
        self.last_target: Decimal | None = None
        self.last_target_main: str | None = None
        self.last_prediction: dict | None = None
        self.signal_records: list[dict] = []
        self.bars_used = 0
        self.unavailable_events = 0
        self.order_updates_received = 0
        self.fills_received = 0

    def on_order(self, event) -> None:
        self.order_updates_received += 1

    def on_fill(self, event) -> None:
        self.fills_received += 1

    def on_custom_bar(self, data_key: str, event: CustomBar) -> None:
        timestamp = int(event.ts_event)
        if timestamp <= self.last_processed_ns:
            return

        instrument = event.bar.bar_type.instrument_id
        if data_key != str(instrument):
            raise ValueError("CustomBar 与 data_key 的合约不一致")

        try:
            assignment = self.data_hub.snapshot(timestamp)
            main = assignment.instrument(self.config.product, "main").lower()
        except SectorDataUnavailable:
            self.unavailable_events += 1
            return
        if (str(instrument.symbol).lower() != main
                or str(instrument.venue).upper() != self.config.venue):
            return

        values = futures_bar_values(
            event, contract_multiplier=self.config.contract_multiplier)
        trade_time = event.factors.get("trade_time")
        if trade_time is None:
            raise ValueError("模型 CustomBar 必须保留原始 trade_time 标签")
        model_bar = {"trade_time": trade_time, "code": self.config.product, **values}
        prediction = self.predictor.on_bar(model_bar)
        self.last_processed_ns = timestamp
        self.last_main = main
        self.bars_used += 1
        if prediction is None:  # 原始 bar 和序列仍在预热，不提交任何目标。
            return
        if str(prediction.get("code", self.config.product)).upper() != self.config.product:
            raise ValueError("模型预测品种与策略品种不一致")
        predicted_z = float(prediction["predicted_z"])
        signal = float(prediction["signal"])
        if not isfinite(predicted_z):
            raise ValueError("模型 predicted_z 非有限值")
        if str(prediction.get("signal_id", self.config.signal_id)) != self.config.signal_id:
            raise ValueError("模型输出的 signal_id 与固化配置不一致")
        self.last_prediction = dict(prediction)

        # 逐 bar 都预测和转信号；是否提交目标由已固化的执行节奏决定。
        minute = getattr(trade_time, "minute", None)
        if minute is None:
            from datetime import datetime
            minute = datetime.fromisoformat(str(trade_time)).minute
        target = signal_to_contracts(signal, self.config.max_contracts)
        if self.config.record_signals:
            self.signal_records.append({
                "trade_time": trade_time, "code": self.config.product,
                "research_main": main, "predicted_z": predicted_z,
                "signal": signal, "target_contracts": int(target),
                "rebalance_due": minute % self.config.rebalance_minutes == 0,
            })

        if minute % self.config.rebalance_minutes:
            return
        if target == self.last_target and main == self.last_target_main:
            return
        self.set_target(self.config.target_key, target, timestamp, metadata={
            "signal_source": self.signal_source,
            "signal_id": self.config.signal_id,
            "predicted_z": predicted_z,
            "signal": signal,
            "research_main": main,
            "signal_time": str(trade_time),
            "target_rounding": "nearest_integer_half_up",
        })
        self.last_target = target
        self.last_target_main = main