"""Hybrid 批次与目标状态；接入框架 RuntimeStateManager 的同一检查点。

模型窗口由历史服务或完整实时分钟重新积累；这里只恢复持仓义务，不重放历史委托。
"""
from decimal import Decimal
from copy import deepcopy

from .holding import PeriodHoldings, completed_minute_ns


class HybridCheckpoint:
    def __init__(self, strategy):
        self.strategy = strategy

    def config(self):
        s, c = self.strategy, self.strategy.config
        result = dict(product=c.product, venue=c.venue, signal_id=c.signal_id,
                    hold_bars=c.hold_bars, max_contracts=c.max_contracts,
                    rebalance_minutes=c.rebalance_minutes, contract_multiplier=str(c.contract_multiplier),
                    target_key=c.target_key, target_keys=s.target_keys)
        if c.max_entry_contracts is not None:
            result['max_entry_contracts'] = c.max_entry_contracts
        return result

    def snapshot_state(self):
        s = self.strategy
        return dict(version=1, config=self.config(), holdings=s.holdings.snapshot_state(),
                    revision=s._revision, last_processed_ns=s.last_processed_ns,
                    bars_used=s.bars_used, last_main=s.last_main,
                    last_targets={k: str(v) for k, v in s.last_targets.items()},
                    targets_pending=s._targets_pending,
                    order_updates_received=s.order_updates_received, fills_received=s.fills_received)

    def restore_state(self, state):
        s = self.strategy
        if s.is_started:
            raise ValueError("须在策略启动前恢复 Hybrid 检查点")
        if state.get('version') != 1 or state.get('config') != self.config():
            raise ValueError("Hybrid 检查点版本、策略配置或执行路由不一致")
        ledger = PeriodHoldings(s.config.hold_bars, s.config.max_contracts, calendar=s.calendar,
                                max_entry_contracts=s.config.max_entry_contracts)
        ledger.restore_state(state['holdings'])
        counters = ('revision', 'bars_used', 'order_updates_received', 'fills_received')
        if any(type(state.get(k)) is not int or state[k] < 0 for k in counters):
            raise ValueError("Hybrid 检查点计数无效")
        stamp, main, pending = state['last_processed_ns'], state['last_main'], state['targets_pending']
        if (type(stamp) is not int or stamp < -1 or type(pending) is not bool
                or (stamp == -1) != (ledger.last_bar_ns == -1)
                or stamp >= 0 and completed_minute_ns(stamp) != ledger.last_bar_ns
                or state['bars_used'] > ledger.bar_index + 1
                or main is not None and (not isinstance(main, str) or not main)):
            raise ValueError("Hybrid 检查点行情位置无效")
        try:
            targets = {key: Decimal(value) for key, value in state['last_targets'].items()}
            if (any(not isinstance(key, str) or not key or not value.is_finite()
                    or value != value.to_integral_value() for key, value in targets.items())
                    or sum((abs(v) for v in targets.values()), Decimal(0)) > s.config.max_contracts
                    or s.target_keys is not None and any(k not in s.target_keys for k in targets)):
                raise ValueError("目标字段无效")
            expected = {}
            for batch in ledger.active:
                expected[batch.instrument] = expected.get(batch.instrument, Decimal(0)) + batch.quantity
            if ({k: v for k, v in targets.items() if v} != {k: v for k, v in expected.items() if v}
                    or bool(targets) != bool(state['revision'])):
                raise ValueError("已发布目标与批次账本不一致")
        except (AttributeError, TypeError, ArithmeticError) as exc:
            raise ValueError("Hybrid 检查点目标格式无效") from exc
        # 校验完成后一次性替换；框架负责同时恢复 TargetStore/账户/订单。
        s.restore_target_revision(state['revision'])
        s.holdings = ledger
        s.last_processed_ns, s.bars_used, s.last_main = stamp, state['bars_used'], main
        s.last_targets, s._targets_pending = targets, pending
        s.last_target = sum(targets.values(), Decimal(0)) if targets else None
        s.last_target_main = main if targets else None
        s.order_updates_received = state['order_updates_received']
        s.fills_received = state['fills_received']
        s.last_prediction = None

    def validate_targets(self, store):
        """恢复时由 RuntimeStateManager 对照统一目标仓位检查点。"""
        s = self.strategy
        target = store.get(s.strategy_id)
        routed = {(s.target_keys[k] if s.target_keys is not None else s.config.target_key): v
                  for k, v in s.last_targets.items()}
        if target is None:
            if s._revision or routed:
                raise ValueError("Hybrid 检查点缺少对应 TargetStore 状态")
        elif target.revision != s._revision or dict(target.targets) != routed:
            raise ValueError("Hybrid 检查点目标/版本与 TargetStore 不一致")


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
    return {'instrument': str(session.references.instrument_id),
            'hybrid': HybridCheckpoint(session.strategy).snapshot_state()}


def restore_recording(session, saved):
    state = deepcopy(saved['hybrid'])
    instrument = str(session.references.instrument_id)
    if saved['instrument'] != instrument:
        if state['holdings']['active'] or any(Decimal(q) for q in state['last_targets'].values()):
            raise ValueError('Recording旧主力仍有未到期批次，不能直接换约')
        symbol = instrument.rsplit('.', 1)[0].lower()
        state['last_main'] = symbol if state['last_main'] else None
        state['last_targets'] = {symbol: '0'} if state['last_targets'] else {}
    HybridCheckpoint(session.strategy).restore_state(state)


class RecordingWindowState:
    """Recording 的进程内批次状态；不创建交易账户检查点。"""
    def __init__(self, session):
        self.session = session

    def snapshot_state(self):
        return recording_snapshot(self.session)

    def restore_state(self, saved):
        restore_recording(self.session, saved)
