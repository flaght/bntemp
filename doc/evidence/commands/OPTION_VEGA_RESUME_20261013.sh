#!/usr/bin/env bash
# Same-day recovery of the verified 08 SimNow session. May submit SimNow orders.
(
  cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1

  export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
  export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
  export CTP_BROKER_ID=9999
  export CTP_PRODUCTION_MODE=true

  python -u - \
    --connect --mode simnow --resume \
    --enable-orders --confirm-simnow \
    --strategy-position-source fills --account-reconciliation strict \
    --state-file /workspace/worker/pj/Chrono/nautilus_trader_demo/pro/tests/results/gap08-realtime-simnow/state-1791869411336135873.json \
    --market-source ctp --ctp-price-type last \
    --simnow-environment realtime \
    --expected-trading-day 20261013 --valuation-day 20261013 \
    --calendar-id china.cffex --reference-source dolphindb \
    --option-product MO --future-product IM --index-code 000852 \
    --index-source dolphindb-dfs \
    --index-database dfs://min_bar --index-table index_1min \
    --index-bar-timestamp end --index-availability observed \
    --index-price-precision 4 --index-size-precision 4 \
    --index-poll-seconds 0.2 --index-query-timeout 2 \
    --reference-timeout 30 --reference-refresh-seconds 5 \
    --frame-wait-seconds 20 --instrument-max-age-seconds 30 \
    --ready-timeout 180 --seconds 86400 \
    --option-slot 13:36 --delta-targets 0.25 \
    --max-option-lots 1 --max-total-option-lots 2 \
    --max-future-lots 1 --max-notional 2000000 \
    --limit-offset-ticks 1 --max-session-orders 4 \
    --report-dir tests/results/gap08-realtime-simnow-resume <<'PY'
import importlib
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")
entry = importlib.import_module("demos.08_option_vega.run_live")
args = entry.parse_args(sys.argv[1:])
zone = ZoneInfo("Asia/Shanghai")
stop_at = datetime.strptime(args.expected_trading_day, "%Y%m%d").replace(
    hour=15, minute=0, second=0, tzinfo=zone)
now = datetime.now(zone)
if now.date() != stop_at.date() or now >= stop_at:
    raise SystemExit("此原状态只恢复 2026-10-13 当日会话；请在收盘前启动。")

logging.basicConfig(level=getattr(logging, args.log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
runtime = entry.build_runtime(args)
original_poll = runtime.controller.poll
stop_requested = False

def request_stop(signum, frame):
    global stop_requested
    if not stop_requested:
        stop_requested = True
        print("已请求停机：等待当前操作结束，随后撤单、核验和保存。", flush=True)

def poll_until_close(session):
    if stop_requested or datetime.now(zone) >= stop_at:
        return False
    return original_poll(session)

runtime.controller.poll = poll_until_close
signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)
print(f"恢复原状态：{args.state_file}\n自动停机：{stop_at.isoformat()}；Ctrl+C 可提前请求停机。", flush=True)
try:
    runtime.run()
finally:
    runtime.stop()
PY
)
