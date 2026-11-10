"""一次性旧账本迁移：显式读取旧策略声明；日常账户服务完全不走此入口。"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .account_config import read_account_config, account_identity


def registry_from_host(host):
    from bomber.framework.calendars.sessions import SessionCalendar
    from bomber.framework.calendars.state import calendar_to_state
    from bomber.framework.trader.execution.registry import validate_registry
    planner = host.service.client.planner.planner
    sessions = []
    for item in host.strategies.values():
        session = item.session
        calendar = next((candidate for candidate in (
            getattr(session, 'session_calendar', None),
            getattr(getattr(session, 'references', None), 'calendar', None),
            getattr(getattr(session, 'strategy', None), 'calendar', None))
            if isinstance(candidate, SessionCalendar)), None)
        if calendar is None:
            raise ValueError('账户执行登记缺少完整SessionCalendar；估值日期元组不能作为交易时段快照')
        state = calendar_to_state(calendar)
        if state not in sessions:
            sessions.append(state)
    state = dict(version=1,
        instruments={str(i):dict(tick=str(planner._tick_size[i]),multiplier=str(r.contract_multiplier))
                     for i,r in host.service.instrument_limits.items()},
        ports={sid:port.configuration() for sid,port in host.service._ports.items()}, sessions=sessions)
    return validate_registry(state)


def isolate_payload(payload, config, registry):
    from bomber.framework.trader.execution.registry import validate_registry
    from .register_worker import quiescent
    result = deepcopy(payload)
    registry = validate_registry(registry)
    client = config['account']['client_id']
    quiescent(result, client)
    previous = result['component_states']['session_identity']['identity']
    if (previous['account'] != config['account'] or previous['environment'] != config['environment']
            or previous['trading_day'] != config['trading_day']
            or set(previous['strategies']) != set(registry['ports'])):
        raise ValueError('迁移后的执行身份、额度、交易日或归属策略与目标账户不一致')
    shared = result['component_states']['shared_account']['shared']
    if shared['routes'] != registry['ports'] or set(shared['managed_instruments']) != set(registry['instruments']):
        raise ValueError('纯执行登记与已核验账户路由不一致')
    if set(result['component_states']) != {'shared_account','session_identity','external_positions'}:
        raise ValueError('旧账户仍有独立业务组件，不能直接丢弃迁移')
    result['component_states']['execution_registry'] = registry
    result['component_states']['session_identity']['identity'] = account_identity(config, registry['ports'])
    return result


def read_sources(account, source_launch, option_path=None, signal_path=None, *, fresh_option=False):
    from .run_workers import load_config
    from .config import read_config
    from .register_worker import validate_source_configuration, validate_option_signal, quiescent
    from bomber.framework.trader.persistence import JsonStateStore
    combined, sources = load_config(source_launch)
    if combined['account'] != account['account']:
        raise ValueError('迁移声明的执行身份/预算须与目标账户相同，不在迁移时更换client_id或每日额度')
    source = Path(combined['account_state_file'])
    destination = Path(account['account_state_file'])
    if destination == source or destination.exists():
        raise ValueError('目标账户须使用独立未占用路径；不能覆盖原账户或重复迁移')
    base = JsonStateStore(source).load()
    if base is None:
        raise ValueError(f'原账户账本不存在: {source}')
    if 'execution_registry' in base.payload['component_states']:
        raise ValueError('来源已经是独立账户格式；直接使用account入口恢复')
    identity = base.payload['component_states']['session_identity']['identity']
    rows = {row['strategy_id']:row for row in combined['strategies']}
    incoming, = [row for row in rows.values() if row['kind'] == 'option_vega']
    joining = incoming['strategy_id'] not in identity['strategies']
    if fresh_option and not joining:
        raise ValueError('全新08策略ID已存在于原账户，不能清空原归属重新开始')
    if fresh_option and (option_path or signal_path or sources.get('option_checkpoint') or sources.get('option_signal')):
        raise ValueError('fresh-option不能同时指定旧08账本或信号')
    original = read_config(sources['base_config']) if sources.get('base_config') else None
    if joining and Path(incoming['state_file']).exists():
        raise ValueError('新08信号路径已占用，须使用独立未占用路径，不覆盖原文件')
    if joining:
        if original is None:
            raise ValueError('旧注册来源缺少原账户配置')
        validate_source_configuration(base.payload, original)
    else:
        if identity['account'] != account['account'] or set(identity['strategies']) != set(rows):
            raise ValueError('原账户策略集合或执行身份不匹配')
        for sid,row in rows.items():
            if any(identity['strategies'][sid][key] != row[key] for key in ('kind','argv')):
                raise ValueError(f'原策略参数与迁移声明不一致: {sid}')
    quiescent(base.payload, account['account']['client_id'])
    source_files = {source:source.read_bytes()}
    for sid, previous in identity['strategies'].items():
        row = rows[sid]
        path = Path(row['state_file'])
        saved = JsonStateStore(path).load()
        bootstrap = base.payload['component_states']['shared_account']['shared'].get('bootstrap_signals', {}).get(sid)
        if saved is None and bootstrap is not None:
            continue
        if saved is None or saved.payload.get('identity') != previous or 'signal' not in saved.payload:
            raise ValueError(f'原策略信号/批次与账户身份不一致: {sid}')
        source_files[path] = path.read_bytes()
    option = signal = None
    if joining and not fresh_option:
        option_path = option_path or sources.get('option_checkpoint')
        if not option_path:
            raise ValueError('首次合入08须提供option-checkpoint；不能以空归属开始')
        option_path = Path(option_path).resolve()
        signal_path = Path(signal_path or sources.get('option_signal') or str(option_path)+'.vega-signal.json').resolve()
        option, signal = JsonStateStore(option_path).load(), JsonStateStore(signal_path).load()
        if option is None or signal is None:
            raise ValueError('原08成交归属或信号检查点不存在')
        if Path(incoming['state_file']).exists():
            raise ValueError('首次合入08的worker信号路径已占用，保留文件并核查')
        quiescent(option.payload, next(iter(option.payload['ctp_drivers'])))
        try:
            validate_option_signal(option.payload, signal.payload, incoming['strategy_id'])
        except ValueError as error:
            raise ValueError(f'{error}；成交账本={option_path}；信号文件={signal_path}') from error
        source_files.update({option_path:option_path.read_bytes(), signal_path:signal_path.read_bytes()})
    if destination in source_files or destination in {Path(row['state_file']) for row in rows.values()}:
        raise ValueError('目标账户不能覆盖任何来源或策略信号文件')
    return combined, original, base, option, signal, source_files


def main(argv=None):
    import argparse
    from datetime import datetime
    def trading_day(value):
        if datetime.strptime(value,'%Y%m%d').strftime('%Y%m%d') != value:
            raise ValueError('交易日须为YYYYMMDD')
        return value
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account-config', type=Path, required=True)
    parser.add_argument('--source-config', type=Path, required=True, help='旧组合声明，仅迁移入口读取')
    parser.add_argument('--trading-day', type=trading_day, required=True)
    parser.add_argument('--option-checkpoint', type=Path)
    parser.add_argument('--option-signal', type=Path)
    parser.add_argument('--fresh-option', action='store_true', help='08作为全新策略登记；不读取旧08账本和信号')
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if args.connect != args.apply:
        parser.error('提交迁移须同时connect和apply；默认只检查文件')
    account = read_account_config(args.account_config)
    account['trading_day'] = args.trading_day
    data = read_sources(account,args.source_config,args.option_checkpoint,args.option_signal,
        fresh_option=args.fresh_option)
    if not args.apply:
        print(json.dumps(dict(status='files_checked',orders_enabled=False,
            account_state_file=account['account_state_file'],source_files=len(data[-1])),ensure_ascii=False))
        return
    from .run import build_host
    from .run_workers import load_calendars
    from .config import load_environment
    from .register_worker import registration_payload
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    with ExitStack() as locks, TemporaryDirectory() as directory:
        for path in sorted({*data[-1], Path(account['account_state_file']),
                Path(account['account_state_file']+'.continuous-owner')},key=str):
            locks.enter_context(strategy_state_lock(path))
        # 锁内重读，不能使用用户检查期间可能已变化的代次。
        combined, original, base, option, signal, source_files = read_sources(
            account,args.source_config,args.option_checkpoint,args.option_signal,fresh_option=args.fresh_option)
        source_account_path = Path(combined['account_state_file'])
        source_account_raw = source_files[source_account_path]
        temporary = Path(directory)/'source-account.json'
        temporary.write_bytes(source_account_raw)
        temporary.chmod(0o600)
        combined.update(trading_day=args.trading_day,account_state_file=str(temporary))
        host = None
        try:
            load_environment(account['account_env_file'])
            host = build_host(combined,resume=True,read_only=True,strategy_processes=True,
                session_order_budget=account['account']['max_session_orders'])
            # 08的references.calendar只含估值日期；独立读取同一配置的完整交易时段，
            # 保存到账户登记后，日常纯账户恢复不再依赖策略配置或数据库规则。
            option_config = {**combined, 'strategies':[
                row for row in combined['strategies'] if row['kind'] == 'option_vega']}
            for sid, calendar in load_calendars(option_config).items():
                host.strategies[sid].session.session_calendar = calendar
            for row in combined['strategies']:
                locks.enter_context(strategy_state_lock(row['state_file']+'.worker-owner'))
            transform = None
            if option is not None or args.fresh_option:
                transform = lambda payload:registration_payload(host.account,payload,
                    option.payload if option else None,signal.payload if signal else None,
                    original,strategy_limits_only=combined['account']['max_gross_quantity'] is None,
                    fresh_option=args.fresh_option)
            host.account.start(resume=True,commit_restored=False,payload_transform=transform)
            host.account.manager.validate_components()
            registry = registry_from_host(host)
            host.account.manager.commit_restore()  # 只提交临时副本，原账户从不改写。
            host.account.stop()  # 最终柜台核验成功后才生成新公共账户。
            payload = isolate_payload(JsonStateStore(temporary).load().payload,account,registry)
            if any(path.read_bytes() != raw for path,raw in source_files.items()):
                raise ValueError('来源文件在迁移期间变化，未提交目标账户')
            destination = Path(account['account_state_file'])
            destination.parent.mkdir(parents=True,exist_ok=True)
            backup = destination.with_name(f'source-account-g{base.generation}.json')
            raw = source_account_raw
            if backup.exists():
                if backup.read_bytes() != raw:
                    raise ValueError('账户来源备份冲突')
            else:
                with backup.open('xb') as output:
                    os.fchmod(output.fileno(),0o600)
                    output.write(raw)
                    output.flush(); os.fsync(output.fileno())
            saved = JsonStateStore(destination).save(payload,expected_generation=0)
            print(json.dumps(dict(status='migrated',orders_enabled=False,broker_verified=True,
                account_state_file=str(destination),source_generation=base.generation,
                source_sha256=hashlib.sha256(raw).hexdigest(),generation=saved.generation),ensure_ascii=False))
        finally:
            if host is not None:
                try:
                    if host.account._started:
                        if host.account.manager._restore_pending:
                            host.account.abort_preparation(); host.account.driver.stop()
                        else:
                            host.account.stop()
                finally:
                    host.resources.close()


if __name__ == '__main__':
    main()
