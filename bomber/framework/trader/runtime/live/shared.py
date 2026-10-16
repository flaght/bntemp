"""同进程多策略宿主；账户生命周期和业务工厂由应用层注入。"""

from dataclasses import dataclass
from pathlib import Path
import time

from bomber.framework.trader.persistence import JsonStateStore
from .local import strategy_state_lock
from .health import readiness_failure_context


def prepare_strategy_history(session):
    """行情已订阅且决策关闭时准备历史；失败不开放实时交易。"""
    runner = session.runner
    prepare = getattr(runner, "prepare_history", None)
    if not callable(prepare):
        return
    if runner.accept_bars:
        raise RuntimeError("历史准备必须在实时决策关闭时执行")
    runner.mark_history_live_start()
    prepare()
    # 查库可能耗时，重新观测参考及远程账户，不能沿用查询前的健康状态。
    session.references.refresh()
    drain = getattr(session.client, "drain_events", None)
    if callable(drain):
        drain()
    poll = getattr(runner, "poll_live_inputs", None)
    if callable(poll):
        poll()
    if runner.failure or not runner.session_ready():
        raise RuntimeError(runner.failure or "历史加载后行情/参考/账户未就绪，保持闭闸")


def history_snapshot(session):
    service = getattr(session.runner, "history_service", None)
    return {"history": service.last_audit if service is not None else None,
            "history_audit": list(service.audit_history) if service is not None else [],
            "complete_frames": getattr(session.strategy, "complete_frames", None),
            "synchronized_frames": getattr(session.strategy, "synchronized_frames", None),
            "history_frames": getattr(session.runner, "history_frame_count", None),
            "live_frames": getattr(session.runner, "live_frame_count", None),
            "bars_used": getattr(session.strategy, "bars_used", None)}


@dataclass
class SharedStrategySession:
    session: object
    state_file: Path
    identity: dict
    active: bool = False
    failure: str | None = None
    generation: int = 0
    saved_revision: int | None = None
    stop_requested_at_ns: int | None = None
    stopped_at_ns: int | None = None
    saved_signal: object = None

    @property
    def strategy_id(self):
        return self.session.strategy.strategy_id

    def restore(self, service, *, resume):
        """只恢复本策略消费版本；归属仓位及冻结订单永远由账户文件恢复。"""
        persisted = JsonStateStore(self.state_file).load()
        bootstrap = getattr(service, "bootstrap_signals", {}).get(self.strategy_id)
        if bool(persisted) != resume and not (resume and persisted is None and bootstrap is not None):
            raise ValueError("策略新会话/恢复与信号检查点存在情况不符")
        source = service.snapshot_state()["sources"].get(self.strategy_id)
        account_revision = source["revision"] if source else 0
        if persisted:
            state = persisted.payload
            if state.get("version") != 1:
                raise ValueError("策略恢复身份或配置不一致")
            if state.get("identity") != self.identity:
                validate = getattr(self.session, "validate_rollover_identity", None)
                if not callable(validate):
                    raise ValueError("策略恢复身份或配置不一致")
                validate(state["identity"], self.identity)
            if state["revision"] > account_revision:
                raise ValueError("策略信号版本领先账户检查点，须对账")
            self.generation = persisted.generation
            self.saved_revision = state["revision"]
            restore = getattr(self.session, "restore_signal", None)
            if callable(restore):
                if "signal" not in state:
                    raise ValueError("08共享检查点缺少选约去重状态")
                restore(state["signal"])
                self.saved_signal = state["signal"]
            if state["identity"] != self.identity:
                self.saved_revision = None
        self.session.strategy.restore_target_revision(account_revision)
        if persisted is None and bootstrap is not None:
            self.session.restore_signal(bootstrap)
        # 指标按各原Runner重新预热。旧账户目标处于暂停状态，不重放历史交易。

    def save(self, service):
        source = service.snapshot_state()["sources"].get(self.strategy_id)
        revision = source["revision"] if source else 0
        checkpoint = getattr(self.session, "signal_checkpoint", None)
        signal = checkpoint.snapshot_state() if checkpoint is not None else None
        if self.saved_revision == revision and self.failure is None and signal == self.saved_signal:
            return
        persisted = JsonStateStore(self.state_file).save({"version": 1, "identity": self.identity,
            "revision": revision,
            "signal_recovery": "rewarm", "failure": self.failure,
            **({"signal": signal} if signal is not None else {})}, expected_generation=self.generation)
        self.generation = persisted.generation
        self.saved_revision = revision
        self.saved_signal = signal


class SharedAccountLiveHost:
    """每个策略保留Runner，执行端口共用账户服务；异常分策略隔离。

    account.start/stop/poll维护物理资源与对账。宿主持有额外账户连接租约，
    避免最后一个策略退出时先断TD而无法确认撤单。构造不连接、不授权。
    """
    def __init__(self, account, service, strategies, *, resources, resume=False, ready_seconds=30):
        self.account, self.service = account, service
        self.strategies = {item.strategy_id: item for item in strategies}
        if len(self.strategies) != len(strategies) or not strategies:
            raise ValueError("共享宿主须声明至少一个不同策略ID")
        if ready_seconds <= 0:
            raise ValueError("策略就绪等待须为正数")
        self.resources, self.resume, self.ready_seconds = resources, resume, ready_seconds
        self._started = self._stopped = False
        self._prepared = False
        self.preparation_check = None

    def _check_preparation(self):
        if self.preparation_check is not None:
            self.preparation_check()

    def start(self):
        if self._stopped:
            raise RuntimeError("已停止的宿主不能重新启动")
        if self._started:
            return
        for item in self.strategies.values():
            self.resources.enter_context(strategy_state_lock(item.state_file))
        try:
            self._check_preparation()
            self.account.start(resume=self.resume)
            self._check_preparation()
            self.service.start()  # 宿主租约；各Runner取得自己的租约。
            self._started = True
            for item in self.strategies.values():
                item.restore(self.service, resume=self.resume)
                item.active = True
                item.session.runner.start()
            deadline = time.monotonic() + self.ready_seconds
            while any(not item.session.runner.session_ready() for item in self.strategies.values()):
                self._check_preparation()
                for item in self.strategies.values():
                    item.session.references.refresh()
                    poll = getattr(item.session.runner, "poll_live_inputs", None)
                    if callable(poll):
                        poll()
                self.account.poll()
                if time.monotonic() > deadline:
                    raise RuntimeError("共享策略未在期限内取得健康行情/参考/账户")
                time.sleep(0.2)
            for item in self.strategies.values():
                self._check_preparation()
                prepare_strategy_history(item.session)
                item.save(self.service)
            self._check_preparation()
            self.account.authorize()
            self._prepared = True
            self._check_preparation()
            for item in self.strategies.values():
                item.session.runner.begin_bars()
        except BaseException:
            abort = getattr(self.account, "abort_preparation", None)
            if not self._prepared and callable(abort):
                abort()
            self.stop()
            raise

    def stop_strategy(self, strategy_id, *, reason="operator"):
        item = self.strategies[strategy_id]
        if not item.active:
            return
        runner, port = item.session.runner, item.session.client
        item.failure = reason
        if item.stop_requested_at_ns is None:
            item.stop_requested_at_ns = time.time_ns()
        # 与正在发布的Bar串行关闭新目标；撤单期间保持策略上下文供回调查仓。
        with runner._submit_lock:
            runner.accept_bars = False
            if self._prepared:
                port.begin_stop()
            else:
                self.service.suspend_strategy(strategy_id)
        if self._prepared:
            deadline = time.monotonic() + getattr(self.account, "cancel_seconds", 15)
            while True:
                self.service.poll()
                port.drain_events()
                if port.finish_stop_events():
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"策略停机撤单或回报未完成，保留绑定及账户锁: {strategy_id}")
                time.sleep(0.05)
            item.save(self.service)
        runner.stop()
        item.active = False
        item.stopped_at_ns = time.time_ns()

    def poll(self):
        if not self._started or self._stopped:
            raise RuntimeError("共享宿主未运行")
        self.account.poll()
        self.service.poll()
        if self.service.execution_stop is not None:
            for item in self.strategies.values():
                if item.active:
                    self.stop_strategy(item.strategy_id, reason="order_budget_exhausted")
            return
        for item in self.strategies.values():
            if not item.active:
                continue
            session = item.session
            try:
                session.client.drain_events()
                if session.runner.failure:
                    raise RuntimeError(session.runner.failure)
                session.references.refresh()
                poll = getattr(session.runner, "poll_live_inputs", None)
                if callable(poll):
                    poll()
                current = getattr(session.runner, "reference_is_current", None)
                valid_reference = current() if callable(current) else session.references.spec == session.runner.fixed_spec
                if not valid_reference:
                    session.runner.failure_readiness = readiness_failure_context(session.runner)
                    raise RuntimeError(f"策略参考合约或条款变化: strategy_id={item.strategy_id}")
                if not session.runner.session_ready():
                    session.runner.failure_readiness = readiness_failure_context(session.runner)
                    raise RuntimeError(f"策略行情或账户未就绪: strategy_id={item.strategy_id}")
                item.save(self.service)
            except Exception as error:
                if self.service.client.report_errors or self.service._failed:
                    raise
                self.stop_strategy(item.strategy_id, reason=str(error))

    def restart_strategy(self, strategy_id, session):
        """应用工厂重建本策略Runner后恢复；不恢复或替换账户/其他策略。"""
        old = self.strategies[strategy_id]
        if (old.active or session.strategy.strategy_id != strategy_id
                or session.client.service is not self.service or strategy_id in self.service._draining):
            raise ValueError("单策略重启身份不符、仍在运行或撤单未确认")
        item = SharedStrategySession(session, old.state_file, old.identity)
        item.restore(self.service, resume=True)
        session.client.ready_check = session.runner.session_ready
        try:
            session.runner.start()
            deadline = time.monotonic() + self.ready_seconds
            while not session.runner.session_ready():
                self.account.poll()
                session.references.refresh()
                poll = getattr(session.runner, "poll_live_inputs", None)
                if callable(poll):
                    poll()
                if time.monotonic() > deadline:
                    raise RuntimeError("重启策略未在期限内就绪")
                time.sleep(0.2)
            prepare_strategy_history(session)
            session.runner.begin_bars()
            item.active = True
            self.strategies[strategy_id] = item
        except Exception:
            session.runner.stop()
            raise

    def stop(self):
        if self._stopped:
            return
        errors = []
        for item in self.strategies.values():
            if item.active:
                try:
                    self.stop_strategy(item.strategy_id, reason="host_stop")
                except Exception as error:
                    errors.append(error)
        try:
            self.account.stop()  # 撤单确认、保存、断TD；失败保留锁。
        except Exception as error:
            errors.append(error)
        if errors:
            raise RuntimeError("共享宿主停机未完成，资源锁保留") from errors[0]
        self._started = False
        self._stopped = True
        self.resources.close()

    def snapshot(self):
        positions = self.service.position_manager.snapshot()
        client = self.service.client
        return {"account": self.service.snapshot_state(),
            "execution_diagnostics": {"is_reconciled": client.is_reconciled,
                "is_armed": getattr(client, "is_armed", None),
                "recovery_required": self.service.position_manager.is_recovery_required(self.service.client_id),
                "service_failed": self.service._failed,
                "report_errors": [str(error) for error in client.report_errors],
                "portfolio_failure": getattr(client, "portfolio_failure", None),
                "audit": [{"action": row.action, "detail": row.detail, "ts_ns": row.ts_ns}
                    for row in getattr(client, "audit_records", ())[-20:]]},
            "strategy_positions": [{"strategy_id": sid, "target_key": key, "quantity": str(q)}
                for (sid, key), q in positions.strategy_positions.items()],
            "execution_positions": {str(key.instrument_id): str(q) for key, q in positions.account_positions.items()},
            "strategies": {sid: {"active": item.active, "failure": item.failure,
                **history_snapshot(item.session),
                "failure_readiness": getattr(item.session.runner, "failure_readiness", None),
                "fills_received": getattr(item.session.strategy, "fills_received", 0),
                "execution_events": item.session.client.event_diagnostics(),
                "stop_requested_at_ns": item.stop_requested_at_ns,
                "stopped_at_ns": item.stopped_at_ns,
                "state_file": str(item.state_file)} for sid, item in self.strategies.items()}}
