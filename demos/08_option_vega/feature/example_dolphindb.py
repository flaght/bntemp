"""真实DolphinDB指数→自写DataFrame公式→标准特征文件；只读，不报单。"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "demos.08_option_vega.feature"

from bomber.framework.datahub.features import DatasetRegistry, FeatureError, dataset_from_definition
from bomber.framework.dataprep.features import FeatureProviderFactory, FeatureRequest, load_feature_bundle
from bomber.framework.dataprep.sources.policy import DataSourcePurpose
from .generate import CONFIG, _export_and_check, moving_average_records, timestamp


def calculate_blend(frame, window):
    """自己编写的业务公式：入参/返回值都是DataFrame，最大依赖窗口为60。"""
    import pandas as pd

    close = frame["close"].astype(float)
    groups = close.groupby(frame["segment_id"])
    ma20 = groups.transform(lambda values: values.rolling(20, min_periods=20).mean())
    ma60 = groups.transform(lambda values: values.rolling(window, min_periods=window).mean())
    return pd.DataFrame({"close": close, "ma20": ma20, "ma60": ma60,
        "index_blend": (ma20 + ma60) / 2}, index=frame.index).dropna()


def run_example(*, start_ns, end_ns, output_dir, connection, config=CONFIG,
        dataset="index.minute", schema_version=1, key="000852", source_key=None,
        max_rows=1_000_000, availability=None):
    output_dir = Path(output_dir).expanduser().resolve()
    output = output_dir / "index-blend.csv"
    summary = output_dir / "summary.json"
    if any(path.exists() for path in (output, Path(str(output)+".manifest.json"),
            Path(str(output)+".dataset.json"), summary)):
        raise FeatureError("输出文件已存在，请另选输出目录")
    cutoff = time.time_ns()
    if not 0 <= start_ns <= end_ns <= cutoff:
        raise FeatureError("时间区间无效，结束时刻不能在未来")

    # 1. 读真实指数。来源映射、DolphinDB日期类型与标准时间统一由公共Provider处理。
    bundle = load_feature_bundle(config, connection)
    binding = bundle.binding(dataset, schema_version)
    if binding.backend not in {"dolphindb", "dolphindb-raw"}:
        raise FeatureError("本例须使用DolphinDB来源声明")
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

    # 2. 声明自己公式的输出字段和算法版本，不沿用单MA的计算版本。
    definition = {
        "dataset_id": "research.index.ma20_ma60.minute", "schema_version": 1,
        "computation_version": "pandas-float64-sma20-sma60-half-consecutive-minute-reset-v1",
        "frequency": {"period_ns": 60_000_000_000, "timezone": "Asia/Shanghai"},
        "alignment": "EXACT",
        "fields": [{"name": name, "kind": "float", "minimum": "0", "exclusive_minimum": True}
            for name in ("close", "ma20", "ma60", "index_blend")],
    }
    spec = dataset_from_definition(definition)

    # 3. 适配器将输入整理为DataFrame，调用上面的calculate_blend，然后复用
    # dataframe_to_records转换结果；同时保留60根输入的来源、质量及可见时间约束。
    records, coverage = moving_average_records(inputs, binding.spec, spec, key, 60,
        calculator=calculate_blend)
    if not records:
        raise FeatureError("没有足够的连续60根分钟Bar，未生成文件")

    # 4. 使用与原始指数/单MA相同的导出、manifest、声明和读回校验。
    file_report = _export_and_check(output, {"schema": definition}, DatasetRegistry((spec,)),
        records, key, max_rows)
    report = {"status": "passed", "synthetic": False, "key": key, "source_key": request.keys[0],
        "input_rows": len(batch.records), "feature_rows": len(records), "coverage": coverage,
        "input_source": batch.source_identity, "input_schema_fingerprint": binding.spec.fingerprint,
        "input_availability": getattr(source_config, "availability", "standard-envelope"),
        **file_report, "visibility_note": "真实指数的当前计算快照；不作为历史逐分钟已可用的证明"}
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
