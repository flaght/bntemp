"""Read trading-hours snapshots through existing reference sources; no sidecar file."""
from datetime import date
from pathlib import Path

from bomber.framework.calendars.hours import parse_trading_hours
from bomber.framework.calendars.sessions import SessionBinding, SessionCalendar, SessionOverride
from bomber.framework.calendars.hours import SessionSegment
from bomber.framework.calendars.trading import NativeCalendarProvider, ExportedCalendarProvider
from .sources import ReferenceQuery, ReferenceDataset, ReferenceSourceFactory, DolphinDbReferenceConfig
from .sources.policy import validate_source


def read_trading_hours(source, codes):
    """The caller owns open/close; database and Feather use the exact same parser."""
    codes = tuple(codes)
    batch = source.trading_hours(ReferenceQuery(products=codes))
    snapshot = parse_trading_hours(batch.frame(), source=batch.source)
    for code in codes:
        snapshot.segments(code)
    return snapshot


def validate_session_config(data, config_path, *, product=None, purpose=None):
    """Check assembly declarations without reading a database or environment credentials."""
    if data.get("schema_version") != 2:
        raise ValueError("标准时段装配须为schema_version=2")
    name = data["product"].upper()
    if product is not None and name != product.upper():
        raise ValueError("会话配置品种与请求不一致")
    calendar_config = data["calendar"]
    first, last = date.fromisoformat(calendar_config["start"]), date.fromisoformat(calendar_config["end"])
    if first > last:
        raise ValueError("日期日历覆盖范围无效")
    if calendar_config["backend"] not in {"native", "file"}:
        raise ValueError("日期日历backend须为native或file")
    if calendar_config["backend"] == "native":
        NativeCalendarProvider(calendar_config["calendar_id"])
        from bomber.framework.calendars.trading import RULES_START, RULES_END
        if first < RULES_START or last > RULES_END:
            raise ValueError("日期日历覆盖超出已维护范围")
    elif not (Path(config_path).resolve().parent / calendar_config["path"]).is_file():
        raise ValueError("日期日历导出文件不存在")
    binding = SessionBinding(product=name, calendar_id=calendar_config["calendar_id"], **data["binding"])
    spec = data["hours"]
    if spec["backend"] not in {"file", "dolphindb"}:
        raise ValueError("时段来源backend须为file或dolphindb；MongoDB尚未实现")
    if purpose is not None:
        validate_source(spec["backend"], purpose)
    if spec["backend"] == "file" and not (Path(config_path).resolve().parent / spec["path"]).is_file():
        raise ValueError("交易时段Feather文件不存在")
    overrides = []
    for row in data["overrides"]:
        values = dict(row)
        values["date"] = date.fromisoformat(values["date"])
        if not first <= values["date"] <= last:
            raise ValueError("例外日期超出日历覆盖范围")
        values["segments"] = tuple(SessionSegment(**s) for s in values.get("segments", ()))
        overrides.append(SessionOverride(**values))
    return binding, first, last, tuple(overrides)


def load_session_config(data, config_path, *, product=None, purpose=None):
    """Explicit v2 assembly description, independent from the trading_hours file itself."""
    binding, first, last, overrides = validate_session_config(data, config_path, product=product, purpose=purpose)
    base = Path(config_path).resolve().parent
    calendar_config = data["calendar"]
    backend = calendar_config["backend"]
    if backend == "native":
        provider = NativeCalendarProvider(calendar_config["calendar_id"])
    elif backend == "file":
        provider = ExportedCalendarProvider(base / calendar_config["path"])
    else:
        raise ValueError("日期日历backend须为native或file")
    calendar = provider.snapshot(first, last)
    if calendar.calendar_id != calendar_config["calendar_id"]:
        raise ValueError("日期日历导出与配置身份不一致")
    spec = data["hours"]
    if spec["backend"] == "file":
        config = {ReferenceDataset.TRADING_HOURS: base / spec["path"]}
    elif spec["backend"] == "dolphindb":
        from dataclasses import replace
        config = DolphinDbReferenceConfig.from_env(database=spec.get("database"))
        config = replace(config, hours_table=spec.get("table", config.hours_table))
    else:
        raise ValueError("时段来源backend须为file或dolphindb；MongoDB尚未实现")
    source = (ReferenceSourceFactory.create(spec["backend"], config) if purpose is None else
              ReferenceSourceFactory.create_for(purpose, spec["backend"], config))
    with source:
        hours = read_trading_hours(source, (binding.hours_code,))
    return SessionCalendar(hours, calendar, binding, overrides=overrides)
