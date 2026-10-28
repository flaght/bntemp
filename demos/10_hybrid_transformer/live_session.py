"""Hybrid Live 的参考准备、策略组装与分钟 Runner。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import time
from types import SimpleNamespace

from bomber.framework.calendars import SessionRulesCheckpoint
from bomber.framework.dataprep.live_cached_role import CachedRoleReferences
from bomber.framework.dataprep.live_role import SourceRoleReferences
from bomber.framework.dataprep.reference_freshness import ReferenceFreshnessPolicy
from bomber.framework.dataprep.session_inputs import load_session_inputs, session_inputs_from_args
from bomber.framework.dataprep.sources import (
    DataSourcePurpose, DolphinDbReferenceConfig, ReferenceSourceFactory,
)
from bomber.framework.market.basic.base import CustomBar, DataType
from bomber.framework.market.sources import assemble_minute_market, create_market_feed, source_config
from bomber.framework.trader import DataBinding, ExecutionRoute, RiskLimits
from bomber.framework.trader.assembly import RoleGuard, StrategyBindings, assemble_strategy
from bomber.framework.trader.execution.builders import (
    ExecutionComponents, attach_ctp_persistence, build_ctp_execution,
)
from bomber.framework.trader.execution.policy import execution_policy_from_args
from bomber.framework.trader.execution.recording import RecordingExecutionClient
from bomber.framework.trader.execution.risk import MarketReferencePriceStore
from bomber.framework.trader.live_roles import SessionRoleLiveRunner
from bomber.framework.trader.portfolio import PositionManager
from bomber.framework.trader.runtime.live.channels.ctp import CtpExecutionChannel, CtpLiveProfile
from bomber.framework.trader.runtime.live.contracts import SessionReadiness
from bomber.framework.trader.runtime.live.local import LocalExecutionChannel
from bomber.framework.trader.runtime.live.recovery import SessionIdentityCheckpoint, StrategyPositionBinding

from .checkpoint import HybridCheckpoint
from .history import build_history, warm_history
from .signal_config import load_product_signal
from .strategy import HybridTransformerConfig, HybridTransformerStrategy

CLIENT_ID = 'hybrid-transformer-live'
MINUTE = 60_000_000_000


def model_identity(args):
    root = args.release_path.resolve()
    files = sorted(root.glob('*/*/config.json')) + sorted(root.glob('*/*/models/best_model.pt'))
    files += [args.directions_file, args.signal_config]
    if not files:
        raise ValueError('模型发布目录没有配置/权重')
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.resolve().relative_to(root) if path.is_relative_to(root) else path.name).encode())
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024*1024), b''):
                digest.update(block)
    return {'sha256': digest.hexdigest(), 'history_bars': args.history_bars,
        'min_raw_bars': args.min_raw_bars, 'model_bar_timestamp': args.model_bar_timestamp,
        'market': source_config(args).identity, 'trading_day': args.expected_trading_day}


def prepare_inputs(args, context, lifecycle):
    config = DolphinDbReferenceConfig.from_env(database=args.reference_database,
        read_timeout_seconds=args.reference_timeout)
    source = ReferenceSourceFactory.create_for(DataSourcePurpose.LIVE_REFERENCE, 'dolphindb', config)
    context.resources.callback(source.close)
    source.open()
    references = CachedRoleReferences(SourceRoleReferences(source, product=args.product,
        trading_day=context.trading_day, started_ns=time.time_ns(),
        factor_date_basis=args.factor_date_basis, factor_availability=args.factor_availability,
        refresh_seconds=args.reference_refresh_seconds, allowed_venues=('SHFE', 'INE'),
        required_currency='CNY', freshness=ReferenceFreshnessPolicy(
            expected_source_day=datetime.strptime(args.expected_source_day, '%Y%m%d').date())))
    context.references = references
    assignment = references.snapshot(time.time_ns())
    if assignment.source_day.strftime('%Y%m%d') != args.expected_source_day:
        raise ValueError('主力参考来源日与声明不一致')
    calendar = load_session_inputs(session_inputs_from_args(args, exchange=references.spec.venue),
        purpose=DataSourcePurpose.LIVE_REFERENCE)
    upstream = create_market_feed(source_config(args), 'hybrid-market', lifecycle=lifecycle,
        flow_path='/tmp/bomber-hybrid-md', environment='realtime')
    return SimpleNamespace(references=references, calendar=calendar, upstream=upstream)


class HybridLiveRunner(SessionRoleLiveRunner):
    """固定主力的完整分钟输入；时钟与模型处理在同一控制线程顺序执行。"""
    def begin_bars(self):
        with self._submit_lock:
            warm_history(self, self.args)
            super().begin_bars()
            # 历史窗口已预热至启动边界；排除历史重复并保留完整实时分钟。
            self.first_complete_bar_ns = max(self.first_complete_bar_ns, self.history_last_ns + MINUTE)
            self.expire_due()

    def publish(self, feed_id, event):
        if isinstance(event, CustomBar):
            if event.ts_event <= self.history_last_ns:
                return
            if self.accept_bars and event.ts_event < self.first_complete_bar_ns:
                from bomber.framework.market.futures import futures_bar_values
                values = futures_bar_values(event, contract_multiplier=self.strategy.config.contract_multiplier)
                rows = list(self.strategy.predictor.workflow.history)
                rows.append({"trade_time": event.factors["trade_time"], "code": self.args.product, **values})
                self.strategy.predictor.warm_history(rows)
                self.history_last_ns = event.ts_event
                return
        return super().publish(feed_id, event)

    def _submit_locked(self, intent):
        if not self.session_ready():
            raise RuntimeError('Hybrid提交目标时行情/执行未就绪')
        previous = self.target_store.get(intent.strategy_id)
        if (self.orders and previous is not None and dict(previous.targets) != dict(intent.targets)
                and self.position_manager.working_quantity(self.client.client_id, self.bound_instrument)):
            self.client.cancel_strategy(intent.strategy_id)
        intent = replace(intent, ts_event=self._clock_ns(), metadata={
            **intent.metadata, 'signal_bar_ns': intent.ts_event})
        return super()._submit_locked(intent)

    def expire_due(self):
        if not self.accept_bars:
            return
        with self._submit_lock:
            # 先处理已经到达的完整Bar，再处理同一边界的独立时钟。
            now = self._clock_ns()
            self.strategy.on_time(now)
            if self.orders and self.strategy.last_targets:
                self.continue_execution_target(self.strategy.strategy_id, self.strategy.config.target_key,
                    now, trigger_instrument_id=self.bound_instrument)


def assemble(args, inputs, lifecycle):
    from .model_adapter import MizarWorkflowAdapter
    references, calendar, upstream = inputs.references, inputs.calendar, inputs.upstream
    instrument = references.instrument_id
    selected = load_product_signal(args.signal_config, args.product)
    profile = CtpLiveProfile('realtime')
    feed, market_channel, health = assemble_minute_market(args, upstream, 'HYBRID_1M', profile,
        (instrument,), references.trading_day.strftime('%Y%m%d'), futures_calendar=calendar,
        model_timestamp=args.model_bar_timestamp)
    feed.register_instrument(references.instrument_meta())
    orders = args.mode == 'simnow'
    if orders:
        check = SessionReadiness(market_channel, CtpExecutionChannel(lifecycle.driver,
            references.trading_day.strftime('%Y%m%d'), require_orders=True))
        limits = RiskLimits(max_order_quantity=2*args.max_contracts, max_abs_position=args.max_contracts,
            max_order_notional=args.max_notional, max_abs_position_notional=args.max_notional,
            max_market_age_ns=120_000_000_000, contract_multiplier=references.spec.multiplier)
        execution = build_ctp_execution(lifecycle.driver, instrument_id=instrument,
            trading_day=references.trading_day.strftime('%Y%m%d'), price_increment=references.spec.tick,
            multiplier=references.spec.multiplier, risk_limits=limits, session_check=check,
            orders=True, limit_offset_ticks=args.limit_offset_ticks,
            account_reconciliation=execution_policy_from_args(args).account_reconciliation)
    else:
        execution = ExecutionComponents(RecordingExecutionClient(CLIENT_ID), PositionManager(), MarketReferencePriceStore())
        check = SessionReadiness(market_channel, LocalExecutionChannel(CLIENT_ID,
            lambda: execution.client.is_started, channel='recording'))
    predictor = MizarWorkflowAdapter(release_path=args.release_path, directions_file=args.directions_file,
        code=args.product, signal_id=selected.signal_id, signal_method=selected.signal_method,
        signal_params=selected.signal_params, device=args.device, history_bars=args.history_bars,
        min_raw_bars=args.min_raw_bars)
    strategy = HybridTransformerStrategy(CLIENT_ID, references, predictor, HybridTransformerConfig(
        args.product, references.spec.venue, selected.signal_id, args.max_contracts, selected.hold_bars,
        args.rebalance_minutes, references.spec.multiplier, record_signals=True), calendar=calendar)
    runner = HybridLiveRunner(references=references, session_check=check, orders=orders,
        position_manager=execution.positions, instrument_health=health)
    runner.args, runner.calendar, runner.strategy, runner.client = args, calendar, strategy, execution.client
    runner.history_last_ns = -1
    runner.history_service = build_history(args)
    if not getattr(references, 'refresh_on_event', True):
        references.publication_lock = runner._submit_lock
    bindings = StrategyBindings(
        data=(DataBinding(str(instrument), 'model-bars', instrument, DataType.CUSTOM_BAR, '1-MINUTE'),),
        execution=(ExecutionRoute(strategy.config.target_key, execution.client.client_id, instrument),),
        role_guard=RoleGuard(references, args.product, 'main', lambda: strategy.last_processed_ns))
    assemble_strategy(runner, strategy, feeds={'model-bars': feed}, execution=execution, bindings=bindings)
    components = {'hybrid': HybridCheckpoint(strategy), 'session_rules': SessionRulesCheckpoint(calendar),
        'live_session': SessionIdentityCheckpoint(model_identity(args))}
    manager = attach_ctp_persistence(execution, runner, state_file=args.state_file,
        state_components=components) if orders else None
    runner.manager = manager
    if manager is not None:
        strategy.bind_checkpoint(manager.save)
    return SimpleNamespace(runner=runner, strategy=strategy, execution=execution, client=execution.client,
        driver=lifecycle.driver, upstream=upstream, ledger=execution.ledger, manager=manager,
        references=references, bar_feed=feed, market_profile=profile, readiness=check,
        recovery_binding=StrategyPositionBinding(strategy.strategy_id, execution.client.client_id,
            {strategy.config.target_key: instrument}))
