"""控制线程的横截面预热/实时进度，不改变信号或订单。"""
import time


class CrossSectionProgress:
    def __init__(self):
        self.last_print = None
        self.last_frames = None

    def __call__(self, session):
        now = time.monotonic()
        runner, strategy = session.runner, session.strategy
        with runner._submit_lock:
            count = strategy.synchronized_frames
            if self.last_print is not None and count == self.last_frames and now - self.last_print < 15:
                return
            waiting = [{"ts_event": stamp, "missing": sorted(str(i) for i in runner.fixed_ids - set(frame))}
                       for stamp, frame in sorted(runner.pending_frames.items())][-3:]
            windows = {p: len(values) for p, values in strategy.signal.history.items()}
            print(f"横截面运行进度: history_frames={runner.history_frame_count} live_frames={runner.live_frame_count} "
                  f"window={windows}/{strategy.lookback + 1} rebalances={strategy.rebalances} "
                  f"bars={{{', '.join(str(i) + ': ' + str(n) for i, n in runner.bar_counts.items())}}} "
                  f"waiting_legs={waiting}", flush=True)
        self.last_print, self.last_frames = now, count
