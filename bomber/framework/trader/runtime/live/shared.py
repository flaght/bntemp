"""同进程多策略宿主；账户生命周期和业务工厂由应用层注入。"""

from dataclasses import dataclass
from pathlib import Path
import time

from bomber.framework.trader.persistence import JsonStateStore
from .local import strategy_state_lock


@dataclass
class SharedStrategySession:
    session: object
    state_file: Path
    identity: dict
    active: bool = False
    failure: str | None = None
    generation: int = 0
    saved_revision: int | None = None

    @property
    def strategy_id(self):
        return self.session.strategy.strategy_id

    def restore(self, service, *, resume):
        """只恢复本策略消费版本；归属仓位及冻结订单永远由账户文件恢复。"""
        persisted = JsonStateStore(self.state_file).load()
        if bool(persisted) != resume:
            raise ValueError("策略新会话/恢复与信号检查点存在情况不符")
        source = service.snapshot_state()["sources"].get(self.strategy_id)
        account_revision = source["revision"] if source else 0
        if persisted:
            state = persisted.payload
            if state.get("version") != 1 or state.get("identity") != self.identity:
                raise ValueError("策略恢复身份或配置不一致")
            if state["revision"] > account_revision:
                raise ValueError("策略信号版本领先账户检查点，须对账")
            self.generation = persisted.generation
            self.saved_revision = state["revision"]
        self.session.strategy.restore_target_revision(account_revision)
        # 指标按各原Runner重新预热。旧账户目标处于暂停状态，不重放历史交易。

    def save(self, service):
        source = service.snapshot_state()["sources"].get(self.strategy_id)
        revision = source["revision"] if source else 0
        if self.saved_revision == revision and self.failure is None:
            return
        persisted = JsonStateStore(self.state_file).save({"version": 1, "identity": self.identity,
            "revision": revision,
            "signal_recovery": "rewarm", "failure": self.failure}, expected_generation=self.generation)
        self.generation = persisted.generation
        self.saved_revision = revision


class SharedAccountLiveHost:
    """每个策略保留Runner，执行端口共用账户服务；异常分策略隔离。

    account.start/stop/poll维护物理资源与对账。宿主持有额外账户连接租约，
    避免最后一个策略退出时先断TD而无法确认撤单。构造不连接、不授权。
    """
    def __init__(self, account, service, strategies, *, resources, resume=False, ready_seconds=30):
        self.account, self.service = account, service
        self.strategies = {item.strategy_id: item for item in strategies}
        if len(self.strategies) != len(strategies) or len(strategies) < 2:
            raise ValueError("共享宿主须声明至少两个不同策略ID")
        if ready_seconds <= 0:
            raise ValueError("策略就绪等待须为正数")
        self.resources, self.resume, self.ready_seconds = resources, resume, ready_seconds
        self._started = self._stopped = False
        self._prepared = False

    def start(self):
        if self._stopped:
            raise RuntimeError("已停止的宿主不能重新启动")
        if self._started:
            return
        for item in self.strategies.values():
            self.resources.enter_context(strategy_state_lock(item.state_file))
        try:
            self.account.start(resume=self.resume)
            self.service.start()  # 宿主租约；各Runner取得自己的租约。
            self._started = True
            for item in self.strategies.values():
                item.restore(self.service, resume=self.resume)
                item.active = True
                item.session.runner.start()
            deadline = time.monotonic() + self.ready_seconds
            while any(not item.session.runner.session_ready() for item in self.strategies.values()):
                for item in self.strategies.values():
                    item.session.references.refresh()
                self.account.poll()
                if time.monotonic() > deadline:
                    raise RuntimeError("共享策略未在期限内取得健康行情/参考/账户")
                time.sleep(0.2)
            for item in self.strategies.values():
                item.save(self.service)
            self.account.authorize()
            self._prepared = True
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
        item.session.runner.accept_bars = False
        item.failure = reason
        # 先暂停源贡献，再关闭本策略行情和租约，其他Runner仍运行。
        self.service.suspend_strategy(strategy_id)
        if self._prepared:
            item.save(self.service)
        item.session.runner.stop()
        item.active = False

    def poll(self):
        if not self._started or self._stopped:
            raise RuntimeError("共享宿主未运行")
        self.account.poll()
        self.service.poll()
        for item in self.strategies.values():
            if not item.active:
                continue
            session = item.session
            try:
                session.client.drain_events()
                if session.runner.failure:
                    raise RuntimeError(session.runner.failure)
                session.references.refresh()
                current = getattr(session.runner, "reference_is_current", None)
                valid_reference = current() if callable(current) else session.references.spec == session.runner.fixed_spec
                if not valid_reference:
                    raise RuntimeError(f"策略参考合约或条款变化: strategy_id={item.strategy_id}")
                if not session.runner.session_ready():
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
                if time.monotonic() > deadline:
                    raise RuntimeError("重启策略未在期限内就绪")
                time.sleep(0.2)
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
                "fills_received": getattr(item.session.strategy, "fills_received", 0),
                "state_file": str(item.state_file)} for sid, item in self.strategies.items()}}
