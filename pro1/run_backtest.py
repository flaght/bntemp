from __future__ import annotations

import argparse,pdb
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path

from bomber.backtest.config import BacktestEngineConfig
from bomber.config import LoggingConfig
from bomber.model import Venue
from bomber.model.identifiers import TraderId

from bomber.framework.calendars import NativeCalendarProvider
from bomber.framework.dataprep.bars import PreparedFrameReader, read_bar_frame
from bomber.framework.dataprep.catalog import bar_path
from bomber.framework.dataprep.contracts import BarReadSpec
from bomber.framework.dataprep.futures import instrument as build_instrument
from bomber.framework.dataprep.metadata import venue as venue_for_product
from bomber.framework.dataprep.scenarios.role_futures import prepare_role_research
from bomber.framework.dataprep.session import (
    input_session,
    read_feather,
    write_input_reports,
)
from bomber.framework.dataprep.session_inputs import (
    SessionInputs,
    load_session_inputs,
)
from bomber.framework.fees import CommissionConfig
from bomber.framework.market.basic.base import DataType
from bomber.framework.market.replay.base import FileReplayFeed
from bomber.framework.market.replay.parsers.bar import FixedInstrumentBarParser
from bomber.framework.trader import (
    CtpFuturesBasicProfile,
    DataBinding,
    ExecutionRoute,
    MarketStreamBinding,
    NautilusMarketFeedAdapter,
    NautilusSimExecutionBackend,
    RiskLimits,
    RuntimeMode,
    UnifiedHistoricalRuntime,
    UnifiedStrategyRunner,
)
from bomber.framework.trader.assembly import StrategyBindings, assemble_strategy
from bomber.framework.trader.execution.builders import build_simulation_execution

from .adapter import Adapter
from .signal_config import load_product_signal
from .strategy import HybridTransformerConfig, HybridTransformerStrategy

@input_session
def run_case(
    *,
    product: str,
    start_day: date,
    end_day: date,
    bars_dir: Path,
    contract_struct: Path,
    fut_basic: Path,
    release_path: Path,
    directions_file: Path,
    signal_config: Path,
    hours_file: Path,
    report_dir: Path,
    factors_path: Path | None = None,
    factor_availability: str = "aligned",
    bar_timestamp: str = "end",
    value_column: str = "value",
    open_interest_column: str = "openint",
    max_contracts: int = 1,
    rebalance_minutes: int = 1,
    hold_bars: int | None = None,
    history_bars: int = 1500,
    min_raw_bars: int = 300,
    device: str = "auto",
    max_notional: Decimal = Decimal("1000000"),
    commission_config: CommissionConfig | None = None,
    require_fills: bool = False,
) -> Path:
    pdb.set_trace()
    if isinstance(start_day, str):
        start_day = date.fromisoformat(start_day.strip())

    if isinstance(end_day, str):
        end_day = date.fromisoformat(end_day.strip())

    product = product.strip().upper()
    fees = (
        commission_config
        if commission_config is not None
        else CommissionConfig(commission_source="metadata")
    )

    selected = load_product_signal(signal_config, product)
    signal_id = selected.signal_id
    hold_bars = selected.hold_bars if hold_bars is None else hold_bars

    basic = read_feather(fut_basic)
    exchange = venue_for_product(basic, product)
    venue = Venue(exchange)

    calendar_id = "china.cffex"
    calendar = load_session_inputs(
        SessionInputs(
            product=product,
            calendar_id=calendar_id,
            exchange=exchange,
            backend="file",
            hours_file=hours_file,
            no_overrides=True,
        ),
        calendar_provider=NativeCalendarProvider(calendar_id),
    )
    print(
        f"开始回测：product={product}, exchange={exchange}, "
        f"calendar={calendar_id}, dates={start_day}..{end_day}",
        flush=True,
    )

    pdb.set_trace()
    loaded = prepare_role_research(
        bars_dir=bars_dir,
        contract_struct_path=contract_struct,
        products=(product,),
        signal_role="main",
        end_day=end_day,
        execution_product=product,
        execution_role="main",
        bar_timestamp=bar_timestamp,
        factors_path=factors_path,
        factor_date_basis="trading" if factors_path else None,
        factor_availability=factor_availability,
    )


    ends = dict(loaded.day_end_ns)
    days = tuple(
        day for day in sorted(ends)
        if start_day <= day <= end_day
    )

    if not days:
        raise ValueError("请求日期内没有可用主力合约分钟行情")
    if (
        days[0] - start_day > timedelta(days=14)
        or end_day - days[-1] > timedelta(days=14)
    ):
        raise ValueError("行情日期覆盖不足；检查行情目录和日期范围")

    assignments = tuple(
        loaded.store.snapshot(ends[day])
        for day in days
    )
    symbols = {
        assignment.instrument(product, "main")
        for assignment in assignments
    }

    backend = NautilusSimExecutionBackend(
        f"{product.lower()}-transformer-sim",
        BacktestEngineConfig(
            trader_id=TraderId(f"{product}-HYBRID-001"),
            logging=LoggingConfig(log_level="WARNING"),
            run_analysis=True,
        ),
    )

    profile = CtpFuturesBasicProfile(
        starting_balance=Decimal("1000000"),
        venue=venue,
        **fees.profile_kwargs(),
    )
    backend.add_profile(profile)

    feed = FileReplayFeed(f"{product}_HYBRID_REPLAY")
    instruments = {}
    multipliers = {}

    for symbol in sorted(symbols):
        contract, meta, multiplier = build_instrument(
            basic, profile, product, symbol
        )
        instruments[symbol] = contract.id
        multipliers[contract.id] = multiplier
        backend.add_instrument(contract)
        feed.register_instrument(meta)

    unique_multipliers = {
        Decimal(str(value))
        for value in multipliers.values()
    }
    if len(unique_multipliers) != 1:
        raise ValueError("同品种主力合约乘数不一致，需按合约逐根计算 VWAP")

    multiplier = unique_multipliers.pop()

    config = HybridTransformerConfig(
        product=product,
        venue=exchange,
        signal_id=signal_id,
        max_contracts=max_contracts,
        hold_bars=hold_bars,
        rebalance_minutes=rebalance_minutes,
        contract_multiplier=multiplier,
        record_signals=True,
    )

    spec = BarReadSpec(timestamp_label=bar_timestamp)

    for index, (day, assignment) in enumerate(zip(days, assignments)):
        needed = {assignment.instrument(product, "main")}

        if index:
            previous = assignments[index - 1].instrument(product, "main")
            needed.add(previous)

        for symbol in sorted(needed):
            path = bar_path(bars_dir, symbol, day)
            prepared = read_bar_frame(path, spec=spec)

            missing = {
                value_column,
                open_interest_column,
            } - set(prepared.frame.columns)
            if missing:
                raise ValueError(
                    f"{path}: 缺少模型行情字段 {sorted(missing)}"
                )

            parser = FixedInstrumentBarParser(
                instruments[symbol],
                timestamp="datetime",
                available_nanoseconds="available_ns",
                value=value_column,
                open_interest=open_interest_column,
                trade_time="source_timestamp",
                preserve_raw_ohlcv=True,
            )
            feed.add_source(
                path,
                PreparedFrameReader(prepared.frame),
                parser,
                "bar",
            )

    target_keys = {
        symbol: f"{config.target_key}:{symbol}"
        for symbol in instruments
    }

    execution = build_simulation_execution(
        backend,
        instrument_limits={
            instrument_id: RiskLimits(
                max_order_quantity=2 * config.max_contracts,
                max_abs_position=config.max_contracts,
                max_order_notional=max_notional,
                max_abs_position_notional=max_notional,
                max_market_age_ns=120_000_000_000,
                contract_multiplier=multipliers[instrument_id],
            )
            for instrument_id in multipliers
        },
    )

    predictor = Adapter(
        release_path=release_path,
        directions_file=directions_file,
        code=product,
        signal_id=signal_id,
        signal_method=selected.signal_method,
        signal_params=selected.signal_params,
        history_bars=history_bars,
        min_raw_bars=min_raw_bars,
        device=device,
    )

    strategy = HybridTransformerStrategy(
        f"{product.lower()}-main-hybrid",
        loaded.store,
        predictor,
        config,
        target_keys=target_keys,
        calendar=calendar,
    )

    adapter = NautilusMarketFeedAdapter(
        f"{product.lower()}-hybrid-clock",
        feed,
        backend,
        tuple(
            MarketStreamBinding(item, DataType.BAR, "1-MINUTE")
            for item in instruments.values()
        ),
        manage_lifecycle=False,
    )

    runner = UnifiedStrategyRunner(
        RuntimeMode.HISTORICAL,
        position_manager=execution.positions,
    )

    bindings = StrategyBindings(
        data=tuple(
            DataBinding(
                str(item), "main-bars", item,
                DataType.CUSTOM_BAR, "1-MINUTE",
            )
            for item in instruments.values()
        ),
        execution=tuple(
            ExecutionRoute(
                target_keys[symbol],
                execution.client.client_id,
                item,
            )
            for symbol, item in sorted(instruments.items())
        ),
    )

    # 必须装配后再运行，否则策略没有绑定行情及执行通道。
    assemble_strategy(
        runner,
        strategy,
        feeds={"main-bars": feed},
        execution=execution,
        bindings=bindings,
    )

    runtime = UnifiedHistoricalRuntime(
        f"{product.lower()}-main-hybrid-runtime",
        runner,
        adapter,
    )

    try:
        result = runtime.run()

        trader = backend.engine.trader
        orders = trader.generate_orders_report()
        fills = trader.generate_fills_report()
        positions = trader.generate_positions_report()

        if strategy.bars_used < min_raw_bars:
            raise AssertionError("模型预热没有完成；扩大回测日期区间")
        if execution.client.report_errors:
            raise AssertionError(
                f"模拟执行回报异常: {execution.client.report_errors}"
            )
        if require_fills and fills.empty:
            raise AssertionError("本次要求成交，但没有模拟成交")

        destination = report_dir / str(result.backend_result.run_id)
        destination.mkdir(parents=True, exist_ok=True)

        write_input_reports(destination)
        backend.write_commission_report(destination)

        for name, frame in (
            ("orders.csv", orders),
            ("fills.csv", fills),
            ("positions.csv", positions),
            ("account.csv", trader.generate_account_report(venue)),
        ):
            frame.to_csv(destination / name, index=False)

        import pandas as pd

        pd.DataFrame(strategy.signal_records).to_csv(
            destination / "signals.csv", index=False
        )
        pd.DataFrame(strategy.batch_records).to_csv(
            destination / "batches.csv", index=False
        )

        summary = {
            "product": product,
            "exchange": exchange,
            "signal_id": signal_id,
            "start_day": str(days[0]),
            "end_day": str(days[-1]),
            "bars_used": strategy.bars_used,
            "orders": len(orders),
            "fills": len(fills),
            "holding_bars": hold_bars,
            "calendar": calendar.provenance,
            "active_batches": [
                asdict(batch)
                for batch in strategy.holdings.active
            ],
            "backend": (
                asdict(result.backend_result)
                if is_dataclass(result.backend_result)
                else str(result.backend_result)
            ),
        }
        (destination / "summary.json").write_text(
            json.dumps(
                summary,
                ensure_ascii=False,
                indent=2,
                default=str,
            ) + "\n",
            encoding="utf-8",
        )
        return destination

    finally:
        runtime.stop()


def main():
    product = 'RB'
    start_day = '2026-01-05'
    end_day = '2026-01-09'

    role_dir = Path("/workspace/data/temp_bomber1/ctp/role")
    bars_dir = Path("/workspace/data/temp_bomber1/ctp/kline/fut")
    mizar_dir = Path("/workspace/worker/pj/Chrono/genuis/mizar")

    
    result = run_case(
        product=product,
        start_day=start_day,
        end_day=end_day,
        bars_dir=bars_dir,
        contract_struct=role_dir / "fut_contract.feather",
        fut_basic=role_dir / "fut_basic.feather",
        release_path=(
            mizar_dir / "records/ricso2/1release/rbb/10002"
        ),
        directions_file=(
            mizar_dir
            / "records/ricso2/rbb/rulex/113001/nxt1_ret_5h/cohort_pro.csv"
        ),
        signal_config=Path(__file__).with_name(
            "signal_config.json"
        ),
        hours_file=role_dir / "fut_trading_session.feather",
        report_dir=Path(__file__).parent / "results",
        bar_timestamp="start",
        max_contracts=10,
        rebalance_minutes=1,
        device="cpu",
        commission_config=CommissionConfig(
            commission_source="metadata",
            commission_multiplier=Decimal("1"),
        ),
    )
    print(f"回测完成，结果目录：{result}", flush=True)


if __name__ == "__main__":
    main()