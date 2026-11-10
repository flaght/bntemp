"""原生策略入口的配置适配；生命周期在框架运行时，策略进程由自身入口拥有。"""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

from .config import read_config, strategy_report_dir


def load_native_config(path, rows, env_file):
    from .account_config import read_account_config
    from .config import resolve_config
    declaration = json.loads(Path(path).read_text())
    if 'strategy_configs' not in declaration and 'base_config' not in declaration:
        account = read_account_config(path)
        config = resolve_config({**account,'strategies':rows,
            'strategy_env_files':{row['strategy_id']:str(Path(env_file).expanduser().resolve()) for row in rows}}, path)
        config['_account_only'] = True
        return config
    from .run_workers import load_config
    config, _ = load_config(path)
    registered = {row['strategy_id']: row for row in config['strategies']}
    if not rows or len({row['strategy_id'] for row in rows}) != len(rows):
        raise ValueError('须指定具有不同ID的本地策略实例')
    for row in rows:
        if row != registered.get(row['strategy_id']):
            raise ValueError(f"本地策略参数、ID或信号路径与账户注册声明不一致: {row['strategy_id']}")
        declared = config.get('strategy_env_files', {}).get(row['strategy_id'])
        if declared and str(Path(env_file).expanduser().resolve()) != declared:
            raise ValueError(f"本地策略env与账户注册声明不一致: {row['strategy_id']}")
    return config


def window_credentials(config, rows, window, stop_event, runtime_dir=None):
    from bomber.framework.trader.runtime.live.remote import wait_for_account
    pure = config.get('_account_only', False)
    discovery = (runtime_dir or Path(config['account_state_file']).parent/('service' if pure else 'workers'))/'current.json'
    def validate(candidate):
        if pure:
            from .account_config import read_account_config, FIELDS
            published = read_account_config(candidate['config'])
            expected = {key:deepcopy(config[key]) for key in FIELDS}
        else:
            published = read_config(candidate['config'])
            expected = deepcopy(config)
        expected.update(trading_day=window.trading_day, seconds=86400)
        if published != expected:
            raise ValueError('账户服务声明与本地已核对配置不一致；保留原状态并停止')
        for row in rows:
            path = Path(candidate['credentials_dir'])/(row['strategy_id']+'.json')
            if not path.is_file():
                raise ValueError(f"该策略ID尚未登记到账户: {row['strategy_id']}")
            credentials = json.loads(path.read_text())
            if (credentials['strategy_id'] != row['strategy_id']
                    or Path(credentials['socket']) != Path(candidate['socket'])):
                raise ValueError('账户客户端凭据的策略ID或socket不一致')
    candidate = wait_for_account(discovery, window, stop_event, validate)
    return {row['strategy_id']: Path(candidate['credentials_dir'])/(row['strategy_id']+'.json') for row in rows}


def make_runtime(config, row, credentials, *, resume, activate_at=None, stop_at=None):
    """注入原策略装配函数，复用原worker身份及信号格式，不切换全局env。"""
    from .factories import (strategy_arguments, prepare_strategy_inputs,
        assemble_strategy_session, shared_strategy_identity, instrument_specs)
    from .rollover import validate_strategy_identity
    from bomber.framework.market.sources import source_config
    from bomber.framework.trader.runtime.live.channels.ctp import required
    from bomber.framework.trader.runtime.live.remote import RemoteStrategyRuntime
    from bomber.framework.trader.runtime.reports import LiveRunReport
    entry, args = strategy_arguments(row, config, resume=resume)
    if getattr(args, 'execution_channel', 'local') != 'local':
        raise ValueError('业务配置不能再选择shared启动；请选择原live_simnow业务参数配置')
    identity = dict(strategy_id=row['strategy_id'], kind=row['kind'], argv=row['argv'],
        trading_day=config['trading_day'], environment=config['environment'],
        replay_md_trading_day=config.get('replay_md_trading_day'), execution_mode='independent')
    metadata = dict(identity)
    for key in ('history_missing_policy', 'ctp_instrument_max_age_seconds'):
        if key in config:
            metadata[key] = config[key]
    if row['strategy_id'] in config.get('strategy_history_minutes', {}):
        metadata['effective_history_minutes'] = config['strategy_history_minutes'][row['strategy_id']]

    def prepare(service, driver, context):
        lifecycle = SimpleNamespace(md_front=required('CTP_MD_ADDRESS') if source_config(args).source == 'ctp' else None,
            transport=SimpleNamespace(investor_id=required('CTP_ACCOUNT_ID'), password=required('CTP_PASSWORD'),
                production_mode=os.getenv('CTP_PRODUCTION_MODE', 'true').lower() in {'1', 'true', 'yes', 'on'}))
        inputs = prepare_strategy_inputs(row, entry, args, context, lifecycle)
        if service.allow_reconfigure:
            service.instrument_specs = {str(i):dict(tick=str(spec.tick),multiplier=str(spec.multiplier))
                for i,spec in instrument_specs(row['kind'], inputs.references).items()}
        upstream = getattr(inputs, 'upstream', getattr(inputs, 'ctp_upstream', None))
        if getattr(upstream, 'config', None) is not None and source_config(args).source == 'ctp':
            upstream.config = replace(upstream.config,
                flow_path=f"/tmp/{config['account']['client_id']}-{row['strategy_id']}-worker-md")
        session = assemble_strategy_session(entry, args, inputs, service=service,
            driver=driver, strategy_id=row['strategy_id'])
        current_identity = identity
        if row['kind'] == 'hybrid_transformer':
            current_identity = shared_strategy_identity(row, config, session)
            from importlib import import_module
            import_module('demos.10_hybrid_transformer.shared').configure_report(session, strategy_report_dir(row, config))
        return session, current_identity

    ready = args.ready_timeout if row['kind'] == 'option_vega' else 30
    if source_config(args).source != 'ctp':
        ready = max(ready, source_config(args).max_age_seconds)
    return RemoteStrategyRuntime(strategy_id=row['strategy_id'], state_file=Path(row['state_file']),
        credentials=Path(credentials), trading_day=config['trading_day'], identity=identity,
        prepare=prepare, validate_identity=validate_strategy_identity,
        report=LiveRunReport(strategy_report_dir(row, config), prefix=row['strategy_id'], metadata=metadata),
        resume=resume, activate_at=activate_at, stop_at=stop_at,
        seconds=min(config['seconds'], row.get('stop_after_seconds', config['seconds'])), ready_seconds=ready,
        restore_fills_received=row['kind'] != 'hybrid_transformer', window_context=row['kind'] == 'option_vega')


def resume_available(row, credentials):
    return Path(row['state_file']).is_file() or json.loads(Path(credentials).read_text()).get('bootstrap_signal') is not None
