"""Live 历史窗口、同步覆盖和实时衔接；读数与复权沿用 dataprep.history。"""
from datetime import datetime
from time import monotonic

from bomber.framework.dataprep.history import HistoryError, HistoryUnavailable

MINUTE_NS = 60_000_000_000


def history_phase(stage, calendars, *, now=None):
    """auto 仅在所有绑定日历都处于交易窗口时使用盘中恢复来源。"""
    if stage in {"preopen", "recovery"}:
        return stage
    calendars = tuple(calendars)
    if stage != "auto" or not calendars:
        raise HistoryError("历史阶段须为 preopen/recovery/auto，auto 须提供日历")
    now = now if now is not None else datetime.now(calendars[0].zone)
    return "recovery" if all(calendar.window(now) for calendar in calendars) else "preopen"


def history_stamps(calendar, before_ns, minutes, *, after_ns=None):
    """首次取最近 N 个完整交易分钟，已有断点只取其后的真实缺口。"""
    if after_ns is not None:
        if after_ns >= before_ns:
            raise HistoryError("历史断点晚于或等于恢复边界")
        return tuple(calendar.missing_minutes(after_ns, before_ns))
    return tuple(calendar.last_minutes(before_ns, minutes))


def first_complete_minute(now_ns):
    """启动后首个完整分钟的末尾；排除启动时已经形成的部分分钟。"""
    return ((now_ns + MINUTE_NS - 1) // MINUTE_NS + 1) * MINUTE_NS - 1


class HistoryFrameLoader:
    """按同一分钟取多路交集，覆盖核验通过后才返回业务可预热的输入。

    sources 为 {业务键: (HistoryService, 真实合约)}；价格转换、因子跨品种
    一致性及策略 warm_closed 由调用方完成。逐路服务使用 allow 收集完整覆盖，
    本层的 missing_policy 决定是否接受不完整窗口；来源故障始终失败。
    """
    def __init__(self, *, missing_policy="fail", coverage_key="instruments"):
        if missing_policy not in {"fail", "allow"}:
            raise HistoryError("历史缺失策略须为 fail/allow")
        self.missing_policy = missing_policy
        self.coverage_key = coverage_key
        self.last_audit, self.audit_history = None, []

    def load_frames(self, sources, stamps, *, phase, live_bars=None):
        if not sources:
            raise HistoryError("同步历史须声明至少一路真实合约")
        if any(service.missing_policy != "allow" for service, _ in sources.values()):
            raise HistoryError("逐路历史服务须使用 allow 收集覆盖，由同步加载器统一核验缺失")
        stamps = tuple(sorted(set(stamps)))
        started = monotonic()
        rows, coverage = {}, {}
        for key, (service, instrument) in sources.items():
            result = service.load(str(instrument), stamps, phase=phase,
                live_bars=(live_bars or {}).get(key, ()))
            coverage[key] = {**service.last_audit, "instrument": str(instrument),
                "allowed_missing": self.missing_policy == "allow"}
            rows[key] = {bar.ts_event: bar for bar in result.bars}
        complete = tuple(stamp for stamp in stamps if all(stamp in rows[key] for key in sources))
        found = set(complete)
        missing = tuple(stamp for stamp in stamps if stamp not in found)
        audit = {"phase": phase, "required": len(stamps), "loaded": len(complete),
            "missing": len(missing), "first_missing_ns": missing[0] if missing else None,
            "last_missing_ns": missing[-1] if missing else None,
            "sources": sorted({source for row in coverage.values() for source in row["sources"]}),
            "allowed_missing": self.missing_policy == "allow", self.coverage_key: coverage,
            "load_seconds": round(monotonic() - started, 3)}
        self.last_audit = audit
        self.audit_history = (self.audit_history + [audit])[-50:]
        if any(row["unavailable_sources"] for row in coverage.values()):
            raise HistoryUnavailable(f"历史来源不可用，逐路覆盖={coverage}")
        if missing and self.missing_policy == "fail":
            raise HistoryUnavailable(f"历史缺少{len(missing)}个完整同步帧，逐路覆盖={coverage}")
        return tuple((stamp, {key: rows[key][stamp] for key in sources}) for stamp in complete)


def buffered_history(runner):
    """取得同一代缓冲快照；事件到 HistoryBar 的业务转换由调用方负责。"""
    with runner._submit_lock:
        return tuple(getattr(runner, "history_buffer", ()))


def buffer_history_bar(runner, feed_id, bar):
    """预热期间只缓存声明来源的非未来行情；调用方负责失败时的业务闭闸。"""
    with runner._submit_lock:
        if not getattr(runner, "history_buffering", False):
            return False
        instruments = (set(runner.instruments.values()) if hasattr(runner, "instruments")
                       else {runner.bound_instrument})
        if feed_id != runner.market_feed_id or bar.bar_type.instrument_id not in instruments:
            raise HistoryError("历史缓冲来源或合约与绑定不一致")
        if bar.ts_event > runner._clock_ns():
            raise HistoryError("历史缓冲 Bar 来自未来")
        if len(runner.history_buffer) >= 10000:
            raise HistoryError("历史加载期间实时缓冲溢出")
        runner.history_buffer.append(bar)
        return True


def complete_buffer_boundary(events, instruments, *, first_ns, after_ns):
    """只以全部合约到齐的最后一分钟确定回补边界，不补造缺失腿。"""
    required, groups = set(instruments), {}
    for bar in events:
        if bar.ts_event >= first_ns:
            groups.setdefault(bar.ts_event, set()).add(bar.bar_type.instrument_id)
    complete = [stamp for stamp, ids in groups.items() if ids == required and stamp > after_ns]
    return max(complete) + 1 if complete else None


def start_after_history(runner, *, flush, validate, replay_buffer=False):
    """在提交锁内合并缓冲、核验并放行；历史处理不经实时策略回调。

    多腿 Runner 可回放剩余事件，让原有同步器保留不完整帧。已预热事件由
    Runner 的处理断点过滤；EMA 等单路预热可直接消耗缓冲，不重放交易。
    """
    with runner._submit_lock:
        runner.accept_bars = False
        window = getattr(runner, "window_context", None)
        if window is not None and window.stop_requested():
            window.pause_runner(runner)
            return
        try:
            flush()
            if window is not None and window.stop_requested():
                window.pause_runner(runner)
                return
            validate()
            if window is not None and window.stop_requested():
                window.pause_runner(runner)
                return
            pending = sorted(runner.history_buffer, key=lambda bar: bar.ts_event)
            runner.history_buffer = []
            runner.history_buffering, runner.accept_bars = False, True
            if replay_buffer:
                for bar in pending:
                    runner.publish(runner.market_feed_id, bar)
        except Exception:
            runner.accept_bars = False
            raise
