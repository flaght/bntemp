"""HybridTransformer 独立 Live：可配置 CTP/DolphinDB 行情，Recording/SimNow 执行。"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import time
from types import SimpleNamespace

from dotenv import load_dotenv
from bomber.framework.market.sources import (
    add_market_source_arguments, market_config_from_args, source_config,
    create_market_feed, assemble_minute_market,
)
from bomber.framework.market.basic.base import DataType, CustomBar
from bomber.framework.dataprep.live_role import SourceRoleReferences
from bomber.framework.dataprep.live_cached_role import CachedRoleReferences
from bomber.framework.dataprep.live_references import live_factor_policy
from bomber.framework.dataprep.reference_freshness import ReferenceFreshnessPolicy
from bomber.framework.dataprep.sources import (
    DolphinDbReferenceConfig, ReferenceSourceFactory, DataSourcePurpose,
)
from bomber.framework.dataprep.session_inputs import (
    add_session_arguments, session_inputs_from_args, load_session_inputs, validate_session_arguments,
)
from bomber.framework.trader import DataBinding, ExecutionRoute, RiskLimits
from bomber.framework.trader.assembly import RoleGuard, StrategyBindings, assemble_strategy
from bomber.framework.trader.live_roles import SessionRoleLiveRunner
from bomber.framework.trader.execution.builders import (
    ExecutionComponents, build_ctp_execution, attach_ctp_persistence,
)
from bomber.framework.trader.execution.recording import RecordingExecutionClient
from bomber.framework.trader.execution.risk import MarketReferencePriceStore
from bomber.framework.trader.portfolio import PositionManager
from bomber.framework.trader.execution.policy import (
    add_execution_policy_arguments, execution_policy_from_args,
)
from bomber.framework.trader.execution.ctp import CtpTdApiTransport
from bomber.framework.trader.runtime.live.channels.ctp import (
    CtpLiveProfile, CtpExecutionChannel, CtpSessionLifecycle, required,
    build_simnow_transport, add_environment_arguments, validate_environment_arguments,
)
from bomber.framework.trader.runtime.live.channels.ctp_market import CtpMarketSessionLifecycle
from bomber.framework.trader.runtime.live.channels.ctp_recovery import (
    CtpResumeLifecycle, validate_ctp_restored_positions,
)
from bomber.framework.trader.runtime.live.local import RecordingExecutionLifecycle, LocalExecutionChannel
from bomber.framework.trader.runtime.live.contracts import SessionReadiness
from bomber.framework.trader.runtime.live.recovery import StrategyPositionBinding, SessionIdentityCheckpoint
from bomber.framework.trader.runtime.live.runtime import ManagedLiveRuntime
from bomber.framework.trader.runtime.reports import LiveRunReport
from bomber.framework.calendars import SessionRulesCheckpoint

from .strategy import HybridTransformerConfig, HybridTransformerStrategy
from .checkpoint import HybridCheckpoint
from .signal_config import load_product_signal
from .history import build_history, warm_history

CLIENT_ID = 'hybrid-transformer-live'
MINUTE = 60_000_000_000


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_market_source_arguments(parser)
    add_environment_arguments(parser)
    add_session_arguments(parser)
    add_execution_policy_arguments(parser)
    parser.set_defaults(account_reconciliation='off', strategy_position_source='fills')
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--mode', choices=('recording', 'simnow'), default='recording')
    parser.add_argument('--product', required=True)
    parser.add_argument('--release-path', type=Path, required=True)
    parser.add_argument('--directions-file', type=Path, required=True)
    parser.add_argument('--signal-config', type=Path, required=True)
    parser.add_argument('--device', choices=('auto', 'cpu', 'cuda'), default='cpu')
    parser.add_argument('--history-bars', type=int, default=1500)
    parser.add_argument('--min-raw-bars', type=int, default=300)
    parser.add_argument('--history-minutes', type=int, default=0)
    parser.add_argument('--history-config', type=Path)
    parser.add_argument('--history-file', type=Path)
    parser.add_argument('--model-bar-timestamp', choices=('start', 'end'), default='start')
    parser.add_argument('--max-contracts', type=int, default=10)
    parser.add_argument('--rebalance-minutes', type=int, default=1)
    parser.add_argument('--reference-database')
    parser.add_argument('--reference-timeout', type=int, default=15)
    parser.add_argument('--reference-refresh-seconds', type=float, default=5)
    parser.add_argument('--factor-date-basis', choices=('source', 'trading'), default='source')
    parser.add_argument('--factor-availability', choices=('explicit', 'observed-on-read'), default='observed-on-read')
    parser.add_argument('--expected-trading-day', required=True)
    parser.add_argument('--expected-source-day', required=True)
    parser.add_argument('--max-notional', type=Decimal, default=Decimal('1000000'))
    parser.add_argument('--limit-offset-ticks', type=int, default=1)
    parser.add_argument('--max-session-orders', type=int, default=100)
    parser.add_argument('--query-timeout', type=float, default=15)
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--state-file', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--enable-orders', action='store_true')
    parser.add_argument('--confirm-simnow', action='store_true')
    parser.add_argument('--report-dir', type=Path, default=Path(__file__).parent/'results'/'live')
    args = parser.parse_args(argv)
    try:
        market_config_from_args(args, domestic_contracts=True)
        validate_environment_arguments(args)
        if not args.connect or args.simnow_environment != 'realtime':
            raise ValueError('须 --connect；Hybrid首版仅使用realtime事件时间')
        for name in ('expected_trading_day', 'expected_source_day'):
            value = getattr(args, name)
            if datetime.strptime(value, '%Y%m%d').strftime('%Y%m%d') != value:
                raise ValueError('交易日/来源日须为YYYYMMDD')
        args.product = args.product.strip().upper()
        if not args.release_path.is_dir() or not args.directions_file.is_file():
            raise ValueError('模型发布目录或方向文件不存在')
        load_product_signal(args.signal_config, args.product)
        if not 1 <= args.min_raw_bars <= args.history_bars <= 10000:
            raise ValueError('须满足 1 <= min-raw-bars <= history-bars <= 10000')
        if args.history_minutes and not args.min_raw_bars <= args.history_minutes <= args.history_bars:
            raise ValueError('历史预热根数须介于min-raw-bars和history-bars之间')
        if bool(args.history_config or args.history_file) != bool(args.history_minutes):
            raise ValueError('历史预热须同时声明history-minutes和history-config/history-file')
        if args.history_config and not args.history_config.is_file() or args.history_file and not args.history_file.is_file():
            raise ValueError('历史配置或文件不存在')
        if args.resume and (not args.history_minutes or not args.history_config):
            raise ValueError('恢复持仓须提供history-minutes及数据库history-config重新预热模型')
        if args.mode == 'simnow':
            if not (args.enable_orders and args.confirm_simnow and args.state_file):
                raise ValueError('SimNow须 --enable-orders --confirm-simnow --state-file')
            if args.resume != args.state_file.is_file():
                raise ValueError('新会话须新状态文件；恢复须原文件并加--resume')
        elif args.enable_orders or args.confirm_simnow or args.state_file or args.resume:
            raise ValueError('Recording不接受交易授权、交易状态文件或恢复参数')
        if not args.max_notional.is_finite() or args.max_notional <= 0 or not math.isfinite(args.seconds) or args.seconds <= 0:
            raise ValueError('名义上限和运行时间须为正')
        if not 0 <= args.reference_refresh_seconds <= 30 or not 1 <= args.reference_timeout <= 120:
            raise ValueError('参考刷新须为0至30秒、读取超时须为1至120秒')
        if not 1 <= args.max_session_orders <= 10000 or args.limit_offset_ticks < 0:
            raise ValueError('报单上限或限价偏移无效')
        if not math.isfinite(args.query_timeout) or not 0 < args.query_timeout <= 120:
            raise ValueError('CTP查询超时须在0至120秒')
        validate_session_arguments(args, purpose=DataSourcePurpose.LIVE_REFERENCE)
        execution_policy_from_args(args)
        args.factor_availability = live_factor_policy(args.factor_date_basis, args.factor_availability)
        HybridTransformerConfig(args.product, 'SHFE', 'validation', args.max_contracts,
            5, args.rebalance_minutes, Decimal(1))
    except (ValueError, TypeError, OSError) as error:
        parser.error(str(error))
    return args


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


class HybridSessionLifecycle(CtpResumeLifecycle, CtpSessionLifecycle):
    def validate_restored_session(self, session, day):
        validate_ctp_restored_positions(session, day, session.recovery_binding)
        HybridCheckpoint(session.strategy).validate_targets(session.runner.target_store)
        session.runner.hold_restored_target(session.strategy.strategy_id, session.strategy._revision)
        return session.strategy._revision

    def poll(self, session):
        session.bar_feed.poll()
        result = super().poll(session)
        session.runner.expire_due()
        return result

    def verify(self, session):
        if session.runner.failure or session.client.report_errors:
            raise RuntimeError(session.runner.failure or 'Hybrid执行回报异常')
        if not session.strategy.bars_used and not self.resuming:
            raise RuntimeError('没有处理完整实时分钟')
        # 信号可以合法为0；是否有成交单独写入报告，不以强制开仓作为运行通过条件。


class HybridRecordingLifecycle(RecordingExecutionLifecycle, CtpMarketSessionLifecycle):
    def poll(self, session):
        session.bar_feed.poll()
        result = super().poll(session)
        session.runner.expire_due()
        return result


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


class HybridLiveReport(LiveRunReport):
    def write(self, *, status, fields, session, events):
        if session is not None:
            import pandas as pd
            pd.DataFrame(session.strategy.signal_records).to_csv(self.path/'signals.csv', index=False)
            pd.DataFrame(session.strategy.batch_records).to_csv(self.path/'batches.csv', index=False)
            (self.path/'strategy_state.json').write_text(json.dumps(
                HybridCheckpoint(session.strategy).snapshot_state(), ensure_ascii=False, indent=2) + '\n')
        return super().write(status=status, fields=fields, session=session, events=events)


def build_runtime(args):
    if args.mode == 'recording':
        lifecycle = HybridRecordingLifecycle(expected_trading_day=args.expected_trading_day,
            market_source=source_config(args).source)
    else:
        transport = build_simnow_transport(CtpTdApiTransport, client_id=CLIENT_ID,
            flow_path='/tmp/bomber-hybrid-td', timeout_seconds=args.query_timeout)
        lifecycle = HybridSessionLifecycle(transport,
            md_front=required('CTP_MD_ADDRESS') if source_config(args).source == 'ctp' else None,
            orders=True, max_session_orders=args.max_session_orders, expected_trading_day=args.expected_trading_day,
            resume=args.resume, account_reconciliation=execution_policy_from_args(args).account_reconciliation)
    report = HybridLiveReport(args.report_dir, prefix='hybrid-'+args.mode,
        metadata={'mode': args.mode, 'product': args.product, 'market_config': source_config(args).identity},
        describe=lambda session: {} if session is None else {
            'bars_used': session.strategy.bars_used, 'last_prediction': session.strategy.last_prediction,
            'last_targets': session.strategy.last_targets,
            'active_batches': session.strategy.holdings.snapshot_state()['active'],
            'order_updates_received': session.strategy.order_updates_received,
            'fills_received': session.strategy.fills_received,
            'history': session.runner.history_service.last_audit})
    def progress(session):
        count = session.strategy.bars_used
        if count != getattr(session, '_reported_bars', -1):
            session._reported_bars = count
            print(f"Hybrid分钟: bars={count} prediction={session.strategy.last_prediction} "
                  f"targets={session.strategy.last_targets} active_batches={len(session.strategy.holdings.active)}", flush=True)
    return ManagedLiveRuntime(CLIENT_ID, controller=lifecycle,
        prepare_inputs=lambda context: prepare_inputs(args, context, lifecycle),
        assemble_session=lambda inputs, context: assemble(args, inputs, lifecycle),
        report=report, seconds=args.seconds, progress=progress)


def main(argv=None):
    load_dotenv()
    args = parse_args(argv)
    runtime = build_runtime(args)
    try:
        return runtime.run()
    finally:
        runtime.stop()


if __name__ == '__main__':
    main()
