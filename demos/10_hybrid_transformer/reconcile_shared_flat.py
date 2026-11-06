"""共享账户单策略空仓基线对齐：归档旧未决订单，不补造成交或撤单。"""
from __future__ import annotations

import argparse
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
from bomber.framework.trader.runtime.live.channels.ctp_owned import validate_saved_ownership
from bomber.framework.trader.runtime.live.local import strategy_state_lock


def align_flat_strategy(payload, signal, *, strategy_id, operation_id):
    """只构造原交易日候选；柜台观察和文件提交由reconcile统一完成。"""
    result = deepcopy(payload)
    shared = result['component_states']['shared_account']
    if shared.get('failed') or shared.get('pending'):
        raise ValueError('存在发送状态不明或服务失败，不能按空仓基线处理')
    if shared['shared'].get('execution_mode') != 'independent':
        raise ValueError('只支持独立订单的共享账户')
    route = shared['shared']['routes'][strategy_id]['routes']
    if len(route) != 1:
        raise ValueError('空仓对齐只支持本策略单合约路由')
    target_key, instrument = next(iter(route.items()))
    other_routes = {i for sid, row in shared['shared']['routes'].items() if sid != strategy_id
                    for i in row['routes'].values()}
    if instrument in other_routes:
        raise ValueError('其他策略也管理此合约，不能单独清理')
    client = shared['client_id']
    if any(set(result[key]) != {client} for key in ('orders','ctp_drivers','ctp_ledgers')):
        raise ValueError('仅支持单个物理账户')
    for sid, row in shared['shared'].get('bootstrap_signals', {}).items():
        if sid == strategy_id:
            raise ValueError('存在未消费的初始化业务状态，拒绝覆盖')
    journal = shared['shared'].get('event_journal', {})
    if any(row['status'] != 'completed' for row in journal.get('requests', {}).values()):
        raise ValueError('存在发送未决的跨进程请求，不能丢弃')
    decoded = {key:storage._decode_orders(rows) for key,rows in result['orders'].items()}
    PositionAttributionLedger.validate_checkpoints(result['positions']['attribution'], decoded)
    PositionManager().restore(storage._decode_positions(result['positions']))
    driver = result['ctp_drivers'][client]
    if driver['trading_day'] != result['ctp_ledgers'][client]['trading_day']:
        raise ValueError('原Driver与持仓账本交易日不一致')
    associations = driver['orders']
    retired = [deepcopy(row) for row in associations
               if row['intent']['strategy_id'] == strategy_id and row['intent']['instrument_id'] == instrument]
    retired_ids = {row['client_order_id'] for row in retired}
    if not retired_ids or len(retired_ids) != len(retired):
        raise ValueError('未找到唯一可归档的本策略旧未决订单')
    machines = result['orders'][client]
    by_id = {row['client_order_id']:row for row in machines}
    for row in retired:
        intent = row['intent']
        local = by_id.get(row['client_order_id'])
        if (local is None or local['status'] not in {'ACCEPTED','PARTIALLY_FILLED'}
                or intent['position_effect'] not in {'CLOSE','CLOSE_TODAY','CLOSE_YESTERDAY'}
                or local['instrument_id'] != instrument or Decimal(local['filled_quantity']) != Decimal(row['filled_quantity'])
                or local['side'] != intent['side'] or Decimal(local['order_quantity']) != Decimal(intent['quantity'])):
            raise ValueError('只允许已受理的旧平仓单，且订单机与Driver必须一致')
    if any(not row.state.status.is_terminal and row.state.client_order_id not in retired_ids
           for rows in decoded.values() for row in rows):
        raise ValueError('其他订单仍未终结，不能单策略空仓对齐')
    attribution = result['positions']['attribution']
    if any(row['instrument_id'] == instrument and row['strategy_id'] != strategy_id
           and Decimal(row['quantity']) for row in attribution['owned']):
        raise ValueError('其他策略也有此合约归属仓位，不能单独清理')
    plans = attribution['orders']
    retired_plans = {token:deepcopy(row) for token,row in plans.items()
                     if row['client_order_id'] in retired_ids}
    if {row['client_order_id'] for row in retired_plans.values()} != retired_ids:
        raise ValueError('旧未决订单缺少归属冻结计划')
    if any(leg['strategy_id'] != strategy_id or leg['target_key'] != target_key
           for row in retired_plans.values() for leg in row['legs']):
        raise ValueError('旧委托冻结份额包含其他策略，不可删除')
    source = shared['sources'].get(strategy_id)
    if source is None or signal.get('version') != 1:
        raise ValueError('缺少原目标或业务状态')
    business = deepcopy(signal)
    hybrid = business['hybrid']
    config = hybrid['config']
    if (business['identity']['config'] != config or config['target_key'] != target_key
            or config['product'].lower() != instrument.split('.')[0].rstrip('0123456789').lower()
            or config['venue'] != instrument.rsplit('.',1)[1] or config['target_keys'] is not None):
        raise ValueError('业务状态与原路由不一致')
    previous_targets = deepcopy(source['targets'])
    revision = max(source['revision'],hybrid['revision'])+1
    # 归档不是撤单/成交终态；完整原记录保留在备份和审计中，不加入虚构回报。
    result['orders'][client] = [row for row in machines if row['client_order_id'] not in retired_ids]
    result['ctp_drivers'][client]['orders'] = [row for row in associations if row['client_order_id'] not in retired_ids]
    attribution['orders'] = {token:row for token,row in plans.items() if token not in retired_plans}
    for row in attribution['owned']:
        if row['strategy_id'] == strategy_id and row['target_key'] == target_key:
            if row['client_id'] != client or row['instrument_id'] != instrument:
                raise ValueError('本策略仓位存在其他合约')
            row['quantity'] = '0'
    for key in ('account_positions','working_quantities'):
        for row in result['positions'][key]:
            if row['key'] == {'client_id':client,'instrument_id':instrument}:
                row['quantity'] = '0'
    for row in result['positions']['strategy_positions']:
        if row['strategy_id'] == strategy_id:
            if row['target_key'] != target_key:
                raise ValueError('本策略存在其他逻辑仓位')
            row['quantity'] = '0'
    for row in result['positions']['strategy_revisions']:
        if row['strategy_id'] == strategy_id:row['revision'] = revision
    result['ctp_ledgers'][client]['positions'] = [row for row in result['ctp_ledgers'][client]['positions']
                                                if row['instrument_id'] != instrument]
    for row in result['portfolio']['contributions'].get(strategy_id,[]):row['quantity'] = '0'
    result['portfolio']['strategy_revisions'][strategy_id] = revision
    result['portfolio']['revision'] += 1
    for row in result['targets']:
        if row['strategy_id'] == strategy_id:
            row['revision'] = revision
            row['targets'] = {target_key:'0'}
    hybrid['holdings']['active'] = []
    hybrid.update(revision=revision,last_targets={instrument.split('.')[0].lower():'0'},targets_pending=False)
    business.pop('accepted_state',None)
    source.update(revision=revision,targets={instrument:'0'},deadline_ns=None)
    source['rows'] = [dict(strategy_id=strategy_id,target_key=target_key,instrument_id=instrument,
                          quantity='0',source_revision=revision,source_deadline_ns=None)]
    source['metadata'] = {**source.get('metadata',{}),'hybrid_signal_state':business,
        'operator_flat_alignment':operation_id, 'gross_contracts':'0',
        'position_attribution':deepcopy(source['rows']),
        'portfolio_revision':shared['portfolio_revision']+1}
    shared['strategy_revisions'][strategy_id] = revision
    shared['portfolio_revision'] += 1
    shared['shared']['suspended'] = sorted(set(shared['shared']['suspended'])|{strategy_id})
    shared['shared']['draining'] = [sid for sid in shared['shared']['draining'] if sid != strategy_id]
    ledger = validate_saved_ownership(result,client)
    PositionManager().restore(storage._decode_positions(result['positions']))
    PortfolioCoordinator().restore(storage._decode_portfolio(result['portfolio']))
    evidence = dict(operation='operator_confirmed_broker_flat_unknown_old_order_outcome',
        operation_id=operation_id,strategy_id=strategy_id,instrument_id=instrument,
        archived_driver_orders=retired,archived_order_states=[by_id[key] for key in sorted(retired_ids)],
        archived_attribution_plans=retired_plans,previous_targets=previous_targets,
        previous_signal=signal,new_revision=revision,actual_close_price=None,
        actual_close_time=None,old_order_terminal_status='UNKNOWN',realized_pnl_reconstructed=False)
    return result, ledger, evidence


def _write_new(path, data):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as handle:
        handle.write(data);handle.flush();os.fsync(handle.fileno())


def reconcile(config, *, strategy_id, transport):
    """查两次柜台；只改账户文件，策略文件由账户同代副本在下次恢复时接续。"""
    from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle
    path=Path(config['account_state_file']).resolve()
    rows=[row for row in config['strategies'] if row['strategy_id']==strategy_id and row['kind']=='hybrid_transformer']
    if len(rows)!=1 or config['environment']!='realtime' or config.get('existing_position_policy')!='preserve_external':
        raise ValueError('须指定原共享配置中的唯一Hybrid策略并保留外部基线')
    with ExitStack() as resources:
        resources.enter_context(strategy_state_lock(str(path)+'.continuous-owner'))
        resources.enter_context(strategy_state_lock(path))
        CtpSharedAccountLifecycle.acquire(resources,transport,environment='realtime')
        for row in config['strategies']:resources.enter_context(strategy_state_lock(row['state_file']))
        store=storage.JsonStateStore(path);saved=store.load()
        if saved is None:raise FileNotFoundError(path)
        payload=saved.payload
        old=payload['component_states']['session_identity']['identity']
        if old['account']!=config['account']:raise ValueError('账户参数与检查点不一致，先恢复原配置')
        shared=payload['component_states']['shared_account'];client=shared['client_id']
        previous=payload['ctp_drivers'][client]
        if (previous['account_id']!=transport.account_id or previous['investor_id']!=transport.investor_id
                or previous['broker_id']!=transport.broker_id or transport.broker_id!='9999'):
            raise ValueError('柜台身份与原SimNow检查点不一致')
        signal_saved=storage.JsonStateStore(rows[0]['state_file']).load()
        signal=signal_saved.payload['signal'] if signal_saved else shared['sources'][strategy_id]['metadata']['hybrid_signal_state']
        account_signal=shared['sources'][strategy_id]['metadata']['hybrid_signal_state']
        if signal['identity']!=account_signal['identity']:raise ValueError('账户和信号检查点身份不一致')
        if signal['hybrid']['revision']<account_signal['hybrid']['revision']:signal=account_signal
        operation_id=f'g{saved.generation}-{time.time_ns()}'
        result,ledger,evidence=align_flat_strategy(payload,signal,strategy_id=strategy_id,operation_id=operation_id)
        instrument=evidence['instrument_id']
        managed=set(shared['shared']['managed_instruments'])
        expected={str(i):(row.long_total,row.short_total) for i,row in ledger.positions.items()
                  if row.long_total or row.short_total}
        external=payload['component_states']['external_positions']['gross']
        for key,values in external.items():
            if key in managed:
                a,b=expected.get(key,(Decimal(0),Decimal(0)))
                expected[key]=(a+Decimal(values[0]),b+Decimal(values[1]))
        expected={k:v for k,v in expected.items() if any(v)}
        try:
            session=transport.connect(lambda *_:None,lambda *_:None,lambda *_:None);transport.activate()
            if session.trading_day<=previous['trading_day']:
                raise ValueError('归档旧未决订单只允许已进入后续交易日，不在原交易日忽略活动单')
            for _ in range(2):
                gross={str(k):tuple(Decimal(str(v)) for v in pair) for k,pair in transport.query_gross_positions().items() if any(pair)}
                if gross.get(instrument,(0,0))!=(0,0):raise RuntimeError('待对齐合约柜台仍有多空总仓，保持原文件')
                if {k:v for k,v in gross.items() if k in managed}!=expected:
                    raise RuntimeError('其他管理合约的柜台多空仓与原归属/基线不一致，保持原文件')
                if any(str(row.instrument_id) in managed for row in transport.query_active_orders().orders):
                    raise RuntimeError('柜台管理范围仍有活动委托，保持原文件')
            original=path.read_bytes()
            current=store.load()
            if current is None or current.generation!=saved.generation:raise RuntimeError('原检查点已变化')
            backup=path.with_name(path.name+f'.before-shared-flat-{operation_id}.bak')
            audit=path.with_name(path.name+f'.shared-flat-{operation_id}.json')
            evidence.update(previous_generation=saved.generation,observed_trading_day=session.trading_day,
                persisted_trading_day=previous['trading_day'],backup_file=str(backup),
                original_sha256=hashlib.sha256(original).hexdigest(),
                managed_broker_gross={k:[str(v) for v in pair] for k,pair in gross.items() if k in managed},
                active_orders=0,orders_submitted=0)
            _write_new(backup,original)
            _write_new(audit,(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n').encode())
            updated=store.save(result,expected_generation=saved.generation)
            print(f'空仓基线对齐完成: {strategy_id} {instrument}；generation={updated.generation}；未发送订单')
            print(f'备份: {backup}\n审计: {audit}\n旧委托真实终态及平仓盈亏未知，未补造成交；其他策略保持原样')
            return updated
        finally:transport.close()


def main(argv=None):
    from dotenv import load_dotenv
    from scripts.integration.shared_account.config import read_config
    from bomber.framework.trader.execution.ctp import CtpTdApiTransport
    from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--strategy-id',required=True)
    parser.add_argument('--confirm-broker-flat',action='store_true')
    args=parser.parse_args(argv)
    if not args.confirm_broker_flat:parser.error('须明确confirm-broker-flat；这是空仓基线对齐，不是成交补记')
    load_dotenv(Path(__file__).resolve().parents[2]/'.env')
    config=read_config(args.config)
    transport=build_simnow_transport(CtpTdApiTransport,client_id=config['account']['client_id'],
        flow_path='/tmp/bomber-shared-flat',timeout_seconds=30)
    reconcile(config,strategy_id=args.strategy_id,transport=transport)


if __name__=='__main__':main()
