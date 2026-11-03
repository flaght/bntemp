"""Hybrid接入既有共享宿主/IPC；模型和五分钟批次使用同一套实现。"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bomber.framework.calendars import NativeCalendarProvider
from bomber.framework.trader.contracts import TargetPortfolio
from .checkpoint import HybridCheckpoint


def previous_source_day(day, calendar_id='china.sse'):
    point = datetime.strptime(day, '%Y%m%d').date()
    days = NativeCalendarProvider(calendar_id).snapshot(point-timedelta(days=30), point).days
    previous = [value for value in days if value < point]
    if not previous:
        raise ValueError('交易日历未提供前一交易日')
    return previous[-1].strftime('%Y%m%d')


class SharedHybridCheckpoint:
    """独立批次检查点；账户目标metadata留存同代副本，支持布局切换恢复。"""
    def __init__(self, session):
        self.session = session
        self.hybrid = HybridCheckpoint(session.strategy)
        self.identity = {'model': {key: value for key, value in session.model_identity.items()
                                  if key != 'trading_day'},
                         'calendar': session.runner.calendar.provenance['sha256'],
                         'config': self.hybrid.config()}

    def snapshot_state(self):
        return {'version': 1, 'identity': deepcopy(self.identity), 'hybrid': self.hybrid.snapshot_state(),
                'execution_cursor': getattr(self.session.client, 'cursor',
                    getattr(self.session.client, '_journal_cursor', 0))}

    def restore_state(self, state):
        port = self.session.client
        if hasattr(port.service, 'snapshot_state'):
            source = port.service.snapshot_state()['sources'].get(port.strategy_id)
            revision = source['revision'] if source else 0
            metadata = source.get('metadata', {}) if source else {}
        else:
            revision = port.state['source_revision']
            metadata = port.state.get('source_metadata', {})
        if state['hybrid']['revision'] < revision:
            state = metadata.get('hybrid_signal_state')
            if state is None or state['hybrid']['revision'] != revision:
                raise ValueError('共享账户已推进但缺少同代Hybrid批次；保留文件并要求核对')
        if (state.get('version') != 1 or state.get('identity') != self.identity
                or state['hybrid']['revision'] != revision):
            raise ValueError('共享Hybrid的模型、日历、参数或目标版本不一致')
        self.hybrid.restore_state(state['hybrid'])
        self.session.restored_signal_cursor = state.get('execution_cursor')
        strategy, runner = self.session.strategy, self.session.runner
        if strategy._revision:
            targets = {strategy.config.target_key: sum(strategy.last_targets.values())}
            runner.target_store.apply(TargetPortfolio(strategy.strategy_id, strategy._revision,
                strategy.last_processed_ns, targets))
        runner.hold_restored_target(strategy.strategy_id, strategy._revision)


def bind_shared_session(session, service):
    """宿主与worker都使用此函数，不持有TD或创建第二个账户检查点。"""
    runner = session.runner
    runner.shared_execution = True
    if (hasattr(session.client, "_events") and service.event_journal is not None
            and not getattr(session.strategy.predictor, "execution_only", False)):
        session.client._consume_journal = True
        session.client._journal_cursor = service.event_journal.ack[session.strategy.strategy_id]
    checkpoint = SharedHybridCheckpoint(session)
    session.signal_checkpoint = runner.shared_signal = checkpoint
    session.session_identity = deepcopy(checkpoint.identity)
    session.market_source_identity = session.model_identity['market']
    session.restore_signal = checkpoint.restore_state
    session.bind_signal_checkpoint = session.strategy.bind_checkpoint
    events = []
    session.client.register_execution_event_handler(events.append)
    session.shared_events = events
    session.shared_report = None

    def poll():
        session.bar_feed.poll()
        if runner.accept_bars:
            runner.expire_due()
        if session.shared_report is not None:
            session.shared_report.record(session, events)
    runner._shared_poll = poll

    def finish():
        if session.shared_report is not None:
            session.shared_report.write(status='failed' if runner.failure else 'completed',
                fields={'failure': runner.failure, 'strategy_id': session.strategy.strategy_id},
                session=session, events=events)
            session.shared_report = None
    session.finish_report = finish


def configure_report(session, report_dir):
    from .run_live import HybridLiveReport
    session.shared_report = HybridLiveReport(Path(report_dir),
        prefix=f'hybrid-simnow-{session.strategy.config.product}',
        trading_day=session.references.trading_day.strftime('%Y%m%d'),
        metadata={'mode': 'simnow', 'execution': 'shared',
                  'strategy_id': session.strategy.strategy_id,
                  'product': session.strategy.config.product},
        describe=lambda current: {} if current is None else {
            'bars_used': current.strategy.bars_used,
            'last_prediction': current.strategy.last_prediction,
            'max_contracts': current.strategy.config.max_contracts,
            'entry_contracts': current.strategy.config.max_entry_contracts,
            'last_targets': current.strategy.last_targets,
            'active_batches': current.strategy.holdings.snapshot_state()['active'],
            'fills_received': current.strategy.fills_received,
            'order_updates_received': current.strategy.order_updates_received,
            'model_window_bars': len(current.strategy.predictor.workflow.history),
            'history': current.runner.history_service.last_audit})
    session.shared_report.begin()


class ExecutionOnlyPredictor:
    """账户server只声明合约/风险，不加载模型，不生成信号。"""
    execution_only = True
    def __init__(self):
        from types import SimpleNamespace
        from .run_live import MODEL_MIN_RAW_BARS
        self.workflow = SimpleNamespace(history=[], min_raw_bars=MODEL_MIN_RAW_BARS)
    def on_bar(self, row):
        raise RuntimeError('账户服务不运行Hybrid模型')


def execution_only_strategy(references, calendar, *, product, signal_config,
                            max_contracts, entry_contracts, strategy_id):
    from .strategy import HybridTransformerConfig, HybridTransformerStrategy
    from .signal_config import load_product_signal
    from .run_live import REBALANCE_MINUTES
    selected = load_product_signal(signal_config, product)
    config = HybridTransformerConfig(product=product, venue=references.spec.venue,
        signal_id=selected.signal_id, max_contracts=max_contracts, hold_bars=selected.hold_bars,
        rebalance_minutes=REBALANCE_MINUTES, contract_multiplier=references.spec.multiplier,
        target_key=f'{product.lower()}_main', max_entry_contracts=entry_contracts, record_signals=True)
    return HybridTransformerStrategy(strategy_id, references, ExecutionOnlyPredictor(), config, calendar=calendar)


def import_native_payload(account, payload):
    """原Hybrid账户文件原地导入共享执行；保留订单和批次，柜台核验后才提交。"""
    from .checkpoint import validate_model_identity
    from bomber.framework.trader.runtime.live.channels.ctp_owned import validate_saved_ownership
    from bomber.framework.trader.runtime.live.channels.ctp_rollover import observe_rollover
    components = payload['component_states']
    original = components['hybrid']
    matches = [session for session in account.hybrid_sessions.values()
               if session.strategy.config.product == original['config']['product']]
    if len(matches) != 1 or not account.preserve_external:
        raise ValueError('原Hybrid导入须唯一对应原品种，并显式使用preserve_external归属政策')
    session = matches[0]
    sid = session.strategy.strategy_id
    if set(payload['ctp_drivers']) != {account.service.client_id}:
        raise ValueError('原Hybrid导入须保持原CTP客户端ID')
    owned_ids = {row['strategy_id'] for row in payload['positions']['attribution']['owned']}
    if owned_ids - {sid} or set(payload['portfolio']['contributions']) - {sid}:
        raise ValueError('原Hybrid导入须保持原策略ID，不能改写已有仓位归属')
    if original['config'] != session.signal_checkpoint.hybrid.config():
        raise ValueError('原Hybrid导入不能同时改变手数、信号、周期或路由')
    validate_model_identity(components['live_session']['identity'], session.model_identity)
    if components['session_rules']['fingerprint'] != session.runner.calendar.provenance['sha256']:
        raise ValueError('原Hybrid导入不能改变交易日历或时段')
    ledger = validate_saved_ownership(payload, account.service.client_id)
    if not set(ledger.positions) <= set(account.service.instrument_limits):
        raise ValueError('原Hybrid账本包含本次未声明的合约')
    if ledger.trading_day != account.expected_day:
        account.rollover_audit = observe_rollover(payload, driver=account.driver, transport=account.transport,
            day=account.expected_day, multipliers={i: limit.contract_multiplier
                for i, limit in account.service.instrument_limits.items()}, scope=account.position_scope,
            preserve_external=True, generic_close_venues=account.generic_close_venues)
    current = account.manager.state_components['session_identity'].identity
    shared = account.service.snapshot_state()
    shared.update(portfolio_revision=payload['portfolio']['revision'],
        strategy_revisions=deepcopy(payload['portfolio']['strategy_revisions']),
        known_instruments=sorted(str(i) for i in account.service.instrument_limits))
    wrapper = session.signal_checkpoint.snapshot_state()
    wrapper['hybrid'] = deepcopy(original)
    if original['revision']:
        instrument = str(session.references.instrument_id)
        if any(batch['instrument'] != instrument.rsplit('.', 1)[0].lower()
               for batch in original['holdings']['active']):
            raise ValueError('原Hybrid未到期批次不能迁移至新合约')
        quantity = str(sum((Decimal(q) for q in original['last_targets'].values()),
                           Decimal(0)))
        shared['sources'][sid] = dict(revision=original['revision'], ts_event=original['last_processed_ns'],
            targets={instrument: quantity}, rows=[dict(strategy_id=sid,
                target_key=session.strategy.config.target_key, instrument_id=instrument, quantity=quantity)],
            deadline_ns=None, execution_policy='DIRECT', metadata={'hybrid_signal_state': wrapper})
    shared['shared']['bootstrap_signals'] = {key: value.signal_checkpoint.snapshot_state()
        for key, value in account.hybrid_sessions.items()}
    shared['shared']['bootstrap_signals'][sid] = wrapper
    payload['component_states'] = {'shared_account': shared,
        'session_identity': {'version': 1, 'identity': current},
        'external_positions': {'version': 1,
            'managed_instruments': sorted(account.position_scope.instruments), 'gross': {}, 'buckets': {}}}
    payload['portfolio']['known_keys'] = [dict(client_id=account.service.client_id, instrument_id=str(i))
        for i in sorted(account.service.instrument_limits, key=str)]
    return payload
