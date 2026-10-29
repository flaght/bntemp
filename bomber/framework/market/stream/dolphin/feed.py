"""基于公共 StreamDataFeed 契约的 DolphinDB 流表适配器。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from bomber.framework.market.basic.base import DataType, InstrumentId, InstrumentMeta, SubscriptionRequest
from bomber.framework.market.stream.base import StreamDataFeed
from bomber.framework.market.stream.health import StreamHealthConfig

from bomber.framework.market.native.dolphin import (
    DolphinDbDriver,
    NativeDolphinSubscription,
    create_native_driver,
)
from bomber.framework.market.stream.dolphin.config import DolphinDbConfig, DolphinDbStreamSpec, BAR_REQUIRED_COLUMNS, BAR_STATE_REQUIRED_COLUMNS
from bomber.framework.market.stream.dolphin.converter import ConvertedEvent, DolphinDbMarketConverter, DolphinDbMinuteStateBuffer


logger = logging.getLogger("DolphinDbLiveDataFeed")
DriverFactory = Callable[[], DolphinDbDriver]
_SUPPORTED_TYPES = frozenset(
    (DataType.QUOTE_TICK, DataType.TRADE_TICK, DataType.BAR, DataType.CUSTOM_BAR),
)


class DolphinDbLiveDataFeed(StreamDataFeed):
    def __init__(
        self,
        config: DolphinDbConfig,
        source_id: str = "DOLPHINDB_LIVE_SOURCE",
        queue_size: int = 100_000,
        driver_factory: DriverFactory = create_native_driver,
        health_config: StreamHealthConfig | None = None,
    ) -> None:
        super().__init__(
            source_id=source_id,
            queue_size=queue_size,
            health_config=health_config,
        )
        self.config = config
        self._driver_factory = driver_factory
        self._driver: DolphinDbDriver | None = None
        self._converter = DolphinDbMarketConverter()
        self._minute_states = DolphinDbMinuteStateBuffer()
        self._symbol_to_instrument: dict[str, InstrumentId] = {}
        self._ready = threading.Event()
        self._observation_lock = threading.RLock()
        self._latest_received_ns = self._latest_event_ns = None
        self._received_bars = self._conversion_errors = 0
        self._last_conversion_error = None
        self._resolved_columns = {}

    @property
    def latest_received_ns(self):
        with self._observation_lock:
            return self._latest_received_ns

    @property
    def latest_event_ns(self):
        with self._observation_lock:
            return self._latest_event_ns

    @property
    def conversion_failure(self):
        with self._observation_lock:
            return self._last_conversion_error

    @property
    def manifest(self):
        with self._observation_lock:
            return {"source_id": self.source_id, "received_bars": self._received_bars,
                "latest_received_ns": self._latest_received_ns, "latest_event_ns": self._latest_event_ns,
                "conversion_errors": self._conversion_errors, "last_conversion_error": self._last_conversion_error,
                "resolved_columns": dict(getattr(self._driver, "resolved_columns", self._resolved_columns))}

    def register_instrument(self, meta: InstrumentMeta) -> None:
        super().register_instrument(meta)
        symbol = str(meta.instrument_id).rsplit(".", 1)[0]
        self._symbol_to_instrument[symbol.casefold()] = meta.instrument_id

    def subscribe(
        self,
        instrument_id: InstrumentId | str,
        data_type: DataType = DataType.TRADE_TICK,
        bar_spec: str | None = None,
        fields: Sequence[str] = (),
        **extra: Any,
    ) -> None:
        resolved = InstrumentId.from_str(instrument_id) if isinstance(instrument_id, str) else instrument_id
        if data_type not in _SUPPORTED_TYPES:
            raise ValueError(f"DolphinDB market stream does not support {data_type}")
        if self.get_instrument_meta(resolved) is None:
            raise ValueError(f"instrument is not registered: {resolved}")
        if data_type in {DataType.BAR, DataType.CUSTOM_BAR} and not bar_spec:
            raise ValueError("bar_spec is required for DolphinDB Bar subscriptions")
        super().subscribe(resolved, data_type, bar_spec, fields, **extra)

    def connect(self) -> None:
        if not self._subscriptions:
            raise RuntimeError("register at least one DolphinDB market subscription before connect")
        super().connect()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def _start_network_client(self) -> None:
        with self._observation_lock:
            self._latest_received_ns = self._latest_event_ns = None
        driver = self._driver_factory()
        self._driver = driver
        try:
            driver.start(
                host=self.config.host,
                port=self.config.port,
                username=self.config.username,
                password=self.config.password,
                streaming_port=self.config.streaming_port,
                keep_alive_seconds=self.config.keep_alive_seconds,
            )
            for spec in self.config.streams:
                native = NativeDolphinSubscription(
                    table_name=spec.table_name,
                    action_name=spec.action_name,
                    columns=spec.columns,
                    offset=spec.offset,
                    resub=spec.resub,
                    batch_size=spec.batch_size,
                    throttle=spec.throttle,
                    filter=spec.filter,
                    required_columns=(BAR_STATE_REQUIRED_COLUMNS if spec.bar_mode == "state" else BAR_REQUIRED_COLUMNS)
                        if spec.auto_schema else (),
                )
                driver.subscribe(
                    native,
                    lambda row, stream=spec: self.on_stream_row(stream, row),
                )
        except Exception:
            try:
                driver.stop()
            except Exception:
                logger.exception("DolphinDB start cleanup failed")
            self._driver = None
            raise
        self._ready.set()

    def _stop_network_client(self) -> None:
        self._ready.clear()
        driver = self._driver
        if driver is not None:
            self._resolved_columns = dict(getattr(driver, "resolved_columns", {}))
            driver.stop()
        self._driver = None
        self._converter.reset()
        self._minute_states.reset()

    def _on_subscription_added(self, request: SubscriptionRequest) -> None:
        self._validate_request(request)

    def _on_subscription_removed(self, request: SubscriptionRequest) -> None:
        # DolphinDB 主题按表订阅，合约筛选在本地完成，
        # 因此修改单个逻辑订阅不会取消整个主题。
        return None

    def _validate_request(self, request: SubscriptionRequest) -> None:
        if request.data_type not in _SUPPORTED_TYPES:
            raise ValueError(f"unsupported DolphinDB data type: {request.data_type}")

    def on_stream_row(
        self,
        spec: DolphinDbStreamSpec,
        row: Mapping[str, Any],
    ) -> None:
        try:
            symbol = str(row.get("Code") or "").strip()
            if not symbol:
                raise ValueError("DolphinDB row has no Code")
            instrument_id = self._symbol_to_instrument.get(symbol.casefold())
            if instrument_id is None:
                logger.debug("ignore unregistered DolphinDB instrument: %s", symbol)
                return
            meta = self.get_instrument_meta(instrument_id)
            if meta is None:
                raise ValueError(f"missing InstrumentMeta: {instrument_id}")

            if spec.kind == "tick":
                converted = self._converter.convert_tick(row, instrument_id, meta)
            else:
                with self._observation_lock:
                    received = time.time_ns()
                    if spec.bar_mode == "state":
                        self._latest_received_ns = received
                        rows = self._minute_states.push((spec.table_name, instrument_id), row, received_ns=received)
                    else:
                        rows = (row,)
                    converted = []
                    for closed_row in rows:
                        converted.extend(self._converter.convert_bar(closed_row, instrument_id, meta, spec.bar_spec,
                            volume_mode=spec.volume_mode,
                            received_ns=received if spec.local_receipt_time else None,
                            timestamp_field="barTime" if spec.bar_mode == "state" else None))
            for item in converted:
                if self._is_subscribed(instrument_id, item):
                    with self._observation_lock:
                        self._latest_received_ns = time.time_ns()
                        self._latest_event_ns = max(self._latest_event_ns or 0, item.event.ts_event)
                        if item.data_type is DataType.BAR:
                            self._received_bars += 1
                    self.enqueue_event(item.event)
        except Exception as exc:
            with self._observation_lock:
                self._conversion_errors += 1
                self._last_conversion_error = str(exc)
            self.report_stream_interruption(f"DolphinDB分钟/逐笔转换失败: {spec.table_name}: {exc}")
            logger.exception(
                "DolphinDB market row conversion failed: table=%s kind=%s",
                spec.table_name,
                spec.kind,
            )

    def _is_subscribed(
        self,
        instrument_id: InstrumentId,
        item: ConvertedEvent,
    ) -> bool:
        for request in self._subscriptions.get(instrument_id, set()):
            if request.data_type is not item.data_type:
                continue
            if item.data_type in {DataType.BAR, DataType.CUSTOM_BAR}:
                return (request.bar_spec or "").upper() == (item.bar_spec or "").upper()
            return True
        return False
