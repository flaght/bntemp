"""独立恢复旧配置，只平已到期仓位；不推理、不新开仓、不迁移手数。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import Decimal
import math
import os
from pathlib import Path
from types import SimpleNamespace

from bomber.framework.trader.execution.contracts import PositionEffect
from bomber.framework.trader.execution.policy import ExecutionPolicy
from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.runtime.ctp_account import position_buckets
from bomber.framework.trader.runtime.live.local import strategy_state_lock

from . import run_live as live
from .strategy import HybridTransformerConfig, HybridTransformerStrategy


class NoEntryPredictor:
    """满足现有Runner的窗口接口；始终不产生新批次信号。"""
    def __init__(self, signal_id):
        self.signal_id = signal_id
        self.workflow = SimpleNamespace(history=[], min_raw_bars=live.MODEL_MIN_RAW_BARS)

    def warm_history(self, rows):
        self.workflow.history = list(rows)[-live.MODEL_HISTORY_BARS:]
        return len(self.workflow.history)

    def on_bar(self, row):
        return None


class CloseOnlyPlanner:
    """在发送前拒绝OPEN腿；普通平仓继续由CTP今昨仓规划器处理。"""
    def __init__(self, planner):
        self.planner = planner

    def plan(self, request):
        orders = tuple(self.planner.plan(request))
        if any(order.position_effect is PositionEffect.OPEN or not order.reduce_only for order in orders):
            raise RuntimeError('旧仓恢复只允许平仓，禁止开仓委托')
        return orders


def validate_broker_positions(session, transport):
    """即使旧策略为off政策，此维护入口仍核对柜台管理合约的多空总仓和今昨桶。"""
    instrument = session.references.instrument_id
    saved = session.ledger.snapshot(instrument)
    expected = (saved.long_today, saved.long_yesterday, saved.short_today, saved.short_yesterday)
    gross = transport.query_gross_positions()
    actual = gross.get(str(instrument), (Decimal(0), Decimal(0)))
    if actual != (expected[0] + expected[1], expected[2] + expected[3]):
        raise RuntimeError(f'柜台与旧账本总仓不一致: instrument={instrument} broker={actual} ledger={expected}')
    rows = [row for row in transport.query_position_details()
            if f"{row['InstrumentID']}.{row['ExchangeID']}" == str(instrument)]
    buckets = position_buckets(rows, (instrument,))
    observed = buckets[str(instrument)].quantities if str(instrument) in buckets else (Decimal(0),) * 4
    if observed != expected:
        raise RuntimeError('柜台与旧账本今昨仓分桶不一致，保留检查点并停止恢复')
    return actual


class RecoveryLifecycle(live.HybridSessionLifecycle):
    def __init__(self, *args, saved, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = saved
        self.confirmed_flat = False

    def lock_state(self, resources):
        resources.enter_context(strategy_state_lock(str(Path(self.state_file).resolve()) + '.continuous-owner'))
        super().lock_state(resources)
        store = JsonStateStore(self.state_file)
        current = store.load()
        if current is None or current.generation != self.saved.generation or current.payload != self.saved.payload:
            raise RuntimeError('读取后旧检查点已变化，停止恢复，请重新启动维护入口')
        path = Path(self.state_file)
        backup = path.with_name(path.name + f'.before-expired-recovery-g{current.generation}.bak')
        data = path.read_bytes()
        if backup.exists():
            if backup.read_bytes() != data:
                raise RuntimeError('旧仓恢复备份内容冲突')
        else:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        print(f'旧仓恢复备份: {backup}', flush=True)

    def restore_session(self, session, context):
        old_day = self.saved.payload['component_states']['live_session']['identity']['trading_day']
        if old_day == context.trading_day:
            return session.manager.restore()
        from .checkpoint import payload_for_day
        return session.manager.restore(payload_transform=lambda payload: payload_for_day(
            session, payload, driver=self.driver, transport=self.transport, day=context.trading_day))

    def validate_restored_session(self, session, day):
        revision = super().validate_restored_session(session, day)
        validate_broker_positions(session, self.transport)
        return revision

    def poll(self, session):
        running = super().poll(session)
        states = session.client.order_state_machine.states()
        ledger = session.ledger.snapshot(session.references.instrument_id)
        if (not session.strategy.holdings.active and not ledger.long_total and not ledger.short_total
                and not session.strategy._targets_pending
                and all(quantity == 0 for quantity in session.strategy.last_targets.values())
                and not any(session.runner.position_manager.snapshot().working_quantities.values())
                and all(order.status.is_terminal for order in states)):
            validate_broker_positions(session, self.transport)
            if self.driver.reconcile_active_orders().orders:
                raise RuntimeError('旧仓恢复完成前柜台仍有活动订单')
            self.confirmed_flat = True
            print('旧批次已到期退出，柜台及本地账本均为空仓；不再开仓，结束维护会话', flush=True)
            return False
        return running

    def verify(self, session):
        if not self.confirmed_flat:
            raise RuntimeError('旧仓恢复尚未全部成交；保留原手数配置，不能执行新手数迁移')
        if session.runner.failure or session.client.report_errors:
            raise RuntimeError(session.runner.failure or '旧仓恢复执行回报异常')


def saved_config(payload):
    part = payload['component_states']['hybrid']
    config = deepcopy(part['config'])
    if config.pop('target_keys') is not None:
        raise ValueError('此维护入口仅用于原单合约固定路由')
    config['record_signals'] = True
    result = HybridTransformerConfig(**config)
    if result.product != 'RB' or result.venue != 'SHFE' or result.max_contracts > 10:
        raise ValueError('此维护入口仅恢复已核对的RB/SHFE、总上限不超过10手的旧检查点')
    return result


def build_runtime(*, state_file, release_path, directions_file, signal_config, market_config,
                  expected_trading_day, expected_source_day, seconds=600, report_dir=None,
                  no_session_overrides=False):
    if not math.isfinite(seconds) or not 0 < seconds <= 1800:
        raise ValueError('旧仓维护时间须为(0,1800]秒')
    state_file = Path(state_file)
    saved = JsonStateStore(state_file).load()
    if saved is None:
        raise FileNotFoundError(state_file)
    config = saved_config(saved.payload)
    from bomber.framework.trader.runtime.live.continuous import orders_for_day
    remaining_orders = 100 - orders_for_day(saved.payload, expected_trading_day)
    if remaining_orders <= 0:
        raise ValueError('本交易日报单额度已用完，旧仓恢复不能重置当日报单预算')
    policy_state = saved.payload['component_states'].get('execution_policy')
    policy = ExecutionPolicy('strict' if policy_state is None else policy_state['account_reconciliation'], 'fills')
    transport = live.build_simnow_transport(live.CtpTdApiTransport, client_id=live.CLIENT_ID,
        flow_path='/tmp/bomber-hybrid-recovery-td', timeout_seconds=15)
    lifecycle = RecoveryLifecycle(transport, saved=saved, orders=True, max_session_orders=remaining_orders,
        expected_trading_day=expected_trading_day, resume=True, state_file=state_file,
        account_reconciliation=policy.account_reconciliation,
        md_front=live.required('CTP_MD_ADDRESS') if market_config.source == 'ctp' else None)
    lifecycle.window = lifecycle.stop_event = None

    def assemble(inputs, context):
        strategy = HybridTransformerStrategy(live.CLIENT_ID, inputs.references,
            NoEntryPredictor(config.signal_id), config, calendar=inputs.calendar)
        session = live.assemble(inputs, lifecycle, product=config.product, release_path=Path(release_path),
            directions_file=Path(directions_file), signal_config=Path(signal_config), market_config=market_config,
            mode='simnow', max_contracts=config.max_contracts, state_file=state_file, resume=True,
            execution_policy=policy, strategy=strategy)
        session.client.planner = CloseOnlyPlanner(session.client.planner)
        # 决策闭闸期间也接收已校验的完整Bar参考价；授权后的首次到期平仓才有有效限价。
        # 这里只更新价格，不调用策略，也不重放旧目标。
        session.bar_feed.register_bar_handler(session.execution.prices.on_market_event)
        return session

    report = live.HybridLiveReport(report_dir or state_file.parent, prefix='hybrid-recover-expired',
        metadata={'mode': 'recover-expired-only', 'new_entries': False}, describe=lambda session: {} if session is None else {
            'last_targets': session.strategy.last_targets,
            'active_batches': session.strategy.holdings.snapshot_state()['active'],
            'broker_flat_confirmed': lifecycle.confirmed_flat})
    return live.ManagedLiveRuntime(live.CLIENT_ID, controller=lifecycle,
        prepare_inputs=lambda context: live.prepare_inputs(context, lifecycle, product=config.product,
            expected_source_day=expected_source_day, market_config=market_config,
            no_session_overrides=no_session_overrides), assemble_session=assemble, report=report, seconds=seconds)


def main(argv=None):
    live.load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('connect', 'enable-orders', 'confirm-simnow', 'no-session-overrides'):
        parser.add_argument('--' + name, action='store_true')
    for name in ('state-file', 'release-path', 'directions-file', 'signal-config', 'market-config'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--expected-trading-day', required=True)
    parser.add_argument('--expected-source-day', required=True)
    parser.add_argument('--seconds', type=float, default=600)
    args = parser.parse_args(argv)
    if not (args.connect and args.enable_orders and args.confirm_simnow):
        parser.error('维护报单须显式 --connect --enable-orders --confirm-simnow')
    for path in (args.state_file, args.release_path, args.directions_file, args.signal_config, args.market_config):
        if not path.exists():
            parser.error(f'文件或目录不存在: {path}')
    runtime = build_runtime(state_file=args.state_file, release_path=args.release_path,
        directions_file=args.directions_file, signal_config=args.signal_config,
        market_config=live.load_market_config(args.market_config),
        expected_trading_day=args.expected_trading_day, expected_source_day=args.expected_source_day,
        seconds=args.seconds, no_session_overrides=args.no_session_overrides)
    result = runtime.run()
    if result.get('status') != 'passed' or not runtime.controller.confirmed_flat:
        raise RuntimeError('旧仓恢复没有完成，不执行手数迁移')


if __name__ == '__main__':
    main()
