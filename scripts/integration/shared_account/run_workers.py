"""复用一个账户服务，按各策略日历调度Hybrid和08独立worker。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

from .config import read_config, resolve_config, load_environment, strategy_environment


def load_config(path):
    """公共账户声明平等引用策略文件；原Hybrid配置不再作为运行时基类。"""
    path = Path(path).resolve()
    declaration = json.loads(path.read_text())
    if 'base_config' in declaration:
        return _load_legacy_config(path)  # 已部署的旧组合文件仍可读取。
    files = declaration.get('strategy_configs')
    if not isinstance(files, list) or not files or any(not isinstance(p, str) or not p.strip() for p in files):
        raise ValueError('请指定公共workers配置（须包含strategy_configs），不能把单策略声明当账户启动配置')
    config = {key: deepcopy(value) for key, value in declaration.items()
              if key not in {'strategy_configs', 'registration'}}
    for name in ('account_state_file', 'report_dir', 'account_env_file'):
        if config.get(name) is not None:
            config[name] = str((path.parent/Path(config[name]).expanduser()).resolve())
    strategies, environments, seen = [], {}, set()
    for filename in files:
        source = (path.parent/Path(filename).expanduser()).resolve()
        if source in seen:
            raise ValueError('strategy_configs不能重复引用同一文件')
        seen.add(source)
        content = json.loads(source.read_text())
        if 'version' in content:
            parsed = read_config(source)  # 旧完整配置只贡献策略行，不提供公共账户限额。
        else:
            if set(content)-{'strategies', 'strategies_file', 'env_file'} or (
                    ('strategies' in content) == ('strategies_file' in content)):
                raise ValueError(f'策略文件须声明strategies或strategies_file，以及可选env_file: {source}')
            if 'strategies_file' in content:
                reference = content['strategies_file']
                if not isinstance(reference, str) or not reference.strip():
                    raise ValueError('strategies_file须为非空文件路径')
                rows = read_config((source.parent/Path(reference).expanduser()).resolve())['strategies']
            else:
                rows = content['strategies']
            prototype = {**deepcopy(config), 'strategies': rows}
            prototype.pop('strategy_env_files', None)
            if content.get('env_file') is not None:
                prototype['env_file'] = content['env_file']
            parsed = resolve_config(prototype, source)
        if any(row['kind'] not in {'hybrid_transformer', 'option_vega'} for row in parsed['strategies']):
            raise ValueError('当前worker调度支持Hybrid和08')
        strategies.extend(parsed['strategies'])
        environments.update(parsed.get('strategy_env_files', {}))
    config['strategies'] = strategies
    if environments:
        config['strategy_env_files'] = environments
    config = resolve_config(config, path)
    if (config['environment'] != 'realtime' or config.get('execution_mode') != 'independent'
            or config.get('existing_position_policy') != 'preserve_external'):
        raise ValueError('worker账户须为realtime、independent和preserve_external')
    registration = declaration.get('registration', {})
    if not isinstance(registration, dict) or set(registration)-{'source_account_config', 'option_checkpoint', 'option_signal'}:
        raise ValueError('registration仅用于首次注册来源声明')
    def source_path(name):
        value = registration.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError('注册来源路径须为非空字符串或null')
        return str((path.parent/Path(value).expanduser()).resolve())
    sources = {'base_config': source_path('source_account_config'),
        'strategy_limits_only': config['account']['max_gross_quantity'] is None,
        'option_checkpoint': source_path('option_checkpoint'), 'option_signal': source_path('option_signal')}
    if sources['option_signal'] and not sources['option_checkpoint']:
        raise ValueError('option_signal须同时声明option_checkpoint')
    return config, sources


def _load_legacy_config(path):
    """仅组合启动声明；不改Hybrid配置、算法参数或信号文件。"""
    path = Path(path).resolve()
    declaration = json.loads(path.read_text())
    if set(declaration) - {'base_config', 'option', 'strategy_limits_only', 'option_checkpoint', 'option_signal', 'env_files'}:
        raise ValueError('worker启动声明存在未知字段')
    base_path = (path.parent / declaration['base_config']).resolve()
    config = read_config(base_path)
    if (config.get('execution_mode') != 'independent' or config['environment'] != 'realtime'
            or config.get('existing_position_policy') != 'preserve_external'
            or any(row['kind'] != 'hybrid_transformer' for row in config['strategies'])):
        raise ValueError('base_config须为原Hybrid实时独立归属配置')
    incoming_config = {**deepcopy(config), 'strategies': [declaration['option']]}
    incoming_config.pop('strategy_env_files', None)
    incoming = resolve_config(incoming_config, path)['strategies'][0]
    if incoming['kind'] != 'option_vega':
        raise ValueError('新增worker须为option_vega')
    config['strategies'].append(incoming)
    environment_files = declaration.get('env_files', {})
    if (not isinstance(environment_files, dict) or set(environment_files)-{'account', 'hybrid_transformer', 'option_vega'}
            or any(value is not None and (not isinstance(value, str) or not value.strip()) for value in environment_files.values())):
        raise ValueError('env_files只接受account、hybrid_transformer、option_vega到env路径的映射')
    account_environment = environment_files.get('account') or environment_files.get('hybrid_transformer')
    if account_environment:
        config['account_env_file'] = str((path.parent/Path(account_environment).expanduser()).resolve())
    for row in config['strategies']:
        if environment_files.get(row['kind']):
            config.setdefault('strategy_env_files', {})[row['strategy_id']] = str(
                (path.parent/Path(environment_files[row['kind']]).expanduser()).resolve())
    # Hybrid手数/参数原样保留；联合预算只能在显式选择后改变，报单额度原样继承。
    strategy_only = declaration.get('strategy_limits_only', False)
    if type(strategy_only) is not bool:
        raise ValueError('strategy_limits_only须为布尔值')
    if strategy_only:
        config['account'].update(max_gross_quantity=None, max_gross_notional=None)
    config = resolve_config(config, path)
    sources = {'base_config': str(base_path), 'strategy_limits_only': strategy_only}
    for name in ('option_checkpoint', 'option_signal'):
        value = declaration.get(name)
        sources[name] = str((path.parent / value).resolve()) if value else None
    if sources['option_signal'] and not sources['option_checkpoint']:
        raise ValueError('option_signal须同时声明option_checkpoint')
    if sources['option_checkpoint'] in {config['account_state_file'], incoming['state_file']}:
        raise ValueError('原08账户检查点不能覆盖账户服务或worker信号文件')
    return config, sources


def load_calendars(config):
    from .factories import strategy_arguments
    from bomber.framework.dataprep.session_inputs import SessionInputs, load_session_inputs
    result = {}
    for row in config['strategies']:
        with strategy_environment(config, row['strategy_id']):
            entry, args = strategy_arguments(row, config, resume=Path(row['state_file']).is_file())
            if row['kind'] == 'option_vega':
                calendar = load_session_inputs(entry.session_inputs(args))
                entry.validate_calendar(calendar, args.option_product)
            else:
                calendar = load_session_inputs(SessionInputs(product=args.product, calendar_id=args.calendar_id,
                    database=os.getenv('DDB_TRADING_HOURS_DATABASE') or 'dfs://bomber_static',
                    hours_table=os.getenv('DDB_TRADING_HOURS_TABLE') or 'fut_trading_session',
                    overrides_table=os.getenv('DDB_SESSION_OVERRIDES_TABLE') or None,
                    no_overrides=args.no_session_overrides))
        result[row['strategy_id']] = calendar
    return result


class WorkerCalendars:
    """账户服务覆盖日历窗口的并集；并集只控制连接，不替代任一策略日历。"""
    def __init__(self, calendars):
        self.calendars = dict(calendars)
        self.zone = next(iter(calendars.values())).zone
        if any(c.zone != self.zone for c in calendars.values()):
            raise ValueError('worker日历须使用相同时区')

    def is_trading_day(self, day):
        return any([calendar.is_trading_day(day) for calendar in self.calendars.values()])

    def windows(self, day):
        from bomber.framework.calendars.sessions import TradingWindow
        windows = sorted((w for calendar in self.calendars.values() for w in calendar.windows(day)), key=lambda w: w.start)
        merged = []
        for window in windows:
            if merged and window.start <= merged[-1].end:
                previous = merged[-1]
                if window.trading_day != previous.trading_day:
                    raise ValueError('重叠worker窗口的交易日不同')
                merged[-1] = replace(previous, end=max(previous.end, window.end))
            else:
                merged.append(TradingWindow(window.trading_day, window.start, window.end, f'workers_{len(merged)}'))
        return tuple(merged)

    def window(self, now):
        active = [calendar.window(now) for calendar in self.calendars.values()]
        days = {w.trading_day for w in active if w is not None}
        if len(days) > 1:
            raise ValueError('活动worker日历的交易日不同')
        return next((w for day in days for w in self.windows(day) if w.start <= now < w.end), None)

    def next_window(self, now, *, before):
        candidates = [calendar.next_window(now, before=before) for calendar in self.calendars.values()]
        days = {w.trading_day for w in candidates if w is not None}
        return min((w for day in days for w in self.windows(day) if now < w.start <= before),
                   key=lambda w: w.start, default=None)

    def worker_windows(self, config, window):
        result = {}
        for row in config['strategies']:
            windows = self.calendars[row['strategy_id']].windows(window.trading_day)
            # Hybrid沿用原休盘前30秒停机；08在MO/IO/HO自己的连续交易终点停机。
            lead = 30 if row['kind'] == 'hybrid_transformer' else 0
            result[row['strategy_id']] = tuple(replace(w, end=w.end-timedelta(seconds=lead)) for w in windows
                if w.start < window.end and w.end > window.start)
        return result


def run_remote_window(config, window, event, worker_windows, discovery_file, prepare_seconds):
    """独立终端只启动worker；等待账户服务发布本窗口，不建立TD连接。"""
    from .continuous import ProcessWindowRuntime
    import time
    clock = lambda: datetime.now(window.end.tzinfo)
    deadline = time.monotonic()+180+max(0, (window.start-clock()).total_seconds())
    discovery = None
    while not event.is_set() and clock() < window.end:
        if discovery_file.exists():
            try:
                candidate = json.loads(discovery_file.read_text())
                if (candidate['trading_day'] == window.trading_day and Path(candidate['socket']).exists()
                        and datetime.fromisoformat(candidate['start']) <= window.start
                        and datetime.fromisoformat(candidate['end']) >= window.end):
                    published = read_config(candidate['config'])
                    expected = deepcopy(config)
                    expected.update(trading_day=window.trading_day, seconds=86400)
                    if published != expected:
                        raise ValueError('worker启动声明与账户服务不一致；不能连接其他服务或更改策略参数')
                    discovery = candidate
                    break
            except FileNotFoundError:
                pass  # 正常窗口切换中的原子替换，等待下一份能力文件。
        if time.monotonic() >= deadline:
            raise RuntimeError('未找到本窗口账户服务；先启动run_workers --role account')
        event.wait(0.2)
    if discovery is None:
        return {'status': 'completed'}
    runtime = ProcessWindowRuntime(published, window, event, 1,
        worker_windows=worker_windows, prepare_seconds=prepare_seconds)
    def server_status():
        try:
            return None if (Path(discovery['socket']).exists() and
                json.loads(discovery_file.read_text()).get('config') == discovery['config']) else 1
        except FileNotFoundError:
            return 1
    runtime.server = SimpleNamespace(poll=server_status)
    try:
        runtime.run_scheduled_workers(Path(discovery['config']), Path(discovery['credentials_dir']))
    finally:
        for process in runtime.workers:
            runtime.stop_process(process)
    return {'status': 'completed'}


def run_account_window(config, window, stop_event, remaining, discovery_file):
    """账户角色在当前进程运行TD服务；策略由各自原生入口运行。"""
    from .server import _run
    import uuid
    directory = Path(config['report_dir'])/('account-window-'+uuid.uuid4().hex)
    directory.mkdir(parents=True, mode=0o700)
    config_file = directory/'config.json'
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2)+'\n')
    args = SimpleNamespace(socket=Path('/tmp')/('bomber-shared-'+uuid.uuid4().hex+'.sock'),
        credentials_dir=directory/'credentials', resume=True, remaining_orders=remaining,
        activate_at=window.start, stop_at=window.end, env_file=config.get('account_env_file'),
        discovery_file=discovery_file, discovery_config=config_file, discovery_window=window)
    try:
        _run(args, config, stop_event)
        return {'status': 'completed'}
    finally:
        if discovery_file.exists() and json.loads(discovery_file.read_text()).get('config') == str(config_file):
            discovery_file.unlink()


def main(argv=None):
    from bomber.framework.cli import ConfigArgumentParser
    parser = ConfigArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True, help='引用原Hybrid配置与08声明的JSON')
    parser.add_argument('--prepare-seconds', type=int, default=600)
    parser.add_argument('--run-forever', action='store_true')
    parser.add_argument('--role', choices=('all', 'account', 'worker'), default='all',
        help='all统一启动；account仅运行TD服务；worker在独立终端接入现有服务')
    parser.add_argument('--strategy-id', action='append', help='worker角色选择策略ID，可重复')
    parser.add_argument('--kind', choices=('option_vega', 'hybrid_transformer'), help='worker角色选择策略类型')
    parser.add_argument('--runtime-dir', type=Path, help='同机各终端共用发现目录；默认账户检查点旁workers目录')
    for flag in ('connect', 'enable-orders', 'confirm-simnow'):
        parser.add_argument('--'+flag, action='store_true')
    args = parser.parse_args(argv)
    config, _ = load_config(args.config)
    if args.prepare_seconds < 0:
        parser.error('prepare-seconds须为非负整数')
    ids = set(args.strategy_id or [r['strategy_id'] for r in config['strategies']
                                 if args.kind is None or r['kind'] == args.kind])
    if (not ids or not ids <= {r['strategy_id'] for r in config['strategies']}
            or args.role != 'worker' and (args.kind or args.strategy_id)):
        parser.error('策略选择仅用于worker角色，且须使用配置中的策略ID')
    if not args.connect:
        if args.enable_orders or args.confirm_simnow:
            parser.error('离线校验不接受报单授权')
        print('worker配置通过；Hybrid参数未改，未连接、未报单')
        return
    if not (args.enable_orders and args.confirm_simnow):
        parser.error('连接须显式enable-orders和confirm-simnow')
    from .continuous import ProcessWindowRuntime
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, StopSignal, orders_for_day
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    selected_env = None
    if args.role == 'worker':
        files = {config.get('strategy_env_files', {}).get(sid) for sid in ids}
        selected_env = next(iter(files)) if len(files) == 1 else None
    load_environment(selected_env or config.get('account_env_file'))
    with ExitStack() as stack, StopSignal() as stop:
        if args.role == 'worker':
            for row in config['strategies']:
                if row['strategy_id'] in ids:
                    stack.enter_context(strategy_state_lock(row['state_file']+'.worker-owner'))
        else:
            stack.enter_context(strategy_state_lock(config['account_state_file']+'.continuous-owner'))
        saved = JsonStateStore(config['account_state_file']).load()
        expected = {row['strategy_id'] for row in config['strategies']}
        if saved is None or set(saved.payload['component_states']['session_identity']['identity']['strategies']) != expected:
            parser.error('首次添加08须先执行register_worker只读柜台核验；不自动新建或替换原账本')
        selected = (config if args.role != 'worker' else {**config, 'strategies':
            [row for row in config['strategies'] if row['strategy_id'] in ids]})
        calendars = WorkerCalendars(load_calendars(selected))
        discovery = (args.runtime_dir or Path(config['account_state_file']).parent/'workers')/'current.json'
        def run_window(window, event):
            def run():
                current = deepcopy(config)
                current.update(trading_day=window.trading_day, seconds=86400)
                if args.role == 'worker':
                    plan = calendars.worker_windows({'strategies': [r for r in current['strategies']
                        if r['strategy_id'] in ids]}, window)
                    return run_remote_window(current, window, event, plan, discovery, args.prepare_seconds)
                saved = JsonStateStore(current['account_state_file']).load()
                remaining = current['account']['max_session_orders'] - orders_for_day(saved.payload, window.trading_day)
                if remaining <= 0:
                    raise RuntimeError('共享TD当日报单额度已用完，不在切换worker时重置')
                # 留出独立终端在最后一个策略窗口结束后的撤单/游标确认时间。
                service_window = replace(window, end=window.end+timedelta(seconds=30))
                if args.role == 'account':
                    return run_account_window(current, service_window, event, remaining, discovery)
                return ProcessWindowRuntime(current, service_window, event, remaining, prepare_before_open=True,
                    worker_windows=calendars.worker_windows(current, window),
                    prepare_seconds=args.prepare_seconds, discovery_file=discovery).run()
            return SimpleNamespace(run=run)
        print('独立worker调度：各策略使用自己的日历；原归属、批次及共享TD报单计数继续恢复', flush=True)
        return ContinuousPositionRuntime(calendars, run_window, run_forever=args.run_forever,
            stop_event=stop, prepare_seconds=args.prepare_seconds).run()


if __name__ == '__main__':
    main()
