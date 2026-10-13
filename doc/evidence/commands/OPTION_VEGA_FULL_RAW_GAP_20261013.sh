#!/usr/bin/env bash
# Reads the saved contract set and subscribes MD only; no TD, state restore or orders.
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
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from tests import run_ctp_live as probe

state_path = Path("tests/results/gap08-realtime-simnow/state-1791869411336135873.json")
document = json.loads(state_path.read_text())
identity = document["payload"]["component_states"]["session_identity"]["identity"]
contracts = identity["contracts"]
held = os.environ["CTP_SYMBOL"]
future = contracts[held]["future"]
symbols = set(contracts) | {row["future"] for row in contracts.values()}
if not 1 <= len(symbols) <= 1000:
    raise SystemExit("原状态合约数量异常，未连接。")
now = datetime.now(ZoneInfo("Asia/Shanghai"))
sample_seconds = float(os.environ["CTP_MARKET_SAMPLE_SECONDS"])
session_end = now.replace(hour=11 if now.hour < 12 else 14,
                          minute=30 if now.hour < 12 else 57, second=0, microsecond=0)
session_start = now.replace(hour=9 if now.hour < 12 else 13,
                            minute=30 if now.hour < 12 else 0, second=0, microsecond=0)
if now.date().isoformat() != identity["valuation_day"] or not session_start <= now < session_end:
    raise SystemExit("该对比使用原会话当日合约，请在 2026-10-13 期权连续竞价时段运行；下午截至 14:57。")
startup_budget = 3 * float(os.environ["CTP_CONNECT_TIMEOUT"]) + float(os.environ["CTP_MARKET_TIMEOUT"])
if now + timedelta(seconds=sample_seconds + startup_budget) >= session_end:
    raise SystemExit("连接/订阅/首份行情与采样预算可能跨越连续竞价边界；本次不连接。")

lock = threading.Lock()
accepted = set()
rejected = {}
window_start = None
stats = {key: {"ticks": 0, "last": None, "max_gap": 0.0, "last_source": None}
         for key in ("ALL", held, future)}
original_depth = probe.MarketDataProbe.on_depth_market_data

def login_all(self, error):
    probe.LoginProbe.on_login_response(self, error)
    if probe._error_id(error) or self.driver is None:
        return
    print(f"全链原始订阅：{len(symbols)} 个合约；监控 ALL / {held} / {future}", flush=True)
    try:
        for symbol in sorted(symbols):
            self.driver.subscribe(symbol)
    except Exception as exc:
        self.subscribe_error = {"ErrorID": -1, "ErrorMsg": str(exc)}
        self.subscribe_done.set()

def subscription(self, data, error):
    symbol = str((data or {}).get("InstrumentID", ""))
    with lock:
        if probe._error_id(error):
            rejected[symbol] = dict(error)
            self.subscribe_error = error
            self.subscribe_done.set()
        else:
            accepted.add(symbol)
            if symbols <= accepted:
                self.subscribe_done.set()

def observe(self, data):
    global window_start
    symbol = str(data.get("InstrumentID", ""))
    now = time.monotonic()
    with lock:
        if window_start is None and symbol == held:
            window_start = now
        if window_start is not None and symbol in symbols and now <= window_start + sample_seconds:
            for key in ("ALL", symbol):
                if key not in stats:
                    continue
                row = stats[key]
                previous = window_start if row["last"] is None else row["last"]
                row["max_gap"] = max(row["max_gap"], now - previous)
                row["last"] = now
                row["ticks"] += 1
                row["last_source"] = {k: data.get(k) for k in
                    ("InstrumentID", "TradingDay", "ActionDay", "UpdateTime", "UpdateMillisec")}
    # Original stage 4 waits and prints the held contract, not another symbol.
    if symbol == held:
        return original_depth(self, data)

probe.MarketDataProbe.on_login_response = login_all
probe.MarketDataProbe.on_subscribe_response = subscription
probe.MarketDataProbe.on_depth_market_data = observe
try:
    probe.test4()
finally:
    with lock:
        print("FULL_RAW_SUBSCRIPTIONS:", {
            "requested": len(symbols), "accepted": len(symbols & accepted),
            "rejected": rejected, "missing": sorted(symbols - accepted - rejected.keys())}, flush=True)
        if window_start is None:
            print("FULL_RAW_GAP_RESULT: 未收到持仓合约原始行情", flush=True)
        else:
            end = min(time.monotonic(), window_start + sample_seconds)
            results = {}
            for key, row in stats.items():
                last = window_start if row["last"] is None else row["last"]
                tail_gap = max(0.0, end - last)
                results[key] = {
                    "ticks": row["ticks"],
                    "max_receive_gap_seconds": round(max(row["max_gap"], tail_gap), 3),
                    "trailing_silence_seconds": round(tail_gap, 3),
                    "last_source": row["last_source"]}
            print("FULL_RAW_GAP_RESULT:", {
                "sample_seconds": round(end - window_start, 3), "streams": results}, flush=True)
PY
)
