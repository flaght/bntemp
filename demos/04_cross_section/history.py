"""横截面Live历史准备：按各品种会话取共同分钟、复权、完整帧初始化。"""
from pathlib import Path
import json

from bomber.framework.dataprep.history import (
    HistoryBar, HistoryError, HistoryService, DolphinDbHistoryConfig,
    SessionReferenceFactorAdjuster)
from bomber.framework.dataprep.sources import DolphinDbReferenceConfig, DataSourcePurpose
from bomber.framework.dolphindb import DolphinDbConnectionConfig
from bomber.framework.dataprep.session_inputs import load_session_inputs, session_inputs_from_args
from bomber.framework.trader.runtime.live.history import (
    HistoryFrameLoader, history_stamps, history_phase, buffered_history, complete_buffer_boundary)


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


class CrossSectionHistory(HistoryFrameLoader):
    def __init__(self, services, calendars, minutes, *, missing_policy="fail", stage="recovery"):
        super().__init__(missing_policy=missing_policy, coverage_key="products")
        self.services, self.calendars = services, calendars
        self.calendar, self.minutes = CommonCalendar(calendars.values()), minutes
        self.stage = stage

    def warm(self, runner, before_ns, *, phase=None):
        strategy = runner.strategy
        stamps = history_stamps(self.calendar, before_ns, self.minutes,
            after_ns=strategy.last_emitted_ns if strategy.synchronized_frames else None)
        if not stamps:
            return
        print(f"横截面历史预热开始: required={len(stamps)} products={list(runner.instruments)}", flush=True)
        buffered = buffered_history(runner)
        live = {product: tuple(HistoryBar(str(instrument), b.ts_event, close=b.close.as_decimal())
            for b in buffered if b.bar_type.instrument_id == instrument
            and b.ts_event >= runner.first_complete_bar_ns) for product, instrument in runner.instruments.items()}
        loaded = self.load_frames({p: (self.services[p], i) for p, i in runner.instruments.items()},
            stamps, phase=phase or self.stage, live_bars=live)
        print(f"横截面历史预热: {self.last_audit}", flush=True)
        source_days = {}
        for product in self.last_audit["products"].values():
            for row in product.get("price_adjustment") or ():
                if row.get("factor_date_basis") == "source":
                    source_days.setdefault(row["trading_day"], set()).add(row["source_day"])
        if any(len(days) > 1 for days in source_days.values()):
            raise HistoryError("同一历史交易日的跨品种因子来源日不一致，拒绝混用")
        # 先验证全部输入再推进策略，价格错误不能留下部分初始化窗口。
        frames = [(stamp, {p: bar.adjusted_close for p, bar in frame.items()}) for stamp, frame in loaded]
        if any(v is None or not v.is_finite() or v <= 0 for _, frame in frames for v in frame.values()):
            raise HistoryError("横截面历史需要有效复权价，不能直接用原始close或当前因子替代")
        with runner._submit_lock:
            for stamp, frame in frames:
                strategy.warm_closed(stamp, frame)
                runner.history_frame_count += 1

    def flush(self, runner):
        boundary = complete_buffer_boundary(buffered_history(runner), runner.fixed_ids,
            first_ns=runner.first_complete_bar_ns, after_ns=runner.strategy.last_emitted_ns)
        if boundary is not None:
            self.warm(runner, boundary, phase="recovery")


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
        config = DolphinDbHistoryConfig(connection=DolphinDbConnectionConfig.from_env(
            purpose="market", read_timeout_seconds=args.reference_timeout), **cfg) if cfg else None
        adjuster = SessionReferenceFactorAdjuster(DolphinDbReferenceConfig.from_env(
            database=factor_db, read_timeout_seconds=args.reference_timeout), calendar,
            factor_date_basis=args.factor_date_basis, max_source_age_days=args.reference_max_source_age_days)
        services[product] = HistoryService(database_config=config, file_path=args.history_file,
            missing_policy="allow", price_adjuster=adjuster)
    stage = history_phase(args.history_stage, calendars.values())
    return CrossSectionHistory(services, calendars, args.history_minutes or args.lookback + 1,
        missing_policy=args.history_missing_policy, stage=stage)
