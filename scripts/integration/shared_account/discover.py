"""只读观察三策略所需TD日、资料日、原始MD日和合约；不创建交易检查点。"""

import argparse
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import time

from .prepare_session import parse_cross_products, observed_budget


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true", required=True)
    parser.add_argument("--environment", choices=("replay", "realtime"), required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("scripts/integration/shared_account/results"))
    parser.add_argument("--output", type=Path, help="保存本次只读观察摘要，供会话生成器读取")
    parser.add_argument("--cross-products", type=parse_cross_products, default="CU,AL")
    parser.add_argument("--profile", choices=("commodity", "im-if"), default="commodity")
    parser.add_argument("--ctp-future-buffer-ms", type=int, default=0)
    args = parser.parse_args(argv)
    selected_product, selected_roles = ("IM", ("main", "secondary")) if args.profile == "im-if" else ("CU", ("secondary", "far"))
    if args.profile == "im-if":
        args.cross_products = ("IM", "IF")
        if args.environment != "realtime":
            parser.error("IM/IF联合观察仅支持realtime")
    if not 0 <= args.ctp_future_buffer_ms <= 100 or (args.ctp_future_buffer_ms and args.environment != "realtime"):
        parser.error("CTP未来事件缓冲须为realtime的0至100毫秒")
    from dotenv import load_dotenv
    from .run import PROJECT_ROOT
    from bomber.framework.trader.execution.ctp import CtpNativeTraderDriver, CtpTdApiTransport
    from bomber.framework.trader.runtime.live.channels.ctp import (
        build_simnow_transport, build_md_config, required, validate_replay_environment, occupied_positions,
        CtpLiveProfile, CtpMarketChannel, InstrumentHealthGate)
    from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle
    from bomber.framework.trader.runtime.reports import LiveRunReport
    from bomber.framework.dataprep.live_references import LiveFuturesReferences
    from bomber.framework.dataprep.sources import DataSourcePurpose, DolphinDbReferenceConfig, ReferenceSourceFactory
    from bomber.framework.market.stream.ctp import CtpLiveDataFeed, CtpMdConfig
    from bomber.framework.market.basic.base import InstrumentId, InstrumentMeta, DataType
    from bomber.framework.trader.execution.ipc import wire
    from types import SimpleNamespace
    load_dotenv(PROJECT_ROOT / ".env")
    report = LiveRunReport(args.report_dir, prefix=f"three-discover-{args.environment}")
    report.begin()
    status, failure, driver, feed, fields = "failed", None, None, None, {"cross_products": args.cross_products, "profile": args.profile}
    with ExitStack() as resources:
        try:
            transport = build_simnow_transport(CtpTdApiTransport, client_id="three-discover",
                flow_path=str(report.path / "td-flow"), timeout_seconds=15)
            if args.environment == "replay":
                validate_replay_environment(required("CTP_MD_ADDRESS"), transport.front, transport.production_mode)
            CtpSharedAccountLifecycle.acquire(resources, transport, environment="simnow")
            driver = CtpNativeTraderDriver("three-discover", transport.account_id, transport,
                                           enable_simnow_orders=False)
            resources.callback(driver.stop)
            driver.start(lambda _: None)
            if not driver.is_simnow_authenticated:
                raise RuntimeError("只读会话未确认SimNow身份")
            if driver.reconcile_active_orders().orders:
                raise RuntimeError("账户有活动单，先保留并核查原运行，禁止并发测试")
            fields["observed_td_trading_day"] = driver.trading_day
            fields["gross_positions"] = occupied_positions(transport)
            source = ReferenceSourceFactory.create_for(DataSourcePurpose.LIVE_REFERENCE, "dolphindb",
                                                      DolphinDbReferenceConfig.from_env())
            resources.enter_context(source)
            references = [LiveFuturesReferences(source, products=(product,), trading_day=driver.trading_day,
                started_ns=time.time_ns(), roles=roles, factor_roles=(), refresh_seconds=0)
                for product, roles in [(p, tuple(dict.fromkeys(("main", *selected_roles))) if p == selected_product else ("main",))
                    for p in dict.fromkeys((selected_product, *args.cross_products))]]
            source_days = {r.snapshot(time.time_ns()).source_day.strftime("%Y%m%d") for r in references}
            fields["observed_source_days"] = sorted(source_days)
            if len(source_days) != 1:
                raise RuntimeError("声明品种资料来源日不一致，不能生成同一会话")
            lifecycle = SimpleNamespace(transport=transport, md_front=required("CTP_MD_ADDRESS"))
            from dataclasses import replace
            feed = CtpLiveDataFeed(replace(build_md_config(CtpMdConfig, lifecycle, flow_path=str(report.path / "md-flow")),
                future_buffer_ns=args.ctp_future_buffer_ms * 1_000_000))
            resources.callback(feed.disconnect)
            instruments = {}
            for references_by_product in references:
                for product, specs in references_by_product.instrument_specs.items():
                    for role, spec in specs.items():
                        from bomber.framework.dataprep.live_portfolio import ctp_futures_symbol
                        instrument = InstrumentId.from_str(f"{ctp_futures_symbol(spec.symbol, spec.venue)}.{spec.venue}")
                        instruments[instrument] = spec
                        fields.setdefault("contracts", {}).setdefault(product, {})[role] = str(instrument)
                        fields.setdefault("instrument_specs", {})[str(instrument)] = {"tick": str(spec.tick), "multiplier": str(spec.multiplier)}
            for instrument, spec in instruments.items():
                feed.register_instrument(InstrumentMeta(instrument,
                    price_precision=max(0, -spec.tick.normalize().as_tuple().exponent),
                    price_increment=spec.tick, multiplier=spec.multiplier, exchange=spec.venue, currency="CNY"))
                feed.subscribe(instrument, DataType.QUOTE_TICK)
            import threading
            quote_lock, quotes = threading.RLock(), {}
            def on_quote(tick):
                with quote_lock:
                    quotes[str(tick.instrument_id)] = {"bid": str(tick.bid_price.as_decimal()), "ask": str(tick.ask_price.as_decimal()),
                        "ts_event": tick.ts_event, "received_monotonic_ns": time.monotonic_ns()}
            feed.register_quote_tick_handler(on_quote)
            feed.connect()
            until = time.monotonic() + 30
            while True:
                with quote_lock:
                    complete = {str(i) for i in instruments} <= quotes.keys()
                if set(instruments) <= set(feed.depth_observations) and complete:
                    observed_days = {row.trading_day for row in feed.depth_observations.values()}
                    if len(observed_days) == 1:
                        profile = CtpLiveProfile(args.environment,
                            next(iter(observed_days)) if args.environment == "replay" else None)
                        market = CtpMarketChannel(feed, driver.trading_day, profile=profile,
                            instrument_health=InstrumentHealthGate(feed, instruments, driver.trading_day, policy=profile))
                        readiness = market.check()
                        fields["market_readiness"] = readiness.as_dict()
                        if readiness.ready:
                            break
                if time.monotonic() >= until:
                    raise RuntimeError(f"只读观察未取得完整健康声明合约MD；保留实际MD观察: {fields.get('market_readiness')}")
                time.sleep(0.2)
            with quote_lock:
                fields["quotes"] = deepcopy(quotes)
            fields["budget_upper_bound"] = observed_budget(fields["contracts"], fields["instrument_specs"],
                fields["quotes"], args.cross_products, product=selected_product, roles=selected_roles)
            days = {row.trading_day for row in feed.depth_observations.values()}
            fields["observed_md_trading_days"] = sorted(days)
            if len(days) != 1:
                raise RuntimeError("原始MD日不一致")
            fields["session_arguments"] = {"trading_day": driver.trading_day,
                "source_day": next(iter(source_days)),
                "replay_md_trading_day": next(iter(days)) if args.environment == "replay" else None}
            status = "observed"
        except BaseException as error:
            failure = str(error)
            raise
        finally:
            if feed:
                fields["markets"] = {str(i): wire(row) for i, row in feed.depth_observations.items()}
            result = report.write(status=status, fields={**fields, "failure": failure,
                "environment": args.environment, "verification": "readonly_dates_and_contracts",
                "orders_submitted": driver.submitted_orders if driver else 0}, session=None, events=())
            if args.output:
                import json
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")


if __name__ == "__main__":
    main()
