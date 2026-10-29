"""固定双腿历史准备；复用公共历史Provider，只以完整同步原始价差预热。"""
import json
import math
import time
from pathlib import Path

from bomber.framework.dataprep.history import (
    DolphinDbHistoryConfig, HistoryBar, HistoryError, HistoryService, HistoryUnavailable)
from bomber.framework.dolphindb import DolphinDbConnectionConfig


class CalendarHistory:
    def __init__(self, service, calendar, minutes, *, missing_policy="fail", stage="recovery"):
        self.service, self.calendar, self.minutes = service, calendar, minutes
        self.missing_policy, self.stage = missing_policy, stage
        self.last_audit = None
        self.audit_history = []

    def warm(self, runner, before_ns, *, phase=None):
        strategy = runner.strategy
        stamps = (self.calendar.missing_minutes(strategy.last_frame_ns, before_ns)
            if strategy.complete_frames else self.calendar.last_minutes(before_ns, self.minutes))
        stamps = tuple(stamps)
        if not stamps:
            return
        started = time.monotonic()
        print(f"跨期历史预热开始: required={len(stamps)} first_ns={stamps[0]} last_ns={stamps[-1]} "
              f"instruments={[str(i) for i in runner.instruments.values()]}", flush=True)
        # 每腿显式保留覆盖，最后按时间交集组帧；不跨分钟或复制缺失腿。
        legs, coverage = {}, {}
        with runner._submit_lock:
            buffered = tuple(runner.history_buffer)
        for instrument in runner.instruments.values():
            live = tuple(HistoryBar(str(instrument), bar.ts_event, close=bar.close.as_decimal())
                for bar in buffered if bar.bar_type.instrument_id == instrument
                and bar.ts_event >= runner.first_complete_bar_ns)
            result = self.service.load(str(instrument), stamps, phase=phase or self.stage, live_bars=live)
            coverage[str(instrument)] = dict(self.service.last_audit)
            coverage[str(instrument)]["allowed_missing"] = self.missing_policy == "allow"
            legs[instrument] = {bar.ts_event: bar for bar in result.bars}
        ids = tuple(runner.instruments.values())
        complete = tuple(stamp for stamp in stamps if all(stamp in legs[i] for i in ids))
        complete_set = set(complete)
        missing = tuple(stamp for stamp in stamps if stamp not in complete_set)
        audit = {"phase": phase or self.stage, "required": len(stamps), "loaded": len(complete),
            "missing": len(missing), "first_missing_ns": missing[0] if missing else None,
            "last_missing_ns": missing[-1] if missing else None,
            "sources": sorted({s for row in coverage.values() for s in row["sources"]}),
            "allowed_missing": self.missing_policy == "allow", "legs": coverage,
            "load_seconds": round(time.monotonic() - started, 3)}
        self.last_audit = audit
        self.audit_history = (self.audit_history + [audit])[-50:]
        print(f"跨期历史预热: {audit}", flush=True)
        if any(row["unavailable_sources"] for row in coverage.values()):
            raise HistoryUnavailable("跨期历史来源不可用，详见逐腿覆盖报告")
        if missing and self.missing_policy == "fail":
            raise HistoryUnavailable(f"跨期历史缺少{len(missing)}个完整双腿帧；逐腿覆盖={coverage}")
        prices = []
        for stamp in complete:
            first, second = (legs[i][stamp].close for i in ids)
            if first is None or second is None or not math.isfinite(float(first - second)):
                raise HistoryError("跨期预热需要双腿原始close，不使用复权价替代")
            prices.append((stamp, first, second))
        pair = tuple(str(i.symbol).lower() for i in ids)
        with runner._submit_lock:
            for stamp, first, second in prices:
                strategy.warm_closed(stamp, first, second, pair)
                runner.history_frame_count = getattr(runner, "history_frame_count", 0) + 1

    def flush(self, runner):
        groups = {}
        for bar in runner.history_buffer:
            if bar.ts_event >= runner.first_complete_bar_ns:
                groups.setdefault(bar.ts_event, set()).add(bar.bar_type.instrument_id)
        complete = [stamp for stamp, instruments in groups.items() if instruments == runner.fixed_ids]
        if complete and max(complete) > runner.strategy.last_frame_ns:
            self.warm(runner, max(complete) + 1, phase="recovery")


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
    stage = args.history_stage
    if stage == "auto":
        from datetime import datetime
        stage = "recovery" if calendar.window(datetime.now(calendar.zone)) else "preopen"
    return CalendarHistory(service, calendar, args.history_minutes or args.lookback,
        missing_policy=args.history_missing_policy, stage=stage)
