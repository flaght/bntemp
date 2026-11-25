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
import csv
import os
import time
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parent.parent


class DirectStrategyReport:
    """直连模式的只读交易日归档；journal是来源，报告从不参与交易恢复。"""
    def __init__(self, host, item, report):
        self.host, self.item, self.report = host, item, report
        self.sid = item.strategy_id
        self.events = getattr(item.session, 'shared_events', [])
        self.owns_events = not hasattr(item.session, 'shared_events')
        self.cursor = 0
        self.rows = {'orders.csv':[], 'fills.csv':[], 'positions.csv':[]}
        self.seen = {}
        self.known_instruments = set()
        self.last_positions = None
        self.finished = False
        for name in ('orders.csv','fills.csv'):
            path = report.path/name
            self.seen[name] = set()
            if path.exists():
                with path.open(newline='') as stream:
                    self.seen[name] = {row['event_id'] for row in csv.DictReader(stream)}
        path = report.path/'positions.csv'
        if path.exists():
            with path.open(newline='') as stream:
                self.known_instruments = {row['instrument_id'] for row in csv.DictReader(stream)}
        report.metadata.update(execution='direct_ctp',strategy_id=self.sid)

    @staticmethod
    def wall_time(ns):
        return datetime.fromtimestamp(ns/1e9,ZoneInfo('Asia/Shanghai')).isoformat()

    def record(self):
        if self.finished or not self.host._prepared:
            return
        service = self.host.service
        with service.client._submit_lock:
            journal = service.event_journal
            incoming = deepcopy(journal.events[self.sid][self.cursor:])
            self.cursor += len(incoming)
            buckets = self.host.account.driver.buckets.snapshot_state()
            relevant = {row['instrument_id'] for row in buckets['positions'] if row['strategy_id'] == self.sid}
            relevant.update(row['instrument_id'] for row in buckets['frozen'].values() if row['strategy_id'] == self.sid)
            working = {str(instrument):str(service.position_manager.attribution.working_quantity(
                service.client_id,instrument,self.sid,key)) for key,instrument in service._ports[self.sid].routes.items()
                if str(instrument) in relevant}
        from bomber.framework.trader.execution.ipc import decode_event
        fresh = []
        for row in incoming:
            event = row['event']
            identity = event['identity']
            # 夜盘按柜台委托的交易日归档，不按本机自然日期分目录。
            if identity['client_order_id'].rsplit('-',2)[-2] != self.report.trading_day:
                continue
            name = 'fills.csv' if row['type'] == 'FillEvent' else 'orders.csv'
            if event['event_id'] in self.seen[name]:
                continue
            record = dict(event_id=event['event_id'],strategy_id=self.sid,
                instrument_id=identity['instrument_id'],client_order_id=identity['client_order_id'],
                venue_order_id=identity.get('venue_order_id'),side=event['side'],ts_event=event['ts_event'],
                report_time=self.wall_time(event['ts_event']))
            if name == 'fills.csv':
                record.update(trade_id=event['trade_id'],position_effect=event.get('position_effect'),
                    quantity=event['quantity'],price=event['price'],commission=event.get('commission'),
                    commission_currency=event.get('commission_currency'),
                    ctp_trade_date=event.get('metadata',{}).get('ctp_trade_date'),
                    ctp_trade_time=event.get('metadata',{}).get('ctp_trade_time'))
            else:
                record.update(status=event['status'],order_quantity=event['order_quantity'],
                    cumulative_filled=event['cumulative_filled'],reason=event.get('reason'))
            self.rows[name].append(record)
            self.seen[name].add(event['event_id'])
            fresh.append(row)
        for name in ('orders.csv','fills.csv'):
            self.report.record_csv(name,self.rows[name])
        if self.owns_events:
            self.events.extend(decode_event(row) for row in fresh)
        self.report.record(self.item.session,self.events)
        positions = [row for row in buckets['positions'] if row['strategy_id'] == self.sid]
        frozen = {key:row for key,row in buckets['frozen'].items() if row['strategy_id'] == self.sid}
        snapshot = dict(positions=positions,frozen=frozen,
            working={key:value for key,value in working.items() if Decimal(value)})
        if snapshot != self.last_positions:
            stamp = time.time_ns()
            by_instrument = {row['instrument_id']:row['quantities'] for row in positions}
            self.known_instruments.update(by_instrument)
            self.known_instruments.update(row['instrument_id'] for row in frozen.values())
            for instrument in sorted(self.known_instruments):
                values = by_instrument.get(instrument,['0']*4)
                freezes = [row['buckets'] for row in frozen.values() if row['instrument_id'] == instrument]
                names = ('long_today','long_yesterday','short_today','short_yesterday')
                self.rows['positions.csv'].append(dict(strategy_id=self.sid,instrument_id=instrument,
                    snapshot_time=self.wall_time(stamp),ts_snapshot=stamp,
                    **dict(zip(names,values)),
                    **{'frozen_'+name:str(sum((Decimal(row[n]) for row in freezes),Decimal(0))) for n,name in enumerate(names)},
                    working_quantity=working.get(instrument,'0')))
            self.report.record_csv('positions.csv',self.rows['positions.csv'])
            document = dict(trading_day=self.report.trading_day,run_id=self.report.run_id,
                strategy_id=self.sid,snapshot_time=self.wall_time(stamp),
                scope='strategy_owned_report_not_recovery_checkpoint',**snapshot)
            temporary = self.report.path/('positions.json.'+self.report.run_id+'.tmp')
            temporary.write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n')
            os.replace(temporary,self.report.path/'positions.json')
            self.last_positions = snapshot

    def finish(self, *, status=None, failure=None):
        if self.finished:
            return
        self.record()
        session = self.item.session
        failure = failure or session.runner.failure
        if not self.host._prepared:
            status = status or 'failed'
            failure = failure or '策略尚未完成准备及授权'
        self.report.write(status=status or ('failed' if failure else 'completed'),
            fields={'failure':failure,'strategy_id':self.sid},
            session=session if self.host._prepared else None,events=self.events)
        if hasattr(session,'shared_report'):
            session.shared_report = None
        self.finished = True


def configure_direct_reports(host, config):
    reports = []
    for row in config['strategies']:
        item = host.strategies[row['strategy_id']]
        session = item.session
        if row['kind'] == 'option_vega':
            from importlib import import_module
            report = import_module('demos.08_option_vega.reports').OptionLiveReport(config['report_dir'],
                prefix=item.strategy_id,trading_day=config['trading_day'],
                metadata={'mode':'simnow','strategy_parameters':session.session_identity.get('config',{})})
            report.begin()
        else:
            report = session.shared_report
        adapter = DirectStrategyReport(host,item,report)
        session.finish_report = adapter.finish
        reports.append(adapter)
    host.direct_reports = reports


def record_direct_reports(host):
    for report in getattr(host,'direct_reports',()):
        report.record()


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


def _extend_option_scope(account, payload, old, new):
    """跨日只新增08候选路由；旧持仓、成交、目标版本及计数均不重建。"""
    before, after = set(old.get('managed_instruments', ())), set(new.get('managed_instruments', ()))
    if not before < after:
        raise ValueError('直连自动恢复仅支持新增08候选，管理范围移除须另行核验')
    shared = payload['component_states']['shared_account']
    current = account.service.snapshot_state()['shared']['routes']
    added_instruments = set()
    for sid, declaration in current.items():
        previous = shared['shared']['routes'][sid]
        if previous == declaration:
            continue
        if new['strategies'][sid]['kind'] != 'option_vega':
            raise ValueError('直连管理范围变化包含非08路由')
        routes, old_routes = declaration['routes'], previous['routes']
        if (not old_routes.keys() <= routes.keys()
                or any(routes[key] != instrument or declaration['limits'][instrument] != previous['limits'][instrument]
                       for key, instrument in old_routes.items())):
            raise ValueError('08新增候选不能移除或改变原路由及限额')
        expected = deepcopy(previous)
        expected.update(routes=deepcopy(routes), limits=deepcopy(declaration['limits']))
        if expected != declaration or declaration != new['strategies'][sid]['routes']:
            raise ValueError('08新增候选不能改变原预算或策略声明')
        added = {key: instrument for key, instrument in routes.items() if key not in old_routes}
        if set(added.values()) & before:
            raise ValueError('新增08候选不能重绑定已有管理合约')
        added_instruments.update(added.values())
        source = shared['sources'].get(sid)
        if source:
            for key, instrument in added.items():
                source['targets'][instrument] = '0'
                source['rows'].append(dict(strategy_id=sid, target_key=key, instrument_id=instrument, quantity='0'))
                payload['portfolio']['contributions'][sid].append(dict(
                    key={'client_id': account.service.client_id, 'instrument_id': instrument}, quantity='0'))
    if after - before != added_instruments - before:
        raise ValueError('新增管理合约必须全部来自已校验的08候选路由')
    shared['shared']['routes'] = deepcopy(current)
    shared['shared']['managed_instruments'] = sorted(after)
    shared['known_instruments'] = sorted(after)
    payload['portfolio']['known_keys'] = [dict(client_id=account.service.client_id, instrument_id=i) for i in sorted(after)]
    payload['component_states']['external_positions']['managed_instruments'] = sorted(after)
    logging.getLogger('DirectCtpRecovery').info(
        '08跨日新增候选，原归属与目标版本保留，待柜台核验后提交: added=%s', sorted(after-before))


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
        if old['strategies'].keys() != new['strategies'].keys():
            raise ValueError('直连跨日不能更改策略集合')
        for sid in new['strategies']:
            previous = deepcopy(old['strategies'][sid])
            previous['trading_day'] = old['trading_day']
            validate_strategy_identity(previous, new['strategies'][sid])
        candidate.update(trading_day=day, strategies=deepcopy(new['strategies']))
        if old.get('managed_instruments') != new.get('managed_instruments'):
            _extend_option_scope(account, result, old, new)
            candidate['managed_instruments'] = deepcopy(new['managed_instruments'])
    elif old['strategies'] != new['strategies']:
        if old['strategies'].keys() != new['strategies'].keys():
            raise ValueError('直连恢复不能更改策略集合')
        for sid in new['strategies']:
            if old['strategies'][sid] != new['strategies'][sid]:
                validate_strategy_identity(old['strategies'][sid], new['strategies'][sid])
        candidate['strategies'] = deepcopy(new['strategies'])
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


def configure_execution_quotes(host, config):
    """从各策略原market_config取Tick流表；连接直接复用对应Bar的DDB配置。"""
    if config.get('execution_driver', 'ctp') != 'nt_algo_ctp':
        return
    if config.get('execution_tick_source') is not None:
        raise ValueError('请移除execution_tick_source，在market_dolphindb.json中配置tick_table')
    from bomber.framework.market.basic.base import DataType
    from bomber.framework.market.sources import create_dolphin_tick_feed
    groups, assignments = {}, {}
    for item in host.strategies.values():
        session = item.session
        feeds = (getattr(session, 'upstream', None), *getattr(session, 'feeds', {}).values())
        for bar in feeds:
            source = getattr(bar, 'market_source_config', None)
            if source is None or source.tick_table is None:
                continue
            connection = bar.config
            key = (connection.host, connection.port, connection.username, connection.password,
                connection.streaming_port, connection.keep_alive_seconds, source.tick_max_age_seconds)
            group = groups.setdefault(key, dict(connection=connection, tables=set(), instruments={},
                max_age_seconds=source.tick_max_age_seconds))
            group['tables'].add(source.tick_table)
            for instrument in set(session.client.routes.values()):
                if instrument not in host.service.instrument_limits:
                    continue
                meta = bar.get_instrument_meta(instrument)
                if meta is None:
                    raise ValueError(f'Tick配置的执行合约未注册到对应Bar行情源: {instrument}')
                previous = assignments.setdefault(instrument, (key, source.tick_table))
                if previous != (key, source.tick_table):
                    raise ValueError(f'同一合约配置了不同Tick流表或连接: {instrument}')
                group['instruments'][instrument] = meta
    if not groups:
        logging.getLogger('ExecutionQuotes').info('执行报价使用Bar close: market_config.tick_table未配置')
    for index, group in enumerate(groups.values()):
        if not group['instruments']:
            continue
        tables = sorted(group['tables'])
        feed = create_dolphin_tick_feed(tables, f'execution-quotes-{index}',
            market_config=group['connection'], max_age_seconds=group['max_age_seconds'])
        for instrument, meta in group['instruments'].items():
            feed.register_instrument(meta)
            feed.subscribe(instrument, DataType.QUOTE_TICK)
        host.service.prices.enable_tick_quotes(group['instruments'],
            max_age_ns=max(1, int(group['max_age_seconds'] * 1e9)))
        feed.register_tick_snapshot_handler(host.service.prices.on_tick_snapshot)
        host.resources.callback(feed.disconnect)
        feed.connect()  # 不把执行Tick接入Runner策略回调。
        logging.getLogger('ExecutionQuotes').info('执行报价已订阅DolphinDB Tick: tables=%s max_age_seconds=%s',
            tables, group['max_age_seconds'])


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
        configure_execution_quotes(host, config)
        configure_direct_reports(host,config)
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
    for report in getattr(host,'direct_reports',()):
        try:
            report.finish(status='disconnected',failure='CTP连接中断；仅保留最后有效恢复账本')
        except Exception:
            logging.exception('断线会话的交易日报告写入失败')
    for item in host.strategies.values():
        with item.session.runner._submit_lock:
            item.session.runner.accept_bars = False
        try:
            item.session.runner.stop()
        except Exception:
            logging.exception('断线后清理策略Runner失败')
    host.account.driver.stop()
    host.resources.close()


def _can_reconnect(host, error, attempts):
    """仅重建可恢复连接；归属/回报冲突不能被查询超时重试分支绕过。"""
    if host is not None:
        if host.service.client.report_errors:
            return False
        reason = getattr(host.account.driver, 'last_disconnect', None)
        if reason is not None:
            # CTP SDK的网络读写、心跳和错误报文断线；-1等本地冲突保持闭闸。
            return reason['reason'] in {0x1001, 0x1002, 0x2001, 0x2002, 0x2003}
    return attempts > 0 and isinstance(error, (ConnectionError, TimeoutError))


def run_window(config, window, stop, *, import_state, counter_file):
    from bomber.framework.trader.runtime.live.continuous import wait_for_open, StopRequested
    attempts = 0
    while not stop.is_set() and datetime.now(window.end.tzinfo) < window.end:
        host = None
        from bomber.framework.trader.runtime.reports import TradingDayLiveReport
        report = TradingDayLiveReport(config['report_dir'],prefix='direct-ctp-runtime',trading_day=window.trading_day,metadata={
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
            record_direct_reports(host)
            while not stop.is_set() and datetime.now(window.end.tzinfo) < window.end:
                host.poll()
                record_direct_reports(host)
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
            retry = not stop.is_set() and _can_reconnect(host, error, attempts)
            if host is not None:
                if retry or not host.account.driver.is_simnow_authenticated:
                    dispose_disconnected(host)
                else:
                    try:
                        host.stop()
                    except Exception as stop_error:
                        # 首次查询超时时连接可能仍显示在线；停机排空期间才收到断线。
                        # 必须在销毁连接前重读原因，保留原账本和计数文件重建宿主。
                        retry = not stop.is_set() and _can_reconnect(host, stop_error, attempts)
                        logging.getLogger('DirectCtpRecovery').warning(
                            '停机核验失败: original_error=%s stop_error=%s disconnect=%s reconnect=%s',
                            error, stop_error, getattr(host.account.driver, 'last_disconnect', None), retry)
                        dispose_disconnected(host)
                        if not retry:
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
    parser.add_argument('--report-dir',type=Path,default=demo/'results/direct',
        help='本进程独立报告目录；多账号运行时分别指定，默认沿用原目录')
    parser.add_argument('--counter-file',type=Path,default=PROJECT/'demos/shared_account/results/daily_orders.sqlite3')
    parser.add_argument('--import-state',type=Path,help='10首次接续的最新公共账户成交账本，原文件只读')
    parser.add_argument('--prepare-seconds',type=int,default=600)
    parser.add_argument('--stop-before-close-seconds', type=int, default=180,
        help='每个交易时段结束前提前多少秒停止策略报单并撤单对账（默认180，0表示到时段结束才停机）')
    parser.add_argument('--run-forever',action='store_true')
    parser.add_argument('--execution-driver', choices=('ctp', 'nt_algo_ctp'), default='ctp',
        help='ctp沿用原独立归属Driver；nt_algo_ctp启用公共NT算法积木')
    parser.add_argument('--execution-algorithm', choices=('limit_reprice',),
        help='执行算法；nt_algo_ctp未指定时默认limit_reprice（限价超时撤单重报）')
    parser.add_argument('--algo-wait-seconds', type=float, default=10,
        help='NT限价委托等待秒数，撤单确认后才按有效报价重报')
    parser.add_argument('--algo-max-attempts', type=int, default=3,
        help='每个NT任务的最大实际报单次数，包含首次')
    parser.add_argument('--algo-retry-step-ticks', type=int, choices=(0, 1), default=1,
        help='每次重报相对上次实际委托价买加/卖减的跳数；首次沿用账户limit_offset_ticks')
    parser.add_argument('--execution-tick-source',
        help='旧字段已停用；请在策略market_dolphindb.json配置tick_table')
    if kind == 'hybrid_transformer':
        alignment_entry = import_module('demos.10_hybrid_transformer.reconcile_direct_positions')
        parser.add_argument('--config',type=Path,default=demo/'config/instances_rb_p.json')
        parser.add_argument('--align-positions', nargs='+', type=alignment_entry.parse_position,
            help='本次启动先按当前实际仓位对齐，再继续交易，例如 p2701.DCE=2 rb2701.SHFE=0')
    else:
        parser.add_argument('--strategy-id',default='option-vega-direct')
        parser.add_argument('--state-file',type=Path,default=demo/'results/direct/signal.json')
        parser.add_argument('--live-config-file',type=Path,default=demo/'config/strategy.yaml')
        parser.add_argument('--live-config-id',default='live_simnow')
    for flag in ('connect','enable-orders','confirm-simnow'):
        parser.add_argument('--'+flag,action='store_true')
    options = parser.parse_args(argv)
    alignment_positions = getattr(options, 'align_positions', None)
    if alignment_positions:
        if not options.connect:
            parser.error('align-positions须配合connect，在柜台核验后执行')
        if len({instrument.casefold() for instrument, _ in alignment_positions}) != len(alignment_positions):
            parser.error('align-positions不能重复声明同一合约')
    if options.prepare_seconds < 0:
        parser.error('prepare-seconds须为非负整数')
    if options.stop_before_close_seconds < 0:
        parser.error('stop-before-close-seconds须为非负整数')
    if options.execution_tick_source is not None:
        parser.error('请移除execution_tick_source，在策略market_dolphindb.json中配置tick_table；Tick与Bar统一从DolphinDB订阅')
    account = read_account_config(options.account_config)
    if options.execution_driver == 'nt_algo_ctp':
        from math import isfinite
        if not isfinite(options.algo_wait_seconds) or options.algo_wait_seconds <= 0:
            parser.error('algo-wait-seconds须为有限正数')
        if not 1 <= options.algo_max_attempts <= 1000:
            parser.error('algo-max-attempts须为1至1000整数')
        account.update(execution_driver='nt_algo_ctp',
            execution_algorithm=options.execution_algorithm or 'limit_reprice', nt_algo=dict(
            wait_seconds=options.algo_wait_seconds, max_attempts=options.algo_max_attempts,
            retry_step_ticks=options.algo_retry_step_ticks))
    elif (options.execution_algorithm is not None
            or options.algo_wait_seconds != 10 or options.algo_max_attempts != 3
            or options.algo_retry_step_ticks != 1 or options.execution_tick_source is not None):
        parser.error('算法参数须配合execution-driver=nt_algo_ctp')
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
        'report_dir':str(options.report_dir.resolve()), 'seconds':86400,
        'strategy_env_files':{row['strategy_id']:str(options.env_file.resolve()) for row in rows}}
    if not options.connect:
        if options.enable_orders or options.confirm_simnow:
            parser.error('离线校验不接受报单授权')
        print(json.dumps(dict(status='config_checked',mode='direct_ctp', strategies=[r['strategy_id'] for r in rows],
            state_file=config['account_state_file'], import_state=str(source) if source else None,
            report_dir=config['report_dir'], counter_file=str(options.counter_file.resolve()),
            env_file=str(options.env_file.resolve()),
            execution_driver=options.execution_driver, execution_algorithm=config.get('execution_algorithm'),
            nt_algo=config.get('nt_algo'),
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
    if alignment_positions:
        from bomber.framework.trader.persistence import JsonStateStore
        from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport
        from bomber.framework.trader.execution.ctp import CtpTdApiTransport
        saved = JsonStateStore(options.account_state_file).load()
        if saved is None:
            raise FileNotFoundError(options.account_state_file)
        client_id = next(iter(saved.payload['ctp_drivers']))
        transport = build_simnow_transport(CtpTdApiTransport, client_id=client_id,
            flow_path='/tmp/bomber-hybrid-position-alignment')
        alignment_entry.reconcile(state_file=options.account_state_file, rows=rows,
            expected=dict(alignment_positions), transport=transport)
        print('当前仓位已对齐，继续启动直连策略', flush=True)
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
            stop_before_close_seconds = options.stop_before_close_seconds
            if stop_before_close_seconds >= (window.end - window.start).total_seconds():
                print(f'收盘缓冲覆盖整个交易时段，跳过 TD={window.trading_day} segment={window.segment_id}',
                    flush=True)
                return SimpleNamespace(run=lambda: {'status': 'skipped'})
            current = {**config, 'trading_day':window.trading_day}
            own_window = replace(window, end=window.end - timedelta(seconds=stop_before_close_seconds))
            print(f'直连停机时间: {own_window.end.isoformat()}，提前 {stop_before_close_seconds} 秒', flush=True)
            return SimpleNamespace(run=lambda:run_window(current,own_window,event,
                import_state=source,counter_file=options.counter_file))
        print(f"本进程直连CTP：{[r['strategy_id'] for r in rows]}；昨仓优先；无需账户服务",flush=True)
        return ContinuousPositionRuntime(calendar,factory,stop_event=stop,
            run_forever=options.run_forever,prepare_seconds=options.prepare_seconds).run()
