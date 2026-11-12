"""按操作者确认的柜台仓位重建Hybrid直连恢复基准，并归档未知旧单。"""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import argparse
import hashlib
import json
import time

from bomber.framework.trader import persistence as storage
from bomber.framework.trader.portfolio import PortfolioCoordinator
from bomber.framework.trader.runtime.ctp_account import position_buckets
from bomber.framework.trader.runtime.live.local import strategy_state_lock
from bomber.framework.trader.runtime.live.channels.ctp_owned import validate_saved_ownership
from bomber.framework.trader.runtime.live.channels.ctp_maintenance import _write_new
from .flat_state import flat_business_state

DEMO = Path(__file__).resolve().parent
BUCKETS = ('long_today', 'long_yesterday', 'short_today', 'short_yesterday')
TERMINAL = {'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'}


def parse_position(value):
    instrument, separator, text = value.partition('=')
    try:
        quantity = Decimal(text)
        if not separator or '.' not in instrument or not quantity.is_finite() or quantity != quantity.to_integral_value():
            raise ValueError
    except (ValueError, ArithmeticError):
        raise argparse.ArgumentTypeError('仓位须写为 p2701.DCE=2，正数多仓、负数空仓、0为空仓') from None
    return instrument, quantity


def observe_positions(transport, expected, multipliers):
    """验证多空总仓、今昨数量、成本及活动委托，返回同一管理范围的基准。"""
    canonical = {key.casefold(): key for key in expected}
    gross = {}
    for instrument, pair in transport.query_gross_positions().items():
        key = canonical.get(str(instrument).casefold())
        if key:
            previous = gross.get(key, (Decimal(0), Decimal(0)))
            gross[key] = tuple(previous[i] + Decimal(str(q)) for i, q in enumerate(pair))
    for instrument, quantity in expected.items():
        actual = gross.get(instrument, (Decimal(0), Decimal(0)))
        wanted = (max(quantity, Decimal(0)), max(-quantity, Decimal(0)))
        if actual != wanted:
            raise RuntimeError(f'柜台仓位与声明不一致: {instrument} 实际多空={actual} 声明多空={wanted}')
    if any(str(order.instrument_id).casefold() in canonical for order in transport.query_active_orders().orders):
        raise RuntimeError('管理合约仍有活动委托，请先处理后重新核对')
    rows = []
    quantities = {key: [Decimal(0)] * 4 for key in expected}
    costs = {key: [Decimal(0)] * 4 for key in expected}
    for raw in transport.query_position_details():
        key = canonical.get(f"{raw['InstrumentID']}.{raw['ExchangeID']}".casefold())
        if key is None or not Decimal(str(raw['Position'])):
            continue
        row = dict(raw, InstrumentID=key.rsplit('.', 1)[0], ExchangeID=key.rsplit('.', 1)[1])
        parsed = position_buckets([row], expected)[key].quantities
        cost = Decimal(str(row['PositionCost']))
        multiplier = Decimal(str(multipliers[key]))
        if not cost.is_finite() or cost <= 0 or not multiplier.is_finite() or multiplier <= 0:
            raise ValueError(f'柜台持仓成本或合约乘数无效: {key}')
        # 聚合市场以当前总持仓成本建立加权基准，不还原旧单成交价。
        unit_cost = cost / Decimal(str(row['Position']))
        for index, amount in enumerate(parsed):
            quantities[key][index] += amount
            costs[key][index] += amount * unit_cost
        rows.append({name: str(value) for name, value in row.items()})
    bases = {}
    for key, q in quantities.items():
        if (q[0] + q[1], q[2] + q[3]) != gross.get(key, (0, 0)):
            raise RuntimeError(f'柜台总仓与今昨明细不一致: {key}')
        bases[key] = [costs[key][i] / amount / Decimal(str(multipliers[key])) if amount else Decimal(0)
                      for i, amount in enumerate(q)]
    return {'quantities': quantities, 'bases': bases, 'raw_positions': rows}


def aligned_signal(payload, sid, instrument, quantity, local_signal, revision, cursor):
    source = payload['component_states']['shared_account']['sources'][sid]
    candidates = [local_signal, source.get('metadata', {}).get('hybrid_signal_state')]
    for driver in payload['ctp_drivers'].values():
        candidates.extend(row['intent'].get('metadata', {}).get('hybrid_signal_state')
                          for row in driver['orders'] if row['intent']['strategy_id'] == sid)
    candidates = [state for state in candidates if isinstance(state, dict) and state.get('version') == 1
                  and isinstance(state.get('hybrid'), dict)]
    if not candidates:
        raise ValueError(f'缺少原Hybrid批次副本: {sid}')
    latest = max(candidates, key=lambda state: state['hybrid']['last_processed_ns'])
    candidates = [state for state in candidates if state['identity'] == latest['identity']]
    symbol = instrument.rsplit('.', 1)[0].lower()
    if quantity:
        matching = []
        for state in candidates:
            hybrid = state['hybrid']
            batches = hybrid['holdings']['active']
            targets = {key.lower(): Decimal(value) for key, value in hybrid['last_targets'].items() if Decimal(value)}
            if (targets == {symbol: quantity} and batches
                    and all(row['instrument'].lower() == symbol for row in batches)
                    and sum((Decimal(row['quantity']) for row in batches), Decimal(0)) == quantity):
                matching.append(state)
        if not matching:
            raise ValueError(f'{sid}缺少与当前仓位{quantity}一致的原批次，无法保留其原到期时间')
        result = deepcopy(max(matching, key=lambda state: state['hybrid']['last_processed_ns']))
        result['hybrid'].update(revision=revision, targets_pending=False)
    else:
        result = deepcopy(latest)
        result['hybrid'] = flat_business_state(result['hybrid'], revision, [instrument])
    for name in ('fills_received', 'order_updates_received'):
        result['hybrid'][name] = max(state['hybrid'][name] for state in candidates)
    result.pop('accepted_state', None)
    result['execution_cursor'] = cursor
    return result


def prepare_alignment(payload, expected, observed, *, trading_day, signals, operation_id):
    result = deepcopy(payload)
    components = result['component_states']
    shared = components['shared_account']
    client, driver = next(iter(result['ctp_drivers'].items()))
    if len(result['ctp_drivers']) != 1 or set(result['orders']) != {client} or set(result['ctp_ledgers']) != {client}:
        raise ValueError('仅支持唯一执行客户端的直连检查点')
    if trading_day < driver['trading_day']:
        raise ValueError('仓位对齐不能回退交易日')
    routes = shared['shared']['routes']
    if shared['shared'].get('execution_mode') != 'independent' or set(routes) != set(signals):
        raise ValueError('须提供本直连进程的完整Hybrid实例声明')
    mapping = {}
    for sid, declaration in routes.items():
        if len(declaration['routes']) != 1:
            raise ValueError('仅支持每个Hybrid实例一个合约路由')
        target, instrument = next(iter(declaration['routes'].items()))
        if instrument in mapping:
            raise ValueError('多个策略管理同一合约，须明确各自仓位归属')
        mapping[instrument] = (sid, target)
    if set(expected) != set(mapping):
        raise ValueError(f'须明确声明全部管理合约仓位: {sorted(mapping)}')
    unknown = {row['client_order_id'] for row in result['orders'][client] if row['status'] not in TERMINAL}
    unknown.update(row['client_order_id'] for row in driver['orders'])
    unknown.update(components['strategy_buckets']['frozen'])
    records = components['ctp_order_owners']['records']
    unknown.update(row['client_order_id'] for row in records.values() if not row['terminal'])
    if any(row['strategy_id'] not in routes or row['instrument_id'] not in expected
           for row in records.values() if row['client_order_id'] in unknown):
        raise ValueError('待归档订单包含声明范围之外的策略或合约')
    plans = result['positions']['attribution']['orders']
    retired_plans = {key: row for key, row in plans.items()
                     if row['client_order_id'] in unknown or not row['released']
                     and any(Decimal(leg['filled']) < Decimal(leg['quantity']) for leg in row['legs'])}
    evidence = {'operation_id': operation_id, 'old_order_terminal_status': 'UNKNOWN',
        'historical_fills_reconstructed': False, 'historical_pnl_reconstructed': False,
        'cost_basis_source': 'broker_position_cost_weighted_average',
        'archived_order_states': [row for row in payload['orders'][client] if row['client_order_id'] in unknown],
        'archived_driver_orders': deepcopy(driver['orders']), 'archived_attribution_plans': deepcopy(retired_plans),
        'archived_order_owners': {key: row for key, row in records.items() if row['client_order_id'] in unknown},
        'archived_frozen': deepcopy(components['strategy_buckets']['frozen']),
        'previous_strategy_positions': deepcopy(payload['positions']['strategy_positions'])}
    result['orders'][client] = [row for row in result['orders'][client] if row['client_order_id'] not in unknown]
    driver.update(orders=[], trading_day=trading_day)
    components['ctp_order_owners']['records'] = {key: row for key, row in records.items() if row['client_order_id'] not in unknown}
    result['positions']['attribution']['orders'] = {key: row for key, row in plans.items() if key not in retired_plans}
    positions = result['positions']
    positions.update(strategy_positions=[], account_positions=[], working_quantities=[], strategy_revisions=[])
    positions['attribution']['owned'] = []
    positions['recovery_required_clients'] = [key for key in positions['recovery_required_clients'] if key != client]
    ledger = result['ctp_ledgers'][client]
    ledger.update(trading_day=trading_day, positions=[])
    buckets = components['strategy_buckets']
    buckets.update(trading_day=trading_day, positions=[], frozen={})
    journal = shared['shared'].get('event_journal')
    targets = {row['strategy_id']: row for row in result['targets']}
    for instrument, (sid, target) in mapping.items():
        quantity = expected[instrument]
        source = shared['sources'][sid]
        revision = max(source['revision'], signals[sid]['revision'] if signals[sid] else 0) + 1
        cursor = len(journal['events'][sid]) if journal else 0
        signal = aligned_signal(payload, sid, instrument, quantity,
                                signals[sid]['signal'] if signals[sid] else None, revision, cursor)
        if abs(quantity) > signal['hybrid']['config']['max_contracts']:
            raise ValueError(f'当前仓位超过策略总上限: {sid}')
        source.update(revision=revision, targets={instrument: str(quantity)}, deadline_ns=None)
        source['rows'] = [dict(strategy_id=sid, target_key=target, instrument_id=instrument, quantity=str(quantity),
                              source_revision=revision, source_deadline_ns=None)]
        source['metadata'].update(hybrid_signal_state=deepcopy(signal),
            checkpoint_alignment={'revision': revision, 'signal': deepcopy(signal)},
            operator_position_alignment=operation_id, gross_contracts=str(abs(quantity)))
        shared['shared'].setdefault('bootstrap_signals', {})[sid] = deepcopy(signal)
        if journal:
            journal['ack'][sid] = cursor
        positions['strategy_positions'].append(dict(strategy_id=sid, target_key=target, quantity=str(quantity)))
        positions['strategy_revisions'].append(dict(strategy_id=sid, target_key=target, revision=revision))
        key = dict(client_id=client, instrument_id=instrument)
        positions['account_positions'].append(dict(key=key, quantity=str(quantity)))
        positions['attribution']['owned'].append(dict(**key, strategy_id=sid, target_key=target, quantity=str(quantity)))
        result['portfolio']['contributions'][sid] = [dict(key=key, quantity=str(quantity))]
        result['portfolio']['strategy_revisions'][sid] = revision
        shared['strategy_revisions'][sid] = revision
        previous = targets.get(sid, {})
        targets[sid] = dict(strategy_id=sid, revision=revision, ts_event=source['ts_event'],
            targets={target: str(quantity)}, execution_policy=previous.get('execution_policy', 'DIRECT'),
            deadline_ns=None, metadata=deepcopy(source['metadata']))
        q, bases = observed['quantities'][instrument], observed['bases'][instrument]
        if any(q):
            ledger['positions'].append(dict(instrument_id=instrument,
                **{name: str(q[i]) for i, name in enumerate(BUCKETS)},
                **{name+'_basis': str(bases[i]) for i, name in enumerate(BUCKETS)}))
        buckets['positions'].append(dict(strategy_id=sid, instrument_id=instrument, quantities=list(map(str, q))))
    result['targets'] = list(targets.values())
    result['portfolio']['revision'] += 1
    shared.update(portfolio_revision=result['portfolio']['revision'], pending=False, failed=False)
    shared['shared'].update(suspended=sorted(routes), draining=[], execution_stop=None)
    external = components['external_positions']
    for name in ('gross', 'buckets'):
        external[name] = {key: row for key, row in external[name].items() if key not in expected}
    external['version'] = 2
    validate_saved_ownership(result, client)
    PortfolioCoordinator().restore(storage._decode_portfolio(result['portfolio']))
    from bomber.framework.trader.execution.ctp.ledger import CtpPositionLedger
    CtpPositionLedger().restore(storage._decode_ctp_ledger(ledger))
    from bomber.framework.trader.execution.ctp.independent import StrategyBuckets, OrderIdentityBook
    StrategyBuckets(trading_day).restore_state(buckets)
    book = OrderIdentityBook(driver['broker_id'], driver['investor_id'])
    book.day = trading_day
    book.restore_state(components['ctp_order_owners'])
    storage._decode_targets(result['targets'])
    evidence.update(aligned_positions={key: str(value) for key, value in expected.items()},
                    observed_trading_day=trading_day, previous_trading_day=payload['ctp_drivers'][client]['trading_day'])
    return result, evidence


def reconcile(*, state_file, rows, expected, transport):
    from bomber.framework.trader.runtime.live.channels.ctp_independent import IndependentCtpLifecycle
    path = Path(state_file).resolve()
    with ExitStack() as resources:
        resources.enter_context(strategy_state_lock(str(path) + '.direct-owner'))
        resources.enter_context(strategy_state_lock(path))
        for row in rows:
            resources.enter_context(strategy_state_lock(row['state_file'] + '.worker-owner'))
            resources.enter_context(strategy_state_lock(row['state_file']))
        IndependentCtpLifecycle.acquire(resources, transport, environment='realtime',
                                        strategy_ids=[row['strategy_id'] for row in rows])
        store = storage.JsonStateStore(path)
        saved = store.load()
        if saved is None:
            raise FileNotFoundError(path)
        client, driver = next(iter(saved.payload['ctp_drivers'].items()))
        if (transport.broker_id, transport.investor_id, transport.account_id) != (
                '9999', driver['investor_id'], driver['account_id']) or driver['broker_id'] != '9999':
            raise ValueError('连接账户与原检查点不一致，当前入口仅支持SimNow')
        declarations = saved.payload['component_states']['shared_account']['shared']['routes']
        multipliers = {instrument: limits['contract_multiplier'] for row in declarations.values()
                       for instrument, limits in row['limits'].items()}
        canonical = {key.casefold(): key for row in declarations.values() for key in row['routes'].values()}
        expected = {canonical.get(key.casefold(), key): value for key, value in expected.items()}
        if set(expected) != set(canonical.values()):
            raise ValueError(f'仓位声明须覆盖原路由: {sorted(canonical.values())}')
        signals = {}
        for row in rows:
            saved_signal = storage.JsonStateStore(Path(row['state_file'])).load()
            signals[row['strategy_id']] = saved_signal.payload if saved_signal else None
        try:
            session = transport.connect(lambda *_: None, lambda *_: None, lambda *_: None)
            transport.activate()
            observed = observe_positions(transport, expected, multipliers)
            second = observe_positions(transport, expected, multipliers)
            if observed != second:
                raise RuntimeError('柜台仓位或成本在核验期间发生变化，请重新核对')
            operation_id = f'g{saved.generation}-{time.time_ns()}'
            candidate, evidence = prepare_alignment(saved.payload, expected, observed,
                trading_day=session.trading_day, signals=signals, operation_id=operation_id)
            original = path.read_bytes()
            backup = path.with_name(path.name + f'.before-position-alignment-{operation_id}.bak')
            audit = path.with_name(path.name + f'.position-alignment-{operation_id}.json')
            evidence.update(backup_file=str(backup), original_sha256=hashlib.sha256(original).hexdigest(),
                            raw_broker_positions=observed['raw_positions'], orders_submitted=0,
                            previous_generation=saved.generation)
            _write_new(backup, original)
            _write_new(audit, (json.dumps(evidence, ensure_ascii=False, indent=2) + '\n').encode())
            updated = store.save(candidate, expected_generation=saved.generation)
            print(f'直连仓位基准已对齐: {evidence["aligned_positions"]} generation={updated.generation}')
            print(f'备份: {backup}\n审计: {audit}')
            print('旧单终态按UNKNOWN归档，历史成交和盈亏未补算。原批次期限保留，恢复后按有效参考价处理到期仓位。')
            return updated
        finally:
            transport.close()


def main(argv=None):
    from bomber.framework.cli import load_environment
    from bomber.framework.trader.runtime.live.remote import read_strategy_rows
    from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport
    from bomber.framework.trader.execution.ctp import CtpTdApiTransport
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-file', type=Path, default=DEMO/'results/direct/state.json')
    parser.add_argument('--config', type=Path, default=DEMO/'config/instances_rb_p.json')
    parser.add_argument('--env-file', type=Path, default=DEMO/'config/.env')
    parser.add_argument('--position', type=parse_position, action='append', required=True)
    parser.add_argument('--confirm-broker-positions', action='store_true')
    args = parser.parse_args(argv)
    if not args.confirm_broker_positions or len(dict(args.position)) != len(args.position):
        parser.error('须明确confirm-broker-positions，并为每个管理合约声明一次仓位')
    rows = read_strategy_rows(args.config)
    if any(row['kind'] != 'hybrid_transformer' for row in rows):
        parser.error('此入口仅处理Hybrid直连进程')
    saved = storage.JsonStateStore(args.state_file).load()
    if saved is None:
        raise FileNotFoundError(args.state_file)
    client = next(iter(saved.payload['ctp_drivers']))
    load_environment(args.env_file)
    transport = build_simnow_transport(CtpTdApiTransport, client_id=client,
        flow_path='/tmp/bomber-hybrid-position-alignment')
    return reconcile(state_file=args.state_file, rows=rows, expected=dict(args.position), transport=transport)


if __name__ == '__main__':
    main()
