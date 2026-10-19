"""08文件回测：既有特征可见时钟选版本，原生模拟时钟封闭分钟帧。"""
from collections import defaultdict

from bomber.framework.datahub import DataHub
from bomber.framework.datahub.features import (FeatureHub, FeatureRequirement,
    FeatureError, MarketFrameInput, replay_feature_frames)
from bomber.framework.market.frames import FrameObservation, MinuteFrame
from bomber.framework.trader import UnifiedStrategyRunner, RuntimeMode


def prepare_feature_frames(events, bundle, binding, records, index_key, wait_ns, max_age_ns=300_000_000_000):
    if not events:
        raise ValueError("回测交易腿文件没有可回放的Bar")
    grouped = defaultdict(list)
    for bar in events:
        if bar.ts_init < bar.ts_event:
            raise ValueError("回测完整Bar不能提前可见")
        grouped[bar.ts_event].append(bar)
    market, deadlines = [], {}
    last_available = max(bar.ts_init for bar in events)
    for event, bars in sorted(grouped.items()):
        received = min(bar.ts_init for bar in bars)
        deadline = received + wait_ns
        # 结束前需留有真实行情推进原生时钟；不生成虚构价格来完成撮合。
        if deadline >= last_available:
            continue
        observations = {str(bar.bar_type.instrument_id.symbol): FrameObservation(
            bar.close.as_decimal(), "option-bars", bar.ts_event, bar.ts_init)
            for bar in bars if bar.ts_init <= deadline}
        market.append(MarketFrameInput(str(event), event, received, observations))
        deadlines[event] = deadline
    requirement = FeatureRequirement("index", binding.spec.dataset_id,
        binding.spec.schema_version, index_key, max_source_age_ns=max_age_ns)
    outcomes = replay_feature_frames(DataHub(features=FeatureHub(bundle.registry)),
        (requirement,), market, records, wait_ns=wait_ns)
    if any(row.reason and row.reason.startswith("QUALITY:") for row in outcomes):
        raise FeatureError("指数特征最新可见版本质量不合格")
    return outcomes, deadlines


class OptionFeatureBacktestRunner(UnifiedStrategyRunner):
    """行情照常进入撮合；策略只消费与Live相同的封闭分钟帧。"""
    def __init__(self, *, positions, feed, backend, bundle, binding, records, index_key, wait_seconds,
                 max_market_age_seconds=300):
        super().__init__(RuntimeMode.HISTORICAL, position_manager=positions)
        self.feed, self.backend = feed, backend
        self.bundle, self.binding, self.records = bundle, binding, records
        self.index_key, self.wait_ns = index_key, int(wait_seconds * 1e9)
        self.max_age_ns = int(max_market_age_seconds * 1e9)
        self.strategy = None
        self.frame_audit = []
        self._scheduled = set()
        self.failure = None

    def run_replay(self):
        outcomes, self.deadlines = prepare_feature_frames(self.feed.load_events(), self.bundle,
            self.binding, self.records, self.index_key, self.wait_ns, self.max_age_ns)
        self.outcomes = {row.event_ns: row for row in outcomes}
        result = super().run_replay()
        if self.failure is not None:
            raise RuntimeError("指数特征分钟决策失败") from self.failure
        if len(self.frame_audit) != len(self.outcomes):
            raise RuntimeError("原生模拟时钟未完成特征帧封闭，拒绝输出成功回测")
        return result

    def publish(self, feed_id, event):
        # 保留公共价格观察者；不再向策略重复分发普通Bar。
        for observer in tuple(self._market_observers):
            observer.on_market_event(event)
        timestamp = event.ts_event
        if timestamp not in self.outcomes or timestamp in self._scheduled:
            return
        self._scheduled.add(timestamp)
        # 同一ts_init的文件Bar逐条推进引擎；晚1ns触发，确保截止边界
        # 的所有市场腿已进入撮合，不能在第一条Bar回调内读后续同刻Bar。
        self.backend.engine.kernel.clock.set_time_alert_ns(
            name=f"option-feature-{timestamp}", alert_time_ns=self.deadlines[timestamp]+1,
            callback=lambda timer, stamp=timestamp: self._close_frame(stamp, timer.ts_event))

    def _close_frame(self, timestamp, now_ns):
        outcome = self.outcomes[timestamp]
        audit = {"valuation_ns": timestamp, "request_ns": now_ns,
            "deadline_ns": self.deadlines[timestamp], "status": outcome.status, "reason": outcome.reason}
        self.frame_audit.append(audit)
        if outcome.status != "READY":
            return
        record = outcome.frame.features.latest("index")
        audit.update(revision=record.revision, available_ns=record.available_ns,
            received_ns=record.received_ns, feature_decision_ns=outcome.frame.decision_ns)
        observations = dict(outcome.frame.market)
        field = self.binding.bar_projection["fields"]["close"]
        observations[self.index_key] = FrameObservation(getattr(record.payload, field),
            "index-bars", record.source_ns, record.visible_ns)
        try:
            self.strategy.on_market_frame(MinuteFrame(timestamp, now_ns, observations))
        except Exception as error:
            self.failure = error
            audit["status"] = "FAILED"
            raise
