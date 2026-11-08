"""Hybrid空仓后的业务状态；账户、订单及持久化由公共维护模块处理。"""
from copy import deepcopy
import re

from bomber.framework.trader.runtime.live.channels.ctp_maintenance import align_flat_execution
from bomber.framework.trader.runtime.live.channels.ctp_owned import validate_saved_ownership


def managed_instruments(payload):
    state = payload['component_states']['hybrid']
    config = state['config']
    product, venue = config['product'], config['venue']
    values = list(state['last_targets']) + [row['instrument'] for row in state['holdings']['active']]
    if state['last_main']:
        values.append(state['last_main'])
    for ledger in payload['ctp_ledgers'].values():
        values.extend(row['instrument_id'] for row in ledger['positions'])
    values.extend(row['key']['instrument_id'] for name in ('account_positions', 'working_quantities')
                  for row in payload['positions'][name])
    values.extend(row['instrument_id'] for row in payload['positions']['attribution']['owned'])
    values.extend(row['instrument_id'] for row in payload['portfolio']['known_keys'])
    if not values:
        values.extend(row['instrument_id'] for rows in payload['orders'].values() for row in rows)
    result = set()
    for value in values:
        match = re.fullmatch(r'(' + re.escape(product) + r'[0-9]+)(?:\.' + re.escape(venue) + r')?', str(value), re.I)
        if match is None:
            raise ValueError(f'检查点包含非{product}合约，不能清理其状态: {value}')
        result.add(match[1].lower() + '.' + venue)
    if not result:
        raise ValueError('检查点缺少具体合约，不能猜测对齐范围')
    return frozenset(result)


def flat_business_state(state, revision, instruments, *, target_config=None):
    result = deepcopy(state)
    config = result['config']
    if any(type(state[name]) is not int or state[name] < 0
           for name in ('revision','bars_used','order_updates_received','fills_received')):
        raise ValueError('原Hybrid业务计数无效')
    holdings = state['holdings']
    if (holdings.get('version') != 1 or holdings['holding_bars'] != config['hold_bars']
            or holdings['max_contracts'] != config['max_contracts']
            or holdings.get('max_entry_contracts') != config.get('max_entry_contracts')):
        raise ValueError('原批次账本与手数或周期配置不一致')
    if result['version'] != 1 or config['target_keys'] is not None:
        raise ValueError('只支持单品种Hybrid业务状态')
    sizes = ('max_contracts', 'max_entry_contracts')
    if target_config is not None:
        if ({k:v for k,v in config.items() if k not in sizes}
                != {k:v for k,v in target_config.items() if k not in sizes}):
            raise ValueError('空仓对齐不能改变信号、周期、乘数或路由')
        config = result['config'] = dict(target_config)
    cap, entry = config['max_contracts'], config.get('max_entry_contracts')
    if (type(cap) is not int or cap < 1 or entry is not None
            and (type(entry) is not int or not 1 <= entry <= cap)):
        raise ValueError('目标总手数及每批手数无效')
    result['holdings'].update(active=[], max_contracts=cap, max_entry_contracts=entry)
    result.update(revision=revision, last_targets={i.split('.')[0].lower():'0' for i in instruments} if revision else {},
                  targets_pending=False)
    return result


def prepare_native_payload(payload, *, trading_day, target_config=None):
    client, driver = next(iter(payload['ctp_drivers'].items()))
    old = payload['component_states']['hybrid']
    if trading_day != driver['trading_day'] or payload['component_states']['live_session']['identity']['trading_day'] != trading_day:
        raise ValueError('此维护操作须在原检查点同一交易日执行')
    instruments = managed_instruments(payload)
    result, evidence = align_flat_execution(payload, strategy_id=client, instruments=instruments)
    if old['revision'] != evidence['new_revision']:
        raise ValueError('原策略目标版本与执行账本不一致')
    result['component_states']['hybrid'] = flat_business_state(old, evidence['new_revision'], instruments,
                                                              target_config=target_config)
    evidence.update(previous_targets=old['last_targets'], previous_batches=old['holdings']['active'])
    return result, evidence


def aligned_payload(payload, *, trading_day, target_config=None):
    return prepare_native_payload(payload, trading_day=trading_day, target_config=target_config)[0]


def align_shared_signal(signal, revision, routes, source, *, signal_revision=None):
    reference = source.get('metadata', {}).get('hybrid_signal_state')
    aligned = source.get('metadata', {}).get('checkpoint_alignment')
    if aligned and aligned['revision'] == source['revision']:
        reference = aligned['signal']
    if (signal.get('version') != 1 or reference is not None and signal['identity'] != reference['identity']):
        raise ValueError('账户和信号检查点身份不一致')
    if reference is not None and signal['hybrid']['revision'] < reference['hybrid']['revision']:
        signal = reference
    if signal_revision is not None and signal_revision < source['revision'] and reference is None:
        raise ValueError('原业务文件落后且缺少账户同代副本，不能重建业务状态')
    state = signal['hybrid']
    config = state['config']
    if (signal['identity']['config'] != config or len(routes) != 1 or config['target_key'] not in routes
            or config['target_keys'] is not None):
        raise ValueError('业务状态与原路由不一致')
    instrument = routes[config['target_key']]
    if (config['product'].lower() != instrument.split('.')[0].rstrip('0123456789').lower()
            or config['venue'] != instrument.rsplit('.', 1)[1]):
        raise ValueError('业务品种与原路由不一致')
    result = deepcopy(signal)
    result['hybrid'] = flat_business_state(state, revision, routes.values())
    result.pop('accepted_state', None)
    return result, {'hybrid_signal_state':deepcopy(result), 'gross_contracts':'0'}


def align_flat_strategy(payload, signal, *, strategy_id, operation_id):
    """保留旧调用接口；实际执行变换委托给公共模块。"""
    source = payload['component_states']['shared_account']['sources'][strategy_id]
    routes = payload['component_states']['shared_account']['shared']['routes'][strategy_id]['routes']
    result, evidence = align_flat_execution(payload, strategy_id=strategy_id,
        instruments=routes.values(), archive_unknown_close=True)
    aligned, metadata = align_shared_signal(signal, evidence['new_revision'], routes, source)
    new_source = result['component_states']['shared_account']['sources'][strategy_id]
    new_source['metadata'] = {**new_source.get('metadata', {}), **metadata,
        'checkpoint_alignment': {'revision':evidence['new_revision'], 'signal':aligned},
        'operator_flat_alignment':operation_id}
    evidence.update(operation_id=operation_id, previous_signal=signal,
        previous_targets=source['targets'], instrument_id=next(iter(routes.values())))
    ledger = validate_saved_ownership(result, evidence['client_id'])
    return result, ledger, evidence
