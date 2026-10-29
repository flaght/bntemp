"""Hybrid按日历窗口常驻；复用公共调度、CTP结转和原联合检查点。

正常休市结束当前连接，进程等待下个窗口。故障仍向外抛出，不自动重试。
"""
from contextlib import nullcontext
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.runtime.live.continuous import (
    ContinuousPositionRuntime, StopSignal, orders_for_day)
from bomber.framework.trader.runtime.live.local import strategy_state_lock


def validate_model_identity(previous, current):
    """只有交易日可向前变化，模型、信号文件和行情口径须保持一致。"""
    old, new = deepcopy(previous), deepcopy(current)
    old_day, new_day = old.pop('trading_day'), new.pop('trading_day')
    if old != new or old_day > new_day:
        raise ValueError('持续恢复不能改变模型、信号或行情口径，交易日不能倒退')


def _rebind_flat_route(payload, instrument):
    """主力变化只允许空仓续接；未到期批次绝不改成新合约。"""
    hybrid = payload['component_states']['hybrid']
    old_instruments = {r['instrument_id'] for ledger in payload['ctp_ledgers'].values()
                       for r in ledger['positions']}
    old_instruments |= {r['instrument_id'] for r in payload['portfolio']['known_keys']}
    if not old_instruments - {str(instrument)}:
        return
    positions = payload['positions']
    quantities = [r['quantity'] for name in ('account_positions', 'strategy_positions', 'working_quantities')
                  for r in positions[name]]
    quantities += [r['quantity'] for r in positions['attribution']['owned']]
    quantities += [q for row in payload['targets'] for q in row['targets'].values()]
    quantities += [r['quantity'] for rows in payload['portfolio']['contributions'].values() for r in rows]
    quantities += list(hybrid['last_targets'].values())
    quantities += [r[k] for ledger in payload['ctp_ledgers'].values() for r in ledger['positions']
                   for k in ('long_today', 'long_yesterday', 'short_today', 'short_yesterday')]
    if hybrid['holdings']['active'] or any(Decimal(q) for q in quantities):
        raise ValueError('主力已切换但旧合约仍有批次、持仓或目标；保留原检查点，不能自动换约')
    # 清理的是零仓路由，终结订单/成交归属审计仍完整保留。
    for ledger in payload['ctp_ledgers'].values():
        ledger['positions'] = []
    for name in ('account_positions', 'working_quantities'):
        positions[name] = []
    positions['attribution']['owned'] = []
    key = {'client_id': next(iter(payload['ctp_ledgers'])), 'instrument_id': str(instrument)}
    portfolio = payload['portfolio']
    portfolio['known_keys'] = [key] if portfolio['known_keys'] else []
    for sid, rows in portfolio['contributions'].items():
        portfolio['contributions'][sid] = [{'key': key, 'quantity': '0'}] if rows else []
    if hybrid['last_main'] is not None:
        hybrid['last_main'] = str(instrument).rsplit('.', 1)[0].lower()
    if hybrid['last_targets']:
        hybrid['last_targets'] = {str(instrument).rsplit('.', 1)[0].lower(): '0'}


def payload_for_day(session, payload, *, driver, transport, day):
    """暂存跨日状态；柜台核验通过后由RuntimeStateManager原子提交并备份。"""
    components = payload['component_states']
    current = session.manager.state_components['live_session'].snapshot_state()
    previous = components['live_session']
    validate_model_identity(previous['identity'], current['identity'])
    if previous['identity']['trading_day'] == day:
        return payload  # 同日恢复仍走原严格检查，不能隐式换路由。
    if current['identity']['trading_day'] != day:
        raise ValueError('跨日恢复的会话日期不一致')
    if components['session_rules'] != session.manager.state_components['session_rules'].snapshot_state():
        raise ValueError('持续恢复不能隐式改变交易日历、时段或日期例外')
    if components['hybrid']['config'] != session.manager.state_components['hybrid'].config():
        raise ValueError('持续恢复不能改变持仓周期、手数、合约乘数或策略配置')
    instrument = session.references.instrument_id
    _rebind_flat_route(payload, instrument)
    from bomber.framework.trader.runtime.live.channels.ctp_rollover import observe_rollover
    session.rollover_audit = observe_rollover(payload, driver=driver, transport=transport, day=day,
        multipliers={instrument: session.references.spec.multiplier},
        preserve_external=session.client.account_reconciliation.value != 'strict')
    components['live_session'] = current
    # holdings的opened/expires绝对时间一律不改；换日不会重新计算五分钟期限。
    return payload


def recording_snapshot(session):
    """Recording在进程内续接分析状态；没有账户/订单或可恢复交易状态文件。"""
    from .checkpoint import HybridCheckpoint
    return {'instrument': str(session.references.instrument_id),
            'hybrid': HybridCheckpoint(session.strategy).snapshot_state()}


def restore_recording(session, saved):
    from .checkpoint import HybridCheckpoint
    state = deepcopy(saved['hybrid'])
    instrument = str(session.references.instrument_id)
    if saved['instrument'] != instrument:
        if state['holdings']['active'] or any(Decimal(q) for q in state['last_targets'].values()):
            raise ValueError('Recording旧主力仍有未到期批次，不能直接换约')
        symbol = instrument.rsplit('.', 1)[0].lower()
        state['last_main'] = symbol if state['last_main'] else None
        state['last_targets'] = {symbol: '0'} if state['last_targets'] else {}
    HybridCheckpoint(session.strategy).restore_state(state)


class HybridWindowRuntime:
    """只组装一个日历窗口，不持有args/Namespace，不实现另一个策略或执行账本。"""
    def __init__(self, factory, calendar, window, stop_event, carry, *, mode, state_file,
                 max_session_orders, expected_trading_day=None, expected_source_day=None):
        self.factory, self.calendar, self.window, self.stop = factory, calendar, window, stop_event
        self.carry, self.mode, self.state_file = carry, mode, state_file
        self.max_orders = max_session_orders
        self.first_day, self.first_source_day = expected_trading_day, expected_source_day

    def run(self):
        day = self.window.trading_day
        source_day = self.calendar.previous_trading_day(day)
        if not self.carry.get('started'):
            if self.first_day is not None and self.first_day != day:
                raise ValueError('首个日历窗口交易日与expected-trading-day不一致')
            if self.first_source_day is not None and self.first_source_day != source_day:
                raise ValueError('首个窗口参考来源日与expected-source-day不一致')
        if self.stop.is_set():
            return {'status': 'completed'}
        resume, used = False, 0
        if self.mode == 'simnow':
            saved = JsonStateStore(self.state_file).load()
            resume = saved is not None
            used = orders_for_day(saved.payload, day) if saved else 0
            if used >= self.max_orders:
                raise RuntimeError('本交易日报单额度已用完；保留检查点，不以重建会话刷新额度')
        runtime = self.factory(expected_trading_day=day, expected_source_day=source_day,
            resume=resume, window=self.window, stop_event=self.stop,
            max_session_orders=self.max_orders-used,
            recording_state=self.carry.get('recording'), model_seed=self.carry.get('model'))
        result = runtime.run()
        if result.get('status') != 'passed':
            raise RuntimeError('Hybrid交易窗口停机核验失败，不进入下一窗口')
        session = runtime.session
        self.carry['started'] = True
        self.carry['instrument'] = str(session.references.instrument_id)
        self.carry['model'] = {
            'instrument': self.carry['instrument'],
            'identity': session.model_identity,
            'rows': deepcopy(list(session.strategy.predictor.workflow.history)),
        }
        if self.mode == 'recording':
            self.carry['recording'] = recording_snapshot(session)
        print(f'Hybrid交易窗口结束: TD={day} segment={self.window.segment_id} '
              f'model_window={len(self.carry["model"]["rows"])}；休市等待下一窗口', flush=True)
        return result


def run_continuous(factory, *, calendar, mode, state_file=None, max_session_orders=100,
                   expected_trading_day=None, expected_source_day=None, stop_event=None,
                   clock=None, wait=None):
    """复用08的公共日历调度；日期由窗口产生，TD登录仍负责权威核对。"""
    if mode == 'simnow' and state_file is None:
        raise ValueError('SimNow持续运行须提供同一个state-file')
    lock = (strategy_state_lock(str(Path(state_file).resolve())+'.continuous-owner')
            if state_file is not None else nullcontext())
    signal_scope = StopSignal() if stop_event is None else nullcontext(stop_event)
    with lock, signal_scope as stop:
        carry = {}
        runtime = ContinuousPositionRuntime(calendar,
            lambda window, event: HybridWindowRuntime(factory, calendar, window, event, carry,
                mode=mode, state_file=state_file, max_session_orders=max_session_orders,
                expected_trading_day=expected_trading_day, expected_source_day=expected_source_day),
            run_forever=True, stop_event=stop, clock=clock, wait=wait)
        return runtime.run()


def warm_model_seed(runner, seed, *, identity, max_bars, minimum, minute_ns):
    """合并同一模型、同一真实合约的已完成窗口；不补造断线分钟。"""
    if seed is None:
        return
    validate_model_identity(seed['identity'], identity)
    if seed['instrument'] != str(runner.bound_instrument):
        return  # 新合约只读自己的历史，不能拿旧主力分钟预热。
    import pandas as pd
    end = runner._clock_ns() // minute_ns * minute_ns
    expected = set(runner.calendar.last_minutes(end, max_bars))
    rows = {}
    for row in [*seed['rows'], *runner.strategy.predictor.workflow.history]:
        label = pd.Timestamp(row['trade_time'])
        if label.tzinfo is None:
            raise ValueError('持续模型窗口时间必须携带时区')
        stamp = label.value + minute_ns - 1  # Hybrid固定使用分钟开始标签。
        if stamp not in expected:
            continue
        if stamp in rows and rows[stamp] != row:
            raise ValueError(f'持续窗口与历史源同一分钟内容冲突: ts_event={stamp}')
        rows[stamp] = row
    count = runner.strategy.predictor.warm_history([rows[s] for s in sorted(rows)][-max_bars:])
    runner.history_last_ns = max(runner.history_last_ns, max(rows, default=-1))
    print(f'Hybrid休市窗口续接: model_window={count} min_raw_bars={minimum} '
          f'warmup_remaining={max(0, minimum-count)}；不重放历史交易', flush=True)
