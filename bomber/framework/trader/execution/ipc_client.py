"""策略进程执行端口：仅镜像自己的归属，不持有TD或物理账户写入锁。"""

from decimal import Decimal
import time

from bomber.framework.trader.portfolio import PositionManager
from .ipc import MAX_PRICE_AGE_NS, decode_event, wire
from .risk import MarketReferencePriceStore


class RemoteAccountService:
    def __init__(self, connection, configuration, state):
        self.connection, self.configuration = connection, configuration
        self.client_id, self.account_id = state["client_id"], state["account_id"]
        self.position_manager, self.prices = PositionManager(), MarketReferencePriceStore()
        self.state = state
        self.port = None

    def strategy_client(self, strategy_id, *, routes, budget, instrument_limits=None):
        if strategy_id != self.connection.sid or self.port is not None:
            raise ValueError("远程服务只能装配声明的单个策略")
        declaration = {"routes": {key: str(i) for key, i in routes.items()}, "budget": budget.state(),
            "limits": {str(i): {name: None if value is None else str(value)
                for name, value in vars(limits).items()} for i, limits in instrument_limits.items()}}
        if declaration != self.configuration:
            raise ValueError("策略路由或风控声明与账户服务不一致")
        self.port = RemoteStrategyExecutionClient(self, strategy_id, routes, declaration)
        return self.port


class RemoteStrategyExecutionClient:
    shared_account = True

    def __init__(self, service, sid, routes, configuration):
        self.service, self.strategy_id, self.routes = service, sid, dict(routes)
        self.client_id, self.account_id = service.client_id, service.account_id
        self.position_manager, self._configuration = service.position_manager, configuration
        self.ready_check = lambda: False
        self._started = self._stopping = False
        self._handlers, self._pending = [], []
        self.cursor = 0
        self.state = service.state
        self._observed = 0
        self._revision = 0
        self.completed = None
        self.received_events = []
        self.price_sync = {"expired_observations": 0, "last_expired": []}

    def configuration(self):
        return self._configuration

    @property
    def is_reconciled(self):
        return bool(self._started and self.state.get("reconciled") and time.monotonic() - self._observed <= 30)

    @property
    def report_errors(self):
        return () if self.is_reconciled else ("账户服务连接或执行状态未就绪",)

    @property
    def account_reconciliation(self):
        from .policy import AccountReconciliationMode
        return AccountReconciliationMode.OFF

    account_observation = None

    def sync(self, state):
        self.state, self._observed = state, time.monotonic()
        positions = {}
        for key, instrument in self.routes.items():
            q = Decimal(state["positions"][key])
            self.position_manager.set_strategy_position(self.strategy_id, key, q)
            positions[instrument] = positions.get(instrument, Decimal(0)) + q
            self.position_manager.attribution._owned[(self.client_id, str(instrument), self.strategy_id, key)] = q
            current = self.position_manager.working_quantity(self.client_id, instrument)
            self.position_manager.adjust_working_quantity(self.client_id, instrument,
                Decimal(state["working"][key]) - current)
        self._revision += 1
        self.position_manager.apply_account_snapshot(self.client_id, positions,
            revision=self._revision, ts_event=time.time_ns())

    def start(self):
        if not self._started:
            result = self.service.connection.call("attach", {"configuration": self.configuration()})
            self.sync(result["state"])
            self._started, self._stopping = True, False

    def register_execution_event_handler(self, handler):
        if handler not in self._handlers:
            self._handlers.append(handler)

    def set_cursor(self, cursor):
        self.cursor = cursor

    def deliver(self, rows, completed=None):
        for row in rows:
            if row["sequence"] != self.cursor + 1:
                raise RuntimeError("远程事件序列缺口")
            event = decode_event(row)
            if event.identity.strategy_id != self.strategy_id:
                raise ValueError("远程事件包含其他策略")
            for handler in tuple(self._handlers):
                handler(event)
            self.received_events.append(event)
            callback = completed or self.completed
            if callback is not None:
                callback(row["sequence"])
            self.cursor = row["sequence"]

    def drain_events(self, *, completed=None):
        if not self._started:
            return  # begin_stop已排空并确认回报；不能再轮询已关闭的租约。
        result = self.service.connection.call("poll", {"cursor": self.cursor,
            "ready": bool(not self._stopping and self.ready_check()), "prices": self.price_state(fresh_only=True)})
        if result.get("expired_prices"):
            self.price_sync["expired_observations"] += len(result["expired_prices"])
            self.price_sync["last_expired"] = result["expired_prices"]
        self.sync(result["state"])
        self.deliver(result["events"], completed)
        if result["events"]:
            self.service.connection.call("poll", {"cursor": self.cursor, "ready": bool(not self._stopping and self.ready_check())})
        stopped = self.state.get("execution_stop")
        if stopped:
            from .contracts import MarketDataUnavailable, OrderBudgetExhausted
            if stopped["reason"] == "order_budget_exhausted":
                raise OrderBudgetExhausted(stopped["limit"], stopped["submitted"], stopped["requested"])
            if stopped["reason"] == "market_data_unavailable":
                raise MarketDataUnavailable(stopped["readiness"])
            raise RuntimeError(f"未知账户停止原因: {stopped['reason']}")

    def price_state(self, *, fresh_only=False):
        prices = self.service.prices.snapshot()
        now = time.time_ns()
        return {str(i): {"price": str(p.price), "ts_event": p.ts_event}
                for i, p in prices.items() if i in self.routes.values()
                and (not fresh_only or 0 <= now - p.ts_event <= MAX_PRICE_AGE_NS)}

    def submit_targets(self, request):
        if self._stopping or request.strategy_id != self.strategy_id:
            raise RuntimeError("策略身份不符或已经停止")
        result = self.service.connection.call("submit", {"request": wire(request), "prices": self.price_state(),
            "ready": bool(self.ready_check())})
        self.sync(result["state"])

    def continue_target(self, strategy_id, target_key, now_ns, *, instrument_id):
        if self._stopping:
            return
        if strategy_id != self.strategy_id or self.routes.get(target_key) != instrument_id:
            raise ValueError("远程执行路由不属于本策略")
        result = self.service.connection.call("continue", {"target_key": target_key, "now_ns": now_ns,
            "instrument_id": str(instrument_id), "prices": self.price_state(), "ready": bool(self.ready_check())})
        self.sync(result["state"])

    def cancel_strategy(self, strategy_id):
        if strategy_id != self.strategy_id:
            raise ValueError("不能撤销其他策略")
        self.service.connection.call("cancel", {})

    def disarm(self, reason="operator"):
        self.begin_stop()

    def begin_stop(self):
        self._stopping = True
        if self._started:
            result = self.service.connection.call("stop", {"cursor": self.cursor})
            self.sync(result["state"])
            self.deliver(result["events"])
            if len(result["events"]) == 100:
                raise RuntimeError("停机回报批次尚未排空，保留策略检查点")
            self.service.connection.call("ack_stopped", {"cursor": self.cursor})
            self._started = False

    def stop(self):
        if self._started:
            self.begin_stop()


class RemoteTraderSession:
    def __init__(self, service, trading_day):
        self.service, self.trading_day, self.driver_id = service, trading_day, service.client_id

    @property
    def _connected(self):
        return bool(self.service.port and self.service.port.is_reconciled)

    @property
    def is_simnow_session(self):
        return self._connected
