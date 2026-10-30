"""HybridTransformer 逐 bar 信号到 Bomber 目标仓位的策略层。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from typing import Mapping, Protocol

from bomber.framework.datahub.sector_roles import SectorDataUnavailable
from bomber.framework.market.basic.base import CustomBar
from bomber.framework.market.futures import futures_bar_values
from bomber.framework.trader.template import StrategyTemplate

from .holding import PeriodHoldings, completed_minute_ns


class ModelSignalPort(Protocol):
    """输入完整研究口径 bar，返回 predicted_z 和已转换的 signal。"""

    def on_bar(self, bar: Mapping[str, object]) -> Mapping[str, object] | None: ...


class RoleSnapshotPort(Protocol):
    def snapshot(self, as_of_ns: int): ...

@dataclass(frozen=True)
class HybridTransformerConfig:
    product: str # 交易品种
    venue: str # 交易所表示
    signal_id: str # 外部配置指定的信号规则编号
    max_contracts: int # 所有未到期批次的绝对手数总上限
    hold_bars: int # 持仓周期
    rebalance_minutes: int # 新批次开仓间隔；到期始终处理
    contract_multiplier: Decimal # 合约乘数，即每手对应的标的数量
    target_key: str = "" # 提交目标仓位时使用的路由标识 留空时自动生成，例如 P → p_main、RB → rb_main
    record_signals: bool = False # 默认 False；开启后保存在内存中的 signal_records，方便分析，
    max_entry_contracts: int | None = None # 每批开仓上限；Live显式设为1，研究入口沿用原预算


    def __post_init__(self) -> None:
        product = self.product.strip().upper()
        venue = self.venue.strip().upper()
        signal_id = str(self.signal_id).strip()
        if not product.isalpha() or not venue or not signal_id:
            raise ValueError("品种、交易所和信号编号不能为空")
        if type(self.hold_bars) is not int or self.hold_bars < 1:
            raise ValueError("持仓周期须为正整数")
        if (type(self.max_contracts) is not int or self.max_contracts < 1
                or type(self.rebalance_minutes) is not int
                or self.rebalance_minutes < 1 or 60 % self.rebalance_minutes):
            raise ValueError("最大手数须为正整数；调仓间隔须为 60 的正约数")
        if self.max_entry_contracts is not None and (type(self.max_entry_contracts) is not int
                or not 1 <= self.max_entry_contracts <= self.max_contracts):
            raise ValueError("每批开仓上限须为不超过总手数上限的正整数")
        multiplier = Decimal(str(self.contract_multiplier))
        if not multiplier.is_finite() or multiplier <= 0:
            raise ValueError("合约乘数须为有限正数，且与成交额单位一致")
        object.__setattr__(self, "product", product)
        object.__setattr__(self, "venue", venue)
        object.__setattr__(self, "signal_id", signal_id)
        object.__setattr__(self, "contract_multiplier", multiplier)
        object.__setattr__(self, "target_key", self.target_key.strip() or f"{product.lower()}_main")


class HybridTransformerStrategy(StrategyTemplate):
    """仅消费完整研究口径 CustomBar；执行合约与下单由 Bomber 路由。"""

    signal_source = "hybrid_transformer"

    def __init__(self, strategy_id: str, data_hub: RoleSnapshotPort,
                 predictor: ModelSignalPort, config: HybridTransformerConfig,
                 target_keys: Mapping[str, str] | None = None, *, calendar) -> None:
        super().__init__(strategy_id)
        self.data_hub = data_hub
        self.predictor = predictor
        self.config = config
        if calendar.product.upper() != config.product:
            raise ValueError("持仓交易日历与策略品种不一致")
        self.calendar = calendar
        self.holdings = PeriodHoldings(holding_bars=config.hold_bars,
            max_contracts=config.max_contracts, calendar=calendar, max_entry_contracts=config.max_entry_contracts)
        self._targets_pending = False
        self._checkpoint = None
        self.target_keys = ({str(symbol).lower(): str(key).strip()
                             for symbol, key in target_keys.items()}
                            if target_keys is not None else None)
        if self.target_keys is not None and (not self.target_keys
                or any(not key for key in self.target_keys.values())
                or len(set(self.target_keys.values())) != len(self.target_keys)):
            raise ValueError("真实合约须映射到非空、互不相同的目标键")

        self.last_targets: dict[str, Decimal] = {}
        self.batch_records: list[dict] = []
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


    def bind_checkpoint(self, save) -> None:
        """装配时接入 RuntimeStateManager.save；目标不变的批次续期也必须落盘。"""
        if not callable(save) or self._checkpoint is not None:
            raise ValueError("持久化回调须可调用且只能绑定一次")
        self._checkpoint = save

    def _save_checkpoint(self) -> None:
        if self._checkpoint is not None:
            self._checkpoint()

    def on_order(self, event) -> None:
        self.order_updates_received += 1

    def on_fill(self, event) -> None:
        self.fills_received += 1

    def on_custom_bar(self, data_key: str, event: CustomBar) -> None:
        timestamp = int(event.ts_event)
        if (self.last_processed_ns >= 0 and completed_minute_ns(timestamp)
                <= completed_minute_ns(self.last_processed_ns)):
            return

        instrument = event.bar.bar_type.instrument_id
        if data_key != str(instrument):
            raise ValueError("CustomBar 与 data_key 的合约不一致")

        try:
            assignment = self.data_hub.snapshot(timestamp)
            main = assignment.instrument(self.config.product, "main").lower()
        except SectorDataUnavailable:
            self.unavailable_events += 1
            self.on_time(completed_minute_ns(timestamp))
            return
        if (str(instrument.symbol).lower() != main
                or str(instrument.venue).upper() != self.config.venue):
            return

        if self.target_keys is not None and main not in self.target_keys:
            raise ValueError(f"主力合约 {main} 缺少静态目标路由")
        if self.target_keys is None and self.last_main not in (None, main):
            raise ValueError("跨合约持仓须提供逐合约 target_keys，不能提前迁移旧批次")

        try:
            values = futures_bar_values(
                event, contract_multiplier=self.config.contract_multiplier)

            trade_time = event.factors.get("trade_time")
            if trade_time is None:
                raise ValueError("模型 CustomBar 必须保留原始 trade_time 标签")

            model_bar = {"trade_time": trade_time, "code": self.config.product, **values}
            prediction = self.predictor.on_bar(model_bar)

            predicted_z = signal = None
            if prediction is not None:
                if str(prediction.get("code", self.config.product)).upper() != self.config.product:
                    raise ValueError("模型预测品种与策略品种不一致")
                predicted_z = float(prediction["predicted_z"])
                signal = float(prediction["signal"])
                if not isfinite(predicted_z) or not isfinite(signal) or abs(signal) > 1:
                    raise ValueError("模型预测/信号非有限值或信号越界")
                if str(prediction.get("signal_id", self.config.signal_id)) != self.config.signal_id:
                    raise ValueError("模型输出的 signal_id 与固化配置不一致")
                self.last_prediction = dict(prediction)

            minute = getattr(trade_time, "minute", None)
            if minute is None:
                from datetime import datetime
                minute = datetime.fromisoformat(str(trade_time)).minute
            admission_due = minute % self.config.rebalance_minutes == 0

        except Exception:
            # 模型预热错误或坏行情不能阻止已有批次到期。
            self.on_time(completed_minute_ns(timestamp))
            raise

        # 每根有效主力 bar 都推进到期账本；预热或非开仓时点也不能延长旧批次。
        try:
            update = self.holdings.advance(main, signal if admission_due else None, trade_time,
                                           timestamp_ns=timestamp)
        except Exception:
            self.on_time(completed_minute_ns(timestamp))
            raise

        self.last_processed_ns = timestamp
        self.last_main = main
        self.bars_used += 1

        self._apply_holding_update(update, timestamp, trade_time, prediction,
                                   predicted_z, signal, admission_due)

    def on_time(self, ts_event: int) -> None:
        """由运行时独立时钟驱动；恢复后先处理到期，再允许新信号。"""
        previous_check = self.holdings.last_check_ns
        update = self.holdings.expire(ts_event)
        if update.expired or self._targets_pending:
            from bomber.framework.calendars.sessions import ns_datetime
            self._apply_holding_update(update, ts_event,
                ns_datetime(ts_event, self.calendar.zone), None, None, None, False)
        elif self.holdings.last_check_ns != previous_check:
            self._save_checkpoint()

    def _apply_holding_update(self, update, timestamp, trade_time,
                              prediction, predicted_z, signal, admission_due) -> None:
        main = self.last_main
        active_targets = update.targets
        symbols = set(self.last_targets) | set(active_targets)
        if prediction is not None:
            symbols.add(main)

        targets = {symbol: active_targets.get(symbol, Decimal(0)) for symbol in sorted(symbols)}
        target = sum(targets.values(), Decimal(0))


        if self.config.record_signals:
            import json
            self.signal_records.append({
                "trade_time": trade_time, "code": self.config.product,
                "research_main": main, "predicted_z": predicted_z, "signal": signal,
                "target_contracts": int(target), "gross_contracts": int(update.gross_contracts),
                "active_batches": len(update.active), "bar_index": update.bar_index,
                "slot": update.slot, "slot_capacity": update.capacity,
                "opened_contracts": int(update.opened.quantity) if update.opened else 0,
                "expired_contracts": int(sum((batch.quantity for batch in update.expired), Decimal(0))),
                "rebalance_due": admission_due, "holding_bars": self.config.hold_bars,
                "targets_by_contract": json.dumps({key: int(value) for key, value in targets.items()}),
            })

            for event_name, batches in (("expire", update.expired),
                                        ("open", (update.opened,) if update.opened else ())):
                for batch in batches:
                    self.batch_records.append({
                        "event": event_name, "observed_bar": update.bar_index,
                        "trade_time": trade_time, "batch_id": batch.batch_id,
                        "slot": batch.slot, "research_main": batch.instrument,
                        "opened_bar": batch.opened_bar, "expires_bar": batch.expires_bar,
                        "quantity": int(batch.quantity), "signal": batch.signal,
                        "signal_time": batch.signal_time,
                        "opened_at_ns": batch.opened_at_ns, "expires_at_ns": batch.expires_at_ns,
                    })

        # 账本已记录旧批次到期/新批次建立；同合约净目标不变时无需重复交易。
        if not targets or (targets == self.last_targets and not self._targets_pending):
            self._save_checkpoint()
            return

        routed = {(self.target_keys[symbol] if self.target_keys is not None else self.config.target_key): quantity
                  for symbol, quantity in targets.items()}
        # 写前检查点必须同时看到新账本、新目标和模板递增后的 revision。
        self.last_targets = targets
        self.last_target = target
        self.last_target_main = main
        self._targets_pending = True
        self.set_targets(routed, timestamp, metadata={
            "signal_source": self.signal_source, "signal_id": self.config.signal_id,
            "predicted_z": predicted_z, "signal": signal, "research_main": main,
            "signal_time": str(trade_time), "holding_bars": self.config.hold_bars,
            "bar_index": update.bar_index, "gross_contracts": str(update.gross_contracts),
            "target_rounding": "per_slot_nearest_integer_half_up",
        })
        self._targets_pending = False
        self._save_checkpoint()
