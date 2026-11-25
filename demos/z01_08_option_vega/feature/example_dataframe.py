"""可直接运行的合成例子：DataFrame计算→标准特征→文件/manifest/声明→读回。"""
import argparse
import json
from pathlib import Path
import sys
import time

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "demos.08_option_vega.feature"

from bomber.framework.datahub.features import DatasetRegistry, dataset_from_definition
from bomber.framework.dataprep.features import FeatureRequest, export_features, load_feature_bundle
from .dataframe import dataframe_to_records


def calculate_features(frame):
    """业务公式只处理DataFrame；本例输入是一个连续上午时段。"""
    result = frame.copy()
    result["ma20"] = result["close"].rolling(20, min_periods=20).mean()
    result["ma60"] = result["close"].rolling(60, min_periods=60).mean()
    result["index_blend"] = (result["ma20"] + result["ma60"]) / 2
    return result.dropna()  # 先完成60根预热，才转换输出。


def run_example(output_dir):
    output_dir = Path(output_dir).expanduser().resolve()
    output = output_dir / "index-blend.csv"
    declaration = Path(str(output) + ".dataset.json")
    manifest = Path(str(output) + ".manifest.json")
    summary = output_dir / "summary.json"
    if any(path.exists() for path in (output, declaration, manifest, summary)):
        raise ValueError("输出文件已存在，请另选输出目录")

    # 1. 准备业务DataFrame。120根合成M1，仅用于演示，不连接真实数据源。
    frame = pd.DataFrame({"close": [6500.0 + i * .2 for i in range(120)]},
        index=pd.date_range("2026-09-01 09:31:00", periods=120, freq="min",
            tz="Asia/Shanghai", name="ts_event"))

    # 2. 普通Pandas计算。使用真实数据时，可在此替换数据和公式。
    result = calculate_features(frame)
    completed_ns = time.time_ns()  # 实际完成后取时钟，保留当前计算快照语义。

    # 3. 声明输出字段及算法版本；转换器不推断字段含义。
    definition = {
        "dataset_id": "example.index.ma20_ma60.minute", "schema_version": 1,
        "computation_version": "pandas-float64-sma20-sma60-half-example-v1",
        "frequency": {"period_ns": 60_000_000_000, "timezone": "Asia/Shanghai"},
        "alignment": "EXACT",
        "fields": [{"name": name, "kind": "float", "minimum": "0", "exclusive_minimum": True}
            for name in ("close", "ma20", "ma60", "index_blend")],
    }
    spec = dataset_from_definition(definition)

    # 4. DataFrame→标准记录。来源时间按行提供，发布/接收时间为本次计算完成时间。
    records = dataframe_to_records(result, spec,
        key="000852", source="example:synthetic-ma20-ma60",
        available_ns=completed_ns, received_ns=completed_ns,
        source_ns=pd.Series([stamp.value for stamp in result.index], index=result.index, dtype="Int64"))

    # 5. 公共导出接口生成CSV和manifest；保存相同Schema的可移动文件声明。
    export_features(output, DatasetRegistry((spec,)), records)
    with declaration.open("x", encoding="utf-8") as stream:
        json.dump({"format_version": 1, "datasets": [{"schema": definition,
            "source": {"backend": "file", "path": output.name, "require_manifest": True}}]},
            stream, ensure_ascii=False, indent=2)
        stream.write("\n")

    # 6. 从文件声明重新加载，验证每条记录及manifest，不依赖原内存声明。
    bundle = load_feature_bundle(declaration, None)
    with bundle.provider(spec.dataset_id, spec.schema_version) as provider:
        loaded = provider.read(FeatureRequest(spec.dataset_id, spec.schema_version, ("000852",),
            records[0].ts_event, records[-1].ts_event, time.time_ns())).records
    if loaded != records:
        raise ValueError("导出后文件读回记录不一致")
    report = {"status": "passed", "synthetic": True, "input_rows": len(frame),
        "feature_rows": len(records), "dataset_id": spec.dataset_id,
        "output": str(output), "manifest": str(manifest), "dataset_config": str(declaration),
        "file_roundtrip": "passed", "first_minute": result.index[0].isoformat(),
        "last_minute": result.index[-1].isoformat(), "first_value": records[0].payload.index_blend,
        "visibility_note": "合成数据及当前计算快照，用于验证接口与文件读写，不是历史策略收益回测"}
    with summary.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_example(args.output_dir)
    except (ValueError, RuntimeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
