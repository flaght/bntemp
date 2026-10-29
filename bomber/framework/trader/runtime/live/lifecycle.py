"""Live 会话准备、检查点提交与本地收尾；账户规则仍由通道生命周期负责。"""
from contextlib import nullcontext

from .readiness import wait_for_session_ready
from .recovery import restored_target_revision


def close_session_gate(session):
    if session is not None:
        with getattr(session.runner, "_submit_lock", nullcontext()):
            session.runner.accept_bars = False


def pause_requested(session):
    window = getattr(session.runner, "window_context", None)
    if window is not None and window.stop_requested():
        window.pause_runner(session.runner, session.client)
        return True
    return False


def validate_session_ready(session):
    runner = session.runner
    current = getattr(runner, "reference_is_current", None)
    if callable(current):
        reference_ok = current()
    elif hasattr(runner, "fixed_spec"):
        reference_ok = session.references.spec == runner.fixed_spec
    else:
        reference_ok = True
    if getattr(runner, "failure", None) or not reference_ok or not runner.session_ready():
        raise RuntimeError(getattr(runner, "failure", None) or "会话准备后参考、行情或执行失效，保持闭闸")


def prepare_strategy_history(session):
    """订阅后且决策关闭时准备历史，结束后重新观测参考和执行状态。"""
    runner = session.runner
    prepare = getattr(runner, "prepare_history", None)
    if not callable(prepare):
        return True
    if runner.accept_bars:
        raise RuntimeError("历史准备必须在实时决策关闭时执行")
    if pause_requested(session):
        return False
    runner.mark_history_live_start()
    if prepare() is False:
        return False
    if pause_requested(session):
        return False
    session.references.refresh()
    drain = getattr(session.client, "drain_events", None)
    if callable(drain):
        drain()
    poll = getattr(runner, "poll_live_inputs", None)
    if callable(poll):
        poll()
    validate_session_ready(session)
    return True


def prepare_live_session(session, ready_seconds, *, lost=None):
    """统一启动后的就绪和历史准备；业务恢复在调用前完成。"""
    close_session_gate(session)
    validate_session_checkpoint(session)
    if pause_requested(session):
        return False
    session.runner.start()
    ready = wait_for_session_ready(session, ready_seconds, lost=lost,
        cancelled=lambda: pause_requested(session))
    if ready is False:
        return False
    validate_session_ready(session)
    return prepare_strategy_history(session)


def commit_session_checkpoint(session, *, restored=False):
    """只在稳定阶段核对业务目标并提交；暂存恢复沿用原 commit_restore。"""
    manager = getattr(session, "manager", None)
    if manager is None:
        return None
    with getattr(session.runner, "_submit_lock", nullcontext()):
        binding = getattr(session, "recovery_binding", None)
        if binding is not None:
            restored_target_revision(session, binding, allow_empty=True)
        validate = getattr(manager, "validate_components", None)
        if callable(validate):
            validate()
        commit = getattr(manager, "commit_restore", manager.save) if restored else manager.save
        return commit()


def validate_session_checkpoint(session):
    manager = getattr(session, "manager", None)
    validate = getattr(manager, "validate_binding", None)
    if callable(validate):
        runner = session.runner
        validate(runner.target_store, runner.portfolio_coordinator, runner.position_manager,
            client_id=session.client.client_id,
            order_machine=getattr(session.client, "order_state_machine", None))


def activate_live_session(controller, session, *, restored=False, authorize=None):
    """检查点成功后才允许授权和决策；停止信号在各边界重新核对。"""
    with getattr(session.runner, "_submit_lock", nullcontext()):
        session.runner.accept_bars = False
        if pause_requested(session):
            return False
        validate_session_ready(session)
        commit_session_checkpoint(session, restored=restored)
        controller.resume_ready = True
        if pause_requested(session):
            return False
        if authorize is not None:
            authorize()
        if pause_requested(session):
            return False
        session.runner.begin_bars()
        return True


def shutdown_local_session(session, *, save_state=False, restored=False):
    """先关决策，再保存，最后始终释放 Runner；Recording 无需 backend。"""
    errors = []
    if session is not None:
        close_session_gate(session)
        try:
            if save_state:
                commit_session_checkpoint(session, restored=restored)
        except Exception as error:
            errors.append(str(error))
        finally:
            try:
                session.runner.stop()
            except Exception as error:
                errors.append(str(error))
    return {"cleanup_errors": errors, "final_gross": None, "final_active_orders": None}
