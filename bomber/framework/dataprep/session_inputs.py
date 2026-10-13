"""Direct session Provider assembly. No JSON calendar or manually declared trading dates."""
from dataclasses import dataclass, replace
from pathlib import Path
import os
import re

from bomber.framework.calendars import NativeCalendarProvider, SessionBinding, SessionCalendar
from bomber.framework.calendars.overrides import override_from_record
from .trading_hours import read_trading_hours
from .sources import (DataSourcePurpose, DolphinDbReferenceConfig, ReferenceDataset,
                      ReferenceQuery, ReferenceSourceFactory, validate_source)


@dataclass(frozen=True)
class SessionInputs:
    product: str
    calendar_id: str
    hours_code: str | None = None
    timezone: str = "Asia/Shanghai"
    exchange: str = ""
    asset_type: str = "FUTURE"
    backend: str = "dolphindb"
    hours_file: Path | None = None
    database: str | None = None
    hours_table: str | None = None
    overrides_file: Path | None = None
    overrides_table: str | None = None
    no_overrides: bool = False

    @property
    def binding(self):
        return SessionBinding(self.product, self.hours_code or self.product, self.calendar_id,
                              self.timezone, self.exchange, self.asset_type)


def validate_session_inputs(inputs, *, purpose=None, calendar_provider=None):
    if not isinstance(inputs, SessionInputs):
        raise TypeError("会话装配需要SessionInputs")
    binding = inputs.binding
    ReferenceQuery(products=(binding.hours_code,))
    if calendar_provider is None:
        NativeCalendarProvider(binding.calendar_id)
    if inputs.backend not in {"file", "dolphindb"}:
        raise ValueError("会话来源须为file或dolphindb，MongoDB尚未实现")
    validate_source(inputs.backend, purpose or (DataSourcePurpose.OFFLINE_REFERENCE
        if inputs.backend == "file" else DataSourcePurpose.LIVE_REFERENCE))
    if type(inputs.no_overrides) is not bool:
        raise ValueError("no_overrides须为显式布尔声明")
    if inputs.backend == "file":
        if inputs.hours_file is None or not Path(inputs.hours_file).is_file():
            raise ValueError("须提供存在的trading_hours.feather")
        if inputs.database is not None or inputs.hours_table is not None or inputs.overrides_table is not None:
            raise ValueError("文件会话来源不能混用数据库表配置")
        if inputs.overrides_file is not None and not Path(inputs.overrides_file).is_file():
            raise ValueError("会话例外Feather文件不存在")
    elif inputs.hours_file is not None or inputs.overrides_file is not None:
        raise ValueError("数据库会话来源不能混用本地规则文件")
    if inputs.database is not None and re.fullmatch(r"dfs://[A-Za-z0-9_/-]+", inputs.database) is None:
        raise ValueError("会话数据库须为安全dfs路径")
    for table in (inputs.hours_table, inputs.overrides_table):
        if table is not None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) is None:
            raise ValueError("会话表名须为普通标识符")
    explicit_source = inputs.overrides_file is not None or inputs.overrides_table is not None
    if inputs.no_overrides == explicit_source:
        raise ValueError("须配置会话例外来源，或显式声明no_overrides；二者不能同时使用")
    return binding


def build_session_calendar(source, binding, calendar_provider=None, *, overrides):
    """The caller owns source open/close; the date Provider declares its own coverage."""
    dates = (calendar_provider or NativeCalendarProvider(binding.calendar_id)).snapshot()
    hours = read_trading_hours(source, (binding.hours_code,))
    if overrides is None:
        raise ValueError("会话例外完整性未知，不能将缺失数据解释为没有例外")
    return SessionCalendar(hours, dates, binding, overrides=tuple(overrides))


def load_session_inputs(inputs, *, purpose=None, calendar_provider=None):
    binding = validate_session_inputs(inputs, purpose=purpose, calendar_provider=calendar_provider)
    dates = (calendar_provider or NativeCalendarProvider(binding.calendar_id)).snapshot()
    if dates.calendar_id != binding.calendar_id:
        raise ValueError("会话绑定与日期日历身份不一致")
    if inputs.backend == "file":
        config = {ReferenceDataset.TRADING_HOURS: inputs.hours_file}
        if inputs.overrides_file is not None:
            config[ReferenceDataset.SESSION_OVERRIDES] = inputs.overrides_file
    else:
        config = DolphinDbReferenceConfig.from_env(database=inputs.database)
        config = replace(config, hours_table=inputs.hours_table or config.hours_table,
                         overrides_table=inputs.overrides_table)
    actual_purpose = purpose or (DataSourcePurpose.OFFLINE_REFERENCE if inputs.backend == "file"
                                else DataSourcePurpose.LIVE_REFERENCE)
    with ReferenceSourceFactory.create_for(actual_purpose, inputs.backend, config) as source:
        hours = read_trading_hours(source, (binding.hours_code,))
        if inputs.no_overrides:
            overrides = ()
        else:
            batch = source.session_overrides(ReferenceQuery(products=(binding.hours_code,)))
            overrides = tuple(override_from_record(row) for row in batch.rows)
    # Only records within the Provider's declared scope participate in this snapshot.
    selected = tuple(o for o in overrides if dates.start <= o.date <= dates.end)
    return SessionCalendar(hours, dates, binding, overrides=selected)


def add_session_arguments(parser):
    group = parser.add_argument_group("会话资料：Provider获取时段，calendars判断交易日期")
    group.add_argument("--trading-calendar", type=Path,
                       help="显式旧JSON兼容入口；默认不使用任何日历JSON")
    group.add_argument("--calendar-id", help="原生日历标识，亦可配置BOMBER_CALENDAR_ID")
    group.add_argument("--hours-code", help="显式时段规则键；省略时使用product")
    group.add_argument("--session-timezone", default="Asia/Shanghai")
    group.add_argument("--trading-hours-file", type=Path, help="本地时段Feather；正式Live使用数据库")
    group.add_argument("--trading-hours-database", help="时段与例外DFS路径；省略时使用参考库配置")
    group.add_argument("--trading-hours-table", help="源时段表；亦可配置DDB_TRADING_HOURS_TABLE")
    group.add_argument("--session-overrides-file", type=Path, help="本地例外Feather，与本地时段配套")
    group.add_argument("--session-overrides-table", help="例外源表；亦可配置DDB_SESSION_OVERRIDES_TABLE")
    group.add_argument("--no-session-overrides", action="store_true",
                       help="明确声明所用规则范围内没有日期例外；不能代替缺失的例外资料")


def session_inputs_from_args(args, *, product=None, exchange=""):
    file_path = getattr(args, "trading_hours_file", None)
    return SessionInputs(
        product=product or args.product,
        calendar_id=getattr(args, "calendar_id", None) or os.getenv("BOMBER_CALENDAR_ID", ""),
        hours_code=getattr(args, "hours_code", None),
        timezone=getattr(args, "session_timezone", "Asia/Shanghai"), exchange=exchange,
        backend="file" if file_path is not None else "dolphindb", hours_file=file_path,
        database=(getattr(args, "trading_hours_database", None) or
                  getattr(args, "reference_database", None)),
        hours_table=getattr(args, "trading_hours_table", None),
        overrides_file=getattr(args, "session_overrides_file", None),
        overrides_table=(getattr(args, "session_overrides_table", None) or
                         ((os.getenv("DDB_SESSION_OVERRIDES_TABLE") or None) if file_path is None else None)),
        no_overrides=getattr(args, "no_session_overrides", False))


def validate_session_arguments(args, *, product=None, purpose=None):
    """CLI preflight is purely local: no credentials, database session or dataset read."""
    legacy = getattr(args, "trading_calendar", None)
    if legacy is not None:
        if any(getattr(args, name, None) for name in (
            "calendar_id", "hours_code", "trading_hours_file", "trading_hours_database",
            "trading_hours_table", "session_overrides_file", "session_overrides_table", "no_session_overrides")):
            raise ValueError("旧JSON兼容入口与直接Provider配置不能混用")
        from bomber.framework.trader.runtime.trading_sessions import validate_sessions
        validate_sessions(legacy, product or args.product, purpose=purpose)
    else:
        validate_session_inputs(session_inputs_from_args(args, product=product), purpose=purpose)
