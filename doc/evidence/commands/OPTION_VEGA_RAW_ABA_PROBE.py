"""MD-only full/reduced/full comparison on one connection; no trading recovery."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SH = ZoneInfo("Asia/Shanghai")


def require_window(day, seconds, *, now=None):
    now = now or datetime.now(SH)
    if now.date() != day or now.weekday() >= 5:
        raise RuntimeError("只允许指定的当日工作日，不使用昨日恢复文件")
    start = now.replace(hour=9 if now.hour < 12 else 13,
                        minute=30 if now.hour < 12 else 0, second=0, microsecond=0)
    end = now.replace(hour=11 if now.hour < 12 else 14,
                      minute=30 if now.hour < 12 else 57, second=0, microsecond=0)
    if not start <= now < end or now + timedelta(seconds=seconds) >= end:
        raise RuntimeError("准备及三段采样须全部处于连续竞价：09:30—11:30 / 13:00—14:57")
    return end


def event_age(source):
    if not source:
        return None
    try:
        stamp = datetime.strptime(str(source.get("ActionDay") or source["TradingDay"])
                                  + " " + str(source["UpdateTime"]), "%Y%m%d %H:%M:%S")
        stamp = stamp.replace(tzinfo=SH) + timedelta(milliseconds=int(source.get("UpdateMillisec") or 0))
        return round((datetime.now(SH) - stamp).total_seconds(), 3)
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


class RawComparison:
    def __init__(self, held, future, day):
        self.held, self.future, self.day = held, future, day.strftime("%Y%m%d")
        self.lock = threading.Lock()
        self.login_done = threading.Event()
        self.changed = threading.Event()
        self.stop_requested = threading.Event()
        self.driver = None
        self.failure = None
        self.operation = None
        self.pending = set()
        self.acked = set()
        self.phase = None
        self.rows = []
        self.transitions = []
        self.last_raw = None

    def fail(self, message):
        with self.lock:
            self.failure = self.failure or message
        self.changed.set()
        self.login_done.set()

    def on_front_connected(self):
        try:
            self.driver.login(os.environ["CTP_BROKER_ID"], os.environ["CTP_ACCOUNT_ID"],
                              os.environ["CTP_PASSWORD"])
        except Exception as exc:
            self.fail(f"登录请求失败: {exc}")

    def on_front_disconnected(self, reason):
        self.fail(f"前置断开 reason={reason}；单连接对比中止")

    def on_login_response(self, error):
        if int((error or {}).get("ErrorID", 0) or 0):
            self.fail(f"登录失败: {error}")
        self.login_done.set()

    def acknowledge(self, operation, data, error):
        if int((error or {}).get("ErrorID", 0) or 0):
            self.fail(f"{operation}失败: {data} {error}")
            return
        with self.lock:
            symbol = str((data or {}).get("InstrumentID", ""))
            if operation == self.operation and symbol in self.pending:
                self.acked.add(symbol)
                if self.pending <= self.acked:
                    self.changed.set()

    def on_subscribe_response(self, data, error):
        self.acknowledge("subscribe", data, error)

    def on_unsubscribe_response(self, data, error):
        self.acknowledge("unsubscribe", data, error)

    def on_api_error(self, error):
        if int((error or {}).get("ErrorID", 0) or 0):
            self.fail(f"MD API 错误: {error}")

    def on_depth_market_data(self, data):
        received = time.monotonic()
        with self.lock:
            self.last_raw = received
            phase = self.phase
            if phase is None or received > phase["stop"]:
                return
            symbol = str(data.get("InstrumentID", ""))
            if symbol not in phase["symbols"]:
                phase["unexpected_ticks"] += 1
                return
            for key in ("ALL", symbol):
                if key not in phase["streams"]:
                    continue
                row = phase["streams"][key]
                previous = phase["start"] if row["last"] is None else row["last"]
                row["max_gap"] = max(row["max_gap"], received - previous)
                row["last"] = received
                row["ticks"] += 1
                row["last_source"] = {k: data.get(k) for k in
                    ("InstrumentID", "TradingDay", "ActionDay", "UpdateTime", "UpdateMillisec")}
                if str(data.get("TradingDay", "")) != self.day:
                    row["wrong_trading_day_ticks"] += 1

    def check(self):
        if self.failure:
            raise RuntimeError(self.failure)
        if self.stop_requested.is_set():
            raise InterruptedError("用户主动停止")

    def wait(self, event, timeout):
        deadline = time.monotonic() + timeout
        while not event.wait(min(0.2, max(0, deadline - time.monotonic()))):
            self.check()
            if time.monotonic() >= deadline:
                raise TimeoutError("登录或订阅响应超时")
        self.check()

    def change(self, operation, symbols, timeout=30):
        if not symbols:
            return
        with self.lock:
            self.operation, self.pending, self.acked = operation, set(symbols), set()
            self.changed.clear()
        for symbol in sorted(symbols):
            self.check()
            getattr(self.driver, operation)(symbol)
        try:
            self.wait(self.changed, timeout)
        finally:
            with self.lock:
                row = {"operation": operation, "requested": len(symbols),
                       "accepted": len(self.acked), "missing": sorted(self.pending - self.acked)}
                self.transitions.append(row)
            print("ABA_SUBSCRIPTIONS:", json.dumps(row, ensure_ascii=False), flush=True)

    def snapshot(self, end=None):
        # Caller owns lock: retain exact leading and trailing silences, including zero ticks.
        p = self.phase
        end = min(time.monotonic() if end is None else end, p["stop"])
        streams = {}
        for key, row in p["streams"].items():
            tail = max(0, end - (p["start"] if row["last"] is None else row["last"]))
            streams[key] = {"ticks": row["ticks"],
                            "max_receive_gap_seconds": round(max(row["max_gap"], tail), 3),
                            "trailing_silence_seconds": round(tail, 3),
                            "last_source": row["last_source"],
                            "latest_event_age_seconds": event_age(row["last_source"]),
                            "wrong_trading_day_ticks": row["wrong_trading_day_ticks"]}
        return {"phase": p["name"], "started_at": p["started_at"],
                "ended_at": datetime.now(SH).isoformat(), "subscriptions": len(p["symbols"]),
                "sample_seconds": round(end - p["start"], 3),
                "unexpected_ticks": p["unexpected_ticks"], "streams": streams}

    def sample(self, name, symbols, seconds, end_of_session):
        require_window(datetime.now(SH).date(), seconds)
        start = time.monotonic()
        with self.lock:
            self.phase = {"name": name, "symbols": set(symbols), "start": start,
                          "stop": start + seconds, "started_at": datetime.now(SH).isoformat(),
                          "unexpected_ticks": 0,
                          "streams": {k: {"ticks": 0, "last": None, "max_gap": 0.0,
                                          "last_source": None, "wrong_trading_day_ticks": 0}
                                      for k in ("ALL", self.held, self.future)}}
        print(f"ABA_PHASE_START: {name} 合约={len(symbols)} 秒={seconds:g}", flush=True)
        progress_at = start + 30
        try:
            while time.monotonic() < start + seconds:
                self.check()
                if datetime.now(SH) >= end_of_session:
                    raise RuntimeError("到达连续竞价边界，停止采样")
                self.stop_requested.wait(0.2)
                if time.monotonic() >= progress_at:
                    with self.lock:
                        progress = self.snapshot()
                    print("ABA_PROGRESS:", json.dumps(progress, ensure_ascii=False), flush=True)
                    progress_at += 30
        finally:
            with self.lock:
                row = self.snapshot()
                self.rows.append(row)
                self.phase = None
            print("ABA_PHASE_RESULT:", json.dumps(row, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, help="今天的交易日 YYYYMMDD")
    parser.add_argument("--held", default="MO2611-C-7400", help="待监控期权，不读取或修改持仓")
    parser.add_argument("--phase-seconds", type=float, default=300)
    args = parser.parse_args()
    day = datetime.strptime(args.day, "%Y%m%d").date()
    if not math.isfinite(args.phase_seconds) or not 60 <= args.phase_seconds <= 600:
        parser.error("每段采样须为60至600秒")
    # Startup references + login/subscription and two transitions have a bounded reserve.
    end = require_window(day, args.phase_seconds * 3 + 300)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    for key in ("CTP_MD_ADDRESS", "CTP_BROKER_ID", "CTP_ACCOUNT_ID", "CTP_PASSWORD"):
        if not os.getenv(key):
            raise RuntimeError(f"缺少配置 {key}")
    from bomber.framework.dataprep.live_options import LiveOptionReferences
    from bomber.framework.dataprep.sources import DolphinDbReferenceConfig, ReferenceSourceFactory
    from bomber.framework.market.native.ctp.driver import create_native_driver

    source = ReferenceSourceFactory.create("dolphindb", DolphinDbReferenceConfig.from_env(read_timeout_seconds=30))
    try:
        source.open()
        refs = LiveOptionReferences(source, option_product="MO", future_product="IM", index_code="000852",
                                    valuation_day=day, max_refresh_seconds=120)
        snap, manifest = refs.snapshot(), refs.manifest()
        if args.held not in snap.options:
            raise RuntimeError("监控期权不在今日有效认购链中；未连接 MD")
        future = "IM" + snap.options[args.held].month[2:]
        full = set(snap.options) | set(snap.futures)
    finally:
        source.close()
    end = require_window(day, args.phase_seconds * 3 + 180)
    print("ABA_REFERENCE:", json.dumps(manifest, ensure_ascii=False), flush=True)
    print(f"监控 {args.held} / {future}，今日全链 {len(full)} 个；参考读取已关闭，采样期间无 DFS 轮询", flush=True)
    monitor = RawComparison(args.held, future, day)
    reduced = {args.held, future}
    status, failure, cleanup_errors = "failed", None, []
    report = ROOT / "tests/results/option-vega-raw-aba" / f"sample-{time.time_ns()}"
    report.mkdir(parents=True)
    driver = None
    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, lambda *_: monitor.stop_requested.set())
    try:
        driver = create_native_driver(monitor)
        monitor.driver = driver
        with tempfile.TemporaryDirectory(prefix="vega-raw-aba-") as flow:
            try:
                driver.start(os.environ["CTP_MD_ADDRESS"], Path(flow),
                             os.getenv("CTP_PRODUCTION_MODE", "true").lower() in {"1", "true", "yes"})
                monitor.wait(monitor.login_done, 60)
                monitor.change("subscribe", full)
                monitor.sample("FULL_1", full, args.phase_seconds, end)
                monitor.change("unsubscribe", full - reduced)
                monitor.sample("REDUCED", reduced, args.phase_seconds, end)
                monitor.change("subscribe", full - reduced)
                monitor.sample("FULL_2", full, args.phase_seconds, end)
                status = "completed"
            finally:
                try:
                    driver.stop()
                except Exception as exc:
                    cleanup_errors.append(str(exc))
    except InterruptedError as exc:
        status, failure = "interrupted", str(exc)
    except Exception as exc:
        failure = str(exc)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        result = {"status": status, "failure": failure, "cleanup_errors": cleanup_errors,
                  "scope": "raw_md_only", "trading_day": args.day, "held": args.held, "future": future,
                  "reference_manifest_at_start": manifest, "full_symbols": sorted(full),
                  "subscription_transitions": monitor.transitions, "phases": monitor.rows,
                  "completed_means": "sampling_finished_not_market_health_accepted"}
        (report / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print("ABA_FINAL:", json.dumps({k: result[k] for k in ("status", "failure", "cleanup_errors")}, ensure_ascii=False), flush=True)
        print("报告:", report / "summary.json", flush=True)
    return 0 if status == "completed" and not cleanup_errors else 1


if __name__ == "__main__":
    sys.exit(main())
