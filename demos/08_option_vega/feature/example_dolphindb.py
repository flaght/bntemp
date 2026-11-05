"""DolphinDB指数→DataFrame→自定义计算→标准特征文件；只读，不报单。"""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "demos.08_option_vega.feature"

from bomber.framework.datahub.features import DatasetRegistry, FeatureError, dataset_from_definition
from bomber.framework.dataprep.features import FeatureProviderFactory, FeatureRequest, export_features, load_feature_bundle
from bomber.framework.dataprep.sources.policy import DataSourcePurpose
from .dataframe import dataframe_to_records

CONFIG = Path(__file__).with_name("index.json")


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("时间须带时区，例如2026-10-22T00:00:00+08:00")
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def calculate_features(frame):
    """按输入行滚动，跨午休/跨日延续；公式和窗口规则由业务代码决定。"""
    result = frame.copy()
    result["ma20"] = result["close"].rolling(20, min_periods=20).mean()
    result["ma60"] = result["close"].rolling(60, min_periods=60).mean()
    result["index_blend"] = (result["ma20"] + result["ma60"]) / 2
    return result.dropna(subset=["ma20", "ma60", "index_blend"])


def run_example(*, start_ns, end_ns, output_dir, connection, config=CONFIG,
        dataset="index.minute", schema_version=1, key="000852", source_key=None,
        max_rows=1_000_000, availability=None):
    output_dir = Path(output_dir).expanduser().resolve()
    output = output_dir / "index-blend.csv"
    declaration = Path(str(output) + ".dataset.json")
    manifest = Path(str(output) + ".manifest.json")
    summary = output_dir / "summary.json"
    if any(path.exists() for path in (output, declaration, manifest, summary)):
        raise FeatureError("输出文件已存在，请另选输出目录")
    cutoff = time.time_ns()
    if not 0 <= start_ns <= end_ns <= cutoff:
        raise FeatureError("时间区间无效，结束时刻不能在未来")

    # 1. 通过连接配置、声明和公共Provider读取DolphinDB。
    bundle = load_feature_bundle(config, connection)
    binding = bundle.binding(dataset, schema_version)
    if binding.backend not in {"dolphindb", "dolphindb-raw"}:
        raise FeatureError("本例须使用DolphinDB来源声明")
    if binding.spec.frequency.period_ns != 60_000_000_000:
        raise FeatureError("本例输入须为一分钟指数数据")
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
    inputs = tuple(replace(record, key=key) for record in batch.records)

    # 2. inputs→DataFrame。元数据单独保存，业务计算只接收close。
    raw = pd.DataFrame({
        "ts_event": [record.ts_event for record in inputs],
        "revision": pd.Series([record.revision for record in inputs], dtype=object),
        "close": [record.payload.close for record in inputs],
        "source_ns": pd.Series([record.source_ns for record in inputs], dtype=object),
        "visible_ns": pd.Series([record.visible_ns for record in inputs], dtype=object),
        "quality_flags": pd.Series([record.quality_flags for record in inputs], dtype=object),
    })
    # 当前研究快照：同一业务分钟取最新修订；质量不合格时失败，不退回旧版。
    raw = raw.sort_values(["ts_event", "revision"]).drop_duplicates("ts_event", keep="last")
    raw.index = pd.DatetimeIndex(pd.to_datetime(raw.pop("ts_event"), unit="ns", utc=True)).tz_convert("Asia/Shanghai")
    raw.index.name = "ts_event"
    raw["close"] = raw["close"].astype(float)
    if not np.isfinite(raw["close"].to_numpy()).all() or (raw["close"] <= 0).any():
        raise FeatureError("指数close须为有限正价格")
    if any(flags & binding.spec.rejected_quality_flags for flags in raw["quality_flags"]):
        raise FeatureError("输入最新修订质量不合格")
    frame = raw[["close"]].copy()

    # 3. 自己计算DataFrame公式。
    result = calculate_features(frame)
    completed_ns = time.time_ns()
    if result.empty:
        raise FeatureError("不足60根记录，计算结果为空，未生成文件")
    if completed_ns < max(raw["visible_ns"]):
        raise FeatureError("计算完成时间早于输入可见时间")

    # 4. 声明输出字段；版本名与上面的按行滚动口径保持一致。
    definition = {
        "dataset_id": "research.index.ma20_ma60.minute", "schema_version": 1,
        "computation_version": "pandas-float64-sma20-sma60-half-row-window-v1",
        "frequency": {"period_ns": 60_000_000_000, "timezone": "Asia/Shanghai"},
        "alignment": "EXACT",
        "fields": [{"name": name, "kind": "float", "minimum": "0", "exclusive_minimum": True}
            for name in ("close", "ma20", "ma60", "index_blend")],
    }
    spec = dataset_from_definition(definition)

    # 5. 将每条结果依赖的最近60行源时间和质量传给通用转换器。
    # 元数据用object整数运算，避免rolling.max把纳秒转成float而丢精度。
    window_index = raw.index[59:]
    source_times = pd.Series(np.lib.stride_tricks.sliding_window_view(
        raw["source_ns"].to_numpy(dtype=object), 60).max(axis=1), index=window_index, dtype=object)
    qualities = pd.Series(np.bitwise_or.reduce(np.lib.stride_tricks.sliding_window_view(
        raw["quality_flags"].to_numpy(dtype=object), 60), axis=1), index=window_index, dtype=object)
    records = dataframe_to_records(result, spec, key=key,
        source=f"derived:ma20-ma60:{batch.source_identity}",
        available_ns=completed_ns, received_ns=completed_ns,
        source_ns=source_times.reindex(result.index), quality_flags=qualities.reindex(result.index))

    # 6. 标准导出：CSV和manifest由公共接口一起生成。
    export_features(output, DatasetRegistry((spec,)), records)
    with declaration.open("x", encoding="utf-8") as stream:
        json.dump({"format_version": 1, "datasets": [{"schema": definition,
            "source": {"backend": "file", "path": output.name, "require_manifest": True}}]},
            stream, ensure_ascii=False, indent=2)
        stream.write("\n")

    # 7. 从文件声明读回，核对记录、类型、时间及manifest。
    file_bundle = load_feature_bundle(declaration, None)
    with file_bundle.provider(spec.dataset_id, spec.schema_version) as provider:
        loaded = provider.read(FeatureRequest(spec.dataset_id, spec.schema_version, (key,),
            records[0].ts_event, records[-1].ts_event, time.time_ns(), max_rows=max_rows)).records
    if loaded != records:
        raise FeatureError("导出后文件读回记录不一致")
    report = {"status": "passed", "synthetic": False, "key": key, "source_key": request.keys[0],
        "input_rows": len(inputs), "feature_rows": len(records), "dataset_id": spec.dataset_id,
        "coverage": {"selected_input_rows": len(frame), "warmup_skipped": 59, "window_policy": "rows"},
        "input_source": batch.source_identity, "input_availability": getattr(source_config, "availability", "standard-envelope"),
        "output": str(output), "manifest": str(manifest), "dataset_config": str(declaration),
        "file_roundtrip": "passed", "first_minute": result.index[0].isoformat(),
        "last_minute": result.index[-1].isoformat(), "first_value": records[0].payload.index_blend,
        "visibility_note": "真实指数的当前计算快照，不是历史逐分钟可用性回放"}
    with summary.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--dataset", default="index.minute")
    parser.add_argument("--schema-version", type=int, default=1)
    parser.add_argument("--key", default="000852")
    parser.add_argument("--source-key")
    parser.add_argument("--start", type=timestamp, required=True)
    parser.add_argument("--end", type=timestamp, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--query-timeout", type=int, default=30)
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    parser.add_argument("--availability", choices=("observed", "source-time"))
    args = parser.parse_args(argv)
    if not args.connect:
        parser.error("读取DolphinDB须显式--connect，只读查询，不报单")
    if not 0 <= args.start <= args.end <= time.time_ns() or not 1 <= args.max_rows <= 1_000_000:
        parser.error("日期区间或max-rows无效")
    from dotenv import load_dotenv
    from bomber.framework.dolphindb import DolphinDbConnectionConfig
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    try:
        connection = DolphinDbConnectionConfig.from_env(purpose="market", read_timeout_seconds=args.query_timeout)
        report = run_example(start_ns=args.start, end_ns=args.end, output_dir=args.output_dir,
            connection=connection, config=args.config, dataset=args.dataset, schema_version=args.schema_version,
            key=args.key, source_key=args.source_key, max_rows=args.max_rows, availability=args.availability)
    except (ValueError, RuntimeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
