"""完整期货分钟：CTP 深度增量或 DolphinDB 已完成分钟，共用 CustomBar 契约。

网络回调只入队；调用方在运行时控制线程 poll()，避免模型阻塞原始行情线程。
成交额必须来自来源真实成交额，绝不以末价乘成交量补造。
"""
from datetime import timedelta
from decimal import Decimal
from queue import Queue, Empty, Full
import threading
import time

from bomber.framework.market.basic.base import (
    DataType, MarketDataFeed, make_bar, make_custom_bar,
)
from bomber.framework.calendars.sessions import ns_datetime, datetime_ns

MINUTE = 60_000_000_000


def number(value, name, *, positive=False):
    try:
        value = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"期货分钟缺少有效 {name}") from exc
    if not value.is_finite() or value < 0 or (positive and not value) or value > Decimal('1e100'):
        raise ValueError(f"期货分钟 {name} 无效")
    return value


class FuturesMinuteBarFeed(MarketDataFeed):
    """显式启用的完整期货分钟；不改变现有 OHLCV 聚合器默认行为。

CTP 按 TradingDay 内累计 Volume/Turnover 差分；首个部分分钟丢弃。
DDB volume_mode 同时声明 volume 与成交额的累计/本周期口径。
model_timestamp 仅决定模型的原始分钟标签，事件统一为分钟结束减1ns。
"""
    def __init__(self, source_id, upstream, *, config, calendar,
                 model_timestamp='start', clock_ns=None, close_delay_ns=1_000_000_000, archive=None):
        super().__init__(source_id)
        if config.source not in {'ctp', 'dolphindb'} or model_timestamp not in {'start', 'end'}:
            raise ValueError('完整期货分钟仅支持 CTP/DolphinDB 和 start/end 模型标签')
        self.upstream, self.config, self.calendar = upstream, config, calendar
        self.model_timestamp = model_timestamp
        self.archive = archive
        self._clock_ns = clock_ns or time.time_ns
        self.close_delay_ns = close_delay_ns
        self._queue = Queue(maxsize=100000)
        self._lock = threading.RLock()
        self._states, self._previous, self._last_emitted = {}, {}, {}
        self.observations = {}
        self.required_instruments = frozenset()
        self.failure = None
        self._attached = False
        self._first_start = 0

    @property
    def health_snapshot(self):
        return self.upstream.health_snapshot

    def register_health_handler(self, handler):
        self.upstream.register_health_handler(handler)

    def acknowledge_health_degradation(self):
        return self.upstream.acknowledge_health_degradation()

    def register_instrument(self, meta):
        super().register_instrument(meta)
        self.upstream.register_instrument(meta)

    def subscribe(self, instrument_id, data_type=DataType.CUSTOM_BAR, bar_spec=None, fields=(), **extra):
        if data_type not in {DataType.BAR, DataType.CUSTOM_BAR} or (bar_spec or '1-MINUTE') != '1-MINUTE':
            raise ValueError('完整期货行情只提供1-MINUTE BAR/CUSTOM_BAR')
        super().subscribe(instrument_id, data_type, '1-MINUTE', fields, **extra)

    def connect(self):
        if self._is_connected:
            return
        self._first_start = (self._clock_ns() // MINUTE + 1) * MINUTE
        if not self._attached:
            if self.config.source == 'ctp':
                self.upstream.register_depth_handler(self._enqueue_depth)
            else:
                self.upstream.register_custom_bar_handler(self._enqueue_bar)
            self._attached = True
        for instrument in self._subscriptions:
            dtype = DataType.TRADE_TICK if self.config.source == 'ctp' else DataType.CUSTOM_BAR
            self.upstream.subscribe(instrument, dtype, None if dtype is DataType.TRADE_TICK else '1-MINUTE')
        if self.archive is not None:
            if self.config.source != 'ctp':
                raise ValueError('CTP分钟存档不能录制其他来源')
            self.archive.open()
        try:
            self.upstream.connect()
        except Exception:
            if self.archive is not None:
                self.archive.close()
            raise
        self._is_connected = True

    def disconnect(self):
        try:
            self.upstream.disconnect()
        finally:
            self._is_connected = False
            if self.archive is not None:
                self.archive.close()
            self._states.clear()
            self._previous.clear()
            while True:
                try:
                    self._queue.get_nowait()
                except Empty:
                    break

    def _on_subscription_added(self, request):
        raise RuntimeError('完整期货分钟须在连接前完成订阅')

    def _on_subscription_removed(self, request):
        pass

    def _enqueue(self, item):
        try:
            self._queue.put_nowait(item)
        except Full:
            self.failure = '完整期货分钟队列溢出，不能继续使用缺失行情'

    def _enqueue_depth(self, instrument, ts_event, row):
        self._enqueue(('ctp', instrument, ts_event, dict(row)))

    def _enqueue_bar(self, event):
        end = event.ts_event + (MINUTE if self.config.timestamp_label == 'start' else 0) - 1
        self.observations[event.bar.bar_type.instrument_id] = (end, self._clock_ns())
        self._enqueue(('dolphindb', event))

    def poll(self, now_ns=None):
        now_ns = self._clock_ns() if now_ns is None else now_ns
        with self._lock:
            if self.failure:
                raise RuntimeError(self.failure)
            try:
                for _ in range(self._queue.qsize()):
                    try:
                        item = self._queue.get_nowait()
                    except Empty:
                        break
                    if item[0] == 'ctp':
                        self._depth(*item[1:])
                    else:
                        self._dolphin(item[1], now_ns)
                # 给收盘边界快照一个有界到达窗口；不等待下一笔成交才封闭最后一分钟。
                for instrument, state in tuple(self._states.items()):
                    if state['start'] + MINUTE + self.close_delay_ns <= now_ns:
                        self._finish(instrument)
            except Exception as error:
                self.failure = str(error)
                raise

    def _depth(self, instrument, stamp, row):
        if instrument not in self._subscriptions:
            return
        day = str(row.get('TradingDay', ''))
        volume = number(row.get('Volume'), 'Volume')
        amount = number(row.get('Turnover'), 'Turnover')
        if volume != volume.to_integral_value():
            raise ValueError('CTP累计成交量须为整数')
        previous = self._previous.get(instrument)
        self._previous[instrument] = (day, stamp, volume, amount)
        if previous is None or previous[0] != day:
            self._finish(instrument)
            return
        if stamp < previous[1] or volume < previous[2] or amount < previous[3]:
            raise ValueError('CTP同交易日累计量额或时间倒退')
        dv, da = volume - previous[2], amount - previous[3]
        if (dv == 0) != (da == 0):
            raise ValueError('CTP成交量与成交额增量不一致')
        point = ns_datetime(stamp, self.calendar.zone)
        window = self.calendar.window(point)
        if window is None:
            prior = self.calendar.window(point - timedelta(microseconds=1))
            if prior is None or datetime_ns(prior.end) != stamp:
                return
            # 精确结束边界的最后快照属于上一分钟。
            stamp -= 1
        start = stamp // MINUTE * MINUTE
        if (stamp - previous[1] > MINUTE
                and self.calendar.window(ns_datetime(previous[1], self.calendar.zone)) == window):
            raise ValueError('CTP时段内深度缺口超过一分钟，不能把缺失成交归入当前分钟')
        if start < self._first_start:
            return
        interest = number(row.get('OpenInterest'), 'OpenInterest')
        price = number(row.get('LastPrice'), 'LastPrice', positive=True)
        if start + MINUTE - 1 <= self._last_emitted.get(instrument, -1):
            if dv:
                raise ValueError('CTP已封闭分钟收到迟到成交增量')
            return
        state = self._states.get(instrument)
        if state is not None and state['start'] != start:
            self._finish(instrument)
            state = None
        if state is None:
            if not dv:
                return
            state = dict(start=start, open=price, high=price, low=price, close=price,
                         volume=Decimal(0), value=Decimal(0), open_interest=interest)
            self._states[instrument] = state
        if dv:
            state.update(high=max(state['high'], price), low=min(state['low'], price), close=price)
            state['volume'] += dv
            state['value'] += da
        state['open_interest'] = interest

    def _finish(self, instrument):
        state = self._states.pop(instrument, None)
        if state is not None and state['volume'] > 0:
            self._emit_values(instrument, state['start'] + MINUTE - 1, state)

    def _dolphin(self, event, now):
        bar, fields = event.bar, dict(event.factors)
        instrument = bar.bar_type.instrument_id
        if instrument not in self._subscriptions:
            return
        end = event.ts_event + (MINUTE if self.config.timestamp_label == 'start' else 0) - 1
        if event.ts_event % MINUTE or end > now:
            raise ValueError('DolphinDB分钟时间未对齐或尚未完成')
        point = ns_datetime(end, self.calendar.zone)
        if self.calendar.window(point) is None:
            # 盘外尾部累计快照不能在下一交易时段被重复归入首根分钟。
            previous = self._previous.get(instrument)
            if self.config.volume_mode == 'cumulative' and previous is not None:
                amount = number(fields.get('value', fields.get('turnover')), 'value/turnover')
                volume = number(fields.get('source_volume'), 'source_volume')
                if amount < previous[1] or volume < previous[2]:
                    raise ValueError('DolphinDB盘外累计量额倒退')
                self._previous[instrument] = (previous[0], amount, volume)
            return
        if end <= self._last_emitted.get(instrument, -1):
            if end < self._last_emitted[instrument]:
                raise ValueError('DolphinDB分钟时间倒退')
            return
        values = {key: number(fields[key] if key in fields else getattr(bar, key).as_decimal(), key)
                  for key in ('open', 'high', 'low', 'close', 'volume')}
        amount = number(fields.get('value', fields.get('turnover')), 'value/turnover')
        if self.config.volume_mode == 'cumulative':
            day = self.calendar.window(point).trading_day
            cumulative_volume = number(fields.get('source_volume'), 'source_volume')
            previous = self._previous.get(instrument)
            self._previous[instrument] = (day, amount, cumulative_volume)
            if previous is None or previous[0] != day:
                return
            if amount < previous[1] or cumulative_volume < previous[2]:
                raise ValueError('DolphinDB同交易日累计量额倒退')
            amount -= previous[1]
            values['volume'] = cumulative_volume - previous[2]
        values['value'] = amount
        values['open_interest'] = number(fields.get('open_interest', fields.get('openint')), 'open_interest')
        if (values['volume'] == 0) != (amount == 0):
            raise ValueError('DolphinDB本周期成交量与成交额不一致')
        if values['volume'] > 0:
            self._emit_values(instrument, end, values)

    def _emit_values(self, instrument, end, values):
        from bomber.framework.market.futures import futures_bar_values
        meta = self.get_instrument_meta(instrument)
        if meta is None or values['volume'] != values['volume'].to_integral_value():
            raise ValueError('期货分钟须有合约资料且成交量为整数')
        fields = {name: float(values[name]) for name in
                  ('open', 'high', 'low', 'close', 'volume', 'value', 'open_interest')}
        label = end + 1 - (MINUTE if self.model_timestamp == 'start' else 0)
        fields['trade_time'] = ns_datetime(label, self.calendar.zone)
        bar = make_bar(instrument_id=instrument, ts_event=end, ts_init=self._clock_ns(), meta=meta,
                       **{k: fields[k] for k in ('open', 'high', 'low', 'close', 'volume')})
        event = make_custom_bar(bar, fields)
        futures_bar_values(event, contract_multiplier=meta.multiplier)
        if self.archive is not None:
            from bomber.framework.dataprep.history import HistoryBar
            # 先落盘，再发布；即使模型/策略回调失败，完整行情也已经保存。
            self.archive.append(HistoryBar(str(instrument), end,
                **{key: values[key] for key in
                   ('open', 'high', 'low', 'close', 'volume', 'value', 'open_interest')}))
        self._last_emitted[instrument] = end
        self.observations[instrument] = (end, self._clock_ns())
        self._emit_bar(bar)
        self._emit_custom_bar(event)

    def stale_instruments(self):
        now = self._clock_ns()
        return {str(instrument): {'event_ns': stamp, 'received_ns': received, 'reason': self.failure or '分钟行情缺失或失效'}
                for instrument in self.required_instruments
                for stamp, received in (self.observations.get(instrument, (None, None)),)
                if self.failure or stamp is None or not 0 <= now - stamp <= self.config.max_age_seconds * 1e9
                or received is None or not 0 <= now - received <= self.config.max_age_seconds * 1e9}
