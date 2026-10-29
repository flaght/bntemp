"""实际CTP深度回调与健康状态机；无SDK、网络和伪造派生成交。"""
import ast
from copy import deepcopy
from decimal import Decimal
import math
from collections.abc import Mapping
from dataclasses import dataclass
import importlib.util
import logging
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("_depth_health_contract",
    ROOT / "bomber/framework/market/stream/health.py")
health = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = health
spec.loader.exec_module(health)
gate_spec = importlib.util.spec_from_file_location("_ctp_time_gate", ROOT / "bomber/framework/market/stream/ctp/time_gate.py")
time_gate = importlib.util.module_from_spec(gate_spec)
gate_spec.loader.exec_module(time_gate)
source = ast.parse((ROOT / "bomber/framework/market/stream/ctp/feed.py").read_text())
observation = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "CtpDepthObservation")
feed = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "CtpLiveDataFeed")
callback = next(n for n in feed.body if isinstance(n, ast.FunctionDef) and n.name == "on_depth_market_data")
namespace = dict(dataclass=dataclass, Mapping=Mapping, Any=Any, time=time, logger=logging.getLogger(__name__))
namespace.update(CtpFutureEventError=time_gate.CtpFutureEventError, wait_for_event_time=time_gate.wait_for_event_time)
exec(compile(ast.Module([observation, callback], type_ignores=[]), "<actual CTP depth callback>", "exec"), namespace)
Handler = type("Handler", (), {"on_depth_market_data": namespace["on_depth_market_data"]})
converter_tree = ast.parse((ROOT / "bomber/framework/market/stream/ctp/converter.py").read_text())
number = next(n for n in converter_tree.body if isinstance(n, ast.FunctionDef) and n.name == "_ctp_number")
converter_class = next(n for n in converter_tree.body if isinstance(n, ast.ClassDef) and n.name == "CtpTickConverter")
reference = deepcopy(next(n for n in converter_class.body if isinstance(n, ast.FunctionDef) and n.name == "depth_reference_price"))
reference.decorator_list = []
reference_namespace = dict(Any=Any, Decimal=Decimal, math=math)
exec(compile(ast.Module([number, reference], type_ignores=[]), "<actual CTP raw reference parser>", "exec"), reference_namespace)


class DepthHealthTests(unittest.TestCase):
    def test_raw_reference_uses_actual_book_and_rejects_missing_crossed_or_sentinel_prices(self):
        reference_price = reference_namespace["depth_reference_price"]
        self.assertEqual(reference_price({"BidPrice1": 100, "AskPrice1": 104}), "102.0")
        for data in ({}, {"BidPrice1": 104, "AskPrice1": 100},
                {"BidPrice1": 100, "AskPrice1": 1.7976931348623157e308},
                {"BidPrice1": float("nan"), "AskPrice1": 100}):
            self.assertIsNone(reference_price(data))

    def setUp(self):
        self.now = 0
        self.monitor = health.StreamHealthMonitor("ctp", monotonic_ns=lambda: self.now, wall_time_ns=lambda: self.now)
        self.monitor.on_connected()
        self.received = []
        self.handler = Handler()
        self.handler.config = SimpleNamespace(future_buffer_ns=0)
        self.handler._stop_event = threading.Event()
        self.handler._health_monitor = self.monitor
        self.handler._symbol_to_instrument = {"IM2610": "IM2610.CFFEX"}
        self.handler.get_instrument_meta = lambda i: SimpleNamespace(exchange="CFFEX")
        self.handler._converter = SimpleNamespace(convert=lambda *args: (),
            _timestamp_ns=lambda data, exchange: data["ts"], depth_reference_price=lambda data: "3000")
        self.handler._lock = threading.RLock()
        self.handler._depth_observations = {}
        self.handler._depth_handlers = []
        self.handler.enqueue_event = self.received.append
        self.handler._event_is_subscribed = lambda *args: True

    def depth(self, stamp=100):
        with patch.object(time, "monotonic_ns", return_value=self.now):
            self.handler.on_depth_market_data({"InstrumentID": "IM2610", "TradingDay": "20261012", "ts": stamp})

    def test_unchanged_depth_keeps_health_ready_without_derived_ticks_and_still_times_out(self):
        for seconds in range(0, 50, 5):
            self.now = seconds * 1_000_000_000
            self.depth()
            self.assertEqual(self.monitor.check_timeout().state, health.MarketHealthState.READY)
        self.assertEqual(self.received, [])
        self.assertEqual(self.handler._depth_observations["IM2610.CFFEX"].trading_day, "20261012")
        self.now += 31_000_000_000
        self.assertEqual(self.monitor.check_timeout().reason, health.MarketHealthReason.STALE)

    def test_raw_timestamp_rollback_remains_sticky(self):
        self.depth(100)
        self.depth(99)
        self.assertEqual(self.monitor.snapshot.reason, health.MarketHealthReason.TIMESTAMP_ROLLBACK)
        self.assertTrue(self.handler._depth_observations["IM2610.CFFEX"].timestamp_regressed)
        self.depth(101)
        self.assertEqual(self.monitor.snapshot.state, health.MarketHealthState.DEGRADED)
        self.assertEqual(self.received, [])

    def test_source_receipt_does_not_clear_dispatch_failure(self):
        self.monitor.on_dispatch_error(RuntimeError("broken handler"))
        self.depth()
        self.assertEqual(self.monitor.snapshot.reason, health.MarketHealthReason.DISPATCH_ERROR)

    def test_unregistered_or_invalid_depth_cannot_make_source_ready(self):
        with self.assertLogs(__name__, level="WARNING"):
            self.handler.on_depth_market_data({"InstrumentID": "UNKNOWN"})
        self.handler._converter.convert = lambda *args: (_ for _ in ()).throw(ValueError("invalid depth"))
        with self.assertLogs(__name__, level="ERROR"):
            self.depth()
        self.assertEqual(self.monitor.snapshot.reason, health.MarketHealthReason.WAITING_FOR_FIRST_EVENT)
        self.assertEqual(self.handler._depth_observations, {})

    def test_small_future_depth_waits_before_publication_and_retains_raw_timestamp_and_receipt(self):
        wall = [1_000_000_000]
        handler = self.handler
        class ClockStop:
            def is_set(self): return False
            def wait(inner, seconds):
                self.assertFalse(handler._depth_observations)
                self.assertFalse(self.received)
                self.now += int(seconds * 1e9)
                wall[0] += int(seconds * 1e9)
                return False
        handler._stop_event = ClockStop()
        handler.config.future_buffer_ns = 100_000_000
        raw = SimpleNamespace(ts_event=wall[0] + 10_000_000)
        handler._converter.convert = lambda *args: (raw,)
        with patch.object(time, "time_ns", side_effect=lambda: wall[0]), self.assertNoLogs(__name__, level="ERROR"):
            self.depth(raw.ts_event)
        self.assertEqual(self.received, [raw])
        observed = handler._depth_observations["IM2610.CFFEX"]
        self.assertEqual((observed.ts_event, observed.received_monotonic_ns), (raw.ts_event, 0))
        self.assertEqual(wall[0], raw.ts_event)

    def test_large_future_depth_closes_health_without_publication(self):
        self.handler.config.future_buffer_ns = 100_000_000
        with patch.object(time, "time_ns", return_value=1_000_000_000), self.assertLogs(__name__, level="ERROR"):
            self.depth(1_101_000_000)
        self.assertEqual(self.monitor.snapshot.reason, health.MarketHealthReason.DISPATCH_ERROR)
        self.assertFalse(self.handler._depth_observations)
        self.assertFalse(self.received)

    def test_future_wait_has_monotonic_deadline_and_obeys_shutdown(self):
        mono = [0]
        class Stop:
            def is_set(self): return False
            def wait(self, seconds):
                mono[0] += int(seconds * 1e9)
                return False
        with self.assertRaises(time_gate.CtpFutureEventError):
            time_gate.wait_for_event_time(10_000_000, 100_000_000, Stop(), clock_ns=lambda: 0,
                monotonic_ns=lambda: mono[0])
        self.assertEqual(mono[0], 100_000_000)
        event = threading.Event(); event.set()
        self.assertFalse(time_gate.wait_for_event_time(10, 100, event, clock_ns=lambda: 0))
        for value in (-1, True, 100_000_001):
            with self.assertRaises(ValueError):
                time_gate.wait_for_event_time(10, value, event)


if __name__ == "__main__":
    unittest.main()
