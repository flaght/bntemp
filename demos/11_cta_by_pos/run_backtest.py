"""日目标 CTA 的标准框架回测：公共数据准备、合约、费用、风控和执行。"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import date
from decimal import Decimal
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from bomber.framework.dataprep.commissions import add_commission_arguments, commission_config_from_args
from bomber.framework.dataprep.session import current_session, input_session, write_input_reports

DEFAULT_POSITIONS = ROOT / "3rd/strategies/kd_cta_by_pos/positions.csv"
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent / "results"


def assemble(*, strategy, feed, backend, instruments, multipliers,
             max_quantity, max_notional, max_market_age_seconds):
    """沿用 01/10 的执行积木、绑定声明、Runner、历史 Runtime。"""
    from bomber.framework.market.basic.base import DataType
    from bomber.framework.trader import (
        DataBinding, ExecutionRoute, MarketStreamBinding, NautilusMarketFeedAdapter,
        RiskLimits, RuntimeMode, UnifiedHistoricalRuntime, UnifiedStrategyRunner,
    )
    from bomber.framework.trader.assembly import StrategyBindings, assemble_strategy
    from bomber.framework.trader.execution.builders import build_simulation_execution

    execution = build_simulation_execution(backend, instrument_limits={item: RiskLimits(
        max_order_quantity=max_quantity * 2, max_abs_position=max_quantity,
        max_order_notional=max_notional, max_abs_position_notional=max_notional,
        max_market_age_ns=max_market_age_seconds * 1_000_000_000,
        contract_multiplier=multipliers[item]) for item in instruments.values()})
    runner = UnifiedStrategyRunner(RuntimeMode.HISTORICAL, position_manager=execution.positions)
    bindings = StrategyBindings(
        data=tuple(DataBinding(str(item), "cta-bars", item, DataType.BAR, "1-MINUTE")
                   for item in instruments.values()),
        execution=tuple(ExecutionRoute(key, execution.client.client_id, item)
                        for key, item in instruments.items()),
    )
    assemble_strategy(runner, strategy, feeds={"cta-bars": feed}, execution=execution, bindings=bindings)
    adapter = NautilusMarketFeedAdapter("cta-by-pos-clock", feed, backend,
        tuple(MarketStreamBinding(item, DataType.BAR, "1-MINUTE")
              for item in instruments.values()), manage_lifecycle=False)
    return UnifiedHistoricalRuntime("cta-by-pos-runtime", runner, adapter), strategy, execution.client


def export_comparison_fills(fills, path):
    """仅统一报表列名，数量、成交价和手续费全部来自框架真实成交。"""
    import pandas as pd

    records = []
    for order_id, row in fills.iterrows():
        stamp = pd.Timestamp(row["ts_event"])
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp
        commission = Decimal(str(row["commission"]).rsplit(" ", 1)[0].replace(",", ""))
        records.append({"timestamp": stamp.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
            "instrument_id": row["instrument_id"], "order_id": order_id,
            "side": row["order_side"], "qty": row["last_qty"], "price": row["last_px"],
            "commission": commission})
    pd.DataFrame(records, columns=("timestamp", "instrument_id", "order_id", "side", "qty", "price",
                                  "commission")).to_csv(path, index=False)


@input_session
def run_case(*, positions_path: Path, start_day: date, end_day: date, bars_dir: Path,
             fut_basic: Path, signal_time: str = "09:40",
             starting_balance: Decimal = Decimal("15000000"),
             commission_per_contract: Decimal = Decimal(1), commission_config=None,
             margin_init: Decimal = Decimal("0.10"), margin_maint: Decimal = Decimal("0.08"),
             max_quantity: Decimal = Decimal(100), max_notional: Decimal = Decimal("50000000"),
             max_market_age_seconds: int = 120, bar_timestamp: str = "end",
             log_level: str = "WARNING", report_dir: Path = DEFAULT_REPORT_DIR,
             tearsheet: bool = True, require_fills: bool = False) -> Path:
    import pandas as pd
    from bomber.backtest.config import BacktestEngineConfig
    from bomber.config import LoggingConfig
    from bomber.model import Venue
    from bomber.model.identifiers import TraderId
    from bomber.framework.dataprep.bars import add_prepared_bar_source, key_from_path
    from bomber.framework.dataprep.catalog import bar_files
    from bomber.framework.dataprep.contracts import BarReadSpec
    from bomber.framework.dataprep.futures import make_instrument
    from bomber.framework.dataprep.metadata import contract_rows, symbol_of, venue_of
    from bomber.framework.dataprep.scenarios.fixed_contracts import plan_fixed_contracts, prepare_fixed_contracts
    from bomber.framework.dataprep.session import read_feather
    from bomber.framework.fees import CommissionConfig
    from bomber.framework.market.replay.base import FileReplayFeed
    from bomber.framework.trader import CtpFuturesBasicProfile, NautilusSimExecutionBackend

    if end_day < start_day:
        raise ValueError("日期范围无效")
    if any(not value.is_finite() or value <= 0 for value in
           (starting_balance, margin_init, margin_maint, max_quantity, max_notional)):
        raise ValueError("资金、保证金、手数和名义金额上限须为有限正数")
    if (margin_init > 1 or margin_maint > margin_init or not commission_per_contract.is_finite()
            or commission_per_contract < 0 or max_quantity != max_quantity.to_integral_value()
            or max_market_age_seconds < 1):
        raise ValueError("保证金、手续费、最大手数或行情时效无效")
    strategy_module = importlib.import_module("demos.11_cta_by_pos.strategy")
    session = current_session()
    session.fingerprint(positions_path)
    strategy = strategy_module.CtaByPosStrategy("cta-by-pos", positions_path,
        strategy_module.CtaByPosConfig(signal_time), start_day=start_day, end_day=end_day)
    if strategy.max_target_quantity > max_quantity:
        raise ValueError("文件目标手数超过 --max-quantity")
    rows = contract_rows(read_feather(fut_basic), set(strategy.get_insts()))
    if {venue_of(row) for row in rows.values()} != {"CFFEX"}:
        raise ValueError("股指期货基础资料须属于 CFFEX")
    files = bar_files(bars_dir, {symbol_of(row) for row in rows.values()}, start_day, end_day)
    plan = plan_fixed_contracts(
        (key_from_path(path) for paths in files.values() for path in paths),
        requested=(start_day, end_day),
    )
    bundle = prepare_fixed_contracts(bars_dir, plan, spec=BarReadSpec(timestamp_label=bar_timestamp))
    backend = NautilusSimExecutionBackend("cta-by-pos-sim", BacktestEngineConfig(
        trader_id=TraderId("CTA-BY-POS-001"), logging=LoggingConfig(log_level=log_level), run_analysis=True))
    runtime = None
    try:
        venue = Venue("CFFEX")
        fees = commission_config or CommissionConfig(commission_source="metadata")
        profile = CtpFuturesBasicProfile(venue=venue, starting_balance=starting_balance,
            commission_per_contract=commission_per_contract, **fees.profile_kwargs())
        backend.add_profile(profile)
        feed = FileReplayFeed("CTA_BY_POS_REPLAY")
        instruments, multipliers = {}, {}
        for key, row in rows.items():
            contract, meta, multiplier = make_instrument(row, profile, margin_init, margin_maint)
            backend.add_instrument(contract)
            feed.register_instrument(meta)
            instruments[key] = contract.id
            multipliers[contract.id] = multiplier
        by_symbol = {symbol_of(rows[key]).lower(): item for key, item in instruments.items()}
        for source in bundle.sources:
            add_prepared_bar_source(feed, source.result, by_symbol[source.result.key.symbol.lower()])
        runtime, strategy, client = assemble(strategy=strategy, feed=feed, backend=backend,
            instruments=instruments, multipliers=multipliers, max_quantity=max_quantity,
            max_notional=max_notional, max_market_age_seconds=max_market_age_seconds)
        result = runtime.run()
        destination = Path(report_dir) / str(result.backend_result.run_id)
        destination.mkdir(parents=True, exist_ok=True)
        write_input_reports(destination)
        backend.write_commission_report(destination)
        trader = backend.engine.trader
        orders, fills = trader.generate_orders_report(), trader.generate_fills_report()
        for name, frame in (("orders.csv", orders), ("fills.csv", fills),
                            ("positions.csv", trader.generate_positions_report()),
                            ("account.csv", trader.generate_account_report(venue))):
            frame.to_csv(destination / name)
        export_comparison_fills(fills, destination / "fills_output.csv")
        pd.DataFrame(strategy.target_records,
            columns=("ts_event", "trading_day", "instrument_id", "target_qty")).to_csv(
                destination / "signals.csv", index=False)
        submitted_days = {record["trading_day"] for record in strategy.target_records}
        summary = {
            "backend": asdict(result.backend_result) if is_dataclass(result.backend_result)
                       else str(result.backend_result),
            "strategy": {"positions_path": str(Path(positions_path).resolve()),
                "start_day": str(start_day), "end_day": str(end_day), "signal_time": signal_time,
                "submitted_days": sorted(submitted_days),
                "untriggered_days": [str(day) for day in strategy.target_dates
                                     if day not in strategy.handled_days],
                "skipped_expired": strategy.skipped_expired,
                "instruments": {key: str(item) for key, item in instruments.items()}},
            "bar_timestamp": bar_timestamp, "bars": result.replay_summary.bars,
            "orders": len(orders), "fills": len(fills),
            "execution_report_errors": [str(error) for error in client.report_errors],
        }
        (destination / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        if tearsheet:
            try:
                from bomber.analysis.tearsheet import create_tearsheet
                create_tearsheet(engine=backend.engine, output_path=str(destination / "tearsheet.html"),
                                 title="CTA By Positions Backtest")
            except ImportError as exc:
                print(f"绩效图未生成（{exc}）；CSV 和 JSON 已保存")
        print(f"回测报表: {destination.resolve()}")
        if client.report_errors:
            raise AssertionError(f"执行回报异常: {client.report_errors}")
        if require_fills and fills.empty:
            raise AssertionError("本次要求成交，但没有模拟成交")
        return destination
    finally:
        if runtime is not None:
            runtime.stop()
        else:
            backend.stop()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_commission_arguments(parser)
    parser.set_defaults(commission_source="metadata")
    parser.add_argument("--positions", type=Path, default=DEFAULT_POSITIONS)
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--bars-dir", type=Path)
    parser.add_argument("--fut-basic", type=Path)
    parser.add_argument("--signal-time", default="09:40")
    parser.add_argument("--bar-timestamp", choices=("start", "end"), default="end")
    parser.add_argument("--starting-balance", type=Decimal, default=Decimal("15000000"))
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal(1))
    parser.add_argument("--margin-init", type=Decimal, default=Decimal("0.10"))
    parser.add_argument("--margin-maint", type=Decimal, default=Decimal("0.08"))
    parser.add_argument("--max-quantity", type=Decimal, default=Decimal(100))
    parser.add_argument("--max-notional", type=Decimal, default=Decimal("50000000"))
    parser.add_argument("--max-market-age-seconds", type=int, default=120)
    parser.add_argument("--log-level", choices=("ERROR", "WARNING", "INFO", "DEBUG"), default="WARNING")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--no-tearsheet", action="store_true")
    parser.add_argument("--require-fills", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    from bomber.framework.dataprep.paths import resolve_futures_args, resolve_targets_path
    args = parse_args(argv)
    paths = resolve_futures_args(args, require_roles=False, use_environment=False)
    return run_case(positions_path=resolve_targets_path(args.positions, ROOT), start_day=args.start_day,
        end_day=args.end_day, bars_dir=paths.fut, fut_basic=paths.fut_basic, signal_time=args.signal_time,
        starting_balance=args.starting_balance, commission_per_contract=args.commission_per_contract,
        commission_config=commission_config_from_args(args), margin_init=args.margin_init,
        margin_maint=args.margin_maint, max_quantity=args.max_quantity, max_notional=args.max_notional,
        max_market_age_seconds=args.max_market_age_seconds, bar_timestamp=args.bar_timestamp,
        log_level=args.log_level, report_dir=args.report_dir, tearsheet=not args.no_tearsheet,
        require_fills=args.require_fills)


if __name__ == "__main__":
    main()
