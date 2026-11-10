"""复用一个账户服务，按各策略日历调度Hybrid和08独立worker。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace

from .config import read_config, resolve_config


def load_config(path):
    """仅组合启动声明；不改Hybrid配置、算法参数或信号文件。"""
    path = Path(path).resolve()
    declaration = json.loads(path.read_text())
    if set(declaration) - {'base_config', 'option', 'strategy_limits_only', 'option_checkpoint', 'option_signal'}:
        raise ValueError('worker启动声明存在未知字段')
    base_path = (path.parent / declaration['base_config']).resolve()
    config = read_config(base_path)
    if (config.get('execution_mode') != 'independent' or config['environment'] != 'realtime'
            or config.get('existing_position_policy') != 'preserve_external'
            or any(row['kind'] != 'hybrid_transformer' for row in config['strategies'])):
        raise ValueError('base_config须为原Hybrid实时独立归属配置')
    incoming = resolve_config({**deepcopy(config), 'strategies': [declaration['option']]}, path)['strategies'][0]
    if incoming['kind'] != 'option_vega':
        raise ValueError('新增worker须为option_vega')
    config['strategies'].append(incoming)
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


def main(argv=None):
    from bomber.framework.cli import ConfigArgumentParser
    parser = ConfigArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True, help='引用原Hybrid配置与08声明的JSON')
    parser.add_argument('--prepare-seconds', type=int, default=600)
    parser.add_argument('--run-forever', action='store_true')
    for flag in ('connect', 'enable-orders', 'confirm-simnow'):
        parser.add_argument('--'+flag, action='store_true')
    args = parser.parse_args(argv)
    config, _ = load_config(args.config)
    if args.prepare_seconds < 0:
        parser.error('prepare-seconds须为非负整数')
    if not args.connect:
        if args.enable_orders or args.confirm_simnow:
            parser.error('离线校验不接受报单授权')
        print('worker配置通过；Hybrid参数未改，未连接、未报单')
        return
    if not (args.enable_orders and args.confirm_simnow):
        parser.error('连接须显式enable-orders和confirm-simnow')
    from dotenv import load_dotenv
    from .run import PROJECT_ROOT
    from .continuous import ProcessWindowRuntime
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, StopSignal, orders_for_day
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    load_dotenv(PROJECT_ROOT/'.env')
    with strategy_state_lock(config['account_state_file']+'.continuous-owner'), StopSignal() as stop:
        saved = JsonStateStore(config['account_state_file']).load()
        expected = {row['strategy_id'] for row in config['strategies']}
        if saved is None or set(saved.payload['component_states']['session_identity']['identity']['strategies']) != expected:
            parser.error('首次添加08须先执行register_worker只读柜台核验；不自动新建或替换原账本')
        calendars = WorkerCalendars(load_calendars(config))
        def run_window(window, event):
            def run():
                current = deepcopy(config)
                current.update(trading_day=window.trading_day, seconds=86400)
                saved = JsonStateStore(current['account_state_file']).load()
                remaining = current['account']['max_session_orders'] - orders_for_day(saved.payload, window.trading_day)
                if remaining <= 0:
                    raise RuntimeError('共享TD当日报单额度已用完，不在切换worker时重置')
                return ProcessWindowRuntime(current, window, event, remaining, prepare_before_open=True,
                    worker_windows=calendars.worker_windows(current, window), prepare_seconds=args.prepare_seconds).run()
            return SimpleNamespace(run=run)
        print('独立worker调度：各策略使用自己的日历；原归属、批次及共享TD报单计数继续恢复', flush=True)
        return ContinuousPositionRuntime(calendars, run_window, run_forever=args.run_forever,
            stop_event=stop, prepare_seconds=args.prepare_seconds).run()


if __name__ == '__main__':
    main()
