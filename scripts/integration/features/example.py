"""分钟特征教学例子：合成Bar回放、真实指数MA导出及只读轮询。

Live采用已计算字段的声明式投影，不创建表或写数据库，不创建交易目标。
本文件是共同使用的教学入口，不是新增特征专用Provider。
"""
import argparse
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal, localcontext
import hashlib
import json
import math
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from bomber.framework.datahub import DataHub
from bomber.framework.datahub.features import (
    Alignment, DatasetRegistry, ExternalDataRecord, FeatureHub, FeatureQuery,
    FeatureRequirement, FeatureUnavailable, MarketFrameInput,
    dataset_from_definition, replay_feature_frames,
)
from bomber.framework.dataprep.features import (
    DEFAULT_FEATURE_CONFIG, FeatureProviderFactory, FeatureQueryCache, FeatureRequest,
    encode_record, export_features, load_feature_bundle,
)

CONFIG = Path(__file__).with_name("minute_return.example.json")
DATASET = "example.bar_return.minute"
MINUTE_NS = 60_000_000_000
WAIT_NS = 5_000_000_000


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


def export_moving_average(args):
    from dotenv import load_dotenv
    from bomber.framework.dolphindb import DolphinDbConnectionConfig

    output = args.output.resolve()
    dataset_path = Path(str(output) + ".dataset.json")
    if any(path.exists() for path in (output, Path(str(output) + ".manifest.json"), dataset_path)):
        raise ValueError("输出文件或配套清单已存在，请换一个新输出路径")
    definition = moving_average_definition(args.window)
    output_spec = dataset_from_definition(definition)
    output_registry = DatasetRegistry((output_spec,))
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    connection = DolphinDbConnectionConfig.from_env(purpose="market", read_timeout_seconds=args.timeout)
    bundle = load_feature_bundle(args.config, connection)
    input_spec = bundle.binding(args.dataset, args.schema_version).spec
    if not any(field.name == "close" and field.kind in {"decimal", "float", "int"}
               for field in input_spec.payload_fields):
        raise ValueError("输入Schema须有数值close字段")
    request = FeatureRequest(args.dataset, args.schema_version, (args.key,),
        args.start, args.end, time.time_ns(), max_rows=args.max_rows)
    with bundle.provider(args.dataset, args.schema_version, purpose="feature_export") as provider:
        batch = provider.read(request)
        received = time.time_ns()
    if not batch.records:
        raise ValueError("指定区间没有指数行情")
    inputs = tuple(replace(row, received_ns=max(received, row.received_ns or 0)) for row in batch.records)
    records, coverage = moving_average_records(inputs, input_spec, output_spec, args.key, args.window)
    if not records:
        raise ValueError(f"没有足够的连续{args.window}根分钟Bar；请扩大区间或核验源数据缺口")
    manifest = export_features(output, output_registry, records)
    # 同目录相对路径，使导出文件包可整体移动到回测机。
    file_definition = {"format_version": 1, "datasets": [{"schema": definition,
        "source": {"backend": "file", "path": output.name, "require_manifest": True}}]}
    with dataset_path.open("x", encoding="utf-8") as stream:
        json.dump(file_definition, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    replay_bundle = load_feature_bundle(dataset_path, None)
    replay_request = FeatureRequest(output_spec.dataset_id, 1, (args.key,),
        records[0].ts_event, records[-1].ts_event, time.time_ns(), max_rows=args.max_rows)
    with replay_bundle.provider(output_spec.dataset_id) as file_provider:
        reloaded = file_provider.read(replay_request).records
    if reloaded != records:
        raise ValueError("MA导出与文件逐记录读回不一致")
    report = {"status": "passed", "key": args.key, "input_dataset": args.dataset,
        "input_rows": len(inputs), "input_schema_fingerprint": input_spec.fingerprint,
        "input_source": batch.source_identity,
        "input_records_sha256": hashlib.sha256(json.dumps([encode_record(row) for row in inputs],
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        "input_config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "dataset_id": output_spec.dataset_id, "window": args.window, "rows": len(records),
        **coverage, "output": str(output), "dataset_config": str(dataset_path),
        "manifest": manifest, "file_roundtrip": "passed",
        "availability": "actual_calculation_completion_after_input_observation",
        "preview": [asdict(row) for row in records[-3:]]}
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str, allow_nan=False), flush=True)


def spec_and_registry():
    definition = json.loads(CONFIG.read_text(encoding="utf-8"))
    spec = dataset_from_definition(definition["datasets"][0]["schema"])
    return spec, DatasetRegistry((spec,))


def sample_bars():
    """三个合成收盘Bar；时间也是教学模拟时钟，不是真实行情证据。"""
    result = []
    for minute, closing in ((1, 101.0), (2, 99.0), (3, 100.5)):
        event = int(datetime.fromisoformat(
            f"2026-10-12T13:{minute:02d}:00+08:00").timestamp()) * 1_000_000_000
        result.append({"event_ns": event, "available_ns": event + 200_000_000,
                       "open": 100.0, "close": closing})
    return tuple(result)


def build_feature(spec, key, bar, completed_ns, *, source, revision, quality_flags=0):
    """实际接入时，completed_ns应在计算完成后取time.time_ns()。

    bar.available_ns须保留真实输入可用性；上层另行管理重复输入和源修订。
    本例的return_1m是分钟内(open->close)收益率，不是跨分钟close收益率。
    """
    opening, closing = float(bar["open"]), float(bar["close"])
    if not all(math.isfinite(value) and value > 0 for value in (opening, closing)):
        raise ValueError("输入Bar的open/close须为有限正数")
    spec.frequency.close_ns(bar["event_ns"])
    if completed_ns < bar["available_ns"] or bar["available_ns"] < bar["event_ns"]:
        raise ValueError("已收盘Bar和计算结果不能提前可用")
    # 自动生成的frozen/slots Payload负责字段类型、有限值及边界校验。
    payload = spec.payload_type(return_1m=(closing - opening) / opening)
    return ExternalDataRecord(
        dataset_id=spec.dataset_id, schema_version=spec.schema_version,
        key=key, payload=payload, ts_event=bar["event_ns"],
        available_ns=completed_ns, source_ns=bar["event_ns"],
        revision=revision, source=source, quality_flags=quality_flags,
        received_ns=completed_ns,
    )


def requirement(key):
    return FeatureRequirement("minute_return", DATASET, 1, key, alignment=Alignment.EXACT)


def build_and_export(args):
    spec, registry = spec_and_registry()
    records = []
    # 第二分钟模拟特征晚到6秒，用于演示5秒等待后跳过；不修改业务时间。
    for bar, delay in zip(sample_bars(), (400_000_000, 6_000_000_000, 400_000_000)):
        completed = bar["event_ns"] + delay
        records.append(build_feature(spec, args.key, bar, completed,
            source="example:synthetic-bar", revision=completed))
    manifest = export_features(args.output, registry, records)
    print(json.dumps({"synthetic": True, "output": str(args.output),
        "manifest": manifest, "values": [r.payload.return_1m for r in records]},
        ensure_ascii=False, indent=2), flush=True)


def backtest(args):
    _, registry = spec_and_registry()
    bars = sample_bars()
    # 先把文件载入内存；可见性由模拟时钟/available_ns控制，不按载入顺序决策。
    request = FeatureRequest(DATASET, 1, (args.key,), bars[0]["event_ns"],
        bars[-1]["event_ns"], bars[-1]["event_ns"] + 10_000_000_000)
    with FeatureProviderFactory.create_for("offline_feature", "file", args.input, registry) as provider:
        records = provider.read(request).records
    hub = DataHub(features=FeatureHub(registry))
    frames = tuple(MarketFrameInput(str(bar["event_ns"]), bar["event_ns"],
        bar["available_ns"], {"open": bar["open"], "close": bar["close"]}) for bar in bars)
    outcomes = replay_feature_frames(hub, (requirement(args.key),), frames, records, wait_ns=WAIT_NS)
    for outcome in outcomes:
        result = {"event_time": datetime.fromtimestamp(outcome.event_ns // 1_000_000_000,
            ZoneInfo("Asia/Shanghai")).isoformat(), "status": outcome.status,
            "reason": outcome.reason}
        if outcome.frame is not None:
            result.update(return_1m=outcome.frame.features.latest("minute_return").payload.return_1m,
                          decision_ns=outcome.frame.decision_ns)
        print(json.dumps(result, ensure_ascii=False), flush=True)


def live(args):
    from dotenv import load_dotenv
    from bomber.framework.dolphindb import DolphinDbConnectionConfig

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    bundle = load_feature_bundle(CONFIG, DolphinDbConnectionConfig.from_env(purpose="market", read_timeout_seconds=2))
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    start = int(now.replace(hour=9, minute=31, second=0, microsecond=0).timestamp()) * 1_000_000_000
    end = int(now.replace(hour=15, minute=0, second=0, microsecond=0).timestamp()) * 1_000_000_000
    if not start <= time.time_ns() <= end:
        raise ValueError("Live例子请在日盘行情时段运行；它不改写历史日期或跨日续跑")
    cache = FeatureQueryCache(bundle.provider(DATASET), dataset_id=DATASET, schema_version=1,
        keys=(args.key,), start_ns=start, end_ns=end, poll_seconds=0.2)
    subscribed = (requirement(args.key),)
    stop, last_reported = time.monotonic() + args.seconds, None
    try:
        cache.open()
        print("Live只读监视：当前已收盘分钟EXACT；等待期间不前填。", flush=True)
        while time.monotonic() < stop:
            cache.poll()  # 运行控制线程查询数据库；策略回调不调用poll。
            observed = time.time_ns()
            minute_end = observed // MINUTE_NS * MINUTE_NS
            try:
                snapshot = cache.hub.feature_snapshot(subscribed, FeatureQuery(minute_end, observed))
            except FeatureUnavailable as error:
                if error.code not in {"MISSING", "NOT_READY"}:
                    raise
                token = (minute_end, error.code)
                output = {"event_ns": minute_end, "status": "WAITING", "reason": error.code}
            else:
                record = snapshot.latest("minute_return")
                token = record.identity
                output = {"event_ns": record.ts_event, "available_ns": record.available_ns,
                    "received_ns": record.received_ns, "revision": record.revision,
                    "return_1m": record.payload.return_1m, "status": "VISIBLE"}
            if token != last_reported:
                print(json.dumps(output, ensure_ascii=False), flush=True)
                last_reported = token
            time.sleep(0.2)
        print(json.dumps(cache.manifest, ensure_ascii=False, indent=2), flush=True)
    finally:
        cache.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="构建合成分钟特征并导出CSV/Parquet")
    build.add_argument("--output", type=Path, required=True)
    replay = commands.add_parser("backtest", help="导入已导出文件，用同一对齐接口演示模拟时钟决策")
    replay.add_argument("--input", type=Path, required=True)
    online = commands.add_parser("live", help="只读轮询真实index_1min的已计算字段")
    online.add_argument("--connect", action="store_true")
    online.add_argument("--seconds", type=float, default=180)
    ma = commands.add_parser("export-ma", help="读取真实指数分钟行情，计算MA并导出标准特征文件")
    ma.add_argument("--connect", action="store_true")
    ma.add_argument("--config", type=Path, default=DEFAULT_FEATURE_CONFIG)
    ma.add_argument("--dataset", default="index.minute")
    ma.add_argument("--schema-version", type=int, default=1)
    from .read_only import timestamp
    ma.add_argument("--start", type=timestamp, required=True)
    ma.add_argument("--end", type=timestamp, required=True)
    ma.add_argument("--window", type=int, default=5)
    ma.add_argument("--timeout", type=int, default=15)
    ma.add_argument("--max-rows", type=int, default=10000)
    ma.add_argument("--output", type=Path, required=True)
    for command in (build, replay, online, ma):
        command.add_argument("--key", default="000852")
    args = parser.parse_args(argv)
    if args.command == "live" and (not args.connect or not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error("Live须显式--connect，seconds须为有限正数")
    if args.command == "export-ma":
        if not args.connect:
            parser.error("从数据库导出MA须显式--connect")
        if not 1 <= args.window <= args.max_rows <= 1_000_000 or args.window > 10000:
            parser.error("须满足1 <= window <= max-rows <= 1000000，window最多10000")
        if args.start > args.end or args.end > time.time_ns():
            parser.error("起止区间须有序，结束时间不能在未来")
    {"build": build_and_export, "backtest": backtest, "live": live,
     "export-ma": export_moving_average}[args.command](args)


if __name__ == "__main__":
    main()
