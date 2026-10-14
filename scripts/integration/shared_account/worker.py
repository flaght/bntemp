"""单策略进程；复用demo算法和Runner，通过本机账户服务执行。"""

import argparse
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import time

from .config import read_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--confirm-simnow", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    config = read_config(args.config)
    rows = [r for r in config["strategies"] if r["strategy_id"] == args.strategy_id]
    if len(rows) != 1 or config.get("execution_mode") != "independent":
        parser.error("须声明独立模式及配置中的单策略ID")
    row = rows[0]
    if args.connect and not (args.enable_orders and args.confirm_simnow):
        parser.error("策略连接须显式双报单授权")
    if not args.connect:
        if args.enable_orders or args.confirm_simnow or args.resume:
            parser.error("离线检查不接受授权或恢复")
        print(f"策略进程配置通过：{args.strategy_id}；未连接")
        return
    from dotenv import load_dotenv
    from .run import PROJECT_ROOT
    from .factories import strategy_arguments, assemble_strategy_session
    from bomber.framework.trader.execution.ipc import UnixExecutionConnection
    from bomber.framework.trader.execution.ipc_client import RemoteAccountService, RemoteTraderSession
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.runtime.live.contracts import LiveRunContext
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    from bomber.framework.trader.runtime.live.channels.ctp import required
    from bomber.framework.trader.runtime.reports import LiveRunReport
    load_dotenv(PROJECT_ROOT / ".env")
    identity = {"strategy_id": row["strategy_id"], "kind": row["kind"], "argv": row["argv"],
        "trading_day": config["trading_day"], "environment": config["environment"],
        "replay_md_trading_day": config.get("replay_md_trading_day"), "execution_mode": "independent"}
    report = LiveRunReport(config["report_dir"], prefix=row["strategy_id"], metadata=identity)
    report.begin()
    session, failure, shutdown_failure, status = None, None, None, "failed"
    save = None
    with ExitStack() as resources:
        try:
            resources.enter_context(strategy_state_lock(Path(row["state_file"])))
            store = JsonStateStore(Path(row["state_file"]))
            persisted = store.load()
            if bool(persisted) != args.resume or (persisted and persisted.payload["identity"] != identity):
                raise ValueError("策略进程恢复标志、身份或状态文件不一致")
            credentials = json.loads(args.credentials.read_text())
            if credentials["strategy_id"] != args.strategy_id:
                raise ValueError("客户端凭据不属于本策略")
            connection = UnixExecutionConnection(Path(credentials["socket"]), args.strategy_id, credentials["key"])
            state = connection.call("describe", {})["state"]
            if state["trading_day"] != config["trading_day"]:
                raise ValueError("账户服务交易日不一致")
            service = RemoteAccountService(connection, credentials["configuration"], state)
            driver = RemoteTraderSession(service, config["trading_day"])
            entry, strategy_args = strategy_arguments(row, config, resume=args.resume)
            import os
            lifecycle = SimpleNamespace(md_front=required("CTP_MD_ADDRESS"), transport=SimpleNamespace(
                investor_id=required("CTP_ACCOUNT_ID"), password=required("CTP_PASSWORD"),
                production_mode=os.getenv("CTP_PRODUCTION_MODE", "true").lower() in {"1", "true", "yes", "on"}))
            from datetime import datetime
            context = LiveRunContext(resources=resources, trading_day=datetime.strptime(config["trading_day"], "%Y%m%d").date())
            inputs = entry.prepare_inputs(strategy_args, context, lifecycle)
            inputs.upstream.config = replace(inputs.upstream.config,
                flow_path=f"/tmp/{config['account']['client_id']}-{args.strategy_id}-worker-md")
            session = assemble_strategy_session(entry, strategy_args, inputs,
                service=service, driver=driver, strategy_id=args.strategy_id)
            port = session.client
            if persisted and persisted.payload["revision"] > state["source_revision"]:
                raise ValueError("策略信号版本领先账户服务")
            if not persisted and state["source_revision"]:
                raise ValueError("账户已有策略来源，须使用原策略检查点恢复")
            session.strategy.restore_target_revision(state["source_revision"])
            session.strategy.fills_received = persisted.payload.get("fills_received", 0) if persisted else 0
            port.set_cursor(persisted.payload.get("cursor", 0) if persisted else 0)
            generation = persisted.generation if persisted else 0
            def save(cursor=None):
                nonlocal generation
                saved = store.save({"version": 1, "identity": identity,
                    "revision": port.state["source_revision"], "cursor": port.cursor if cursor is None else cursor,
                    "fills_received": session.strategy.fills_received}, expected_generation=generation)
                generation = saved.generation
            port.completed = save
            port.ready_check = session.runner.session_ready
            session.runner.start()
            until_ready = time.monotonic() + 30
            while not session.runner.session_ready():
                port.drain_events()
                session.references.refresh()
                if time.monotonic() >= until_ready:
                    raise RuntimeError("策略未在期限内取得健康行情/参考/账户")
                time.sleep(0.2)
            port.drain_events()
            save()
            session.runner.begin_bars()
            until = time.monotonic() + min(config["seconds"], row.get("stop_after_seconds", config["seconds"]))
            while time.monotonic() < until:
                with session.runner._submit_lock:
                    port.drain_events()
                    session.references.refresh()
                    if session.runner.failure or not session.runner.session_ready() or not session.runner.reference_is_current():
                        raise RuntimeError(session.runner.failure or "策略行情/参考/账户失效")
                    save()
                time.sleep(0.2)
            status = "completed"
        except BaseException as error:
            failure = str(error) or type(error).__name__
            raise
        finally:
            try:
                if session:
                    with session.runner._submit_lock:
                        session.runner.accept_bars = False
                        session.client.begin_stop()  # 撤单、同步归属及回调完成后才解绑。
                        if save is not None:
                            save()
                    session.runner.stop()
            except Exception as error:
                shutdown_failure, status = str(error), "failed"
                raise
            finally:
                report.write(status=status, fields={"failure": failure, "shutdown_failure": shutdown_failure,
                    "verification": "independent_strategy_process", "strategy_id": args.strategy_id,
                    "positions": session.client.state["positions"] if session else None,
                    "cursor": session.client.cursor if session else None,
                    "fills_received": session.strategy.fills_received if session else None,
                    "source_revision": session.client.state["source_revision"] if session else None,
                    "last_targets": getattr(session.strategy, "last_targets", None) if session else None,
                    "reference_manifest": getattr(session.references, "manifest", None) if session else None,
                    "bar_counts": {str(i): n for i, n in getattr(session.runner, "bar_counts", {}).items()} if session else {},
                    "last_processed_ns": getattr(session.strategy, "last_processed_ns", None) if session else None},
                    session=session, events=session.client.received_events if session else ())


if __name__ == "__main__":
    main()
