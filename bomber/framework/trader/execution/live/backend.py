"""由可替换Nautilus实时Driver驱动的统一Live Backend。"""

from __future__ import annotations

import time
import json
import logging
from threading import RLock
from decimal import Decimal
from typing import Callable

from bomber.framework.market.basic.base import InstrumentId
from bomber.framework.trader.execution.contracts import (
    AccountPositionSnapshot,
    ExecutionBackendKind,
    ExecutionReport,
    OrderIntent,
)
from bomber.framework.trader.execution.live.driver import NautilusLiveDriverPort
from bomber.framework.trader.execution.events import AccountStateEvent, ActiveOrderSnapshot


class NautilusLiveExecutionBackend:
    """把Nautilus TradingNode生命周期隐藏在ExecutionBackendPort之后。"""

    kind = ExecutionBackendKind.LIVE

    def __init__(self, backend_id: str, driver: NautilusLiveDriverPort) -> None:
        if not backend_id.strip():
            raise ValueError("backend_id不能为空")
        if driver.driver_id != backend_id:
            raise ValueError("driver_id必须与backend_id一致")
        self.backend_id = backend_id
        self.driver = driver
        self._report_handlers: list[Callable[[ExecutionReport], None]] = []
        self._reports: list[ExecutionReport] = []
        self._started = False
        self._reconcile_revision = 0

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def reports(self) -> tuple[ExecutionReport, ...]:
        return tuple(self._reports)

    def start(self) -> None:
        if self._started:
            return
        self.driver.start(self._receive_report)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self.driver.stop()
        finally:
            self._started = False

    def submit_order(self, order: OrderIntent) -> None:
        if not self._started:
            raise RuntimeError("Live Backend尚未启动")
        if order.backend_id != self.backend_id:
            raise ValueError(
                f"订单Backend不匹配: {order.backend_id} != {self.backend_id}",
            )
        self.driver.submit_order(order)

    def check_order_capacity(self, quantity):
        check = getattr(self.driver, "check_order_capacity", None)
        if callable(check):
            check(quantity)

    def register_report_handler(
        self,
        handler: Callable[[ExecutionReport], None],
    ) -> None:
        if handler not in self._report_handlers:
            self._report_handlers.append(handler)

    def cancel_strategy(self, strategy_id: str) -> None:
        if not strategy_id.strip():
            raise ValueError("strategy_id不能为空")
        self.driver.cancel_strategy(strategy_id)

    def reconcile(self) -> AccountPositionSnapshot:
        if not self._started:
            raise RuntimeError("Live Backend尚未启动")
        raw_positions = self.driver.reconcile()
        positions: dict[InstrumentId, Decimal] = {}
        for instrument_id, quantity in raw_positions.items():
            normalized_id = (
                instrument_id
                if isinstance(instrument_id, InstrumentId)
                else InstrumentId.from_str(str(instrument_id))
            )
            positions[normalized_id] = (
                quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
            )
        self._reconcile_revision += 1
        return AccountPositionSnapshot(
            backend_id=self.backend_id,
            revision=self._reconcile_revision,
            ts_event=time.time_ns(),
            positions=positions,
        )

    def reconcile_account_state(self) -> AccountStateEvent:
        """可选权威资金能力；缺失时受控客户端保持闭闸。"""
        if not self._started:
            raise RuntimeError("Live Backend尚未启动")
        query = getattr(self.driver, "reconcile_account_state", None)
        if query is None:
            raise RuntimeError("Live Driver不支持权威账户资金查询")
        state = query()
        if not isinstance(state, AccountStateEvent) or state.client_id != self.backend_id:
            raise ValueError("权威账户资金快照类型或客户端不匹配")
        return state

    def reconcile_active_orders(self) -> ActiveOrderSnapshot:
        """可选柜台全量活动订单查询；缺失时不解除恢复闸门。"""
        if not self._started:
            raise RuntimeError("Live Backend尚未启动")
        query = getattr(self.driver, "reconcile_active_orders", None)
        if query is None:
            raise RuntimeError("Live Driver不支持权威活动订单查询")
        snapshot = query()
        if not isinstance(snapshot, ActiveOrderSnapshot) or snapshot.client_id != self.backend_id:
            raise ValueError("权威活动订单快照类型或客户端不匹配")
        return snapshot

    def _receive_report(self, report: ExecutionReport) -> None:
        if report.backend_id != self.backend_id:
            raise ValueError(
                f"执行回报Backend不匹配: {report.backend_id} != {self.backend_id}",
            )
        self._reports.append(report)
        for handler in tuple(self._report_handlers):
            handler(report)



audit_logger = logging.getLogger("ExecutionAudit")


class AuditedExecutionBackend:
    """只读报单诊断代理，委托原样转发一次，不改变定价或重试发送。"""

    def __init__(self, backend, prices, *, max_quote_age_seconds=30, clock_ns=None):
        self._backend, self._prices = backend, prices
        self._clock_ns = clock_ns or time.time_ns
        self._max_quote_age_ns = int(max_quote_age_seconds * 1e9)
        self._quotes, self._records, self._lock = {}, [], RLock()

    def __getattr__(self, name):
        return getattr(self._backend, name)

    def observe_quote(self, quote):
        row = {"bid": str(quote.bid_price.as_decimal()), "ask": str(quote.ask_price.as_decimal()),
            "bid_size": str(quote.bid_size.as_decimal()), "ask_size": str(quote.ask_size.as_decimal()),
            "source_ns": quote.ts_event, "received_ns": quote.ts_init}
        with self._lock:
            self._quotes[quote.instrument_id] = row

    @property
    def snapshot(self):
        with self._lock:
            return [{**row, "quote": None if row["quote"] is None else dict(row["quote"])} for row in self._records]

    def submit_order(self, order):
        now = self._clock_ns()
        reference = self._prices.get(order.instrument_id)
        with self._lock:
            quote = self._quotes.get(order.instrument_id)
            quote = None if quote is None else dict(quote)
            age = None if quote is None else now - quote["received_ns"]
            side = getattr(order.side, "value", order.side)
            crosses = None
            if quote is not None and order.price is not None and 0 <= age <= self._max_quote_age_ns:
                crosses = Decimal(str(order.price)) <= Decimal(quote["bid"]) if side == "SELL" else Decimal(str(order.price)) >= Decimal(quote["ask"])
            row = {"submitted_ns": now, "instrument": str(order.instrument_id), "side": side,
                "quantity": str(order.quantity), "limit_price": None if order.price is None else str(order.price),
                "reference_price": None if reference is None else str(reference.price),
                "reference_received_ns": None if reference is None else reference.ts_event,
                "quote": quote, "quote_age_ns": age, "crosses_observed_quote": crosses,
                "status": "SUBMIT_ATTEMPT", "client_order_id": None}
            self._records.append(row)
            del self._records[:-32]
        audit_logger.info("报单诊断: %s", json.dumps(row, ensure_ascii=False))
        try:
            result = self._backend.submit_order(order)
        except Exception as error:
            with self._lock:
                row.update(status="SUBMIT_RAISED", error_type=type(error).__name__)
            raise
        with self._lock:
            row.update(status="SUBMIT_RETURNED", client_order_id=result)
        return result
