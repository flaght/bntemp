#!/usr/bin/env bash
# Usage: bash ... YYYYMMDD HH:MM actualSharedStreamTable start|end cumulative|per-bar
# Market push only; index DFS query; Recording does not connect TD or submit orders.
set -euo pipefail
VEGA_DAY=${1:?请提供当日YYYYMMDD}
VEGA_SLOT=${2:?请提供启动完成后的选约HH:MM}
VEGA_TABLE=${3:-stream_cffex_1min}
VEGA_BAR_LABEL=${4:?请确认minTime为start或end}
VEGA_VOLUME_MODE=${5:?请确认volume为cumulative或per-bar}
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
python -u -m demos.08_option_vega.run_live \
  --connect --mode recording --market-source dolphindb \
  --market-bar-table "$VEGA_TABLE" \
  --market-bar-timestamp "$VEGA_BAR_LABEL" --market-volume-mode "$VEGA_VOLUME_MODE" \
  --market-bar-max-age-seconds 120 \
  --simnow-environment realtime --expected-trading-day "$VEGA_DAY" --valuation-day "$VEGA_DAY" \
  --calendar-id china.cffex --reference-source dolphindb \
  --option-product MO --future-product IM --index-code 000852 \
  --index-source dolphindb-dfs --index-database dfs://min_bar --index-table index_1min \
  --index-bar-timestamp end --index-availability observed \
  --index-price-precision 4 --index-size-precision 4 \
  --index-poll-seconds 0.2 --index-query-timeout 2 \
  --reference-timeout 30 --reference-refresh-seconds 5 \
  --frame-wait-seconds 20 --ready-timeout 180 --seconds 900 \
  --option-slot "$VEGA_SLOT" --delta-targets 0.25 \
  --max-option-lots 1 --max-total-option-lots 2 --max-future-lots 1 \
  --max-notional 2000000 --report-dir tests/results/gap08-dolphin-recording
