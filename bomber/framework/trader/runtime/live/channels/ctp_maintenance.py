"""CTP检查点空仓基线维护；业务状态转换由策略提供，不发送或撤销订单。"""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time

from bomber.framework.trader import persistence as storage
from bomber.framework.trader.execution.attribution import PositionAttributionLedger
from bomber.framework.trader.portfolio import PositionManager, PortfolioCoordinator
from .ctp_owned import validate_saved_ownership
from .ctp_shared import CtpSharedAccountLifecycle
from ..local import strategy_state_lock


def align_flat_execution(payload, *, strategy_id, instruments, archive_unknown_close=False):
    """只生成执行层候选。普通对齐保留订单历史；归档模式仅处理本策略旧平仓单。"""
    result = deepcopy(payload)
    instruments = frozenset(map(str, instruments))
    if not instruments or len(result['ctp_drivers']) != 1:
        raise ValueError('空仓对齐须有明确合约范围及唯一物理账户')
    client, driver = next(iter(result['ctp_drivers'].items()))
    if any(set(result[key]) != {client} for key in ('orders', 'ctp_ledgers')):
        raise ValueError('执行检查点须属于同一物理账户')
    if driver['driver_id'] != client:
        raise ValueError('Driver身份与执行客户端不一致')
    if driver['trading_day'] != result['ctp_ledgers'][client]['trading_day']:
        raise ValueError('原Driver与持仓账本交易日不一致')
    shared = result['component_states'].get('shared_account')
    if shared:
        state = shared['shared']
        if shared['client_id'] != client:
            raise ValueError('共享服务与Driver身份不一致')
        if shared.get('failed') or shared.get('pending'):
            raise ValueError('存在发送状态不明或服务失败，不能按空仓基线处理')
        if state.get('execution_mode') != 'independent':
            raise ValueError('共享空仓对齐仅支持独立订单')
        routes = state['routes'][strategy_id]['routes']
        if set(routes.values()) != instruments:
            raise ValueError('处理范围与策略路由不一致')
        if any(instruments.intersection(row['routes'].values())
               for sid, row in state['routes'].items() if sid != strategy_id):
            raise ValueError('其他共享策略也管理此合约，不能单独清理')
        if any(row['status'] != 'completed'
               for row in state.get('event_journal', {}).get('requests', {}).values()):
            raise ValueError('存在发送未决的跨进程请求，不能丢弃')
    else:
        owners = {r['strategy_id'] for r in result['positions']['strategy_positions']}
        owners.update(r['strategy_id'] for r in result['positions']['attribution']['owned'])
        owners.update(result['portfolio']['contributions'])
        owners.update(r['strategy_id'] for r in result['targets'])
        if owners - {strategy_id}:
            raise ValueError('单策略检查点存在其他策略归属，不能清理')
        routes = None
    decoded = {key: storage._decode_orders(rows) for key, rows in result['orders'].items()}
    attribution = result['positions']['attribution']
    if not archive_unknown_close and (driver['orders'] or any(not row.state.status.is_terminal
            for rows in decoded.values() for row in rows)):
        raise ValueError('旧检查点仍有未结束订单或CTP在途关联，先核对订单后再对齐')
    PositionAttributionLedger.validate_checkpoints(attribution, decoded)
    PositionManager().restore(storage._decode_positions(result['positions']))
    if any(r['instrument_id'] in instruments and r['strategy_id'] != strategy_id and Decimal(r['quantity'])
           for r in attribution['owned']):
        raise ValueError('其他策略也有此合约归属仓位，不能单独清理')
    for row in attribution['owned']:
        if row['strategy_id'] == strategy_id and (row['client_id'] != client or row['instrument_id'] not in instruments):
            raise ValueError('本策略还有处理范围外的归属仓位')
    associations = driver['orders']
    retired = [deepcopy(row) for row in associations if row['intent']['strategy_id'] == strategy_id]
    if not archive_unknown_close and any(Decimal(r['quantity']) for r in result['positions']['working_quantities']):
        raise ValueError('旧检查点有未完成委托量，不能直接对齐')
    retired_ids = {row['client_order_id'] for row in retired} if archive_unknown_close else set()
    if archive_unknown_close and (not retired_ids or len(retired_ids) != len(retired)):
        raise ValueError('未找到唯一可归档的本策略旧未决订单')
    machines = result['orders'][client]
    by_id = {row['client_order_id']: row for row in machines}
    for row in retired:
        intent, local = row['intent'], by_id.get(row['client_order_id'])
        if (local is None or local['status'] not in {'ACCEPTED', 'PARTIALLY_FILLED'}
                or intent['position_effect'] not in {'CLOSE', 'CLOSE_TODAY', 'CLOSE_YESTERDAY'}
                or intent['instrument_id'] not in instruments or local['instrument_id'] != intent['instrument_id']
                or Decimal(local['filled_quantity']) != Decimal(row['filled_quantity'])
                or local['side'] != intent['side'] or Decimal(local['order_quantity']) != Decimal(intent['quantity'])):
            raise ValueError('只允许已受理的旧平仓单，且订单机与Driver必须一致')
    if any(not row.state.status.is_terminal and row.state.client_order_id not in retired_ids
           for rows in decoded.values() for row in rows):
        raise ValueError('其他订单仍未终结，不能单策略空仓对齐')
    plans = attribution['orders']
    retired_plans = {token: deepcopy(row) for token, row in plans.items() if row['client_order_id'] in retired_ids}
    if {row['client_order_id'] for row in retired_plans.values()} != retired_ids:
        raise ValueError('旧未决订单缺少归属冻结计划')
    if any(leg['strategy_id'] != strategy_id or routes is not None and leg['target_key'] not in routes
           for row in retired_plans.values() for leg in row['legs']):
        raise ValueError('旧委托冻结份额包含其他策略，不可删除')
    result['orders'][client] = [row for row in machines if row['client_order_id'] not in retired_ids]
    driver['orders'] = [row for row in associations if row['client_order_id'] not in retired_ids]
    attribution['orders'] = {token: row for token, row in plans.items() if token not in retired_plans}
    for row in attribution['owned']:
        if row['strategy_id'] == strategy_id:
            row['quantity'] = '0'
    for key in ('account_positions', 'working_quantities'):
        for row in result['positions'][key]:
            if row['key']['client_id'] == client and row['key']['instrument_id'] in instruments:
                row['quantity'] = '0'
    for row in result['positions']['strategy_positions']:
        if row['strategy_id'] == strategy_id:
            row['quantity'] = '0'
    result['ctp_ledgers'][client]['positions'] = [row for row in result['ctp_ledgers'][client]['positions']
                                               if row['instrument_id'] not in instruments]
    revision = result['portfolio']['strategy_revisions'].get(strategy_id, 0)
    if shared:
        source = shared['sources'].get(strategy_id)
        if source is None:
            raise ValueError('缺少原策略目标')
        revision = max(revision, source['revision']) + 1
        shared['strategy_revisions'][strategy_id] = revision
        shared['portfolio_revision'] += 1
        result['portfolio']['revision'] += 1
        source.update(revision=revision, targets={i: '0' for i in instruments}, deadline_ns=None)
        source['rows'] = [dict(strategy_id=strategy_id, target_key=key, instrument_id=i, quantity='0',
                              source_revision=revision, source_deadline_ns=None) for key, i in routes.items()]
        state['suspended'] = sorted(set(state['suspended']) | {strategy_id})
        state['draining'] = [sid for sid in state['draining'] if sid != strategy_id]
    for row in result['positions']['strategy_revisions']:
        if row['strategy_id'] == strategy_id:
            row['revision'] = revision
    for row in result['portfolio']['contributions'].get(strategy_id, []):
        row['quantity'] = '0'
    if strategy_id in result['portfolio']['strategy_revisions']:
        result['portfolio']['strategy_revisions'][strategy_id] = revision
    for row in result['targets']:
        if row['strategy_id'] == strategy_id:
            row.update(revision=revision, targets={key: '0' for key in row['targets']}, deadline_ns=None)
    validate_saved_ownership(result, client)
    PortfolioCoordinator().restore(storage._decode_portfolio(result['portfolio']))
    evidence = dict(strategy_id=strategy_id, client_id=client, managed_instruments=sorted(instruments),
        new_revision=revision, archived_driver_orders=retired, archived_attribution_plans=retired_plans,
        archived_order_states=[by_id[key] for key in sorted(retired_ids)],
        old_order_terminal_status='UNKNOWN' if retired_ids else None, realized_pnl_reconstructed=False,
        actual_close_price=None, actual_close_time=None, actual_close_prices=None, actual_close_times=None)
    return result, evidence


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def reconcile_flat_checkpoint(*, state_file, transport, prepare, signal_files=(), archive_unknown_close=False):
    """锁定、观察两次柜台、业务候选校验、备份及CAS提交。prepare不应读写柜台。"""
    path = Path(state_file).resolve()
    with ExitStack() as resources:
        resources.enter_context(strategy_state_lock(str(path) + '.continuous-owner'))
        resources.enter_context(strategy_state_lock(path))
        CtpSharedAccountLifecycle.acquire(resources, transport, environment='realtime')
        for value in sorted({Path(p).resolve() for p in signal_files} - {path}):
            resources.enter_context(strategy_state_lock(value))
        store = storage.JsonStateStore(path)
        saved = store.load()
        if saved is None:
            raise FileNotFoundError(path)
        if len(saved.payload['ctp_drivers']) != 1:
            raise ValueError('仅支持唯一物理账户')
        client, driver = next(iter(saved.payload['ctp_drivers'].items()))
        if (driver['account_id'] != transport.account_id or driver['investor_id'] != transport.investor_id
                or driver['broker_id'] != transport.broker_id or transport.broker_id != '9999'):
            raise ValueError('连接账户与旧检查点不一致；本入口仅支持SimNow')
        try:
            context = transport.connect(lambda *_: None, lambda *_: None, lambda *_: None)
            transport.activate()
            if archive_unknown_close:
                if context.trading_day <= driver['trading_day']:
                    raise ValueError('归档旧未决订单只允许后续交易日')
            elif context.trading_day != driver['trading_day']:
                raise ValueError('普通空仓对齐须在原检查点同一交易日执行')
            candidate, evidence = prepare(deepcopy(saved.payload), context.trading_day)
            if evidence['client_id'] != client:
                raise ValueError('候选账户身份不一致')
            ledger = validate_saved_ownership(candidate, client)
            scoped = candidate['component_states'].get('shared_account', {}).get('shared', {}).get('managed_instruments')
            managed = set(scoped if scoped is not None else evidence['managed_instruments'])
            expected = {str(i): (row.long_total, row.short_total) for i, row in ledger.positions.items()
                        if row.long_total or row.short_total}
            external = saved.payload['component_states'].get('external_positions', {}).get('gross', {})
            for instrument, pair in external.items():
                if instrument in managed:
                    long, short = expected.get(instrument, (Decimal(0), Decimal(0)))
                    expected[instrument] = (long + Decimal(pair[0]), short + Decimal(pair[1]))
            expected = {i: pair for i, pair in expected.items() if any(pair)}
            canonical = {i.casefold(): i for i in managed}
            for _ in range(2):
                gross = {}
                for key, pair in transport.query_gross_positions().items():
                    key = canonical.get(str(key).casefold(), str(key))
                    old = gross.get(key, (Decimal(0), Decimal(0)))
                    gross[key] = tuple(old[n] + Decimal(str(q)) for n, q in enumerate(pair))
                gross = {i: pair for i, pair in gross.items() if any(pair)}
                observed = {i: pair for i, pair in gross.items() if i in managed}
                if observed != expected:
                    differences = []
                    for instrument in sorted(observed.keys() | expected.keys()):
                        actual, wanted = observed.get(instrument, (0, 0)), expected.get(instrument, (0, 0))
                        if actual != wanted:
                            baseline = external.get(instrument, ('0', '0'))
                            differences.append(f'{instrument} 柜台多仓={actual[0]} 空仓={actual[1]}；'
                                f'原外部基线多仓={baseline[0]} 空仓={baseline[1]}；预期={wanted}')
                    raise RuntimeError('柜台多空总仓与空仓候选不一致；' + '；'.join(differences) + '；原文件未修改')
                active = transport.query_active_orders().orders
                if any(str(row.instrument_id).casefold() in canonical for row in active):
                    raise RuntimeError('管理合约柜台仍有活动订单，原文件未修改')
            if candidate == saved.payload:
                print('空仓基线已经对齐，无需重复提交；未发送订单')
                return saved
            original = path.read_bytes()
            operation_id = f'g{saved.generation}-{time.time_ns()}'
            backup = path.with_name(path.name + f'.before-flat-alignment-{operation_id}.bak')
            audit = path.with_name(path.name + f'.flat-alignment-{operation_id}.json')
            evidence.update(operation='archive_unknown_close' if archive_unknown_close else 'manual_flat',
                operation_id=operation_id, previous_generation=saved.generation,
                observed_trading_day=context.trading_day, persisted_trading_day=driver['trading_day'],
                original_sha256=hashlib.sha256(original).hexdigest(), backup_file=str(backup),
                managed_broker_gross={i: [str(q) for q in pair] for i, pair in observed.items()},
                preserved_external_gross=deepcopy(external), aligned_strategy_gross=['0', '0'],
                external_gross={i: [str(q) for q in pair] for i, pair in gross.items() if i not in managed},
                external_active_orders=sum(str(row.instrument_id).casefold() not in canonical for row in active), orders_submitted=0)
            _write_new(backup, original)
            _write_new(audit, (json.dumps(evidence, ensure_ascii=False, indent=2) + '\n').encode())
            updated = store.save(candidate, expected_generation=saved.generation)
            print(f'空仓基线对齐完成: {evidence["strategy_id"]}；generation={updated.generation}；未发送订单')
            retained = {i: pair for i, pair in external.items() if i in managed}
            print(f'本策略仓位=0；保留管理范围内原外部基线={retained}')
            print(f'备份: {backup}\n审计: {audit}\n保留其他策略和原外部基线；未补造成交或盈亏')
            return updated
        finally:
            transport.close()


def reconcile_shared_flat(config, *, strategy_id, transport, align_signal, archive_unknown_close=False):
    """共享检查点单策略维护。align_signal只处理业务数据，框架不识别策略类型。"""
    matches = [row for row in config['strategies'] if row['strategy_id'] == strategy_id]
    if len(matches) != 1 or config['environment'] != 'realtime' or config.get('existing_position_policy') != 'preserve_external':
        raise ValueError('须指定唯一策略及preserve_external共享配置')
    def prepare(payload, day):
        identity = payload['component_states']['session_identity']['identity']
        if identity['account'] != config['account']:
            raise ValueError('账户参数与检查点不一致，先恢复原配置')
        source = payload['component_states']['shared_account']['sources'][strategy_id]
        persisted = storage.JsonStateStore(matches[0]['state_file']).load()
        if persisted is None:
            raise ValueError('缺少原策略信号检查点')
        declared = identity.get('strategies', {}).get(strategy_id)
        if declared is not None and persisted.payload['identity'] != declared:
            raise ValueError('策略信号文件身份与原账户声明不一致')
        routes = payload['component_states']['shared_account']['shared']['routes'][strategy_id]['routes']
        result, evidence = align_flat_execution(payload, strategy_id=strategy_id,
            instruments=routes.values(), archive_unknown_close=archive_unknown_close)
        signal, signal_revision = persisted.payload['signal'], persisted.payload['revision']
        if signal_revision > source['revision']:
            raise ValueError('策略信号版本领先账户检查点，须先对账')
        previous_alignment = source.get('metadata', {}).get('checkpoint_alignment')
        if signal_revision < source['revision'] and previous_alignment is not None:
            if previous_alignment.get('revision') != source['revision'] or 'signal' not in previous_alignment:
                raise ValueError('维护业务副本与账户目标版本不一致')
            signal, signal_revision = previous_alignment['signal'], source['revision']
        aligned, metadata = align_signal(signal, evidence['new_revision'], routes, source,
                                         signal_revision=signal_revision)
        new_source = result['component_states']['shared_account']['sources'][strategy_id]
        new_source['metadata'] = {**new_source.get('metadata', {}), **metadata, 'checkpoint_alignment': {
            'revision': evidence['new_revision'], 'signal': aligned}}
        bootstrap = result['component_states']['shared_account']['shared'].get('bootstrap_signals', {})
        if strategy_id in bootstrap:
            bootstrap[strategy_id] = deepcopy(aligned)
        evidence['previous_signal'] = persisted.payload['signal']
        return result, evidence
    return reconcile_flat_checkpoint(state_file=config['account_state_file'], transport=transport, prepare=prepare,
        signal_files=[row['state_file'] for row in config['strategies']], archive_unknown_close=archive_unknown_close)
