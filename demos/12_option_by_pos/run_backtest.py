"""期货 M1 + 外部期权 M5 Feather 的目标持仓回测。"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from bomber.framework.dataprep.commissions import add_commission_arguments, commission_config_from_args
from bomber.framework.dataprep.session import input_session, current_session, write_input_reports

DEFAULT_POSITIONS = ROOT / "3rd/strategies/kd_option_by_pos/positions.csv"
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent / "results"
PAIRS = {"MO": ("IM", "000852"), "IO": ("IF", "000300"), "HO": ("IH", "000016")}


def export_fills(frame, path):
    import pandas as pd
    rows = []
    for order_id, row in frame.iterrows():
        stamp = pd.Timestamp(row["ts_event"])
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp
        rows.append(dict(timestamp=stamp.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
            instrument_id=row["instrument_id"], order_id=order_id, side=row["order_side"],
            qty=row["last_qty"], price=row["last_px"],
            commission=Decimal(str(row["commission"]).rsplit(" ", 1)[0].replace(",", ""))))
    rows.sort(key=lambda row: (row["timestamp"], row["instrument_id"], str(row["order_id"])))
    pd.DataFrame(rows, columns=("timestamp", "instrument_id", "order_id", "side", "qty", "price",
                                "commission")).to_csv(path, index=False)


@input_session
def run_case(args):
    import pandas as pd
    from bomber.backtest.config import BacktestEngineConfig
    from bomber.config import LoggingConfig
    from bomber.model import Venue
    from bomber.model.identifiers import TraderId
    from bomber.framework.dataprep.bars import read_bar_frame, add_prepared_bar_source
    from bomber.framework.dataprep.catalog import scan_bar_files
    from bomber.framework.dataprep.contracts import BarReadSpec, BarFileKey
    from bomber.framework.dataprep.metadata import load_options_basic, contract_rows, venue_of
    from bomber.framework.dataprep.option_settlements import load_option_settlements
    from bomber.framework.dataprep.paths import resolve_option_args, resolve_targets_path
    from bomber.framework.dataprep.session import read_feather
    from bomber.framework.dataprep.futures import make_instrument
    from bomber.framework.trader.instrument_factory import day_ns, make_option, instrument_meta
    from bomber.framework.market.basic.base import DataType
    from bomber.framework.market.replay.base import FileReplayFeed
    from bomber.framework.trader import (CtpFuturesBasicProfile, DataBinding, ExecutionRoute,
        MarketStreamBinding, NautilusMarketFeedAdapter, NautilusSimExecutionBackend,
        RiskLimits, RuntimeMode, UnifiedHistoricalRuntime, UnifiedStrategyRunner)
    from bomber.framework.trader.assembly import StrategyBindings, assemble_strategy
    from bomber.framework.trader.execution.builders import build_simulation_execution

    paths = resolve_option_args(args, require_futures=True, require_index=False, use_environment=False)
    positions_path = resolve_targets_path(args.positions, ROOT)
    current_session().fingerprint(positions_path)
    module = importlib.import_module("demos.12_option_by_pos.strategy")
    strategy = module.OptionByPosStrategy("option-by-pos", positions_path,
        module.OptionByPosConfig(args.legacy_close_early, args.missing_signal_policy),
        start_day=args.start_day, end_day=args.end_day)
    if strategy.max_target_quantity > args.max_quantity:
        raise ValueError("目标手数超过 --max-quantity")
    option_symbols = {symbol for symbol, kind in strategy.instrument_types.items() if kind == "OPTION"}
    future_symbols = set(strategy.get_insts()) - option_symbols
    options = {}
    for product in sorted({symbol[:2] for symbol in option_symbols}):
        index = PAIRS[product][1]
        options.update({key: value for key, value in load_options_basic(paths.opt_basic, product, index).items()
                        if key in option_symbols})
    if option_symbols - set(options):
        raise ValueError(f"opt_basic 缺少合约: {sorted(option_symbols - set(options))}")
    futures = contract_rows(read_feather(paths.fut_basic), future_symbols)
    if any(venue_of(row) != "CFFEX" for row in futures.values()):
        raise ValueError("期货基础资料必须属于 CFFEX")
    last_trade = {key: day_ns(info.last_trade, "15:00") for key, info in options.items()}
    listed = {key: day_ns(info.listed) for key, info in options.items()}
    # futures.make_instrument 沿用现有期货条款解析和费用字段。
    for key, row in futures.items():
        last_trade[key] = day_ns(pd.Timestamp(row["lastTradeDate"]).date(), "15:00")
        listed[key] = day_ns(pd.Timestamp(row["listDate"]).date())
    for ts, targets in strategy.schedule.items():
        if any(qty and ts < listed[key] for key, qty in targets.items() if key in listed):
            raise ValueError("目标文件包含合约上市前的非零目标")
        if any(qty and ts >= last_trade[key] for key, qty in targets.items() if key in last_trade):
            raise ValueError("目标文件包含最后交易时刻之后的非零目标")
    strategy.set_lifecycles(last_trade)
    settlements = (load_option_settlements(args.option_settlements, options,
        start_day=args.start_day, end_day=args.end_day, available_time=args.settlement_available_time,
        fee_per_contract=args.exercise_fee_per_contract) if args.option_settlements else {})
    qualified_settlements = {item.instrument_id: item for item in settlements.values()}
    # 是否需要终值由到期时的实际残仓决定。目标归零不代表一定成交，
    # 因此交给生命周期模块在最后一根 Bar 撮合、余单撤销之后检查。
    period_end_ns = day_ns(args.end_day + timedelta(days=1)) - 1
    if any(item.available_ns > period_end_ns for item in settlements.values()):
        raise ValueError("结算可用时刻超过回测结束；请延长 --end-day")

    feed = FileReplayFeed("OPTION_BY_POS_REPLAY")
    backend = NautilusSimExecutionBackend("option-by-pos-sim", BacktestEngineConfig(
        trader_id=TraderId("OPTION-BY-POS-001"), logging=LoggingConfig(log_level=args.log_level),
        run_analysis=True), execution_timing=args.execution_timing,
        execution_delay_bars=args.execution_delay_bars)
    runtime = None
    try:
        profile = CtpFuturesBasicProfile(venue=Venue("CFFEX"), starting_balance=args.starting_balance,
            commission_per_contract=args.commission_per_contract,
            option_commission_per_contract=args.option_commission_per_contract,
            **commission_config_from_args(args).profile_kwargs())
        backend.add_profile(profile, option_settlements=qualified_settlements,
            option_last_trade_times={key + ".CFFEX": last_trade[key] for key in options},
            settlement_end_ns=period_end_ns)
        instruments, multipliers, specs = {}, {}, {}
        for key, row in futures.items():
            contract, meta, multiplier = make_instrument(row, profile, args.margin_init, args.margin_maint)
            backend.add_instrument(contract)
            feed.register_instrument(meta)
            instruments[key], multipliers[contract.id] = contract.id, multiplier
            specs[key] = BarReadSpec(timestamp_label=args.futures_bar_timestamp,
                                    trading_day_policy="day_session")
        for key, info in options.items():
            contract = make_option(SimpleNamespace(symbol=key, tick=info.tick, multiplier=info.multiplier,
                strike=info.strike, list_day=info.listed, last_day=info.last_trade),
                args.option_margin_init, args.option_margin_maint, info.underlying,
                kind=info.option_kind, profile_id="OPTION_BY_POS", expiry_policy="CASH_SETTLEMENT")
            backend.add_instrument(contract)
            feed.register_instrument(instrument_meta(contract))
            instruments[key], multipliers[contract.id] = contract.id, info.multiplier
            specs[key] = BarReadSpec(interval_seconds=300, timestamp_label=args.options_bar_timestamp,
                trading_day_policy="day_session", ohlc_policy=args.option_ohlc_policy,
                zero_price_policy=args.option_zero_price_policy)
        inventories = {"FUTURE": scan_bar_files(paths.fut_dir, "future", args.start_day, args.end_day),
                       "OPTION": scan_bar_files(paths.opt_dir, "option", args.start_day, args.end_day)}
        files_by_identity = {}
        loaded_rows = {}
        for kind, inventory in inventories.items():
            for key, path in sorted(inventory.items()):
                symbol = key.symbol
                if symbol not in instruments or strategy.instrument_types[symbol] != kind:
                    continue
                if not listed[symbol] <= day_ns(key.trading_day) <= last_trade[symbol]:
                    continue
                result = read_bar_frame(path, specs[symbol], BarFileKey(
                    key.asset_kind, symbol, key.trading_day, "CFFEX"))
                files_by_identity[key.trading_day, symbol] = path
                if result.frame.empty:
                    continue
                add_prepared_bar_source(feed, result, instruments[symbol])
                loaded_rows[symbol] = loaded_rows.get(symbol, 0) + len(result.frame)
        if not loaded_rows:
            raise ValueError("没有可回放的行情")
        # 提前指出显式目标当日缺文件；保留目标文件决定的范围，不截断策略。
        for ts, targets in strategy.schedule.items():
            day = pd.Timestamp(ts, tz="UTC").tz_convert("Asia/Shanghai").date()
            missing = [key for key in targets if key in instruments and ts < last_trade[key]
                       and (day, key) not in files_by_identity]
            if missing:
                raise FileNotFoundError(f"{day}: 目标合约缺少行情文件: {missing}")
        execution = build_simulation_execution(backend, instrument_limits={item: RiskLimits(
            max_order_quantity=args.max_quantity * 2, max_abs_position=args.max_quantity,
            max_order_notional=args.max_notional, max_abs_position_notional=args.max_notional,
            max_market_age_ns=(args.option_max_market_age_seconds if key in options
                              else args.futures_max_market_age_seconds) * 1_000_000_000,
            contract_multiplier=multipliers[item]) for key, item in instruments.items()})
        runner = UnifiedStrategyRunner(RuntimeMode.HISTORICAL, position_manager=execution.positions)
        bindings = StrategyBindings(data=tuple(DataBinding(key, "positions-bars", item, DataType.BAR,
            specs[key].bar_spec) for key, item in instruments.items()),
            execution=tuple(ExecutionRoute(key, execution.client.client_id, item)
                            for key, item in instruments.items()))
        assemble_strategy(runner, strategy, feeds={"positions-bars": feed}, execution=execution, bindings=bindings)
        adapter = NautilusMarketFeedAdapter("option-by-pos-clock", feed, backend,
            tuple(MarketStreamBinding(item, DataType.BAR, specs[key].bar_spec)
                  for key, item in instruments.items()), manage_lifecycle=False)
        runtime = UnifiedHistoricalRuntime("option-by-pos-runtime", runner, adapter)
        result = runtime.run()
        destination = Path(args.report_dir) / str(result.backend_result.run_id)
        destination.mkdir(parents=True, exist_ok=False)
        write_input_reports(destination)
        backend.write_commission_report(destination)
        trader = backend.engine.trader
        fills = trader.generate_fills_report()
        for name, frame in (("fills.csv", fills), ("orders.csv", trader.generate_orders_report()),
                            ("positions.csv", trader.generate_positions_report()),
                            ("account.csv", trader.generate_account_report(Venue("CFFEX")))):
            frame.to_csv(destination / name)
        export_fills(fills, destination / "fills_output.csv")
        pd.DataFrame(strategy.target_records).to_csv(destination / "signals.csv", index=False)
        pd.DataFrame(backend.option_settlement_records).to_csv(destination / "settlements.csv", index=False)
        missing_slots = sorted(set(strategy.schedule) - strategy.handled_slots)
        summary = dict(positions_path=str(positions_path), start_day=str(args.start_day), end_day=str(args.end_day),
            futures_dir=str(paths.fut_dir), options_dir=str(paths.opt_dir), opt_basic=str(paths.opt_basic),
            option_settlements=str(args.option_settlements) if args.option_settlements else None,
            settlement_prices_loaded=len(settlements),
            execution_timing=args.execution_timing, execution_delay_bars=backend.execution_delay_bars,
            intervals={key: spec.bar_spec for key, spec in specs.items()}, bars_by_instrument=loaded_rows,
            fills=len(fills), settlements=len(backend.option_settlement_records),
            untriggered_slots=missing_slots, skipped_slots=strategy.skipped_slots,
            framework_positions={key: str(execution.positions.account_position(backend.backend_id, item))
                                 for key, item in instruments.items()},
            strategy_positions={key: str(execution.positions.position(strategy.strategy_id, key))
                                for key in instruments},
            report_errors=[str(error) for error in execution.client.report_errors])
        (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        print(f"回测报表: {destination.resolve()}")
        strategy.verify_signals()
        if execution.client.report_errors:
            raise AssertionError(f"执行回报冲突: {execution.client.report_errors}")
        if args.require_fills and fills.empty:
            raise AssertionError("没有模拟成交")
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
    for name in ("data-root", "fut-dir", "opt-dir", "fut-basic", "opt-basic"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--option-settlements", type=Path,
        help="标准最终结算文件，或含 code,trade_date,settlPrice 的 YYYY-MM-DD.feather 日行情目录")
    parser.add_argument("--settlement-available-time", default="16:00")
    parser.add_argument("--exercise-fee-per-contract", type=Decimal, default=Decimal(0))
    parser.add_argument("--starting-balance", type=Decimal, default=Decimal("40000000"))
    parser.add_argument("--commission-per-contract", type=Decimal, default=Decimal(1))
    parser.add_argument("--option-commission-per-contract", type=Decimal, default=Decimal("0.15"))
    parser.add_argument("--margin-init", type=Decimal, default=Decimal("0.10"))
    parser.add_argument("--margin-maint", type=Decimal, default=Decimal("0.08"))
    parser.add_argument("--option-margin-init", type=Decimal, default=Decimal("0.15"))
    parser.add_argument("--option-margin-maint", type=Decimal, default=Decimal("0.10"))
    parser.add_argument("--max-quantity", type=Decimal, default=Decimal(1000))
    parser.add_argument("--max-notional", type=Decimal, default=Decimal("500000000"))
    parser.add_argument("--futures-max-market-age-seconds", type=int, default=120)
    parser.add_argument("--option-max-market-age-seconds", type=int, default=300)
    parser.add_argument("--futures-bar-timestamp", choices=("start", "end"), default="end")
    parser.add_argument("--options-bar-timestamp", choices=("start", "end"), default="end")
    parser.add_argument("--option-ohlc-policy", choices=("reject", "expand_range"), default="reject")
    parser.add_argument("--option-zero-price-policy", choices=("reject", "drop"), default="reject")
    parser.add_argument("--legacy-close-early", action="store_true")
    parser.add_argument("--missing-signal-policy", choices=("error", "skip"), default="error")
    parser.add_argument("--execution-timing", choices=("native", "next_market"), default="next_market")
    parser.add_argument("--execution-delay-bars", type=int)
    parser.add_argument("--log-level", choices=("ERROR", "WARNING", "INFO", "DEBUG"), default="WARNING")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--require-fills", action="store_true")
    args = parser.parse_args(argv)
    if args.end_day < args.start_day:
        parser.error("结束日期不能早于开始日期")
    if args.execution_delay_bars is None:
        args.execution_delay_bars = 1 if args.execution_timing == "next_market" else 0
    if args.execution_delay_bars < 0 or (args.execution_timing == "next_market" and args.execution_delay_bars < 1) or (
            args.execution_timing == "native" and args.execution_delay_bars != 0):
        parser.error("native 只支持延迟0根，next_market 至少延迟1根")
    for name in ("starting_balance", "max_quantity", "max_notional", "margin_init", "margin_maint",
                 "option_margin_init", "option_margin_maint"):
        value = getattr(args, name)
        if not value.is_finite() or value <= 0:
            parser.error(f"{name} 必须为有限正数")
    for name in ("commission_per_contract", "option_commission_per_contract", "exercise_fee_per_contract"):
        value = getattr(args, name)
        if not value.is_finite() or value < 0:
            parser.error(f"{name} 必须为有限非负数")
    if (args.margin_init > 1 or args.margin_maint > args.margin_init or args.option_margin_init > 1
            or args.option_margin_maint > args.option_margin_init or args.max_quantity % 1
            or min(args.futures_max_market_age_seconds, args.option_max_market_age_seconds) < 1):
        parser.error("保证金、最大手数或行情时效参数无效")
    return args


if __name__ == "__main__":
    run_case(parse_args())
