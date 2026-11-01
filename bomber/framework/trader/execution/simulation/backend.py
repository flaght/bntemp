"""以Nautilus BacktestEngine为内核的通用模拟执行Backend。"""

from __future__ import annotations

from typing import Any, Callable, Sequence
from pathlib import Path

from bomber.backtest.config import BacktestEngineConfig
from bomber.backtest.engine import BacktestEngine

from bomber.framework.trader.execution.contracts import ExecutionBackendKind, ExecutionReport, OrderIntent
from bomber.framework.trader.execution.ports import VenueSimulationProfilePort
from bomber.framework.trader.execution.simulation.gateway import NautilusOrderGateway


# Rust 全局日志器不能在关闭后重新注册。保留首个引擎提供的公开日志守卫，
# 让连续回测复用进程日志；不保留引擎，日志线程随进程结束统一关闭。
_PROCESS_LOG_GUARD: Any = None


class NautilusSimExecutionBackend:
    """统一拥有BacktestEngine及其配置、行情推进和释放过程。

    E5b建立引擎所有权和生命周期；E5c通过内部
    ``NautilusOrderGateway``接入订单意图、原生订单和统一执行回报。
    """

    kind = ExecutionBackendKind.SIMULATION

    def __init__(
        self,
        backend_id: str,
        config: BacktestEngineConfig | None = None,
        *,
        engine: Any | None = None,
        execution_timing: str = "native",
        execution_delay_bars: int | None = None,
    ) -> None:
        if not backend_id.strip():
            raise ValueError("backend_id不能为空")
        if execution_timing not in {"native", "next_market"}:
            raise ValueError("execution_timing必须为native或next_market")
        if execution_delay_bars is None:
            execution_delay_bars = 0 if execution_timing == "native" else 1
        if (isinstance(execution_delay_bars, bool) or not isinstance(execution_delay_bars, int)
                or execution_delay_bars < 0):
            raise ValueError("execution_delay_bars必须为非负整数")
        if execution_timing == "native" and execution_delay_bars != 0:
            raise ValueError("native模式的execution_delay_bars必须为0")
        if execution_timing == "next_market" and execution_delay_bars < 1:
            raise ValueError("next_market模式的execution_delay_bars必须至少为1")
        self.backend_id = backend_id
        # native保持原有提交行为；next_market仅在本合约严格更晚的新行情后放行。
        self._execution_timing = execution_timing
        self._execution_delay_bars = execution_delay_bars
        self.engine = (
            engine
            if engine is not None
            else BacktestEngine(config=config or BacktestEngineConfig())
        )
        global _PROCESS_LOG_GUARD
        if _PROCESS_LOG_GUARD is None:
            get_log_guard = getattr(self.engine, "get_log_guard", None)
            if get_log_guard is not None:
                _PROCESS_LOG_GUARD = get_log_guard()
        self._profiles: dict[str, VenueSimulationProfilePort] = {}
        self._report_handlers: list[Callable[[ExecutionReport], None]] = []
        self._reports: list[ExecutionReport] = []
        self._gateway: NautilusOrderGateway | None = None
        self._started = False
        self._streaming = False
        self._finalized = False
        self._disposed = False
        self._result: Any = None
        self._option_expiry_modules = []
        self._option_last_trade_times = {}
        self._settlement_end_ns = None
        self._last_market_ns = 0

    @property
    def execution_timing(self) -> str:
        return self._execution_timing

    @property
    def execution_delay_bars(self) -> int:
        return self._execution_delay_bars

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def is_disposed(self) -> bool:
        return self._disposed

    @property
    def profiles(self) -> tuple[VenueSimulationProfilePort, ...]:
        return tuple(self._profiles.values())

    @property
    def reports(self) -> tuple[ExecutionReport, ...]:
        return tuple(self._reports)

    def write_commission_report(self, destination):
        """逐笔导出标准费用、系数和实际扣费，与原生成交报表核对。"""
        import csv
        import json
        records = [row for profile in self._profiles.values()
                   for row in getattr(getattr(profile, "fee_model", None), "records", ())]
        columns = ("instrument_id", "client_order_id", "quantity", "fill_price",
            "contract_multiplier", "source", "source_value", "source_unit", "per_contract",
            "rate", "standard_commission", "commission_multiplier", "actual_commission")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        with (destination / "commissions.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(records)
        configs = {str(profile.venue): {**profile.fee_model.config.profile_kwargs(),
            "commission_per_contract": str(profile.commission_per_contract)}
            for profile in self._profiles.values()
            if hasattr(getattr(profile, "fee_model", None), "config")}
        (destination / "commission_config.json").write_text(
            json.dumps(configs, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    def add_profile(self, profile: VenueSimulationProfilePort, *, option_settlements=None,
                    option_last_trade_times=None, settlement_end_ns=None) -> None:
        """注册交易场所Profile，并立即创建对应模拟Venue。"""

        self._ensure_configurable()
        venue_key = str(profile.venue)
        if venue_key in self._profiles:
            raise ValueError(f"模拟Venue已注册: {venue_key}")
        config = dict(profile.build_backend_config())
        if option_last_trade_times is not None:
            from .option_expiry import OptionCashSettlementModule
            module = OptionCashSettlementModule(self.backend_id, option_last_trade_times,
                option_settlements or {}, self._receive_report,
                lambda instrument_id: self._ensure_gateway().cancel_pending_orders(
                    instrument_id=instrument_id, reason="option reached last trading time"))
            config["modules"] = [*(config.get("modules") or []), module]
            self._option_expiry_modules.append(module)
            self._option_last_trade_times.update(option_last_trade_times)
            self._settlement_end_ns = settlement_end_ns
        self.engine.add_venue(**config)
        self._profiles[venue_key] = profile

    def add_instrument(self, instrument: Any) -> None:
        self._ensure_configurable()
        if (getattr(instrument, "info", None) or {}).get("expiry_policy") == "CASH_SETTLEMENT":
            if str(instrument.id) not in self._option_last_trade_times:
                raise ValueError("CASH_SETTLEMENT option requires a registered settlement lifecycle")
        profile = self._profiles.get(str(instrument.id).rsplit(".", 1)[-1])
        validator = getattr(getattr(profile, "fee_model", None), "validate_instrument", None)
        if validator is not None:
            validator(instrument)
        self.engine.add_instrument(instrument)

    def add_strategy(self, strategy: Any) -> None:
        self._ensure_configurable()
        self.engine.add_strategy(strategy)

    def add_data(self, data: Sequence[Any], **kwargs: Any) -> None:
        """注册历史行情；行情必须来自market/replay的标准事件。"""

        self._ensure_configurable()
        batch = list(data)
        if not batch:
            raise ValueError("data不能为空")
        self.engine.add_data(batch, **kwargs)

    def start(self) -> None:
        """封闭配置阶段；BacktestEngine会在首次run时启动内部组件。"""

        if self._disposed:
            raise RuntimeError("已释放的Backend不能重新启动")
        if self._finalized:
            raise RuntimeError("已结束的Backend不能重新启动")
        self._ensure_gateway()
        self._started = True

    def run(self) -> Any:
        """一次性运行已注册的历史行情并返回Nautilus回测结果。"""

        self._ensure_runnable()
        if self.execution_timing == "next_market":
            raise RuntimeError("next_market需要使用UnifiedHistoricalRuntime逐事件回放")
        self.start()
        self.engine.run()
        self._finalized = True
        self._result = self.engine.get_result()
        return self._result

    def process_market_event(self, event: Any) -> Sequence[ExecutionReport]:
        """以streaming模式推进一个标准行情事件。

        该入口用于后续在线仿真；每次推进后清除已消费批次，唯一模拟时钟仍由
        BacktestEngine掌握。返回值只包含本次事件推进新产生的执行回报。
        """

        self._ensure_runnable()
        event_ns = int(getattr(event, "ts_init", event.ts_event))
        if event_ns < self._last_market_ns:
            raise ValueError("Historical market time cannot move backwards")
        report_offset = len(self._reports)
        self.start()
        if self.execution_timing == "next_market":
            self._gateway.watch_market_event(event)
        self.engine.add_data([event])
        self.engine.run(streaming=True)
        self.engine.clear_data()
        self._streaming = True
        self._last_market_ns = event_ns
        return tuple(self._reports[report_offset:])

    def submit_order(self, order: OrderIntent) -> None:
        self._ensure_runnable()
        if order.backend_id != self.backend_id:
            raise ValueError(
                f"订单Backend不匹配: {order.backend_id} != {self.backend_id}",
            )
        cutoff = self._option_last_trade_times.get(str(order.instrument_id))
        clock_ns = self._gateway.clock.timestamp_ns() if self._gateway is not None else 0
        if cutoff is not None and max(clock_ns, self._last_market_ns,
                                     int(order.metadata.get("request_ts", 0))) >= cutoff:
            raise RuntimeError(f"Option is past last trading time: {order.instrument_id}")
        gateway = self._ensure_gateway()
        gateway.enqueue(order)

    def register_report_handler(
        self,
        handler: Callable[[ExecutionReport], None],
    ) -> None:
        if handler not in self._report_handlers:
            self._report_handlers.append(handler)

    def cancel_strategy(self, strategy_id: str) -> None:
        if not strategy_id.strip():
            raise ValueError("strategy_id不能为空")
        if self._gateway is not None:
            self._gateway.cancel_strategy_orders(strategy_id)

    def result(self) -> Any:
        return self._result

    def finish(self) -> Any:
        """结束流式撮合并生成结果，但保留Engine供报告查询。

        正式Historical Runtime需要先返回结果和原生报告，随后再由stop统一释放
        Engine。一次性``run()``已经自行结束，不会重复调用``engine.end()``。
        """

        if self._disposed:
            raise RuntimeError("已释放的Backend不能结束运行")
        if self._streaming and not self._finalized:
            if self._settlement_end_ns is not None and self._settlement_end_ns > self._last_market_ns:
                from .option_expiry import settlement_clock
                from bomber.model.identifiers import ClientId
                self.engine.add_data([settlement_clock(self._settlement_end_ns)],
                                     client_id=ClientId("OPTION-SETTLEMENT-CLOCK"))
                self.engine.run(streaming=True)
                self.engine.clear_data()
                self._last_market_ns = self._settlement_end_ns
            # 没有后续本合约行情的意图不可用最后一根旧Bar补成交。
            self._gateway.cancel_pending_orders(reason="end of historical market data")
            self.engine.end()
            self._finalized = True
            self._result = self.engine.get_result()
            self._started = False
        return self._result

    @property
    def option_settlement_records(self):
        return tuple(record for module in self._option_expiry_modules for record in module.records)

    def stop(self) -> None:
        """结束流式运行并释放引擎；重复调用安全。"""

        if self._disposed:
            return
        self.finish()
        self.engine.dispose()
        self._disposed = True
        self._started = False

    def _ensure_configurable(self) -> None:
        if self._started or self._finalized or self._disposed:
            raise RuntimeError("Backend启动、结束或释放后不能修改静态回测配置")

    def _ensure_runnable(self) -> None:
        if self._disposed:
            raise RuntimeError("已释放的Backend不能运行")
        if self._finalized:
            raise RuntimeError("已结束的Backend不能再次运行")

    def _ensure_gateway(self) -> NautilusOrderGateway:
        if self._gateway is None:
            self._gateway = NautilusOrderGateway(self.backend_id, self._receive_report,
                                                delay_bars=self.execution_delay_bars or 1)
            self.engine.add_strategy(self._gateway)
        return self._gateway

    def _receive_report(self, report: ExecutionReport) -> None:
        self._reports.append(report)
        for handler in tuple(self._report_handlers):
            handler(report)
