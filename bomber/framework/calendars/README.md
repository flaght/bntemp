# Calendar Cython build

This library is independent of the Bomber trading engine and NumPy. Its eleven
Cython extensions must be built with the Python interpreter, operating system
and CPU architecture used by the application. The existing Python 3.8 Linux
extension files cannot serve a Python 3.12 application.

## Build in the running environment

From the `pro` project root, with the target virtual environment activated:

```bash
uv pip install -r bomber/framework/calendars/requirements-build.txt
python bomber/framework/calendars/setup.py build_ext --inplace
python -u -m unittest tests.test_calendars_compiled -v
```

Without uv, use `python -m pip install -r
bomber/framework/calendars/requirements-build.txt` for the first step. A C compiler
and Python headers are required. Linux uses a working GCC/Clang toolchain; macOS
uses the Command Line Tools; Windows uses a compatible MSVC toolchain.

`setup.py` normalizes the working directory to the project root, discovers the
`.pyx` modules, and uses their full `bomber.framework.calendars.*` module names.
It generates new C sources and build intermediates under `.build/calendars/`,
leaving the historical `.c` files unchanged. `--inplace` places the extensions
beside their sources. Use `build_ext --inplace --force` to force recompilation
when compiler options change.

For manual transfer to another machine, copy the `.py`, `.pyx`, `.pxd` files and
`requirements-build.txt` in this directory and its subdirectories, plus
`tests/test_calendars_compiled.py`. Rebuild on that machine. Do not transfer the
local machine's `.so`/`.pyd`, `__pycache__` or `.build` artifacts as runtime inputs.
The source imports and Cython cimports now use the Bomber namespace; an external
`alphacopilot` installation is not needed.

## Interface

```python
from datetime import date
from bomber.framework.calendars.api import isBizDay, bizDatesList
from bomber.framework.calendars.DateUtilities import Calendar, Date

isBizDay("china.cffex", date(2026, 1, 12))
days = bizDatesList("china.cffex", date(2026, 1, 12), date(2026, 1, 16))
remaining = Calendar("china.cffex").bizDaysBetween(
    Date(2026, 1, 12), Date(2026, 1, 16), False, True,
)
```

The public date-list API returns Python `datetime` objects. `bizDaysBetween`
accepts explicit inclusion flags; the example excludes today and includes the
last trading day, matching the option-vega strategy's counting convention.

The public `NativeCalendarProvider` in `trading.py` now wraps the compiled rules
and returns an immutable `CalendarSnapshot` with explicit identity, version,
coverage and content fingerprint. It supports the bundled `china.cffex` and
`china.sse` rules for 1991–2026 and rejects requests outside that range. The
existing CFFEX implementation uses the SSE holiday rules. Holiday data is
unchanged; the boundary is declared for the bundled table, not inferred from
whether an unknown future weekday happens to be absent from its holiday set.

08 Live now consumes a native snapshot by default, without a calendar file.
`export_calendar()` writes a new CSV and completion manifest;
`ExportedCalendarProvider` verifies coverage, file SHA256 and snapshot identity.
The public dataprep calendar loader uses this provider for manifested exports,
so a backtest consumes the same rules version from a file.

```bash
python -u -m unittest tests.test_calendars_compiled tests.test_trading_calendar -v
python -m scripts.integration.reference_data.calendar_export \
  --calendar-id china.cffex --start 2026-01-01 --end 2026-12-31 \
  --output tests/results/cffex-2026.csv
```

Intraday sessions and night-session trading-day attribution remain separate
from this date calendar.

## Trading-hours snapshots and shared session queries

`trading_hours` is a reusable static table; it does not store a row for every
trading date. Its standard columns are:

```text
code, segment_id, date_anchor, start_day_offset, start_time,
end_day_offset, end_time
```

The file input is `trading_hours.feather`, with lowercase `code` and no sidecar
manifest. The DolphinDB adapter maps its source `Code` to `code`, just as existing
reference datasets use their own field mappings. Files do not accept `Code`.
DolphinDB clock columns are explicitly queried as strings; Feather accepts clock
strings, `datetime.time`, and durations with explicit units. Numeric epoch values
and full timestamps are not interpreted as clocks. All first-stage segments
must align to full minutes; offsets are integer 0 or 1.

`TRADING_DAY` anchors a segment to its owning trading date. `NIGHT_START` anchors
it to the previous trading date from the configured `CalendarSnapshot`. For
example, a split night stores `21:00 -> offset 1, 00:00`, then
`offset 1, 00:00 -> offset 1, 01:00`. Both belong to the next trading date, even
when the night falls on Friday/Saturday and the owning day is Monday.

Calendar identity is explicit. The native provider currently supports
`china.cffex` and `china.sse`; choosing a calendar for a commodity exchange is an
explicit configuration decision, not an inferred alias. Calendar exports retain
their existing coverage manifest; removing the **hours** manifest does not change
the date-calendar export contract.

Read either source with the existing `ReferenceSourceFactory` and the shared
parser:

```python
from bomber.framework.dataprep.sources import (
    ReferenceSourceFactory, ReferenceDataset, DataSourcePurpose,
    DolphinDbReferenceConfig,
)
from bomber.framework.dataprep.trading_hours import read_trading_hours

# Backtest: no database connection, no trading_hours.feather.manifest.json.
with ReferenceSourceFactory.create_for(
    DataSourcePurpose.OFFLINE_REFERENCE, "file",
    {ReferenceDataset.TRADING_HOURS: "data/trading_hours.feather"},
) as source:
    hours = read_trading_hours(source, ("ZN",))

# Live: uses DDB_HOST/PORT/USERNAME/PASSWORD and existing connection lifecycle.
with ReferenceSourceFactory.create_for(
    DataSourcePurpose.LIVE_REFERENCE, "dolphindb",
    DolphinDbReferenceConfig.from_env(),
) as source:
    live_hours = read_trading_hours(source, ("ZN",))
```

The default table is `trading_hours`; override `DDB_TRADING_HOURS_TABLE` and the
reference database configuration to match the actual deployment. Queries use
`ReferenceQuery(products=...)`, not real-contract symbols or source-date filters.
Source failures, truncated results, missing codes, alias conflicts and duplicate
segments fail explicitly.

`TradingHoursSnapshot.fingerprint` hashes normalized rows sorted by
`code/segment_id`. Source location, row order and equivalent clock representations
do not change it. Each selected code is a **complete static group**. This first
implementation accepts exactly the seven columns above; it does not select
historical effective/available revisions. Use a snapshot whose applicability to
the requested range has been established. A content hash is not evidence of a
historical publication time.

Construct the public service independently of storage:

```python
from bomber.framework.calendars import SessionBinding, SessionCalendar

binding = SessionBinding(
    product="ZN", hours_code="ZN", calendar_id=dates.calendar_id,
    timezone="Asia/Shanghai", exchange="SHFE", asset_type="FUTURE",
)
calendar = SessionCalendar(hours, dates, binding, overrides=())
windows = calendar.windows(trading_day)
last = calendar.last_minutes(before_ns, 300)
missing = calendar.missing_minutes(after_ns, before_ns)
```

`dates` is a `CalendarSnapshot`; `trading_day` is a `datetime.date`, and cutoff
values are integer UTC nanoseconds. A period is `[start,end)`. Minute stamps use
the existing historical convention `bucket_end_ns - 1`; partial minutes are
excluded. Minute windows cross trading days and have a configurable resource
limit (`max_minutes`, default 10000), not a product-specific 240-minute cap.
Intervals are expanded and cached only for queried natural-date anchors.

A new product or option series chooses `hours_code`, `calendar_id` and timezone
through `SessionBinding`. Setting an option binding's `hours_code` to the explicitly
chosen underlying's code expresses shared hours without guessing option symbols.
The session service does not authorize trading or derive an underlying relation.

Special-date rules are `SessionOverride` values. `date_basis` is `NIGHT_START`
(the actual night-start date) or `TRADING_DAY` (the owning date), `action` is
`CANCEL` or `REPLACE`, and `target` is `NIGHT`, `DAY`, `ALL` or a segment ID.
Cancelling `NIGHT` removes both pre-midnight and post-midnight pieces. A group
replacement replaces its entire segment set, including an explicit empty set;
conflicting applicable rules or overlapping intervals fail rather than depend
on read order. The caller declares the complete exception set for the range.
Neither weekdays nor the next trading date alone prove that a night opens.

## Runtime configuration and compatibility

The existing `load_sessions(path, product)` also accepts an assembly JSON with
`schema_version=2`. It is application configuration, not a manifest attached to
`trading_hours.feather`. A Live example is:

```json
{
  "schema_version": 2,
  "product": "IM",
  "calendar": {
    "backend": "native", "calendar_id": "china.cffex",
    "start": "2026-01-01", "end": "2026-12-31"
  },
  "binding": {
    "hours_code": "IM", "timezone": "Asia/Shanghai",
    "exchange": "CFFEX", "asset_type": "FUTURE"
  },
  "hours": {
    "backend": "dolphindb", "table": "trading_hours"
  },
  "overrides": []
}
```

For a backtest, replace `hours` with
`{"backend":"file","path":"trading_hours.feather"}`; paths are relative to the
assembly file. The date calendar can also use
`{"backend":"file","calendar_id":"...","path":"calendar.csv","start":"...","end":"..."}`
with the existing verified calendar export. This pins the same date rules locally.
An explicit `overrides: []` declares no exceptions for the configured range;
populate it when night cancellations or special hours apply. For example:

```json
{
  "date_basis": "NIGHT_START", "date": "2026-09-30",
  "action": "CANCEL", "target": "NIGHT"
}
```

Use `--trading-calendar /path/to/session.json` for the EMA Live/history and
continuous entry points. CLI validation of v2 configuration does not query the
database or read connection credentials. Actual loading occurs at runtime and
closes the reference connection after obtaining a frozen snapshot. Formal Live
v2 hours use DolphinDB; local source testing requires the existing
`--allow-file-reference-test` flag. Existing channel/product execution restrictions
still apply; a new hours row does not expand order authorization.

Editable assembly examples are checked in at
`scripts/integration/reference_data/configs/trading_hours_live.example.json` and
`trading_hours_backtest.example.json`. They use the already supported CFFEX/IM
binding as an illustration; configure the actual code, calendar, exception set,
database/table or Feather path before use. These are application configuration
files and do not accompany or validate the hours file as a manifest would.

The continuous supervisor passes the same session snapshot to each worker. New
v2 EMA persisted states include a `session_rules` fingerprint component; changing
hours, calendar coverage/rules, bindings or exceptions rejects resume until an
explicit migration is designed. The recovery precheck verifies the same identity.
Old v1 session JSON remains supported as a thin value/configuration adapter;
its window and minute algorithms now delegate to the public service.
`ImDayWindow` remains a single-day IM probe adapter using the same service;
`SessionHistoryWindow` exposes the generic service for new historical consumers.

Independent offline verification:

```bash
python -m unittest tests.test_trading_hours tests.test_trading_calendar \
  tests.test_calendars_compiled -v
```

The trading-hours tests use a fake DolphinDB session plus real Feather round trips;
they do not establish a connection to a deployed database or order gateway.
