"""Hybrid 原始分钟预热；共用历史 Provider，不发布历史目标。"""
import json
from pathlib import Path

from bomber.framework.dataprep.history import HistoryService, DolphinDbHistoryConfig, HistoryError
from bomber.framework.dataprep.sources import DolphinDbReferenceConfig
from bomber.framework.calendars.sessions import ns_datetime

MINUTE = 60_000_000_000


def build_history(args):
    config = None
    if args.history_config:
        values = json.loads(Path(args.history_config).read_text())
        config = DolphinDbHistoryConfig(
            connection=DolphinDbReferenceConfig.from_env(), **values)
        required = {'open', 'high', 'low', 'close', 'volume', 'value', 'open_interest'}
        if required - config.columns.keys():
            raise HistoryError(f"Hybrid 历史配置缺少原始字段: {sorted(required - config.columns.keys())}")
    return HistoryService(database_config=config, file_path=args.history_file, missing_policy='fail')


def warm_history(runner, args):
    if not args.history_minutes:
        return
    end = runner._clock_ns() // MINUTE * MINUTE
    stamps = runner.calendar.last_minutes(end, args.history_minutes)
    print(f"Hybrid预热开始: required={len(stamps)} instrument={runner.bound_instrument}", flush=True)
    result = runner.history_service.load(str(runner.bound_instrument), stamps,
        phase='recovery' if args.resume else 'preopen')
    rows = []
    for bar in result.bars:
        fields = ('open', 'high', 'low', 'close', 'volume', 'value', 'open_interest')
        if any(getattr(bar, field) is None for field in fields):
            raise HistoryError('Hybrid历史分钟必须包含原始OHLCV、value、open_interest')
        if bar.cumulative_factor is not None or bar.adjusted_close is not None:
            raise HistoryError('Hybrid模型使用原始行情，历史源不能提供复权分钟')
        if bar.volume <= 0:
            raise HistoryError('Hybrid历史分钟成交量须为正')
        point = ns_datetime(bar.ts_event, runner.calendar.zone)
        if runner.calendar.window(point) is None:
            raise HistoryError('Hybrid历史包含盘外分钟')
        label = bar.ts_event + 1 - (MINUTE if args.model_bar_timestamp == 'start' else 0)
        row = {name: float(getattr(bar, name)) for name in fields if name != 'open_interest'}
        row.update(trade_time=ns_datetime(label, runner.calendar.zone), code=args.product,
                   openint=float(bar.open_interest),
                   vwap=float(bar.value / bar.volume / runner.strategy.config.contract_multiplier))
        tolerance = max(1e-8, row['high'] * 1e-6)
        if not row['low'] - tolerance <= row['vwap'] <= row['high'] + tolerance:
            raise HistoryError('Hybrid历史VWAP与OHLC不一致')
        rows.append(row)
    count = runner.strategy.predictor.warm_history(rows)
    runner.history_last_ns = max((bar.ts_event for bar in result.bars), default=-1)
    print(f"Hybrid预热完成: loaded={count} sources={result.sources}", flush=True)
