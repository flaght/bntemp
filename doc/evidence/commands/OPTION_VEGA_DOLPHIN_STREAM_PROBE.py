"""Read-only raw minute-push probe; no CTP, strategy, price conversion or orders."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SH = ZoneInfo("Asia/Shanghai")
TABLE_KEYS = {"stream_cffex_1min": ("MO2611-C-7400", "IM2611"),
              "stream_index_1min": ("000852",)}


def serializable(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class RawPushProbe:
    def __init__(self):
        self.lock = threading.Lock()
        self.tables = {name: {"rows_received": 0, "first_row": None,
                             "matched": {key: {"rows": 0, "last_observation": None, "observations": []}
                                         for key in keys}}
                       for name, keys in TABLE_KEYS.items()}

    def on_row(self, table, row):
        received_ns = time.time_ns()
        observation = {"table": table, "received_ns": received_ns,
            "received_at": datetime.fromtimestamp(received_ns / 1e9, SH).isoformat(),
            "row": serializable(dict(row)),
            "python_types": {key: type(value).__name__ +
                (f"[{value.dtype}]" if hasattr(value, "dtype") else "") for key, value in row.items()}}
        key = str(row.get("Code", "")).strip()
        with self.lock:
            state = self.tables[table]
            state["rows_received"] += 1
            first = state["first_row"] is None
            if first:
                state["first_row"] = observation
            matched = state["matched"].get(key)
            if matched is not None:
                matched["rows"] += 1
                matched["last_observation"] = observation
                matched["observations"].append(observation)
                del matched["observations"][:-20]
        if first:
            print("DDB_FIRST_ROW:", json.dumps(observation, ensure_ascii=False), flush=True)
        if matched is not None:
            print("DDB_BAR_ROW:", json.dumps(observation, ensure_ascii=False), flush=True)

    def snapshot(self):
        with self.lock:
            return serializable(self.tables)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args(argv)
    if not 60 <= args.seconds <= 600:
        parser.error("采样时长须为60至600秒")
    from dotenv import load_dotenv
    from bomber.framework.market.native.dolphin.driver import create_native_driver, NativeDolphinSubscription
    load_dotenv(ROOT / ".env")
    for key in ("DDB_HOST", "DDB_USERNAME", "DDB_PASSWORD"):
        if not os.getenv(key):
            raise RuntimeError(f"缺少环境配置 {key}")
    required_columns = ("minTime", "date", "Code", "open", "high", "low", "close", "volume")
    probe, stop = RawPushProbe(), threading.Event()
    report = ROOT / "tests/results/option-vega-dolphin-stream-probe" / f"sample-{time.time_ns()}"
    report.mkdir(parents=True)
    driver = None
    status, failure, cleanup_errors = "failed", None, []
    schema = {}
    started_at = ended_at = None
    old_handlers = {sig: signal.signal(sig, lambda *_: stop.set())
                    for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        driver = create_native_driver()
        driver.start(os.environ["DDB_HOST"], int(os.getenv("DDB_PORT", "8848")),
                     os.environ["DDB_USERNAME"], os.environ["DDB_PASSWORD"],
                     streaming_port=int(os.getenv("DDB_STREAMING_PORT", "0")))
        for table in TABLE_KEYS:
            if stop.is_set():
                raise InterruptedError("用户主动停止")
            driver.subscribe(NativeDolphinSubscription(table_name=table,
                action_name=f"vega-minute-probe-{os.getpid()}-{time.time_ns()}", columns=(),
                offset=-1, required_columns=required_columns),
                lambda row, name=table: probe.on_row(name, row))
            schema[table] = list(driver.resolved_columns[table])
            print("DDB_STREAM_SCHEMA:", json.dumps({"table": table, "columns": schema[table]}, ensure_ascii=False), flush=True)
        started_at = datetime.now(SH).isoformat()
        print(f"DDB_STREAM_PROBE_START: {started_at} seconds={args.seconds}; 只读推送，不运行策略", flush=True)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline and not stop.wait(min(.2, max(0, deadline - time.monotonic()))):
            pass
        ended_at = datetime.now(SH).isoformat()
        status = "interrupted" if stop.is_set() else "completed"
    except InterruptedError as exc:
        status, failure = "interrupted", str(exc)
    except Exception as exc:
        failure = str(exc)
    finally:
        if driver is not None:
            try:
                driver.stop()
            except Exception as exc:
                cleanup_errors.append(str(exc))
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        tables = probe.snapshot()
        missing = [key for state in tables.values() for key, row in state["matched"].items() if row["rows"] == 0]
        result = {"status": status, "failure": failure, "cleanup_errors": cleanup_errors,
            "scope": "raw_minute_push_only", "started_at": started_at, "ended_at": ended_at,
            "schema": schema, "tables": tables, "missing_monitored_keys": missing,
            "completed_means": "sampling_finished_not_completed_bar_semantics_or_strategy_accepted"}
        (report / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print("DDB_STREAM_PROBE_FINAL:", json.dumps({k: result[k] for k in
            ("status", "failure", "cleanup_errors", "missing_monitored_keys")}, ensure_ascii=False), flush=True)
        print("报告:", report / "summary.json", flush=True)
    return 0 if status == "completed" and not cleanup_errors and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
