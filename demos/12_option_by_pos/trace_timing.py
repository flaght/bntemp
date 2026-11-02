"""诊断回放时间链路；包装原入口，不改变目标、行情或撮合参数。

python trace_timing.py --entry /path/run.py --output /tmp/old.jsonl -- 原入口参数
只记录主线程Python边界，不记录价格，不修改策略状态。
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
import json
from pathlib import Path
import runpy
import sys
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")
FUNCTIONS = {"on_bar", "onBatchBar", "_flush_bar_batch", "_on_send_target_vol", "_submit_target",
    "process_market_event", "enqueue", "_prepare", "_release_orders", "on_order_accepted", "on_order_filled"}


def role_of(code):
    path = code.co_filename.replace("\\", "/")
    if path.endswith("bomber_adapter/bridge.py"):
        return "old_bridge"
    if code.co_name == "onBatchBar" and path.endswith("strategy.py"):
        return "old_strategy"
    if path.endswith("12_option_by_pos/strategy.py"):
        return "new_strategy"
    if path.endswith("execution/simulation/backend.py"):
        return "new_backend"
    if path.endswith("execution/simulation/gateway.py"):
        return "new_gateway"
    return None


def beijing(value):
    return datetime.fromtimestamp(value / 1_000_000_000, ZONE).isoformat() if value is not None else None


def instrument_of(item):
    if item is None:
        return None
    bar_type = getattr(item, "bar_type", None)
    value = getattr(bar_type, "instrument_id", None) if bar_type is not None else getattr(item, "instrument_id", None)
    return str(value) if value is not None else None


class TimingTrace:
    def __init__(self, output, focus_day, instrument="IM2606", windows=None):
        self.output = Path(output).expanduser().resolve()
        self.instrument = instrument.removesuffix(".CFFEX")
        self.qualified = self.instrument + ".CFFEX"
        self.windows = []
        for window in windows or ("09:58-10:06", "10:58-11:06", "13:28-13:36"):
            left, right = window.split("-")
            stamps = [int(datetime.combine(focus_day, time.fromisoformat(clock), ZONE).timestamp()) * 1_000_000_000
                      for clock in (left, right)]
            if stamps[0] > stamps[1]:
                raise ValueError("追踪窗口结束时间不能早于开始时间")
            self.windows.append(tuple(stamps))
        self.last_clock_ns = None
        self.rows = 0
        self.roles = {}

    def __enter__(self):
        self.previous = sys.getprofile()
        if self.previous is not None:
            raise RuntimeError("已有Python profiler，请在独立诊断进程运行")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.output.open("x", encoding="utf-8")
        self._write(dict(stage="trace_start", instrument=self.qualified,
            windows=[[beijing(left), beijing(right)] for left, right in self.windows], argv=sys.argv,
            note="仅读取Python回调边界，不含价格；clock为模拟时间，非系统当前时间"))
        sys.setprofile(self._profile)
        return self

    def __exit__(self, exc_type, exc, tb):
        sys.setprofile(self.previous)
        self._write(dict(stage="trace_end", records=self.rows, error=str(exc) if exc else None))
        self.stream.close()

    def _write(self, item):
        self.stream.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        self.stream.flush()

    def _profile(self, frame, event, argument):
        if event not in {"call", "return"} or frame.f_code.co_name not in FUNCTIONS:
            return
        code = frame.f_code
        if code not in self.roles:
            self.roles[code] = role_of(code)
        role = self.roles[code]
        if role is None:
            return
        local = frame.f_locals
        if code.co_name == "onBatchBar" and not local.get("isLast", False):
            return
        host = local.get("self")
        bar = local.get("bar", local.get("event"))
        intent = local.get("intent", local.get("order"))
        subject = instrument_of(intent) or instrument_of(bar) or local.get("code") or local.get("instrument_id")
        if code.co_name not in {"on_bar", "process_market_event", "onBatchBar", "_flush_bar_batch"}:
            if subject is not None and str(subject).removesuffix(".CFFEX") != self.instrument:
                return
        clock_ns = None
        try:
            clock = getattr(host, "clock", None)
            if clock is not None:
                clock_ns = int(clock.timestamp_ns())
        except (AttributeError, RuntimeError):
            pass
        if code.co_name == "process_market_event" and event == "call":
            self.last_clock_ns = int(getattr(bar, "ts_init", getattr(bar, "ts_event", 0)))
        if clock_ns:
            self.last_clock_ns = clock_ns
        current = clock_ns or self.last_clock_ns
        if current is None or not any(left <= current <= right for left, right in self.windows):
            return
        row = dict(stage=code.co_name, boundary=event, role=role, source=code.co_filename,
            source_line=frame.f_lineno, clock_ns=current, clock_bj=beijing(current),
            clock_from_host=bool(clock_ns))
        if subject is not None:
            row["instrument"] = str(subject)
        if bar is not None:
            row.update(market_instrument=instrument_of(bar), bar_type=str(getattr(bar, "bar_type", "")))
            for name in ("ts_event", "ts_init"):
                value = getattr(bar, name, None)
                if value is not None:
                    row[name] = int(value)
                    row[name + "_bj"] = beijing(int(value))
            if code.co_name.startswith("on_order"):
                row["client_order_id"] = str(getattr(bar, "client_order_id", ""))
        if role == "old_strategy":
            row["signal_ns"] = int(local["ts"]) * 1000  # 旧接口为微秒。
        elif code.co_name == "_on_send_target_vol":
            row.update(signal_ns=int(local["timestamp"]) * 1000, target=local.get("vol"))
        elif role == "new_strategy" and bar is not None:
            row["signal_ns"] = int(bar.ts_event)
            row["target"] = getattr(host, "goals", {}).get(self.instrument)
        metadata = getattr(intent, "metadata", {}) or {}
        if metadata.get("request_ts") is not None:
            row["request_ns"] = int(metadata["request_ts"])
        for name in ("after_ts", "ts_event", "target_vol", "current_qty", "delta"):
            if name in local:
                row[name] = local[name]
        native_order = local.get("order", local.get("native_order"))
        if getattr(native_order, "client_order_id", None) is not None:
            row["client_order_id"] = str(native_order.client_order_id)
            if getattr(native_order, "ts_init", None) is not None:
                row["order_init_ns"] = int(native_order.ts_init)
        if role == "old_bridge":
            row["batch_ns"] = getattr(host, "_batch_ts", None)
            row["last_focus_bar_ns"] = getattr(host, "_last_bar_ts", {}).get(self.qualified)
            batch = getattr(host, "_batch_bars", {}).get(row["batch_ns"], ())
            row["batch_instruments"] = [symbol for symbol, _ in batch]
            if code.co_name == "_flush_bar_batch" and frame.f_back is not None:
                trigger = frame.f_back.f_locals.get("bar")
                row["trigger_instrument"] = instrument_of(trigger)
                row["trigger_event_ns"] = getattr(trigger, "ts_event", None)
        if role == "new_gateway":
            row["pending"] = [dict(client_order_id=str(item.native_order.client_order_id),
                after_ns=item.after_ts, last_market_ns=item.last_market_ts, remaining_bars=item.remaining_bars)
                for item in getattr(host, "_pending", ()) if str(item.intent.instrument_id) == self.qualified]
        for name in ("signal_ns", "request_ns", "batch_ns", "last_focus_bar_ns", "order_init_ns"):
            if row.get(name) is not None:
                row[name + "_bj"] = beijing(int(row[name]))
        self._write(row)
        self.rows += 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", type=Path, required=True, help="原来运行的新版或旧版入口")
    parser.add_argument("--output", type=Path, required=True, help="新JSONL文件，不覆盖已有日志")
    parser.add_argument("--focus-day", type=date.fromisoformat, default=date(2026, 5, 29))
    parser.add_argument("--focus-instrument", default="IM2606")
    parser.add_argument("--window", action="append", help="北京时间HH:MM-HH:MM，可重复")
    parser.add_argument("entry_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    entry = args.entry.expanduser().resolve()
    if not entry.is_file():
        parser.error(f"入口不存在: {entry}")
    forwarded = args.entry_args[1:] if args.entry_args[:1] == ["--"] else args.entry_args
    original_argv = sys.argv
    original_path = list(sys.path)
    try:
        sys.argv = [str(entry), *forwarded]
        sys.path.insert(0, str(entry.parent))
        with TimingTrace(args.output, args.focus_day, args.focus_instrument, args.window) as trace:
            runpy.run_path(str(entry), run_name="__main__")
        print(f"时间追踪日志: {trace.output}，记录{trace.rows}条")
    finally:
        sys.argv = original_argv
        sys.path[:] = original_path


if __name__ == "__main__":
    main()
