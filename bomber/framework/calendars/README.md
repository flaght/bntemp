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

## Direct Provider assembly (default)

Formal EMA history/continuous startup and recovery precheck no longer choose a
CU JSON file by default. They read reusable hours through the configured source
and obtain dates directly from `CalendarProvider.snapshot()`. Calling the native
provider without dates uses its maintained coverage; calling an exported provider
without dates uses the coverage in that date export. Backtest task dates remain
query parameters, not a second list of trading dates.

```python
from bomber.framework.dataprep.session_inputs import SessionInputs, load_session_inputs

# Local backtest: standard lowercase fields, no hours or calendar JSON.
inputs = SessionInputs(
    product="ZN", calendar_id="china.sse", exchange="SHFE",
    backend="file", hours_file="data/trading_hours.feather",
    overrides_file="data/session_overrides.feather",
)
sessions = load_session_inputs(inputs)

# Live: database/environment configuration, same calendar and rule parser.
live_inputs = SessionInputs(
    product="ZN", calendar_id="china.sse", exchange="SHFE",
    hours_table="trading_hours", overrides_table="trading_session_overrides",
)
live_sessions = load_session_inputs(live_inputs)
```

The commodity example explicitly selects the supported `china.sse` date rules;
it does not register an automatic SHFE/SSE alias. Set the actual source locations,
calendar binding and authoritative exceptions for the deployment. Native coverage
currently ends in 2026; requests beyond maintained dates fail.

For a dataset whose scope has been checked and has no exceptions, replace
`overrides_file`/`overrides_table` with `no_overrides=True`. Missing exception
configuration, failed queries and truncated results are not treated as empty.
An explicitly read empty exception table is a known empty set.

Callers that already own an open source may use
`build_session_calendar(source, binding, calendar_provider, overrides=...)`.
This accepts a native or exported date Provider and does not require a JSON path,
start/end declaration or manual trading-day list. A provided empty tuple declares
no exceptions; `None` means unknown and is rejected. The source's open/close
lifecycle remains caller-owned.

## Special-date source format

The new `session_overrides` dataset is read from DolphinDB or Feather. Its seven
standard fields are:

```text
code, override_id, date_basis, date, action, target, segments
```

`code` is the explicit hours-rule key. DolphinDB source `Code` is mapped to `code`;
files already use lowercase names. `override_id` must be unique within a code.
`date` is a DATE or ISO date, and `date_basis` is `TRADING_DAY` or `NIGHT_START`.
`action` is `CANCEL`/`REPLACE`; `target` is `DAY`, `NIGHT`, `ALL` or a segment ID.
For a cancellation, `segments` is NULL or `[]`. For a replacement it holds a list
of the six segment fields (the hours schema without `code`). Source storage may
use a JSON **text cell** for this list or an Arrow list of objects; this is payload
inside the table, not a separate JSON calendar file. A full empty replacement
explicitly closes the selected group.

Example cancellation row:

```text
code=ZN
 override_id=holiday-night
 date_basis=NIGHT_START
 date=2026-09-30
 action=CANCEL
 target=NIGHT
 segments=[]
```

This is a format illustration; the deployed records must be checked against the
actual exchange/series rules. No table is automatically created or populated.
Set the source table through `--session-overrides-table` or
`DDB_SESSION_OVERRIDES_TABLE`; no undeclared table is guessed. In a backtest pass
`overrides_file` to `SessionInputs`. Replacements and cancellations contribute to
the session fingerprint. Only records in the date Provider's covered range are
selected. Historical publication/revision selection remains outside these static
snapshot readers.

## EMA runtime arguments and explicit legacy compatibility

Use the following session arguments when history windows or continuous supervision
are enabled:

```text
--calendar-id               or BOMBER_CALENDAR_ID
--hours-code                optional explicit sharing/inheritance key
--session-timezone          default Asia/Shanghai
--trading-hours-database     optional; otherwise reference database configuration
--trading-hours-table        optional; otherwise DDB_TRADING_HOURS_TABLE
--session-overrides-table    or DDB_SESSION_OVERRIDES_TABLE
--no-session-overrides      explicit known-empty alternative to an exception source
```

Formal Live hours and exceptions use DolphinDB. For file-source testing, use the
existing `--reference-source file --allow-file-reference-test` authorization and
pass `--trading-hours-file` and `--session-overrides-file` (or the known-empty flag).
Backtest libraries use local file inputs directly. CLI validation is local and
does not read credentials or create an SDK connection. Source loading occurs
only when the runtime needs a calendar.

`--trading-calendar /path/to/old.json` is retained as an **explicit** v1/v2
compatibility entry. Its parser remains versioned; it cannot be mixed with direct
Provider flags. The JSON examples in
`scripts/integration/reference_data/configs/` illustrate that compatibility path,
not the default assembly. Existing legacy files and checkpoints do not have their
rule identities silently rewritten.

The continuous supervisor passes one frozen snapshot to its workers. Direct
Provider and v2 states include `session_rules`; changes to hours, date rules or
coverage, bindings or exceptions reject resume until an explicit migration is
provided. The recovery precheck uses the same source assembly and identity check.
Old v1 config uses the shared algorithm through the compatibility adapter.
Existing channel/product order restrictions remain unchanged by session loading.

Historical consumers use `SessionHistoryWindow` (the public service).
`ImDayWindow` remains a single-day probe adapter. Integration tests use in-memory
session snapshots from `tests/session_fixtures.py`; fixed dates there belong to
the test fixture, not the application's default data source.

Independent verification:

```bash
python -m unittest tests.test_trading_hours tests.test_session_inputs \
  tests.test_trading_calendar tests.test_calendars_compiled -v
python -m tests.run_main_ema_acceptance --only trading-hours
```

The tests use fake DolphinDB sessions plus real Feather round trips. They do not
connect to a deployed database or order gateway.
