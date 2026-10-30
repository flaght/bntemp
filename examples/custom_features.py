import time
import hashlib
import json
from collections import deque
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from bomber.framework.dolphindb import DolphinDbConnectionConfig
from bomber.framework.dataprep.sources.dolphindb import DolphinDbReferenceConfig
from bomber.framework.datahub.features import dataset_from_definition, DatasetRegistry, ExternalDataRecord
from bomber.framework.dataprep.features import load_feature_bundle,FeatureRequest,export_features,encode_record



MINUTE_NS = 60_000_000_000


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("时间须带时区，例如2026-10-26T00:00:00+08:00")
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000

def moving_average_definition(window):
    """计算算法与Schema共用窗口参数；普通Payload仍由声明生成。"""
    if type(window) is not int or not 1 <= window <= 10000:
        raise ValueError("MA窗口须为1—10000的整数")
    positive = {"kind": "decimal", "minimum": "0", "exclusive_minimum": True}
    return {
        "dataset_id": f"research.index.ma{window}.minute", "schema_version": 1,
        "computation_version": f"decimal34-sma{window}-consecutive-minute-reset-v1",
        "frequency": {"period_ns": MINUTE_NS, "timezone": "Asia/Shanghai"},
        "alignment": "EXACT",
        "fields": [{"name": "close", **positive}, {"name": f"ma{window}", **positive},
                   {"name": "window", "kind": "int", "minimum": str(window), "maximum": str(window)}],
    }

def _prepare_ma_records(records, input_spec, key):
    """输入准备：选最新修订、排序，并检查频率、质量和价格。"""
    if input_spec.frequency.period_ns != MINUTE_NS:
        raise ValueError("本例输入须为一分钟频率")
    selected = {}
    for record in records:
        if (record.dataset_id, record.schema_version, record.key) != (
                input_spec.dataset_id, input_spec.schema_version, key):
            raise ValueError("MA输入数据集、版本或业务键不一致")
        previous = selected.get(record.ts_event)
        if previous is None or record.revision > previous.revision:
            selected[record.ts_event] = record
        elif record.revision == previous.revision and record != previous:
            raise ValueError("同一输入修订存在冲突")
    ordered = sorted(selected.values(), key=lambda record: record.ts_event)
    for record in ordered:
        input_spec.frequency.close_ns(record.ts_event)
        if record.quality_flags & input_spec.rejected_quality_flags:
            raise ValueError("输入最新修订质量不合格，不能计算MA")
        closing = Decimal(str(record.payload.close))
        if not closing.is_finite() or closing <= 0:
            raise ValueError("输入close须为有限正数")
    return ordered


def _ma_source(ordered, input_spec, window):
    digest = hashlib.sha256(json.dumps(
        sorted({record.source for record in ordered})).encode()).hexdigest()
    return f"derived:ma{window}:{input_spec.fingerprint}:{digest}"


def _pack_ma_record(history, average, output_spec, key, source, clock_ns, last_completed):
    """把算好的数值包装成标准记录，保留时间、来源和质量约束。"""
    record, closing = history[-1]
    window = len(history)
    payload = output_spec.payload_type(**{"close": closing, f"ma{window}": average, "window": window})
    completed = clock_ns()
    if completed < max(item.visible_ns for item, _ in history) or completed < last_completed:
        raise ValueError("计算时钟回退或输入尚未可见，不能回填MA发布时间")
    flags = 0
    for item, _ in history:
        flags |= item.quality_flags
    return ExternalDataRecord(
        dataset_id=output_spec.dataset_id, schema_version=output_spec.schema_version,
        key=key, payload=payload, ts_event=record.ts_event,
        available_ns=completed, source_ns=max(item.source_ns for item, _ in history),
        revision=completed, source=source, quality_flags=flags, received_ns=completed,
    )


def moving_average_records(records, input_spec, output_spec, key, window, *, clock_ns=time.time_ns):
    """主流程：准备行情 → 滚动计算MA → 封装特征。"""
    ordered = _prepare_ma_records(records, input_spec, key)
    source = _ma_source(ordered, input_spec, window)
    history, derived, gaps = deque(maxlen=window), [], []
    last_event, last_day, warmup = None, None, 0
    zone = ZoneInfo(input_spec.frequency.timezone)

    for record in ordered:
        # 缺分钟、午休间隔或跨日，从一个新窗口开始。
        day = datetime.fromtimestamp(record.ts_event // 1_000_000_000, zone).date()
        if last_event is not None and (record.ts_event - last_event != MINUTE_NS or day != last_day):
            gaps.append({"previous_event_ns": last_event, "next_event_ns": record.ts_event})
            history.clear()
        closing = Decimal(str(record.payload.close))
        history.append((record, closing))
        last_event, last_day = record.ts_event, day
        if len(history) < window:
            warmup += 1
            continue

        # MA只需要这一行求均值，使用Decimal的34位有效数字。
        with localcontext() as context:
            context.prec = 34
            average = sum((close for _, close in history), Decimal(0)) / Decimal(window)
        last_completed = derived[-1].available_ns if derived else -1
        derived.append(_pack_ma_record(history, average, output_spec, key, source, clock_ns, last_completed))

    return tuple(derived), {"selected_input_rows": len(ordered), "warmup_skipped": warmup,
                            "window_resets": len(gaps), "gaps": gaps}

def export_moving_average(window, config, dataset, 
                schema_version, key, start, end, max_rows, source_key=None):
    load_dotenv()
    config = Path(config).expanduser().resolve()
    query_key = key if source_key is None else source_key
    output = Path(__file__).resolve().parent / "temp" / f"index_{key}_ma{window}.csv"
    dataset_path = Path(str(output) + ".dataset.json")
    index_output = output.with_name(f"index_{key}_1min.csv")
    index_dataset_path = Path(str(index_output) + ".dataset.json")
    # 两类输出均拒绝覆盖，提前检查，避免导出一半才遇到旧文件。
    for data_path in (output, index_output):
        for path in (data_path, Path(str(data_path) + ".manifest.json"),
                     Path(str(data_path) + ".dataset.json")):
            if path.exists():
                raise ValueError(f"输出已存在，请更换输出路径或保留旧文件后另选目录: {path}")
    definition = moving_average_definition(window)

    output_spec = dataset_from_definition(definition)

    output_registry = DatasetRegistry((output_spec,))

    # 保留market连接参数，只转换成旧版原表Provider接受的配置类型。
    market_connection = DolphinDbConnectionConfig.from_env(purpose="market", read_timeout_seconds=30)
    connection = DolphinDbReferenceConfig(
        host=market_connection.host,
        port=market_connection.port,
        username=market_connection.username,
        password=market_connection.password,
        read_timeout_seconds=30,
        enable_ssl=market_connection.enable_ssl,
    )

    bundle = load_feature_bundle(config, connection)

    binding = bundle.binding(dataset, schema_version)
    input_spec = binding.spec
    if binding.bar_projection is None:
        raise ValueError("同时供08使用时，源配置须包含完整OHLCV及bar_projection")
    # 保留输入声明，原始指数文件与08使用相同的Schema和计算版本。
    input_definition = next(item for item in json.loads(config.read_text(encoding="utf-8"))["datasets"]
        if (item["schema"]["dataset_id"], item["schema"]["schema_version"]) == (dataset, schema_version))

    request = FeatureRequest(dataset, schema_version, (query_key,),
        start, end, time.time_ns(), max_rows=max_rows)
    

    source_config = bundle.binding(dataset, schema_version).source_config
    print("DolphinDB:", connection.host, connection.port, flush=True)
    print("库表:", source_config.database, source_config.table, flush=True)
    print("指数代码: 查询", query_key, "-> 导出", key, flush=True)
    
    ### 查询数据
    with bundle.provider(dataset, schema_version, purpose="feature_export") as provider:
        batch = provider.read(request)
        received = time.time_ns()
    
    if not batch.records:
        raise ValueError("指定区间没有指数行情")

    # 源stockID使用SH000852；文件使用策略显式绑定的000852，不按名称猜测。
    inputs = tuple(replace(row, key=key, received_ns=max(received, row.received_ns or 0))
                   for row in batch.records)
    ## 衍生计算
    records, coverage = moving_average_records(inputs, input_spec, output_spec, key, window)

    if not records:
        raise ValueError(f"没有足够的连续{window}根分钟行情，无法导出MA{window}")

    # 原始指数使用输入注册表，全部分钟保留，不受MA预热裁剪。
    index_manifest = export_features(index_output, bundle.registry, inputs)
    index_file_definition = {"format_version": 1, "datasets": [{
        **input_definition,
        "source": {"backend": "file", "path": index_output.name, "require_manifest": True}
    }]}
    with index_dataset_path.open("x", encoding="utf-8") as stream:
        json.dump(index_file_definition, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    index_replay_bundle = load_feature_bundle(index_dataset_path, None)
    index_replay_request = FeatureRequest(dataset, schema_version, (key,),
        min(row.ts_event for row in inputs), max(row.ts_event for row in inputs),
        time.time_ns(), max_rows=max_rows)
    with index_replay_bundle.provider(dataset, schema_version) as file_provider:
        if file_provider.read(index_replay_request).records != inputs:
            raise ValueError("原始指数导出与文件逐记录读回不一致")

    # MA使用自己的输出注册表，与原始指数分开保存。
    manifest = export_features(output, output_registry, records)

    file_definition = {"format_version": 1, "datasets": [{"schema": definition,
        "source": {"backend": "file", "path": output.name, "require_manifest": True}}]}

    with dataset_path.open("x", encoding="utf-8") as stream:
        json.dump(file_definition, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    replay_bundle = load_feature_bundle(dataset_path, None)
    replay_request = FeatureRequest(output_spec.dataset_id, 1, (key,),
        records[0].ts_event, records[-1].ts_event, time.time_ns(), max_rows=max_rows)
    with replay_bundle.provider(output_spec.dataset_id) as file_provider:
        reloaded = file_provider.read(replay_request).records
    if reloaded != records:
        raise ValueError("MA导出与文件逐记录读回不一致")
   
    report = {"status": "passed", "key": key, "source_key": query_key, "input_dataset": dataset,
        "index_export": {
            "output": str(index_output), "dataset_config": str(index_dataset_path),
            "dataset_id": dataset, "schema_version": schema_version, "rows": len(inputs),
            "manifest": index_manifest, "file_roundtrip": "passed",
            "availability_note": "保留原始实际可用时间；格式读回成功不代表历史决策时已可见"
        },
        "input_rows": len(inputs), "input_schema_fingerprint": input_spec.fingerprint,
        "input_source": batch.source_identity,
        "input_records_sha256": hashlib.sha256(json.dumps([encode_record(row) for row in inputs],
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        "input_config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "dataset_id": output_spec.dataset_id, "window": window, "rows": len(records),
        **coverage, "output": str(output), "dataset_config": str(dataset_path),
        "manifest": manifest, "file_roundtrip": "passed",
        "availability": "actual_calculation_completion_after_input_observation",
        "preview": [asdict(row) for row in records[-3:]]}
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str, allow_nan=False), flush=True)
    return report



if __name__ == "__main__":
    export_moving_average(
        window=10,
        config=Path(__file__).resolve().with_name("index_tinysoft.json"),
        dataset="index.minute",
        schema_version=3,
        key="000852",
        source_key="SH000852",
        start=timestamp("2026-10-30T00:00:00+08:00"),
        end=timestamp("2026-10-30T15:00:00+08:00"),
        max_rows=10000,
    )
