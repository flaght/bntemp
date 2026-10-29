"""EMA 单次及精确断点恢复；连接和常驻调度沿用公共 Live 组件。"""

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import time
from bomber.framework.trader.runtime.ctp_account import position_buckets
from bomber.framework.trader.execution.ctp.ledger import CtpLedgerState, CtpPositionSnapshot
from bomber.framework.trader.runtime.live.channels.ctp import CtpSessionLifecycle, account_lock, occupied_positions
from bomber.framework.trader.runtime.live.channels.ctp_recovery import CtpResumeLifecycle
from bomber.framework.trader.runtime.live.contracts import LiveRunContext
from bomber.framework.trader.runtime.live.lifecycle import prepare_live_session, activate_live_session, validate_session_checkpoint
from bomber.framework.trader.runtime.live.recovery import restored_target_revision
from bomber.framework.trader.runtime.live.channels.ctp_recovery import (
    validate_ctp_account, validate_ctp_restored_positions)
from bomber.framework.trader.runtime.trading_sessions import SHANGHAI
from .history import recover_minutes, warm_closed_bar


def recovery_identity(args, references, policy):
    from bomber.framework.market.sources import source_config
    assignment = references.snapshot(time.time_ns())
    product = args.product.strip().upper()
    result = {
        "strategy_id": "main-ema-simnow", "client_id": "main-ema-simnow",
        "product": product, "instrument": str(references.instrument_id),
        "target_key": f"{product.lower()}_main",
        "trading_day": references.trading_day.strftime("%Y%m%d"),
        "simnow_environment": policy.environment,
        "replay_md_trading_day": policy.replay_md_trading_day,
        "source_day": str(assignment.source_day), "factor_date": str(references.factor_date),
        "factor_basis": references.factor_date_basis,
        "factor": str(assignment.factor(product, "main")),
        "tick": str(references.spec.tick), "multiplier": str(references.spec.multiplier),
        "currency": references.spec.currency, "venue": references.spec.venue,
        "listed": str(references.spec.listed), "last_trade": str(references.spec.last_trade),
        "fast": args.fast, "slow": args.slow, "quantity": str(args.quantity),
    }
    from bomber.framework.trader.execution.policy import ExecutionPolicy, execution_policy_from_args
    execution = execution_policy_from_args(args)
    if execution != ExecutionPolicy():
        result["execution_policy"] = execution.snapshot_state()
    if source_config(args).source != "ctp":
        result["market_config"] = source_config(args).identity
    return result


def validate_restored_session(session, day):
    binding = session.recovery_binding
    validate_ctp_restored_positions(session, day, binding)
    revision = restored_target_revision(session, binding, allow_empty=True)
    owned = session.runner.position_manager.position(binding.strategy_id, session.strategy.config.target_key)
    if abs(owned) > session.strategy.config.quantity:
        raise RuntimeError("恢复仓位超过本策略目标手数上限")
    # 恢复执行事实；本次重新计算EMA，首个新目标版本接在原版本后面。
    with session.strategy._revision_lock:
        session.strategy._revision = revision
    session.runner.hold_restored_target(binding.strategy_id, revision)
    return revision


def verify_managed_position(session, transport):
    strategy = session.strategy
    if (strategy.bars_used < strategy.config.slow_period or strategy.last_target is None
            or session.runner.failure or session.client.report_errors):
        raise RuntimeError("接管/恢复后尚未取得有效新EMA目标或存在会话错误")
    validate_ctp_account(session, transport, session.recovery_binding)
    instrument = session.references.instrument_id
    if (strategy.position(strategy.config.target_key) != strategy.last_target or
            session.runner.position_manager.working_quantity(session.client.client_id, instrument)):
        raise RuntimeError("接管/恢复后最新EMA目标尚未完成")


class EmaCheckpointLifecycle(CtpResumeLifecycle, CtpSessionLifecycle):
    def __init__(self, *args, state_file, window_context, **kwargs):
        super().__init__(*args, resume=Path(state_file).exists(), state_file=state_file, **kwargs)
        self.window_context = window_context
        self.window, self.stop_event = window_context.window, window_context.stop_event

    def prepare(self, resources):
        if not self.resuming:
            return super().prepare(resources)
        self.lock_state(resources)
        for namespace in dict.fromkeys((self.lock_namespace, *self.legacy_lock_namespaces)):
            resources.enter_context(account_lock(self.transport.account_id, namespace=namespace))
        self.driver.start(lambda report: None)
        self._observed_td_day = self.driver.trading_day
        if self.driver.trading_day != self.window.trading_day:
            raise RuntimeError("登录TD交易日与当前日夜盘窗口不一致")
        self.driver.reconcile_account_state()
        # 此时仅查询；关联、策略归属在最终会话restore/start后校验。
        return LiveRunContext(self.driver.trading_day, resources)

    def start(self, session, context):
        if not self.resuming:
            super().start(session, context)
            self.resume_ready = True
            return
        self.session = session
        validate_session_checkpoint(session)
        summary = session.manager.restore(legacy_component_states=getattr(session, "legacy_component_states", None))
        if summary is None:
            raise RuntimeError("EMA原检查点不存在，不能作为空仓启动")
        self.restored_generation = summary.generation
        session.client.start()  # 先恢复活动订单，再查询账户净仓和资金；不继承旧授权。
        self._refresh_ledger(session, context.trading_day)
        if not prepare_live_session(session, self.ready_seconds, lost=lambda: bool(self.lost)):
            return
        if getattr(session.runner, "history_service", None) is None:
            recover_minutes(session.runner.ema_checkpoint, session.runner.calendar,
                session.runner._clock_ns(), session.runner.recovery_bars)
        if activate_live_session(self, session, restored=True,
                authorize=lambda: session.client.arm_demo(session.client.DEMO_CONFIRMATION)):
            self._next_check = time.monotonic() + self.reconcile_seconds
            print(f"连续会话已恢复：TD={context.trading_day} EMA bars={session.strategy.bars_used} "
                  f"策略仓位={session.strategy.position(session.strategy.config.target_key)}", flush=True)

    def _refresh_ledger(self, session, day):
        instrument = session.references.instrument_id
        net = session.runner.position_manager.account_position(session.client.client_id, instrument)
        owned = session.runner.position_manager.position(session.strategy.strategy_id, session.strategy.config.target_key)
        if net != owned or session.runner.position_manager.unassigned_position(session.client.client_id, instrument):
            raise RuntimeError("恢复时柜台净仓与策略归属不一致，不能自动续跑")
        gross = occupied_positions(self.transport)
        expected = {} if not net else {str(instrument): (max(net, 0), max(-net, 0))}
        if gross != expected or abs(net) > 1:
            raise RuntimeError("恢复时全账户总仓与已知单策略一手仓不一致")
        buckets = position_buckets(self.transport.query_position_details(), (instrument,),
            require_cost=True, unique_buckets=True)
        bucket = buckets.get(str(instrument))
        values = bucket.quantities if bucket else (Decimal(0),) * 4
        bases = tuple(cost / (qty * session.references.spec.multiplier) if qty else Decimal(0)
                      for qty, cost in zip(values, bucket.costs if bucket else (Decimal(0),) * 4))
        if (values[0] + values[1], values[2] + values[3]) != expected.get(str(instrument), (0, 0)):
            raise RuntimeError("权威总仓与今昨明细不一致")
        snap = CtpPositionSnapshot(instrument, *values, *bases)
        session.ledger.restore(CtpLedgerState(day, {instrument: snap} if net else {}))

    def verify(self, session):
        # 常驻的计划休市/停止不要求每个窗口都有成交；不复用验收的强制成交标准。
        if session.client.report_errors or session.runner.failure:
            raise RuntimeError("连续会话存在未解决的执行/策略错误")

    def shutdown(self, session, context):
        error = None
        try:
            self._save_close_bar(session)
        except Exception as failure:
            error = str(failure)
        result = super().shutdown(session, context)
        if error:
            result["cleanup_errors"].append(error)
        return result

    def _save_close_bar(self, session):
        context = getattr(self, "window_context", None)
        now = context.now() if context is not None else datetime.now(SHANGHAI)
        if session and now >= self.window.end and not session.runner.failure:
            # 收盘墙钟已越过完整分钟；仅预热最后Bar，休市不发布目标。
            session.runner.accept_bars = False
            session.client.disarm("scheduled_close")
            # 聚合线程持Feed锁再调用Runner；先取出Bar，再获取Runner/客户端锁。
            bars = session.bar_feed.drain_completed(int(self.window.end.timestamp() * 1e9))
            with session.runner._submit_lock, session.client._submit_lock:
                for bar in bars:
                    warm_closed_bar(session.runner, bar)


def legacy_ema_state(report_path, checkpoint, persisted, state_file):
    """仅迁移已通过的首次预热窗口；不从旧目标猜测后来EMA变化。"""
    report = json.loads(Path(report_path).read_text())
    targets = persisted.payload["targets"]
    if len(targets) != 1:
        raise ValueError("旧状态须为单EMA目标")
    target, config = targets[0], checkpoint.config()
    meta = target["metadata"]
    if (report.get("status") != "passed" or report.get("signal_source") != "ema"
            or report.get("fixed_instrument") != checkpoint.instrument
            or Path(report["state_file"]).resolve() != Path(state_file).resolve()
            or report.get("initial_position_mode") != "fresh_flat"
            or report["bars_used"] != config["slow"] or report["ema"]["fast"] != config["fast"]
            or report["ema"]["slow"] != config["slow"] or report["ema"]["quantity"] != config["quantity"]
            or str(report["last_target"]) != target["targets"][config["target_key"]]
            or float(report["fast_ema"]) != float(meta["fast_ema"])
            or float(report["slow_ema"]) != float(meta["slow_ema"])
            or report["final_active_orders"] != 0 or report["cleanup_errors"]
            or report["last_order_status"] != "FILLED"):
        raise ValueError("旧报告不能证明准确EMA断点；须使用完整EMA检查点或历史Bar重建")
    orders = [row for rows in persisted.payload["orders"].values() for row in rows]
    if not any(row["client_order_id"] == report["last_order_id"] and row["status"] == "FILLED" for row in orders):
        raise ValueError("旧报告成交与状态文件订单不匹配")
    state = checkpoint.snapshot_state()
    state.update(fast=meta["fast_ema"], slow=meta["slow_ema"], bars_used=report["bars_used"],
        last_processed_ns=target["ts_event"], last_main=meta["research_main"],
        last_target=str(report["last_target"]), revision=target["revision"],
        order_updates_received=report["order_updates_received"], fills_received=report["fills_received"])
    return {"ema": state}
