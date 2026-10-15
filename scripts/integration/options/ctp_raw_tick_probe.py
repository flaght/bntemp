"""直接订阅CTP行情回调，统计稀疏合约；只使用MD API，不连接TD。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts.integration.options.dolphin_tick_bar_probe import DEFAULT_KEYS, SH, TickBarProbe, emit


def error_code(error):
    return int((error or {}).get("ErrorID", 0) or 0)


class RawCtpProbe:
    def __init__(self, keys, output, credentials, stop):
        self.collector = TickBarProbe(keys, output)
        self.credentials = credentials
        self.stop = stop
        self.driver = None
        self.connected = threading.Event()
        self.logged_in = threading.Event()
        self.subscription_events = {key: threading.Event() for key in keys}
        self.errors = []
        self.lock = threading.Lock()
        self.subscriptions = {}
        self.closing = False

    def fail(self, reason):
        with self.lock:
            self.errors.append(reason)
        self.stop.set()

    def on_front_connected(self):
        self.connected.set()
        if self.closing or self.stop.is_set():
            return
        try:
            self.driver.login(*self.credentials)
        except Exception as exc:
            self.fail("login submission: " + type(exc).__name__)

    def on_front_disconnected(self, reason):
        if not self.closing:
            self.fail(f"MD disconnected: reason={reason}")

    def on_login_response(self, error):
        if error_code(error):
            self.fail(f"MD login failed: ErrorID={error_code(error)}")
        else:
            self.logged_in.set()

    def on_subscribe_response(self, data, error):
        key = str((data or {}).get("InstrumentID", "")).strip()
        if error_code(error):
            self.fail(f"MD subscribe failed: symbol={key} ErrorID={error_code(error)}")
        elif key in self.subscription_events:
            self.subscriptions[key] = "accepted"
            self.subscription_events[key].set()

    def on_unsubscribe_response(self, data, error):
        if error_code(error):
            self.fail(f"MD unsubscribe failed: ErrorID={error_code(error)}")

    def on_api_error(self, error):
        if error_code(error):
            self.fail(f"MD API error: ErrorID={error_code(error)}")

    def on_depth_market_data(self, data):
        try:
            # Preserve CTP fields; Code is an explicitly added lookup alias only.
            row = dict(data)
            row["Code"] = str(data.get("InstrumentID", "")).strip()
            self.collector.on_row("tick", "CTP_OnRtnDepthMarketData", row)
        except Exception as exc:
            self.fail("tick capture failed: " + type(exc).__name__)

    def wait_for(self, event, timeout, stage):
        deadline = time.monotonic() + timeout
        while not self.stop.is_set():
            if event.wait(min(.1, max(0, deadline - time.monotonic()))):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(stage + " timeout")
        raise InterruptedError(stage + " interrupted")


def env_bool(value):
    if value.lower() not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError("CTP_PRODUCTION_MODE须为true/false或1/0")
    return value.lower() in ("true", "1", "yes")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--keys", default=DEFAULT_KEYS)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--report-dir", type=Path, default=ROOT / "tests/results/option-vega-ctp-raw-probe")
    args = parser.parse_args(argv)
    keys = tuple(dict.fromkeys(k.strip() for k in args.keys.split(",") if k.strip()))
    if not keys or not 60 <= args.seconds <= 1800 or not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("须指定合约；采样60至1800秒，连接超时须在(0,120]秒")
    from dotenv import load_dotenv
    from bomber.framework.market.native.ctp.driver import create_native_driver
    load_dotenv(ROOT / ".env")
    required = ("CTP_MD_ADDRESS", "CTP_BROKER_ID", "CTP_ACCOUNT_ID", "CTP_PASSWORD")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError("缺少环境配置: " + ",".join(missing))
    production = env_bool(os.getenv("CTP_PRODUCTION_MODE", "false"))
    directory = args.report_dir / f"sample-{time.time_ns()}"
    directory.mkdir(parents=True)
    stop = threading.Event()
    handlers = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    driver, status, sample_started = None, "failed", None
    started = datetime.now(SH).isoformat()
    with (directory / "observations.jsonl").open("w", encoding="utf-8") as output:
        probe = RawCtpProbe(keys, output, tuple(os.environ[k] for k in required[1:]), stop)
        try:
            try:
                driver = create_native_driver(probe)
            except (RuntimeError, ImportError, OSError):
                probe.fail("CTP MD扩展不可用；检查bomber_ctp_md安装及动态库依赖，参考binding/README.md")
                raise InterruptedError("native driver unavailable") from None
            probe.driver = driver
            driver.start(os.environ["CTP_MD_ADDRESS"], directory / "md-flow", production)
            probe.wait_for(probe.connected, args.timeout, "connect")
            probe.wait_for(probe.logged_in, args.timeout, "login")
            for key in keys:
                if stop.is_set():
                    raise InterruptedError("subscribe interrupted")
                driver.subscribe(key)
                probe.wait_for(probe.subscription_events[key], args.timeout, "subscribe " + key)
            sample_started = datetime.now(SH).isoformat()
            emit("CTP_RAW_START", {"report": str(directory), "keys": keys, "seconds": args.seconds,
                "sample_started_at": sample_started, "source": "MD API callback before feature/trade conversion"})
            deadline = time.monotonic() + args.seconds
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if stop.wait(min(10, remaining)):
                    break
                snap = probe.collector.snapshot()
                emit("CTP_RAW_COUNTS", {k: {field: value for field, value in v.items()
                    if not field.startswith("last_")} for k, v in snap["contracts"].items()})
            status = "interrupted" if stop.is_set() else "completed"
        except InterruptedError:
            status = "interrupted"
        except Exception as exc:
            probe.fail(type(exc).__name__ + ": " + str(exc) if isinstance(exc, TimeoutError) else type(exc).__name__)
        finally:
            probe.closing = True
            if driver is not None:
                try:
                    driver.stop()
                except Exception as exc:
                    probe.fail("cleanup: " + type(exc).__name__)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            if probe.errors:
                status = "failed"
            result = {"status": status, "errors": probe.errors, "started_at": started,
                "sample_started_at": sample_started, "ended_at": datetime.now(SH).isoformat(),
                "production_mode": production, "subscriptions": probe.subscriptions,
                "scope": "CTP MD only; no TD API; no orders", **probe.collector.snapshot(),
                "limitations": ["首次订阅可能收到旧的初始快照；须检查UpdateTime和TradingDay",
                    "接收时间是原生扩展队列转发到Python的时刻，不是C++入口硬件时间戳",
                    "采样包含订阅建立阶段；首末边界无更新时长不计入最大相邻tick间隔",
                    "与DDB比较须同一交易时段，最好同一行情前置；历史不同时间段不能逐条对比",
                    "行情账号/前置由CTP_MD_ADDRESS等环境配置决定，不能自动证明与DDB采集端相同",
                    "无回调不自动判故障；completed仅表示采样结束"]}
            (directory / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    emit("CTP_RAW_FINAL", {"status": status, "errors": probe.errors, "report": str(directory)})
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
