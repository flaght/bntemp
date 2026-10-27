from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path

from bomber.model import Venue
from bomber.config import LoggingConfig
from bomber.model.identifiers import TraderId
from bomber.backtest.config import BacktestEngineConfig
from bomber.framework.dataprep.bars import PreparedFrameReader, filter_session_bars, read_bar_frame
from bomber.framework.dataprep.catalog import bar_path
from bomber.framework.dataprep.contracts import BarReadSpec
from bomber.framework.dataprep.futures import instrument as build_instrument
from bomber.framework.market.replay.base import FileReplayFeed
from bomber.framework.dataprep.metadata import venue as venue_for_product
from bomber.framework.dataprep.scenarios.role_futures import prepare_role_research
from bomber.framework.dataprep.session import input_session, read_feather, write_input_reports
from bomber.framework.fees import CommissionConfig
from bomber.framework.trader import (NautilusSimExecutionBackend,CtpFuturesBasicProfile,
    RiskLimits,RuntimeMode,NautilusMarketFeedAdapter,MarketStreamBinding,
    UnifiedStrategyRunner,DataBinding,ExecutionRoute,UnifiedHistoricalRuntime)
from bomber.framework.trader.assembly import StrategyBindings, assemble_strategy
from bomber.framework.trader.execution.builders import build_simulation_execution
from bomber.framework.calendars import SessionCalendar
from bomber.framework.market.basic.base import DataType
from bomber.framework.market.replay.parsers.bar import FixedInstrumentBarParser


from .signal_config import load_product_signal
from .strategy import HybridTransformerConfig,HybridTransformerStrategy
from .model_adapter import MizarWorkflowAdapter
from .checkpoint import HybridCheckpoint

@input_session
def run_case(*, product: str, start_day: date, end_day: date,
             bars_dir: Path, contract_struct: Path, fut_basic: Path,
             release_path: Path, directions_file: Path, signal_config: Path,
             report_dir: Path, calendar: SessionCalendar, factors_path: Path | None = None,
             factor_availability: str = "aligned",
             bar_timestamp: str = "end", value_column: str = "value",
             open_interest_column: str = "openint", max_contracts: int = 1,
             rebalance_minutes: int = 1, hold_bars: int | None = None, history_bars: int = 1500,
             min_raw_bars: int = 300, device: str = "auto",
             max_notional: Decimal = Decimal("1000000"),
             commission_config: CommissionConfig | None = None,
             require_fills: bool = False) -> Path:
    """按 fut_basic 费用回测；commission_config 仅接受 metadata 来源及系数配置。"""
    fees = commission_config if commission_config is not None else CommissionConfig(commission_source="metadata")
    if not isinstance(fees, CommissionConfig) or fees.commission_source != "metadata":
        raise ValueError("Hybrid 回测手续费须来自 fut_basic，请使用 commission_source='metadata'")
    product = product.strip().upper()
    if calendar.product.upper() != product:
        raise ValueError("持仓交易日历与回测品种不一致")
    selected = load_product_signal(signal_config, product)

    signal_id = selected.signal_id
    hold_bars = selected.hold_bars if hold_bars is None else hold_bars

    if type(hold_bars) is not int or hold_bars < 1:
        raise ValueError("持仓周期须为正整数")
    if start_day > end_day:
        raise ValueError("日期范围无效")
    if history_bars < min_raw_bars or min_raw_bars < 1:
        raise ValueError("须满足 history_bars >= min_raw_bars >= 1")
    if not max_notional.is_finite() or max_notional <= 0:
        raise ValueError("名义金额上限须为有限正数")


    ### 数据加载器
    loaded = prepare_role_research(
        bars_dir=bars_dir, contract_struct_path=contract_struct,
        products=(product,), signal_role="main", end_day=end_day,
        execution_product=product, execution_role="main",
        bar_timestamp=bar_timestamp, factors_path=factors_path,
        factor_date_basis="trading" if factors_path else None,
        factor_availability=factor_availability,
    )

    ends = dict(loaded.day_end_ns)
    days = tuple(day for day in sorted(ends) if start_day <= day <= end_day)

    if not days:
        raise ValueError("请求日期内没有可用主力合约分钟行情")
    if (days[0] - start_day > timedelta(days=14)
            or end_day - days[-1] > timedelta(days=14)):
        raise ValueError("行情日期覆盖不足；检查行情目录和日期范围")

    ## 取合约快照
    assignments = tuple(loaded.store.snapshot(ends[day]) for day in days)
    symbols = {row.instrument(product, "main") for row in assignments}
    symbols.update(
        previous.instrument(product, "main")
        for previous, current in zip(assignments, assignments[1:])
        if previous.instrument(product, "main") != current.instrument(product, "main")
    )


    basic = read_feather(fut_basic)
    required_basic = {"code", "symbol", "exchangeCD", "contMultNum",
                      "minChgPriceNum", "listDate", "lastTradeDate"}

    if required_basic - set(basic.columns):
        raise ValueError(f"fut_basic 缺少字段: {sorted(required_basic - set(basic.columns))}")

    ## 合约注册
    venue = Venue(venue_for_product(basic, product))


    backend = NautilusSimExecutionBackend(
        f"{product.lower()}-transformer-sim",
        BacktestEngineConfig(
            trader_id=TraderId(f"{product}-HYBRID-001"),
            logging=LoggingConfig(log_level="WARNING"), run_analysis=True,
        ),
    )

    profile = CtpFuturesBasicProfile(
        starting_balance=Decimal("1000000"),
        venue=venue, **fees.profile_kwargs(),
    )
    backend.add_profile(profile)

    feed = FileReplayFeed(f"{product}_HYBRID_REPLAY")

    instruments, multipliers = {}, {}

    for symbol in sorted(symbols):
        contract, meta, multiplier = build_instrument(basic, profile, product, symbol)
        instruments[symbol] = contract.id
        multipliers[contract.id] = multiplier
        backend.add_instrument(contract)
        feed.register_instrument(meta)

    unique_multipliers = {Decimal(str(value)) for value in multipliers.values()}
    if len(unique_multipliers) != 1:
        raise ValueError("同品种主力合约乘数不一致，需按合约逐根计算 VWAP")

    multiplier = unique_multipliers.pop()


    ## 可以放在模版里回调
    config = HybridTransformerConfig(
        product=product, venue=str(venue), signal_id=signal_id,
        max_contracts=max_contracts, hold_bars=hold_bars, rebalance_minutes=rebalance_minutes,
        contract_multiplier=multiplier, record_signals=True)

    spec = BarReadSpec(timestamp_label=bar_timestamp)

    for index, (day, assignment) in enumerate(zip(days, assignments)):
        needed = {assignment.instrument(product, "main")}
        if index:
            previous = assignments[index - 1].instrument(product, "main")
            if previous not in needed:
                needed.add(previous)  # 换月日旧合约还需要真实报价来平仓。

        for symbol in sorted(needed):
            path = bar_path(bars_dir, symbol, day)
            prepared = filter_session_bars(read_bar_frame(path, spec=spec), calendar)
            missing = {value_column, open_interest_column} - set(prepared.frame.columns)
            if missing:
                raise ValueError(f"{path}: 缺少模型行情字段 {sorted(missing)}")
            parser = FixedInstrumentBarParser(
                instruments[symbol], timestamp="datetime",
                available_nanoseconds="available_ns",
                value=value_column, open_interest=open_interest_column,
                trade_time="source_timestamp", preserve_raw_ohlcv=True,
            )
            feed.add_source(path, PreparedFrameReader(prepared.frame), parser, "bar")

    target_keys = {symbol: f"{config.target_key}:{symbol}" for symbol in instruments}
    execution = build_simulation_execution(backend, instrument_limits={
        instrument_id: RiskLimits(
            max_order_quantity=2 * config.max_contracts,
            max_abs_position=config.max_contracts,
            max_order_notional=max_notional,
            max_abs_position_notional=max_notional,
            max_market_age_ns=120_000_000_000,
            contract_multiplier=multipliers[instrument_id],
        ) for instrument_id in multipliers
    })


    predictor = MizarWorkflowAdapter(
        release_path=release_path, directions_file=directions_file,
        code=product, signal_id=signal_id,
        signal_method=selected.signal_method, signal_params=selected.signal_params,
        history_bars=history_bars,
        min_raw_bars=min_raw_bars, device=device,
    )


    strategy = HybridTransformerStrategy(
        f"{product.lower()}-main-hybrid", loaded.store, predictor, config,
        target_keys=target_keys, calendar=calendar,
    )

    adapter = NautilusMarketFeedAdapter(
        f"{product.lower()}-hybrid-clock", feed, backend,
        tuple(MarketStreamBinding(item, DataType.BAR, "1-MINUTE")
              for item in instruments.values()),
        manage_lifecycle=False,
    )

    runner = UnifiedStrategyRunner(RuntimeMode.HISTORICAL,
                                   position_manager=execution.positions)

    bindings = StrategyBindings(
        data=tuple(DataBinding(str(item), "main-bars", item,
                               DataType.CUSTOM_BAR, "1-MINUTE")
                   for item in instruments.values()),
        execution=tuple(ExecutionRoute(target_keys[symbol], execution.client.client_id, item)
                        for symbol, item in sorted(instruments.items())),
    )

    assemble_strategy(runner, strategy, feeds={"main-bars": feed},
                      execution=execution, bindings=bindings)

    runtime = UnifiedHistoricalRuntime(
        f"{product.lower()}-main-hybrid-runtime", runner, adapter)

    try:
        result = runtime.run()
        trader = backend.engine.trader
        orders = trader.generate_orders_report()
        fills = trader.generate_fills_report()
        positions = trader.generate_positions_report()
        if strategy.bars_used < min_raw_bars:
            raise AssertionError("模型预热没有完成；扩大回测起始区间")
        if execution.client.report_errors:
            raise AssertionError(f"模拟执行回报异常: {execution.client.report_errors}")
        if require_fills and fills.empty:
            raise AssertionError("本次要求成交，但没有模拟成交")
        destination = report_dir / str(result.backend_result.run_id)
        destination.mkdir(parents=True, exist_ok=True)
        write_input_reports(destination)
        backend.write_commission_report(destination)
        (destination / "strategy_state.json").write_text(
            json.dumps(HybridCheckpoint(strategy).snapshot_state(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        for name, frame in (
            ("orders.csv", orders), ("fills.csv", fills),
            ("positions.csv", positions),
            ("account.csv", trader.generate_account_report(venue)),
        ):
            frame.to_csv(destination / name, index=False)
        import pandas as pd
        pd.DataFrame(strategy.signal_records).to_csv(
            destination / "signals.csv", index=False)
        pd.DataFrame(strategy.batch_records).to_csv(
            destination / "batches.csv", index=False)
        summary = {
            "product": product, "signal_id": signal_id,
            "start_day": str(days[0]), "end_day": str(days[-1]),
            "calendar": calendar.provenance,
            "bar_timestamp": bar_timestamp, "bars_used": strategy.bars_used,
            "signals": sum(row["predicted_z"] is not None for row in strategy.signal_records),
            "holding_bars": config.hold_bars,
            "signal_method": selected.signal_method,
            "signal_params": dict(selected.signal_params),
            "signal_config": str(signal_config),
            "slot_capacities": strategy.holdings.capacities,
            "active_batches": [asdict(batch) for batch in strategy.holdings.active],
            "last_targets_by_contract": {key: str(value) for key, value in strategy.last_targets.items()},
            "last_main": strategy.last_main,
            "last_target": str(strategy.last_target),
            "unavailable_events": strategy.unavailable_events,
            "orders": len(orders), "fills": len(fills),
            "backend": (asdict(result.backend_result)
                        if is_dataclass(result.backend_result)
                        else str(result.backend_result)),
        }
        (destination / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return destination
    finally:
        runtime.stop()
