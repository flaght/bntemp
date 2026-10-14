"""三策略TD/参考/逐合约MD只读预检；不运行算法，不启用报单。"""

import argparse
from pathlib import Path
import time

from .config import read_config


def connect_session_market(session, connected):
    """只读原始深度不依赖Runner启动产生的Bar订阅。"""
    if hasattr(session, "feeds"):
        for feed in set(session.feeds.values()):
            connected.append(feed)
            feed.connect()
        return
    from bomber.framework.market.basic.base import DataType
    upstream = session.upstream
    for instrument in sorted(set(session.client.routes.values()), key=str):
        upstream.subscribe(instrument, DataType.QUOTE_TICK)
    connected.append(upstream)  # connect部分失败也须关闭已建立的资源。
    upstream.connect()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    config = read_config(args.config)
    if not args.connect:
        print(f"三策略配置通过：{len(config['strategies'])}个策略；未连接")
        return
    from dotenv import load_dotenv
    from .run import PROJECT_ROOT, build_host
    from bomber.framework.trader.execution.ipc import wire
    from bomber.framework.trader.runtime.reports import LiveRunReport
    load_dotenv(PROJECT_ROOT / ".env")
    report = LiveRunReport(config["report_dir"], prefix="three-readonly", metadata={"config": config})
    report.begin()
    host, failure, status, diagnostics, markets = None, None, "failed", {}, {}
    try:
        host = build_host(config, resume=args.resume, diagnostics=diagnostics, read_only=True,
                          strategy_processes=True)
        connected = []
        for item in host.strategies.values():
            connect_session_market(item.session, connected)
        until = time.monotonic() + 180
        while True:
            markets = {sid: {str(i): wire(observation) for i, observation in
                getattr(item.session.upstream, "depth_observations", {}).items()}
                for sid, item in host.strategies.items()}
            healthy = []
            for sid, item in host.strategies.items():
                if hasattr(item.session, "feeds"):
                    item.session.runner.poll_features()
                    checks = [channel.check() for channel in item.session.readiness.channels
                              if channel.identity.domain == "market"]
                    markets[sid] = {"sources": [r.as_dict() for r in checks]}
                    healthy.append(all(r.ready for r in checks))
                else:
                    healthy.append(bool(markets[sid] and set(markets[sid]) >= set(
                        item.session.client.configuration()["routes"].values())))
            if all(healthy):
                break
            if time.monotonic() >= until:
                raise RuntimeError("预检未取得三个策略完整原始深度；查看markets中的实际MD交易日")
            time.sleep(0.2)
        print(f"原始逐合约MD观察：{markets}", flush=True)
        for item in host.strategies.values():
            health = getattr(item.session.runner, "instrument_health", None)
            stale = health.stale_instruments() if health is not None else {}
            if stale:
                raise RuntimeError(f"逐合约行情未通过环境/时效检查: {item.strategy_id}: {wire(stale)}")
        status = "prechecked"
    except BaseException as error:
        failure = str(error)
        raise
    finally:
        try:
            if host:
                for feed in reversed(locals().get("connected", [])):
                    feed.disconnect()
                host.account.driver.stop()
                host.resources.close()
        finally:
            report.write(status=status, fields={"failure": failure, "td_preflight": diagnostics,
                "markets": markets, "verification": "readonly_no_strategy_no_orders",
                "orders_submitted": host.account.driver.submitted_orders if host else 0}, session=host, events=())


if __name__ == "__main__":
    main()
