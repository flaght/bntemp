"""Small, run-local timing totals; no logging or market/strategy dependencies."""
from contextlib import contextmanager
from time import perf_counter


class PerformanceStats:
    """Aggregate wall durations and counts. Nested timings are inclusive, not additive.

    One historical replay thread owns an instance. This is diagnostic wall time,
    not simulated market time or a CPU profiler.
    """

    def __init__(self, clock=perf_counter):
        self.clock = clock
        self.timings = {}
        self.counters = {}

    def add(self, name, seconds):
        row = self.timings.setdefault(name, {"seconds": 0.0, "calls": 0})
        row["seconds"] += seconds
        row["calls"] += 1

    def count(self, name, amount=1):
        self.counters[name] = self.counters.get(name, 0) + amount

    @contextmanager
    def measure(self, name):
        started = self.clock()
        try:
            yield
        finally:
            self.add(name, self.clock() - started)

    def snapshot(self):
        return {"timing_semantics": "wall_seconds_inclusive_nested_do_not_sum",
            "timings": {key: {"seconds": round(row["seconds"], 6), "calls": row["calls"]}
                for key, row in sorted(self.timings.items())},
            "counters": dict(sorted(self.counters.items()))}
