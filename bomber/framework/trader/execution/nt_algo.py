"""公共NT算法宿主：管理明确订单的执行；实际订单仍由原执行端风控和记账。"""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
import logging
from math import isfinite
from threading import RLock
import time
from uuid import uuid4

from bomber.framework.market.basic.base import InstrumentId
from .contracts import OrderIntent, OrderType, ExecutionReportType

TASK_KEY = "nt_algo_task_id"
log = logging.getLogger("NTAlgoExecution")


@dataclass(frozen=True)
class NTAlgoConfig:
    wait_seconds: float = 10
    max_attempts: int = 3

    def __post_init__(self):
        if (isinstance(self.wait_seconds, bool) or not isinstance(self.wait_seconds, (int, float))
                or not isfinite(self.wait_seconds) or self.wait_seconds <= 0):
            raise ValueError("NT算法等待秒数须为有限正数")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 1000:
            raise ValueError("NT算法max_attempts须为1至1000整数，包含首次报单")


def encode_order(order):
    return dict(strategy_id=order.strategy_id, backend_id=order.backend_id,
        instrument_id=str(order.instrument_id), side=order.side.value, quantity=str(order.quantity),
        order_type=order.order_type.value, price=None if order.price is None else str(order.price),
        position_effect=order.position_effect.value, reduce_only=order.reduce_only,
        metadata=deepcopy(dict(order.metadata)))


def decode_order(row):
    return OrderIntent(**{**row, "instrument_id": InstrumentId.from_str(row["instrument_id"])})


class NativeNTHost:
    """真正使用NT ExecAlgorithm、LiveClock和spawn_limit；不另建第二份账户账本。

    算法的实际委托经adapter进入原风控/冻结/持久化入口，不启动TradingNode，
    也不伪造NT账户资金。原生SDK只在选用算法执行时加载。
    """
    def __init__(self):
        try:
            from bomber.execution.algorithm import ExecAlgorithm
            from bomber.cache.cache import Cache
            from bomber.common.component import LiveClock, MessageBus
            from bomber.portfolio.portfolio import Portfolio
            from bomber.model.identifiers import TraderId
        except ImportError as error:
            raise RuntimeError("NT算法执行需要远程运行环境的完整Bomber NT编译SDK") from error
        host = self

        class LimitRepriceAlgorithm(ExecAlgorithm):
            def on_order(self, order):
                host.primaries[str(order.client_order_id)] = order

        self.primaries = {}
        self.clock = LiveClock()
        self.trader_id = TraderId("ALGO-001")
        self.cache = Cache()
        self.msgbus = MessageBus(trader_id=self.trader_id, clock=self.clock)
        self.portfolio = Portfolio(msgbus=self.msgbus, cache=self.cache, clock=self.clock)
        self.algorithm = LimitRepriceAlgorithm()
        self.algorithm.register(self.trader_id, self.portfolio, self.msgbus, self.cache, self.clock)
        self.algorithm.start()

    def primary(self, task_id, order, attempts=0):
        from bomber.core.uuid import UUID4
        from bomber.execution.messages import SubmitOrder
        from bomber.model.identifiers import ClientOrderId, StrategyId, InstrumentId as NativeInstrumentId
        from bomber.model.enums import OrderSide
        from bomber.model.objects import Quantity, Price
        from bomber.model.orders import LimitOrder

        # NT要求策略ID含连字符；统一加前缀，保持一一对应，不改原策略归属ID。
        primary = LimitOrder(trader_id=self.trader_id, strategy_id=StrategyId(f"NTAlgo-{order.strategy_id}"),
            instrument_id=NativeInstrumentId.from_str(str(order.instrument_id)),
            client_order_id=ClientOrderId(task_id), order_side=OrderSide[order.side.value],
            quantity=Quantity.from_str(format(order.quantity, "f")),
            price=Price.from_str(format(order.price, "f")), init_id=UUID4(),
            ts_init=self.clock.timestamp_ns(), reduce_only=order.reduce_only,
            exec_algorithm_id=self.algorithm.id)
        self.cache.add_order(primary)
        self.algorithm.execute(SubmitOrder(trader_id=self.trader_id, strategy_id=primary.strategy_id,
            order=primary, command_id=UUID4(), ts_init=self.clock.timestamp_ns()))
        # 恢复时通过公开派生接口推进序号，不复用已经落盘的派生单标识。
        for _ in range(attempts):
            self.child(task_id, order)

    def child(self, task_id, order):
        from bomber.model.objects import Quantity, Price
        child = self.algorithm.spawn_limit(self.primaries[task_id],
            Quantity.from_str(format(order.quantity, "f")), Price.from_str(format(order.price, "f")),
            reduce_only=order.reduce_only, reduce_primary=False)
        return str(child.client_order_id)

    def alarm(self, task_id, when_ns, callback):
        self.cancel_alarm(task_id)
        self.clock.set_time_alert_ns(task_id, when_ns, callback=lambda event: callback(), allow_past=True)

    def cancel_alarm(self, task_id):
        if task_id in self.clock.timer_names:
            self.clock.cancel_timer(task_id)

    def close(self):
        self.clock.cancel_timers()
        if self.algorithm.is_running:
            self.algorithm.stop()


class NTAlgoRuntime:
    """单执行上下文的限价撤单重报任务；无CTP/BN/LXGBN特有字段。

    NT时钟只产生待执行命令。adapter在原控制线程排空命令，以保留宿主线程
    约束及原提交锁顺序；回报回调中不直接补单。
    """
    def __init__(self, config=None, *, host_factory=NativeNTHost, clock_ns=time.time_ns):
        self.config = config or NTAlgoConfig()
        self._host_factory, self.clock_ns = host_factory, clock_ns
        self._host = self.adapter = None
        self._tasks, self._commands, self._stops = {}, deque(), deque()
        self._lock = RLock()

    def bind(self, adapter):
        if self.adapter is not None:
            raise RuntimeError("NT算法适配器只能绑定一次")
        self.adapter = adapter
        self.start()

    def start(self):
        with self._lock:
            if self._host is not None:
                return
            self._commands.clear()
            self._host = self._host_factory()
            for key, task in self._tasks.items():
                self._host.primary(key, decode_order(task["order"]), task["attempts"])
                self._resume(key, task)

    def close(self):
        with self._lock:
            if self._host is not None:
                self._host.close()
                self._host = None

    def submit(self, order):
        if order.order_type is not OrderType.LIMIT:
            raise ValueError("NT首期限价追单只接受显式限价订单")
        with self._lock:
            self.start()
            task_id = "NTALG-" + uuid4().hex
            self._tasks[task_id] = dict(order=encode_order(order), filled="0", attempts=0,
                active=None, submitted_ns=None, cancel_requested=False, stopped=False, seen=[])
            try:
                self._host.primary(task_id, order)
            except Exception:
                self._tasks.pop(task_id)
                raise
        try:
            self._send(task_id, order, initial=True)
        except Exception:
            with self._lock:
                task = self._tasks.get(task_id)
                if task is not None and task['active'] is None:
                    self._finish(task_id, 'submission_failed')
            raise

    def _send(self, task_id, order, *, initial=False):
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["stopped"] or task["active"] is not None:
                raise RuntimeError("NT任务已停止或仍有活动委托，拒绝重报")
            child_id = self._host.child(task_id, order)
            task["attempts"] += 1
            task["cancel_requested"] = False
            metadata = {**order.metadata, TASK_KEY: task_id, "nt_algo_child_id": child_id,
                        "nt_algo_attempt": task["attempts"]}
            if not initial:
                metadata.pop("allocation_token", None)
                metadata.pop("allocation_legs", None)
        # 原执行端可能等待资金查询并释放提交锁，不能持算法锁调用它。
        try:
            self.adapter.send_order(replace(order, metadata=metadata), initial=initial)
        except Exception:
            with self._lock:
                if task_id in self._tasks:
                    if task["active"] is None:
                        self._finish(task_id, "submission_failed")
                    else:
                        task["stopped"] = True
            raise

    def bind_order(self, identity, order):
        with self._lock:
            task_id = order.metadata[TASK_KEY]
            task = self._tasks[task_id]
            if task["stopped"] or task["active"] is not None:
                raise RuntimeError("NT任务已停止或仍存在活动委托")
            task["active"], task["submitted_ns"] = identity, self.clock_ns()
            self._schedule(task_id, task)

    def unsent(self, identity):
        with self._lock:
            for key, task in tuple(self._tasks.items()):
                if task["active"] == identity:
                    self._finish(key, "unsent")

    def _schedule(self, key, task):
        when = task["submitted_ns"] + int(self.config.wait_seconds * 1e9)
        deadline = task["order"]["metadata"].get("actual_send_deadline_ns")
        if deadline is not None:
            when = min(when, deadline)
        self._host.alarm(key, when, lambda: self._timeout(key))

    def _timeout(self, key):
        with self._lock:
            task = self._tasks.get(key)
            if task is not None and task["active"] and not task["cancel_requested"]:
                task["cancel_requested"] = True
                self._commands.append(("cancel", key, task["active"]))

    def on_report(self, report):
        with self._lock:
            key = report.metadata.get(TASK_KEY)
            task = self._tasks.get(key)
            if task is None:
                return
            report_id = report.report_id
            if not report_id:
                raise ValueError("NT任务回报须提供稳定report_id")
            if report_id in task["seen"]:
                return
            if report.client_order_id != task["active"]:
                raise RuntimeError("NT任务回报与当前实际委托不一致")
            task["seen"].append(report_id)
            if report.report_type in (ExecutionReportType.PARTIALLY_FILLED, ExecutionReportType.FILLED):
                filled = Decimal(task["filled"]) + report.filled_quantity
                if filled > Decimal(task["order"]["quantity"]):
                    raise RuntimeError("NT任务累计成交超过原订单数量")
                task["filled"] = str(filled)
            if report.report_type in (ExecutionReportType.FILLED, ExecutionReportType.CANCELED,
                                      ExecutionReportType.REJECTED):
                task["active"] = None
                self._host.cancel_alarm(key) if self._host is not None else None
                if (task["stopped"] or report.report_type is not ExecutionReportType.CANCELED
                        or task["attempts"] >= self.config.max_attempts
                        or Decimal(task["filled"]) == Decimal(task["order"]["quantity"])):
                    reason = ("strategy_cancel" if task["stopped"] else
                              "attempt_limit" if report.report_type is ExecutionReportType.CANCELED
                              and task["attempts"] >= self.config.max_attempts else report.report_type.value)
                    self._finish(key, reason)
                elif task["cancel_requested"]:
                    self._commands.append(("submit", key, None))
                else:
                    self._finish(key, "external_cancel")

    def cancel_strategy(self, strategy_id, *, opening_only=False, identities=None):
        with self._lock:
            for key, task in tuple(self._tasks.items()):
                order = task["order"]
                if (order["strategy_id"] != strategy_id or
                        opening_only and order["position_effect"] != "OPEN" or
                        identities is not None and task["active"] not in identities):
                    continue
                task["stopped"] = True
                if task["active"] is None:
                    self._finish(key, "strategy_cancel")

    def drain(self):
        self._drain_stops()
        with self._lock:
            count = len(self._commands)
        for _ in range(count):
            with self._lock:
                if not self._commands:
                    break
                action, key, identity = self._commands.popleft()
                task = self._tasks.get(key)
                if task is None:
                    continue
                order = decode_order(task["order"])
                if action == "cancel":
                    valid = task["active"] == identity
                else:
                    valid = not task["stopped"] and task["active"] is None
                if not valid:
                    continue
                remaining = order.quantity - Decimal(task["filled"])
            if action == "cancel":
                self.adapter.save()
                self.adapter.cancel_order(identity, order)
                continue
            deadline = order.metadata.get("actual_send_deadline_ns")
            if deadline is not None and self.clock_ns() > deadline:
                with self._lock:
                    if key in self._tasks:
                        self._finish(key, "deadline")
                self.adapter.save()
                continue
            candidate = self.adapter.reprice(replace(order, quantity=remaining))
            if candidate is None or not self.adapter.can_execute(order):
                with self._lock:
                    if key in self._tasks:
                        self._commands.append((action, key, identity))
                continue
            self._send(key, candidate)
        self._drain_stops()

    def _drain_stops(self):
        while True:
            with self._lock:
                if not self._stops:
                    return
                row = self._stops.popleft()
            self.adapter.execution_stopped(decode_order(row["order"]), row["reason"])

    def _finish(self, key, reason):
        task = self._tasks.pop(key)
        if not task["stopped"] and Decimal(task["filled"]) < Decimal(task["order"]["quantity"]):
            self._stops.append(dict(order=task["order"], reason=reason))
        if self._host is not None:
            self._host.cancel_alarm(key)
        log.info("NT算法执行结束: task=%s reason=%s filled=%s quantity=%s attempts=%s",
                 key, reason, task["filled"], task["order"]["quantity"], task["attempts"])

    def snapshot_state(self):
        with self._lock:
            return deepcopy(dict(version=1, config=dict(wait_seconds=self.config.wait_seconds,
                max_attempts=self.config.max_attempts), tasks=self._tasks, stops=list(self._stops)))

    def active_instruments(self):
        with self._lock:
            return ({task["order"]["instrument_id"] for task in self._tasks.values()
                     if not task["stopped"] or task["active"] is not None}
                    | {row["order"]["instrument_id"] for row in self._stops})

    def restore_state(self, state):
        with self._lock:
            if (state.get("version") != 1 or state.get("config") != self.snapshot_state()["config"]):
                raise ValueError("NT算法检查点版本或执行参数不一致")
            tasks = deepcopy(state["tasks"])
            for task in tasks.values():
                order = decode_order(task["order"])
                filled = Decimal(task["filled"])
                if not 0 <= filled <= order.quantity or not 1 <= task["attempts"] <= self.config.max_attempts:
                    raise ValueError("NT任务成交数量或报单次数无效")
            self.close()
            self._tasks, self._commands = tasks, deque()
            self._stops = deque(deepcopy(state.get("stops", [])))
            self.start()

    def _resume(self, key, task):
        if task["active"]:
            task["cancel_requested"] = False  # 活动订单经柜台核验后，原期限可再次触发撤单。
            self._schedule(key, task)
        elif not task["stopped"]:
            self._commands.append(("submit", key, None))
