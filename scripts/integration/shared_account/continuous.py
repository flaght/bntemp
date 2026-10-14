"""08共享账户日历调度：同进程多策略，或唯一账户服务加独立策略进程。"""
import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .config import read_config


def config_for_day(config, day):
    result = deepcopy(config)
    if result['environment'] != 'realtime' or any(row['kind'] != 'option_vega' for row in result['strategies']):
        raise ValueError('本批自动跨日装配支持08；其他策略的历史预热/昨仓须另行接入')
    for row in result['strategies']:
        if 'stop_after_seconds' in row or any(flag.split('=', 1)[0] == '--valuation-day' for flag in row['argv']):
            raise ValueError('持续配置不能固定valuation-day或stop_after_seconds；日期由窗口及TD共同核验')
    result.update(trading_day=day, seconds=86400)
    return result


class ProcessWindowRuntime:
    """调度server/worker原入口；先正常停止worker，再停止唯一TD服务。"""
    def __init__(self, config, window, stop_event, remaining, *, popen=None, clock=None):
        self.config, self.window, self.stop, self.remaining = config, window, stop_event, remaining
        self.popen = popen or subprocess.Popen
        self.clock = clock or (lambda: datetime.now(window.end.tzinfo))
        self.workers, self.server = [], None

    def stop_process(self, process):
        if process.poll() is None:
            process.terminate()  # server/worker的SIGTERM进入各自finally，不能直接kill丢回报。
        deadline = time.monotonic() + 45
        while process.poll() is None:
            if time.monotonic() >= deadline:
                raise RuntimeError('进程正常停机超时；保留进程/账户锁，不自动重启或强杀')
            time.sleep(0.05)
        if process.returncode != 0:
            raise RuntimeError(f'共享子进程失败: exit={process.returncode}')

    def run(self):
        if self.config.get('execution_mode') != 'independent':
            raise ValueError('独立进程部署须使用independent执行模式')
        if self.stop.is_set() or self.clock() >= self.window.end:
            return {'status': 'completed'}
        directory = Path(self.config['report_dir']) / ('continuous-window-' + uuid.uuid4().hex)
        directory.mkdir(parents=True, mode=0o700)
        config_file = directory/'config.json'
        socket = Path('/tmp') / ('bomber-vega-' + uuid.uuid4().hex + '.sock')
        credentials = directory/'credentials'
        config_file.write_text(json.dumps(self.config, ensure_ascii=False, indent=2)+'\n')
        common = ['--config', str(config_file), '--connect', '--enable-orders', '--confirm-simnow', '--resume']
        errors = []
        try:
            self.server = self.popen([sys.executable, '-u', '-m', 'scripts.integration.shared_account.server',
                *common, '--socket', str(socket), '--credentials-dir', str(credentials),
                '--remaining-orders', str(self.remaining)], start_new_session=True)
            deadline = time.monotonic() + 180
            while not all((credentials/(row['strategy_id']+'.json')).is_file() for row in self.config['strategies']):
                if self.stop.is_set() or self.clock() >= self.window.end:
                    return {'status': 'completed'}
                if self.server.poll() is not None:
                    raise RuntimeError('账户服务在worker启动前退出；请检查server报告')
                if time.monotonic() >= deadline:
                    raise RuntimeError('账户服务未在期限内发布本窗口能力文件')
                self.stop.wait(0.2)
            for row in self.config['strategies']:
                if self.stop.is_set() or self.clock() >= self.window.end:
                    break
                self.workers.append(self.popen([sys.executable, '-u', '-m', 'scripts.integration.shared_account.worker',
                    *common, '--strategy-id', row['strategy_id'], '--credentials',
                    str(credentials/(row['strategy_id']+'.json'))], start_new_session=True))
            while not self.stop.is_set() and self.clock() < self.window.end:
                if self.server.poll() is not None or any(worker.poll() is not None for worker in self.workers):
                    raise RuntimeError('共享运行进程提前退出，不自动重复启动')
                self.stop.wait(0.2)
        finally:
            for process in self.workers:
                try:
                    self.stop_process(process)
                except Exception as error:
                    errors.append(error)
            # 若worker未完成保存/回报确认，保留TD服务帮助诊断，禁止先断柜台。
            if not errors and self.server is not None:
                try:
                    self.stop_process(self.server)
                except Exception as error:
                    errors.append(error)
            if errors:
                raise RuntimeError('共享窗口停机失败，不进入下一窗口') from errors[0]
        return {'status': 'completed', 'trading_day': self.window.trading_day,
                'segment': self.window.segment_id, 'process_layout': 'separate-processes'}


class SharedWindowRuntime:
    def __init__(self, config, calendar, window, stop_event, layout):
        self.config, self.calendar, self.window, self.stop, self.layout = config, calendar, window, stop_event, layout

    def run(self):
        from bomber.framework.trader.persistence import JsonStateStore
        from bomber.framework.trader.runtime.live.continuous import orders_for_day
        from scripts.integration.options.shared import observe_day, run_session
        config = config_for_day(self.config, self.window.trading_day)
        saved = JsonStateStore(Path(config['account_state_file'])).load()
        if saved is None:
            raise ValueError('持续管理须恢复已有归属检查点；不能新建空账本接管账户仓位')
        if self.stop.is_set():
            return {'status': 'completed'}
        day = observe_day()
        if day != self.window.trading_day:
            raise RuntimeError('TD与公共calendars窗口不一致，禁止跨日装配')
        remaining = config['account']['max_session_orders'] - orders_for_day(saved.payload, day)
        if remaining <= 0:
            raise RuntimeError('当日报单额度已用完，不重新获得额度')
        if self.layout == 'same-process':
            return run_session(config, resume=True, window=self.window, stop_event=self.stop,
                session_order_budget=remaining, calendar_provenance=self.calendar.provenance)
        return ProcessWindowRuntime(config, self.window, self.stop, remaining).run()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--process-layout', choices=('same-process', 'separate-processes'), default='same-process')
    parser.add_argument('--run-forever', action='store_true')
    for flag in ('connect', 'enable-orders', 'confirm-simnow'):
        parser.add_argument('--'+flag, action='store_true')
    from bomber.framework.dataprep.session_inputs import add_session_arguments
    add_session_arguments(parser)
    args = parser.parse_args(argv)
    config = read_config(args.config)
    config_for_day(config, config['trading_day'])
    if args.process_layout == 'separate-processes' and config.get('execution_mode') != 'independent':
        parser.error('不同进程须使用independent')
    if not args.connect:
        if args.enable_orders or args.confirm_simnow:
            parser.error('离线检查不接受报单授权')
        print('持续管理配置通过；未连接、未修改状态')
        return
    if not (args.enable_orders and args.confirm_simnow):
        parser.error('连接须显式双报单授权')
    from dotenv import load_dotenv
    from .run import PROJECT_ROOT
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, StopSignal
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    from scripts.integration.options.shared import load_calendar
    load_dotenv(PROJECT_ROOT/'.env')
    state = Path(config['account_state_file'])
    with strategy_state_lock(str(state)+'.continuous-owner'), StopSignal() as stop:
        saved = JsonStateStore(state).load()
        if saved is None:
            parser.error('须使用原归属检查点')
        identity = saved.payload['component_states']['session_identity']['identity']
        if 'strategies' not in identity:
            parser.error('原生08首次导入使用options.shared；不能把一份原仓复制为多策略归属')
        if set(identity['strategies']) != {row['strategy_id'] for row in config['strategies']}:
            parser.error('配置与原检查点的策略集合不一致，不能自动添加或删除归属')
        first = next(iter(identity['strategies'].values()))['reference']
        if any(row.get('kind') != 'option_vega' or row['reference']['calendar_manifest']['calendar_id'] !=
               first['calendar_manifest']['calendar_id'] for row in identity['strategies'].values()):
            parser.error('本批持续调度须为相同日历的08策略，不能混用其他策略或交易窗口')
        calendar = load_calendar(args, first)
        print('会话日历:', json.dumps(calendar.provenance, ensure_ascii=False), flush=True)
        result = ContinuousPositionRuntime(calendar,
            lambda window, event: SharedWindowRuntime(config, calendar, window, event, args.process_layout),
            run_forever=args.run_forever, stop_event=stop).run()
        print('持续管理结束:', json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
