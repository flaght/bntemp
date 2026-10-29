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


def warm_history_window(service, *, calendar, instrument, before_ns, minutes, consume,
                        phase="preopen", archive=None, archive_source="local-recording", label=""):
    """加载完整单路窗口并调用业务预热；统一来源审计、归档过滤和失败日志。

    consume只接收标准HistoryBar。允许覆盖不足与拒绝来源故障分别处理，
    不调用实时策略，不填造分钟，也不改变服务的来源优先级或恢复阶段。
    """
    import json
    from bomber.framework.dataprep.history import DolphinDbHistoryConfig, HistoryResult
    if type(minutes) is not int or minutes < 0:
        raise HistoryError("历史分钟数须为非负整数")
    if phase not in {"preopen", "recovery"}:
        raise HistoryError("历史阶段须为preopen/recovery")
    if not minutes and archive is None:
        print(f"{label}历史预热未启用: history_minutes=0，等待实时分钟积累窗口", flush=True)
        return None
    end = before_ns // MINUTE_NS * MINUTE_NS
    stamps = history_stamps(calendar, end, minutes)
    started = monotonic()
    backend = archive_source if archive is not None else service.backend
    print(f"{label}预热开始: instrument={instrument} phase={phase} backend={backend} "
          f"required={len(stamps)} first_ns={stamps[0] if stamps else None} "
          f"last_ns={stamps[-1] if stamps else None}", flush=True)
    source = service.config if archive is None else None
    if isinstance(source, DolphinDbHistoryConfig):
        print(f"{label}历史数据源: endpoint={source.connection.host}:{source.connection.port} "
              f"database={source.database} table={source.table}", flush=True)
    try:
        if archive is None:
            result = service.load(str(instrument), stamps, phase=phase)
            if service.backend in result.unavailable_sources:
                raise HistoryUnavailable(f"历史行情源读取失败: {service.backend}；请检查连接和查询")
        else:
            allowed = set(stamps)
            bars = tuple(bar for bar in archive.recent(before_ns=end, limit=minutes)
                         if bar.ts_event in allowed)
            if any(bar.instrument_id != str(instrument) for bar in bars):
                raise HistoryError("本地历史归档与真实合约不一致")
            found = {bar.ts_event for bar in bars}
            missing = tuple(stamp for stamp in stamps if stamp not in found)
            # 归档也报告完整请求窗口的实际覆盖，包括尚未录制的前段。
            result = HistoryResult(bars, missing, (archive_source,), service.missing_policy == "allow")
            service.last_audit = {"phase": "local-recording", "file": str(archive.path), **result.audit()}
            service.audit_history = (getattr(service, "audit_history", []) + [service.last_audit])[-50:]
            if missing and service.missing_policy == "fail":
                raise HistoryUnavailable(f"本地历史窗口缺少{len(missing)}根完整分钟，首根ts_event={missing[0]}")
        consume(result.bars)
    except Exception as error:
        print(f"{label}预热失败: instrument={instrument} phase={phase} required={len(stamps)} "
              f"elapsed_seconds={monotonic()-started:.3f} error={type(error).__name__}: {error}", flush=True)
        print(f"{label}历史读取审计: " + json.dumps(service.last_audit, ensure_ascii=False, default=str), flush=True)
        raise
    print(f"{label}历史加载完成: requested={len(stamps)} loaded={len(result.bars)} "
          f"missing={len(result.missing)} phase={phase} sources={result.sources} "
          f"elapsed_seconds={monotonic()-started:.3f}", flush=True)
    if archive is not None:
        print(f"{label}本地分钟恢复: file={archive.path} loaded={len(result.bars)} "
              f"missing={len(result.missing)} recorded={archive.recorded_bars}", flush=True)
    if result.missing:
        print(f"{label}历史窗口存在真实缺口: first_ns={result.missing[0]} last_ns={result.missing[-1]}；"
              "不补造分钟、不平移时间", flush=True)
    return result


def merge_history_rows(rows, *, calendar, before_ns, minutes, event_ns):
    """按已完成交易分钟合并业务行；业务提供时间映射，相同分钟冲突失败。"""
    expected = set(history_stamps(calendar, before_ns // MINUTE_NS * MINUTE_NS, minutes))
    selected = {}
    for row in rows:
        stamp = event_ns(row)
        if type(stamp) is not int or (stamp+1) % MINUTE_NS:
            raise HistoryError("窗口行须对应完整分钟末纳秒")
        if stamp not in expected:
            continue
        if stamp in selected and selected[stamp] != row:
            raise HistoryError(f"持续窗口与历史源同一分钟内容冲突: ts_event={stamp}")
        selected[stamp] = row
    return tuple(selected[stamp] for stamp in sorted(selected))
