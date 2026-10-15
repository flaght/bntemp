"""只读对照CTP标准化tick流与分钟流；不连接CTP、不运行策略、不下单。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SH = ZoneInfo("Asia/Shanghai")
DEFAULT_KEYS = "MO2610-C-8600,MO2610-C-8500,MO2610-C-8400,MO2610-C-8200,MO2610-C-8100,MO2611-C-7400,IM2611"
QUOTE_FIELDS = ("BidPrice1", "BidVolume1", "AskPrice1", "AskVolume1")


def plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def emit(label, value):
    print(label + ": " + json.dumps(value, ensure_ascii=False, default=str), flush=True)


class TickBarProbe:
    def __init__(self, keys, output):
        self.lock = threading.Lock()
        self.output = output
        self.state = {key: {"tick_rows": 0, "bar_rows": 0,
            "quote_changes_without_volume_change": 0, "volume_increases": 0,
            "max_tick_receipt_gap_seconds": 0, "last_tick": None, "last_bar": None}
            for key in keys}
        self.total = {"tick": 0, "bar": 0}
        self.columns = {}

    def on_row(self, kind, table, row):
        received_ns = time.time_ns()
        key = str(row.get("Code", "")).strip()
        with self.lock:
            self.total[kind] += 1
            state = self.state.get(key)
            if state is None:
                return
            observation = {"kind": kind, "table": table, "received_ns": received_ns,
                "received_at": datetime.fromtimestamp(received_ns / 1e9, SH).isoformat(),
                "row": plain(dict(row))}
            if kind == "tick":
                previous = state["last_tick"]
                state["tick_rows"] += 1
                if previous is not None:
                    gap = (received_ns - previous["received_ns"]) / 1e9
                    state["max_tick_receipt_gap_seconds"] = max(gap, state["max_tick_receipt_gap_seconds"])
                    before, after = previous["row"], observation["row"]
                    old_volume, new_volume = before.get("Volume"), after.get("Volume")
                    if isinstance(old_volume, (int, float)) and isinstance(new_volume, (int, float)):
                        if new_volume > old_volume:
                            state["volume_increases"] += 1
                        elif new_volume == old_volume and all(f in before and f in after for f in QUOTE_FIELDS):
                            if any(before[f] != after[f] for f in QUOTE_FIELDS):
                                state["quote_changes_without_volume_change"] += 1
                state["last_tick"] = observation
            else:
                state["bar_rows"] += 1
                # Snapshot of latest observed tick at bar receipt; correlation is not proof of causation.
                observation["latest_tick_at_bar_receipt"] = state["last_tick"]
                state["last_bar"] = observation
                emit("DDB_TICK_BAR", observation)
            self.output.write(json.dumps(observation, ensure_ascii=False) + "\n")
            if kind == "bar":
                self.output.flush()

    def snapshot(self):
        with self.lock:
            self.output.flush()
            return plain({"total_received": self.total, "schema": self.columns, "contracts": self.state})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--keys", default=DEFAULT_KEYS)
    parser.add_argument("--tick-table", default="stream_ctp_cffex_std")
    parser.add_argument("--bar-table", default="stream_cffex_1min")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "tests/results/option-vega-tick-bar-probe")
    args = parser.parse_args(argv)
    keys = tuple(dict.fromkeys(k.strip() for k in args.keys.split(",") if k.strip()))
    if not keys or not 60 <= args.seconds <= 1800:
        parser.error("须指定合约；采样时长须为60至1800秒")
    for table in (args.tick_table, args.bar_table):
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) is None:
            parser.error("表名须为共享流表标识符")
    if args.tick_table == args.bar_table:
        parser.error("tick和Bar须为不同流表")
    from dotenv import load_dotenv
    from bomber.framework.market.native.dolphin.driver import create_native_driver, NativeDolphinSubscription
    load_dotenv(ROOT / ".env")
    for name in ("DDB_HOST", "DDB_USERNAME", "DDB_PASSWORD"):
        if not os.getenv(name):
            raise RuntimeError(f"缺少环境配置 {name}")
    directory = args.report_dir / f"sample-{time.time_ns()}"
    directory.mkdir(parents=True)
    stop = threading.Event()
    old_handlers = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    driver = None
    errors, status = [], "failed"
    started = datetime.now(SH).isoformat()
    with (directory / "observations.jsonl").open("w", encoding="utf-8") as output:
        probe = TickBarProbe(keys, output)
        try:
            driver = create_native_driver()
            driver.start(os.environ["DDB_HOST"], int(os.getenv("DDB_PORT", "8848")),
                os.environ["DDB_USERNAME"], os.environ["DDB_PASSWORD"], streaming_port=0)
            for kind, table in (("tick", args.tick_table), ("bar", args.bar_table)):
                if stop.is_set():
                    break
                driver.subscribe(NativeDolphinSubscription(table_name=table,
                    action_name=f"tick_bar_probe_{kind}_{os.getpid()}_{time.time_ns()}",
                    columns=(), offset=-1, required_columns=("Code",)),
                    lambda row, k=kind, t=table: probe.on_row(k, t, row))
                with probe.lock:
                    probe.columns[kind] = list(driver.resolved_columns[table])
                emit("DDB_PROBE_SCHEMA", {"kind": kind, "table": table, "columns": probe.columns[kind]})
            emit("DDB_PROBE_START", {"started_at": started, "keys": keys, "report": str(directory),
                "seconds": args.seconds, "note": "仅记录订阅后的新行；缺行不自动判定故障"})
            deadline = time.monotonic() + args.seconds
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if stop.wait(min(10, remaining)):
                    break
                snap = probe.snapshot()
                emit("DDB_PROBE_COUNTS", {key: {k: v for k, v in state.items() if not k.startswith("last_")}
                    for key, state in snap["contracts"].items()})
            status = "interrupted" if stop.is_set() else "completed"
        except Exception as exc:
            errors.append(str(exc))
        finally:
            if driver is not None:
                try:
                    driver.stop()
                except Exception as exc:
                    errors.append("cleanup: " + str(exc))
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            result = {"status": status, "errors": errors, "started_at": started,
                "ended_at": datetime.now(SH).isoformat(), "tick_table": args.tick_table,
                "bar_table": args.bar_table, **probe.snapshot(),
                "limitations": ["标准化tick流不等同于CTP入口原始回调，仍可能存在上游过滤",
                    "接收间隔不等于网络延迟；tick与Bar相邻到达不证明触发关系",
                    "未收到行不能区分无更新、未订阅或上游故障；须结合活跃对照及服务器配置",
                    "volume_increases统计相邻快照累计量增加次数，不是成交笔数；跨日重置不计增加",
                    "完成仅表示采样结束，不代表行情或策略验收通过"]}
            (directory / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    emit("DDB_PROBE_FINAL", {"status": status, "errors": errors, "report": str(directory)})
    return 0 if status == "completed" and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
