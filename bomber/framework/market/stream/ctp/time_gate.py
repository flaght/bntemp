"""CTP小幅超前事件只等待本机时钟追上，不改写事件时间或放宽报单时效。"""

import time


class CtpFutureEventError(RuntimeError):
    pass


def wait_for_event_time(timestamp, buffer_ns, stop_event, *, clock_ns=None, monotonic_ns=None):
    if isinstance(buffer_ns, bool) or not isinstance(buffer_ns, int) or not 0 <= buffer_ns <= 100_000_000:
        raise ValueError("CTP未来事件缓冲须为0至100毫秒")
    if not buffer_ns:
        return True  # 旧配置保持原严格健康门控，不改变Feed分派。
    clock_ns = clock_ns or time.time_ns
    monotonic_ns = monotonic_ns or time.monotonic_ns
    deadline = monotonic_ns() + buffer_ns
    while True:
        if stop_event.is_set():
            return False
        now = clock_ns()
        ahead = timestamp - now
        if ahead <= 0:
            return not stop_event.is_set()
        if ahead > buffer_ns or monotonic_ns() >= deadline:
            raise CtpFutureEventError(f"CTP事件时间超前且超过缓冲: event_ns={timestamp} now_ns={now} "
                f"ahead_ns={ahead} buffer_ns={buffer_ns}")
        remaining = min(ahead, deadline - monotonic_ns())
        if remaining <= 0:
            continue
        if stop_event.wait(remaining / 1e9):
            return False
