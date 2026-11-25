"""独立归属CTP积木加公共NT算法；每次实际委托沿用原账本及恢复路径。"""
from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_FLOOR

from ..contracts import OrderSide
from ..nt_algo import NTAlgoRuntime, TASK_KEY
from .independent import IndependentCtpDriver
from .native_driver import _serialized_transition


class NTAlgoCtpDriver(IndependentCtpDriver):
    def __init__(self, *args, algo_runtime=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.algo_runtime = algo_runtime or NTAlgoRuntime()
        self._algo_service = None

    def bind_algorithm_service(self, service):
        if service.execution_mode != "independent":
            raise ValueError("NTAlgoCtpDriver首期须使用independent独立归属执行")
        self._algo_service = service
        self.algo_runtime.bind(self)

    def start(self, sink):
        def receive(report):
            report = self.algo_runtime.annotate_report(report)
            sink(report)  # 先由原订单机/归属/会计入账，再推进算法任务。
            self.algo_runtime.on_report(report)
        super().start(receive)
        if self._algo_service is not None:
            self.algo_runtime.start()

    def stop(self):
        self.algo_runtime.close()
        super().stop()

    def bind_durability(self, before_send, after_transition, on_unsent=None, **kwargs):
        def before(identity, order):
            if TASK_KEY in order.metadata:
                self.algo_runtime.bind_order(identity, order)
            before_send(identity, order)

        def unsent(identity):
            self.algo_runtime.unsent(identity)
            if on_unsent is not None:
                on_unsent(identity)
        super().bind_durability(before, after_transition, unsent, **kwargs)

    @_serialized_transition
    def submit_order(self, order):
        if self._algo_service is None or self._before_send is None:
            raise RuntimeError("NT算法Driver须绑定原执行服务及持久化后才能报单")
        if TASK_KEY in order.metadata:
            super().submit_order(order)
        else:
            self.algo_runtime.submit(order)

    # 公共算法模块通过这些方法调用原执行端。
    def send_order(self, order, *, initial=False):
        if initial:
            super().submit_order(order)
        else:
            self._algo_service.client.submit_algorithm_order(order)

    def cancel_order(self, identity, order):
        # 算法撤销单次尝试，不取消整个任务。外部撤单走下面的cancel_strategy。
        IndependentCtpDriver.cancel_strategy(self, order.strategy_id, client_order_ids=(identity,))

    def can_execute(self, order):
        service = self._algo_service
        if (service is None or service._failed or service.execution_stop is not None
                or service.client.report_errors or not service.client.is_reconciled
                or not service.client.is_armed or order.strategy_id in service._suspended):
            return False
        source = service._sources.get(order.strategy_id)
        port = service._ports.get(order.strategy_id)
        # 一个订单的任务只依赖本合约目标；同组合其他腿变动不撤销/阻塞它。
        rows = tuple(row for row in order.metadata.get("position_attribution", ())
                     if row["instrument_id"] == str(order.instrument_id))
        original = {(r["target_key"], r["instrument_id"]): r["quantity"] for r in rows}
        current = {(r["target_key"], r["instrument_id"]): r["quantity"] for r in source["rows"]
                   if (r["target_key"], r["instrument_id"]) in original} if source else {}
        if not (source and port and port._started and port.ready_check()
                and original and original == current):
            return False
        service._check_budget(source['rows'], port.budget, service.clock_ns())
        service._check_budget(tuple(row for value in service._sources.values() for row in value['rows']),
                              service.budget, service.clock_ns())
        return True

    def reprice(self, order, *, step_ticks):
        service = self._algo_service
        reference = service.prices.get_for_order(order.instrument_id, order.side)
        limits = service.instrument_limits[order.instrument_id]
        if reference is None or not 0 <= service.clock_ns() - reference.ts_event <= (
                limits.max_market_age_ns or 120_000_000_000):
            return None
        planner = service.client.planner.planner
        tick = planner._tick_size
        tick = tick[order.instrument_id] if isinstance(tick, dict) else tick
        raw = order.price + tick * step_ticks * (1 if order.side is OrderSide.BUY else -1)
        rounding = ROUND_CEILING if order.side is OrderSide.BUY else ROUND_FLOOR
        price = (raw / tick).to_integral_value(rounding=rounding) * tick
        if not price.is_finite() or price <= 0:
            raise ValueError("NT算法重报价无效")
        reducing = (service._ports[order.strategy_id].reduce_only_check()
            or service._sources[order.strategy_id].get('metadata', {}).get('market_health_mode') == 'REDUCE_ONLY'
            or order.metadata.get('market_health_mode') == 'REDUCE_ONLY')
        return replace(order, price=price, metadata={**order.metadata,
            "actual_send_ts_ns": service.clock_ns(),
            "market_health_mode": "REDUCE_ONLY" if reducing else "NORMAL"})

    def save(self):
        if self._after_transition is not None:
            self._after_transition()

    def process_algorithm_commands(self):
        if self._algo_service.client.is_armed:
            try:
                self.algo_runtime.drain()
            except Exception:
                self._algo_service._failed = True
                self._algo_service.client.disarm("nt_algorithm_failed")
                raise

    def execution_stopped(self, order, reason):
        # 原目标冻结在已成交归属；只有后续新版本策略决策才能恢复，避免Runner无限重开任务。
        service = self._algo_service
        if order.strategy_id not in service._suspended:
            service.suspend_strategy(order.strategy_id)
        service.poll_stopping()
        self.save()

    def algorithm_active_instruments(self):
        return self.algo_runtime.active_instruments()

    def validate_algorithm_state(self):
        known = {identity: order for identity, order in self._orders.values()}
        tasks = self.algo_runtime.snapshot_state()["tasks"]
        for key, task in tasks.items():
            order = known.get(task["active"])
            if task["active"] is not None and (order is None or order.metadata.get(TASK_KEY) != key):
                raise RuntimeError("NT算法任务缺少柜台已核验的真实委托关联")
        for identity, order in known.items():
            task = tasks.get(order.metadata.get(TASK_KEY))
            if task is None or task["active"] != identity:
                raise RuntimeError("真实委托缺少对应的NT算法任务进度")

    @_serialized_transition
    def cancel_algorithm_tasks(self, strategy_id, *, instruments=None, reason="strategy_cancel"):
        self.algo_runtime.cancel_strategy(strategy_id, instruments=instruments, reason=reason)
        self.save()

    def cancel_strategy(self, strategy_id, *, opening_only=False, client_order_ids=None):
        self.algo_runtime.cancel_strategy(strategy_id, opening_only=opening_only,
            identities=None if client_order_ids is None else frozenset(client_order_ids))
        return super().cancel_strategy(strategy_id, opening_only=opening_only,
            client_order_ids=client_order_ids)
