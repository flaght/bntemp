"""P/1101、RB/1301 的历史逐 bar 模拟回测；不连接真实账户。"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bomber.backtest.config import BacktestEngineConfig
from bomber.config import LoggingConfig
from bomber.model import Venue
from bomber.model.identifiers import TraderId
from bomber.framework.dataprep.bars import PreparedFrameReader, read_bar_frame
from bomber.framework.dataprep.catalog import bar_path
from bomber.framework.dataprep.contracts import BarReadSpec
from bomber.framework.dataprep.futures import instrument as build_instrument
from bomber.framework.dataprep.metadata import venue as venue_for_product
from bomber.framework.dataprep.scenarios.role_futures import prepare_role_research
from bomber.framework.dataprep.session import input_session, read_feather, write_input_reports
from bomber.framework.market.basic.base import DataType
from bomber.framework.market.replay.base import FileReplayFeed
from bomber.framework.trader import (
    ContractAssignment, CtpFuturesBasicProfile, DataBinding,
    DynamicExecutionRoute, MarketStreamBinding, NautilusMarketFeedAdapter,
    NautilusSimExecutionBackend, RiskLimits, RuntimeMode,
    ScheduledContractResolver, UnifiedHistoricalRuntime, UnifiedStrategyRunner,
)
from bomber.framework.trader.assembly import StrategyBindings, assemble_strategy
from bomber.framework.trader.execution.builders import build_simulation_execution

market = import_module("demos.10_hybrid_transformer.market_adapter")
model = import_module("demos.10_hybrid_transformer.model_adapter")
strategy_module = import_module("demos.10_hybrid_transformer.strategy")


@input_session
def run_case(*, product: str, start_day: date, end_day: date,
             bars_dir: Path, contract_struct: Path, fut_basic: Path,
             release_path: Path, directions_file: Path,
             report_dir: Path, factors_path: Path | None = None,
             factor_availability: str = "aligned",
             bar_timestamp: str = "end", value_column: str = "value",
             open_interest_column: str = "openint", max_contracts: int = 1,
             rebalance_minutes: int = 1, history_bars: int = 1500,
             min_raw_bars: int = 300, device: str = "auto",
             max_notional: Decimal = Decimal("1000000"),
             commission_per_contract: Decimal = Decimal("1"),
             require_fills: bool = False) -> Path:
    """一个品种一次独立运行；动态主力路由要求独占模拟执行客户端。"""
    product = product.strip().upper()
    signal_id = {"P": "1101", "RB": "1301"}.get(product)
    if signal_id is None:
        raise ValueError("当前只固化 P/1101、RB/1301")
    if start_day > end_day:
        raise ValueError("日期范围无效")
    if history_bars < min_raw_bars or min_raw_bars < 1:
        raise ValueError("须满足 history_bars >= min_raw_bars >= 1")
    if (not max_notional.is_finite() or max_notional <= 0
            or not commission_per_contract.is_finite()
            or commission_per_contract < 0):
        raise ValueError("名义金额上限须为正；手续费不得为负")

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
        commission_per_contract=commission_per_contract, venue=venue,
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
    config = strategy_module.HybridTransformerConfig(
        product=product, venue=str(venue), signal_id=signal_id,
        max_contracts=max_contracts, rebalance_minutes=rebalance_minutes,
        contract_multiplier=multiplier, record_signals=True,
    )

    spec = BarReadSpec(timestamp_label=bar_timestamp)
    for index, (day, assignment) in enumerate(zip(days, assignments)):
        needed = {assignment.instrument(product, "main")}
        if index:
            previous = assignments[index - 1].instrument(product, "main")
            if previous not in needed:
                needed.add(previous)  # 换月日旧合约还需要真实报价来平仓。
        for symbol in sorted(needed):
            path = bar_path(bars_dir, symbol, day)
            prepared = read_bar_frame(path, spec=spec)
            missing = {value_column, open_interest_column} - set(prepared.frame.columns)
            if missing:
                raise ValueError(f"{path}: 缺少模型行情字段 {sorted(missing)}")
            parser = market.ResearchCustomBarParser(
                instruments[symbol], value_column=value_column,
                open_interest_column=open_interest_column,
            )
            feed.add_source(path, PreparedFrameReader(prepared.frame), parser, "bar")

    resolver = ScheduledContractResolver(tuple(
        ContractAssignment(config.target_key, instruments[row.instrument(product, "main")],
                           row.effective_ns, row.available_ns, revision)
        for revision, row in enumerate(assignments, 1)
    ))
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
    predictor = model.MizarWorkflowAdapter(
        release_path=release_path, directions_file=directions_file,
        code=product, signal_id=signal_id, history_bars=history_bars,
        min_raw_bars=min_raw_bars, device=device,
    )
    strategy = strategy_module.HybridTransformerStrategy(
        f"{product.lower()}-main-hybrid", loaded.store, predictor, config,
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
        execution=(DynamicExecutionRoute(config.target_key,
                   execution.client.client_id, resolver),),
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
        for name, frame in (
            ("orders.csv", orders), ("fills.csv", fills),
            ("positions.csv", positions),
            ("account.csv", trader.generate_account_report(venue)),
        ):
            frame.to_csv(destination / name, index=False)
        import pandas as pd
        pd.DataFrame(strategy.signal_records).to_csv(
            destination / "signals.csv", index=False)
        summary = {
            "product": product, "signal_id": signal_id,
            "start_day": str(days[0]), "end_day": str(days[-1]),
            "bar_timestamp": bar_timestamp, "bars_used": strategy.bars_used,
            "signals": len(strategy.signal_records),
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", choices=("P", "RB"), required=True)
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--contract-struct", type=Path, required=True)
    parser.add_argument("--fut-basic", type=Path, required=True)
    parser.add_argument("--release-path", type=Path, required=True)
    parser.add_argument("--directions-file", type=Path, required=True)
    parser.add_argument("--factors", type=Path)
    parser.add_argument("--factor-availability", choices=("aligned", "explicit"),
                        default="aligned")
    parser.add_argument("--bar-timestamp", choices=("start", "end"), default="end")
    parser.add_argument("--value-column", default="value")
    parser.add_argument("--open-interest-column", default="openint")
    parser.add_argument("--max-contracts", type=int, default=1)
    parser.add_argument(
        "--rebalance-minutes", type=int, default=1,
        help="目标仓位更新间隔；默认每分钟。训练标签的 5 分钟预测期、评估的 5 分钟重采样均不限制交易频率",
    )
    parser.add_argument("--history-bars", type=int, default=1500)
    parser.add_argument("--min-raw-bars", type=int, default=300)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-notional", type=Decimal, default=Decimal("1000000"))
    parser.add_argument("--commission-per-contract", type=Decimal,
                        default=Decimal("1"))
    parser.add_argument("--require-fills", action="store_true")
    parser.add_argument("--report-dir", type=Path,
                        default=Path(__file__).resolve().parent / "results")
    args = parser.parse_args()
    for path in (args.bars_dir, args.contract_struct, args.fut_basic,
                 args.release_path, args.directions_file):
        if not path.exists():
            parser.error(f"输入不存在: {path}")
    if args.factors is not None and not args.factors.exists():
        parser.error(f"输入不存在: {args.factors}")
    destination = run_case(
        product=args.product, start_day=args.start_day, end_day=args.end_day,
        bars_dir=args.bars_dir, contract_struct=args.contract_struct,
        fut_basic=args.fut_basic, release_path=args.release_path,
        directions_file=args.directions_file, report_dir=args.report_dir,
        factors_path=args.factors, factor_availability=args.factor_availability,
        bar_timestamp=args.bar_timestamp, value_column=args.value_column,
        open_interest_column=args.open_interest_column,
        max_contracts=args.max_contracts, rebalance_minutes=args.rebalance_minutes,
        history_bars=args.history_bars, min_raw_bars=args.min_raw_bars,
        device=args.device, max_notional=args.max_notional,
        commission_per_contract=args.commission_per_contract,
        require_fills=args.require_fills,
    )
    print(f"历史模拟报表: {destination}")


if __name__ == "__main__":
    main()
