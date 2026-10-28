"""Hybrid Live：少量命令行选项；模型、策略与运行器在本文件中构建。"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pandas as pd
from dotenv import load_dotenv
from bomber.framework.calendars import SessionRulesCheckpoint
from bomber.framework.dataprep.live_cached_role import CachedRoleReferences
from bomber.framework.dataprep.live_references import live_factor_policy
from bomber.framework.dataprep.live_role import SourceRoleReferences
from bomber.framework.dataprep.reference_freshness import ReferenceFreshnessPolicy
from bomber.framework.dataprep.session_inputs import load_session_inputs, session_inputs_from_args, validate_session_arguments
from bomber.framework.dataprep.sources import DataSourcePurpose, DolphinDbReferenceConfig, ReferenceSourceFactory
from bomber.framework.market.basic.base import CustomBar, DataType
from bomber.framework.market.sources import assemble_minute_market, create_market_feed, market_config_from_args, source_config
from bomber.framework.trader import DataBinding, ExecutionRoute, RiskLimits
from bomber.framework.trader.assembly import RoleGuard, StrategyBindings, assemble_strategy
from bomber.framework.trader.execution.builders import ExecutionComponents, attach_ctp_persistence, build_ctp_execution
from bomber.framework.trader.execution.ctp import CtpTdApiTransport
from bomber.framework.trader.execution.policy import ExecutionPolicy, StrategyPositionSource
from bomber.framework.trader.execution.recording import RecordingExecutionClient
from bomber.framework.trader.execution.risk import MarketReferencePriceStore
from bomber.framework.trader.live_roles import SessionRoleLiveRunner
from bomber.framework.trader.portfolio import PositionManager
from bomber.framework.trader.runtime.live.channels.ctp import CtpExecutionChannel, CtpLiveProfile, CtpSessionLifecycle, build_simnow_transport, required
from bomber.framework.trader.runtime.live.channels.ctp_market import CtpMarketSessionLifecycle
from bomber.framework.trader.runtime.live.channels.ctp_recovery import CtpResumeLifecycle, validate_ctp_restored_positions
from bomber.framework.trader.runtime.live.contracts import SessionReadiness
from bomber.framework.trader.runtime.live.local import LocalExecutionChannel, RecordingExecutionLifecycle
from bomber.framework.trader.runtime.live.recovery import SessionIdentityCheckpoint, StrategyPositionBinding
from bomber.framework.trader.runtime.live.runtime import ManagedLiveRuntime
from bomber.framework.trader.runtime.reports import LiveRunReport

from .checkpoint import HybridCheckpoint
from .history import build_history, warm_history
from .signal_config import load_product_signal
from .strategy import HybridTransformerConfig, HybridTransformerStrategy

CLIENT_ID = 'hybrid-transformer-live'
MINUTE = 60_000_000_000

# 模型与策略声明的默认设置；不扩展成命令行开关。
MODEL_DEVICE = 'cpu'
MODEL_HISTORY_BARS = 1500
MODEL_MIN_RAW_BARS = 300
MODEL_BAR_TIMESTAMP = 'start'
REBALANCE_MINUTES = 1


def parse_args(argv=None):
    """命令行只保留业务输入、运行选择及交易/恢复开关。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--mode', choices=('recording', 'simnow'), default='recording')
    parser.add_argument('--product', required=True)
    parser.add_argument('--release-path', type=Path, required=True)
    parser.add_argument('--directions-file', type=Path, required=True)
    parser.add_argument('--signal-config', type=Path, required=True)
    parser.add_argument('--expected-trading-day', required=True)
    parser.add_argument('--expected-source-day', required=True)
    parser.add_argument('--max-contracts', type=int, default=10)
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--market-source', choices=('ctp', 'dolphindb'))
    parser.add_argument('--market-config', type=Path, help='行情源配置；DolphinDB在JSON中声明表名、时间标签和量额口径')
    parser.add_argument('--history-minutes', type=int, default=0)
    parser.add_argument('--history-config', type=Path)
    parser.add_argument('--history-file', type=Path)
    parser.add_argument('--state-file', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--enable-orders', action='store_true')
    parser.add_argument('--confirm-simnow', action='store_true')
    parser.add_argument('--report-dir', type=Path, default=Path(__file__).parent/'results'/'live')
    parser.add_argument('--no-session-overrides', action='store_true')
    parser.set_defaults(calendar_id=os.getenv('BOMBER_CALENDAR_ID') or 'china.sse')
    args = parser.parse_args(argv)
    try:
        if not args.connect:
            raise ValueError('须显式 --connect')
        market_config_from_args(args, domestic_contracts=True)
        for name in ('expected_trading_day', 'expected_source_day'):
            value = getattr(args, name)
            if datetime.strptime(value, '%Y%m%d').strftime('%Y%m%d') != value:
                raise ValueError('交易日/来源日须为YYYYMMDD')
        args.product = args.product.strip().upper()
        if not args.product.isalpha():
            raise ValueError('品种须为非空字母代码')
        if not args.release_path.is_dir() or not args.directions_file.is_file():
            raise ValueError('模型发布目录或方向文件不存在')
        load_product_signal(args.signal_config, args.product)
        if args.max_contracts < 1 or not math.isfinite(args.seconds) or args.seconds <= 0:
            raise ValueError('最大手数和运行时间须为正，运行时间须为有限数')
        if args.history_minutes and not MODEL_MIN_RAW_BARS <= args.history_minutes <= MODEL_HISTORY_BARS:
            raise ValueError('历史预热根数须介于模型最小根数与历史窗口之间')
        if bool(args.history_config or args.history_file) != bool(args.history_minutes):
            raise ValueError('历史预热须同时声明history-minutes及history-config/history-file')
        if args.history_config and not args.history_config.is_file() or args.history_file and not args.history_file.is_file():
            raise ValueError('历史配置或文件不存在')
        if args.resume and (not args.history_minutes or not args.history_config):
            raise ValueError('恢复须提供history-minutes和数据库history-config重新预热')
        if args.mode == 'simnow':
            if not (args.enable_orders and args.confirm_simnow and args.state_file):
                raise ValueError('SimNow须 --enable-orders --confirm-simnow --state-file')
            if args.resume != args.state_file.is_file():
                raise ValueError('新会话须新状态文件；恢复须原文件并加--resume')
        elif args.enable_orders or args.confirm_simnow or args.state_file or args.resume:
            raise ValueError('Recording不接受交易授权、交易状态文件或恢复参数')
        validate_session_arguments(args, purpose=DataSourcePurpose.LIVE_REFERENCE)
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
    return {'sha256': digest.hexdigest(), 'history_bars': MODEL_HISTORY_BARS,
        'min_raw_bars': MODEL_MIN_RAW_BARS, 'model_bar_timestamp': MODEL_BAR_TIMESTAMP,
        'market': source_config(args).identity, 'trading_day': args.expected_trading_day}


def prepare_inputs(args, context, lifecycle, *, reference_database=None,
                   reference_timeout=15, refresh_seconds=5, trading_hours_database=None):
    config = DolphinDbReferenceConfig.from_env(database=reference_database,
        read_timeout_seconds=reference_timeout)
    source = ReferenceSourceFactory.create_for(DataSourcePurpose.LIVE_REFERENCE, 'dolphindb', config)
    context.resources.callback(source.close)
    source.open()
    references = CachedRoleReferences(SourceRoleReferences(source, product=args.product,
        trading_day=context.trading_day, started_ns=time.time_ns(),
        factor_date_basis='source', factor_availability=live_factor_policy('source', 'observed-on-read'),
        refresh_seconds=refresh_seconds, allowed_venues=('SHFE', 'INE'),
        required_currency='CNY', freshness=ReferenceFreshnessPolicy(
            expected_source_day=datetime.strptime(args.expected_source_day, '%Y%m%d').date())))
    context.references = references
    assignment = references.snapshot(time.time_ns())
    if assignment.source_day.strftime('%Y%m%d') != args.expected_source_day:
        raise ValueError('主力参考来源日与声明不一致')
    session_inputs = session_inputs_from_args(args, exchange=references.spec.venue)
    session_inputs = replace(session_inputs,
        database=trading_hours_database or reference_database or session_inputs.database)
    calendar = load_session_inputs(session_inputs,
        purpose=DataSourcePurpose.LIVE_REFERENCE)
    upstream = create_market_feed(source_config(args), 'hybrid-market', lifecycle=lifecycle,
        flow_path='/tmp/bomber-hybrid-md', environment='realtime')
    return SimpleNamespace(references=references, calendar=calendar, upstream=upstream)


class HybridLiveRunner(SessionRoleLiveRunner):
    """固定主力的完整分钟输入；时钟与模型处理在同一控制线程顺序执行。"""
    def begin_bars(self):
        with self._submit_lock:
            warm_history(self, SimpleNamespace(**{
                **vars(self.args), 'model_bar_timestamp': MODEL_BAR_TIMESTAMP}))
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


def build_strategy(args, references, calendar):
    """模型和策略在这里明确声明，不经命令行或运行器回调参数传入。"""
    from .model_adapter import MizarWorkflowAdapter
    selected = load_product_signal(args.signal_config, args.product)
    predictor = MizarWorkflowAdapter(
        release_path=args.release_path, directions_file=args.directions_file,
        code=args.product, signal_id=selected.signal_id,
        signal_method=selected.signal_method, signal_params=selected.signal_params,
        device=MODEL_DEVICE, history_bars=MODEL_HISTORY_BARS,
        min_raw_bars=MODEL_MIN_RAW_BARS)
    config = HybridTransformerConfig(
        product=args.product, venue=references.spec.venue, signal_id=selected.signal_id,
        max_contracts=args.max_contracts, hold_bars=selected.hold_bars,
        rebalance_minutes=REBALANCE_MINUTES,
        contract_multiplier=references.spec.multiplier, record_signals=True)
    return HybridTransformerStrategy(CLIENT_ID, references, predictor, config, calendar=calendar)


def assemble(args, inputs, lifecycle, *, execution_policy=None,
             max_notional=Decimal('1000000'), limit_offset_ticks=1):
    execution_policy = execution_policy or ExecutionPolicy('off', 'fills')
    references, calendar, upstream = inputs.references, inputs.calendar, inputs.upstream
    instrument = references.instrument_id
    profile = CtpLiveProfile('realtime')
    feed, market_channel, health = assemble_minute_market(args, upstream, 'HYBRID_1M', profile,
        (instrument,), references.trading_day.strftime('%Y%m%d'), futures_calendar=calendar,
        model_timestamp=MODEL_BAR_TIMESTAMP)
    feed.register_instrument(references.instrument_meta())
    orders = args.mode == 'simnow'
    if orders:
        check = SessionReadiness(market_channel, CtpExecutionChannel(lifecycle.driver,
            references.trading_day.strftime('%Y%m%d'), require_orders=True))
        limits = RiskLimits(max_order_quantity=2*args.max_contracts, max_abs_position=args.max_contracts,
            max_order_notional=max_notional, max_abs_position_notional=max_notional,
            max_market_age_ns=120_000_000_000, contract_multiplier=references.spec.multiplier)
        execution = build_ctp_execution(lifecycle.driver, instrument_id=instrument,
            trading_day=references.trading_day.strftime('%Y%m%d'), price_increment=references.spec.tick,
            multiplier=references.spec.multiplier, risk_limits=limits, session_check=check,
            orders=True, limit_offset_ticks=limit_offset_ticks,
            account_reconciliation=execution_policy.account_reconciliation)
    else:
        execution = ExecutionComponents(RecordingExecutionClient(CLIENT_ID), PositionManager(), MarketReferencePriceStore())
        check = SessionReadiness(market_channel, LocalExecutionChannel(CLIENT_ID,
            lambda: execution.client.is_started, channel='recording'))
    strategy = build_strategy(args, references, calendar)
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


class HybridSessionLifecycle(CtpResumeLifecycle, CtpSessionLifecycle):
    def validate_restored_session(self, session, day):
        validate_ctp_restored_positions(session, day, session.recovery_binding)
        HybridCheckpoint(session.strategy).validate_targets(session.runner.target_store)
        revision = session.strategy._revision
        session.runner.hold_restored_target(session.strategy.strategy_id, revision)
        return revision

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


class HybridRecordingLifecycle(RecordingExecutionLifecycle, CtpMarketSessionLifecycle):
    def poll(self, session):
        session.bar_feed.poll()
        result = super().poll(session)
        session.runner.expire_due()
        return result


class HybridLiveReport(LiveRunReport):
    def write(self, *, status, fields, session, events):
        if session is not None:
            pd.DataFrame(session.strategy.signal_records).to_csv(
                self.path/'signals.csv', index=False, encoding='utf-8')
            pd.DataFrame(session.strategy.batch_records).to_csv(
                self.path/'batches.csv', index=False, encoding='utf-8')
            (self.path/'strategy_state.json').write_text(json.dumps(
                HybridCheckpoint(session.strategy).snapshot_state(), ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8')
        return super().write(status=status, fields=fields, session=session, events=events)


def build_runtime(args, *, query_timeout=15, max_session_orders=100,
                  max_notional=Decimal('1000000'), limit_offset_ticks=1,
                  execution_policy=None):
    """运行细项使用默认值；需要调整时在代码中显式传入。"""
    market_config = source_config(args)
    execution_policy = execution_policy or ExecutionPolicy('off', 'fills')
    if args.mode not in {'recording', 'simnow'}:
        raise ValueError('mode 仅支持 recording 或 simnow')
    if market_config.source not in {'ctp', 'dolphindb'}:
        raise ValueError('Hybrid实时行情仅支持 ctp 或 dolphindb')
    if args.mode == 'recording' and args.resume:
        raise ValueError('recording 模式不支持恢复交易会话')
    if execution_policy.position_source is not StrategyPositionSource.FILLS:
        raise ValueError('recording/simnow 模式须使用 fills 仓位来源')
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        raise ValueError('seconds 必须是有限正数')
    if not math.isfinite(query_timeout) or not 0 < query_timeout <= 120:
        raise ValueError('CTP查询超时须在0至120秒')
    if not 1 <= max_session_orders <= 10000 or limit_offset_ticks < 0:
        raise ValueError('报单上限或限价偏移无效')
    if not max_notional.is_finite() or max_notional <= 0:
        raise ValueError('名义上限须为有限正数')

    if args.mode == 'recording':
        lifecycle = HybridRecordingLifecycle(expected_trading_day=args.expected_trading_day,
            market_source=market_config.source)
    else:
        transport = build_simnow_transport(CtpTdApiTransport, client_id=CLIENT_ID,
            flow_path='/tmp/bomber-hybrid-td', timeout_seconds=query_timeout)
        lifecycle = HybridSessionLifecycle(transport,
            md_front=required('CTP_MD_ADDRESS') if market_config.source == 'ctp' else None,
            orders=True, max_session_orders=max_session_orders, expected_trading_day=args.expected_trading_day,
            resume=args.resume, account_reconciliation=execution_policy.account_reconciliation)

    def describe(session):
        if session is None:
            return {}
        strategy = session.strategy
        return {
            'bars_used': strategy.bars_used, 'last_prediction': strategy.last_prediction,
            'last_targets': strategy.last_targets,
            'active_batches': strategy.holdings.snapshot_state()['active'],
            'order_updates_received': strategy.order_updates_received,
            'fills_received': strategy.fills_received,
            'history': session.runner.history_service.last_audit,
        }

    report = HybridLiveReport(args.report_dir, prefix='hybrid-'+args.mode,
        metadata={'mode': args.mode, 'product': args.product, 'market_config': market_config.identity,
            'execution_policy': execution_policy.snapshot_state()}, describe=describe)

    def progress(session):
        strategy = session.strategy
        count = strategy.bars_used
        if count != getattr(session, '_reported_bars', -1):
            session._reported_bars = count
            print(f"Hybrid分钟: bars={count} prediction={strategy.last_prediction} "
                  f"targets={strategy.last_targets} active_batches={len(strategy.holdings.active)}", flush=True)

    return ManagedLiveRuntime(CLIENT_ID, controller=lifecycle,
        prepare_inputs=lambda context: prepare_inputs(args, context, lifecycle),
        assemble_session=lambda inputs, context: assemble(args, inputs, lifecycle,
            execution_policy=execution_policy, max_notional=max_notional,
            limit_offset_ticks=limit_offset_ticks),
        report=report, seconds=args.seconds, progress=progress)


def main(argv=None):
    load_dotenv()
    runtime = build_runtime(parse_args(argv))
    try:
        return runtime.run()
    finally:
        runtime.stop()


if __name__ == '__main__':
    main()
