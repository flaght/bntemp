"""Standard special-date records, shared by DolphinDB and Feather adapters."""
from datetime import date, datetime
import json

from .hours import SessionSegment, required_text
from .sessions import SessionOverride

OVERRIDE_COLUMNS = ("code", "override_id", "date_basis", "date", "action", "target", "segments")


def override_from_record(row):
    day = row["date"]
    if isinstance(day, str):
        day = date.fromisoformat(day)
    elif isinstance(day, datetime):
        if day.tzinfo is not None or any((day.hour, day.minute, day.second, day.microsecond)):
            raise ValueError("例外date须为业务日期，不是带时间/时区的时间戳")
        day = day.date()
    if type(day) is not date:
        raise ValueError("例外date须为DATE或YYYY-MM-DD")
    parts = row["segments"]
    import pandas as pd
    if parts is None or (pd.api.types.is_scalar(parts) and pd.isna(parts)):
        parts = []
    elif isinstance(parts, str):
        parts = json.loads(parts)
    elif hasattr(parts, "tolist"):
        parts = parts.tolist()
    if not isinstance(parts, (list, tuple)):
        raise ValueError("例外segments须为分段对象列表或其JSON文本")
    return SessionOverride(row["date_basis"], day, row["action"], row["target"],
                           tuple(SessionSegment(**dict(part)) for part in parts))


def normalize_overrides(frame):
    import pandas as pd
    if (not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique
            or set(frame.columns) != set(OVERRIDE_COLUMNS)):
        raise ValueError("例外表须包含七个标准字段: " + ",".join(OVERRIDE_COLUMNS))
    rows, seen = [], set()
    for raw in frame.to_dict("records"):
        code = required_text(raw["code"], "code").upper()
        identity = required_text(raw["override_id"], "override_id")
        if (code, identity) in seen:
            raise ValueError("code/override_id重复，不能依赖读取顺序选择")
        seen.add((code, identity))
        override = override_from_record(raw)
        parts = []
        for segment in override.segments:
            value = segment.row(code)
            del value["code"]
            parts.append(value)
        rows.append(dict(code=code, override_id=identity, date_basis=override.date_basis,
                         date=override.date, action=override.action, target=override.target,
                         segments=json.dumps(parts, sort_keys=True, separators=(",", ":"))))
    return tuple(sorted(rows, key=lambda row: (row["code"], row["override_id"])))
