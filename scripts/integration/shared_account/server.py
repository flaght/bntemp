"""同机独立策略账户服务；唯一TD写入者，策略算法在独立worker进程运行。"""

import argparse
from pathlib import Path
import secrets
import time

from .config import read_config


def connect_valuation_market(host, config):
    """复用已准备的CTP原始Feed，观察全合约估值；不连接策略Runner或生成Bar。"""
    if config["environment"] != "realtime":
        return None
    feeds = [item.session.upstream for item in host.strategies.values()
        if hasattr(getattr(item.session, "upstream", None), "depth_observations")]
    if not feeds:
        return None
    from bomber.framework.market.basic.base import DataType
    from bomber.framework.trader.runtime.live.channels.ctp import CtpMarketChannel, InstrumentHealthGate
    from bomber.framework.trader.runtime.live.contracts import ReadinessIssue, ReadinessResult
    from decimal import Decimal
    feed, instruments = feeds[0], set()
    for raw in feeds:
        for key in ("front", "broker_id", "user_id", "production_mode", "timezone", "dce_use_receive_date", "future_buffer_ns"):
            if getattr(raw.config, key) != getattr(feed.config, key):
                raise ValueError("账户CTP估值源声明不一致")
    for item in host.strategies.values():
        raw = getattr(item.session, "upstream", None)
        if raw not in feeds:
            continue
        for instrument in item.session.client.routes.values():
            meta = raw.get_instrument_meta(instrument)
            if meta is None:
                raise ValueError("账户估值合约缺少原始元数据")
            feed.register_instrument(meta)
            instruments.add(instrument)
    for instrument in sorted(instruments, key=str):
        feed.subscribe(instrument, DataType.QUOTE_TICK)
    host.resources.callback(feed.disconnect)
    feed.connect()
    gate = InstrumentHealthGate(feed, instruments, config["trading_day"])
    quote_gate = InstrumentHealthGate(feed, instruments, config["trading_day"])
    class ValuationChannel(CtpMarketChannel):
        def check(self):
            positions = getattr(host.service, "position_manager", None)
            if positions is None:
                required = instruments
            else:
                owned = {row["instrument_id"] for row in positions.attribution.state()["owned"] if Decimal(row["quantity"])}
                owned.update(host.service._active_instruments())
                required = {i for i in instruments if str(i) in owned}
            self.instrument_health.instruments = frozenset(required)
            result = super().check()
            if not result.ready:
                return result
            invalid = quote_gate.stale_instruments()
            observations, updates, issues = dict(quote_gate.depth_records), [], []
            for instrument in instruments:
                row = observations.get(instrument)
                value = getattr(row, "reference_price", None)
                if value is None or str(instrument) in invalid:
                    if instrument in required:
                        issues.append(ReadinessIssue(self.identity, "PRICE_REFERENCE_MISSING", str(instrument)))
                else:
                    updates.append((instrument, Decimal(value), row.ts_event))
            if not issues:
                for instrument, price, stamp in updates:
                    host.service.prices.update(instrument, price, stamp)
            return ReadinessResult(result.sources, tuple(issues))
    return ValuationChannel(feed, config["trading_day"], instrument_health=gate)


def _main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--credentials-dir", type=Path, required=True)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--confirm-simnow", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--remaining-orders", type=int, help="持续调度从账户订单历史扣除后的本窗口额度")
    args = parser.parse_args(argv)
    config = read_config(args.config)
    if config.get("execution_mode") != "independent":
        parser.error("独立进程账户服务须execution_mode=independent")
    if args.connect and not (args.enable_orders and args.confirm_simnow):
        parser.error("连接须显式双报单授权")
    if not args.connect:
        if args.enable_orders or args.confirm_simnow or args.resume:
            parser.error("离线配置检查不接受授权或恢复")
        print(f"独立账户服务配置通过：{len(config['strategies'])}个策略；未连接")
        return
    from bomber.framework.trader.runtime.live.continuous import StopSignal
    with StopSignal() as stop_event:
        return _run(args, config, stop_event)


def _run(args, config, stop_event):
    from bomber.framework.trader.runtime.live.continuous import StopRequested
    from dotenv import load_dotenv
    from .run import build_host, PROJECT_ROOT
    from bomber.framework.trader.execution.ipc import AccountExecutionProtocol, UnixExecutionServer, encode
    from bomber.framework.trader.runtime.reports import LiveRunReport
    load_dotenv(PROJECT_ROOT / ".env")
    report = LiveRunReport(config["report_dir"], prefix="shared-account-server", metadata={"config": config})
    report.begin()
    host = server = None
    diagnostics, failure, shutdown_failure, status = {}, None, None, "failed"
    try:
        if args.socket.exists():
            raise RuntimeError("执行socket已存在；禁止接管其他账户服务")
        host = build_host(config, resume=args.resume, diagnostics=diagnostics, strategy_processes=True,
            session_order_budget=args.remaining_orders)
        keys = {sid: secrets.token_hex(32) for sid in host.strategies}
        protocol = AccountExecutionProtocol(host.service, keys=keys, trading_day=config["trading_day"])
        host.account.start(resume=args.resume)
        if any(row["status"] != "completed" for row in protocol.journal.requests.values()):
            raise RuntimeError("恢复存在未决跨进程请求，须权威核查，拒绝自动重复发单")
        host.service.start()
        valuation = connect_valuation_market(host, config)
        until_market = time.monotonic() + 30
        while valuation is not None and not valuation.check().ready:
            if stop_event.is_set():
                raise StopRequested()
            if time.monotonic() >= until_market:
                raise RuntimeError(f"账户估值行情未就绪: {valuation.check().as_dict()}")
            time.sleep(0.05)
        if valuation is not None:
            submission_check = host.account.execution.backend.submission_check
            host.account.execution.backend.submission_check = lambda: submission_check() and valuation.check().ready
        if stop_event.is_set():
            raise StopRequested()
        host.account.authorize()
        args.credentials_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        server = UnixExecutionServer(args.socket, protocol)
        for sid, key in keys.items():
            import os
            path = args.credentials_dir / f"{sid}.json"
            # 文件仅传递本机能力令牌，不打印令牌；服务重启后旧连接失效。
            from tempfile import NamedTemporaryFile
            with NamedTemporaryFile(dir=args.credentials_dir, prefix=f".{sid}-", delete=False) as output:
                temporary = Path(output.name)
                try:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(encode({"version": 1, "strategy_id": sid, "key": key,
                        "socket": str(args.socket.resolve()), "configuration": host.service._ports[sid].configuration(),
                        "bootstrap_signal": host.service.bootstrap_signals.get(sid),
                        "reference": getattr(host.strategies[sid].session, "session_identity", None)}))
                    output.flush(); os.fsync(output.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)

        print(f"账户服务就绪：socket={args.socket}；策略客户端文件目录={args.credentials_dir}", flush=True)
        until = time.monotonic() + config["seconds"] + 120  # 给分终端启动和策略先停机留出时间。
        while not stop_event.is_set() and time.monotonic() < until:
            if valuation is not None and not valuation.check().ready:
                raise RuntimeError(f"账户估值行情失效: {valuation.check().as_dict()}")
            server.poll()
            protocol.poll()
            host.account.poll()
            time.sleep(0.02)
        status = "completed"
    except StopRequested:
        status = "completed"
    except BaseException as error:
        failure = str(error) or type(error).__name__
        raise
    finally:
        try:
            if host:
                if server:
                    for sid in tuple(protocol.leases):
                        protocol.stop_owner(sid)
                host.account.stop()
                host.service.stop()
                host.resources.close()
            if server:
                server.close()
        except Exception as error:
            shutdown_failure, status = str(error), "failed"
            raise
        finally:
            report.write(status=status, fields={"failure": failure, "shutdown_failure": shutdown_failure,
                "td_preflight": diagnostics, "account": host.service.snapshot_state() if host else None,
                "final_gross": host.account.final_gross if host else None,
                "final_active_orders": host.account.final_active_orders if host else None,
                "verification": "independent_process_account_service"},
                session=host, events=host.account.execution.backend.reports if host else ())


def main(argv=None):
    return _main(argv)


if __name__ == "__main__":
    main()
