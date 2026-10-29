"""08自身的会话恢复与验收；通用账户、订单、归属检查使用公共模块。"""
import time

from bomber.framework.trader.runtime.live.local import LocalExecutionLifecycle
from bomber.framework.trader.runtime.live.recovery import restored_target_revision
from bomber.framework.trader.runtime.live.channels.ctp import (CtpSessionLifecycle,
    occupied_positions)
from bomber.framework.trader.runtime.live.channels.ctp_recovery import (
    CtpResumeLifecycle, validate_ctp_restored_positions)


def validate_restored_session(session, day):
    validate_ctp_restored_positions(session, day, session.recovery_binding)
    revision = restored_target_revision(session, session.recovery_binding, allow_empty=True)
    session.strategy.restore_target_revision(revision)
    return revision


class OptionControl:
    def check_frames(self, session):
        if session.runner.failure or getattr(session.client, "report_errors", ()):
            raise RuntimeError(session.runner.failure or "执行回报冲突")
        session.references.refresh()
        session.runner.poll_features()
        if not session.runner.session_ready():
            raise RuntimeError(f"期权会话未就绪: {session.readiness.check().as_dict()}")
        stale = session.runner.stale_instruments()
        if stale:
            session.runner.close_role_gate(f"必需行情失效: {stale}")
        session.runner.flush_frames()

    def verify(self, session):
        if session.runner.failure or getattr(session.client, "report_errors", ()):
            raise RuntimeError(session.runner.failure or "执行回报冲突")
        if session.strategy.frames == 0:
            raise RuntimeError("没有取得封闭分钟帧")
        if self.require_fills and session.strategy.fills_received == 0:
            raise RuntimeError("本次未取得归属成交，不能通过成交验收")

    def snapshot(self, session, context):
        return {"resume_ready": self.resume_ready,
            "restored_generation": self.restored_generation,
            "bar_time_basis": "replay_test_mapped" if getattr(getattr(session, "bar_feed", None), "time_map", None) else "source_event",
            "valuation_day": self.valuation_day,
            "request_time_basis": "receive_wall_clock", "reference_time_basis": "observation_wall_clock",
            "trading_day": context.trading_day if context else None,
            "observed_td_trading_day": getattr(self, "_observed_td_day", None),
            "observed_md_trading_day": getattr(self, "_observed_md_day", None),
            "reference_manifest": {} if context is None or context.references is None else context.references.manifest,
            "orders_submitted": getattr(getattr(self, "driver", None), "submitted_orders", 0),
            "acceptance": "attributed_fills" if self.require_fills else "market_frames",
            "positions_retained_on_stop": True}


class OptionLocalLifecycle(OptionControl, LocalExecutionLifecycle):
    def __init__(self, trading_day, *, mode, valuation_day, resume=False, state_file=None,
                 ready_seconds=90, require_fills=False):
        super().__init__(trading_day, resume=resume, state_file=state_file, ready_seconds=ready_seconds)
        self.mode, self.valuation_day, self.require_fills = mode, valuation_day, require_fills
        self._observed_md_day = None

    @property
    def event_label(self):
        return "Recording目标" if self.mode == "recording" else "模型执行回报"

    def poll(self, session):
        self.check_frames(session)
        return True

    def shutdown(self, session, context):
        if session is not None:
            session.strategy.stopping = True
            self._observed_md_day = getattr(session.upstream, "latest_trading_day", None)
        return super().shutdown(session, context)


class OptionCtpLifecycle(OptionControl, CtpResumeLifecycle, CtpSessionLifecycle):
    def __init__(self, *args, valuation_day, state_file, require_fills=False, **kwargs):
        super().__init__(*args, state_file=state_file, **kwargs)
        self.valuation_day, self.require_fills = valuation_day, require_fills
        self.bar_time_basis = "source_event"

    def validate_restored_session(self, session, day):
        return validate_restored_session(session, day)

    def restore_session(self, session, context):
        saved = session.manager.repository.load()
        if saved is None:
            return session.manager.restore()
        old = saved.payload["component_states"]["session_identity"]["identity"]
        if old["td_day"] == context.trading_day:
            return session.manager.restore()
        from .rollover import payload_for_day
        return session.manager.restore(payload_transform=lambda payload: payload_for_day(session, payload,
            driver=self.driver, transport=self.transport, day=context.trading_day))

    def poll(self, session):
        if self.lost:
            raise RuntimeError(self.lost[-1])
        self.check_frames(session)
        if getattr(session.client, "portfolio_failure", None):
            raise RuntimeError(session.client.portfolio_failure)
        if self.driver.submitted_orders >= self.max_session_orders:
            return False  # 公共停机流程撤单、核验与保存，保留已成交仓位。
        if time.monotonic() >= self._next_check:
            session.client.refresh_account_state()
            if self.account_reconciliation != "off":
                gross = self._managed_gross(occupied_positions(self.transport))
                expected = self._expected_gross(session)
                self.position_differences = {key: {"observed": gross.get(key, (0, 0)),
                    "execution": expected.get(key, (0, 0))} for key in gross.keys() | expected.keys()
                    if gross.get(key, (0, 0)) != expected.get(key, (0, 0))}
                if self.position_differences and self.account_reconciliation == "strict":
                    raise RuntimeError(f"柜台与策略成交账本不一致: {self.position_differences}")
            self._next_check = time.monotonic() + self.reconcile_seconds
        return True

    @staticmethod
    def _expected_gross(session):
        result = {}
        for instrument in session.position_scope.instruments:
            from bomber.framework.market.basic.base import InstrumentId
            snap = session.ledger.snapshot(InstrumentId.from_str(instrument))
            if snap.long_total or snap.short_total:
                result[instrument] = (snap.long_total, snap.short_total)
        return result

    def shutdown(self, session, context):
        if session is not None:
            session.strategy.stopping = True
        return super().shutdown(session, context)
