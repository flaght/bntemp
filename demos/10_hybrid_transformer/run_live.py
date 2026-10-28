"""HybridTransformer Live 入口：接线并运行 Recording/SimNow。"""
from __future__ import annotations

import json
import math

import pandas as pd
from dotenv import load_dotenv

from bomber.framework.market.sources import MarketSourceConfig, source_config
from bomber.framework.trader.execution.ctp import CtpTdApiTransport
from bomber.framework.trader.execution.policy import (
    ExecutionPolicy, StrategyPositionSource, execution_policy_from_args,
)
from bomber.framework.trader.runtime.live.channels.ctp import (
    CtpSessionLifecycle, build_simnow_transport, required,
)
from bomber.framework.trader.runtime.live.channels.ctp_market import CtpMarketSessionLifecycle
from bomber.framework.trader.runtime.live.channels.ctp_recovery import (
    CtpResumeLifecycle, validate_ctp_restored_positions,
)
from bomber.framework.trader.runtime.live.local import RecordingExecutionLifecycle
from bomber.framework.trader.runtime.live.runtime import ManagedLiveRuntime
from bomber.framework.trader.runtime.reports import LiveRunReport

from .checkpoint import HybridCheckpoint
from .live_args import parse_args
from .live_session import CLIENT_ID, assemble, prepare_inputs


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


def build_runtime(
    product, mode, expected_trading_day, query_timeout, max_session_orders,
    resume, report_dir, *, market_config: MarketSourceConfig,
    execution_policy: ExecutionPolicy, seconds: float, prepare_inputs, assemble_session,
):
    """构建运行器，不在此处启动会话。

    回调约定：
        prepare_inputs(context, lifecycle) -> inputs
        assemble_session(inputs, lifecycle) -> session
    """
    if mode not in {'recording', 'simnow'}:
        raise ValueError('mode 仅支持 recording 或 simnow')
    if market_config.source not in {'ctp', 'dolphindb'}:
        raise ValueError('Hybrid实时行情仅支持 ctp 或 dolphindb')
    if mode == 'recording' and resume:
        raise ValueError('recording 模式不支持恢复交易会话')
    if execution_policy.position_source is not StrategyPositionSource.FILLS:
        raise ValueError('recording/simnow 模式须使用 fills 仓位来源')
    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('seconds 必须是有限正数')
    if not callable(prepare_inputs) or not callable(assemble_session):
        raise TypeError('prepare_inputs 和 assemble_session 必须是可调用函数')

    if mode == 'recording':
        lifecycle = HybridRecordingLifecycle(expected_trading_day=expected_trading_day,
            market_source=market_config.source)
    else:
        transport = build_simnow_transport(CtpTdApiTransport, client_id=CLIENT_ID,
            flow_path='/tmp/bomber-hybrid-td', timeout_seconds=query_timeout)
        lifecycle = HybridSessionLifecycle(transport,
            md_front=required('CTP_MD_ADDRESS') if market_config.source == 'ctp' else None,
            orders=True, max_session_orders=max_session_orders, expected_trading_day=expected_trading_day,
            resume=resume, account_reconciliation=execution_policy.account_reconciliation)

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

    report = HybridLiveReport(report_dir, prefix='hybrid-'+mode,
        metadata={'mode': mode, 'product': product, 'market_config': market_config.identity,
            'execution_policy': execution_policy.snapshot_state()}, describe=describe)

    def progress(session):
        strategy = session.strategy
        count = strategy.bars_used
        if count != getattr(session, '_reported_bars', -1):
            session._reported_bars = count
            print(f"Hybrid分钟: bars={count} prediction={strategy.last_prediction} "
                  f"targets={strategy.last_targets} active_batches={len(strategy.holdings.active)}", flush=True)

    return ManagedLiveRuntime(CLIENT_ID, controller=lifecycle,
        prepare_inputs=lambda context: prepare_inputs(context, lifecycle),
        assemble_session=lambda inputs, context: assemble_session(inputs, lifecycle),
        report=report, seconds=seconds, progress=progress)


def main(argv=None):
    load_dotenv()
    args = parse_args(argv)
    runtime = build_runtime(
        product=args.product,
        mode=args.mode,
        expected_trading_day=args.expected_trading_day,
        query_timeout=args.query_timeout,
        max_session_orders=args.max_session_orders,
        resume=args.resume,
        report_dir=args.report_dir,
        market_config=source_config(args),
        execution_policy=execution_policy_from_args(args),
        seconds=args.seconds,
        prepare_inputs=lambda context, lifecycle: prepare_inputs(args, context, lifecycle),
        assemble_session=lambda inputs, lifecycle: assemble(args, inputs, lifecycle),
    )
    try:
        return runtime.run()
    finally:
        runtime.stop()


if __name__ == '__main__':
    main()
