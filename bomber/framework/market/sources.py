"""行情来源配置与工厂；只装配标准Feed，不选择策略或执行客户端。"""

from dataclasses import dataclass, asdict
import json
import math
import os
from pathlib import Path
import time


@dataclass(frozen=True)
class MarketSourceConfig:
    source: str = "ctp"
    table: str | None = None
    timestamp_label: str | None = None
    volume_mode: str | None = None
    max_age_seconds: float = 120
    ws_base_url: str | None = None
    market_type: str = "spot"

    def __post_init__(self):
        if self.source not in {"file", "ctp", "bn-ws", "dolphindb"}:
            raise ValueError("未知market-source")
        if not math.isfinite(self.max_age_seconds) or not 60 < self.max_age_seconds <= 120:
            raise ValueError("分钟行情时效须在(60,120]秒")
        if self.source == "dolphindb":
            if not self.table:
                raise ValueError("DolphinDB行情须配置共享分钟流表market-bar-table；禁止猜测表名")
            if self.timestamp_label not in {"start", "end"} or self.volume_mode not in {"per-bar", "cumulative"}:
                raise ValueError("DolphinDB分钟推送须明确market-bar-timestamp及market-volume-mode")
        if self.market_type not in {"spot", "futures"}:
            raise ValueError("BN market_type须为spot/futures")

    @property
    def identity(self):
        return asdict(self)


def add_market_source_arguments(parser, *, backtest=False, table_env="DDB_MARKET_BAR_TABLE"):
    parser.add_argument("--market-config", type=Path, help="公共行情JSON配置；命令行显式参数覆盖配置")
    parser.add_argument("--market-source", choices=("file",) if backtest else ("ctp", "bn-ws", "dolphindb"))
    if not backtest:
        parser.add_argument("--market-bar-table", help=f"DolphinDB共享分钟流表；也可用{table_env}")
        parser.add_argument("--market-bar-timestamp", choices=("start", "end"))
        parser.add_argument("--market-volume-mode", choices=("per-bar", "cumulative"))
        parser.add_argument("--market-bar-max-age-seconds", type=float)
    parser.set_defaults(_market_backtest=backtest, _market_table_env=table_env)


def market_config_from_args(args, *, domestic_contracts=False):
    values = {}
    path = getattr(args, "market_config", None)
    if path:
        values = json.loads(Path(path).read_text())
        if not isinstance(values, dict):
            raise ValueError("market-config须为JSON对象")
    backtest = getattr(args, "_market_backtest", False)
    values.setdefault("source", "file" if backtest else "ctp")
    mapping = {"market_source": "source", "market_bar_table": "table",
        "market_bar_timestamp": "timestamp_label", "market_volume_mode": "volume_mode",
        "market_bar_max_age_seconds": "max_age_seconds"}
    for key, field in mapping.items():
        value = getattr(args, key, None)
        if value is not None:
            values[field] = value
    if values["source"] == "dolphindb" and not values.get("table"):
        values["table"] = os.getenv(getattr(args, "_market_table_env", "DDB_MARKET_BAR_TABLE"))
    config = MarketSourceConfig(**values)
    if backtest != (config.source == "file"):
        raise ValueError("回测行情使用file；Live使用ctp/bn-ws/dolphindb")
    if config.source != "ctp" and getattr(args, "simnow_environment", "realtime") != "realtime":
        raise ValueError("非CTP行情使用realtime，不采用CTP Replay日期豁免")
    if domestic_contracts and config.source == "bn-ws":
        raise ValueError("BN-WS不提供当前demo声明的国内期货/期权合约；须配置匹配BINANCE标的的业务输入")
    args.market_source = config.source
    args.market_bar_table = config.table
    args.market_bar_timestamp = config.timestamp_label
    args.market_volume_mode = config.volume_mode
    args.market_bar_max_age_seconds = config.max_age_seconds
    args.market_source_config = config
    return config


def source_config(args):
    return getattr(args, "market_source_config", None) or MarketSourceConfig(
        source=getattr(args, "market_source", "ctp"), table=getattr(args, "market_bar_table", None),
        timestamp_label=getattr(args, "market_bar_timestamp", None),
        volume_mode=getattr(args, "market_volume_mode", None),
        max_age_seconds=getattr(args, "market_bar_max_age_seconds", 120))


def create_dolphin_bar_feed(table, source_id, *, max_age_seconds=120, volume_mode="per-bar"):
    from bomber.framework.market.stream.dolphin import DolphinDbLiveDataFeed, DolphinDbConfig, DolphinDbStreamSpec
    from bomber.framework.market.stream.health import StreamHealthConfig
    from bomber.framework.trader.runtime.live.channels.ctp import required
    if not table:
        raise ValueError(f"须配置{source_id}的共享分钟流表")
    return DolphinDbLiveDataFeed(DolphinDbConfig(host=required("DDB_HOST"),
        port=int(os.getenv("DDB_PORT", "8848")), username=required("DDB_USERNAME"), password=required("DDB_PASSWORD"),
        streaming_port=int(os.getenv("DDB_STREAMING_PORT", "0")),
        streams=(DolphinDbStreamSpec.bar(table,
            action_name=f"{source_id.replace('-', '_')}_{os.getpid()}_{time.time_ns()}",
            auto_schema=True, volume_mode=volume_mode, local_receipt_time=True),)),
        source_id=source_id, health_config=StreamHealthConfig(startup_grace_seconds=max_age_seconds,
            stale_after_seconds=max_age_seconds))


def create_market_feed(config, source_id, *, lifecycle=None, flow_path=None, environment="realtime"):
    """构造不连接；文件parser/注册、业务合约及网络启动由装配层声明。"""
    if config.source == "file":
        from bomber.framework.market.replay.base import FileReplayFeed
        return FileReplayFeed(source_id)
    if config.source != "ctp" and lifecycle is not None and hasattr(lifecycle, "ready_seconds"):
        # 分钟源首根完成Bar可能等待一个完整分钟，不沿用CTP Tick的30秒预算。
        lifecycle.ready_seconds = max(lifecycle.ready_seconds, config.max_age_seconds)
    if config.source == "ctp":
        from dataclasses import replace
        from bomber.framework.market.stream.ctp import CtpLiveDataFeed, CtpMdConfig
        from bomber.framework.trader.runtime.live.channels.ctp import build_md_config
        feed = CtpLiveDataFeed(replace(build_md_config(CtpMdConfig, lifecycle,
            flow_path=flow_path), dce_use_receive_date=environment == "realtime"))
    elif config.source == "dolphindb":
        feed = create_dolphin_bar_feed(config.table, source_id,
            max_age_seconds=config.max_age_seconds, volume_mode=config.volume_mode)
    else:
        from bomber.framework.market.stream.bn.feed import BNWSConfig, BNWSStreamDataFeed
        url = config.ws_base_url or ("wss://fstream.binance.com" if config.market_type == "futures"
                                    else "wss://stream.binance.com:9443")
        feed = BNWSStreamDataFeed(BNWSConfig(ws_base_url=url,
            market_type=config.market_type), source_id=source_id)
    feed.market_source_config = config
    return feed


def assemble_minute_market(args, upstream, source_id, profile, instruments, expected_day,
                           *, after_tick=None, clock_ns=None, fallback_events=None, fallback_receives=None):
    """公共Live装配，CTP保留深度/逐笔门禁，分钟源使用真实Bar逐合约门禁。"""
    from bomber.framework.trader.runtime.live.channels.ctp import CtpMarketChannel, InstrumentHealthGate
    from bomber.framework.trader.runtime.live.profiles import build_minute_feed
    config = source_config(args)
    if config.source == "file":
        raise ValueError("文件行情使用回测Runtime，不进入Live分钟装配")
    if config.source == "ctp":
        gate = InstrumentHealthGate(upstream, instruments, expected_day, policy=profile,
            max_age_seconds=getattr(args, "instrument_max_age_seconds", 10), clock_ns=clock_ns,
            fallback_events=fallback_events, fallback_receives=fallback_receives)
        feed = build_minute_feed(source_id, upstream, profile, after_tick=after_tick,
            max_age_seconds=getattr(args, "instrument_max_age_seconds", 10), clock_ns=clock_ns)
        return feed, CtpMarketChannel(upstream, expected_day, profile=profile, instrument_health=gate), gate
    from bomber.framework.market.stream.completed import CompletedMinuteBarFeed
    from bomber.framework.trader.runtime.live.channels.dolphin import DolphinMarketChannel
    from bomber.framework.trader.runtime.live.channels.binance import BinanceMarketChannel
    from bomber.framework.trader.runtime.live.health import InstrumentMarketChannel
    feed = CompletedMinuteBarFeed(source_id, upstream, config=config, clock_ns=clock_ns)
    feed.required_instruments = frozenset(instruments)
    channel = (DolphinMarketChannel(upstream, max_receive_age_seconds=config.max_age_seconds,
        event_offset_seconds=60 if config.timestamp_label == "start" else 0, clock_ns=clock_ns)
        if config.source == "dolphindb" else BinanceMarketChannel(upstream,
            max_age_seconds=config.max_age_seconds, clock_ns=clock_ns))
    return feed, InstrumentMarketChannel(channel, feed), feed
