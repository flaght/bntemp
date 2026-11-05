"""08特征业务入口：导出、声明、读取及回测分钟决策，复用公共特征框架。"""
import argparse
import json
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "demos.08_option_vega"

from bomber.framework.datahub import DataHub
from bomber.framework.datahub.features import (Alignment, FeatureError, FeatureHub,
    FeatureRequirement, MarketFrameInput, replay_feature_frames)
from bomber.framework.market.frames import FrameObservation, MinuteFrame
from bomber.framework.dataprep.features import (DEFAULT_FEATURE_CONFIG, bundle_from_definition,
    load_feature_bundle, FeatureProviderFactory, FeatureRequest, FileFeatureProvider, export_features)
from bomber.framework.dataprep.sources.policy import DataSourcePurpose


def add_index_feature_arguments(parser):
    parser.add_argument("--index-feature-config", type=Path, default=DEFAULT_FEATURE_CONFIG)
    parser.add_argument("--index-dataset", default="index.minute")
    parser.add_argument("--index-schema-version", type=int, default=1)


def select_index_value_field(binding, field=None):
    """回测可显式消费标量特征；默认沿用指数Bar的close投影。"""
    if field is None:
        if binding.bar_projection is None:
            raise FeatureError("指数特征没有默认价格投影，请显式指定--index-value-field")
        field = binding.bar_projection["fields"]["close"]
    declared = next((item for item in binding.spec.payload_fields if item.name == field), None)
    if declared is None or declared.kind not in {"decimal", "float", "int"} or declared.nullable:
        raise FeatureError(f"指数特征字段须为声明中的非空数值: {field}")
    return field


def _index_price(record, field):
    value = Decimal(str(getattr(record.payload, field)))
    if not value.is_finite() or value <= 0:
        raise FeatureError(f"指数信号须为有限正价格: {field}")
    return value


def index_bundle(path, dataset_id, schema_version, *, connection=None, file_path=None, value_field=None):
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
            or binding.spec.default_alignment is not Alignment.EXACT):
        raise FeatureError("08指数特征须声明一分钟EXACT对齐")
    select_index_value_field(binding, value_field)
    return bundle, binding


def generate_index_features(args, start_ns, end_ns, *, connection):
    """显式从DolphinDB生成标准包；不回填可见时间、不覆盖已有文件。"""
    output = Path(args.output).expanduser().resolve()
    if output.suffix.lower() not in {".csv", ".parquet"}:
        raise FeatureError("特征导出须为CSV或Parquet")
    if output.exists() or Path(str(output) + ".manifest.json").exists():
        raise FeatureError("导出目标已存在，请使用新文件名，不覆盖历史包")
    bundle, binding = index_bundle(args.index_feature_config, args.index_dataset,
        args.index_schema_version, connection=connection)
    if binding.backend not in {"dolphindb", "dolphindb-raw"}:
        raise FeatureError("08生成入口须使用DolphinDB来源声明")
    config = binding.source_config
    availability = getattr(args, "index_availability", None)
    if availability is not None:
        if binding.backend != "dolphindb-raw":
            raise FeatureError("可见时间覆盖仅适用于DolphinDB原表；标准记录保留原时间")
        config = replace(config, availability=availability)
    cutoff = time.time_ns()
    if end_ns > cutoff:
        raise FeatureError("特征导出结束时刻尚未到达，请选择已结束的日期区间")
    request = FeatureRequest(args.index_dataset, args.index_schema_version,
        (args.index_code,), start_ns, end_ns, cutoff, max_rows=args.max_rows)
    with FeatureProviderFactory.create_for(DataSourcePurpose.FEATURE_EXPORT,
            binding.backend, config, bundle.registry, **binding.provider_options) as provider:
        batch = provider.read(request)
    if not batch.records:
        raise FeatureError("指数来源在请求区间/代码下没有可见记录，未生成文件")
    manifest = export_features(output, bundle.registry, batch.records)
    with FileFeatureProvider(output, bundle.registry) as provider:
        loaded = provider.read(replace(request, as_of_ns=max(cutoff, time.time_ns())))
    if loaded.records != batch.records:
        raise FeatureError("生成后的特征文件与来源记录不一致")
    mode = getattr(config, "availability", "standard-envelope")
    return {"status": "passed", "output": str(output), "manifest": str(output) + ".manifest.json",
        "dataset_id": binding.spec.dataset_id, "schema_version": binding.spec.schema_version,
        "computation_version": binding.spec.computation_version, "source": batch.source_identity,
        "records": len(batch.records), "availability": mode, "file_roundtrip": "passed",
        "first_event_ns": min(r.ts_event for r in batch.records),
        "last_event_ns": max(r.ts_event for r in batch.records),
        "max_visibility_delay_seconds": max((r.visible_ns - r.ts_event) / 1e9 for r in batch.records),
        "visibility_note": "本次观测时间，不能作为过去分钟当时已可用的证明" if mode == "observed"
            else "保留源记录的可用时间；原表发布时间含义由数据来源保证",
        "export_manifest": manifest}


def read_index_features(args, start_ns, end_ns):
    file_path = Path(args.index_feature_file).expanduser().resolve()
    bundle, binding = index_bundle(args.index_feature_config, args.index_dataset,
        args.index_schema_version, file_path=file_path, value_field=getattr(args, "index_value_field", None))
    provider = FeatureProviderFactory.create_for(DataSourcePurpose.OFFLINE_FEATURE,
        "file", file_path, bundle.registry)
    with provider:
        batch = provider.read(FeatureRequest(args.index_dataset, args.index_schema_version,
            (args.index_code,), start_ns, end_ns, 2**63-1, max_rows=1_000_000))
    if not batch.records:
        raise FeatureError("指数特征文件没有请求日期/代码的记录")
    field = select_index_value_field(binding, getattr(args, "index_value_field", None))
    for record in batch.records:
        _index_price(record, field)
    identity = {"backend": "file", "path": str(Path(args.index_feature_file).expanduser().resolve()),
        "source": batch.source_identity, "dataset_id": binding.spec.dataset_id,
        "schema_version": binding.spec.schema_version, "schema_fingerprint": binding.spec.fingerprint,
        "computation_version": binding.spec.computation_version,
        "value_field": field,
        "alignment": binding.spec.default_alignment.value, "records": len(batch.records)}
    return bundle, binding, batch.records, identity


def prepare_feature_frames(events, bundle, binding, records, index_key, wait_ns, max_age_ns=300_000_000_000):
    if not events:
        raise ValueError("回测交易腿文件没有可回放的Bar")
    grouped = defaultdict(list)
    for bar in events:
        if bar.ts_init < bar.ts_event:
            raise ValueError("回测完整Bar不能提前可见")
        grouped[bar.ts_event].append(bar)
    market, deadlines = [], {}
    last_available = max(bar.ts_init for bar in events)
    for event, bars in sorted(grouped.items()):
        received = min(bar.ts_init for bar in bars)
        deadline = received + wait_ns
        # 结束前需留有真实行情推进原生时钟；不生成虚构价格来完成撮合。
        if deadline >= last_available:
            continue
        observations = {str(bar.bar_type.instrument_id.symbol): FrameObservation(
            bar.close.as_decimal(), "option-bars", bar.ts_event, bar.ts_init)
            for bar in bars if bar.ts_init <= deadline}
        market.append(MarketFrameInput(str(event), event, received, observations))
        deadlines[event] = deadline
    requirement = FeatureRequirement("index", binding.spec.dataset_id,
        binding.spec.schema_version, index_key, max_source_age_ns=max_age_ns)
    outcomes = replay_feature_frames(DataHub(features=FeatureHub(bundle.registry)),
        (requirement,), market, records, wait_ns=wait_ns)
    if any(row.reason and row.reason.startswith("QUALITY:") for row in outcomes):
        raise FeatureError("指数特征最新可见版本质量不合格")
    return outcomes, deadlines


def build_backtest_runner(**kwargs):
    """按既有特征契约构造回测Runner，导入本模块不加载交易引擎。"""
    from bomber.framework.trader import UnifiedStrategyRunner, RuntimeMode

    class OptionFeatureBacktestRunner(UnifiedStrategyRunner):
        """行情照常进入撮合；策略只消费与Live相同的封闭分钟帧。"""
        def __init__(self, *, positions, feed, backend, bundle, binding, records, index_key, wait_seconds,
                     max_market_age_seconds=300, index_value_field=None):
            super().__init__(RuntimeMode.HISTORICAL, position_manager=positions)
            self.feed, self.backend = feed, backend
            self.bundle, self.binding, self.records = bundle, binding, records
            self.index_value_field = select_index_value_field(binding, index_value_field)
            self.index_key, self.wait_ns = index_key, int(wait_seconds * 1e9)
            self.max_age_ns = int(max_market_age_seconds * 1e9)
            self.strategy = None
            self.frame_audit = []
            self._scheduled = set()
            self.failure = None

        def run_replay(self):
            outcomes, self.deadlines = prepare_feature_frames(self.feed.load_events(), self.bundle,
                self.binding, self.records, self.index_key, self.wait_ns, self.max_age_ns)
            self.outcomes = {row.event_ns: row for row in outcomes}
            result = super().run_replay()
            if self.failure is not None:
                raise RuntimeError("指数特征分钟决策失败") from self.failure
            if len(self.frame_audit) != len(self.outcomes):
                raise RuntimeError("原生模拟时钟未完成特征帧封闭，拒绝输出成功回测")
            return result

        def publish(self, feed_id, event):
            # 保留公共价格观察者；不再向策略重复分发普通Bar。
            for observer in tuple(self._market_observers):
                observer.on_market_event(event)
            timestamp = event.ts_event
            if timestamp not in self.outcomes or timestamp in self._scheduled:
                return
            self._scheduled.add(timestamp)
            # 同一ts_init的文件Bar逐条推进引擎；晚1ns触发，确保截止边界
            # 的所有市场腿已进入撮合，不能在第一条Bar回调内读后续同刻Bar。
            self.backend.engine.kernel.clock.set_time_alert_ns(
                name=f"option-feature-{timestamp}", alert_time_ns=self.deadlines[timestamp]+1,
                callback=lambda timer, stamp=timestamp: self._close_frame(stamp, timer.ts_event))

        def _close_frame(self, timestamp, now_ns):
            outcome = self.outcomes[timestamp]
            audit = {"valuation_ns": timestamp, "request_ns": now_ns,
                "deadline_ns": self.deadlines[timestamp], "status": outcome.status, "reason": outcome.reason}
            self.frame_audit.append(audit)
            if outcome.status != "READY":
                return
            record = outcome.frame.features.latest("index")
            audit.update(revision=record.revision, value_field=self.index_value_field, available_ns=record.available_ns,
                received_ns=record.received_ns, feature_decision_ns=outcome.frame.decision_ns)
            observations = dict(outcome.frame.market)
            field = self.index_value_field
            observations[self.index_key] = FrameObservation(_index_price(record, field),
                "index-bars", record.source_ns, record.visible_ns)
            try:
                self.strategy.on_market_frame(MinuteFrame(timestamp, now_ns, observations))
            except Exception as error:
                self.failure = error
                audit["status"] = "FAILED"
                raise

    return OptionFeatureBacktestRunner(**kwargs)


def main(argv=None):
    """生成一次特征文件，之后由回测入口复用本模块读取和驱动帧决策。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true", help="显式允许只读连接DolphinDB")
    add_index_feature_arguments(parser)
    parser.add_argument("--index-code", default="000852")
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--index-availability", choices=("observed", "source-time"),
        help="省略沿用声明；source-time须确认配置的发布时间是该版本真实可用时刻")
    parser.add_argument("--query-timeout", type=int, default=30)
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    args = parser.parse_args(argv)
    if not args.connect:
        parser.error("生成特征须显式--connect；只读查询，不报单")
    if (args.end_day < args.start_day or len(args.index_code) != 6
            or not args.index_code.isascii() or not args.index_code.isdigit()):
        parser.error("日期范围或六位指数代码无效")
    zone = ZoneInfo("Asia/Shanghai")
    start = int(datetime.combine(args.start_day, datetime.min.time(), zone).timestamp()) * 1_000_000_000
    end = int(datetime.combine(args.end_day + timedelta(days=1), datetime.min.time(), zone).timestamp()) * 1_000_000_000 - 1
    from dotenv import load_dotenv
    from bomber.framework.dolphindb import DolphinDbConnectionConfig
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    try:
        connection = DolphinDbConnectionConfig.from_env(purpose="market", read_timeout_seconds=args.query_timeout)
        result = generate_index_features(args, start, end, connection=connection)
    except (ValueError, RuntimeError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
