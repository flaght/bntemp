"""业务DataFrame转标准特征记录；不计算公式、不查询数据库、不推断可见时间。"""
import pandas as pd

from bomber.framework.datahub.features import DatasetRegistry, FeatureError
from bomber.framework.dataprep.features import decode_record


def _values(value, index, name):
    if isinstance(value, pd.Series):
        if not value.index.equals(index):
            raise FeatureError(f"{name}的Series索引须与DataFrame完全一致")
        return value.tolist()
    if not pd.api.types.is_scalar(value):
        raise FeatureError(f"{name}须为标量或按DataFrame索引对齐的Series")
    return [value] * len(index)


def dataframe_to_records(frame, spec, *, key, source, available_ns, source_ns,
        received_ns=None, revision=None, quality_flags=0):
    """转换声明字段，返回ExternalDataRecord元组；元数据支持标量或同索引Series。

    frame索引须为带时区DatetimeIndex，或命名ts_event的UTC Unix纳秒整数索引。
    列名对应spec.payload_fields；额外辅助列不导出。NaN/pd.NA仅在可空字段转None。
    时间字段均由调用方显式提供整数纳秒；revision省略时沿用available_ns。
    不排序、补值、计算预热或修改时间；导出仍使用公共export_features。
    """
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        raise FeatureError("特征输入须为列名唯一的DataFrame")
    fields = spec.payload_fields
    missing = {field.name for field in fields} - set(frame.columns)
    if missing:
        raise FeatureError(f"DataFrame缺少声明字段: {sorted(missing)}")
    if isinstance(frame.index, pd.DatetimeIndex):
        if frame.index.tz is None or frame.index.hasnans:
            raise FeatureError("DatetimeIndex须带显式时区且不得含NaT")
        events = [stamp.value for stamp in frame.index]  # Timestamp.value固定为UTC纳秒。
    else:
        if frame.index.name != "ts_event":
            raise FeatureError("整数时间索引须命名ts_event，单位为UTC Unix纳秒")
        events = frame.index.tolist()  # 类型/频率由公共decode_record严格核验，不将float转int。
    metadata = {name: _values(value, frame.index, name) for name, value in {
        "key": key, "source": source, "available_ns": available_ns, "source_ns": source_ns,
        "received_ns": received_ns, "revision": available_ns if revision is None else revision,
        "quality_flags": quality_flags}.items()}
    # 不使用iterrows，保留整数、Decimal与NumPy标量的列类型和纳秒精度。
    rows = frame[[field.name for field in fields]].to_dict(orient="records")
    registry, records, seen = DatasetRegistry((spec,)), [], set()
    for position, (event, payload) in enumerate(zip(events, rows)):
        envelope = {"dataset_id": spec.dataset_id, "schema_version": spec.schema_version,
            "ts_event": event, **{name: values[position] for name, values in metadata.items()}}
        if pd.api.types.is_scalar(envelope["received_ns"]) and pd.isna(envelope["received_ns"]):
            envelope["received_ns"] = None
        for field in fields:
            value = payload[field.name]
            if field.nullable and pd.api.types.is_scalar(value) and pd.isna(value):
                value = None
            envelope["payload."+field.name] = value
        record = decode_record(envelope, registry)
        if record.identity in seen:
            raise FeatureError("DataFrame含重复的数据集/业务键/业务时间/修订身份")
        seen.add(record.identity)
        records.append(record)
    return tuple(records)
