"""独立账户入口：只读取账户JSON和账户账本，策略由各自入口运行。"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = 'demos.shared_account'

from bomber.framework.account_config import read_account_config


class AccountCalendars:
    """已登记市场的连接窗口并集，不决定策略的启动与停止。"""
    def __init__(self, calendars):
        self.calendars = tuple(calendars)
        if not self.calendars:
            raise ValueError('账户执行登记缺少市场时段')
        self.zone = self.calendars[0].zone
        if any(c.zone != self.zone for c in self.calendars):
            raise ValueError('账户市场时区不一致')

    def is_trading_day(self, day):
        return any([calendar.is_trading_day(day) for calendar in self.calendars])

    def windows(self, day):
        from bomber.framework.calendars.sessions import TradingWindow
        windows = sorted((w for c in self.calendars for w in c.windows(day)), key=lambda w:w.start)
        merged = []
        for window in windows:
            if merged and window.start <= merged[-1].end:
                if window.trading_day != merged[-1].trading_day:
                    raise ValueError('账户市场窗口的交易日不一致')
                merged[-1] = replace(merged[-1], end=max(window.end, merged[-1].end))
            else:
                merged.append(TradingWindow(window.trading_day,window.start,window.end,f'account_{len(merged)}'))
        return tuple(merged)

    def window(self, now):
        days = {w.trading_day for c in self.calendars if (w:=c.window(now)) is not None}
        if len(days) > 1:
            raise ValueError('账户活动窗口交易日不一致')
        return next((w for day in days for w in self.windows(day) if w.start <= now < w.end), None)

    def next_window(self, now, *, before):
        days = {w.trading_day for c in self.calendars if (w:=c.next_window(now,before=before)) is not None}
        return min((w for day in days for w in self.windows(day) if now < w.start <= before),
            key=lambda w:w.start, default=None)


def load_account_calendar(registry):
    from bomber.framework.calendars.state import calendar_from_state
    return AccountCalendars(calendar_from_state(row) for row in registry['sessions'])


def run_window(config, window, event, remaining, discovery):
    from bomber.framework.trader.runtime.live.account import serve_account
    directory = Path(config['report_dir'])/('window-'+uuid.uuid4().hex)
    directory.mkdir(parents=True, mode=0o700)
    path = directory/'account.json'
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2)+'\n')
    args = SimpleNamespace(socket=Path('/tmp')/('bomber-account-'+uuid.uuid4().hex+'.sock'),
        credentials_dir=directory/'credentials', resume=True, remaining_orders=remaining,
        activate_at=window.start, stop_at=window.end, env_file=config['account_env_file'],
        discovery_file=discovery, discovery_config=path, discovery_window=window)
    try:
        serve_account(args, config, event)
        return {'status':'completed'}
    finally:
        if discovery.exists() and json.loads(discovery.read_text()).get('config') == str(path):
            discovery.unlink()


def main(argv=None):
    from bomber.framework.cli import ConfigArgumentParser
    parser = ConfigArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--prepare-seconds', type=int, default=600)
    parser.add_argument('--run-forever', action='store_true')
    parser.add_argument('--runtime-dir', type=Path)
    for flag in ('connect','enable-orders','confirm-simnow'):
        parser.add_argument('--'+flag, action='store_true')
    args = parser.parse_args(argv)
    config = read_account_config(args.config)
    if args.prepare_seconds < 0:
        parser.error('prepare-seconds须为非负整数')
    if not args.connect:
        if args.enable_orders or args.confirm_simnow:
            parser.error('离线校验不接受报单授权')
        print('纯账户JSON通过；未读取任何策略配置、未连接')
        return
    if not (args.enable_orders and args.confirm_simnow):
        parser.error('连接须显式enable-orders和confirm-simnow')
    from bomber.framework.cli import load_environment
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.execution.registry import validate_registry
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, StopSignal, orders_for_day
    load_environment(config['account_env_file'])
    with ExitStack() as resources, StopSignal() as stop:
        resources.enter_context(strategy_state_lock(config['account_state_file']+'.continuous-owner'))
        saved = JsonStateStore(config['account_state_file']).load()
        if saved is None:
            parser.error(
                f"公共账户账本不存在: {config['account_state_file']}\n"
                'account_state_file的相对路径以账户JSON所在目录为基准。'
                '已有纯账户账本请填写其实际路径；只有旧Hybrid/08账本时须先一次性迁移，'
                '见demos/shared_account/README.md。不自动创建空账本。')
        registry = validate_registry(saved.payload['component_states'].get('execution_registry'))
        calendar = load_account_calendar(registry)
        discovery = (args.runtime_dir or Path(config['account_state_file']).parent/'service')/'current.json'
        def factory(window, event):
            def run():
                current = deepcopy(config)
                current.update(trading_day=window.trading_day, seconds=86400)
                saved = JsonStateStore(current['account_state_file']).load()
                remaining = current['account']['max_session_orders']-orders_for_day(saved.payload, window.trading_day)
                if remaining <= 0:
                    return {'status':'skipped','reason':'daily_order_limit'}
                return run_window(current, replace(window,end=window.end+timedelta(seconds=30)), event, remaining, discovery)
            return SimpleNamespace(run=run)
        print('独立账户服务：仅从公共账本恢复执行登记；策略自行连接', flush=True)
        return ContinuousPositionRuntime(calendar, factory, stop_event=stop,
            run_forever=args.run_forever, prepare_seconds=args.prepare_seconds).run()


if __name__ == '__main__':
    main()
