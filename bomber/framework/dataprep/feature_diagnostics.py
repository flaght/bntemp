"""Read-only measurements of the shared prepared-frame restoration service.

This diagnostic accepts the persisted v1 schema even after timing code changes.
It verifies artifact files and the exact declared feature input; it neither runs
a strategy nor certifies the source/native compatibility required by backtests.
"""
from datetime import date, datetime, time, timedelta
import gc
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .features import FeatureRequest, bundle_from_definition
from .replay_inputs import ReplayInputs, implementation_identity
from .session import InputSession


def profile_prepared_frames(root, feature_config, *, verify="metadata", repeat=1, backend="auto"):
    if type(repeat) is not int or not 1 <= repeat <= 10:
        raise ValueError("repeat须为1～10的整数")
    root, feature_config = Path(root).resolve(), Path(feature_config).resolve()
    with InputSession() as setup:
        setup.fingerprint(root/"manifest.json")
        manifest = json.loads((root/"manifest.json").read_text(encoding="utf-8"))
        context = manifest["context"]
        feature = context["feature_input"]
        setup.fingerprint(feature_config)
        definition = json.loads(feature_config.read_text(encoding="utf-8"))
        datasets = [item for item in definition["datasets"] if
            (item["schema"]["dataset_id"], item["schema"]["schema_version"]) ==
            (feature["dataset_id"], feature["schema_version"])]
        if len(datasets) != 1:
            raise ValueError("特征声明必须唯一匹配产物中的数据集/版本")
        # Rebind the declared schema to the recorded local file; never connect to a database.
        datasets[0]["source"] = {"backend": "file", "path": feature["path"]}
        bundle = bundle_from_definition({**definition, "datasets": datasets}, None)
        binding = bundle.binding(feature["dataset_id"], feature["schema_version"])
        if binding.spec.fingerprint != feature["schema_fingerprint"]:
            raise ValueError("特征Schema/计算版本与帧产物不匹配")
        zone = ZoneInfo(binding.spec.frequency.timezone)
        first, last = (date.fromisoformat(value) for value in context["requested"])
        start = int(datetime.combine(first, time(), zone).timestamp())*1_000_000_000
        end = int(datetime.combine(last+timedelta(days=1), time(), zone).timestamp())*1_000_000_000-1
        with setup.performance.measure("profile_feature_input_read"):
            with bundle.provider(feature["dataset_id"], feature["schema_version"]) as provider:
                batch = provider.read(FeatureRequest(feature["dataset_id"], feature["schema_version"],
                    (context["frame_rules"]["index_key"],), start, end, 2**63-1, max_rows=1_000_000))
        if batch.source_identity != feature["source"] or len(batch.records) != feature["records"]:
            raise ValueError("特征文件内容/映射/记录数与帧产物不匹配")
        setup.fingerprint(feature_config)
        setup.fingerprint(root/"manifest.json")
        # Diagnostic-only: exact v1 schema and file checks, not a compatibility bypass for trading.
        reader = ReplayInputs(root, context, verify=verify)
        setup_performance = setup.performance.snapshot()
    runs = []
    for _ in range(repeat):
        with InputSession() as session:
            outcomes, deadlines = reader.load_frames(batch.records, binding, backend=backend)
            stats = session.performance.snapshot()
            if len(outcomes) != len(deadlines):
                raise ValueError("恢复后的帧数与期限数不一致")
            stats["frames"] = len(outcomes)
            stats["restore_backend"] = "cython" if stats["counters"].get("prepared_restore_cython_runs") else "python"
            phases = ("prepared_frames_table_read", "prepared_observations_table_read",
                "prepared_frames_to_python", "prepared_observations_to_python", "prepared_observations_columnar_prepare",
                "prepared_frame_reference_checks",
                "prepared_observation_decimal", "prepared_observation_objects", "prepared_frame_objects")
            measured = sum(stats["timings"][name]["seconds"] for name in phases)
            stats["restore_other_seconds"] = round(max(0., stats["timings"]["prepared_frames_load"]["seconds"]-measured), 6)
            runs.append(stats)
        # Destruction/collection is outside the next measured restoration interval.
        del outcomes, deadlines
        gc.collect()
    return {"status": "passed", "diagnostic_only": True, "artifact": reader.identity,
        "requested_restore_backend": backend,
        "feature_file": feature["path"], "feature_config": str(feature_config),
        "feature_dataset": feature["dataset_id"], "feature_records": len(batch.records),
        "current_implementation_matches_artifact": implementation_identity() == context["implementation"],
        "setup_performance": setup_performance, "runs": runs,
        "measurement": "wall seconds; batch clocks, no per-observation clocks; repeated runs reuse OS caches",
        "scope": "公共帧文件读取/转换/装配诊断；不运行策略/撮合，不证明正式回测源码或SDK兼容；父级计时不要叠加"}
