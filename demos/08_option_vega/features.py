"""08共用指数声明；离线仅替换存储来源，保留Schema、计算版本和时间。"""
import json
from pathlib import Path

from bomber.framework.datahub.features import Alignment, FeatureError
from bomber.framework.dataprep.features import (DEFAULT_FEATURE_CONFIG, bundle_from_definition,
    load_feature_bundle, FeatureProviderFactory, FeatureRequest)
from bomber.framework.dataprep.sources.policy import DataSourcePurpose


def add_index_feature_arguments(parser):
    parser.add_argument("--index-feature-config", type=Path, default=DEFAULT_FEATURE_CONFIG)
    parser.add_argument("--index-dataset", default="index.minute")
    parser.add_argument("--index-schema-version", type=int, default=1)


def index_bundle(path, dataset_id, schema_version, *, connection=None, file_path=None):
    if file_path is None:
        bundle = load_feature_bundle(path, connection)
    else:
        # 同一份声明，只把选定数据集的来源换为标准文件；不构造数据库连接。
        try:
            definition = json.loads(Path(path).read_text(encoding="utf-8"))
            datasets = [item for item in definition["datasets"] if
                (item["schema"]["dataset_id"], item["schema"]["schema_version"]) == (dataset_id, schema_version)]
        except (OSError, ValueError, KeyError, TypeError):
            raise FeatureError("指数特征配置不可读取或结构无效") from None
        if not datasets:
            raise FeatureError("特征清单未声明该指数数据集/版本")
        for item in datasets:
            item["source"] = {"backend": "file", "path": str(Path(file_path).expanduser().resolve())}
        bundle = bundle_from_definition({**definition, "datasets": datasets}, None)
    binding = bundle.binding(dataset_id, schema_version)
    if (binding.spec.frequency.period_ns != 60_000_000_000
            or binding.spec.default_alignment is not Alignment.EXACT or binding.bar_projection is None):
        raise FeatureError("08指数须声明一分钟EXACT对齐及真实OHLCV投影")
    return bundle, binding


def read_index_features(args, start_ns, end_ns):
    file_path = Path(args.index_feature_file).expanduser().resolve()
    bundle, binding = index_bundle(args.index_feature_config, args.index_dataset,
        args.index_schema_version, file_path=file_path)
    provider = FeatureProviderFactory.create_for(DataSourcePurpose.OFFLINE_FEATURE,
        "file", file_path, bundle.registry)
    with provider:
        batch = provider.read(FeatureRequest(args.index_dataset, args.index_schema_version,
            (args.index_code,), start_ns, end_ns, 2**63-1, max_rows=1_000_000))
    if not batch.records:
        raise FeatureError("指数特征文件没有请求日期/代码的记录")
    identity = {"backend": "file", "path": str(Path(args.index_feature_file).expanduser().resolve()),
        "source": batch.source_identity, "dataset_id": binding.spec.dataset_id,
        "schema_version": binding.spec.schema_version, "schema_fingerprint": binding.spec.fingerprint,
        "computation_version": binding.spec.computation_version,
        "alignment": binding.spec.default_alignment.value, "records": len(batch.records)}
    return bundle, binding, batch.records, identity
