"""独立策略执行的Live生命周期；不登录、查询或锁定任何物理账户。"""

from contextlib import contextmanager
from .contracts import ChannelIdentity, LiveRunContext, ReadinessResult
from .recovery import validate_finished_orders, validate_owned_positions, restored_target_revision
from .lifecycle import prepare_live_session, activate_live_session, shutdown_local_session, validate_session_checkpoint


@contextmanager
def strategy_state_lock(state_file):
    """仅保护策略状态文件；不意味着占用或关联物理交易账户。"""
    import fcntl
    import hashlib
    from pathlib import Path
    key = hashlib.sha256(str(Path(state_file).resolve()).encode()).hexdigest()
    with open(f"/tmp/bomber-strategy-state-{key}.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("本策略状态文件已有进程使用") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class LocalExecutionChannel:
    def __init__(self, client_id, ready, *, channel="model"):
        self.identity = ChannelIdentity("execution", channel, client_id)
        self._ready = ready

    def check(self):
        from .contracts import ReadinessIssue
        issues = () if self._ready() else (ReadinessIssue(self.identity, "EXECUTION_NOT_READY", "策略执行未就绪"),)
        return ReadinessResult((self.identity,), issues)


class LocalExecutionLifecycle:
    mode = "model"
    event_label = "模型执行回报"

    def __init__(self, trading_day, *, resume=False, ready_seconds=30, state_file=None):
        self.trading_day = trading_day
        self.resuming = resume
        self.ready_seconds = ready_seconds
        self.resume_ready = False
        self.restored_generation = None
        self.driver = None
        self.state_file = state_file

    def prepare(self, resources):
        if self.state_file is not None:
            resources.enter_context(strategy_state_lock(self.state_file))
        return LiveRunContext(self.trading_day, resources)

    def finish_preparation(self, context):
        pass

    def restore_session(self, session, context):
        return session.manager.restore()

    def validate_restored_session(self, session, day):
        validate_finished_orders(session)
        owned = validate_owned_positions(session.runner.position_manager, session.recovery_binding,
            account_scope=getattr(session, "position_scope", None))
        position_client = getattr(session.client, "position_client", session.client)
        risk = position_client.risk_manager
        for instrument, quantity in owned.items():
            limits = risk.instrument_limits[instrument]
            if limits.max_abs_position is not None and abs(quantity) > limits.max_abs_position:
                raise RuntimeError("恢复策略仓位超过配置上限")
        revision = restored_target_revision(session, session.recovery_binding, allow_empty=True)
        session.strategy.restore_target_revision(revision)
        hold = getattr(session.runner, "hold_restored_target", None)
        if callable(hold):
            hold(session.strategy.strategy_id, revision)
        return revision

    def start(self, session, context):
        session.runner.accept_bars = False
        validate_session_checkpoint(session)
        if self.resuming:
            summary = self.restore_session(session, context)
            if summary is None:
                raise RuntimeError("策略恢复文件不存在")
            self.restored_generation = summary.generation
            self.validate_restored_session(session, context.trading_day)
        if not prepare_live_session(session, self.ready_seconds):
            return
        if activate_live_session(self, session, restored=self.resuming):
            print(f"策略会话就绪: execution={self.mode}；停机保留策略仓位", flush=True)

    def _execution_failed(self, session):
        return not session.client.is_started if self.mode == "recording" else bool(session.client.report_errors)

    def poll(self, session):
        if getattr(session.runner, "failure", None) or self._execution_failed(session):
            raise RuntimeError("独立策略执行或行情发生冲突")
        if not session.runner.session_ready():
            raise RuntimeError("独立策略行情交易日、健康或接收心跳失效")
        session.references.refresh()
        reference_check = getattr(session.runner, "reference_is_current", None)
        current = reference_check() if callable(reference_check) else session.references.spec == session.runner.fixed_spec
        if not current:
            session.runner.close_role_gate("独立策略参考合约或版本变化")
        stale = session.runner.stale_instruments()
        if stale:
            session.runner.close_role_gate(f"独立策略行情失效: {stale}")
        return True

    def verify(self, session):
        if getattr(session.runner, "failure", None):
            raise RuntimeError(session.runner.failure)
        if session.client.report_errors or not any(getattr(r.report_type, "value", r.report_type) == "FILLED"
                                                 for r in session.client.backend.reports):
            raise RuntimeError("未取得策略模型成交，检查行情和信号预热")

    def events(self, session):
        if session is None:
            return ()
        return session.client.requests if self.mode == "recording" else session.client.backend.reports

    def shutdown(self, session, context):
        self._shutdown_observed_md_day = (None if session is None else
            getattr(getattr(session, "upstream", None), "latest_trading_day", None))
        preserved = self.resuming and not self.resume_ready
        result = shutdown_local_session(session,
            save_state=self.mode != "recording" and self.resume_ready, restored=self.resuming)
        result["resume_state_preserved"] = preserved
        if session is not None and self.mode != "recording":
            result["model_reports"] = len(session.client.backend.reports)
        return result

    def snapshot(self, session, context):
        recording = self.mode == "recording"
        return {"strategy_position_source": "none" if recording else "model", "account_reconciliation": "off",
            "execution_origin": self.mode, "model": None if recording else "reference-price",
            "trading_day": self.trading_day, "observed_td_trading_day": None,
            "observed_md_trading_day": getattr(self, "_shutdown_observed_md_day",
                None if session is None else getattr(session.upstream, "latest_trading_day", None)),
            "resume_ready": self.resume_ready, "restored_generation": self.restored_generation,
            "account_dispatch": {} if session is None else getattr(session.client, "dispatch_status", {})}


class RecordingExecutionLifecycle(LocalExecutionLifecycle):
    """记录标准目标；独立于行情厂商，不创建任何TD或模型成交。"""
    event_label = "Recording目标"

    mode = "recording"

    def verify(self, session):
        if session.runner.failure or not session.client.requests:
            raise RuntimeError(session.runner.failure or "没有取得策略目标，检查行情与窗口预热")
