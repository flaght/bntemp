"""跨期运行可观测性：控制线程报告新帧或定期心跳，不改变交易判断。"""
from datetime import datetime
import time
from zoneinfo import ZoneInfo


class CalendarProgress:
    def __init__(self, interval_seconds=15, *, monotonic=None):
        self.interval = interval_seconds
        self.clock = monotonic or time.monotonic
        self.last_print = None
        self.last_frames = None

    def __call__(self, session):
        now = self.clock()
        runner, strategy = session.runner, session.strategy
        with runner._submit_lock:
            frames = strategy.complete_frames
            if (self.last_print is not None and frames == self.last_frames
                    and now - self.last_print < self.interval):
                return
            last = strategy.last_frame_ns
            stamp = (datetime.fromtimestamp((last + 1) // 10**9, ZoneInfo("Asia/Shanghai")).isoformat()
                     if last >= 0 else None)
            missing = [{"ts_event": ts, "missing": sorted(str(i) for i in runner.fixed_ids - set(frame))}
                       for ts, frame in sorted(runner.pending_frames.items())][-3:]
            print(f"跨期运行进度: history_frames={getattr(runner, 'history_frame_count', 0)} "
                  f"live_frames={getattr(runner, 'live_frame_count', 0)} "
                  f"window={len(strategy.signal.history)}/{strategy.config.lookback} "
                  f"total_frames={frames} last_completed_minute={stamp} "
                  f"bars={{{', '.join(str(i) + ': ' + str(n) for i, n in runner.bar_counts.items())}}} "
                  f"waiting_legs={missing} direction={strategy.direction} "
                  f"last_z={strategy.last_z} submissions={strategy.submissions}", flush=True)
        self.last_print, self.last_frames = now, frames
