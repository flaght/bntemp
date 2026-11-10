"""远程账户策略运行时：当前进程运行Runner；装配由策略入口注入。"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import re
import time
from threading import Event, Thread

from bomber.framework.trader.execution.contracts import MarketDataUnavailable, OrderBudgetExhausted
from bomber.framework.trader.persistence import JsonStateStore
from .contracts import LiveRunContext
from .continuous import StopRequested
from .local import strategy_state_lock


PREPARATION_HEARTBEAT_SECONDS = 5


class PreparationLease:
    """只在闭闸准备期间续租；独立连接不参与回报交付或信号保存。"""
    def __init__(self, connection, *, interval=None):
        self.connection = connection
        self.interval = PREPARATION_HEARTBEAT_SECONDS if interval is None else interval
        self.stopped = Event()
        self.failure = None
        self.thread = Thread(target=self._run, name='strategy-preparation-lease', daemon=True)

    def _run(self):
        from bomber.framework.trader.execution.ipc import UnixExecutionConnection
        connection = None
        while not self.stopped.wait(self.interval):
            token = getattr(self.connection, 'lease', None)
            if token is None:
                continue  # Runner尚未attach，不创建或接管租约。
            try:
                if connection is None:
                    connection = UnixExecutionConnection(self.connection.path, self.connection.sid,
                        self.connection.key, timeout=5)
                connection.lease = token
                connection.call('heartbeat', {})
            except Exception as error:
                self.failure = error
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, kind, error, traceback):
        self.stopped.set()
        self.thread.join(timeout=11)
        if kind is None:
            if self.thread.is_alive():
                raise RuntimeError('准备续租线程未退出，拒绝开放策略决策')
            if self.failure is not None:
                raise RuntimeError(f'策略准备期间续租失败: {self.failure}') from self.failure


def resolve_strategy_rows(rows, path, *, path_arguments=()):
    """仅解析实例声明与路径，不加载策略，不接受覆盖执行/授权/恢复政策。"""
    path = Path(path).expanduser().resolve()
    if not isinstance(rows, list) or not rows:
        raise ValueError('须声明至少一个策略实例')
    ids, files, result = set(), set(), []
    protected = {'--connect','--mode','--enable-orders','--confirm-simnow','--resume','--state-file',
        '--expected-trading-day','--seconds','--account-reconciliation','--strategy-position-source',
        '--run-forever','--adopt-existing-position','--expected-position','--simnow-environment',
        '--replay-md-trading-day','--execution-channel'}
    for raw in rows:
        if not isinstance(raw,dict) or set(raw) != {'strategy_id','kind','state_file','argv'}:
            raise ValueError('每个实例只接受strategy_id、kind、state_file和argv')
        row = deepcopy(raw)
        sid = row['strategy_id']
        if not isinstance(sid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',sid) or sid in ids:
            raise ValueError('策略ID无效或重复')
        if not isinstance(row['kind'],str) or not row['kind'].strip():
            raise ValueError('须声明策略类型')
        if not isinstance(row['state_file'],str) or not row['state_file'].strip():
            raise ValueError('须声明策略信号文件')
        state_file = str((path.parent/Path(row['state_file']).expanduser()).resolve())
        if state_file in files:
            raise ValueError('策略实例不能共用信号文件')
        argv = row['argv']
        if not isinstance(argv,list) or any(not isinstance(arg,str) for arg in argv):
            raise ValueError('策略argv须为字符串数组')
        if any(arg.split('=',1)[0] in protected for arg in argv):
            raise ValueError('策略参数不能覆盖执行、授权或恢复政策')
        for index,arg in enumerate(argv):
            key,separator,value = arg.partition('=')
            if key not in path_arguments:
                continue
            if not separator:
                if index+1 >= len(argv) or argv[index+1].startswith('--'):
                    raise ValueError(f'{key}缺少路径')
                value = argv[index+1]
            resolved = str((path.parent/Path(value).expanduser()).resolve())
            argv[index if separator else index+1] = f'{key}={resolved}' if separator else resolved
        row['state_file'] = state_file
        ids.add(sid); files.add(state_file); result.append(row)
    return result


def read_strategy_rows(path, *, path_arguments=()):
    path = Path(path).expanduser().resolve()
    document = json.loads(path.read_text())
    if not isinstance(document,dict) or set(document) != {'version','strategies'} or document['version'] != 1:
        raise ValueError('策略JSON只接受version和strategies，不包含账户字段')
    return resolve_strategy_rows(document['strategies'],path,path_arguments=path_arguments)


def load_account_connection(path, rows):
    """读取纯账户地址与本策略实例；不读取任何其他策略的配置。"""
    from bomber.framework.account_config import read_account_config
    account = read_account_config(path)
    rows = resolve_strategy_rows(rows,path)
    if any(row['state_file'] == account['account_state_file'] for row in rows):
        raise ValueError('账户账本和策略信号文件必须不同')
    return {**account,'strategies':rows}


def window_credentials(config, rows, window, stop_event, runtime_dir=None):
    from bomber.framework.account_config import read_account_config, FIELDS
    discovery = (runtime_dir or Path(config['account_state_file']).parent/'service')/'current.json'
    def validate(candidate):
        published = read_account_config(candidate['config'])
        expected = {key:deepcopy(config[key]) for key in FIELDS}
        expected.update(trading_day=window.trading_day,seconds=86400)
        if published != expected:
            raise ValueError('账户服务声明与本地配置不一致')
        for row in rows:
            path = Path(candidate['credentials_dir'])/(row['strategy_id']+'.json')
            if not path.is_file():
                raise ValueError(f"该策略ID尚未登记到账户: {row['strategy_id']}")
            credential = json.loads(path.read_text())
            if credential['strategy_id'] != row['strategy_id'] or Path(credential['socket']) != Path(candidate['socket']):
                raise ValueError('账户凭据的策略ID或socket不一致')
    candidate = wait_for_account(discovery,window,stop_event,validate)
    return {row['strategy_id']:Path(candidate['credentials_dir'])/(row['strategy_id']+'.json') for row in rows}


def resume_available(row, credentials):
    return Path(row['state_file']).is_file() or json.loads(Path(credentials).read_text()).get('bootstrap_signal') is not None


def validate_forward_identity(old, new):
    previous = deepcopy(old)
    if not previous['trading_day'] < new['trading_day'] or new['environment'] != 'realtime':
        raise ValueError('策略恢复仅支持向前的realtime交易日')
    previous['trading_day'] = new['trading_day']
    if previous != new:
        raise ValueError('跨日不能改变策略身份、参数、路由、预算或固定参考')


def run_remote_group(runtimes, stop_event):
    """在当前进程运行多个实例；任一实例失败时等待其余实例正常保存退出。"""
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    from threading import Event
    runtimes = list(runtimes)
    if not runtimes or len({r.strategy_id for r in runtimes}) != len(runtimes):
        raise ValueError("须提供具有不同策略ID的运行实例")
    if stop_event.is_set():
        return
    if len(runtimes) == 1:
        return runtimes[0].run(stop_event)
    stopped = Event()
    failure = None
    with ThreadPoolExecutor(max_workers=len(runtimes), thread_name_prefix="strategy") as pool:
        pending = {pool.submit(runtime.run, stopped) for runtime in runtimes}
        while pending:
            if stop_event.is_set():
                stopped.set()
            done, pending = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    future.result()
                except BaseException as error:
                    stopped.set()
                    failure = failure or error
    if failure is not None:
        raise failure


def wait_for_account(discovery_file, window, stop_event, validate, *, clock=None, timeout=180):
    """发现覆盖本策略窗口的账户服务；不创建进程或连接TD。"""
    clock = clock or (lambda: datetime.now(window.end.tzinfo))
    deadline = time.monotonic() + timeout + max(0, (window.start-clock()).total_seconds())
    while not stop_event.is_set() and clock() < window.end:
        try:
            candidate = json.loads(Path(discovery_file).read_text())
            if (candidate['trading_day'] == window.trading_day and Path(candidate['socket']).exists()
                    and datetime.fromisoformat(candidate['start']) <= window.start
                    and datetime.fromisoformat(candidate['end']) >= window.end):
                validate(candidate)
                return candidate
        except FileNotFoundError:
            pass  # 窗口切换时等待新的原子发布文件。
        if time.monotonic() >= deadline:
            raise RuntimeError("未找到本窗口账户服务；请先独立启动账户服务")
        stop_event.wait(.2)
    raise StopRequested()


def session_reference_is_current(session):
    """沿用共享宿主的固定参考检查，兼容没有专属检查方法的Runner。"""
    current = getattr(session.runner, "reference_is_current", None)
    return current() if callable(current) else session.references.spec == session.runner.fixed_spec


def capture_session_failure(session):
    """停机清空Feed之前保存原健康结果及本策略账户连接状态。"""
    from bomber.framework.trader.runtime.live.health import readiness_failure_context
    context = readiness_failure_context(session.runner)
    port = session.client
    context["execution_port"] = {"started": port._started, "stopping": port._stopping,
        "reconciled": port.is_reconciled, "account_reconciled": port.state.get("reconciled"),
        "observation_age_seconds": time.monotonic() - port._observed}
    session.runner.failure_readiness = context
    return context


def require_session_health(session):
    runner = session.runner
    if runner.failure:
        raise RuntimeError(runner.failure)
    reason = None
    if not runner.session_ready():
        reason = "策略行情或账户未就绪"
    elif not session_reference_is_current(session):
        reason = "策略参考合约或条款变化"
    if reason:
        context = capture_session_failure(session)
        raise RuntimeError(f"{reason}: {context}")


def poll_strategy_session(session, save):
    # 原参考缓存只在发布版本时持提交锁；数据库查询允许行情分派继续推进。
    with session.runner._submit_lock:
        if session.runner.failure:
            raise RuntimeError(session.runner.failure)
    session.references.refresh()
    with session.runner._submit_lock:
        if session.runner.failure:
            raise RuntimeError(session.runner.failure)
        session.client.drain_events()
        poll = getattr(session.runner, "poll_live_inputs", None)
        if callable(poll):
            poll()
        require_session_health(session)
        save()


@dataclass
class RemoteStrategyRuntime:
    strategy_id: str
    state_file: Path
    credentials: Path
    trading_day: str
    identity: dict
    prepare: object
    validate_identity: object
    report: object
    resume: bool = False
    activate_at: datetime | None = None
    stop_at: datetime | None = None
    seconds: float = 86400
    ready_seconds: float = 30
    restore_fills_received: bool = True
    window_context: bool = False
    poll_session: object = poll_strategy_session

    def run(self, stop_event):
        from bomber.framework.trader.execution.ipc import UnixExecutionConnection
        from bomber.framework.trader.execution.ipc_client import RemoteAccountService, RemoteTraderSession
        from .continuous import wait_for_open
        identity = self.identity
        report = self.report
        report.begin()
        session, failure, shutdown_failure, status = None, None, None, "failed"
        stop_reason = None
        save = None
        with ExitStack() as resources:
            try:
                if stop_event.is_set():
                    raise StopRequested()
                resources.enter_context(strategy_state_lock(Path(self.state_file)))
                store = JsonStateStore(Path(self.state_file))
                persisted = store.load()
                if persisted and persisted.payload.get('version') != 1:
                    raise ValueError('策略检查点版本无效，保留原文件')
                credentials = json.loads(self.credentials.read_text())
                bootstrap = credentials.get("bootstrap_signal")
                if bool(persisted) != self.resume and not (self.resume and persisted is None and bootstrap is not None):
                    raise ValueError("策略进程恢复标志、身份或状态文件不一致")
                if persisted and self.restore_fills_received and persisted.payload["identity"] != identity:
                    self.validate_identity(persisted.payload["identity"], identity)
                if credentials["strategy_id"] != self.strategy_id:
                    raise ValueError("客户端凭据不属于本策略")
                connection = UnixExecutionConnection(Path(credentials["socket"]), self.strategy_id, credentials["key"])
                state = connection.call("describe", {})["state"]
                if state["trading_day"] != self.trading_day:
                    raise ValueError("账户服务交易日不一致")
                service = RemoteAccountService(connection, credentials["configuration"], state)
                service.allow_reconfigure = bool(credentials.get('allow_reconfigure', False))
                driver = RemoteTraderSession(service, self.trading_day)
                context = LiveRunContext(resources=resources,
                    trading_day=datetime.strptime(self.trading_day, "%Y%m%d").date())
                session, identity = self.prepare(service, driver, context)
                port = session.client
                if persisted and persisted.payload["identity"] != identity:
                    self.validate_identity(persisted.payload["identity"], identity)
                if (credentials.get('check_reference', True) and hasattr(session, "session_identity")
                        and credentials.get("reference") != session.session_identity):
                    raise ValueError("策略行情/模型/条款身份与账户服务声明不一致")
                if persisted and persisted.payload["revision"] > state["source_revision"]:
                    raise ValueError("策略信号版本领先账户服务")
                if not persisted and state["source_revision"] and bootstrap is None:
                    raise ValueError("账户已有策略来源，须使用原策略检查点恢复")
                session.strategy.restore_target_revision(state["source_revision"])
                if persisted and hasattr(session, "restore_signal"):
                    session.restore_signal(persisted.payload["signal"])
                elif bootstrap is not None:
                    session.restore_signal(bootstrap)
                if self.restore_fills_received:
                    session.strategy.fills_received = persisted.payload.get("fills_received", 0) if persisted else 0
                restored_cursor = getattr(session, "restored_signal_cursor", None)
                port.set_cursor(restored_cursor if restored_cursor is not None else
                    (persisted.payload.get("cursor", state["ack"]) if persisted else state["ack"]))
                generation = persisted.generation if persisted else 0
                def save(cursor=None):
                    nonlocal generation
                    saved = store.save({"version": 1, "identity": identity,
                        "revision": port.state["source_revision"], "cursor": port.cursor if cursor is None else cursor,
                        "fills_received": session.strategy.fills_received,
                        **({"signal": session.signal_checkpoint.snapshot_state()}
                           if hasattr(session, "signal_checkpoint") else {})}, expected_generation=generation)
                    generation = saved.generation
                port.completed = save
                bind = getattr(session, "bind_signal_checkpoint", None)
                if callable(bind):
                    bind(save)
                preparing = True
                port.ready_check = lambda: not preparing and session.runner.session_ready()
                stop_at = self.stop_at
                if stop_at is not None:
                    from bomber.framework.calendars.sessions import TradingWindow
                    window = TradingWindow(self.trading_day, self.activate_at, stop_at, 'worker')
                    session.runner.continuous_window = window
                    session.runner.continuous_stop_event = stop_event
                    if self.window_context:
                        from bomber.framework.trader.runtime.live.continuous import LiveWindowContext
                        session.runner.window_context = LiveWindowContext(None, window, stop_event,
                            source_day=self.trading_day, resume=self.resume)
                with PreparationLease(connection):
                    session.runner.start()
                    from bomber.framework.trader.runtime.live.shared import prepare_strategy_history
                    activate_at = self.activate_at
                    if activate_at is not None:
                        if prepare_strategy_history(session, require_ready=False) is False:
                            raise StopRequested()
                        def poll_preparation():
                            if stop_at is not None and datetime.now(stop_at.tzinfo) >= stop_at:
                                raise StopRequested()
                            if session.runner.failure:
                                raise RuntimeError(session.runner.failure)
                            port.drain_events()
                            session.references.refresh()
                            poll = getattr(session.runner, "poll_live_inputs", None)
                            if callable(poll):
                                poll()
                        wait_for_open(activate_at, stop_event, poll=poll_preparation)
                    ready_seconds = self.ready_seconds
                    until_ready = time.monotonic() + ready_seconds
                    while not session.runner.session_ready():
                        if stop_event.is_set() or stop_at is not None and datetime.now(stop_at.tzinfo) >= stop_at:
                            raise StopRequested()
                        if session.runner.failure:
                            raise RuntimeError(session.runner.failure)
                        port.drain_events()
                        session.references.refresh()
                        poll = getattr(session.runner, "poll_live_inputs", None)
                        if callable(poll):
                            poll()
                        if time.monotonic() >= until_ready:
                            context = capture_session_failure(session)
                            raise RuntimeError(f"策略未在期限内取得健康行情/参考/账户: {context}")
                        time.sleep(0.2)
                    port.drain_events()
                    save()
                    if stop_event.is_set():
                        raise StopRequested()
                    if activate_at is None:
                        if prepare_strategy_history(session) is False:
                            raise StopRequested()
                    else:
                        from bomber.framework.trader.runtime.live.lifecycle import validate_session_ready
                        session.references.refresh()
                        validate_session_ready(session)
                    if stop_event.is_set():
                        raise StopRequested()
                preparing = False
                session.runner.begin_bars()
                until = time.monotonic() + self.seconds
                while (not stop_event.is_set() and time.monotonic() < until
                        and (stop_at is None or datetime.now(stop_at.tzinfo) < stop_at)):
                    self.poll_session(session, save)
                    time.sleep(0.2)
                status = "completed"
            except OrderBudgetExhausted:
                stop_reason, status = "order_budget_exhausted", "completed"
            except MarketDataUnavailable as error:
                stop_reason, status = "market_data_unavailable", "completed"
                try:
                    context = capture_session_failure(session)
                except Exception as diagnostic_error:
                    context = {"diagnostic_error": str(diagnostic_error)}
                context["account_valuation"] = error.readiness
                session.runner.failure_readiness = context
            except StopRequested:
                status = "completed"
            except BaseException as error:
                failure = str(error) or type(error).__name__
                if session and getattr(session.runner, "failure_readiness", None) is None:
                    try:
                        capture_session_failure(session)
                    except Exception as diagnostic_error:
                        session.runner.failure_readiness = {"diagnostic_error": str(diagnostic_error)}
                raise
            finally:
                try:
                    if session:
                        try:
                            with session.runner._submit_lock:
                                session.runner.accept_bars = False
                                session.client.begin_stop()  # 撤单、同步归属及回调完成后才解绑。
                                if save is not None:
                                    save()
                            finish = getattr(session, "finish_report", None)
                            if callable(finish):
                                finish()
                        finally:
                            session.runner.stop()
                except Exception as error:
                    shutdown_failure, status = str(error), "failed"
                    if failure is None:
                        raise
                finally:
                    from bomber.framework.trader.runtime.live.shared import history_snapshot
                    report.write(status=status, fields={"failure": failure, "shutdown_failure": shutdown_failure,
                        "stop_reason": stop_reason,
                        **(history_snapshot(session) if session else {}),
                        "failure_readiness": getattr(session.runner, "failure_readiness", None) if session else None,
                        "verification": "remote_strategy_runtime", "strategy_id": self.strategy_id,
                        "positions": session.client.state["positions"] if session else None,
                        "cursor": session.client.cursor if session else None,
                        "fills_received": session.strategy.fills_received if session else None,
                        "price_sync": getattr(session.client, "price_sync", None) if session else None,
                        "source_revision": session.client.state["source_revision"] if session else None,
                        "last_targets": getattr(session.strategy, "last_targets", None) if session else None,
                        "reference_manifest": getattr(session.references, "manifest", None) if session else None,
                        "bar_counts": {str(i): n for i, n in getattr(session.runner, "bar_counts", {}).items()} if session else {},
                        "last_processed_ns": getattr(session.strategy, "last_processed_ns", None) if session else None},
                        session=session, events=session.client.received_events if session else ())
