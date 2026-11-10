"""一次性注册08 worker：来源为原成交检查点；禁单核验后原子提交并保留备份。"""
from copy import deepcopy
from decimal import Decimal
import hashlib
from importlib import import_module
import json
from pathlib import Path

from .run_workers import load_config
from .config import read_config, load_environment


def validate_source_configuration(payload, config):
    """先离线核对源身份，不能把待注册的联合限额覆盖当作源配置已匹配。"""
    identity = payload['component_states']['session_identity']['identity']
    saved, declared = identity['account'], config['account']
    differences = {key: {'checkpoint': saved.get(key), 'config': declared.get(key)}
        for key in sorted(saved.keys() | declared.keys()) if saved.get(key) != declared.get(key)}
    if differences:
        raise ValueError('原Hybrid账户身份或限额与源配置不一致；差异='
            + json.dumps(differences, ensure_ascii=False, sort_keys=True)
            + '；先恢复与原检查点一致的源配置，禁止修改检查点绕过核验')
    rows = {row['strategy_id']: row for row in config['strategies']}
    if rows.keys() != identity['strategies'].keys():
        raise ValueError('原账户策略集合与base_config不符；已注册时请使用run_workers恢复')
    for sid, row in rows.items():
        previous = identity['strategies'][sid]
        if previous['kind'] != row['kind'] or previous['argv'] != row['argv']:
            raise ValueError(f'原Hybrid策略参数与源配置不一致: {sid}；禁止在注册时隐式改变')
    return identity


def validate_option_signal(option, signal, strategy_id):
    """原08信号须与原成交账本配套；仅报告身份字段路径及目标版本。"""
    from bomber.framework.trader.runtime.live.recovery import identity_difference_paths
    previous = option['component_states']['session_identity']['identity']
    shared = option['component_states'].get('shared_account')
    if shared is None or set(previous.get('strategies', {})) != {strategy_id}:
        raise ValueError('请使用原08共享成交检查点；原生或模型文件不能隐式导入')
    expected = previous['strategies'][strategy_id]
    differences = []
    if signal['identity'] != expected:
        differences.append('身份差异字段='
            + ', '.join(identity_difference_paths(expected, signal['identity'])))
    revision = shared['strategy_revisions'].get(strategy_id, 0)
    if signal['revision'] != revision:
        show = lambda value: str(value) if type(value) is int else '<非整数>'
        differences.append(f"账户revision={show(revision)}, 信号revision={show(signal['revision'])}")
    if differences:
        raise ValueError('原08信号与成交账户身份/来源版本不一致；'
            + '；'.join(differences) + '；须使用配套信号文件，不修改检查点绕过核验')


def quiescent(payload, client):
    from bomber.framework.trader.runtime.live.channels.ctp_owned import validate_saved_ownership
    ledger = validate_saved_ownership(payload, client)
    driver = payload['ctp_drivers'][client]
    shared = payload['component_states'].get('shared_account', {})
    journal = shared.get('shared', {}).get('event_journal', {})
    if (driver['orders'] or shared.get('failed') or shared.get('pending')
            or shared.get('shared', {}).get('execution_stop') is not None
            or any(journal.get('ack', {}).get(sid, 0) != len(rows) for sid, rows in journal.get('events', {}).items())
            or any(row['status'] != 'completed' for row in journal.get('requests', {}).values())):
        raise ValueError('注册要求原执行已停止且没有未决订单、请求或账户故障')
    return ledger


def remap_client(value, previous, current):
    """只映射执行端client/backend；策略ID、订单ID、成交ID均保持不变。"""
    if isinstance(value, dict):
        return {key: current if key in {'client_id', 'backend_id'} and item == previous else remap_client(item, previous, current)
                for key, item in value.items()}
    if isinstance(value, list):
        return [remap_client(item, previous, current) for item in value]
    return value


def merge_positions(base, incoming, client, old_client, base_day, option_day):
    """合并闭合的成交历史与多空份额；保留成本，不生成虚构成交或按净仓抵消。"""
    incoming = remap_client(deepcopy(incoming), old_client, client)
    if option_day > base_day:
        raise ValueError('08检查点晚于原账户；先用原Hybrid入口完成跨日对账再注册')
    a, b = base['positions'], incoming['positions']
    for name in ('strategy_positions', 'strategy_revisions'):
        a[name].extend(b[name])
    left, right = a['attribution'], b['attribution']
    if set(left['orders']) & set(right['orders']):
        raise ValueError('两个来源的归属订单令牌重复')
    left['owned'].extend(right['owned'])
    left['orders'].update(right['orders'])
    left['transfers'].extend(right['transfers'])
    # 不把原08的账户查询快照当成系统仓位。系统净仓由两个来源的成交账本重新求和。
    rows = {row['instrument_id']: row for row in base['ctp_ledgers'][client]['positions']}
    for raw in incoming['ctp_ledgers'][old_client]['positions']:
        row = deepcopy(raw)
        if option_day < base_day:
            for side in ('long', 'short'):
                today, yesterday = (Decimal(row[side+suffix]) for suffix in ('_today', '_yesterday'))
                total = today+yesterday
                basis = (today*Decimal(row[side+'_today_basis'])+yesterday*Decimal(row[side+'_yesterday_basis']))
                row.update({side+'_today': '0', side+'_today_basis': '0', side+'_yesterday': str(total),
                            side+'_yesterday_basis': str(basis/total if total else Decimal(0))})
        key = row['instrument_id']
        if key not in rows:
            rows[key] = row
            continue
        for bucket in ('long_today', 'long_yesterday', 'short_today', 'short_yesterday'):
            q1, q2 = Decimal(rows[key][bucket]), Decimal(row[bucket])
            basis = q1*Decimal(rows[key][bucket+'_basis'])+q2*Decimal(row[bucket+'_basis'])
            rows[key][bucket] = str(q1+q2)
            rows[key][bucket+'_basis'] = str(basis/(q1+q2) if q1+q2 else Decimal(0))
    base['ctp_ledgers'][client]['positions'] = list(rows.values())
    a['account_positions'] = [dict(key=dict(client_id=client, instrument_id=key), quantity=str(
        sum((Decimal(row[b]) for b in ('long_today', 'long_yesterday')), Decimal(0))
        -sum((Decimal(row[b]) for b in ('short_today', 'short_yesterday')), Decimal(0))))
        for key, row in rows.items()]
    old_orders = incoming['orders'][old_client]
    def ids(orders):
        return {row['client_order_id'] for row in orders}
    if ids(base['orders'][client]) & ids(old_orders):
        raise ValueError('两个来源的订单ID重复')
    base['orders'][client].extend(old_orders)
    base['ctp_drivers'][client]['next_ref'] = max(base['ctp_drivers'][client]['next_ref'],
        incoming['ctp_drivers'][old_client]['next_ref'])
    base['targets'].extend(incoming['targets'])
    for name in ('strategy_revisions', 'contributions'):
        base['portfolio'][name].update(incoming['portfolio'][name])
    base['portfolio']['revision'] = max(base['portfolio']['revision'], incoming['portfolio']['revision'])
    return incoming


def registration_payload(account, payload, option, option_signal, base_config, *, strategy_limits_only):
    """变换只作用于暂存副本；RuntimeStateManager在柜台核验成功后才提交。"""
    from bomber.framework.trader.runtime.live.channels.ctp_owned import restore_external_baseline
    from .rollover import validate_strategy_identity
    from bomber.framework.trader.runtime.live.channels.ctp_rollover import observe_rollover
    current = account.manager.state_components['session_identity'].identity
    old = validate_source_configuration(payload, base_config)
    original_ids = {row['strategy_id'] for row in base_config['strategies']}
    added, = current['strategies'].keys()-original_ids
    if set(old['strategies']) != original_ids or any(row['kind'] != 'hybrid_transformer' for row in old['strategies'].values()):
        raise ValueError('原账户策略集合与base_config不符；已经注册不能重复导入')
    before_budget, after_budget = deepcopy(old['account']), deepcopy(current['account'])
    if strategy_limits_only:
        before_budget.update(max_gross_quantity=None, max_gross_notional=None)
    if before_budget != after_budget:
        raise ValueError('只能显式取消联合持仓/名义限额；TD身份、每日额度和跳价不能变化')
    for sid in original_ids:
        previous, new = old['strategies'][sid], current['strategies'][sid]
        if previous['trading_day'] == new['trading_day']:
            if previous != new:
                raise ValueError('注册不能改变Hybrid模型、参数、路由、手数或信号身份')
        else:
            validate_strategy_identity(previous, new)
    comparable = deepcopy(old)
    for key in ('account', 'strategies', 'trading_day', 'managed_instruments'):
        comparable[key] = deepcopy(current[key])
    if comparable != current:
        raise ValueError('注册不能改变原账户执行政策或身份')
    client = account.service.client_id
    quiescent(payload, client)
    option = deepcopy(option)
    if len(option['ctp_drivers']) != 1:
        raise ValueError('原08须为唯一CTP成交客户端')
    old_client, = option['ctp_drivers']
    quiescent(option, old_client)
    before_driver, source_driver = payload['ctp_drivers'][client], option['ctp_drivers'][old_client]
    if any(before_driver[key] != source_driver[key] for key in ('account_id', 'broker_id', 'investor_id')):
        raise ValueError('原08与Hybrid不是同一个CTP账户')
    previous = option['component_states']['session_identity']['identity']
    shared = option['component_states'].get('shared_account')
    if shared is None or set(previous.get('strategies', {})) != {added}:
        raise ValueError('请使用原08共享成交检查点；原生或模型文件不能隐式导入')
    reference = previous['strategies'][added]['reference']
    new_reference = current['strategies'][added]['reference']
    recovery = import_module('demos.08_option_vega.recovery')
    if reference['td_day'] == new_reference['td_day']:
        if reference != new_reference:
            raise ValueError('原08与worker策略、来源或风险参数不一致')
    else:
        recovery.validate_identity(reference, new_reference)
    validate_option_signal(option, option_signal, added)
    if any(row['strategy_id'] != added for row in option['positions']['strategy_positions']):
        raise ValueError('原08包含其他策略归属')
    if (set(shared['sources']) - {added} or set(shared['strategy_revisions']) - {added}
            or set(option['portfolio']['contributions']) - {added}
            or {row['strategy_id'] for row in option['targets']} - {added}):
        raise ValueError('原08包含其他策略目标，不能合并或覆盖')
    restore = account.option_sessions[added].restore_signal
    restore(option_signal['signal'])  # 校验业务状态及旧非零目标，禁单阶段不能丢弃。
    incoming = merge_positions(payload, option, client, old_client,
        payload['ctp_ledgers'][client]['trading_day'], option['ctp_ledgers'][old_client]['trading_day'])
    shared = incoming['component_states']['shared_account']
    target = payload['component_states']['shared_account']
    target['portfolio_revision'] = payload['portfolio']['revision']
    target['strategy_revisions'].update(shared['strategy_revisions'])
    target['sources'].update(shared['sources'])
    declaration = account.service.snapshot_state()['shared']
    routes = declaration['routes'][added]['routes']
    # 到期的零仓候选可移除；真实仓位、旧目标、在途均不能丢弃。
    for name in ('strategy_positions', 'strategy_revisions'):
        rows = payload['positions'][name]
        for row in rows:
            if row['strategy_id'] == added and row['target_key'] not in routes and Decimal(row.get('quantity', '0')):
                raise ValueError('原08归属合约不在当前路由，禁止丢弃或切换仓位')
        payload['positions'][name] = [r for r in rows if r['strategy_id'] != added or r['target_key'] in routes]
    owned = payload['positions']['attribution']['owned']
    for row in owned:
        if row['strategy_id'] == added and row['target_key'] not in routes and Decimal(row['quantity']):
            raise ValueError('原08真实合约归属不在当前路由')
    payload['positions']['attribution']['owned'] = [r for r in owned
        if r['strategy_id'] != added or r['target_key'] in routes]
    source = target['sources'].get(added)
    if source:
        if any(Decimal(r['quantity']) for r in source['rows'] if r['target_key'] not in routes):
            raise ValueError('原08仍有非零旧目标不在当前路由')
        values = {key: source['targets'].get(instrument, '0') for key, instrument in routes.items()}
        source['targets'] = {i: values[key] for key, i in routes.items()}
        source['rows'] = [dict(strategy_id=added, target_key=key, instrument_id=i, quantity=values[key]) for key, i in routes.items()]
        payload['portfolio']['contributions'][added] = [dict(key=dict(client_id=client, instrument_id=i), quantity=values[key]) for key, i in routes.items()]
    for name in ('routes', 'budget', 'managed_instruments'):
        target['shared'][name] = deepcopy(declaration[name])
    target['shared'].setdefault('bootstrap_signals', {})[added] = deepcopy(option_signal['signal'])
    journal = target['shared'].setdefault('event_journal', {'version': 1, 'events': {}, 'ack': {}, 'requests': {}})
    source_journal = shared['shared'].get('event_journal', {})
    journal['events'][added] = deepcopy(source_journal.get('events', {}).get(added, []))
    journal['ack'][added] = source_journal.get('ack', {}).get(added, 0)
    requests = source_journal.get('requests', {})
    if journal['requests'].keys() & requests.keys():
        raise ValueError('原08的跨进程请求ID冲突')
    journal['requests'].update(requests)
    if option_signal.get('cursor', journal['ack'][added]) != journal['ack'][added]:
        raise ValueError('原08尚有未确认的成交事件；不能跳过或重复交付')
    known = sorted(set(target['known_instruments']) | set(routes.values()))
    target['known_instruments'] = known
    payload['portfolio']['known_keys'] = [dict(client_id=client, instrument_id=i) for i in known]
    target['shared']['suspended'] = sorted(set(target['shared']['suspended']) | {added})
    from bomber.framework.trader.runtime.live.continuous import orders_for_day
    budget = dict(limit=current['account']['max_session_orders'], submitted=orders_for_day(payload, account.expected_day))
    account.driver.restore_order_budget(budget)
    target['shared']['order_budget'] = budget
    if payload['ctp_ledgers'][client]['trading_day'] != account.expected_day:
        account.rollover_audit = observe_rollover(payload, driver=account.driver, transport=account.transport,
            day=account.expected_day, multipliers={i: r.contract_multiplier for i, r in account.service.instrument_limits.items()},
            scope=account.position_scope, preserve_external=True, generic_close_venues=account.generic_close_venues)
    payload['component_states']['session_identity']['identity'] = deepcopy(current)
    payload['component_states']['external_positions']['managed_instruments'] = current['managed_instruments']
    return restore_external_baseline(account, payload, refresh=True)


def main(argv=None):
    import argparse
    from bomber.framework.trader.persistence import JsonStateStore
    from .run import build_host
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--option-checkpoint', type=Path)
    parser.add_argument('--option-signal', type=Path)
    parser.add_argument('--trading-day', required=True)
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--apply', action='store_true', help='禁单柜台核验成功后提交注册；保留原账户代次备份')
    parser.add_argument('--env-file', type=Path, help='账户env文件；策略参考资料使用各自env')
    args = parser.parse_args(argv)
    config, sources = load_config(args.config)
    config['trading_day'] = args.trading_day
    if args.apply != args.connect:
        parser.error('禁单注册须同时connect及apply；默认只检查文件，不连接、不修改')
    option_path = args.option_checkpoint or sources['option_checkpoint']
    if not option_path:
        parser.error('须指定原08成交检查点，不能自动以空归属启动')
    option_path = Path(option_path).resolve()
    signal_path = (args.option_signal or sources['option_signal'] or Path(str(option_path)+'.vega-signal.json'))
    row, = [r for r in config['strategies'] if r['kind'] == 'option_vega']
    protected = {Path(config['account_state_file']), *(Path(r['state_file']) for r in config['strategies'])}
    if option_path in protected or Path(signal_path).resolve() in protected:
        parser.error('来源检查点必须独立，不能覆盖账户或任一worker信号文件')
    if sources.get('base_config') is None:
        parser.error('首次注册须配置registration.source_account_config，指向原账户配置；日常启动不使用该来源')
    original_config = read_config(sources['base_config'])
    initial = JsonStateStore(config['account_state_file']).load()
    if initial is None:
        raise ValueError(f"检查点不存在: {config['account_state_file']}")
    # --connect --apply也先执行：配置差异不需要登录、查询DDB或组装策略才能发现。
    validate_source_configuration(initial.payload, original_config)
    host = None
    try:
        if args.connect:
            load_environment(args.env_file or config.get('account_env_file'))
            host = build_host(config, resume=True, read_only=True, strategy_processes=True,
                session_order_budget=config['account']['max_session_orders'])
            for path in [option_path, signal_path, *(r['state_file'] for r in config['strategies'])]:
                host.resources.enter_context(strategy_state_lock(Path(path)))
        def load(path):
            saved = JsonStateStore(path).load()
            if saved is None:
                raise ValueError(f'检查点不存在: {path}')
            return saved
        base = load(config['account_state_file'])
        validate_source_configuration(base.payload, original_config)  # 取得账户锁后重新核对。
        if row['strategy_id'] in base.payload['component_states']['session_identity']['identity']['strategies']:
            raise ValueError('08已经注册；请使用run_workers恢复，不能重复导入旧仓')
        option, signal = load(option_path), load(signal_path)
        for original_row in original_config['strategies']:
            persisted = load(original_row['state_file']).payload
            previous = base.payload['component_states']['session_identity']['identity']['strategies'][original_row['strategy_id']]
            if (persisted.get('identity') != previous or 'signal' not in persisted
                    or previous['argv'] != original_row['argv']):
                raise ValueError('原Hybrid信号/批次与账户身份或配置不一致，禁止覆盖')
        if Path(row['state_file']).exists():
            raise ValueError('08 worker信号文件已经存在；首次注册须使用独立未占用路径')
        quiescent(base.payload, config['account']['client_id'])
        quiescent(option.payload, next(iter(option.payload['ctp_drivers'])))
        if args.connect:
            host.account.start(resume=True, commit_restored=False, payload_transform=lambda payload: registration_payload(
                host.account, payload, option.payload, signal.payload, original_config,
                strategy_limits_only=sources['strategy_limits_only']))
            host.account.manager.validate_components()
            host.account.manager.commit_restore()
        print(json.dumps({'status': 'registered' if args.apply else 'files_checked', 'orders_enabled': False,
            'broker_verified': args.apply, 'account_state_file': config['account_state_file'],
            'source_option': str(option_path), 'source_generation': option.generation,
            'source_sha256': hashlib.sha256(option_path.read_bytes()).hexdigest(),
            'hybrid_signals_unchanged': True, 'account_limits': config['account'],
            'note': '默认检查不代表柜台核验；注册后仅启动worker，不再启动第二个TD'}, ensure_ascii=False, indent=2))
    finally:
        if host is not None:
            try:
                if host.account._started:
                    if not host.account.manager._restore_pending:
                        host.account.stop()
                    else:
                        host.account.abort_preparation()
                        host.account.driver.stop()
            finally:
                host.resources.close()


if __name__ == '__main__':
    main()
