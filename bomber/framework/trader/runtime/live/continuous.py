"""公共持仓运行调度：calendars决定窗口，应用负责装配，账本仍由执行服务保存。"""
from datetime import datetime
import signal
import threading


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
                if result.get("status") not in {"completed", "passed"}:
                    raise RuntimeError("交易窗口停机核验未通过，不自动进入下一窗口")
                self.results.append(result)
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
                "windows": self.windows_completed}
