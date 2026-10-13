"""分钟特征教学例子：合成Bar构建/导出、模拟时钟回放、真实库只读轮询。

Live采用已计算字段的声明式投影，不创建表或写数据库，不创建交易目标。
本文件是共同使用的教学入口，不是新增特征专用Provider。
"""
import argparse
from datetime import datetime
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
    FeatureProviderFactory, FeatureQueryCache, FeatureRequest,
    export_features, load_feature_bundle,
)

CONFIG = Path(__file__).with_name("minute_return.example.json")
DATASET = "example.bar_return.minute"
MINUTE_NS = 60_000_000_000
WAIT_NS = 5_000_000_000


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
    from bomber.framework.dataprep.sources.dolphindb import DolphinDbReferenceConfig

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    bundle = load_feature_bundle(CONFIG, DolphinDbReferenceConfig.from_env(read_timeout_seconds=2))
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
    for command in (build, replay, online):
        command.add_argument("--key", default="000852")
    args = parser.parse_args(argv)
    if args.command == "live" and (not args.connect or not math.isfinite(args.seconds) or args.seconds <= 0):
        parser.error("Live须显式--connect，seconds须为有限正数")
    {"build": build_and_export, "backtest": backtest, "live": live}[args.command](args)


if __name__ == "__main__":
    main()
