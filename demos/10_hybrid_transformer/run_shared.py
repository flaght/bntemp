"""Hybrid原生多实例入口：本进程运行RB/P，连接独立账户服务。"""
from datetime import timedelta
import os
from pathlib import Path
import sys
from types import SimpleNamespace

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = 'demos.10_hybrid_transformer'

SESSION_STOP_SECONDS = 30
PREPARE_SECONDS = 300
PATH_ARGUMENTS = {'--history-file','--history-config','--release-path','--directions-file',
    '--signal-config','--market-config','--report-dir'}


def shared_arguments(row, config, *, resume):
    from . import run_live, shared
    argv = list(row['argv'])+['--connect','--mode','simnow','--enable-orders','--confirm-simnow',
        '--state-file',row['state_file'],'--expected-trading-day',config['trading_day'],
        '--expected-source-day',shared.previous_source_day(config['trading_day']),
        '--seconds',str(config['seconds'])]
    if resume and Path(row['state_file']).is_file():
        argv.append('--resume')
    return run_live, run_live.parse_args(argv,shared_execution=True)


def build_shared_runtime(config, row, credentials, *, resume, activate_at=None, stop_at=None):
    from . import shared
    from bomber.framework.market.sources import source_config
    from bomber.framework.trader.runtime.live.channels.ctp import required
    from bomber.framework.trader.runtime.live.remote import RemoteStrategyRuntime, validate_forward_identity
    from bomber.framework.trader.runtime.reports import LiveRunReport
    entry, args = shared_arguments(row,config,resume=resume)
    identity = dict(strategy_id=row['strategy_id'],kind=row['kind'],argv=row['argv'],
        trading_day=config['trading_day'],environment=config['environment'])
    def prepare(service, driver, context):
        lifecycle = SimpleNamespace(transport=SimpleNamespace(investor_id=required('CTP_ACCOUNT_ID'),
            password=required('CTP_PASSWORD'),
            production_mode=os.getenv('CTP_PRODUCTION_MODE','true').lower() in {'1','true','yes','on'}),
            md_front=required('CTP_MD_ADDRESS') if source_config(args).source == 'ctp' else None)
        inputs = entry.prepare_inputs(context,lifecycle,product=args.product,
            expected_source_day=args.expected_source_day,market_config=args.market_source_config,
            calendar_id=args.calendar_id,no_session_overrides=args.no_session_overrides)
        if source_config(args).source == 'ctp':
            from dataclasses import replace
            inputs.upstream.config = replace(inputs.upstream.config,
                flow_path=f"/tmp/{config['account']['client_id']}-{row['strategy_id']}-worker-md")
        service.instrument_specs = {str(inputs.references.instrument_id):dict(
            tick=str(inputs.references.spec.tick),multiplier=str(inputs.references.spec.multiplier))}
        refresh = getattr(inputs.references,'refresh',None)
        if callable(refresh):
            refresh()
        session = entry.assemble(inputs,SimpleNamespace(driver=driver),product=args.product,
            release_path=args.release_path,directions_file=args.directions_file,signal_config=args.signal_config,
            market_config=args.market_source_config,mode='simnow',max_contracts=args.max_contracts,
            entry_contracts=args.entry_contracts,state_file=args.state_file,resume=args.resume,
            history_minutes=args.history_minutes,history_config=args.history_config,history_file=args.history_file,
            shared_service=service,strategy_id=row['strategy_id'])
        shared.configure_report(session,args.report_dir)
        session.validate_rollover_identity = validate_forward_identity
        current = {**identity,'routes':session.client.configuration(),
            'reference':{'reference':session.session_identity,'market_config':session.market_source_identity}}
        return session,current
    ready = 30 if source_config(args).source == 'ctp' else max(30,source_config(args).max_age_seconds)
    return RemoteStrategyRuntime(row['strategy_id'],Path(row['state_file']),Path(credentials),config['trading_day'],
        identity,prepare,validate_forward_identity,
        LiveRunReport(args.report_dir,prefix=row['strategy_id'],metadata=identity),
        resume=resume,activate_at=activate_at,stop_at=stop_at,seconds=config['seconds'],ready_seconds=ready,
        restore_fills_received=False)


def load_calendar(config):
    from bomber.framework.dataprep.session_inputs import SessionInputs, load_session_inputs
    from bomber.framework.dataprep.sources import DataSourcePurpose
    calendars = []
    for row in config['strategies']:
        _, declaration = shared_arguments(row, config, resume=Path(row['state_file']).is_file())
        calendar = load_session_inputs(SessionInputs(product=declaration.product,
            calendar_id=declaration.calendar_id,
            database=os.getenv('DDB_TRADING_HOURS_DATABASE') or 'dfs://bomber_static',
            hours_table=os.getenv('DDB_TRADING_HOURS_TABLE') or 'fut_trading_session',
            overrides_table=os.getenv('DDB_SESSION_OVERRIDES_TABLE') or None,
            no_overrides=declaration.no_session_overrides), purpose=DataSourcePurpose.LIVE_REFERENCE)
        calendars.append(calendar)
    first = calendars[0]
    # 首版明确要求同一调度时段；不能用RB时段代替其他品种不同的收盘时间。
    for calendar in calendars[1:]:
        if (calendar.calendar.manifest != first.calendar.manifest or calendar.zone != first.zone
                or calendar.segments != first.segments or calendar.overrides != first.overrides):
            raise ValueError('共享Hybrid的日历/时段不同，须按不同窗口分别调度')
    return first


def main(argv=None):
    from bomber.framework.cli import ConfigArgumentParser
    from bomber.framework.account_config import read_account_config
    parser = ConfigArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True,help='只包含Hybrid实例的JSON')
    parser.add_argument('--execution-channel',choices=('shared',),default='shared')
    parser.add_argument('--account-config',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--runtime-dir',type=Path)
    parser.add_argument('--strategy-id',action='append')
    parser.add_argument('--prepare-seconds',type=int,default=PREPARE_SECONDS)
    parser.add_argument('--run-forever',action='store_true')
    for flag in ('connect','enable-orders','confirm-simnow'):
        parser.add_argument('--'+flag,action='store_true')
    options = parser.parse_args(argv)
    if options.prepare_seconds < 0:
        parser.error('prepare-seconds须为非负整数')
    from bomber.framework.trader.runtime.live.remote import read_strategy_rows
    account = read_account_config(options.account_config)
    rows = read_strategy_rows(options.config,path_arguments=PATH_ARGUMENTS)
    if any(row['kind'] != 'hybrid_transformer' for row in rows):
        parser.error('此入口只运行Hybrid实例')
    return run_remote({**account,'strategies':rows},options,parser)


def run_remote(local_config, options, parser):
    """RB/P在本进程内各运行一个Runner，分别持有自己的远程账户客户端。"""
    from contextlib import ExitStack
    from dataclasses import replace
    from bomber.framework.cli import load_environment
    from bomber.framework.trader.runtime.live.remote import (
        load_account_connection, window_credentials, resume_available)
    from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, StopSignal, StopRequested
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    from bomber.framework.trader.runtime.live.remote import run_remote_group
    ids = set(options.strategy_id or [row['strategy_id'] for row in local_config['strategies']])
    rows = [row for row in local_config['strategies'] if row['strategy_id'] in ids]
    if not ids or ids != {row['strategy_id'] for row in rows}:
        parser.error('strategy-id须为本地配置中的Hybrid实例')
    config = load_account_connection(options.account_config, rows)
    if not options.connect:
        if options.enable_orders or options.confirm_simnow:
            parser.error('离线校验不接受报单授权')
        print(f'Hybrid原生入口配置通过；{len(rows)}个策略实例，未连接')
        return
    if not (options.enable_orders and options.confirm_simnow):
        parser.error('连接须显式enable-orders和confirm-simnow')
    load_environment(options.env_file)
    calendar = load_calendar({**config, 'strategies': rows})
    with ExitStack() as resources, StopSignal() as stop:
        for row in rows:
            resources.enter_context(strategy_state_lock(row['state_file']+'.worker-owner'))
        def window_runtime(window, event):
            def run():
                try:
                    own_window = replace(window, end=window.end-timedelta(seconds=SESSION_STOP_SECONDS))
                    credentials = window_credentials(config, rows, own_window, event, options.runtime_dir)
                    current = {**config, 'trading_day': window.trading_day, 'seconds': 86400}
                    runtimes = [build_shared_runtime(current, row, credentials[row['strategy_id']],
                        resume=resume_available(row, credentials[row['strategy_id']]),
                        activate_at=window.start, stop_at=own_window.end) for row in rows]
                    run_remote_group(runtimes, event)
                    return {'status': 'completed'}
                except StopRequested:
                    return {'status': 'completed'}
            return SimpleNamespace(run=run)
        print(f'Hybrid原生进程：{len(rows)}个独立实例，通过IPC连接独立账户服务', flush=True)
        return ContinuousPositionRuntime(calendar, window_runtime, stop_event=stop,
            run_forever=options.run_forever, prepare_seconds=options.prepare_seconds).run()


if __name__ == '__main__':
    main()
