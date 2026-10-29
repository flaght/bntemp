"""Live 常驻调度：日历决定窗口，会话负责连接及执行，业务组件保存自身状态。"""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import signal
import threading
import time
from types import SimpleNamespace


class StopRequested(Exception):
    """主动停止不是行情或账户故障。"""


class StopSignal:
    def __init__(self, event=None):
        self.event = event or threading.Event()
        self.previous = {}

    def __enter__(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.previous[signum] = signal.signal(signum, lambda *_: self.event.set())
        return self.event

    def __exit__(self, *args):
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)


def orders_for_day(payload, day):
    """用持久化订单身份计数，午休或进程重启不重新获得当日额度。"""
    return sum(row["client_order_id"].rsplit("-", 2)[-2] == day
               for rows in payload.get("orders", {}).values() for row in rows)


@dataclass
class LiveWindowContext:
    """一次 Live 窗口的公共参数；state 仅供同一进程的业务组件续接。"""
    calendar: object
    window: object
    stop_event: object
    source_day: str
    resume: bool = False
    max_session_orders: int | None = None
    clock: object = None
    state: dict = field(default_factory=dict)
    scheduled_pause: bool = False
    _flushed: bool = False

    @property
    def trading_day(self):
        return self.window.trading_day

    def now(self):
        value = self.clock() if self.clock is not None else datetime.now(self.window.end.tzinfo)
        if value.tzinfo is None:
            raise ValueError("Live 窗口时钟须携带时区")
        return value

    def stop_requested(self):
        return self.stop_event.is_set() or self.now() >= self.window.end

    def pause_runner(self, runner, client=None):
        """正常暂停只关闭新目标和授权，不平仓、不标记策略故障。"""
        with getattr(runner, "_submit_lock", nullcontext()):
            already_paused = self.scheduled_pause
            runner.accept_bars = False
            runner.scheduled_pause = True
            self.scheduled_pause = True
            if client is None:
                client = getattr(runner, "client", None)
                if client is None:
                    client = next(iter(getattr(runner, "_clients", {}).values()), None)
            disarm = getattr(client, "disarm", None)
            if callable(disarm) and not already_paused:
                disarm("scheduled_pause_or_stop")

    def pause_due(self, session, *, sleep=None):
        """先于会话健康轮询检查休市；可由行情组件声明封闭最后分钟。"""
        if not self.stop_requested():
            return False
        self.pause_runner(session.runner, session.client)
        if getattr(session, "flush_completed_bars", False) and not self._flushed:
            feed = session.bar_feed
            now = self.now()
            if now >= self.window.end:
                delay = getattr(feed, "close_delay_ns", 0)
                delay = delay if type(delay) is int and delay > 0 else 0
                remaining = (self.window.end - now).total_seconds() + delay / 1e9
                if remaining > 0:
                    (sleep or time.sleep)(min(remaining, 1.0))
            feed.poll()
            self._flushed = True
        return True

    def restore_components(self, session):
        """沿用 snapshot_state/restore_state 契约，不识别模型或持仓字段。"""
        components = getattr(session, "window_components", {})
        if self.state and set(self.state) != set(components):
            raise ValueError("窗口续接的业务组件集合变化，不能丢弃旧状态")
        for name, saved in self.state.items():
            components[name].restore_state(deepcopy(saved))


def run_continuous(factory, *, calendar, state_file=None, max_session_orders=None,
                   expected_trading_day=None, expected_source_day=None,
                   order_limit_policy="stop", owner_lock=None, stop_event=None,
                   clock=None, wait=None, status=None):
    """公共 --run-forever 入口；factory(window_context=...) 返回一次会话。

    会话用 ManagedLiveRuntime 管理窗口边界，可注册 window_components 续接进程内
    业务窗口。交易状态仍由原持久化组件恢复；异常及停机失败不会自动重试。
    """
    if order_limit_policy not in {"stop", "wait"}:
        raise ValueError("额度耗尽政策须为 stop 或 wait")
    if max_session_orders is not None and (type(max_session_orders) is not int or max_session_orders < 1):
        raise ValueError("当日报单上限须为正整数")
    from bomber.framework.trader.persistence import JsonStateStore
    from .local import strategy_state_lock
    lock = owner_lock
    if lock is None:
        lock = (strategy_state_lock(str(Path(state_file).resolve()) + ".continuous-owner")
                if state_file is not None else nullcontext())
    signal_scope = StopSignal() if stop_event is None else nullcontext(stop_event)
    carried = {}
    started = False

    def session_factory(window, stop):
        def run_window():
            nonlocal started
            day = window.trading_day
            source_day = calendar.previous_trading_day(day)
            if not started:
                if expected_trading_day is not None and expected_trading_day != day:
                    raise ValueError("首个日历窗口交易日与 expected-trading-day 不一致")
                if expected_source_day is not None and expected_source_day != source_day:
                    raise ValueError("首个窗口参考来源日与 expected-source-day 不一致")
            if stop.is_set():
                return {"status": "completed"}
            saved = JsonStateStore(state_file).load() if state_file is not None else None
            used = orders_for_day(saved.payload, day) if saved else 0
            if max_session_orders is not None and used >= max_session_orders:
                if order_limit_policy == "stop":
                    raise RuntimeError("本交易日报单额度已用完；保留检查点，不重建会话刷新额度")
                runtime.status("本交易日报单额度已用完，等待下一交易日")
                return {"status": "skipped", "reason": "daily_order_limit"}
            context = LiveWindowContext(calendar, window, stop, source_day,
                resume=saved is not None,
                max_session_orders=None if max_session_orders is None else max_session_orders-used,
                clock=runtime.now, state=deepcopy(carried))
            worker = factory(window_context=context)
            worker.window_context = context
            result = worker.run()
            if result.get("status") not in {"passed", "completed"}:
                raise RuntimeError("连续会话停机核对失败，不自动重启")
            session = getattr(worker, "session", None)
            snapshots = {name: component.snapshot_state()
                         for name, component in getattr(session, "window_components", {}).items()}
            carried.clear()
            carried.update(snapshots)
            started = True
            return result
        return SimpleNamespace(run=run_window)

    with lock, signal_scope as stop:
        runtime = ContinuousPositionRuntime(calendar, session_factory, run_forever=True,
            stop_event=stop, clock=clock, wait=wait, status=status)
        return runtime.run()


class ContinuousPositionRuntime:
    """按真实日历窗口续接归属仓位；不复制策略、持仓或柜台恢复算法。

    session_factory(window, stop_event)返回实现run()的会话。正常run结束必须完成
    撤单、对账及保存；任何异常直接传出，禁止盲目重试。会话返回status=completed
    或passed才可进入下一窗口。使用注入时钟/等待器可无网络、无sleep验收。
    """
    def __init__(self, calendar, session_factory, *, run_forever=False, stop_event=None,
                 clock=None, wait=None, status=None, poll_seconds=5):
        if not 0 < poll_seconds <= 60:
            raise ValueError("休市轮询须在(0,60]秒")
        self.calendar, self.session_factory = calendar, session_factory
        self.run_forever, self.stop = run_forever, stop_event or threading.Event()
        self.clock = clock or (lambda: datetime.now(calendar.zone))
        self.wait = wait or self.stop.wait
        self.status = status or (lambda message: print(message, flush=True))
        self.poll_seconds = poll_seconds
        self.results = []
        self.windows_completed = 0
        self.windows_skipped = 0
        self._last_time = None

    def now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("持续运行时钟须携带时区")
        if self._last_time is not None and now < self._last_time:
            raise RuntimeError("持续运行墙钟倒退，停止而不重复进入旧窗口")
        self._last_time = now
        self.calendar.is_trading_day(now.astimezone(self.calendar.zone).date())
        return now

    def run(self):
        first = self.now()
        day = first.astimezone(self.calendar.zone).date()
        completed = set()
        last_status = None
        while not self.stop.is_set():
            now = self.now()
            if not self.run_forever:
                windows = self.calendar.windows(day)
                if not windows or now >= max(window.end for window in windows):
                    break
            window = self.calendar.window(now)
            token = None if window is None else (window.trading_day, window.segment_id, window.start, window.end)
            if window is not None and token not in completed:
                message = f"进入交易窗口: TD={window.trading_day} segment={window.segment_id} end={window.end.isoformat()}"
                self.status(message)
                result = self.session_factory(window, self.stop).run()
                if result.get("status") not in {"completed", "passed", "skipped"}:
                    raise RuntimeError("交易窗口停机核验未通过，不自动进入下一窗口")
                self.results.append(result)
                if result["status"] == "skipped":
                    self.windows_skipped += 1
                else:
                    self.windows_completed += 1
                del self.results[:-32]
                completed = {token}  # 单调墙钟下旧窗口不会重入，常驻无需累积所有日期。
                last_status = None
                if self.stop.is_set():
                    break
                continue
            message = "休市等待，归属仓位保存在原检查点" if window is None else "本窗口已结束管理，等待下一交易窗口"
            if message != last_status:
                self.status(message)
                last_status = message
            self.wait(self.poll_seconds)
        return {"status": "completed", "stopped_by_operator": self.stop.is_set(),
                "windows": self.windows_completed, "skipped_windows": self.windows_skipped}
