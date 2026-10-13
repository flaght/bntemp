#!/usr/bin/env bash
# MD-only sampling of the held contract. Does not connect TD or submit orders.
(
  cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
  export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
  export CTP_BROKER_ID=9999
  export CTP_PRODUCTION_MODE=true
  export CTP_SYMBOL=MO2611-C-7400
  export CTP_CONNECT_TIMEOUT=30
  export CTP_MARKET_TIMEOUT=60
  export CTP_MARKET_SAMPLE_SECONDS=120

  python -u - <<'PY'
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from tests import run_ctp_live as probe

symbol = os.environ["CTP_SYMBOL"]
sample_seconds = float(os.environ["CTP_MARKET_SAMPLE_SECONDS"])
now = datetime.now(ZoneInfo("Asia/Shanghai"))
session_end = now.replace(hour=11 if now.hour < 12 else 14,
                          minute=30 if now.hour < 12 else 57, second=0, microsecond=0)
session_start = now.replace(hour=9 if now.hour < 12 else 13,
                            minute=30 if now.hour < 12 else 0, second=0, microsecond=0)
startup_budget = 3 * float(os.environ["CTP_CONNECT_TIMEOUT"]) + float(os.environ["CTP_MARKET_TIMEOUT"])
if not session_start <= now < session_end or now + timedelta(seconds=sample_seconds + startup_budget) >= session_end:
    raise SystemExit("仅在期权连续竞价期间采样，并预留连接准备时间；下午采样必须在 14:57 前完成。")
original = probe.MarketDataProbe.on_depth_market_data
lock = threading.Lock()
stats = {"ticks": 0, "first": None, "last": None, "max_gap": 0.0,
         "last_source": None}

def observe(self, data):
    if str(data.get("InstrumentID", "")) == symbol:
        now = time.monotonic()
        with lock:
            if stats["first"] is None:
                stats["first"] = now
            if now <= stats["first"] + sample_seconds:
                if stats["last"] is not None:
                    stats["max_gap"] = max(stats["max_gap"], now - stats["last"])
                stats["last"] = now
                stats["ticks"] += 1
                stats["last_source"] = {
                    key: data.get(key) for key in
                    ("TradingDay", "ActionDay", "UpdateTime", "UpdateMillisec")}
    return original(self, data)

probe.MarketDataProbe.on_depth_market_data = observe
try:
    probe.test4()
finally:
    with lock:
        if stats["first"] is None:
            print("RAW_GAP_RESULT: 未收到目标合约原始行情", flush=True)
        else:
            end = min(time.monotonic(), stats["first"] + sample_seconds)
            tail_gap = max(0.0, end - stats["last"])
            print("RAW_GAP_RESULT:", {
                "symbol": symbol,
                "sample_seconds": round(end - stats["first"], 3),
                "ticks": stats["ticks"],
                "max_receive_gap_seconds": round(max(stats["max_gap"], tail_gap), 3),
                "trailing_silence_seconds": round(tail_gap, 3),
                "last_source": stats["last_source"],
            }, flush=True)
PY
)
