"""统一标准化数据，再通过现有行情解析器回放已准备的行。"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from math import isfinite

from .catalog import symbol
from .contracts import BarFileKey, BarLoadResult, BarReadSpec, InputIssue
from .session import current_session, fail, read_feather, record_issue, profile_input


def timestamps(values, timezone="Asia/Shanghai"):
    import pandas as pd
    stamps = pd.to_datetime(values, errors="raise")
    if stamps.isna().any():
        fail("INVALID_TIMESTAMP", "Empty market timestamp")
    return (stamps.dt.tz_localize(timezone) if stamps.dt.tz is None
            else stamps.dt.tz_convert(timezone))


def _nanoseconds(values):
    """Convert the whole datetime array, explicitly normalizing its storage unit.

    Pandas can retain second/microsecond storage; asi8 alone would then silently
    return the wrong unit. The older-Pandas fallback already stores nanoseconds.
    """
    import pandas as pd
    array = values.array
    if hasattr(array, "as_unit"):
        try:
            array = array.as_unit("ns")
        except (OverflowError, ValueError):
            # Keep the original scalar conversion's error for out-of-range dates.
            return values.map(lambda stamp: int(stamp.value))
    return pd.Series(array.asi8, index=values.index, name=values.name)


def _reset_bar_index(frame):
    """A normal Feather RangeIndex is already the index the reader promises."""
    import pandas as pd
    index = frame.index
    if (isinstance(index, pd.RangeIndex) and index.start == 0 and index.stop == len(frame)
            and index.step == 1 and index.name is None):
        return frame
    return frame.reset_index(drop=True)


def _finite_values(values):
    import numpy as np
    import pandas as pd
    dtype = values.dtype
    if dtype.kind in "biu" or (dtype.kind == "f" and dtype.itemsize <= 8):
        # Use float only for the finiteness mask; retain original integer columns
        # for positivity/modulo checks (including quantities above 2**53).
        return np.isfinite(values.to_numpy(dtype=np.float64, na_value=np.nan))
    return values.map(lambda value: not pd.isna(value) and isfinite(float(value)))


def _native_numbers(values):
    """Ordinary numeric columns need neither coercion nor pandas mask objects."""
    import numpy as np
    dtype = values.dtype
    if isinstance(dtype, np.dtype) and dtype.kind in "biuf" and dtype.itemsize <= 8:
        return values.to_numpy(copy=False)
    return None


def _attach_time_columns(frame, columns):
    """Build all time columns once, preserving replacement/append column order."""
    import pandas as pd
    if type(frame.columns) is not pd.Index:
        # Preserve insertion behavior of unusual indexes (e.g. CategoricalIndex).
        for name, values in columns.items():
            frame[name] = values
        return frame
    order = [*frame.columns, *(name for name in columns if name not in frame.columns)]
    result = pd.DataFrame({name: columns[name] if name in columns else frame[name] for name in order},
        index=frame.index, columns=pd.Index(order, dtype=frame.columns.dtype, name=frame.columns.name), copy=False)
    result.attrs = frame.attrs
    if not frame.flags.allows_duplicate_labels:
        result.flags.allows_duplicate_labels = False
    return result


def _ohlc_check(frame):
    """Check homogeneous numeric prices without constructing row-wise frames.

    Mixed/nullable dtypes retain the original Pandas reductions, including its
    dtype promotion and missing-value behavior for audited research inputs.
    """
    import numpy as np
    import pandas as pd
    names = ("open", "high", "low", "close")
    dtype = frame["open"].dtype
    if (isinstance(dtype, np.dtype) and dtype.kind in "biuf"
            and all(frame[name].dtype == dtype for name in names[1:])):
        opening, high, low, closing = (frame[name].to_numpy(copy=False) for name in names)
        maximum = np.fmax(np.fmax(opening, closing), np.fmax(high, low))
        minimum = np.fmin(np.fmin(opening, closing), np.fmin(high, low))
        invalid = pd.Series((high < maximum) | (low > minimum), index=frame.index)
        return invalid, pd.Series(maximum, index=frame.index)
    invalid = (frame.high.lt(frame[["open", "low", "close"]].max(axis=1))
        | frame.low.gt(frame[["open", "high", "close"]].min(axis=1)))
    return invalid, None


def timestamp_column(path):
    frame = read_feather(path)
    for name in ("datetime", "timestamp"):
        if name in frame.columns:
            return name
    fail("MISSING_FIELD", "Missing datetime/timestamp column", source=path)


def key_from_path(path, asset_kind="future"):
    from datetime import date
    code, separator, label = Path(path).stem.rpartition("_")
    if not separator or len(label) != 8 or not label.isdigit():
        fail("IDENTITY_MISMATCH", "Expected contract_YYYYMMDD.feather", source=path)
    day = date.fromisoformat(f"{label[:4]}-{label[4:6]}-{label[6:]}")
    return BarFileKey(asset_kind, code, day)


@profile_input("bar_read_normalize_validate")
def read_bar_frame(path, spec=None, key=None, *, copy_frame=True):
    """Return an isolated frame by default; False borrows a read-only cache frame.

    Borrowing is reserved for preparation/replay paths that never modify it.
    The normal public API continues to protect cached data from caller writes.
    """
    import numpy as np
    import pandas as pd
    spec = spec or BarReadSpec()
    key = key or key_from_path(path)
    path = Path(path).expanduser().resolve()
    session = current_session()
    fingerprint = session.fingerprint(path) if session is not None else None
    cache_key = ("bar", fingerprint, key, spec)
    if session is not None and cache_key in session.cache:
        session.performance.count("bar_cache_hits")
        old = session.cache[cache_key]
        return replace(old, frame=old.frame.copy(deep=True)) if copy_frame else old
    frame = read_feather(path)
    if not frame.columns.is_unique or frame.empty:
        fail("INVALID_SCHEMA", "Empty file or duplicate columns", source=path)
    frame = _reset_bar_index(frame)
    mapping = dict(spec.field_mapping)
    if mapping:
        frame = frame.rename(columns={raw: canonical for canonical, raw in mapping.items()})
    if not frame.columns.is_unique:
        fail("INVALID_SCHEMA", "Field mapping produces duplicate columns", source=path)
    time_key = spec.timestamp_column or next((n for n in ("datetime", "timestamp") if n in frame), None)
    needed = set(spec.required_fields) | ({time_key} if time_key else {"datetime"})
    if needed - set(frame):
        fail("MISSING_FIELD", f"Missing fields: {sorted(needed - set(frame))}", source=path)
    if "symbol" in frame and not frame.symbol.map(symbol).eq(key.symbol).all():
        fail("IDENTITY_MISMATCH", "Row symbol differs from file identity", source=path)
    if key.venue and "exchange" in frame:
        from .metadata import VENUES
        expected = key.venue.upper()
        if not frame.exchange.map(lambda value: VENUES.get(str(value).strip().upper(), str(value).strip().upper())).eq(expected).all():
            fail("IDENTITY_MISMATCH", "Row exchange differs from declared venue", source=path)
    if "trade_date" in frame and not pd.to_datetime(frame.trade_date, errors="raise").dt.date.eq(key.trading_day).all():
        fail("IDENTITY_MISMATCH", "trade_date differs from file trading day", source=path)
    raw = timestamps(frame[time_key], spec.timezone)
    if spec.trading_day_policy == "day_session" and not raw.dt.date.eq(key.trading_day).all():
        fail("IDENTITY_MISMATCH", "Day-session date differs from file", source=path)
    if spec.trading_day_policy == "exchange" and "trade_date" not in frame:
        # 以上游文件声明的身份为准；缺少交易所日历时，夜盘和节假日
        # 不能仅根据自然日期偏移重建交易日。
        if raw.dt.date.map(lambda d: d > key.trading_day).any():
            fail("IDENTITY_MISMATCH", "Timestamp is later than file trading day", source=path)
        if session is not None and "exchange_trading_day_trusted_from_file_identity" not in session.coverage.assumptions:
            session.coverage.assumptions += ("exchange_trading_day_trusted_from_file_identity",)
    event = raw + pd.Timedelta(seconds=spec.interval_seconds if spec.timestamp_label == "start" else 0)
    ns = _nanoseconds(event)
    if spec.require_minute_alignment and (ns % (spec.interval_seconds * 1_000_000_000)).any():
        fail("INVALID_TIMESTAMP", "分钟快照时间必须对齐整分钟", source=path)
    if ns.duplicated().any():
        fail("DUPLICATE_BAR", "Duplicate completed bar timestamp", source=path)
    if spec.trading_day_policy == "day_session" and not event.dt.date.eq(key.trading_day).all():
        fail("IDENTITY_MISMATCH", "分钟时间转换后跨日期", source=path)
    if spec.available_column and spec.available_column not in frame:
        fail("MISSING_FIELD", f"Missing {spec.available_column}", source=path)
    available_values = frame[spec.available_column].copy() if spec.available_column else None
    if spec.available_column:
        available = timestamps(available_values, spec.timezone)
        available_ns = _nanoseconds(available)
    else:
        available, available_ns = event, ns
    if available_ns.lt(ns).any():
        fail("INVALID_TIMESTAMP", "Availability precedes completed minute", source=path)
    # The timezone was already normalized above. Preserve the original ns dtype
    # without parsing the same integer clock through pandas a second time.
    available_datetime = (available.dt.as_unit("ns") if hasattr(available.dt, "as_unit") else available)
    frame = _attach_time_columns(frame, {"source_timestamp": raw, "datetime": event,
        "event_ns": ns, "bar_ns": ns, "trading_day": key.trading_day,
        "available_ns": available_ns, "available_datetime": available_datetime})
    issues = []
    if spec.zero_price_policy == "drop":
        prices = [name for name in ("open", "high", "low", "close") if name in spec.required_fields]
        zeros = frame[prices].apply(pd.to_numeric, errors="coerce").eq(0).any(axis=1) if prices else pd.Series(False, index=frame.index)
        if zeros.any():
            issue = InputIssue("ZERO_PRICE_BAR_DROPPED", f"Explicitly dropped {int(zeros.sum())} zero-price bars",
                "WARNING", str(path), key.symbol, key.trading_day)
            issues.append(issue)
            record_issue(issue)
            frame = frame.loc[~zeros].copy()
    for name in spec.required_fields:
        values = frame[name]
        array = _native_numbers(values)
        if array is None:
            frame[name] = pd.to_numeric(values, errors="coerce")
            values = frame[name]
            array = _native_numbers(values)
        if array is not None and np.isfinite(array).all():
            valid = array >= 0 if name in {"volume", "open_interest"} else array > 0
            if name == "volume" and spec.require_integer_volume:
                valid &= array % 1 == 0
        else:
            # Keep nullable/non-finite/special-type diagnostics and arithmetic
            # behavior; the ordinary, finite case avoids repeated Series ops.
            finite = _finite_values(values)
            valid = finite & (values.ge(0) if name in {"volume", "open_interest"} else values.gt(0))
            if name == "volume" and spec.require_integer_volume:
                valid &= values.mod(1).eq(0)
        for index in frame.index[~valid]:
            issue = InputIssue("INVALID_BAR_FIELDS", f"Invalid {name}",
                "WARNING" if spec.value_policy == "research_audited" else "ERROR",
                str(path), key.symbol, key.trading_day, name, int(index) + 1)
            if spec.value_policy != "research_audited":
                record_issue(issue)
                from .contracts import InputError
                raise InputError(issue)
            issues.append(issue)
    if {"open", "high", "low", "close"} <= set(spec.required_fields):
        invalid, bounds = _ohlc_check(frame)
        if invalid.any():
            if spec.ohlc_policy == "reject":
                fail("INVALID_OHLC", "OHLC relationship invalid", source=path,
                    symbol=key.symbol, trading_day=key.trading_day, row=int(frame.index[invalid][0]) + 1)
            before = frame.loc[invalid, ["open", "high", "low", "close"]].head(3).to_dict("records")
            issue = InputIssue("OHLC_RANGE_EXPANDED",
                f"Expanded {int(invalid.sum())} bar ranges; original examples={before}",
                "WARNING", str(path), key.symbol, key.trading_day)
            issues.append(issue)
            record_issue(issue)
            frame["source_high"], frame["source_low"] = frame.high.copy(), frame.low.copy()
            if bounds is None:
                frame.loc[invalid, "high"] = frame.loc[invalid, ["open", "high", "low", "close"]].max(axis=1)
            else:
                frame.loc[invalid, "high"] = bounds.loc[invalid]
            # Preserve the established repair order: low sees the repaired high.
            frame.loc[invalid, "low"] = frame.loc[invalid, ["open", "high", "low", "close"]].min(axis=1)
    if spec.value_policy == "research_audited":
        frame["input_quality"] = ""
        for issue in issues:
            if issue.code == "INVALID_BAR_FIELDS":
                frame.at[issue.row - 1, "input_quality"] = "INVALID_BAR_FIELDS"
                record_issue(issue)
    if "volume" in frame:
        frame["zero_volume_bar"] = frame.volume.eq(0)
    if frame.available_ns.is_monotonic_increasing and frame.event_ns.is_monotonic_increasing:
        frame = _reset_bar_index(frame)
    else:
        frame = frame.sort_values(["available_ns", "event_ns"], kind="stable").reset_index(drop=True)
    if not frame.event_ns.is_monotonic_increasing:
        fail("UNSUPPORTED_CAPABILITY", "Out-of-order arrival needs stage-two late-frame handling", source=path)
    result = BarLoadResult(key, path, frame, spec, tuple(issues))
    if session is not None:
        session.performance.count("bar_files_prepared")
        session.performance.count("bar_rows_prepared", len(frame))
        session.cache[cache_key] = result
        details = {"identity": str(key), "rows": len(frame),
            "first_ns": result.first_ns, "last_ns": result.last_ns,
            "timestamp_label": spec.timestamp_label, "value_policy": spec.value_policy,
            "available_policy": spec.available_column or "completed_at_end",
            "gap_check": "interval_gaps_not_equivalent_to_missing_trading_minutes"}
        configurations = session.coverage.files.get(str(path), {}).get("specifications", [])
        configuration = {"fields": list(spec.required_fields), "timezone": spec.timezone,
            "timestamp_label": spec.timestamp_label, "value_policy": spec.value_policy,
            "available_column": spec.available_column, "trading_day_policy": spec.trading_day_policy,
            "interval_seconds": spec.interval_seconds, "bar_spec": spec.bar_spec,
            "ohlc_policy": spec.ohlc_policy, "zero_price_policy": spec.zero_price_policy}
        if configuration not in configurations:
            configurations.append(configuration)
        details["specifications"] = configurations
        session.coverage.files[str(path)] = details
        start, end = session.coverage.requested
        field = "dependency_days" if ((start is not None and key.trading_day < start)
            or (end is not None and key.trading_day > end)) else "actual_days"
        setattr(session.coverage, field,
            tuple(sorted(set(getattr(session.coverage, field)) | {key.trading_day})))
    return replace(result, frame=frame.copy(deep=True)) if copy_frame else result


def filter_session_bars(result: BarLoadResult, calendar) -> BarLoadResult:
    """在回放前按品种日历保留完整交易区间内的 Bar，返回独立副本。

    read_bar_frame 已将起点/终点标签统一为 event_ns 结束边界；只有整个
    [event_ns - interval, event_ns) 位于文件所属交易日的一个会话内才保留。
    因此结束标签的收盘 Bar 会保留，开盘边界之前的 Bar、午休、盘后及被
    例外取消的夜盘会剔除。原始时间标签及行情数值不变，不用成交量判定时段。
    剔除数量记入输入诊断；全被剔除或日历覆盖不足时明确失败。
    """
    import pandas as pd
    from bomber.framework.calendars.sessions import datetime_ns, ns_datetime

    frame = result.frame
    if frame.empty:
        fail("NO_SESSION_BARS", "No bars available for session filtering", source=result.path)
    ends = frame.event_ns
    interval_ns = result.spec.interval_seconds * 1_000_000_000
    # 覆盖不足不能被当成已确认休市；跨午夜的夜盘自然日期也须有覆盖。
    calendar.calendar.require_range(
        ns_datetime(int(ends.min()) - interval_ns, calendar.zone).date(),
        ns_datetime(int(ends.max()) - 1, calendar.zone).date(),
    )
    keep = pd.Series(False, index=frame.index)
    for window in calendar.windows(result.key.trading_day):
        keep |= (ends.ge(datetime_ns(window.start) + interval_ns)
                 & ends.le(datetime_ns(window.end)))
    kept = frame.loc[keep].copy(deep=True).reset_index(drop=True)
    dropped = len(frame) - len(kept)
    issues = result.issues
    if dropped:
        samples = ", ".join(map(str, frame.loc[~keep, "source_timestamp"].head(3)))
        issue = InputIssue("OUTSIDE_TRADING_SESSION",
            f"Dropped {dropped}/{len(frame)} bars outside complete trading intervals; "
            f"source timestamp samples: {samples}", "WARNING", str(result.path),
            result.key.symbol, result.key.trading_day, "source_timestamp")
        record_issue(issue)
        issues += (issue,)
    session = current_session()
    if session is not None:
        details = session.coverage.files.setdefault(str(result.path), {})
        report = {"product": calendar.product, "trading_day": str(result.key.trading_day),
            "calendar_sha256": calendar.provenance["sha256"],
            "interval_seconds": result.spec.interval_seconds,
            "input_rows": len(frame), "kept_rows": len(kept), "dropped_rows": dropped}
        filters = details.setdefault("session_filters", [])
        if report not in filters:
            filters.append(report)
    if kept.empty:
        fail("NO_SESSION_BARS", "No complete bars remain in the declared trading sessions",
             source=result.path, symbol=result.key.symbol, trading_day=result.key.trading_day)
    return replace(result, frame=kept, issues=issues)


class PreparedFrameReader:
    suffixes = frozenset({".feather"})

    def __init__(self, frame, *, copy_frame=True, columns=None):
        # Default isolates caller mutations. The backtest composition layer can
        # borrow its already-prepared frame for the duration of a read-only run.
        self.frame = frame.copy(deep=True) if copy_frame else frame
        self.columns = None if columns is None else tuple(columns)

    def read(self, path):
        frame = self.frame if self.columns is None else self.frame.loc[:, list(self.columns)]
        columns = tuple(frame.columns)
        # Stream one mapping at a time; avoid a second full list of row dicts.
        for index, values in enumerate(frame.itertuples(index=False, name=None), 1):
            yield index, dict(zip(columns, values))


def add_bar_source(feed, path, instrument_id, *, spec=None, key=None):
    """将已准备的行注册到现有行情源，不创建或启动行情源。"""
    if key is None:
        key = key_from_path(path)
        key = replace(key, venue=str(instrument_id.venue))
    result = read_bar_frame(path, spec, key)
    add_prepared_bar_source(feed, result, instrument_id)
    return result


def add_prepared_bar_source(feed, result, instrument_id, *, copy_frame=True, fast_parse=False):
    """消费场景已准备的结果，不重复读取或调整时间。"""
    from bomber.framework.market.replay.parsers.bar import FixedInstrumentBarParser
    if result.spec.value_policy != "execution_strict":
        fail("INVALID_SOURCE_PURPOSE", "Standard execution Bar requires execution_strict OHLCV", source=result.path)
    if result.key.venue and result.key.venue != str(instrument_id.venue):
        fail("IDENTITY_MISMATCH", "Prepared source venue differs from instrument", source=result.path)
    if result.key.symbol != symbol(instrument_id.symbol.value):
        fail("IDENTITY_MISMATCH", "Prepared source symbol differs from instrument", source=result.path)
    if "exchange" in result.frame:
        from .metadata import VENUES
        expected = str(instrument_id.venue).upper()
        if not result.frame.exchange.map(lambda value: VENUES.get(str(value).strip().upper(), str(value).strip().upper())).eq(expected).all():
            fail("IDENTITY_MISMATCH", "Prepared rows differ from instrument venue", source=result.path)
    columns = None
    # Legacy ISO parsing has microsecond resolution. Keep its original behavior
    # for exceptional sub-microsecond event clocks rather than changing history.
    event_ns = result.frame.get("event_ns")
    native_clock = _native_numbers(event_ns) if fast_parse and event_ns is not None else None
    use_prepared = (fast_parse and native_clock is not None and native_clock.dtype.kind in "iu"
        and (native_clock % 1000 == 0).all())
    if use_prepared:
        from bomber.framework.market.replay.parsers.bar.fixed import PreparedBarParser
        parser = PreparedBarParser(instrument_id, bar_spec=result.spec.bar_spec)
        columns = ("event_ns", "available_ns", "open", "high", "low", "close", "volume")
    else:
        parser = FixedInstrumentBarParser(instrument_id, timestamp="datetime",
            available_nanoseconds="available_ns", timezone=result.spec.timezone, bar_spec=result.spec.bar_spec)
    if fast_parse and current_session() is not None:
        stats = current_session().performance
        stats.count("prepared_parser_sources" if use_prepared else "prepared_parser_fallback_sources")
        stats.count("prepared_parser_rows" if use_prepared else "prepared_parser_fallback_rows", len(result.frame))
    feed.add_source(result.path, PreparedFrameReader(result.frame, copy_frame=copy_frame, columns=columns), parser, "bar")


def first_ns(path, spec=None):
    spec = spec or BarReadSpec(required_fields=("close",), value_policy="close_strict")
    return read_bar_frame(path, spec).first_ns
