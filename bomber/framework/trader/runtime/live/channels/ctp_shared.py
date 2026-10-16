"""共享账户CTP生命周期；一个TD、一个账户检查点、同机唯一写入锁。"""

import time
import os
from copy import deepcopy
from decimal import Decimal

from bomber.framework.trader.persistence import JsonStateStore, RuntimeStateManager
from bomber.framework.trader.portfolio import TargetStore
from ..recovery import SessionIdentityCheckpoint
from ...ownership import AccountWriterIdentity, account_writer_lease
from ...ctp_account import position_buckets
from .ctp import account_lock, assert_flat_account, occupied_positions


class CtpSharedAccountLifecycle:
    """不管理某个策略的行情、信号或指标；策略宿主与此控制器组合。"""
    def __init__(self, execution, service, *, transport, state_file, identity,
                 resources, expected_day, reconcile_seconds=10, cancel_seconds=15, position_scope=None,
                 rollover_transform=None, preserve_external=False):
        self.execution, self.service, self.transport = execution, service, transport
        self.driver, self.client, self.ledger = execution.driver, execution.client, execution.ledger
        self.resources, self.expected_day = resources, expected_day
        self.rollover_transform = rollover_transform
        self.rollover_audit = None
        self.reconcile_seconds, self.cancel_seconds = reconcile_seconds, cancel_seconds
        if position_scope != service.instrument_scope or (position_scope is not None
                and position_scope != getattr(execution.backend, "instrument_scope", None)):
            raise ValueError("共享账户、归属服务和报单Backend的持仓范围须一致")
        self.position_scope = position_scope
        from bomber.framework.trader.execution.policy import AccountReconciliationMode
        if preserve_external and (position_scope is None or service.execution_mode != "independent"
                or self.client.account_reconciliation is not AccountReconciliationMode.OBSERVE):
            raise ValueError("外部基线须使用独立订单、明确范围及只观测柜台仓位")
        self.preserve_external = preserve_external
        planner = getattr(self.client.planner, "planner", self.client.planner)
        self.generic_close_venues = planner._planner.generic_close_venues
        self.external_gross, self.external_buckets = {}, {}
        components = {"shared_account": service, "session_identity": SessionIdentityCheckpoint(identity)}
        if position_scope is not None:
            components["external_positions"] = self
        self.manager = RuntimeStateManager(JsonStateStore(state_file), TargetStore(), service.coordinator,
            execution.positions, order_machines={service.client_id: self.client.order_state_machine},
            ctp_ledgers={service.client_id: self.ledger}, ctp_drivers={service.client_id: self.driver},
            state_components=components)
        self.manager.enable_ctp_autosave(service.client_id, self.client)
        service.set_checkpoint(self.manager.save)
        self._next_check = 0
        self._started = False
        self._restored = False
        self._ready = False
        self.final_gross = self.final_active_orders = None

    @staticmethod
    def acquire(resources, transport, *, environment):
        """同时兼容已有原生入口与Joint入口，不能绕过旧账户锁。"""
        for namespace in ("bomber-ctp", "bomber-main-ema"):
            resources.enter_context(account_lock(transport.account_id, namespace=namespace))
        resources.enter_context(account_writer_lease((
            AccountWriterIdentity("CTP", environment, f"{transport.broker_id}:{transport.account_id}"),)))

    def _validate_account(self):
        # 查询不持提交/回报锁；查询期间的已知成交改变快照时重新核验，避免误报基线变化。
        for _ in range(3):
            with self.client._submit_lock:
                before = (self.ledger.state(), self.execution.positions.state())
            try:
                gross, buckets = self._observe_positions()
                active = self.driver.reconcile_active_orders().orders
            except Exception:
                with self.client._submit_lock:
                    if before != (self.ledger.state(), self.execution.positions.state()):
                        continue
                raise
            with self.client._submit_lock:
                if before != (self.ledger.state(), self.execution.positions.state()):
                    continue
                self._validate_account_snapshot(gross, buckets, active)
                return
        raise RuntimeError("共享账户权威查询期间成交持续变化，保留账本并重新核验")

    def _validate_account_snapshot(self, gross, buckets, active):
        self.service.validate_owned_state()
        self._validate_external(gross, buckets)
        managed = gross if self.position_scope is None else self.position_scope.select(gross)
        if self.preserve_external:
            managed = self._subtract_baseline(managed, self.position_scope.select(self.external_gross), 2)
        expected = {str(i): (p.long_total, p.short_total) for i, p in self.ledger.state().positions.items()
                    if p.long_total or p.short_total}
        if managed != expected:
            raise RuntimeError("共享账户柜台总仓与保存CTP账本不一致")
        if self.preserve_external:
            from .ctp_owned import external_partition
            remaining, residual = external_partition(self.ledger.state(), gross, buckets, self.generic_close_venues)
            if remaining != self.external_gross:
                raise RuntimeError("柜台剩余多空仓与外部基线不一致；禁止改写策略归属")
            for key, values in residual.items():
                if key.rsplit(".", 1)[-1] not in self.generic_close_venues and values != self.external_buckets.get(key):
                    raise RuntimeError("明确今昨平仓合约的外部桶变化")
            # 通用CLOSE仅减少逻辑归属份额；柜台可能优先减少其他物理今昨桶。
            self.external_buckets = residual
        if not set(self.ledger.state().positions) <= set(self.service.instrument_limits):
            raise RuntimeError("共享CTP账本包含未注册合约")
        for instrument in self.service.instrument_limits:
            state = self.ledger.snapshot(instrument)
            generic = self.preserve_external and str(instrument).rsplit(".", 1)[-1] in self.generic_close_venues
            actual = buckets.get(str(instrument))
            if self.preserve_external and not generic:
                actual = self._subtract_baseline({str(instrument): actual} if actual else {},
                    {str(instrument): self.external_buckets[str(instrument)]}
                    if str(instrument) in self.external_buckets else {}, 4).get(str(instrument))
            if not generic and (actual if actual else (0, 0, 0, 0)) != (
                    state.long_today, state.long_yesterday, state.short_today, state.short_yesterday):
                raise RuntimeError("共享账户柜台今昨仓与保存账本不一致")
            if self.execution.positions.account_position(self.service.client_id, instrument) != state.net_position:
                raise RuntimeError("共享账户执行净仓与CTP账本不一致")
            if self.execution.positions.unassigned_position(self.service.client_id, instrument):
                raise RuntimeError("共享账户存在未归属仓位")
        if active:
            raise RuntimeError("首期共享恢复要求柜台无活动订单；未知或漏成交须先对账")

    def _observe_positions(self, *, external_only=False):
        gross = occupied_positions(self.transport)
        allowed = set(gross) | (set() if self.position_scope is None else self.position_scope.instruments)
        buckets = position_buckets(self.transport.query_position_details(), allowed)
        observed = {key: (row.quantities[0] + row.quantities[1], row.quantities[2] + row.quantities[3])
                    for key, row in buckets.items()}
        # 在线查询期间管理合约可正常成交；停机/恢复才比较其完整总仓。
        if external_only:
            observed, expected = self.position_scope.external(observed), self.position_scope.external(gross)
        else:
            expected = gross
        if observed != expected:
            raise RuntimeError("共享账户权威总仓与今昨明细不一致")
        return gross, {key: row.quantities for key, row in buckets.items()}

    def _validate_external(self, gross, buckets):
        if self.position_scope is None:
            return
        external_gross = self.position_scope.external(self.external_gross)
        external_buckets = self.position_scope.external(self.external_buckets)
        if (self.position_scope.external(gross) != external_gross
                or self.position_scope.external(buckets) != external_buckets):
            self.client.disarm("external_position_changed")
            self.execution.positions.mark_recovery_required(self.service.client_id)
            raise RuntimeError("共享账户范围外持仓或今昨桶变化，保留归属并闭闸")
        if self.preserve_external:
            return  # 执行账本仅为系统自己的成交；基线不写入PositionManager。
        local = {str(key.instrument_id): q for key, q in self.execution.positions.snapshot().account_positions.items()
                 if key.client_id == self.service.client_id and not self.position_scope.contains(key.instrument_id) and q}
        expected = {key: long - short for key, (long, short) in self.external_gross.items() if long != short}
        if local != expected:
            self.client.disarm("external_position_conflict")
            self.execution.positions.mark_recovery_required(self.service.client_id)
            raise RuntimeError("共享账户范围外净仓与权威基线不一致")

    @staticmethod
    def _subtract_baseline(actual, baseline, size):
        result = {}
        for key in actual.keys() | baseline.keys():
            values = tuple(a - b for a, b in zip(actual.get(key, (Decimal(0),) * size),
                baseline.get(key, (Decimal(0),) * size)))
            if any(q < 0 for q in values):
                raise RuntimeError("柜台外部基线仓位被减少，禁止使用其他来源仓位")
            if any(values):
                result[key] = values
        return result

    def snapshot_state(self):
        """外部仓位保存权威基线，不进入策略归属；version2可与执行合约重叠。"""
        return {"version": 2 if self.preserve_external else 1,
            "managed_instruments": sorted(self.position_scope.instruments),
            "gross": {key: [str(q) for q in values] for key, values in self.external_gross.items()},
            "buckets": {key: [str(q) for q in values] for key, values in self.external_buckets.items()}}

    def restore_state(self, state):
        if state.get("version") != (2 if self.preserve_external else 1) or state.get("managed_instruments") != sorted(self.position_scope.instruments):
            raise ValueError("范围外持仓检查点的管理合约不一致")
        gross = {key: tuple(Decimal(q) for q in values) for key, values in state["gross"].items()}
        buckets = {key: tuple(Decimal(q) for q in values) for key, values in state["buckets"].items()}
        if (gross.keys() != buckets.keys() or (not self.preserve_external and any(self.position_scope.contains(key) for key in gross))
                or any(len(values) != size or any(not q.is_finite() or q < 0 or q != q.to_integral_value()
                    for q in values) for rows, size in ((gross, 2), (buckets, 4)) for values in rows.values())
                or any(not (long or short) or (long, short) != (buckets[key][0] + buckets[key][1],
                    buckets[key][2] + buckets[key][3]) for key, (long, short) in gross.items())):
            raise ValueError("范围外持仓检查点的总仓或今昨桶无效")
        self.external_gross, self.external_buckets = gross, buckets

    def scope_diagnostics(self):
        return {"policy": "account" if self.position_scope is None else "strategy_instruments",
            "existing_position_policy": "preserve_external" if self.preserve_external else "require_flat",
            "system_gross": {str(i): (p.long_total, p.short_total) for i, p in self.ledger.state().positions.items()
                if p.long_total or p.short_total},
            "managed_instruments": None if self.position_scope is None else sorted(self.position_scope.instruments),
            "external_gross": self.external_gross,
            "final_managed_gross": None if self.final_gross is None else (
                self.final_gross if self.position_scope is None else self.position_scope.select(self.final_gross))}

    def start(self, *, resume, payload_transform=None, commit_restored=True):
        if self._started:
            return
        if resume:
            saved = self.manager.repository.load()
            transform = payload_transform
            if transform is None and saved and ("vega_signal" in saved.payload["component_states"] or
                    saved.payload["component_states"]["session_identity"]["identity"].get("trading_day", self.expected_day) != self.expected_day):
                if self.rollover_transform is None:
                    raise RuntimeError("此共享会话未配置跨日恢复")
                transform = lambda payload: self.rollover_transform(self, payload)
            if self.preserve_external:
                previous_transform = transform
                def owned_transform(payload):
                    old_identity = payload["component_states"]["session_identity"]["identity"]
                    new_identity = self.manager.state_components["session_identity"].identity
                    if "strategies" in old_identity and old_identity.get("existing_position_policy") is None:
                        # 仅迁移持仓政策，其他身份/预算/算法/路由仍由原恢复严格比较。
                        if new_identity.get("existing_position_policy") == "preserve_external":
                            old_identity["existing_position_policy"] = "preserve_external"
                    payload = previous_transform(payload) if previous_transform else payload
                    from .ctp_owned import restore_external_baseline
                    return restore_external_baseline(self, payload)
                transform = owned_transform
            if self.manager.restore(payload_transform=transform) is None:
                raise RuntimeError("共享账户恢复文件不存在")
            self._restored = True
            if self.service._failed or self.service._pending:
                raise RuntimeError("共享账户检查点存在未决发送，须先权威对账")
            if self.ledger.state().trading_day != self.expected_day:
                raise RuntimeError("首期共享账户仅支持同交易日恢复")
            self.service.validate_owned_state()
        elif self.manager.repository.load() is not None:
            raise RuntimeError("共享账户新会话不能覆盖原文件")
        self.client.start()
        self._started = True
        if self.driver.trading_day != self.expected_day:
            raise RuntimeError("共享账户TD交易日不符")
        if resume:
            if any(not state.status.is_terminal for state in self.client.order_state_machine.states()):
                raise RuntimeError("首期共享恢复要求已结束订单，保留原文件并先对账")
            self._validate_account()
            if transform is not None and commit_restored:
                self.manager.commit_restore()
        else:
            if not self.preserve_external:
                assert_flat_account(self.driver, self.transport, position_scope=self.position_scope)
            if self.position_scope is not None:
                gross, buckets = self._observe_positions()
                self.external_gross = gross if self.preserve_external else self.position_scope.external(gross)
                self.external_buckets = buckets if self.preserve_external else self.position_scope.external(buckets)
                self._validate_account()
        self._ready = True
        self._next_check = time.monotonic() + self.reconcile_seconds

    def authorize(self):
        if not self._ready:
            raise RuntimeError("账户恢复或预检尚未完成")
        if self.position_scope is not None:
            self._validate_account()
        self.manager.save()
        self.client.arm_demo(self.client.DEMO_CONFIRMATION)

    def reconcile_checkpoint(self, *, new_account_config=None):
        """显式无报单修复：完整同日权威对账、备份原文件后保存暂停检查点。"""
        if self._started or self.service._leases:
            raise RuntimeError("检查点只读修复须在宿主和策略启动前执行")
        restored = self.manager.restore()
        if restored is None:
            raise RuntimeError("共享账户修复文件不存在")
        if self.ledger.state().trading_day != self.expected_day:
            raise RuntimeError("只读检查点修复仅支持同交易日")
        if self.service._active_instruments():
            raise RuntimeError("检查点包含未结束订单/冻结份额，拒绝自动修复")
        self.service.validate_owned_state()
        original_checkpoint = self.service.snapshot_state()
        original_budget = self.service.budget
        session_identity = self.manager.state_components["session_identity"]
        original_identity = deepcopy(session_identity.identity)
        new_budget = None
        if new_account_config is not None:
            from bomber.framework.trader.execution.shared import SharedRiskBudget
            expected_account = dict(new_account_config)
            expected_account["max_gross_notional"] = original_identity["account"]["max_gross_notional"]
            if expected_account != original_identity["account"]:
                raise ValueError("只读预算迁移不得改变其他账户身份或限额")
            new_budget = SharedRiskBudget(original_budget.max_gross_quantity, new_account_config["max_gross_notional"])
            if new_budget.max_gross_notional <= original_budget.max_gross_notional:
                raise ValueError("只读预算迁移须显式上调总名义限额")
        backup = self.manager.repository.path.with_name(
            self.manager.repository.path.name + f".before-reconcile-g{restored.generation}.json")
        try:
            self.client.start()  # 原恢复链校验未知活动订单、账户资金和净仓。
            if self.driver.trading_day != self.expected_day:
                raise RuntimeError("只读修复TD交易日不符")
            self._validate_account()  # 总仓、今昨桶、归属、外部基线、活动订单。
            current = self.manager.repository.load()
            if current is None or current.generation != restored.generation:
                raise RuntimeError("权威查询期间检查点发生变化，拒绝修复")
            original_bytes = self.manager.repository.path.read_bytes()
            if backup.exists():
                if backup.read_bytes() != original_bytes:
                    raise RuntimeError("同代修复备份内容不一致")
            else:
                with backup.open("xb") as output:
                    output.write(original_bytes)
                    output.flush()
                    os.fsync(output.fileno())
            # 只写一次最终检查点；冻结过程中不触发中间版本自动保存。
            checkpoint = self.service._checkpoint
            self.service._checkpoint = None
            try:
                self.service.reconcile_finished_checkpoint()
                self._validate_account()
                final_gross = occupied_positions(self.transport)
                if new_budget is not None:
                    self.service.budget = new_budget
                    session_identity.identity["account"] = deepcopy(new_account_config)
                saved = self.manager.save()
            except Exception:
                self.service.budget = original_budget
                session_identity.identity = original_identity
                self.service.restore_state(original_checkpoint)
                raise
            finally:
                self.service._checkpoint = checkpoint
            self.final_gross = final_gross
            self.final_active_orders = 0
            return {"backup_file": str(backup), "previous_generation": restored.generation,
                "saved_generation": saved.generation, "orders_submitted": self.driver.submitted_orders,
                "authorized": self.client.is_armed, "strategies_started": False,
                "previous_budget": original_budget.state(), "saved_budget": self.service.budget.state()}
        finally:
            self.client.stop()
            self.driver.stop()

    def abort_preparation(self):
        """装配/策略恢复失败时禁止清理过程覆盖原账户文件。"""
        self._ready = False
        self.service._checkpoint = None

    def poll(self):
        if (not self._ready or not self.client.is_reconciled or self.client.report_errors
                or not self.driver.is_simnow_session or self.driver.trading_day != self.expected_day
                or self.execution.positions.is_recovery_required(self.service.client_id)
                or getattr(self.client, "portfolio_failure", None)):
            raise RuntimeError("共享账户断线、回报冲突或交易日变化，账户闭闸")
        if time.monotonic() >= self._next_check:
            self.client.refresh_account_state()
            if self.position_scope is not None:
                try:
                    gross, buckets = self._observe_positions(external_only=True)
                    self._validate_external(gross, buckets)
                    # 活动单期间权威总仓与回报可能处于不同瞬间；终态后核对完整基线+归属。
                    if self.preserve_external and not self.service._active_instruments():
                        self._validate_account()
                    if any(not self.position_scope.contains(order.instrument_id)
                           for order in self.driver.reconcile_active_orders().orders):
                        raise RuntimeError("共享账户出现范围外活动订单，账户闭闸")
                except Exception:
                    self.client.disarm("external_position_check_failed")
                    self.execution.positions.mark_recovery_required(self.service.client_id)
                    raise
            self._next_check = time.monotonic() + self.reconcile_seconds

    def stop(self):
        if not self._started:
            self.driver.stop()
            return
        self.client.disarm("shared_host_stop")
        if not self._ready:
            # 恢复失败不能保存、撤销未知订单或覆盖原文件。
            self.client.stop()
            self.driver.stop()
            self._started = False
            return
        self.client.set_risk_mode("HALTED", cancel_active_orders=True)
        deadline = time.monotonic() + self.cancel_seconds
        while self.driver.reconcile_active_orders().orders:
            if time.monotonic() > deadline:
                raise RuntimeError("共享账户停机撤单未确认，账户锁保留")
            time.sleep(0.2)
        self.service.poll()
        self._validate_account()
        self.manager.save()
        self.final_gross = occupied_positions(self.transport)
        self.final_active_orders = len(self.driver.reconcile_active_orders().orders)
        self.client.stop()
        self.driver.stop()
        self._started = False
