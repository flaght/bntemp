"""将已完成分钟Bar统一到inclusive-end；不补价格、不把Bar伪装成Tick。"""

import time
from bomber.framework.market.basic.base import DataType, MarketDataFeed, make_bar

MINUTE = 60_000_000_000


class CompletedMinuteBarFeed(MarketDataFeed):
    def __init__(self, source_id, upstream, *, config, clock_ns=None):
        super().__init__(source_id)
        self.upstream, self.config = upstream, config
        self._clock_ns = clock_ns or time.time_ns
        self.required_instruments = frozenset()
        self.observations = {}
        self.depth_records = {}
        self.failure = None
        self._attached = False

    @property
    def health_snapshot(self):
        return self.upstream.health_snapshot

    def register_health_handler(self, handler):
        self.upstream.register_health_handler(handler)

    def acknowledge_health_degradation(self):
        return self.upstream.acknowledge_health_degradation()

    def register_instrument(self, meta):
        if self.config.source == "bn-ws" and str(meta.instrument_id.venue) != "BINANCE":
            raise ValueError("BN-WS行情标的须为BINANCE，不能映射成国内合约")
        super().register_instrument(meta)
        self.upstream.register_instrument(meta)

    def subscribe(self, instrument_id, data_type=DataType.BAR, bar_spec=None, fields=(), **extra):
        if data_type != DataType.BAR or (bar_spec or "1-MINUTE") != "1-MINUTE":
            raise ValueError("当前策略输入须为已完成1-MINUTE Bar")
        super().subscribe(instrument_id, data_type, "1-MINUTE", fields, **extra)

    def connect(self):
        if self._is_connected:
            return
        if not self._attached:
            self.upstream.register_bar_handler(self._on_bar)
            self._attached = True
        for instrument in self._subscriptions:
            self.upstream.subscribe(instrument, DataType.BAR, "1-MINUTE")
        self.upstream.connect()
        self._is_connected = True

    def disconnect(self):
        self.upstream.disconnect()
        self.observations.clear()
        self._is_connected = False

    def _on_subscription_added(self, request):
        pass

    def _on_subscription_removed(self, request):
        pass

    def _on_bar(self, bar):
        instrument = bar.bar_type.instrument_id
        if instrument not in self._subscriptions:
            return
        if not str(bar.bar_type).endswith("-1-MINUTE-LAST-EXTERNAL"):
            self.failure = "分钟源返回了不匹配的Bar周期或价格类型"
            return
        # DDB start/end按声明转换，BN的T是毫秒级inclusive-end。
        label = self.config.timestamp_label
        stamp = bar.ts_event
        if self.config.source == "bn-ws":
            end = ((stamp // MINUTE) + 1) * MINUTE - 1
        else:
            if stamp % MINUTE:
                self.failure = "DolphinDB分钟时间不在声明的分钟边界"
                return
            end = stamp + (MINUTE if label == "start" else 0) - 1
        now = self._clock_ns()
        previous = self.observations.get(instrument)
        if end > now:
            self.failure = "分钟源返回了未完成或未来Bar"
            return
        if previous and end < previous[0]:
            self.failure = "分钟源逐合约时间回退"
            return
        if previous and end == previous[0]:
            return
        self.observations[instrument] = (end, now)
        normalized = make_bar(instrument_id=instrument, open=bar.open.as_decimal(),
            high=bar.high.as_decimal(), low=bar.low.as_decimal(), close=bar.close.as_decimal(),
            volume=bar.volume.as_decimal(), ts_event=end, ts_init=bar.ts_init,
            meta=self.get_instrument_meta(instrument), bar_type="1-MINUTE")
        self._emit_bar(normalized)

    def stale_instruments(self):
        now = self._clock_ns()
        failures = {}
        for instrument in self.required_instruments:
            event, received = self.observations.get(instrument, (None, None))
            if (self.failure or event is None or received is None or
                    not 0 <= now - event <= int(self.config.max_age_seconds * 1e9) or
                    not 0 <= now - received <= int(self.config.max_age_seconds * 1e9)):
                failures[str(instrument)] = {"event_ns": event, "received_ns": received,
                    "reason": self.failure or "分钟Bar缺失或失效"}
        return failures
