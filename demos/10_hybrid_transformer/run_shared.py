"""同一份Hybrid配置切换同进程/多进程；复用共享执行、IPC和交易日历。"""
from copy import deepcopy
from datetime import datetime, timedelta
import math
import os
from pathlib import Path
from types import SimpleNamespace

from scripts.integration.shared_account.config import read_config

# 正常分钟信号处理完后，给休盘前撤单和回报留出时间。
SESSION_STOP_SECONDS = 30
# 各交易时段默认提前5分钟连接、恢复和预热；可由启动参数覆盖。
PREPARE_SECONDS = 300


def validate_layout(config, layout):
    if config.get('environment') != 'realtime' or config.get('execution_mode') != 'independent':
        raise ValueError('Hybrid布局切换须固定realtime和independent执行政策')
    if layout not in {'same-process', 'separate-processes'}:
        raise ValueError('进程布局无效')
    if any(row['kind'] != 'hybrid_transformer' for row in config['strategies']):
        raise ValueError('此入口管理Hybrid策略；其他策略沿用公共共享入口')
    for row in config['strategies']:
        if 'stop_after_seconds' in row or any(arg.split('=', 1)[0] in {
                '--expected-source-day', '--confirm-manual-flat'} for arg in row['argv']):
            raise ValueError('共享Hybrid日期由日历确定，不能覆盖来源日或清空旧仓')


def load_calendar(config):
    from bomber.framework.dataprep.session_inputs import SessionInputs, load_session_inputs
    from bomber.framework.dataprep.sources import DataSourcePurpose
    from scripts.integration.shared_account.factories import strategy_arguments
    calendars = []
    for row in config['strategies']:
        _, declaration = strategy_arguments(row, config, resume=Path(row['state_file']).is_file())
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


def run_same_process(config, window, stop, *, resume, remaining, clock=None):
    from scripts.integration.shared_account.run import build_host
    from bomber.framework.trader.execution.ctp.query_errors import CtpQueryInterrupted
    from bomber.framework.trader.runtime.live.continuous import StopRequested, wait_for_open
    from bomber.framework.trader.runtime.reports import TradingDayLiveReport
    clock = clock or (lambda: datetime.now(window.end.tzinfo))
    report = TradingDayLiveReport(config['report_dir'], prefix='hybrid-shared-account',
        trading_day=window.trading_day, metadata={'process_layout': 'same-process'})
    report.begin()
    host, failure, shutdown_failure, status = None, None, None, 'failed'
    stop_at = window.end - timedelta(seconds=SESSION_STOP_SECONDS)
    def stop_requested():
        return stop.is_set() or clock() >= stop_at
    def check():
        if stop_requested():
            raise StopRequested()
    try:
        check()
        host = build_host(config, resume=resume, session_order_budget=remaining)
        host.preparation_check = check
        host.activation_wait = lambda: wait_for_open(window.start, stop,
            poll=host.poll_preparation, clock=clock)
        host.account.transport.query_stop_requested = stop_requested
        for item in host.strategies.values():
            item.session.runner.continuous_window = window
            item.session.runner.continuous_stop_event = stop
        host.start()
        print(f'Hybrid休盘前停机: stop_at={stop_at.isoformat()} session_end={window.end.isoformat()}；'
              '撤单并保留持仓及批次', flush=True)
        while not stop.is_set() and clock() < stop_at:
            host.poll()
            if not any(item.active for item in host.strategies.values()):
                reasons = '；'.join(f'{sid}: {item.failure or "未提供停止原因"}'
                    for sid, item in host.strategies.items())
                raise RuntimeError('所有Hybrid策略已停止；' + reasons)
            stop.wait(0.2)
        status = 'completed'
    except (StopRequested, CtpQueryInterrupted):
        status = 'completed'
    except BaseException as error:
        failure = str(error)
        raise
    finally:
        try:
            if host is not None:
                host.stop()
        except BaseException as error:
            status = 'failed'
            shutdown_failure = str(error)
            if failure is not None:
                raise RuntimeError(f'{failure}；停机未完成: {shutdown_failure}') from error
            failure = shutdown_failure
            raise
        finally:
            report.write(status=status, fields={'failure': failure, 'shutdown_failure': shutdown_failure,
                **(host.snapshot() if host else {})}, session=host,
                events=host.account.execution.backend.reports if host else ())
    return {'status': status}


def run_window(config, layout, calendar, window, stop):
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.runtime.live.continuous import orders_for_day
    from scripts.integration.shared_account.continuous import ProcessWindowRuntime
    if any(row['kind'] != 'hybrid_transformer' for row in config['strategies']):
        raise ValueError('此入口只组装Hybrid策略')
    current = deepcopy(config)
    current['trading_day'] = window.trading_day
    current['seconds'] = min(86400, max(1, math.ceil((window.end-datetime.now(window.end.tzinfo)).total_seconds())+60))
    saved = JsonStateStore(current['account_state_file']).load()
    if saved and not {'shared_account', 'hybrid'} & saved.payload.get('component_states', {}).keys():
        raise ValueError('须使用共享账户检查点；原单策略RB文件不能直接当共享账本，原文件保持不变')
    used = orders_for_day(saved.payload, window.trading_day) if saved else 0
    remaining = current['account']['max_session_orders']-used
    if remaining <= 0:
        raise RuntimeError('本交易日报单额度已用完，不通过切换布局重置额度')
    if layout == 'same-process':
        return run_same_process(current, window, stop, resume=saved is not None, remaining=remaining)
    return ProcessWindowRuntime(current, window, stop, remaining, resume=saved is not None,
        stop_before_seconds=SESSION_STOP_SECONDS, prepare_before_open=True).run()


def main(argv=None):
    from bomber.framework.cli import ConfigArgumentParser
    parser = ConfigArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--execution-channel', choices=('local', 'shared'), default='local')
    parser.add_argument('--account-config', type=Path)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--strategy-id', action='append', help='可选，仅运行指定的Hybrid实例')
    parser.add_argument('--process-layout', choices=('same-process', 'separate-processes'), default='same-process')
    parser.add_argument('--run-forever', action='store_true')
    parser.add_argument('--prepare-seconds', type=int, default=PREPARE_SECONDS,
        help='开盘前开始准备的秒数，默认300；0表示开盘时开始准备')
    for flag in ('connect', 'enable-orders', 'confirm-simnow'):
        parser.add_argument('--'+flag, action='store_true')
    options = parser.parse_args(argv)
    if options.prepare_seconds < 0:
        parser.error('prepare-seconds须为非负整数')
    config = read_config(options.config)
    validate_layout(config, options.process_layout)
    if options.execution_channel == 'shared':
        if not options.account_config or not options.env_file:
            parser.error('shared须指定account-config和本策略env-file')
        if options.process_layout != 'same-process':
            parser.error('shared通道在本进程运行实例；分别启动可使用strategy-id选择实例')
        return run_remote(config, options, parser)
    if options.account_config or options.runtime_dir or options.strategy_id:
        parser.error('account-config/runtime-dir/strategy-id仅用于shared执行通道')
    if not options.connect:
        if options.enable_orders or options.confirm_simnow:
            parser.error('离线校验不接受报单授权')
        print('Hybrid共享配置通过；未连接、未报单')
        return
    if not (options.enable_orders and options.confirm_simnow):
        parser.error('连接须显式enable-orders和confirm-simnow')
    from scripts.integration.shared_account.config import load_environment
    load_environment(options.env_file)
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, StopSignal
    with strategy_state_lock(str(Path(config['account_state_file']).resolve())+'.continuous-owner'), StopSignal() as stop:
        calendar = load_calendar(config)
        print(f'Hybrid准备调度: prepare_seconds={options.prepare_seconds}；'
              '按交易日历提前准备，开盘且健康核验通过后才放行', flush=True)
        return ContinuousPositionRuntime(calendar,
            lambda window, event: SimpleNamespace(run=lambda: run_window(config, options.process_layout,
                calendar, window, event)), run_forever=options.run_forever, stop_event=stop,
            prepare_seconds=options.prepare_seconds).run()


def run_remote(local_config, options, parser):
    """RB/P在本进程内各运行一个Runner，分别持有自己的远程账户客户端。"""
    from contextlib import ExitStack
    from dataclasses import replace
    from scripts.integration.shared_account.config import load_environment
    from scripts.integration.shared_account.native import (
        load_native_config, window_credentials, make_runtime, resume_available)
    from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, StopSignal, StopRequested
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    from bomber.framework.trader.runtime.live.remote import run_remote_group
    ids = set(options.strategy_id or [row['strategy_id'] for row in local_config['strategies']])
    rows = [row for row in local_config['strategies'] if row['strategy_id'] in ids]
    if not ids or ids != {row['strategy_id'] for row in rows}:
        parser.error('strategy-id须为本地配置中的Hybrid实例')
    config = load_native_config(options.account_config, rows, options.env_file)
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
                    runtimes = [make_runtime(current, row, credentials[row['strategy_id']],
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
