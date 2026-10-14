"""只读观察三策略所需TD日、资料日、原始MD日和合约；不创建交易检查点。"""

import argparse
from contextlib import ExitStack
from pathlib import Path
import time


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true", required=True)
    parser.add_argument("--environment", choices=("replay", "realtime"), required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("scripts/integration/shared_account/results"))
    parser.add_argument("--output", type=Path, help="保存本次只读观察摘要，供会话生成器读取")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    from .run import PROJECT_ROOT
    from bomber.framework.trader.execution.ctp import CtpNativeTraderDriver, CtpTdApiTransport
    from bomber.framework.trader.runtime.live.channels.ctp import (
        build_simnow_transport, build_md_config, required, validate_replay_environment, occupied_positions)
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
    status, failure, driver, feed, fields = "failed", None, None, None, {}
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
                for product, roles in (("CU", ("main", "secondary", "far")), ("AL", ("main",)))]
            source_days = {r.snapshot(time.time_ns()).source_day.strftime("%Y%m%d") for r in references}
            fields["observed_source_days"] = sorted(source_days)
            if len(source_days) != 1:
                raise RuntimeError("CU/AL资料来源日不一致，不能生成同一会话")
            lifecycle = SimpleNamespace(transport=transport, md_front=required("CTP_MD_ADDRESS"))
            feed = CtpLiveDataFeed(build_md_config(CtpMdConfig, lifecycle, flow_path=str(report.path / "md-flow")))
            resources.callback(feed.disconnect)
            instruments = {}
            for references_by_product in references:
                for product, specs in references_by_product.instrument_specs.items():
                    for role, spec in specs.items():
                        instrument = InstrumentId.from_str(f"{spec.symbol.lower()}.{spec.venue}")
                        instruments[instrument] = spec
                        fields.setdefault("contracts", {}).setdefault(product, {})[role] = str(instrument)
            for instrument, spec in instruments.items():
                feed.register_instrument(InstrumentMeta(instrument,
                    price_precision=max(0, -spec.tick.normalize().as_tuple().exponent),
                    price_increment=spec.tick, multiplier=spec.multiplier, exchange=spec.venue, currency="CNY"))
                feed.subscribe(instrument, DataType.QUOTE_TICK)
            feed.connect()
            until = time.monotonic() + 30
            while not set(instruments) <= set(feed.depth_observations):
                if time.monotonic() >= until:
                    raise RuntimeError("只读观察未取得完整CU/AL逐合约MD；保留实际MD观察，不猜测日期")
                time.sleep(0.2)
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
