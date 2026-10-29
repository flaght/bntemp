"""跨期固定会话的恢复与验收；复用公共成交账本及CTP核验。"""

from bomber.framework.trader.runtime.live.channels.ctp import CtpSessionLifecycle
from bomber.framework.trader.runtime.live.channels.ctp_market import CtpMarketSessionLifecycle
from bomber.framework.trader.runtime.live.channels.ctp_recovery import (
    CtpResumeLifecycle, validate_ctp_restored_positions)
from bomber.framework.trader.runtime.live.recovery import restored_target_revision
from .live_runtime import restore_direction


class CalendarControl:
    require_fills = False

    def verify(self, session):
        runner, strategy = session.runner, session.strategy
        failure = runner.failure or getattr(session.client, "portfolio_failure", None)
        if failure or getattr(session.client, "report_errors", ()):
            raise RuntimeError(failure or "执行回报冲突")
        if strategy.complete_frames < strategy.config.lookback + 1:
            raise RuntimeError(f"双腿同步预热不足: actual={strategy.complete_frames} "
                f"required={strategy.config.lookback + 1}")
        if self.resuming and strategy._force_target:
            raise RuntimeError("恢复后尚未发布重新预热的有效目标")
        if self.require_fills and not strategy.fills_received:
            raise RuntimeError("本次没有归属成交，不能通过成交验收")
        if self.require_fills:
            targets = strategy.last_targets
            binding = session.recovery_binding
            if targets is None or set(targets) != set(binding.targets):
                raise RuntimeError("成交验收缺少完整双腿最新目标")
            positions = runner.position_manager
            for key, instrument in binding.targets.items():
                actual = positions.position(strategy.strategy_id, key)
                working = positions.working_quantity(binding.client_id, instrument)
                unassigned = positions.unassigned_position(binding.client_id, instrument)
                if actual != targets[key] or working != 0 or unassigned != 0:
                    raise RuntimeError(f"双腿目标未完成: {key} target={targets[key]} "
                        f"position={actual} working={working} unassigned={unassigned}")

    def snapshot(self, session, context):
        fields = super().snapshot(session, context)
        fields.pop("fixed_instrument", None)
        fields.pop("factor_date", None)
        fields.pop("factor_date_basis", None)
        return {**fields, "acceptance": "attributed_fills" if self.require_fills else "synchronized_frames",
            "reference_manifest": context.references.manifest if context and context.references else {},
            "bar_time_basis": self.profile.bar_time_basis,
            "reference_time_basis": "current_reference_day_and_wall_clock",
            "positions_retained_on_stop": True,
            "fixed_instruments": {} if session is None else {
                r: str(i) for r, i in session.runner.instruments.items()},
            "pending_frames": [] if session is None else [{"bar_ns": stamp,
                "received": sorted(str(i) for i in frame),
                "missing": sorted(str(i) for i in session.runner.fixed_ids - set(frame))}
                for stamp, frame in sorted(session.runner.pending_frames.items())],
            "expired_frame_count": 0 if session is None else session.runner.expired_frame_count}


class CalendarLocalLifecycle(CalendarControl, CtpMarketSessionLifecycle):
    def __init__(self, *, mode, require_fills=False, **kwargs):
        super().__init__(**kwargs)
        self.mode, self.require_fills = mode, require_fills

    @property
    def event_label(self):
        return "Recording目标" if self.mode == "recording" else "模型执行回报"

    def validate_restored_session(self, session, day):
        revision = super().validate_restored_session(session, day)
        positions = session.runner.position_manager
        if any(abs(positions.position(session.strategy.strategy_id, key)) > session.strategy.config.quantity
                for key in session.recovery_binding.targets):
            raise RuntimeError("恢复仓位超过策略手数上限")
        restore_direction(session)
        return revision

    def poll(self, session):
        if session.runner.failure or getattr(session.client, "report_errors", ()):
            raise RuntimeError(session.runner.failure or "执行回报冲突")
        session.references.refresh()
        session.runner._check()
        return True


class CalendarCtpLifecycle(CalendarControl, CtpResumeLifecycle, CtpSessionLifecycle):
    def __init__(self, *args, state_file, require_fills=False, **kwargs):
        super().__init__(*args, state_file=state_file, **kwargs)
        self.require_fills = require_fills

    def validate_restored_session(self, session, day):
        validate_ctp_restored_positions(session, day, session.recovery_binding)
        revision = restored_target_revision(session, session.recovery_binding, allow_empty=True)
        restore_direction(session)
        session.strategy.restore_target_revision(revision)
        return revision

    def poll(self, session):
        if getattr(session.client, "portfolio_failure", None):
            session.runner.close_role_gate(session.client.portfolio_failure)
        return super().poll(session)

    @staticmethod
    def _expected_gross(session):
        result = {}
        for instrument in session.runner.fixed_ids:
            state = session.ledger.snapshot(instrument)
            if state.long_total or state.short_total:
                result[str(instrument)] = (state.long_total, state.short_total)
        return result
