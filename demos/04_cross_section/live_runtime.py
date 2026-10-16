"""04固定多主力会话装配；算法仍由原strategy.py提供。"""

from bomber.framework.market.sources import source_config, assemble_minute_market

from dataclasses import replace
from types import SimpleNamespace
import time

from bomber.framework.market.basic.base import Bar, InstrumentId, DataType
from bomber.framework.trader import DataBinding, ExecutionRoute, PositionManager, RiskLimits, RuntimeMode
from bomber.framework.trader.runner import UnifiedStrategyRunner
from bomber.framework.trader.assembly import StrategyBindings, assemble_strategy
from bomber.framework.trader.execution.builders import build_ctp_portfolio_execution, attach_ctp_persistence
from bomber.framework.trader.runtime.live.channels.ctp import CtpSessionLifecycle
from bomber.framework.dataprep.live_portfolio import PortfolioRoleReferences as PortfolioReferences
from bomber.framework.trader.runtime.live.channels.ctp import CtpLiveProfile, CtpExecutionChannel
from bomber.framework.trader.runtime.live.contracts import SessionReadiness
from bomber.framework.trader.runtime.live.recovery import StrategyPositionBinding, InstrumentPositionScope
from bomber.framework.trader.runtime.live.health import validate_instrument_max_age
from .recovery import ResumeLifecycle

from .strategy import MainCrossSectionMomentumStrategy
from bomber.framework.trader.execution.policy import execution_policy_from_args, ExecutionPolicy
from bomber.framework.trader.execution.builders import build_model_execution, attach_execution_persistence
from bomber.framework.trader.runtime.live.local import LocalExecutionChannel

CLIENT_ID = "cross-section-simnow"
MINUTE = 60_000_000_000




class CrossSectionSessionRunner(UnifiedStrategyRunner):
    """完整同步帧决策后，以本帧各腿真实行情继续未完成的固定目标。"""
    bound_instrument = None  # 单合约生命周期旧报告字段；组合报告另列全部合约。

    def _submit_locked(self, intent):
        intent = replace(intent, metadata={**intent.metadata,
            **self.session_metadata})
        # 信号属于已收盘分钟；风控估值应与实际执行时刻比较。
        if self.orders:
            intent = replace(intent, ts_event=self._clock_ns(),
                metadata={**intent.metadata, "signal_bar_ns": intent.ts_event})
        return super()._submit_locked(intent)

    def __init__(self, *, references, session_check, orders, position_manager, clock_ns=None,
                 instrument_max_age_seconds=10, session_metadata=None):
        super().__init__(RuntimeMode.LIVE, position_manager=position_manager)
        self.references, self.orders = references, orders
        self._session_check = session_check
        self._clock_ns = clock_ns or (lambda: time.time_ns())
        self.instrument_max_age_ns = validate_instrument_max_age(instrument_max_age_seconds)
        self.expected_day = references.trading_day.strftime("%Y%m%d")
        self.session_metadata = dict(session_metadata or {})
        self.instrument_health = None
        self.fixed_spec = references.spec
        self.instruments = dict(references.instrument_ids)
        self.fixed_ids = frozenset(self.instruments.values())
        self.accept_bars = False
        self.failure = None
        self.first_complete_bar_ns = 0
        self.tick_events = {}
        self.tick_receives = {}
        self.last_bars = {}
        self.strategy = self.client = None
        self.upstream = None
        self.depth_records = {}
        self.expired_bar_count = 0
        self.expired_bar_samples = []
        self.pending_frames = {}
        self.bar_counts = {}
        self.latest_bar_ns = {}
        self.expired_frame_count = 0
        self.history_frame_count = self.live_frame_count = 0

    def bar_is_current(self, event):
        now = self._clock_ns()
        age = now - event.ts_event
        details = {"instrument": str(event.bar_type.instrument_id), "bar_ns": event.ts_event,
                   "now_ns": now, "age_seconds": age / 1e9}
        if age < 0:
            raise RuntimeError(f"分钟Bar来自未来: {details}")
        if age > 120_000_000_000:
            self.expired_bar_count += 1
            self.expired_bar_samples.append(details)
            self.expired_bar_samples = self.expired_bar_samples[-20:]
            print(f"跳过过期成交Bar: {details}", flush=True)
            return False
        return True

    def observe_tick(self, tick):
        if tick.instrument_id in self.fixed_ids:
            with self._submit_lock:
                if tick.ts_event < self.tick_events.get(tick.instrument_id, -1):
                    self.close_role_gate("单合约Tick时间回退")
                self.tick_events[tick.instrument_id] = tick.ts_event
                self.tick_receives[tick.instrument_id] = time.monotonic_ns()

    def continue_on_tick(self, tick):
        """聚合/策略决策之后，用当前合约真实Tick推进保留目标，不更新信号窗口。"""
        with self._submit_lock:
            if (not self.orders or not self.accept_bars or tick.instrument_id not in self.fixed_ids or
                    self.strategy.last_targets is None or
                    self.target_store.get(self.strategy.strategy_id) is None):
                return
            stamp = tick.ts_event
            if stamp <= 0 or not 0 <= self._clock_ns() - stamp <= self.instrument_max_age_ns:
                return
            try:
                if not self.session_ready() or self.references.spec != self.fixed_spec:
                    raise RuntimeError("Tick目标推进时行情或参考版本失效")
                if getattr(self.client, "portfolio_failure", None):
                    raise RuntimeError(self.client.portfolio_failure)
                super().publish("ctp-execution-ticks", tick)
                self.continue_execution_target(self.strategy.strategy_id, str(tick.instrument_id),
                    tick.ts_event, trigger_instrument_id=tick.instrument_id)
            except Exception as error:
                self.close_role_gate(str(error))

    def session_ready(self):
        if self.failure or not self._session_check():
            return False
        return not self.stale_instruments()

    def stale_instruments(self):
        if self.instrument_health is None:
            return {}
        failures = self.instrument_health.stale_instruments()
        self.depth_records = dict(self.instrument_health.depth_records)
        return failures

    def begin_bars(self):
        if getattr(self, "history_service", None) is not None:
            with self._submit_lock:
                self.history_service.flush(self)
                if not self.session_ready() or self.references.spec != self.fixed_spec:
                    raise RuntimeError("横截面历史接续后行情或参考失效")
                pending, self.history_buffer = sorted(self.history_buffer, key=lambda b: b.ts_event), []
                self.history_buffering, self.accept_bars = False, True
                for bar in pending:
                    self.publish(self.market_feed_id, bar)
            return
        self.first_complete_bar_ns = ((self._clock_ns() + MINUTE - 1) // MINUTE + 1) * MINUTE - 1
        self.accept_bars = True

    def mark_history_live_start(self):
        with self._submit_lock:
            self.first_complete_bar_ns = ((self._clock_ns() + MINUTE - 1) // MINUTE + 1) * MINUTE - 1

    def _prepare_history(self):
        self.history_service.warm(self, self._clock_ns())

    def close_role_gate(self, reason):
        from bomber.framework.trader.runtime.live.health import readiness_failure_context
        if not self.failure:
            self.failure_readiness = readiness_failure_context(self)
        self.failure = str(reason)
        self.accept_bars = False
        if self.orders and self.client is not None:
            if not getattr(self.client, "shared_account", False):
                self.position_manager.mark_recovery_required(self.client.client_id)
            self.client.disarm("cross_section_failed")
        raise RuntimeError(reason)

    def publish(self, feed_id, event):
        if isinstance(event, Bar) and getattr(self, "history_buffering", False):
            with self._submit_lock:
                if self.history_buffering:
                    if (feed_id != self.market_feed_id or event.bar_type.instrument_id not in self.fixed_ids
                            or event.ts_event > self._clock_ns() or len(self.history_buffer) >= 10000):
                        self.close_role_gate("横截面历史缓冲来源、时间、合约或容量无效")
                    self.history_buffer.append(event)
                    return
        if not isinstance(event, Bar) or not self.accept_bars:
            return
        instrument = event.bar_type.instrument_id
        if instrument not in self.fixed_ids or event.ts_event < self.first_complete_bar_ns:
            return
        with self._submit_lock:
            now = self._clock_ns()
            for stamp in tuple(self.pending_frames):
                if now - stamp > 120_000_000_000:
                    del self.pending_frames[stamp]
                    self.expired_frame_count += 1
            if event.ts_event <= self.strategy.last_emitted_ns:
                return
            try:
                if not self.session_ready():
                    raise RuntimeError(f"任一品种行情陈旧或MD/TD会话失效: {self.stale_instruments()}")
                if not self.bar_is_current(event):
                    return
                if self.references.spec != self.fixed_spec:
                    raise RuntimeError("主力/条款/累计因子变化，固定会话须重新核对")
                if not self.session_ready():
                    raise RuntimeError("参考刷新后行情失效")
                if not self.bar_is_current(event):
                    return
                frame = self.pending_frames.setdefault(event.ts_event, {})
                if instrument in frame:
                    return
                frame[instrument] = (feed_id, event)
                self.bar_counts[instrument] = self.bar_counts.get(instrument, 0) + 1
                self.latest_bar_ns[instrument] = max(event.ts_event, self.latest_bar_ns.get(instrument, -1))
                for stamp in sorted(tuple(self.pending_frames)):
                    current_frame = self.pending_frames[stamp]
                    if stamp <= self.strategy.last_emitted_ns:
                        del self.pending_frames[stamp]
                        continue
                    if set(current_frame) != self.fixed_ids:
                        continue
                    if getattr(self, "history_service", None) is not None:
                        self.history_service.warm(self, stamp, phase="recovery")
                    if not self.bar_is_current(next(iter(current_frame.values()))[1]):
                        del self.pending_frames[stamp]
                        self.expired_frame_count += 1
                        continue
                    # 只发布同一分钟完整五腿；不会让下一分钟抢先覆盖较慢腿的当前分钟。
                    for current in self.instruments.values():
                        fid, bar = current_frame[current]
                        if not self.session_ready() or self.references.spec != self.fixed_spec:
                            raise RuntimeError("完整帧发布期间行情或参考版本失效")
                        super().publish(fid, bar)
                        self.last_bars[current] = stamp
                    self.live_frame_count += 1
                    del self.pending_frames[stamp]
                    if self.orders and self.strategy.last_targets is not None:
                        for current in self.instruments.values():
                            if not self.session_ready() or getattr(self.client, "portfolio_failure", None):
                                from bomber.framework.trader.runtime.live.health import readiness_failure_context
                                details = {**readiness_failure_context(self),
                                    "report_errors": [str(e) for e in getattr(self.client, "report_errors", ())],
                                    "portfolio_failure": getattr(self.client, "portfolio_failure", None)}
                                raise RuntimeError(f"逐腿推进期间会话失败或出现拒单: {details}")
                            self.continue_execution_target(self.strategy.strategy_id, str(current),
                                stamp, trigger_instrument_id=current)
            except Exception as error:
                self.close_role_gate(str(error))


class CrossSectionLifecycle(ResumeLifecycle, CtpSessionLifecycle):
    @staticmethod
    def _expected_gross(session):
        result = {}
        for instrument in session.runner.fixed_ids:
            state = session.ledger.snapshot(instrument)
            if state.long_total or state.short_total:
                result[str(instrument)] = (state.long_total, state.short_total)
        return result

    def poll(self, session):
        if self.orders and getattr(session.client, "portfolio_failure", None):
            session.runner.close_role_gate(session.client.portfolio_failure)
        stale = session.runner.stale_instruments()
        if stale:
            session.runner.close_role_gate(f"逐合约深度行情陈旧或失效: {stale}")
        return super().poll(session)

    def shutdown(self, session, context):
        if session:
            with session.runner._submit_lock:
                session.runner.accept_bars = False
        return super().shutdown(session, context)

    def verify(self, session):
        required = session.strategy.lookback + 1
        if session.strategy.synchronized_frames < required:
            raise RuntimeError(f"同步帧预热不足: actual={session.strategy.synchronized_frames} required={required} "
                               f"bar_counts={session.runner.bar_counts}")
        if self.resuming:
            if session.client.report_errors or session.strategy.last_targets is None:
                raise RuntimeError("恢复后尚无有效新目标或存在回报错误")
        else:
            super().verify(session)
        if not self.orders:
            return
        positions = session.runner.position_manager
        for instrument in session.runner.fixed_ids:
            if positions.unassigned_position(CLIENT_ID, instrument) != 0:
                raise RuntimeError(f"成交存在未归属仓位: {instrument}")
        if not self.resuming and not session.strategy.fills_received:
            raise RuntimeError("没有策略成交回调，不能认定组合归属通过")
        for key, target in (session.strategy.last_targets or {}).items():
            instrument = InstrumentId.from_str(key)
            if (session.strategy.position(key) != target or
                    positions.working_quantity(CLIENT_ID, instrument) != 0):
                raise RuntimeError(f"组合最新目标尚未完成: {key} target={target} "
                                   f"position={session.strategy.position(key)}")

    def snapshot(self, session, context):
        fields = super().snapshot(session, context)
        fields.pop("fixed_instrument", None)
        return {**fields,
            "resume_ready": self.resume_ready,
            "restored_generation": getattr(self, "restored_generation", None),
            "fixed_instruments": ({p: str(i) for p, i in session.runner.instruments.items()} if session else {}),
            "per_instrument_tick_ns": ({str(i): t for i, t in session.runner.tick_events.items()} if session else {}),
            "per_instrument_depth": ({str(i): vars(o) for i, o in
                session.runner.depth_records.items()} if session else {}),
            "expired_bar_count": session.runner.expired_bar_count if session else 0,
            "expired_bar_samples": list(session.runner.expired_bar_samples) if session else [],
            "per_instrument_bar_count": {str(i): n for i, n in session.runner.bar_counts.items()} if session else {},
            "per_instrument_latest_bar_ns": {str(i): n for i, n in session.runner.latest_bar_ns.items()} if session else {},
            "expired_frame_count": session.runner.expired_frame_count if session else 0,
            "pending_frames": [{"bar_ns": stamp, "received": sorted(str(i) for i in frame),
                "missing": sorted(str(i) for i in session.runner.fixed_ids - set(frame))}
                for stamp, frame in sorted(session.runner.pending_frames.items())] if session else [],
            "portfolio_failure": getattr(session.client, "portfolio_failure", None) if session else None}


def assemble(args, references, driver, upstream, *, account_routes=(), shared_service=None, strategy_id=None):
    references.validate()
    references.assert_expected(args.expected_instruments, getattr(args, "expected_source_symbols", None))
    model = args.mode == "model"
    orders = args.mode in {"simnow", "model"}
    execution_policy = execution_policy_from_args(args)
    environment = getattr(args, "simnow_environment", "realtime")
    replay_day = getattr(args, "replay_md_trading_day", None)
    policy = CtpLiveProfile(environment, replay_day)
    expected_day = references.trading_day.strftime("%Y%m%d")
    positions = shared_service.position_manager if shared_service is not None else PositionManager()
    client_id = shared_service.client_id if shared_service is not None else CLIENT_ID
    client_holder = {}
    local_recording = args.mode == "recording"
    execution_channel = (LocalExecutionChannel(CLIENT_ID, lambda: bool(client_holder.get("client") and
        (client_holder["client"].is_started if local_recording else client_holder["client"].is_reconciled)),
        channel=args.mode) if model or local_recording else CtpExecutionChannel(driver, expected_day, require_orders=orders))
    runner = CrossSectionSessionRunner(references=references, orders=orders,
        instrument_max_age_seconds=getattr(args, "instrument_max_age_seconds", 10),
        session_metadata={"simnow_environment": policy.environment, "replay_md_trading_day": replay_day,
                          **({"execution_policy": execution_policy.snapshot_state()} if execution_policy != ExecutionPolicy() else {})},
        position_manager=positions, session_check=lambda: False)
    runner.replay_md_trading_day = replay_day  # 旧检查点/报告兼容字段，非策略输入。
    runner.expected_md_day = policy.expected_md_day(expected_day)
    runner.upstream = upstream
    feed, market_channel, runner.instrument_health = assemble_minute_market(args, upstream,
        "CROSS_SECTION_1M", policy, runner.fixed_ids, expected_day,
        after_tick=runner.continue_on_tick, clock_ns=runner._clock_ns,
        fallback_events=runner.tick_events, fallback_receives=runner.tick_receives)
    check = SessionReadiness(market_channel, execution_channel)
    runner._session_check = check
    ids, specs = references.instrument_ids, references.specs
    position_scope = InstrumentPositionScope(ids.values())
    limits = {ids[p]: RiskLimits(max_order_quantity=args.max_quantity,
        max_abs_position=args.max_quantity, max_order_notional=args.max_notional,
        max_abs_position_notional=args.max_notional, max_market_age_ns=120_000_000_000,
        contract_multiplier=specs[p].multiplier) for p in args.products}
    if shared_service is not None:
        if model or account_routes or args.mode != "simnow" or not strategy_id:
            raise ValueError("共享执行须为具备独立策略ID的SimNow装配")
        from bomber.framework.trader.execution.builders import ExecutionComponents
        from bomber.framework.trader.execution.shared import SharedRiskBudget
        port = shared_service.strategy_client(strategy_id, routes={str(i): i for i in ids.values()},
            budget=SharedRiskBudget(args.max_quantity * len(ids), args.max_notional * len(ids)),
            instrument_limits=limits)
        execution = ExecutionComponents(port, positions, shared_service.prices)
    elif model:
        execution = build_model_execution(CLIENT_ID, instrument_limits=limits, positions=positions)
    elif local_recording:
        from bomber.framework.trader.execution.builders import ExecutionComponents
        from bomber.framework.trader.execution.recording import RecordingExecutionClient
        from bomber.framework.trader.execution.risk import MarketReferencePriceStore
        execution = ExecutionComponents(RecordingExecutionClient(CLIENT_ID), positions, MarketReferencePriceStore())
    else:
        execution = build_ctp_portfolio_execution(driver, trading_day=expected_day,
            price_increments={ids[p]: specs[p].tick for p in args.products},
            multipliers={ids[p]: specs[p].multiplier for p in args.products},
            instrument_limits=limits, session_check=runner.session_ready, orders=orders,
            positions=positions, limit_offset_ticks=args.limit_offset_ticks, instrument_scope=position_scope,
            account_reconciliation=execution_policy.account_reconciliation,
            generic_close_venues=("DCE", "CZCE"),
            generic_close_today_first_venues=("DCE", "CZCE"))
    client_holder["client"] = execution.client
    if account_routes:
        if not model:
            raise ValueError("入口的一对多装配须使用独立模型主账本；归属成交源可通过公共builder显式选择")
        from bomber.framework.trader.execution.builders import build_fanout_execution
        execution = build_fanout_execution(execution, account_routes)
        client_holder["client"] = execution.client
    strategy = MainCrossSectionMomentumStrategy(strategy_id or CLIENT_ID, products=args.products, roles=references,
        instruments={str(ids[p].symbol).lower(): ids[p] for p in args.products},
        multipliers={ids[p]: specs[p].multiplier for p in args.products},
        lookback=args.lookback, rebalance_interval=args.rebalance_interval,
        target_notional=args.target_notional, group_fraction=args.group_fraction,
        target_quantity_cap=getattr(args, "target_quantity_cap", None),
        require_full_groups=getattr(args, "require_full_groups", False))
    runner.strategy, runner.client = strategy, execution.client
    history_enabled = any(getattr(args, name, None) for name in ("history_minutes", "history_config", "history_file"))
    if history_enabled:
        from .history import build_history
        runner.history_service = build_history(args, references)
        runner.history_buffer, runner.history_buffering = [], True
        runner.prepare_history = runner._prepare_history
    references.publication_lock = runner._submit_lock
    # 先记录各腿Tick新鲜度，再让聚合器分派由新Tick收盘的Bar。
    if source_config(args).source == "ctp":
        upstream.register_trade_tick_handler(runner.observe_tick)
    for p in args.products:
        feed.register_instrument(references.instrument_meta(p))
    runner.market_feed_id = "ctp-bars" if source_config(args).source == "ctp" else "market-bars"
    bindings = StrategyBindings(
        data=tuple(DataBinding(str(i), runner.market_feed_id, i, DataType.BAR, "1-MINUTE") for i in ids.values()),
        execution=tuple(ExecutionRoute(str(i), client_id, i) for i in ids.values()))
    assemble_strategy(runner, strategy, feeds={runner.market_feed_id: feed}, execution=execution, bindings=bindings)
    components = {"execution_policy": execution_policy} if execution_policy != ExecutionPolicy() else {}
    if history_enabled:
        from bomber.framework.trader.runtime.live.recovery import SessionIdentityCheckpoint
        components["history"] = SessionIdentityCheckpoint({
            "config": str(args.history_config), "file": str(args.history_file),
            "minutes": args.history_minutes or args.lookback + 1, "stage": args.history_stage,
            "missing_policy": args.history_missing_policy,
            "calendars": {p: c.provenance["sha256"] for p, c in runner.history_service.calendars.items()}})
    if source_config(args).source != "ctp":
        from bomber.framework.trader.runtime.live.recovery import SessionIdentityCheckpoint
        components["market_source"] = SessionIdentityCheckpoint(source_config(args).identity)
    attach = attach_execution_persistence if model else attach_ctp_persistence
    manager = attach(execution, runner, state_file=args.state_file, state_components=components) if orders and shared_service is None else None
    runner.manager = manager
    return SimpleNamespace(runner=runner, strategy=strategy, client=execution.client,
        execution=execution, ledger=execution.ledger, manager=manager, driver=driver,
        references=references, upstream=upstream, bar_feed=feed, market_profile=policy, readiness=check,
        market_source_identity=source_config(args).identity if source_config(args).source != "ctp" else None,
        position_scope=position_scope,
        recovery_binding=StrategyPositionBinding(strategy.strategy_id, execution.client.client_id,
            {str(i): i for i in ids.values()}))
