"""EMA历史组装与预热；使用公共Provider工厂，策略本身不读取数据库。"""
import json
import os
import time
from contextlib import nullcontext
from pathlib import Path

from bomber.framework.dataprep.history import (
    HistoryService, DolphinDbHistoryConfig, HistoryError, HistoryBar, ReferenceFactorAdjuster,
    SessionReferenceFactorAdjuster, HistoryUnavailable)
from bomber.framework.dataprep.sources import DolphinDbReferenceConfig
from bomber.framework.dolphindb import DolphinDbConnectionConfig


def build_history(args, *, calendar=None):
    path = getattr(args, "history_config", None) or os.getenv("EMA_HISTORY_CONFIG")
    config = None
    adjuster = None
    if path:
        data = json.loads(Path(path).read_text())
        if args.history_db == "dolphindb":
            allowed = {"database", "table", "columns", "time_unit", "time_label", "instrument_format", "venue",
                "date_column", "time_column", "source_timezone", "factor_source_days", "factor_database"}
            if not data.keys() <= allowed or not {"database", "table", "columns"} <= data.keys():
                raise HistoryError("DolphinDB行情配置须声明database/table/columns及可选时间口径")
            source_days = data.pop("factor_source_days", None)
            factor_database = data.pop("factor_database", None)
            config = DolphinDbHistoryConfig(
                connection=DolphinDbConnectionConfig.from_env(purpose="market",
                    read_timeout_seconds=args.reference_timeout), **data)
            if source_days is not None:
                from datetime import date
                adjuster = ReferenceFactorAdjuster(
                    DolphinDbReferenceConfig.from_env(database=factor_database, read_timeout_seconds=args.reference_timeout),
                    {date.fromisoformat(day): date.fromisoformat(source) for day, source in source_days.items()})
            elif "adjusted_close" not in config.columns:
                if calendar is None:
                    raise HistoryError("原始历史价格自动复权需要会话日历")
                adjuster = SessionReferenceFactorAdjuster(
                    DolphinDbReferenceConfig.from_env(database=factor_database or getattr(args, "reference_database", None),
                        read_timeout_seconds=args.reference_timeout), calendar,
                    factor_date_basis=getattr(args, "factor_date_basis", "source"),
                    max_source_age_days=getattr(args, "reference_max_source_age_days", 14))
        else:
            config = data  # 新Provider自行校验配置，不更改策略/协调器。
    return HistoryService(database_backend=args.history_db, database_config=config,
        file_path=args.history_file, missing_policy=args.history_missing_policy, price_adjuster=adjuster)


def load_startup_history(runner, stamps, before_ns, *, phase, live_bars):
    """只等待启动时最新一分钟的落库延迟；请求窗口固定，指标尚不推进。"""
    service = runner.history_service
    eligible = (not runner.ema_checkpoint.strategy.bars_used and phase == "recovery"
        and service.backend == "dolphindb" and service.missing_policy == "fail"
        and bool(stamps) and 0 < before_ns - stamps[-1] <= 30_000_000_000)
    deadline = time.monotonic() + 20
    while True:
        previous_audit = service.last_audit
        try:
            return service.load(runner.ema_checkpoint.instrument, stamps, phase=phase, live_bars=live_bars)
        except HistoryUnavailable:
            audit = service.last_audit
            remaining = deadline - time.monotonic()
            if (not eligible or audit is previous_audit or not audit or remaining <= 0
                    or audit.get("missing") != 1 or audit.get("first_missing_ns") != stamps[-1]
                    or audit.get("unavailable_sources") or service.backend not in audit.get("sources", ())):
                raise
            if runner.accept_bars:
                raise HistoryError("等待历史落库时实时决策必须保持关闭")
            runner.references.refresh()
            if not runner.session_ready() or not runner.reference_is_current():
                raise HistoryUnavailable("等待历史落库时行情或参考失效，保持闭闸")
            print(f"等待最新历史分钟落库: instrument={runner.ema_checkpoint.instrument} "
                  f"ts_event={stamps[-1]} remaining_seconds={remaining:.1f}", flush=True)
            time.sleep(min(2, remaining))
            runner.references.refresh()
            if not runner.session_ready() or not runner.reference_is_current():
                raise HistoryUnavailable("等待历史落库时行情或参考失效，保持闭闸")
            live_bars = buffered_bars(runner)


def warm_history(runner, before_ns, *, phase):
    checkpoint = runner.ema_checkpoint
    strategy = checkpoint.strategy
    live_bars = buffered_bars(runner)
    if strategy.bars_used:
        if strategy.last_processed_ns >= before_ns:
            raise HistoryError("指标断点晚于历史恢复边界")
        stamps = tuple(runner.calendar.missing_minutes(strategy.last_processed_ns, before_ns))
    else:
        stamps = runner.calendar.last_minutes(before_ns, runner.history_minutes)
        if not stamps and live_bars:
            first = min(bar.ts_event for bar in live_bars)
            stamps = tuple(runner.calendar.missing_minutes(first - 60_000_000_000, before_ns))
    started = time.monotonic()
    action = "历史预热" if not strategy.bars_used else "历史回补"
    # 无缺口的逐分钟检查不打印，实际预热/回补始终记录来源和覆盖。
    if stamps:
        print(f"{action}开始: instrument={checkpoint.instrument} phase={phase} "
              f"backend={runner.history_service.backend} required={len(stamps)} "
              f"first_ns={stamps[0]} last_ns={stamps[-1]} "
              f"missing_policy={runner.history_service.missing_policy}", flush=True)
    try:
        result = load_startup_history(runner, stamps, before_ns, phase=phase, live_bars=live_bars)
        prices = []
        # 全部转换成功才推进指标；没有历史复权口径时不拿当前因子补造。
        for bar in result.bars:
            if bar.adjusted_close is None:
                raise HistoryError("EMA历史需要adjusted_close历史复权价；请提供对应字段/因子适配，不能使用当前因子猜造")
            prices.append((bar.ts_event, bar.adjusted_close))
        client = next(iter(runner._clients.values()))
        with runner._submit_lock, getattr(client, "_submit_lock", nullcontext()):
            for stamp, price in prices:
                checkpoint.warm_closed(stamp, price)
    except Exception as error:
        print(f"{action}失败: instrument={checkpoint.instrument} phase={phase} "
              f"required={len(stamps)} elapsed_seconds={time.monotonic() - started:.3f} "
              f"error={type(error).__name__}: {error}", flush=True)
        raise
    if stamps:
        print(f"{action}完成: instrument={checkpoint.instrument} phase={phase} "
              f"{result.audit()} bars_used={strategy.bars_used} "
              f"ema_initialized={strategy.slow.initialized} "
              f"fast_ema={strategy.fast.value} slow_ema={strategy.slow.value} "
              f"elapsed_seconds={time.monotonic() - started:.3f}", flush=True)
        if result.missing:
            print(f"允许历史不足启动：{result.audit()}", flush=True)
    return result


def buffered_bars(runner):
    if not getattr(runner, "history_buffer", ()):
        return ()
    with runner._submit_lock:
        events = tuple(getattr(runner, "history_buffer", ()))
    result = []
    for bar in events:
        if bar.ts_event < runner.first_complete_bar_ns:
            continue
        if str(bar.bar_type.instrument_id) != runner.ema_checkpoint.instrument:
            raise HistoryError("实时缓冲合约变更")
        assignment = runner.references.snapshot(bar.ts_event)
        if assignment.instrument(runner.ema_checkpoint.strategy.config.product, "main").lower() != runner.ema_checkpoint.instrument.split(".")[0].lower():
            raise HistoryError("实时缓冲期间主力角色变更")
        factor = assignment.factor(runner.ema_checkpoint.strategy.config.product, "main")
        result.append(HistoryBar(runner.ema_checkpoint.instrument, bar.ts_event,
            close=bar.close.as_decimal(), adjusted_close=bar.close.as_decimal() * factor))
    return tuple(result)


def flush_buffer(runner):
    live_bars = buffered_bars(runner)
    if live_bars:
        warm_history(runner, max(bar.ts_event for bar in live_bars) + 1, phase="recovery")
    with runner._submit_lock:
        runner.history_buffer = [bar for bar in runner.history_buffer
            if bar.ts_event > runner.ema_checkpoint.strategy.last_processed_ns]
