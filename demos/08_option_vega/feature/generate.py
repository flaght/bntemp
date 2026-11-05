"""08自定义特征例子：只读查询一次DolphinDB，导出原始指数和MA标准文件包。"""
import argparse
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from bomber.framework.datahub.features import Alignment, DatasetRegistry, ExternalDataRecord, FeatureError, dataset_from_definition
from bomber.framework.dataprep.features import FeatureProviderFactory, FeatureRequest, encode_record, export_features, load_feature_bundle
from bomber.framework.dataprep.sources.policy import DataSourcePurpose

CONFIG = Path(__file__).with_name("index.json")
MINUTE_NS = 60_000_000_000


def timestamp(value):
    """带时区ISO时间转整数纳秒，不经浮点数。"""
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("时间须带时区，例如2026-09-01T00:00:00+08:00")
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


def moving_average_records(records, input_spec, output_spec, key, window, *, clock_ns=time.time_ns):
    """以当前可见最新修订计算研究快照；缺分钟/跨日重置，不伪造历史可用性。"""
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
    source_digest = hashlib.sha256(json.dumps(
        sorted({record.source for record in ordered})).encode()).hexdigest()
    source = f"derived:ma{window}:{input_spec.fingerprint}:{source_digest}"
    history, derived, gaps = deque(maxlen=window), [], []
    last_event, last_day, last_completed, warmup = None, None, -1, 0
    for record in ordered:
        input_spec.frequency.close_ns(record.ts_event)
        if record.quality_flags & input_spec.rejected_quality_flags:
            raise ValueError("输入最新修订质量不合格，不能计算MA")
        day = datetime.fromtimestamp(record.ts_event // 1_000_000_000,
            ZoneInfo(input_spec.frequency.timezone)).date()
        if last_event is not None and (record.ts_event - last_event != MINUTE_NS or day != last_day):
            gaps.append({"previous_event_ns": last_event, "next_event_ns": record.ts_event})
            history.clear()
        closing = Decimal(str(record.payload.close))
        if not closing.is_finite() or closing <= 0:
            raise ValueError("输入close须为有限正数")
        history.append((record, closing))
        last_event, last_day = record.ts_event, day
        if len(history) < window:
            warmup += 1
            continue
        with localcontext() as context:
            context.prec = 34
            average = sum((close for _, close in history), Decimal(0)) / Decimal(window)
        payload = output_spec.payload_type(**{"close": closing, f"ma{window}": average, "window": window})
        completed = clock_ns()
        if completed < max(item.visible_ns for item, _ in history) or completed < last_completed:
            raise ValueError("计算时钟回退或输入尚未可见，不能回填MA发布时间")
        flags = 0
        for item, _ in history:
            flags |= item.quality_flags
        derived.append(ExternalDataRecord(output_spec.dataset_id, output_spec.schema_version, key,
            payload, record.ts_event, completed, max(item.source_ns for item, _ in history),
            completed, source, flags, completed))
        last_completed = completed
    return tuple(derived), {"selected_input_rows": len(ordered), "warmup_skipped": warmup,
                            "window_resets": len(gaps), "gaps": gaps}


def _export_and_check(path, definition, registry, records, key, max_rows):
    """公共导出负责manifest；本例只保存可移动声明并逐记录核对。"""
    manifest = export_features(path, registry, records)
    config = Path(str(path) + ".dataset.json")
    definition = {**definition, "source": {
        "backend": "file", "path": path.name, "require_manifest": True}}
    with config.open("x", encoding="utf-8") as stream:
        json.dump({"format_version": 1, "datasets": [definition]}, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    bundle = load_feature_bundle(config, None)
    first = records[0]
    request = FeatureRequest(first.dataset_id, first.schema_version, (key,),
        min(row.ts_event for row in records), max(row.ts_event for row in records),
        time.time_ns(), max_rows=max_rows)
    with bundle.provider(first.dataset_id, first.schema_version) as provider:
        loaded = provider.read(request).records
    if sorted(loaded, key=lambda row: row.identity) != sorted(records, key=lambda row: row.identity):
        raise FeatureError("特征导出后逐记录读回不一致")
    return {"output": str(path), "dataset_config": str(config), "manifest": str(path) + ".manifest.json",
        "dataset_id": first.dataset_id, "schema_version": first.schema_version,
        "rows": len(records), "file_roundtrip": "passed", "sha256": manifest["sha256"]}


def generate_features(*, start_ns, end_ns, output_dir, connection, config=CONFIG,
        dataset="index.minute", schema_version=1, key="000852", source_key=None,
        window=20, max_rows=1_000_000, availability=None):
    """显式注入连接；一次查询，原始记录与MA分别导出；不连接交易柜台。"""
    definition = moving_average_definition(window)
    if type(max_rows) is not int or not window <= max_rows <= 1_000_000:
        raise FeatureError("须满足window <= max_rows <= 1000000")
    cutoff = time.time_ns()
    if not 0 <= start_ns <= end_ns <= cutoff or not key or source_key == "":
        raise FeatureError("时间区间或指数代码无效，结束时刻不能在未来")
    output_dir = Path(output_dir).expanduser().resolve()
    paths = (output_dir / "index.csv", output_dir / f"index-ma{window}.csv")
    for path in paths:
        for candidate in (path, Path(str(path) + ".manifest.json"), Path(str(path) + ".dataset.json")):
            if candidate.exists():
                raise FeatureError(f"输出已存在，请另选目录: {candidate}")
    report_path = output_dir / "summary.json"
    if report_path.exists():
        raise FeatureError("summary.json已存在，请另选输出目录")
    config = Path(config).expanduser().resolve()
    bundle = load_feature_bundle(config, connection)
    binding = bundle.binding(dataset, schema_version)
    if (binding.backend not in {"dolphindb", "dolphindb-raw"}
            or binding.spec.frequency.period_ns != MINUTE_NS
            or binding.spec.default_alignment is not Alignment.EXACT
            or binding.bar_projection is None):
        raise FeatureError("本例须使用DolphinDB一分钟EXACT指数及真实OHLCV投影")
    if not any(field.name == "close" and field.kind in {"decimal", "float", "int"}
               and not field.nullable for field in binding.spec.payload_fields):
        raise FeatureError("MA输入须声明非空数值close")
    source_config = binding.source_config
    if availability is not None:
        if binding.backend != "dolphindb-raw" or availability not in {"observed", "source-time"}:
            raise FeatureError("可见时间覆盖仅支持原表observed或source-time")
        source_config = replace(source_config, availability=availability)
    request = FeatureRequest(dataset, schema_version, (key if source_key is None else source_key,),
        start_ns, end_ns, cutoff, max_rows=max_rows)
    with FeatureProviderFactory.create_for(DataSourcePurpose.FEATURE_EXPORT,
            binding.backend, source_config, bundle.registry, **binding.provider_options) as provider:
        batch = provider.read(request)
    if not batch.records:
        raise FeatureError("指定区间没有可见指数记录，未生成文件")
    # 显式业务键映射；保留源可用时间、接收时间和全部修订，原始文件不裁剪预热。
    inputs = tuple(replace(row, key=key) for row in batch.records)
    output_spec = dataset_from_definition(definition)
    records, coverage = moving_average_records(inputs, binding.spec, output_spec, key, window)
    if not records:
        raise FeatureError(f"没有足够的连续{window}根分钟Bar，未生成文件")
    input_definition = next(item for item in json.loads(config.read_text(encoding="utf-8"))["datasets"]
        if (item["schema"]["dataset_id"], item["schema"]["schema_version"]) == (dataset, schema_version))
    index_report = _export_and_check(paths[0], input_definition, bundle.registry, inputs, key, max_rows)
    ma_report = _export_and_check(paths[1], {"schema": definition}, DatasetRegistry((output_spec,)),
        records, key, max_rows)
    report = {"status": "passed", "key": key, "source_key": request.keys[0], "window": window,
        "input_source": batch.source_identity, "input_schema_fingerprint": binding.spec.fingerprint,
        "input_records_sha256": hashlib.sha256(json.dumps([encode_record(row) for row in inputs],
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        "input_config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "index": index_report, "ma": ma_report, **coverage,
        "index_availability": getattr(source_config, "availability", "standard-envelope"),
        "ma_availability": "actual_calculation_completion_after_input_observation",
        "visibility_note": "MA为当前可见最新修订的研究快照，不是历史逐分钟可用性回放"}
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true", help="允许只读查询DolphinDB")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--dataset", default="index.minute")
    parser.add_argument("--schema-version", type=int, default=1)
    parser.add_argument("--key", default="000852", help="导出后策略使用的业务键")
    parser.add_argument("--source-key", help="源表业务键；省略与key相同，例如SH000852")
    parser.add_argument("--start", type=timestamp, required=True)
    parser.add_argument("--end", type=timestamp, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--window", type=int, default=20)
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    parser.add_argument("--query-timeout", type=int, default=30)
    parser.add_argument("--availability", choices=("observed", "source-time"),
        help="默认沿用声明；source-time须确认发布时间的历史含义")
    args = parser.parse_args(argv)
    if not args.connect:
        parser.error("导出须显式--connect，只读查询，不报单")
    if not (0 <= args.start <= args.end <= time.time_ns()
            and 1 <= args.window <= 10000 and args.window <= args.max_rows <= 1_000_000):
        parser.error("日期区间、window或max-rows无效")
    from dotenv import load_dotenv
    from bomber.framework.dolphindb import DolphinDbConnectionConfig
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    try:
        connection = DolphinDbConnectionConfig.from_env(purpose="market", read_timeout_seconds=args.query_timeout)
        report = generate_features(start_ns=args.start, end_ns=args.end, output_dir=args.output_dir,
            connection=connection, config=args.config, dataset=args.dataset,
            schema_version=args.schema_version, key=args.key, source_key=args.source_key,
            window=args.window, max_rows=args.max_rows, availability=args.availability)
    except (ValueError, RuntimeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()


