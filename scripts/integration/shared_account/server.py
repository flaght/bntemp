"""同机独立策略账户服务；唯一TD写入者，策略算法在独立worker进程运行。"""

import argparse
from pathlib import Path
import secrets
import time

from .config import read_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--credentials-dir", type=Path, required=True)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--confirm-simnow", action="store_true")
    parser.add_argument("--resume", action="store_true")
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
        host = build_host(config, resume=args.resume, diagnostics=diagnostics, strategy_processes=True)
        keys = {sid: secrets.token_hex(32) for sid in host.strategies}
        protocol = AccountExecutionProtocol(host.service, keys=keys, trading_day=config["trading_day"])
        host.account.start(resume=args.resume)
        if any(row["status"] != "completed" for row in protocol.journal.requests.values()):
            raise RuntimeError("恢复存在未决跨进程请求，须权威核查，拒绝自动重复发单")
        host.service.start()
        host.account.authorize()
        args.credentials_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        server = UnixExecutionServer(args.socket, protocol)
        for sid, key in keys.items():
            import os
            path = args.credentials_dir / f"{sid}.json"
            # 文件仅传递本机能力令牌，不打印令牌；服务重启后旧连接失效。
            descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encode({"version": 1, "strategy_id": sid, "key": key,
                    "socket": str(args.socket.resolve()), "configuration": host.service._ports[sid].configuration(),
                    "bootstrap_signal": host.service.bootstrap_signals.get(sid),
                    "reference": getattr(host.strategies[sid].session, "session_identity", None)}))
        print(f"账户服务就绪：socket={args.socket}；策略客户端文件目录={args.credentials_dir}", flush=True)
        until = time.monotonic() + config["seconds"] + 120  # 给分终端启动和策略先停机留出时间。
        while time.monotonic() < until:
            server.poll()
            protocol.poll()
            host.account.poll()
            time.sleep(0.02)
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


if __name__ == "__main__":
    main()
