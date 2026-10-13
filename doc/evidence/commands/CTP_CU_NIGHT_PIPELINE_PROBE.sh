#!/usr/bin/env bash
# MD-only native depth -> Quote/Trade -> minute Bar diagnostic; never connects TD.
(
  cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
  export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
  export CTP_BROKER_ID=9999
  export CTP_PRODUCTION_MODE=true
  export CTP_SYMBOL="${1:-cu2612}"
  export CTP_PIPELINE_SAMPLE_SECONDS="${2:-600}"

  python -u - <<'PY'
import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from bomber.framework.market.basic.base import DataType, InstrumentId, InstrumentMeta
from bomber.framework.market.stream.ctp import CtpLiveDataFeed, CtpMdConfig
from bomber.framework.market.stream.aggregation import TradeTickBarFeed

load_dotenv(Path.cwd() / ".env")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
symbol = os.environ["CTP_SYMBOL"].strip().lower()
if re.fullmatch(r"cu\d{4}", symbol) is None:
    raise SystemExit("本探针元信息只用于 CU 期货；AG/RB 需要各自合约参数。")
seconds = float(os.environ["CTP_PIPELINE_SAMPLE_SECONDS"])
if not 60 <= seconds <= 3600:
    raise SystemExit("采样时长须在 60—3600 秒。")
zone = ZoneInfo("Asia/Shanghai")
now = datetime.now(zone)
if now.hour >= 21:
    end = (now + timedelta(days=1)).replace(hour=1, minute=0, second=0, microsecond=0)
elif now.hour < 1:
    end = now.replace(hour=1, minute=0, second=0, microsecond=0)
else:
    raise SystemExit("请在 CU 夜盘连续竞价期间启动，建议北京时间 21:05 后。")
if now + timedelta(seconds=seconds + 60) >= end:
    raise SystemExit("准备与采样预算可能跨越 CU 夜盘收盘；本次不连接。")

def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"缺少配置：{name}")
    return value

lock = threading.Lock()
stats = {"raw_count": 0, "first_raw": None, "last_raw": None,
         "max_raw_gap_seconds": 0.0, "max_raw_callback_seconds": 0.0,
         "quote_count": 0, "trade_count": 0, "bar_count": 0,
         "last_raw_source": None, "last_bar_event_ns": None}

class MonitoredFeed(CtpLiveDataFeed):
    def on_depth_market_data(self, data):
        started = time.monotonic()
        if str(data.get("InstrumentID", "")) == symbol:
            with lock:
                if stats["last_raw"] is not None:
                    stats["max_raw_gap_seconds"] = max(stats["max_raw_gap_seconds"], started - stats["last_raw"])
                if stats["first_raw"] is None:
                    stats["first_raw"] = started
                stats["last_raw"] = started
                stats["raw_count"] += 1
                stats["last_raw_source"] = {k: data.get(k) for k in
                    ("TradingDay", "ActionDay", "UpdateTime", "UpdateMillisec", "Volume")}
        super().on_depth_market_data(data)
        with lock:
            stats["max_raw_callback_seconds"] = max(stats["max_raw_callback_seconds"], time.monotonic() - started)

instrument = InstrumentId.from_str(f"{symbol}.SHFE")
feed = MonitoredFeed(CtpMdConfig(
    front=required("CTP_MD_ADDRESS"), broker_id=required("CTP_BROKER_ID"),
    user_id=required("CTP_ACCOUNT_ID"), password=required("CTP_PASSWORD"),
    production_mode=True, flow_path=f"/tmp/bomber-cu-md-probe-{os.getpid()}-{time.time_ns()}"))
bars = TradeTickBarFeed("CU_NIGHT_1M", feed)
bars.register_instrument(InstrumentMeta(instrument, price_precision=0, size_precision=0,
    price_increment=Decimal("10"), multiplier=Decimal("5"), exchange="SHFE"))
bars.subscribe(instrument, DataType.BAR, "1-MINUTE")
feed.subscribe(instrument, DataType.QUOTE_TICK)

def on_quote(tick):
    with lock:
        stats["quote_count"] += 1

def on_trade(tick):
    with lock:
        stats["trade_count"] += 1

def on_bar(bar):
    with lock:
        stats["bar_count"] += 1
        stats["last_bar_event_ns"] = bar.ts_event

feed.register_quote_tick_handler(on_quote)
feed.register_trade_tick_handler(on_trade)
bars.register_bar_handler(on_bar)
stop_requested = False

def request_stop(signum, frame):
    global stop_requested
    stop_requested = True
    print("已请求结束只读采样，等待当前操作结束并断开 MD。", flush=True)

signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)

def snapshot():
    clock, wall = time.monotonic(), time.time_ns()
    observation = feed.depth_observations.get(instrument)
    health = feed.health_snapshot
    with lock:
        result = dict(stats)
    result.pop("first_raw", None)
    last = result.pop("last_raw")
    result["raw_receive_age_seconds"] = None if last is None else max(0.0, clock - last)
    if result["raw_receive_age_seconds"] is not None:
        result["max_raw_gap_seconds"] = max(result["max_raw_gap_seconds"], result["raw_receive_age_seconds"])
    result["valid_depth_receive_age_seconds"] = None if observation is None else (
        time.monotonic_ns() - observation.received_monotonic_ns) / 1e9
    result["valid_depth_event_age_seconds"] = None if observation is None else (wall - observation.ts_event) / 1e9
    result["valid_depth_trading_day"] = None if observation is None else observation.trading_day
    result["health"] = {"state": str(getattr(health.state, "value", health.state)),
        "reason": str(getattr(health.reason, "value", health.reason)), "detail": health.detail,
        **{k: getattr(health, k) for k in
            ("queue_overflows", "timestamp_rollbacks", "dispatch_errors", "stream_interruptions")}}
    # Read-only diagnostic of the current StreamDataFeed implementation.
    result["queue_depth"] = feed._event_queue.qsize()
    return result

output = Path("tests/results/ctp-cu-night-probe") / f"sample-{time.time_ns()}"
output.mkdir(parents=True, exist_ok=False)
started = None
cpu_start = None
queue_peak = 0
bad_samples = 0
max_loop_gap = 0.0
health_changes = []
last_health = None
failure = None
print(f"只读夜盘链路采样：{symbol}.SHFE，{seconds:g} 秒；不连接 TD，不报单。", flush=True)
try:
    bars.connect()
    if not feed.wait_until_ready(30):
        raise TimeoutError("30 秒内未取得 MD 登录及订阅提交就绪。")
    first_deadline = time.monotonic() + 30
    while time.monotonic() < first_deadline and not stop_requested:
        if feed.depth_observations.get(instrument) is not None:
            break
        time.sleep(0.2)
    if feed.depth_observations.get(instrument) is None:
        raise TimeoutError("未收到可成功转换的目标合约原始深度。")
    started, cpu_start = time.monotonic(), time.process_time()
    previous = started
    next_log = started
    while time.monotonic() < started + seconds and not stop_requested:
        clock = time.monotonic()
        max_loop_gap = max(max_loop_gap, clock - previous)
        previous = clock
        current = snapshot()
        queue_peak = max(queue_peak, current["queue_depth"])
        if current["health"]["state"] != "ready":
            bad_samples += 1
        signature = (current["health"]["state"], current["health"]["reason"])
        if signature != last_health:
            health_changes.append({"at": datetime.now(zone).isoformat(), "health": current["health"]})
            health_changes = health_changes[-32:]
            last_health = signature
        if clock >= next_log:
            print("CU_PIPELINE_PROGRESS:", json.dumps(current, ensure_ascii=False), flush=True)
            next_log = clock + 30
        time.sleep(0.2)
except Exception as exc:
    failure = str(exc)
    raise
finally:
    final = snapshot()  # Capture before disconnect clears raw observations.
    elapsed = 0.0 if started is None else time.monotonic() - started
    cpu_percent = None if cpu_start is None or elapsed <= 0 else 100 * (time.process_time() - cpu_start) / elapsed
    cleanup_errors = []
    try:
        bars.disconnect()
    except Exception as exc:
        cleanup_errors.append(str(exc))
    final.update(symbol=symbol, sample_seconds=elapsed, requested_seconds=seconds,
        stop_requested=stop_requested, failure=failure, cleanup_errors=cleanup_errors, queue_peak_sampled=queue_peak,
        nonready_samples=bad_samples, max_control_loop_gap_seconds=max_loop_gap,
        process_cpu_percent=cpu_percent, health_changes=health_changes,
        scope="md_only_raw_standard_ticks_and_minute_bars", connects_td=False, orders=False)
    path = output / "summary.json"
    path.write_text(json.dumps(final, ensure_ascii=False, indent=2) + "\n")
    print("CU_PIPELINE_RESULT:", json.dumps(final, ensure_ascii=False), flush=True)
    print("报告：", path, flush=True)
    if cleanup_errors:
        raise SystemExit("MD 清理失败，见 cleanup_errors；诊断报告已经保存。")
PY
)
