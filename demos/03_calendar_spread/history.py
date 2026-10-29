"""固定双腿历史准备；复用公共历史Provider，只以完整同步原始价差预热。"""
import json
import math
from pathlib import Path

from bomber.framework.dataprep.history import (
    DolphinDbHistoryConfig, HistoryBar, HistoryError, HistoryService)
from bomber.framework.dolphindb import DolphinDbConnectionConfig
from bomber.framework.trader.runtime.live.history import (
    HistoryFrameLoader, history_stamps, history_phase, buffered_history, complete_buffer_boundary)


class CalendarHistory(HistoryFrameLoader):
    def __init__(self, service, calendar, minutes, *, missing_policy="fail", stage="recovery"):
        super().__init__(missing_policy=missing_policy, coverage_key="legs")
        self.service, self.calendar, self.minutes, self.stage = service, calendar, minutes, stage

    def warm(self, runner, before_ns, *, phase=None):
        strategy = runner.strategy
        stamps = history_stamps(self.calendar, before_ns, self.minutes,
            after_ns=strategy.last_frame_ns if strategy.complete_frames else None)
        if not stamps:
            return
        print(f"跨期历史预热开始: required={len(stamps)} first_ns={stamps[0]} last_ns={stamps[-1]} "
              f"instruments={[str(i) for i in runner.instruments.values()]}", flush=True)
        buffered = buffered_history(runner)
        ids = tuple(runner.instruments.values())
        live = {str(instrument): tuple(HistoryBar(str(instrument), bar.ts_event, close=bar.close.as_decimal())
            for bar in buffered if bar.bar_type.instrument_id == instrument
            and bar.ts_event >= runner.first_complete_bar_ns) for instrument in ids}
        frames = self.load_frames({str(i): (self.service, i) for i in ids}, stamps,
            phase=phase or self.stage, live_bars=live)
        print(f"跨期历史预热: {self.last_audit}", flush=True)
        prices = []
        for stamp, frame in frames:
            first, second = (frame[str(i)].close for i in ids)
            if first is None or second is None or not math.isfinite(float(first - second)):
                raise HistoryError("跨期预热需要双腿原始close，不使用复权价替代")
            prices.append((stamp, first, second))
        pair = tuple(str(i.symbol).lower() for i in ids)
        with runner._submit_lock:
            for stamp, first, second in prices:
                strategy.warm_closed(stamp, first, second, pair)
                runner.history_frame_count = getattr(runner, "history_frame_count", 0) + 1

    def flush(self, runner):
        boundary = complete_buffer_boundary(buffered_history(runner), runner.fixed_ids,
            first_ns=runner.first_complete_bar_ns, after_ns=runner.strategy.last_frame_ns)
        if boundary is not None:
            self.warm(runner, boundary, phase="recovery")


def build_history(args, calendar):
    config = None
    if getattr(args, "history_config", None):
        data = json.loads(Path(args.history_config).read_text())
        # 同一原始分钟配置可与01共用；跨期信号不加载复权因子。
        data.pop("factor_database", None)
        data.pop("factor_source_days", None)
        config = DolphinDbHistoryConfig(connection=DolphinDbConnectionConfig.from_env(
            purpose="market", read_timeout_seconds=args.reference_timeout), **data)
        if "close" not in config.columns:
            raise HistoryError("跨期历史配置必须提供原始close")
    service = HistoryService(database_backend=args.history_db, database_config=config,
        file_path=getattr(args, "history_file", None), missing_policy="allow")
    stage = history_phase(args.history_stage, (calendar,))
    return CalendarHistory(service, calendar, args.history_minutes or args.lookback,
        missing_policy=args.history_missing_policy, stage=stage)
