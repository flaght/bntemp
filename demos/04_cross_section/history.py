"""横截面Live历史准备：按各品种会话取共同分钟、复权、完整帧初始化。"""
from datetime import datetime
from pathlib import Path
import json
import time

from bomber.framework.dataprep.history import (
    HistoryBar, HistoryError, HistoryUnavailable, HistoryService, DolphinDbHistoryConfig,
    SessionReferenceFactorAdjuster)
from bomber.framework.dataprep.sources import DolphinDbReferenceConfig, DataSourcePurpose
from bomber.framework.dataprep.session_inputs import load_session_inputs, session_inputs_from_args


class CommonCalendar:
    def __init__(self, calendars):
        self.calendars = tuple(calendars)
        self.zone = self.calendars[0].zone

    def last_minutes(self, before, count):
        size = count
        while size <= 10000:
            common = set.intersection(*(set(c.last_minutes(before, size)) for c in self.calendars))
            if len(common) >= count:
                return tuple(sorted(common)[-count:])
            size = max(size + 1, size * 2)
        raise HistoryError("声明范围内找不到足够的跨品种共同交易分钟")

    def missing_minutes(self, after, before):
        return tuple(sorted(set.intersection(*(set(c.missing_minutes(after, before)) for c in self.calendars))))


class CrossSectionHistory:
    def __init__(self, services, calendars, minutes, *, missing_policy="fail", stage="recovery"):
        self.services, self.calendars = services, calendars
        self.calendar, self.minutes = CommonCalendar(calendars.values()), minutes
        self.missing_policy, self.stage = missing_policy, stage
        self.last_audit, self.audit_history = None, []

    def warm(self, runner, before_ns, *, phase=None):
        strategy = runner.strategy
        stamps = (self.calendar.missing_minutes(strategy.last_emitted_ns, before_ns)
            if strategy.synchronized_frames else self.calendar.last_minutes(before_ns, self.minutes))
        if not stamps:
            return
        started = time.monotonic()
        print(f"横截面历史预热开始: required={len(stamps)} products={list(runner.instruments)}", flush=True)
        with runner._submit_lock:
            buffered = tuple(runner.history_buffer)
        coverage, prices = {}, {}
        phase = phase or self.stage
        for product, instrument in runner.instruments.items():
            service = self.services[product]
            live = tuple(HistoryBar(str(instrument), b.ts_event, close=b.close.as_decimal())
                for b in buffered if b.bar_type.instrument_id == instrument and b.ts_event >= runner.first_complete_bar_ns)
            result = service.load(str(instrument), stamps, phase=phase, live_bars=live)
            coverage[product] = {**service.last_audit, "instrument": str(instrument),
                                 "allowed_missing": self.missing_policy == "allow"}
            prices[product] = {b.ts_event: b.adjusted_close for b in result.bars}
        complete = tuple(s for s in stamps if all(s in prices[p] for p in prices))
        found = set(complete)
        missing = tuple(s for s in stamps if s not in found)
        audit = {"phase": phase, "required": len(stamps), "loaded": len(complete), "missing": len(missing),
            "first_missing_ns": missing[0] if missing else None, "last_missing_ns": missing[-1] if missing else None,
            "sources": sorted({s for c in coverage.values() for s in c["sources"]}),
            "allowed_missing": self.missing_policy == "allow", "products": coverage,
            "load_seconds": round(time.monotonic() - started, 3)}
        self.last_audit, self.audit_history = audit, (self.audit_history + [audit])[-50:]
        print(f"横截面历史预热: {audit}", flush=True)
        if any(c["unavailable_sources"] for c in coverage.values()):
            raise HistoryUnavailable("横截面历史来源不可用，详见逐品种覆盖")
        if missing and self.missing_policy == "fail":
            raise HistoryUnavailable(f"横截面缺少{len(missing)}个完整同步帧；逐品种覆盖={coverage}")
        source_days = {}
        for product in coverage.values():
            for row in product.get("price_adjustment") or ():
                if row.get("factor_date_basis") == "source":
                    source_days.setdefault(row["trading_day"], set()).add(row["source_day"])
        if any(len(days) > 1 for days in source_days.values()):
            raise HistoryError("同一历史交易日的跨品种因子来源日不一致，拒绝混用")
        # 先验证全部输入再推进策略，价格错误不能留下部分初始化窗口。
        frames = [(s, {p: prices[p][s] for p in prices}) for s in complete]
        if any(v is None or not v.is_finite() or v <= 0 for _, frame in frames for v in frame.values()):
            raise HistoryError("横截面历史需要有效复权价，不能直接用原始close或当前因子替代")
        with runner._submit_lock:
            for stamp, frame in frames:
                strategy.warm_closed(stamp, frame)
                runner.history_frame_count += 1

    def flush(self, runner):
        groups = {}
        for bar in runner.history_buffer:
            if bar.ts_event >= runner.first_complete_bar_ns:
                groups.setdefault(bar.ts_event, set()).add(bar.bar_type.instrument_id)
        complete = [s for s, ids in groups.items() if ids == runner.fixed_ids]
        if complete and max(complete) > runner.strategy.last_emitted_ns:
            self.warm(runner, max(complete) + 1, phase="recovery")


def build_history(args, references):
    data = json.loads(Path(args.history_config).read_text()) if args.history_config else None
    if data and "sources" in data and set(data["sources"]) != set(args.products):
        raise HistoryError("多源历史配置须完整覆盖本次products")
    services, calendars = {}, {}
    for product, instrument in references.instrument_ids.items():
        calendar = (getattr(args, "_session_calendars", {}) or {}).get(product)
        if calendar is None:
            calendar = load_session_inputs(session_inputs_from_args(args, product=product,
                exchange=str(instrument.venue)), purpose=DataSourcePurpose.LIVE_REFERENCE)
        if calendar.product != product:
            raise HistoryError("历史会话品种与绑定不符")
        if calendar.binding.exchange and calendar.binding.exchange != str(instrument.venue):
            raise HistoryError("历史会话交易所与品种绑定不符")
        calendars[product] = calendar
        cfg = dict(data["sources"][product] if data and "sources" in data else data or {})
        factor_db = cfg.pop("factor_database", None) or args.reference_database
        if "factor_source_days" in cfg:
            raise HistoryError("04历史因子按交易会话自动选择，不使用手工来源日映射")
        config = DolphinDbHistoryConfig(connection=DolphinDbReferenceConfig.from_env(
            read_timeout_seconds=args.reference_timeout), **cfg) if cfg else None
        adjuster = SessionReferenceFactorAdjuster(DolphinDbReferenceConfig.from_env(
            database=factor_db, read_timeout_seconds=args.reference_timeout), calendar,
            factor_date_basis=args.factor_date_basis, max_source_age_days=args.reference_max_source_age_days)
        services[product] = HistoryService(database_config=config, file_path=args.history_file,
            missing_policy="allow", price_adjuster=adjuster)
    stage = args.history_stage
    if stage == "auto":
        now = datetime.now(next(iter(calendars.values())).zone)
        stage = "recovery" if all(c.window(now) for c in calendars.values()) else "preopen"
    return CrossSectionHistory(services, calendars, args.history_minutes or args.lookback + 1,
        missing_policy=args.history_missing_policy, stage=stage)
