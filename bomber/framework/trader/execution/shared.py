"""策略专属端口与共享账户协调；不连接柜台、不运行策略算法。"""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
import threading
import time

from bomber.framework.trader.contracts import ExecutionRequest
from bomber.framework.trader.portfolio import AccountTargetKey
from .account import AccountExecutionService
from .events import FillEvent


@dataclass(frozen=True)
class SharedRiskBudget:
    """按归属绝对量计预算，反向策略不能靠账户净额绕过限制。"""
    max_gross_quantity: Decimal
    max_gross_notional: Decimal

    def __post_init__(self):
        for name in ("max_gross_quantity", "max_gross_notional"):
            value = Decimal(str(getattr(self, name)))
            if not value.is_finite() or value <= 0:
                raise ValueError("共享账户预算须为有限正数")
            object.__setattr__(self, name, value)

    def state(self):
        return {name: str(getattr(self, name)) for name in
                ("max_gross_quantity", "max_gross_notional")}


class _SharedAccountBackend:
    """每笔订单进入原Backend前检查冻结贡献；不替换原Driver及回报链路。"""
    def __init__(self, service, backend):
        self.service, self.backend = service, backend

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def submit_order(self, order):
        service = self.service
        now = service.clock_ns()
        if (service._failed or service.client.report_errors or not service.client.is_reconciled
                or service.position_manager.is_recovery_required(service.client_id)
                or getattr(service.client, "portfolio_failure", None)):
            raise RuntimeError("共享账户逐笔发送闸门关闭")
        with service._lock:
            sources = deepcopy(service._sources)
            suspended = set(service._suspended)
        legs = order.metadata.get("allocation_legs", ())
        if not legs:
            raise ValueError("共享账户订单缺少冻结归属")
        for leg in legs:
            sid = leg["strategy_id"]
            source = sources.get(sid)
            port = service._ports.get(sid)
            if (source is None or port is None or sid in suspended or not port._started
                    or not port.ready_check() or (source.get("deadline_ns") is not None
                        and now > source["deadline_ns"])):
                raise RuntimeError("冻结归属的策略贡献已暂停、过期或未就绪")
        return self.backend.submit_order(order)


class SharedAccountExecutionService(AccountExecutionService):
    """同账户单写核心；策略停止只撤销其未完成意图，已归属仓位保留。

    回报先由既有客户端入账，再排入各端口队列。控制线程派发策略回调，
    避免柜台回调持客户端锁等待策略Runner锁。账户宿主持有独立连接租约。
    """
    def __init__(self, client, *, prices, instrument_limits, budget, checkpoint=None,
                 clock_ns=time.time_ns, max_events=10000, instrument_scope=None, execution_mode="netted"):
        super().__init__(client, checkpoint=checkpoint)
        if max_events < 1 or not instrument_limits:
            raise ValueError("须声明执行合约及正数回报队列上限")
        self.prices, self.instrument_limits = prices, dict(instrument_limits)
        if instrument_scope is not None and instrument_scope.instruments != frozenset(str(i) for i in instrument_limits):
            raise ValueError("共享持仓范围须与执行合约一致")
        self.instrument_scope = instrument_scope
        if execution_mode not in {"netted", "independent"}:
            raise ValueError("共享执行模式须为netted或independent")
        self.execution_mode = execution_mode
        if execution_mode == "independent":
            from .ctp.limit_planner import CtpLimitPlanner, IndependentCtpLimitPlanner
            if not isinstance(client.planner, CtpLimitPlanner):
                raise ValueError("独立模式须使用原生CTP今昨仓限价规划")
            client.planner = IndependentCtpLimitPlanner(client.planner, self.position_manager)
            from .ctp.independent_risk import IndependentCtpRiskManager
            if client.risk_manager is None:
                raise ValueError("独立订单须配置原风控")
            client.risk_manager = IndependentCtpRiskManager(client.risk_manager, self.position_manager)
        self.budget, self.clock_ns, self.max_events = budget, clock_ns, max_events
        self._ports = {}
        self._suspended = set()
        self._draining = set()
        self._cancelled = set()
        self.bootstrap_signals = {}
        self.event_journal = None
        self.execution_stop = None
        self.client.backend = _SharedAccountBackend(self, client.backend)
        self.client.register_execution_event_handler(self._route_event)

    def set_checkpoint(self, checkpoint):
        if self._leases or self._checkpoint is not None:
            raise RuntimeError("账户检查点须在启动前绑定一次")
        self._checkpoint = checkpoint

    def strategy_client(self, strategy_id, *, routes, budget, instrument_limits=None):
        """启动前注册明确的逻辑键到真实合约映射；不同策略使用不同端口。"""
        if not strategy_id.strip():
            raise ValueError("策略身份不能为空")
        routes = dict(routes)
        if not routes or any(not key.strip() or instrument not in self.instrument_limits
                             for key, instrument in routes.items()):
            raise ValueError("策略路由为空或超出账户执行范围")
        if self.execution_mode == "independent" and len(set(routes.values())) != len(routes):
            raise ValueError("独立订单模式的逻辑键须对应不同真实合约")
        limits = dict(instrument_limits or {i: self.instrument_limits[i] for i in routes.values()})
        if set(limits) != set(routes.values()):
            raise ValueError("策略限额须覆盖自己的完整合约路由")
        port = SharedStrategyExecutionClient(self, strategy_id, routes, budget, limits)
        existing = self._ports.get(strategy_id)
        if existing is not None:
            if (existing._started or strategy_id in self._draining
                    or existing.configuration() != port.configuration()):
                raise ValueError("策略租约仍有效、撤单未完成或重启配置不一致")
            return existing
        if self._leases:
            raise ValueError("新策略须在账户启动前注册")
        self._ports[strategy_id] = port
        return port

    def _check_budget(self, rows, budget, now_ns):
        quantity = notional = Decimal(0)
        for row in rows:
            amount = abs(Decimal(str(row["quantity"])))
            if not amount.is_finite():
                raise ValueError("策略贡献数量须有限")
            instrument = next((i for i in self.instrument_limits if str(i) == row["instrument_id"]), None)
            if instrument is None:
                raise ValueError("策略贡献合约未注册")
            owned = self.position_manager.position(row["strategy_id"], row["target_key"])
            working = self.position_manager.attribution.working_quantity(
                self.client_id, instrument, row["strategy_id"], row["target_key"])
            amount = max(amount, abs(owned), abs(owned + working))  # 未平掉/撤掉的风险不能被零目标释放。
            quantity += amount
            if not amount:
                continue
            reference = self.prices.get(instrument)
            limits = self.instrument_limits[instrument]
            if reference is None or not 0 <= now_ns - reference.ts_event <= (limits.max_market_age_ns or 120_000_000_000):
                raise RuntimeError("共享执行预算缺少当前有效参考价")
            notional += amount * reference.price * limits.contract_multiplier
        if quantity > budget.max_gross_quantity or notional > budget.max_gross_notional:
            raise ValueError(f"策略或账户归属总量／名义金额超过预算: gross_quantity={quantity} "
                f"max_gross_quantity={budget.max_gross_quantity} gross_notional={notional} "
                f"max_gross_notional={budget.max_gross_notional}")

    def submit_targets(self, request):
        with self._dispatch_lock:
            # 已知账户故障须在修改来源版本、撤单或写检查点之前拒绝新贡献。
            if (self._failed or self.client.report_errors or not self.client.is_reconciled
                    or self.position_manager.is_recovery_required(self.client_id)
                    or getattr(self.client, "portfolio_failure", None)):
                raise RuntimeError("共享账户执行状态不明，拒绝新策略贡献")
            port = self._ports.get(request.strategy_id)
            if port is None or not port._started or request.strategy_id in self._suspended:
                raise RuntimeError("策略端口未启动或已暂停")
            now = self.clock_ns()
            if (isinstance(request.revision, bool) or not isinstance(request.revision, int) or request.revision < 1
                    or isinstance(request.ts_event, bool) or not isinstance(request.ts_event, int)
                    or not 0 <= now - request.ts_event <= 120_000_000_000):
                raise ValueError("共享策略目标版本或执行时间无效")
            if request.execution_policy != "DIRECT":
                raise ValueError("共享账户首期只支持DIRECT执行政策")
            if request.deadline_ns is not None and (isinstance(request.deadline_ns, bool)
                    or not isinstance(request.deadline_ns, int) or now > request.deadline_ns):
                raise ValueError("策略贡献期限无效或已到期")
            rows = tuple(request.metadata.get("position_attribution", ()))
            if {row["target_key"] for row in rows} != set(port.routes) or any(
                    row["strategy_id"] != port.strategy_id or
                    row["instrument_id"] != str(port.routes[row["target_key"]]) for row in rows):
                raise ValueError("策略贡献须覆盖自己的完整声明路由")
            self._check_budget(rows, port.budget, now)
            totals = {}
            for row in rows:
                instrument = port.routes[row["target_key"]]
                totals[instrument] = totals.get(instrument, Decimal(0)) + Decimal(row["quantity"])
            if len(rows) != len(port.routes) or totals != dict(request.targets):
                raise ValueError("策略归属与完整目标不一致")
            for instrument, quantity in totals.items():
                limits = port.instrument_limits[instrument]
                reference = self.prices.get(instrument)
                if limits.max_abs_position is not None and abs(quantity) > limits.max_abs_position:
                    raise ValueError("策略合约仓位超过限额")
                if (quantity and limits.max_abs_position_notional is not None and
                        abs(quantity) * reference.price * limits.contract_multiplier > limits.max_abs_position_notional):
                    raise ValueError("策略合约名义金额超过限额")
            rows = tuple({**row, "source_revision": request.revision,
                          "source_deadline_ns": request.deadline_ns} for row in rows)
            request = replace(request, metadata={**request.metadata, "position_attribution": rows})
            with self._lock:
                previous = self._sources.get(request.strategy_id)
                all_rows = rows + tuple(row for sid, source in self._sources.items()
                    if sid != request.strategy_id for row in source["rows"])
            self._check_budget(all_rows, self.budget, now)
            # 旧挂单仍按旧冻结计划入账。取消确认后才规划新目标。
            changed = previous is not None and {
                (row["target_key"], row["instrument_id"]): row["quantity"] for row in previous["rows"]} != {
                (row["target_key"], row["instrument_id"]): row["quantity"] for row in rows}
            if changed and request.revision > previous["revision"]:
                self._cancel_owner(request.strategy_id)
            super().submit_targets(request)

    def _active_instruments(self):
        states = {str(state.instrument_id) for state in self.client.order_state_machine.states()
                  if not state.status.is_terminal}
        return states | {order["instrument_id"] for order in
            self.position_manager.attribution.state()["orders"].values()
            if not order["released"] and any(Decimal(leg["filled"]) < Decimal(leg["quantity"])
                                             for leg in order["legs"])}

    def _cancel_owner(self, strategy_id):
        if strategy_id not in self._cancelled:
            try:
                self.client.cancel_strategy(strategy_id)
            except Exception:
                self._failed = True
                self.client.disarm("shared_cancel_failed")
                raise
            self._cancelled.add(strategy_id)

    def _owner_working(self, strategy_id):
        return any(not order["released"] and any(leg["strategy_id"] == strategy_id
            and Decimal(leg["filled"]) < Decimal(leg["quantity"]) for leg in order["legs"])
            for order in self.position_manager.attribution.state()["orders"].values())

    def _send(self, request):
        from .contracts import OrderBudgetExhausted
        if self.execution_stop is not None:
            return
        try:
            self._send_available(request)
        except OrderBudgetExhausted as error:
            self.stop_for_order_budget(error)

    def stop_for_order_budget(self, error):
        self.execution_stop = error.state()
        for sid, port in self._ports.items():
            if port._started and sid not in self._suspended:
                self.suspend_strategy(sid)
        self._save()

    def _send_available(self, request):
        now = self.clock_ns()
        with self._lock:
            sources = deepcopy(self._sources)
        # 任一过期贡献尚未完成撤单/冻结时，不准用另一策略的期限继续发送。
        if any(sid not in self._suspended and source.get("deadline_ns") is not None
               and now > source["deadline_ns"] for sid, source in sources.items()):
            return
        if any(sid not in self._suspended and not self._ports[sid].ready_check()
               for sid in sources):
            return
        active = self._active_instruments()
        targets = {i: q for i, q in request.targets.items() if str(i) not in active}
        if not targets:
            return
        scope = {str(i) for i in targets}
        deadlines = [source["deadline_ns"] for sid, source in sources.items()
                     if sid not in self._suspended and source.get("deadline_ns") is not None
                     and any(row["instrument_id"] in scope for row in source["rows"])]
        deadline = min(deadlines) if deadlines else None
        reduce_only = any(source.get("metadata", {}).get("market_health_mode") == "REDUCE_ONLY"
                          for source in sources.values())
        metadata = {**request.metadata,
            "position_attribution": tuple(row for row in request.metadata["position_attribution"]
                                           if row["instrument_id"] in scope),
            "actual_send_deadline_ns": deadline,
            "execution_trigger_ns": request.ts_event,
            "actual_send_ts_ns": now,
            "market_health_mode": "REDUCE_ONLY" if reduce_only else "NORMAL"}
        if self.execution_mode == "independent":
            self.validate_owned_state()
            for sid, source in sorted(sources.items()):
                if sid in self._suspended or not self._ports[sid]._started:
                    continue
                available = scope - self._active_instruments()
                rows = tuple(row for row in source["rows"] if row["instrument_id"] in available)
                owned_targets = {self._ports[sid].routes[row["target_key"]]: Decimal(row["quantity"])
                                 for row in rows}
                if not owned_targets:
                    continue
                self.client.submit_targets(replace(request, strategy_id=sid, targets=owned_targets, ts_event=now,
                    metadata={**metadata, "position_attribution": rows,
                              "shared_execution_mode": "independent"}))
            return
        self.client.submit_targets(replace(request, targets=targets, ts_event=now,
            metadata=metadata, deadline_ns=deadline))

    def suspend_strategy(self, strategy_id):
        """停止新增风险，撤销涉及本策略的母单，等待回报后冻结在实际归属量。"""
        with self._dispatch_lock:
            if strategy_id not in self._ports:
                raise ValueError("未知策略")
            with self._lock:
                self._suspended.add(strategy_id)
                self._draining.add(strategy_id)
            self._cancel_owner(strategy_id)
            self._save()

    def _save(self):
        if self._checkpoint is not None:
            self._checkpoint()

    def poll(self):
        """由账户控制线程维护期限/停止；不在柜台回调内撤单或重新发送。"""
        with self._dispatch_lock:
            if (self._failed or self.client.report_errors or not self.client.is_reconciled
                    or self.position_manager.is_recovery_required(self.client_id)
                    or getattr(self.client, "portfolio_failure", None)):
                raise RuntimeError("共享账户执行状态不明，保持闭闸")
            driver = getattr(self.client.backend, "driver", None)
            if self.execution_stop is None and driver is not None:
                from .contracts import OrderBudgetExhausted
                try:
                    check = getattr(driver, "check_order_capacity", None)
                    if callable(check):
                        check(1)
                except OrderBudgetExhausted as error:
                    self.stop_for_order_budget(error)
            now = self.clock_ns()
            with self._lock:
                expired = [sid for sid, source in self._sources.items()
                           if sid not in self._suspended and source.get("deadline_ns") is not None
                           and now > source["deadline_ns"]]
            for sid in expired:
                self.suspend_strategy(sid)
            self._cancelled = {sid for sid in self._cancelled if self._owner_working(sid)}
            for sid in tuple(self._draining):
                port = self._ports[sid]
                if self._owner_working(sid):
                    continue
                source = self._sources.get(sid)
                if source is not None:
                    rows = tuple(dict(strategy_id=sid, target_key=key,
                        instrument_id=str(instrument), quantity=str(self.position_manager.position(sid, key)),
                        source_revision=source["revision"], source_deadline_ns=None)
                        for key, instrument in port.routes.items())
                    targets = {}
                    for row in rows:
                        instrument = port.routes[row["target_key"]]
                        targets[instrument] = targets.get(instrument, Decimal(0)) + Decimal(row["quantity"])
                    # 内部版本单调，原信号版本保留供策略恢复；冻结不伪造新信号。
                    with self._lock:
                        source["rows"], source["targets"] = rows, {
                            AccountTargetKey(self.client_id, i): q for i, q in targets.items()}
                        source["deadline_ns"] = None
                        revision = self.coordinator.state().strategy_revisions.get(sid, 0) + 1
                        self.coordinator.update(strategy_id=sid, revision=revision, ts_event=now,
                                                targets=source["targets"])
                with self._lock:
                    self._draining.discard(sid)
                    self._cancelled.discard(sid)
                self._save()

    def continue_targets(self, strategy_id, instrument_id, now_ns):
        """保持各源版本，用当前合约的新行情推进账户剩余量。"""
        with self._dispatch_lock:
            if strategy_id in self._suspended or self._draining or self._failed:
                return False
            source = self._sources.get(strategy_id)
            if source is None or instrument_id not in self.instrument_limits:
                return False
            snapshot = self.coordinator.snapshot(ts_event=now_ns)
            targets = {key.instrument_id: q for key, q in snapshot.targets.items()
                       if key.instrument_id == instrument_id}
            rows = tuple(row for value in self._sources.values() for row in value["rows"]
                         if row["instrument_id"] == str(instrument_id))
            self._pending = True
            try:
                self._save()
                self._send(ExecutionRequest(strategy_id, snapshot.revision, self.client_id, now_ns,
                    targets, "DIRECT", metadata={"position_attribution": rows, "retained_target_retry": True}))
                if self.client.report_errors or not self.client.is_reconciled:
                    raise RuntimeError("账户续执行失败")
                self._pending = False
                self._save()
                return True
            except Exception:
                self._failed = True
                raise

    def reconcile_finished_checkpoint(self):
        """权威账户校验后由生命周期调用；只冻结贡献，不授权或重发目标。"""
        with self._dispatch_lock:
            if (self._leases or self.client.is_armed or not self.client.is_reconciled
                    or self.client.report_errors or self._active_instruments()
                    or self.position_manager.is_recovery_required(self.client_id)):
                raise RuntimeError("只读修复要求未授权、已对账且不存在未结束订单或冻结份额")
            self.validate_owned_state()
            self._failed = self._pending = False
            self._suspended.update(self._ports)
            self._draining.update(self._ports)
            # 全部订单/冻结份额已结束，poll只将贡献冻结在当前归属量。
            self.poll()

    def _route_event(self, event):
        try:
            self._route_owned_event(event)
        except Exception:
            self._failed = True
            raise

    def _route_owned_event(self, event):
        if event.metadata.get("attribution_status") == "UNASSIGNED":
            raise ValueError("共享账户回报缺少可信归属")
        rows = event.metadata.get("attributed_fills" if isinstance(event, FillEvent) else "allocation_owners", ())
        if not rows:
            raise ValueError("共享账户回报缺少冻结所有者")
        remainder = event.commission if isinstance(event, FillEvent) else None
        for index, row in enumerate(rows):
            port = self._ports.get(row["strategy_id"])
            if (port is None or row["target_key"] not in port.routes
                    or str(port.routes[row["target_key"]]) != event.identity.instrument_id):
                raise ValueError("账户回报指向未知策略路由")
            metadata = {k: v for k, v in event.metadata.items()
                        if k not in {"allocation_owners", "attributed_fills"}}
            metadata.update(attributed_target_key=row["target_key"], account_event_id=event.event_id)
            if "target_revision" in row:
                metadata["attributed_target_revision"] = row["target_revision"]
            changes = dict(identity=replace(event.identity, strategy_id=port.strategy_id),
                event_id=f"{event.event_id}:{port.strategy_id}:{row['target_key']}", metadata=metadata)
            if isinstance(event, FillEvent):
                quantity = Decimal(row["quantity"])
                commission = None if event.commission is None else (remainder if index == len(rows) - 1
                    else event.commission * quantity / event.quantity)
                if commission is not None:
                    remainder -= commission
                changes.update(quantity=quantity, commission=commission,
                               cumulative_filled=Decimal(row["cumulative_filled"]))
            port._enqueue(replace(event, **changes))

    def snapshot_state(self):
        state = super().snapshot_state()
        with self._lock:
            state["shared"] = {"version": 1, "budget": self.budget.state(),
                "routes": {sid: port.configuration() for sid, port in self._ports.items()},
                "suspended": sorted(self._suspended), "draining": sorted(self._draining)}
            state["shared"]["execution_stop"] = deepcopy(self.execution_stop)
            driver = getattr(self.client.backend, "driver", None)
            if driver is not None and hasattr(driver, "order_budget"):
                state["shared"]["order_budget"] = driver.order_budget
            if self.execution_mode != "netted":
                state["shared"]["execution_mode"] = self.execution_mode
            if self.event_journal is not None:
                state["shared"]["event_journal"] = self.event_journal.snapshot_state()
            if self.instrument_scope is not None:
                state["shared"]["managed_instruments"] = sorted(self.instrument_scope.instruments)
            if self.bootstrap_signals:
                state["shared"]["bootstrap_signals"] = deepcopy(self.bootstrap_signals)
        return state

    def validate_owned_state(self):
        """账户恢复后核对每个逻辑仓位与真实合约归属，拒绝孤立/外来明细。"""
        totals = {}
        for row in self.position_manager.attribution.state()["owned"]:
            port = self._ports.get(row["strategy_id"])
            if (row["client_id"] != self.client_id or port is None
                    or row["target_key"] not in port.routes
                    or row["instrument_id"] != str(port.routes[row["target_key"]])):
                raise ValueError("共享账本包含未注册策略、账户或真实合约归属")
            key = (row["strategy_id"], row["target_key"])
            totals[key] = totals.get(key, Decimal(0)) + Decimal(row["quantity"])
        snapshot = self.position_manager.snapshot()
        for (sid, key), quantity in snapshot.strategy_positions.items():
            port = self._ports.get(sid)
            if port is None or key not in port.routes or quantity != totals.get((sid, key), Decimal(0)):
                raise ValueError("共享策略仓位与真实合约归属不一致")
        for key in snapshot.account_positions:
            if key.client_id != self.client_id or (self.instrument_scope is None
                    and key.instrument_id not in self.instrument_limits):
                raise ValueError("共享执行账本包含其他账户或合约")
        for key, quantity in snapshot.working_quantities.items():
            if quantity and (key.client_id != self.client_id or key.instrument_id not in self.instrument_limits):
                raise ValueError("共享执行账本包含范围外在途委托")
        if self.execution_mode == "independent":
            ledger = self.client.planner.planner._planner.ledger
            for instrument in self.instrument_limits:
                owned = [Decimal(row["quantity"]) for row in self.position_manager.attribution.state()["owned"]
                         if row["instrument_id"] == str(instrument) and row["client_id"] == self.client_id]
                gross = ledger.snapshot(instrument)
                generic = str(instrument).rsplit(".", 1)[-1] == "CFFEX"
                if ((not generic and (gross.long_yesterday or gross.short_yesterday))
                        or sum((max(q, Decimal(0)) for q in owned), Decimal(0)) != gross.long_total
                        or sum((max(-q, Decimal(0)) for q in owned), Decimal(0)) != gross.short_total):
                    raise ValueError("独立策略归属总仓与CTP同日账本不一致；禁止净额/昨仓账本混用")

    def restore_state(self, state):
        shared = state.get("shared", {})
        self.bootstrap_signals = deepcopy(shared.get("bootstrap_signals", {}))
        if shared.get("execution_mode", "netted") != self.execution_mode:
            raise ValueError("共享账户恢复执行模式不一致")
        if "event_journal" in shared:
            if self.event_journal is None:
                raise ValueError("跨进程账本须由账户服务恢复，不能转为普通宿主")
            self.event_journal.restore_state(shared["event_journal"])
        if shared.get("version") != 1 or shared.get("budget") != self.budget.state() or shared.get("routes") != {
                sid: port.configuration() for sid, port in self._ports.items()}:
            raise ValueError("共享账户恢复策略、路由或预算不一致")
        if shared.get("managed_instruments") != (None if self.instrument_scope is None
                else sorted(self.instrument_scope.instruments)):
            raise ValueError("共享账户恢复持仓范围不一致")
        suspended, draining = set(shared["suspended"]), set(shared["draining"])
        if not draining <= suspended <= self._ports.keys() or not state["sources"].keys() <= self._ports.keys():
            raise ValueError("共享账户恢复包含未知策略")
        for sid, source in state["sources"].items():
            routes = self._ports[sid].routes
            rows = source["rows"]
            deadline = source.get("deadline_ns")
            if (len(rows) != len(routes) or {row["target_key"] for row in rows} != set(routes)
                    or any(row["instrument_id"] != str(routes[row["target_key"]]) for row in rows)
                    or source.get("execution_policy", "DIRECT") != "DIRECT"
                    or (deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, int) or deadline < 0))):
                raise ValueError("恢复贡献的路由、期限或政策无效")
        super().restore_state(state)
        driver = getattr(self.client.backend, "driver", None)
        new_window = getattr(driver, "new_order_budget_window", False)
        self.execution_stop = None if new_window else deepcopy(shared.get("execution_stop"))
        if driver is not None and hasattr(driver, "restore_order_budget") and not new_window:
            budget_state = shared.get("order_budget", {**driver.order_budget,
                "submitted": len(self.client.order_state_machine.states())})
            driver.restore_order_budget(budget_state)
        if self.event_journal is not None and any(row["status"] != "completed"
                for row in self.event_journal.requests.values()):
            self._failed = True
        self._suspended, self._draining = suspended, draining
        # 恢复旧目标先暂停，策略预热产生新目标后显式解除；不自动发送。
        self._suspended.update(self._sources)


class SharedStrategyExecutionClient:
    """ExecutionClientPort兼容端口；PositionManager是账户唯一权威实例。"""
    shared_account = True

    def __init__(self, service, strategy_id, routes, budget, instrument_limits):
        self.service, self.strategy_id, self.routes, self.budget = service, strategy_id, routes, budget
        self.client_id, self.account_id = service.client_id, service.account_id
        self.position_manager = service.position_manager
        self.instrument_limits = instrument_limits
        self.ready_check = lambda: True
        self._started = False
        self._events, self._handlers = deque(), []
        self._lock = threading.RLock()
        self._stopping = self._events_closed = False
        self._fills_enqueued = self._fills_delivered = 0
        self._last_fill_enqueued = self._last_fill_delivered = None

    @property
    def is_reconciled(self):
        return (self.service.client.is_reconciled and not self.service._failed
                and not self.position_manager.is_recovery_required(self.client_id)
                and not getattr(self.service.client, "portfolio_failure", None))

    @property
    def report_errors(self):
        return self.service.client.report_errors

    @property
    def account_reconciliation(self):
        return self.service.client.account_reconciliation

    @property
    def account_observation(self):
        return self.service.client.account_observation

    def configuration(self):
        return {"routes": {k: str(i) for k, i in self.routes.items()}, "budget": self.budget.state(),
            "limits": {str(i): {name: None if value is None else str(value)
                for name, value in vars(limits).items()} for i, limits in self.instrument_limits.items()}}

    def start(self):
        if not self._started:
            if self._events:
                raise RuntimeError("策略尚有未消费回报，不能重启端口")
            self.service.start()
            self._stopping = self._events_closed = False
            self._started = True

    def begin_stop(self):
        """关闭新目标，保留Runner绑定及回调直到冻结订单终结。"""
        self._stopping = True
        self.service.suspend_strategy(self.strategy_id)

    def finish_stop_events(self):
        # 与账户回报入账/归属路由使用同一锁，避免终态已入账但事件尚未入队。
        # 用户回调在释放账户锁后执行，不阻塞柜台线程或反向等待Runner锁。
        with self.service.client._submit_lock:
            if any(not order["released"]
                    and any(Decimal(leg["filled"]) < Decimal(leg["quantity"]) for leg in order["legs"])
                    and any(leg["strategy_id"] == self.strategy_id
                    for leg in order["legs"]) for order in
                    self.position_manager.attribution.state()["orders"].values()):
                return False
            with self._lock:
                self._events_closed = True
        self.drain_events()
        return True

    def event_diagnostics(self):
        with self._lock:
            return {"stopping": self._stopping, "closed": self._events_closed,
                "pending_events": len(self._events),
                "pending_fills": sum(isinstance(event, FillEvent) for event in self._events),
                "fills_enqueued": self._fills_enqueued, "fills_delivered": self._fills_delivered,
                "last_fill_enqueued": self._last_fill_enqueued,
                "last_fill_delivered": self._last_fill_delivered}

    def stop(self):
        if self._started:
            if not self._stopping:
                self.service.suspend_strategy(self.strategy_id)
            self._started = False
            self._handlers.clear()
            self.service.stop()

    def disarm(self, reason="operator"):
        self.service.suspend_strategy(self.strategy_id)

    def cancel_strategy(self, strategy_id):
        if strategy_id != self.strategy_id:
            raise ValueError("策略端口不能撤销其他策略")
        with self.service._dispatch_lock:
            self.service._cancel_owner(strategy_id)

    def submit_targets(self, request):
        if self._stopping:
            raise RuntimeError("策略正在停止，禁止提交新目标")
        if request.strategy_id != self.strategy_id:
            raise ValueError("策略端口请求身份不符")
        # 暂停后的新版本可恢复；旧版本重发不能解除暂停。
        with self.service._dispatch_lock:
            if self.service.execution_stop is not None:
                return
            source = self.service._sources.get(self.strategy_id)
            was_suspended = self.strategy_id in self.service._suspended
            if was_suspended:
                if self.strategy_id in self.service._draining or (source is not None and request.revision <= source["revision"]):
                    raise RuntimeError("暂停策略须完成撤单并提交更高版本目标")
                self.service._suspended.remove(self.strategy_id)
            try:
                self.service.submit_targets(request)
            except Exception:
                if was_suspended:
                    self.service._suspended.add(self.strategy_id)
                raise

    def continue_target(self, strategy_id, target_key, now_ns, *, instrument_id):
        if self._stopping:
            return
        if strategy_id != self.strategy_id or self.routes.get(target_key) != instrument_id:
            raise ValueError("续执行路由不属于此策略")
        return self.service.continue_targets(strategy_id, instrument_id, now_ns)

    def register_execution_event_handler(self, handler):
        if handler not in self._handlers:
            self._handlers.append(handler)

    def _enqueue(self, event):
        with self._lock:
            if self.service.event_journal is not None:
                self.service.event_journal.append(self.strategy_id, event)
                return  # 跨进程事件由持久化日志及确认游标消费，不进入无人绑定队列。
            if self._events_closed:
                self.service._failed = True
                raise RuntimeError("已关闭策略收到新执行回报，账户须闭闸对账")
            if len(self._events) >= self.service.max_events:
                self.service._failed = True
                raise RuntimeError("策略回报队列溢出，账户须闭闸对账")
            self._events.append(event)
            if isinstance(event, FillEvent):
                self._fills_enqueued += 1
                self._last_fill_enqueued = event.event_id

    def drain_events(self):
        if not self._started:
            return 0
        count = 0
        while True:
            with self._lock:
                if not self._events:
                    return count
                event = self._events[0]
            try:
                if not self._handlers:
                    raise RuntimeError("策略回报没有处理器，不能静默消费")
                for handler in tuple(self._handlers):
                    handler(event)
            except Exception:
                self.service._failed = True
                self.service.client.disarm("strategy_callback_failed")
                self.service._save()
                raise
            with self._lock:
                self._events.popleft()
                if isinstance(event, FillEvent):
                    self._fills_delivered += 1
                    self._last_fill_delivered = event.event_id
            count += 1
