"""08/10直连入口共用的装配及恢复；不启动独立账户服务。"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
import json
import logging

PROJECT = Path(__file__).resolve().parent.parent
def project_hybrid(payload, account):
    """从最新公共成交账本提取RB/P；原文件、信号版本、成交成本保持不变。"""
    from bomber.framework.trader import persistence as storage
    from bomber.framework.trader.execution.ctp.independent import BUCKETS
    result = deepcopy(payload)
    client, ids = account.service.client_id, set(account.service._ports)
    components = result['component_states']
    old = components['session_identity']['identity']
    new = account.manager.state_components['session_identity'].identity
    if old['account'] != new['account'] or old['environment'] != new['environment']:
        raise ValueError('10导入账户身份或限额与原公共账本不一致')
    shared = components['shared_account']
    if shared.get('failed') or shared.get('pending') or any(row['orders'] for row in result['ctp_drivers'].values()):
        raise ValueError('切换10入口前须正常停止旧账户及策略，旧账本仍有未决发送/活动委托')
    if any(not row.state.status.is_terminal for rows in result['orders'].values() for row in storage._decode_orders(rows)):
        raise ValueError('切换前旧委托必须全部终结')
    routes = account.service.snapshot_state()['shared']['routes']
    if not ids <= shared['shared']['routes'].keys() or any(shared['shared']['routes'][sid] != routes[sid] for sid in ids):
        raise ValueError('10策略ID、合约路由或策略风控与原账本不一致')
    instruments = {value for row in routes.values() for value in row['routes'].values()}
    positions = result['positions']
    owned = positions['attribution']['owned']
    if any(row['strategy_id'] not in ids and row['instrument_id'] in instruments and Decimal(row['quantity']) for row in owned):
        raise ValueError('导入合约有其他策略归属，无法无歧义拆分旧今昨仓')
    for name in ('strategy_positions', 'strategy_revisions'):
        positions[name] = [row for row in positions[name] if row['strategy_id'] in ids]
    for name in ('account_positions', 'working_quantities', 'account_revisions'):
        positions[name] = [row for row in positions[name] if row['key']['instrument_id'] in instruments]
    positions['attribution']['owned'] = [row for row in owned if row['strategy_id'] in ids]
    orders = positions['attribution']['orders']
    for row in orders.values():
        owners = {leg['strategy_id'] for leg in row['legs']}
        if owners & ids and not owners <= ids:
            raise ValueError('旧母单包含待移出的策略，不能拆改单笔历史成交归属')
    orders = {key: row for key, row in orders.items() if {leg['strategy_id'] for leg in row['legs']} <= ids}
    positions['attribution']['orders'] = orders
    positions['attribution']['transfers'] = [row for row in positions['attribution'].get('transfers', [])
        if row.get('instrument_id') in instruments]
    order_ids = {row['client_order_id'] for row in orders.values()}
    result['orders'][client] = [row for row in result['orders'][client] if row['client_order_id'] in order_ids]
    result['ctp_ledgers'][client]['positions'] = [row for row in result['ctp_ledgers'][client]['positions']
        if row['instrument_id'] in instruments]
    result['targets'] = [row for row in result['targets'] if row['strategy_id'] in ids]
    portfolio = result['portfolio']
    for name in ('strategy_revisions', 'contributions'):
        portfolio[name] = {sid: row for sid, row in portfolio[name].items() if sid in ids}
    portfolio['known_keys'] = [row for row in portfolio['known_keys'] if row['instrument_id'] in instruments]
    for name in ('sources', 'strategy_revisions'):
        shared[name] = {sid: row for sid, row in shared[name].items() if sid in ids}
    shared['known_instruments'] = sorted(instruments)
    shared['shared']['routes'] = routes
    shared['shared']['managed_instruments'] = sorted(account.position_scope.instruments)
    for name in ('suspended','draining'):
        shared['shared'][name] = [sid for sid in shared['shared'][name] if sid in ids]
    shared['shared']['bootstrap_signals'] = {sid: row for sid,row in shared['shared'].get('bootstrap_signals',{}).items() if sid in ids}
    journal = shared['shared'].get('event_journal')
    if journal:
        if any(row['status'] != 'completed' for row in journal['requests'].values()):
            raise ValueError('原账户仍有未决跨进程请求')
        for name in ('events','ack'):
            journal[name] = {sid: row for sid, row in journal[name].items() if sid in ids}
        journal['requests'] = {}  # 已完成RPC的幂等缓存不属于新的进程执行协议。
    components.pop('execution_registry', None)
    components['ctp_order_owners'] = account.driver.book.snapshot_state()
    buckets = account.driver.buckets.snapshot_state()
    buckets['trading_day'] = result['ctp_ledgers'][client]['trading_day']
    for row in result['ctp_ledgers'][client]['positions']:
        owners = {r['strategy_id'] for r in positions['attribution']['owned']
            if r['instrument_id'] == row['instrument_id'] and Decimal(r['quantity'])}
        if len(owners) > 1:
            raise ValueError('同合约多个旧策略缺少独立今昨成本，拒绝猜测')
        if owners:
            buckets['positions'].append(dict(strategy_id=next(iter(owners)), instrument_id=row['instrument_id'],
                quantities=[row[name] for name in BUCKETS]))
    components['strategy_buckets'] = buckets
    components['external_positions']['managed_instruments'] = sorted(account.position_scope.instruments)
    # 策略业务身份仍由各自既有signal检查点核验；公共账户只保存执行登记。
    components['session_identity']['identity'] = {**deepcopy(new), 'trading_day':old['trading_day']}
    return result


def prepare_payload(account, payload):
    """仅推进交易日，结转本策略逻辑今昨桶；柜台核验在恢复后统一完成。"""
    from bomber.framework.trader.runtime.live.recovery import identity_difference_paths
    from scripts.integration.shared_account.rollover import validate_strategy_identity
    result = deepcopy(payload)
    components = result['component_states']
    old = components['session_identity']['identity']
    new = account.manager.state_components['session_identity'].identity
    candidate = deepcopy(old)
    day = account.expected_day
    if old['trading_day'] > day:
        raise ValueError('不能回退策略交易日')
    if old['trading_day'] != day:
        for sid in new['strategies']:
            previous = deepcopy(old['strategies'][sid])
            previous['trading_day'] = old['trading_day']
            validate_strategy_identity(previous, new['strategies'][sid])
        candidate.update(trading_day=day, strategies=deepcopy(new['strategies']))
    if candidate != new:
        raise ValueError('直连恢复配置不一致: '+','.join(identity_difference_paths(candidate,new)))
    client = account.service.client_id
    if result['ctp_ledgers'][client]['trading_day'] != day:
        from bomber.framework.trader import persistence as storage
        if (result['ctp_drivers'][client]['orders'] or any(not row.state.status.is_terminal
                for rows in result['orders'].values() for row in storage._decode_orders(rows))
                or components['strategy_buckets']['frozen']):
            raise ValueError('跨日仍有未结束委托，需先恢复旧日真实成交，不能猜测结转')
        ledger = result['ctp_ledgers'][client]
        for row in ledger['positions']:
            for side in ('long','short'):
                today, yesterday = (Decimal(row[side+suffix]) for suffix in ('_today','_yesterday'))
                total = today+yesterday
                basis = (today*Decimal(row[side+'_today_basis'])+yesterday*Decimal(row[side+'_yesterday_basis']))/total if total else Decimal(0)
                row.update({side+'_today':'0', side+'_today_basis':'0', side+'_yesterday':str(total), side+'_yesterday_basis':str(basis)})
        ledger['trading_day'] = day
        for row in components['strategy_buckets']['positions']:
            a,b,c,d = map(Decimal,row['quantities'])
            row['quantities'] = ['0',str(a+b),'0',str(c+d)]
        components['strategy_buckets']['trading_day'] = day
    shared = components['shared_account']
    shared['pending'] = shared['failed'] = False  # 只在随后完成订单/成交权威核验后才允许提交。
    shared['shared']['suspended'] = list(account.service._ports)
    shared['shared']['draining'] = []
    components['session_identity']['identity'] = deepcopy(new)
    return result


def build_direct_host(config, *, import_state=None, counter_file=None):
    from scripts.integration.shared_account.run import build_host
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.execution.ctp.independent import DailyOrderCounter
    existing = JsonStateStore(config['account_state_file']).load()
    source = None
    if existing is None and import_state is not None:
        source = JsonStateStore(import_state).load()
        if source is None:
            raise ValueError(f'10原公共账本不存在: {import_state}；禁止空仓启动代替恢复')
    host = build_host(config, resume=existing is not None or source is not None, direct=True)
    try:
        account = host.account
        account.import_transform = lambda payload: prepare_payload(account, payload)
        if source is not None:
            account.initial_payload = project_hybrid(source.payload, account)
            print(f'10保留策略状态：来源={Path(import_state).resolve()} generation={source.generation}；待柜台核验后写入本进程账本', flush=True)
        counter = DailyOrderCounter(counter_file or PROJECT/'demos/shared_account/results/daily_orders.sqlite3',
            account.transport.broker_id+':'+account.transport.investor_id,
            config['account']['max_session_orders'])
        for filename in {str(import_state or ''),config.get('budget_source_file',''),config['account_state_file']} - {''}:
            if Path(filename).is_file():
                counter.seed(JsonStateStore(filename).load().payload)
        account.driver.daily_counter = counter
        return host
    except BaseException:
        host.account.driver.stop()
        host.resources.close()
        raise


def dispose_disconnected(host):
    """断线仅销毁连接/Runner；保留最后同代账本，绝不按未核验内存覆盖。"""
    host.service._checkpoint = None
    host.service._cancel_owner = lambda sid: None
    host.service.suspend_strategy = lambda sid: None
    for item in host.strategies.values():
        with item.session.runner._submit_lock:
            item.session.runner.accept_bars = False
        try:
            item.session.runner.stop()
        except Exception:
            logging.exception('断线后清理策略Runner失败')
    host.account.driver.stop()
    host.resources.close()


def run_window(config, window, stop, *, import_state, counter_file):
    from bomber.framework.trader.runtime.live.continuous import wait_for_open, StopRequested
    attempts = 0
    while not stop.is_set() and datetime.now(window.end.tzinfo) < window.end:
        host = None
        from bomber.framework.trader.runtime.reports import LiveRunReport
        report = LiveRunReport(config['report_dir'],prefix='direct-ctp',metadata={
            'trading_day':window.trading_day,'strategies':[row['strategy_id'] for row in config['strategies']]})
        report.begin()
        status, failure = 'failed', None
        try:
            host = build_direct_host(config, import_state=import_state, counter_file=counter_file)
            host.activation_wait = lambda: wait_for_open(window.start,stop,poll=host.poll_preparation)
            def check_preparation():
                if stop.is_set() or datetime.now(window.end.tzinfo) >= window.end:
                    raise StopRequested()
            host.preparation_check = check_preparation
            host.start()
            while not stop.is_set() and datetime.now(window.end.tzinfo) < window.end:
                host.poll()
                stop.wait(0.05)
            host.stop()
            status = 'completed'
            return {'status':'completed'}
        except StopRequested:
            if host is not None:
                if host.account.driver.is_simnow_authenticated:
                    host.stop()
                else:
                    dispose_disconnected(host)
            status = 'completed'
            return {'status':'completed'}
        except Exception as error:
            failure = str(error)
            reason = getattr(host.account.driver, 'last_disconnect', None) if host is not None else None
            retry = reason is not None and reason['reason'] != -1 and not host.service.client.report_errors
            retry = retry or (attempts > 0 and isinstance(error,(ConnectionError,TimeoutError)))
            if host is not None:
                if retry or not host.account.driver.is_simnow_authenticated:
                    dispose_disconnected(host)
                else:
                    try:
                        host.stop()
                    except Exception:
                        dispose_disconnected(host)
                        raise
            if not retry or attempts >= 3:
                raise
            attempts += 1
            status = 'reconnecting'
            print(f'CTP连接中断，闭闸后重连恢复: attempt={attempts}/3；保留旧会话订单关联和冻结', flush=True)
            stop.wait(5)
        finally:
            try:
                snapshot = host.snapshot() if host is not None else {}
            except Exception as error:
                snapshot = {'snapshot_error':str(error)}
            report.write(status=status,fields={**snapshot,'failure':failure},session=None,events=())
    return {'status':'completed'}


def main(kind, argv=None):
    from bomber.framework.cli import ConfigArgumentParser
    from bomber.framework.account_config import read_account_config
    parser = ConfigArgumentParser(description='策略进程直连CTP；无需账户服务；不带--connect只检查配置')
    demo = PROJECT/'demos'/('10_hybrid_transformer' if kind == 'hybrid_transformer' else '08_option_vega')
    parser.add_argument('--account-config',type=Path,default=PROJECT/'demos/shared_account/account.json')
    parser.add_argument('--env-file',type=Path,default=demo/'config/.env')
    parser.add_argument('--account-state-file',type=Path,default=demo/'results/direct/state.json')
    parser.add_argument('--counter-file',type=Path,default=PROJECT/'demos/shared_account/results/daily_orders.sqlite3')
    parser.add_argument('--import-state',type=Path,help='10首次接续的最新公共账户成交账本，原文件只读')
    parser.add_argument('--prepare-seconds',type=int,default=600)
    parser.add_argument('--run-forever',action='store_true')
    if kind == 'hybrid_transformer':
        parser.add_argument('--config',type=Path,default=demo/'config/instances_rb_p.json')
    else:
        parser.add_argument('--strategy-id',default='option-vega-direct')
        parser.add_argument('--state-file',type=Path,default=demo/'results/direct/signal.json')
        parser.add_argument('--live-config-file',type=Path,default=demo/'config/strategy.yaml')
        parser.add_argument('--live-config-id',default='live_simnow')
    for flag in ('connect','enable-orders','confirm-simnow'):
        parser.add_argument('--'+flag,action='store_true')
    options = parser.parse_args(argv)
    if options.prepare_seconds < 0:
        parser.error('prepare-seconds须为非负整数')
    account = read_account_config(options.account_config)
    if account['environment'] != 'realtime' or account['execution_mode'] != 'independent':
        parser.error('直连入口要求realtime和independent')
    if any(account['account'][key] is not None for key in ('max_gross_quantity','max_gross_notional')):
        parser.error('直连模式账户总量/名义限额须为null；各策略继续使用自身有限风控及柜台资金校验')
    if kind == 'hybrid_transformer':
        declaration = json.loads(options.config.read_text())
        if not isinstance(declaration,dict) or set(declaration) != {'version','strategies'} or declaration['version'] != 1:
            parser.error('策略JSON只接受version=1和strategies')
        from scripts.integration.shared_account.config import resolve_config
        rows = resolve_config({**account,'strategies':declaration['strategies']},options.config)['strategies']
        source = options.import_state or Path(account['account_state_file'])
        if any(row['kind'] != kind for row in rows):
            parser.error('10入口仅接受Hybrid实例')
    else:
        if options.import_state:
            parser.error('08新策略不能导入旧08检查点')
        source = None
        rows = [dict(strategy_id=options.strategy_id,kind=kind,state_file=str(options.state_file.resolve()),
            argv=['--config-file',str(options.live_config_file.resolve()),'--config-id',options.live_config_id])]
    if Path(account['account_state_file']).resolve() == options.account_state_file.resolve():
        parser.error('直连账本必须使用独立新文件，不能覆盖原公共账本')
    if source is not None and source.resolve() == options.account_state_file.resolve():
        parser.error('导入来源不能与新成交账本使用同一文件')
    if len({row['strategy_id'] for row in rows}) != len(rows) or any(Path(row['state_file']).resolve() == options.account_state_file.resolve() for row in rows):
        parser.error('策略ID必须唯一；信号文件与成交账本不能相同')
    config = {**account, 'strategies':rows, 'account_state_file':str(options.account_state_file.resolve()),
        'budget_source_file':account['account_state_file'],
        'report_dir':str(demo/'results/direct'), 'seconds':86400,
        'strategy_env_files':{row['strategy_id']:str(options.env_file.resolve()) for row in rows}}
    if not options.connect:
        if options.enable_orders or options.confirm_simnow:
            parser.error('离线校验不接受报单授权')
        print(json.dumps(dict(status='config_checked',mode='direct_ctp', strategies=[r['strategy_id'] for r in rows],
            state_file=config['account_state_file'], import_state=str(source) if source else None,
            orders_enabled=False),ensure_ascii=False))
        return
    if not (options.enable_orders and options.confirm_simnow):
        parser.error('连接须显式enable-orders和confirm-simnow')
    from scripts.integration.shared_account.config import load_environment
    from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, StopSignal
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    from bomber.framework.trader.runtime.live.channels.ctp import account_lock, required
    load_environment(options.env_file)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    if kind == 'hybrid_transformer':
        calendar = import_module('demos.10_hybrid_transformer.run_shared').load_calendar(config)
    else:
        from bomber.framework.dataprep.session_inputs import load_session_inputs
        entry = import_module('demos.08_option_vega.run_live')
        business = entry.shared_arguments(rows[0],config,resume=options.state_file.is_file())
        calendar = load_session_inputs(entry.session_inputs(business))
        entry.validate_calendar(calendar,business.option_product)
    with ExitStack() as resources, StopSignal() as stop:
        resources.enter_context(strategy_state_lock(config['account_state_file']+'.direct-owner'))
        # 与旧10 worker互斥，避免两种入口同时消费同一份业务状态。
        for row in rows:
            resources.enter_context(strategy_state_lock(row['state_file']+'.worker-owner'))
            resources.enter_context(account_lock(required('CTP_ACCOUNT_ID')+':'+row['strategy_id'],
                namespace='bomber-direct-strategy'))
        config['direct_locks_held'] = True
        if source is not None:
            resources.enter_context(strategy_state_lock(str(source)+'.continuous-owner'))
            resources.enter_context(strategy_state_lock(source))
        def factory(window,event):
            current = {**config, 'trading_day':window.trading_day}
            own_window = replace(window,end=window.end-timedelta(seconds=30))
            return SimpleNamespace(run=lambda:run_window(current,own_window,event,
                import_state=source,counter_file=options.counter_file))
        print(f"本进程直连CTP：{[r['strategy_id'] for r in rows]}；昨仓优先；无需账户服务",flush=True)
        return ContinuousPositionRuntime(calendar,factory,stop_event=stop,
            run_forever=options.run_forever,prepare_seconds=options.prepare_seconds).run()
