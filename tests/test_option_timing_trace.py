"""使用旧版真实批次代码验证信号提交延迟；不模拟原生成交或价格。"""
import ast
from collections import defaultdict
from datetime import date
import hashlib
import importlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

import pandas as pd
from tests.test_simulation_market_timing import Bar, EngineHost, components

trace = importlib.import_module("demos.12_option_by_pos.trace_timing")
ROOT = Path(__file__).resolve().parents[1]


def ns(text):
    return int(pd.Timestamp(text, tz="Asia/Shanghai").value)


def methods(relative, class_name, selected, namespace):
    path = ROOT / relative
    cls = next(item for item in ast.parse(path.read_text()).body if isinstance(item, ast.ClassDef) and item.name == class_name)
    body = [item for item in cls.body if isinstance(item, ast.FunctionDef) and item.name in selected]
    assert len(body) == len(selected)
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    return {name: namespace[name] for name in selected}


def old_host():
    bridge = methods("3rd/strategies/bomber_adapter/bridge.py", "BomberBridge",
        {"on_bar", "_flush_bar_batch", "_on_send_target_vol", "_submit_target"},
        {"hashlib": hashlib, "datetime": trace.datetime, "Bar": object, "BarData": NS,
         "is_option": lambda code: code.startswith("MO"), "OrderSide": NS(BUY="BUY", SELL="SELL")})
    strategy = methods("3rd/strategies/kd_option_by_pos/strategy.py", "CtaByPosStrategy",
        {"onBatchBar"}, {"pd": pd, "rp": NS(BarData=object)})

    class Host:
        def __init__(self):
            self.now = 0
            self.clock = NS(timestamp_ns=lambda: self.now)
            self._fill_occurred = False
            self._tz = trace.ZONE
            self._current_day = None
            self._batch_ts = None
            self._batch_bars = defaultdict(list)
            self._bar_sequences, self._last_bar_ts, self._last_bar_close = {}, {}, {}
            self._bar_code_cache, self._bar_extra_fields = {}, {}
            self._pending_targets, self._pending_position_adjustments = {}, {}
            self._last_flush_ts = None
            self.orders = []
            self.cache = NS(instrument=lambda key: NS(make_qty=lambda qty: qty))
            self.order_factory = NS(market=lambda **kw: NS(client_order_id="DEMO", ts_init=self.now, **kw))
            self.log = NS(warning=lambda *a: None)
        def _record_actual_position_snapshot(self, stamp): pass
        def _submit_due_orders(self, *args): pass
        def _roll_trading_day(self, stamp):
            self._current_day = trace.datetime.fromtimestamp(stamp / 1e9, trace.ZONE).strftime("%Y-%m-%d")
        def _bar_spec_to_time_unit(self, *args): return "MINUTE"
        def _resolve_inst_id(self, code): return code + ".CFFEX"
        def get_effective_position(self, code): return 9
        def submit_order(self, order): self.orders.append(order)
        def _flush_targets(self, **kwargs):
            pending, self._pending_targets = self._pending_targets, {}
            for code, (target, stamp) in pending.items(): self._submit_target(code, target)
    for name, function in bridge.items(): setattr(Host, name, function)
    user_class = type("User", (), {"onBar": lambda *args: None, **strategy})
    host = Host()
    user = user_class()
    user._time_slots = {"10:00": []}
    user._positions_map = {pd.Timestamp("2026-05-29 10:00"): {"IM2606": 7}}
    user._sent_slots = {}
    user.sendTargetVol = host._on_send_target_vol
    host._user_stg = user
    return host


def bar(symbol, stamp):
    return NS(bar_type=NS(instrument_id=NS(symbol=NS(value=symbol), __str__=lambda: symbol),
                         spec=NS(aggregation="MINUTE", step=1 if symbol.startswith("IM") else 5)),
              ts_event=ns(stamp), ts_init=ns(stamp), close=1, open=1, high=1, low=1, volume=1)


class TimingTraceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def run_stream(self, stream):
        host = old_host()
        # 实际旧代码用str(instrument_id)作键，使用有真实字符串行为的轻量身份。
        class Id(str):
            @property
            def symbol(self): return NS(value=self.removesuffix(".CFFEX"))
        output = self.root / "trace.jsonl"
        with trace.TimingTrace(output, date(2026, 5, 29)):
            for symbol, stamp in stream:
                item = bar(symbol, stamp)
                item.bar_type.instrument_id = Id(symbol + ".CFFEX")
                host.now = item.ts_event
                host.on_bar(item)
        rows = [json.loads(line) for line in output.read_text().splitlines()]
        return host, rows

    def test_next_m1_timestamp_flushes_previous_signal_at_1001(self):
        host, rows = self.run_stream([("IM2606", "2026-05-29 10:00"), ("IM2606", "2026-05-29 10:01")])
        self.assertEqual(len(host.orders), 1)
        self.assertEqual(host.orders[0].ts_init, ns("2026-05-29 10:01"))
        signal = next(row for row in rows if row.get("stage") == "onBatchBar" and row["boundary"] == "call")
        self.assertEqual(signal["signal_ns"], ns("2026-05-29 10:00"))
        self.assertEqual(signal["clock_ns"], ns("2026-05-29 10:01"))

    def test_missing_m1_with_m5_clock_delays_signal_submission_until_1005(self):
        host, rows = self.run_stream([("IM2606", "2026-05-28 15:00"),
            ("MO2606-C-8500", "2026-05-29 10:00"), ("MO2606-C-8500", "2026-05-29 10:05")])
        self.assertEqual(host.orders[0].ts_init, ns("2026-05-29 10:05"))
        flush = next(row for row in rows if row.get("stage") == "_flush_bar_batch"
            and row["boundary"] == "call" and row.get("batch_ns") == ns("2026-05-29 10:00"))
        self.assertEqual(flush["clock_ns"], ns("2026-05-29 10:05"))
        self.assertEqual(flush["trigger_instrument"], "MO2606-C-8500.CFFEX")
        self.assertEqual(flush["last_focus_bar_ns"], ns("2026-05-28 15:00"))
        submitted = [row for row in rows if row.get("stage") == "_submit_target" and row["boundary"] == "return"]
        self.assertEqual(submitted[0]["order_init_ns"], ns("2026-05-29 10:05"))
        self.assertFalse(any("price" in key for row in rows for key in row))

    def test_profile_is_restored_when_wrapped_program_fails(self):
        previous = sys.getprofile()
        with self.assertRaisesRegex(RuntimeError, "entry failed"):
            with trace.TimingTrace(self.root / "failed.jsonl", date(2026, 5, 29)):
                raise RuntimeError("entry failed")
        self.assertIs(sys.getprofile(), previous)

    def test_new_gateway_trace_records_own_bar_gate(self):
        with components() as sdk:
            engine = EngineHost()
            backend = sdk.backend.NautilusSimExecutionBackend("sim", engine=engine,
                execution_timing="next_market")
            output = self.root / "new.jsonl"
            with trace.TimingTrace(output, date(2026, 5, 29)):
                backend.process_market_event(Bar("IM2606.CFFEX", ns("2026-05-29 10:00")))
                backend.submit_order(sdk.contracts.OrderIntent(strategy_id="cta", backend_id="sim",
                    instrument_id="IM2606.CFFEX", side="SELL", quantity=2,
                    metadata={"request_ts": ns("2026-05-29 10:00")}))
                backend.process_market_event(Bar("MO2606-C-8500.CFFEX", ns("2026-05-29 10:01")))
                self.assertEqual(engine.gateway.submissions, [])
                backend.process_market_event(Bar("IM2606.CFFEX", ns("2026-05-29 10:01")))
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            prepared = next(row for row in rows if row.get("stage") == "_prepare" and row["boundary"] == "return")
            self.assertEqual(prepared["pending"][0]["after_ns"], ns("2026-05-29 10:00"))
            self.assertEqual(prepared["pending"][0]["remaining_bars"], 1)
            self.assertEqual(engine.gateway.submissions[0][0], ns("2026-05-29 10:01"))

    def test_wrapper_forwards_arguments_and_restores_process_context(self):
        entry = self.root / "entry.py"
        observed = self.root / "argv.json"
        entry.write_text("import sys, json\nfrom pathlib import Path\n"
            "Path(sys.argv[1]).write_text(json.dumps(sys.argv))\n")
        before_argv, before_path = sys.argv, list(sys.path)
        trace.main(["--entry", str(entry), "--output", str(self.root / "wrapped.jsonl"),
                    "--", str(observed), "--start-day", "2026-01-01"])
        self.assertEqual(json.loads(observed.read_text()),
            [str(entry.resolve()), str(observed), "--start-day", "2026-01-01"])
        self.assertIs(sys.argv, before_argv)
        self.assertEqual(sys.path, before_path)

    def test_existing_trace_is_not_overwritten(self):
        output = self.root / "existing.jsonl"
        output.write_text("keep")
        with self.assertRaises(FileExistsError):
            with trace.TimingTrace(output, date(2026, 5, 29)): pass
        self.assertEqual(output.read_text(), "keep")


if __name__ == "__main__": unittest.main()
