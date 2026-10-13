"""只读比较DolphinDB与Feather时段；可选核验同一calendars快照下的窗口和分钟。"""
import argparse
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time

from dotenv import load_dotenv

from bomber.framework.calendars import (
    NativeCalendarProvider, SessionBinding, SessionCalendar, TradingHoursSnapshot)
from bomber.framework.calendars.overrides import override_from_record
from bomber.framework.calendars.sessions import datetime_ns
from bomber.framework.dataprep.paths import resolve_paths
from bomber.framework.dataprep.sources import (
    DataSourcePurpose, DolphinDbReferenceConfig, ReferenceDataset, ReferenceQuery,
    ReferenceSourceFactory)
from bomber.framework.dataprep.sources.base import ReferenceBatch
from bomber.framework.dataprep.trading_hours import read_trading_hours

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class SourceParityMismatch(ValueError):
    """A failed comparison with normalized per-field evidence, never a fallback."""
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def row_differences(database_rows, file_rows, key_columns):
    def indexed(rows):
        return {tuple(row[column] for column in key_columns): dict(row) for row in rows}
    database, file = indexed(database_rows), indexed(file_rows)
    differences = []
    for key in sorted(database.keys() | file.keys()):
        left, right = database.get(key), file.get(key)
        if left == right:
            continue
        item = dict(zip(key_columns, key))
        if left is None or right is None:
            item.update(difference="missing_in_dolphindb" if left is None else "missing_in_feather",
                        dolphindb=left, feather=right)
        else:
            item.update(difference="changed", fields={
                column: {"dolphindb": left.get(column), "feather": right.get(column)}
                for column in sorted(left.keys() | right.keys()) if left.get(column) != right.get(column)})
        differences.append(item)
    return differences


def mismatch_report(args, config, live, offline, started, message):
    def evidence(rules):
        return {"hours_sha256": rules.hours.fingerprint, "hours_rows": len(rules.hours.rows),
                "overrides_sha256": rules.overrides.fingerprint if rules.overrides is not None else None,
                "overrides_rows": len(rules.overrides.rows) if rules.overrides is not None else None}
    return {"status": "failed", "failure": "source_content_mismatch", "message": message,
            "purpose": "trading_session_source_parity_readonly", "started_ns": started,
            "observed_ns": time.time_ns(), "product": args.product.upper(), "hours_code": args.code,
            "database": config.database, "table": config.hours_table,
            "file": str(args.trading_hours_file), "dolphindb": evidence(live), "feather": evidence(offline),
            "differences": {
                "trading_hours": row_differences(live.hours.records(), offline.hours.records(),
                                                 ("code", "segment_id")),
                "session_overrides": row_differences(
                    live.overrides.rows if live.overrides is not None else (),
                    offline.overrides.rows if offline.overrides is not None else (), ("code", "override_id"))},
            "sessions": {"status": "not_run"},
            "consistency_evidence": "two_equal_reads_not_transaction_snapshot",
            "revision_evidence": "static_content_hash_not_historical_revision_asof",
            "ctp_connected": False, "orders_submitted": 0}


@dataclass(frozen=True)
class RuleCapture:
    hours: TradingHoursSnapshot
    overrides: ReferenceBatch | None = None

    @property
    def identity(self):
        return (self.hours.fingerprint,
                None if self.overrides is None else self.overrides.fingerprint)


def capture(source, code, *, read_overrides=False):
    hours = read_trading_hours(source, (code,))
    overrides = (source.session_overrides(ReferenceQuery(products=(code,)))
                 if read_overrides else None)
    return RuleCapture(hours, overrides)


def minute_summary(stamps):
    return {"count": len(stamps), "first_ns": stamps[0] if stamps else None,
            "last_ns": stamps[-1] if stamps else None,
            "sha256": hashlib.sha256(json.dumps(stamps, separators=(",", ":")).encode()).hexdigest()}


def compare_sessions(live, offline, binding, dates, trading_days, history_minutes):
    def service(rules):
        overrides = (() if rules.overrides is None else
                     tuple(override_from_record(row) for row in rules.overrides.rows
                           if dates.start <= row["date"] <= dates.end))
        return SessionCalendar(rules.hours, dates, binding, overrides=overrides)

    calendars = [service(rules) for rules in (live, offline)]
    if calendars[0].provenance["sha256"] != calendars[1].provenance["sha256"]:
        raise ValueError("数据库与Feather的会话规则身份不一致")
    windows = []
    for day in trading_days:
        a, b = (calendar.windows(day) for calendar in calendars)
        if a != b:
            raise ValueError(f"{day}数据库与Feather交易窗口不一致")
        windows.extend(a)
    if not windows:
        raise ValueError("所选交易日没有交易窗口，无法验收分钟枚举")
    first = min(datetime_ns(window.start) for window in windows)
    before = max(datetime_ns(window.end) for window in windows)
    history = [calendar.last_minutes(before, history_minutes) for calendar in calendars]
    if history[0] != history[1]:
        raise ValueError("数据库与Feather历史分钟枚举不一致")
    missing = [calendar.missing_minutes(first - 1, before) for calendar in calendars]
    if missing[0] != missing[1]:
        raise ValueError("数据库与Feather缺口分钟枚举不一致")
    return {"status": "passed", "binding": {
                "product": binding.product, "hours_code": binding.hours_code,
                "exchange": binding.exchange, "asset_type": binding.asset_type,
                "timezone": binding.timezone},
            "calendar": dates.manifest, "rules_sha256": calendars[0].provenance["sha256"],
            "trading_days": [str(day) for day in trading_days],
            "windows": [{"trading_day": w.trading_day, "segment_id": w.segment_id,
                         "start": w.start.isoformat(), "end": w.end.isoformat()} for w in windows],
            "history_before_ns": before, "history_minutes": minute_summary(history[0]),
            "gap_after_ns": first - 1, "gap_before_ns": before,
            "gap_minutes": minute_summary(missing[0])}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--product", required=True)
    parser.add_argument("--hours-code", help="显式规则键；期权继承标的时必须声明，不猜测代码")
    parser.add_argument("--database", default=os.getenv("DDB_TRADING_HOURS_DATABASE", "dfs://bomber_static"))
    parser.add_argument("--trading-hours-table", default=os.getenv("DDB_TRADING_HOURS_TABLE", "fut_trading_session"))
    parser.add_argument("--trading-hours-file", type=Path,
                        help="默认资料目录中的fut_trading_session.feather，目录沿用公共ROLE_DIR解析")
    parser.add_argument("--trading-day", action="append", default=[],
                        help="可重复的YYYYMMDD查询日期；省略时仅对照时段内容，不作会话验收")
    parser.add_argument("--calendar-id", help="会话验收必填，例如china.cffex或china.sse")
    parser.add_argument("--session-timezone", default="Asia/Shanghai")
    parser.add_argument("--exchange", default="")
    parser.add_argument("--asset-type", choices=("FUTURE", "OPTION"), default="FUTURE")
    parser.add_argument("--history-minutes", type=int, default=600)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--session-overrides-table", default=os.getenv("DDB_SESSION_OVERRIDES_TABLE") or None)
    group.add_argument("--no-session-overrides", action="store_true")
    parser.add_argument("--session-overrides-file", type=Path)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--report", type=Path, help="可选保存本次报告，不覆盖已有文件")
    args = parser.parse_args(argv)
    if not args.connect:
        parser.error("须显式--connect允许只读数据库连接；本入口不连接CTP、不报单")
    try:
        args.code = ReferenceQuery(products=(args.hours_code or args.product,)).products[0]
        args.days = tuple(sorted(set(datetime.strptime(day, "%Y%m%d").date() for day in args.trading_day)))
        if any(len(day) != 8 or datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d") != day
               for day in args.trading_day):
            raise ValueError("查询日期须为YYYYMMDD")
        if args.days and (args.days[-1] - args.days[0]).days > 31:
            raise ValueError("一次会话验收的查询日期跨度不能超过31天")
        if not 1 <= args.timeout <= 120 or not 1 <= args.history_minutes <= 10000:
            raise ValueError("超时须为1至120秒；历史分钟须为1至10000")
        if args.trading_hours_file is None:
            paths = resolve_paths(project_root=PROJECT_ROOT, validate=False)
            if paths.role is None:
                raise ValueError("须配置ROLE_DIR等资料目录或--trading-hours-file")
            args.trading_hours_file = paths.role / "fut_trading_session.feather"
        args.trading_hours_file = args.trading_hours_file.expanduser().resolve()
        if not args.trading_hours_file.is_file():
            raise ValueError(f"时段Feather不存在: {args.trading_hours_file}")
        if args.no_session_overrides and (args.session_overrides_table or args.session_overrides_file):
            raise ValueError("无例外声明与例外来源不能混用")
        if bool(args.session_overrides_table) != bool(args.session_overrides_file):
            raise ValueError("日期例外对照须同时提供数据库表和Feather文件")
        if args.session_overrides_file is not None:
            args.session_overrides_file = args.session_overrides_file.expanduser().resolve()
            if not args.session_overrides_file.is_file():
                raise ValueError("例外Feather不存在")
        if args.days:
            if not args.calendar_id or not (args.no_session_overrides or args.session_overrides_table):
                raise ValueError("会话验收须指定calendar-id，以及例外来源或已确认的no-session-overrides")
            args.calendar_id = NativeCalendarProvider(args.calendar_id).calendar_id
            args.binding = SessionBinding(args.product, args.code, args.calendar_id,
                args.session_timezone, args.exchange, args.asset_type)
        else:
            args.binding = None
        if args.report is not None:
            args.report = args.report.expanduser().resolve()
            if args.report.exists():
                raise ValueError("报告已存在，请使用新路径保留旧证据")
    except (ValueError, TypeError) as error:
        parser.error(str(error))
    return args


def probe(args, config, *, factory=ReferenceSourceFactory, calendar_provider=None):
    dates = None
    if args.days:
        dates = (calendar_provider or NativeCalendarProvider(args.calendar_id)).snapshot()
        dates.require_range(args.days[0], args.days[-1])
        if any(not dates.is_trading_day(day) for day in args.days):
            raise ValueError("会话验收查询日期必须是日历中的交易日")
    read_overrides = bool(args.session_overrides_table)
    paths = {ReferenceDataset.TRADING_HOURS: args.trading_hours_file}
    if read_overrides:
        paths[ReferenceDataset.SESSION_OVERRIDES] = args.session_overrides_file
    started = time.time_ns()
    with factory.create_for(DataSourcePurpose.LIVE_REFERENCE, "dolphindb", config) as source:
        live = capture(source, args.code, read_overrides=read_overrides)
        with factory.create_for(DataSourcePurpose.OFFLINE_REFERENCE, "file", paths) as file_source:
            offline = capture(file_source, args.code, read_overrides=read_overrides)
        after = capture(source, args.code, read_overrides=read_overrides)
        if after.identity != live.identity:
            raise ValueError("核验期间数据库规则发生变化，本次结果无效，请重新读取")
        if live.hours.fingerprint != offline.hours.fingerprint:
            message = "数据库与Feather时段内容不一致，不能认定同一规则版本"
            raise SourceParityMismatch(message, mismatch_report(args, config, live, offline, started, message))
        if live.identity != offline.identity:
            message = "数据库与Feather日期例外内容不一致"
            raise SourceParityMismatch(message, mismatch_report(args, config, live, offline, started, message))
        sessions = (compare_sessions(live, offline, args.binding, dates, args.days, args.history_minutes)
                    if args.days else {"status": "not_requested"})
    result = {"status": "passed", "purpose": "trading_session_source_parity_readonly",
              "observed_ns": time.time_ns(), "started_ns": started,
              "product": args.product.upper(), "hours_code": args.code,
              "database": config.database, "table": config.hours_table,
              "file": str(args.trading_hours_file), "rows": len(live.hours.rows),
              "hours_sha256": live.hours.fingerprint, "hours_records": live.hours.records(),
              "overrides": {"evidence": ("compared_sources" if read_overrides else
                    "declared_empty" if args.no_session_overrides else "not_checked"),
                    "rows": len(live.overrides.rows) if read_overrides else None,
                    "sha256": live.overrides.fingerprint if read_overrides else None},
              "sessions": sessions, "consistency_evidence": "two_equal_reads_not_transaction_snapshot",
              "revision_evidence": "static_content_hash_not_historical_revision_asof",
              "ctp_connected": False, "orders_submitted": 0}
    return result


def main(argv=None):
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args(argv)
    config = replace(DolphinDbReferenceConfig.from_env(database=args.database,
                     read_timeout_seconds=args.timeout), hours_table=args.trading_hours_table,
                     overrides_table=args.session_overrides_table)
    failed = False
    try:
        result = probe(args, config)
    except SourceParityMismatch as error:
        result, failed = error.report, True
    output = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as handle:
            handle.write(output + "\n")
    print(output, flush=True)
    if failed:
        raise SystemExit(1)
    return result


if __name__ == "__main__":
    main()
