"""单策略进程；复用demo算法和Runner，通过本机账户服务执行。"""

import argparse
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import time

from .config import read_config


def session_reference_is_current(session):
    """沿用共享宿主的固定参考检查，兼容没有专属检查方法的Runner。"""
    current = getattr(session.runner, "reference_is_current", None)
    return current() if callable(current) else session.references.spec == session.runner.fixed_spec


def capture_session_failure(session):
    """停机清空Feed之前保存原健康结果及本策略账户连接状态。"""
    from bomber.framework.trader.runtime.live.health import readiness_failure_context
    context = readiness_failure_context(session.runner)
    port = session.client
    context["execution_port"] = {"started": port._started, "stopping": port._stopping,
        "reconciled": port.is_reconciled, "account_reconciled": port.state.get("reconciled"),
        "observation_age_seconds": time.monotonic() - port._observed}
    session.runner.failure_readiness = context
    return context


def require_session_health(session):
    runner = session.runner
    if runner.failure:
        raise RuntimeError(runner.failure)
    reason = None
    if not runner.session_ready():
        reason = "策略行情或账户未就绪"
    elif not session_reference_is_current(session):
        reason = "策略参考合约或条款变化"
    if reason:
        context = capture_session_failure(session)
        raise RuntimeError(f"{reason}: {context}")


def poll_strategy_session(session, save):
    # 原参考缓存只在发布版本时持提交锁；数据库查询允许行情分派继续推进。
    with session.runner._submit_lock:
        if session.runner.failure:
            raise RuntimeError(session.runner.failure)
    session.references.refresh()
    with session.runner._submit_lock:
        if session.runner.failure:
            raise RuntimeError(session.runner.failure)
        session.client.drain_events()
        poll = getattr(session.runner, "poll_live_inputs", None)
        if callable(poll):
            poll()
        require_session_health(session)
        save()


def _main(argv):
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
    from bomber.framework.trader.runtime.live.continuous import StopSignal
    with StopSignal() as stop_event:
        return _run(args, config, stop_event)


def _run(args, config, stop_event):
    row = next(row for row in config["strategies"] if row["strategy_id"] == args.strategy_id)
    from bomber.framework.trader.runtime.live.continuous import StopRequested
    from bomber.framework.trader.execution.contracts import MarketDataUnavailable, OrderBudgetExhausted
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
    metadata = dict(identity)
    if "history_missing_policy" in config:
        metadata["history_missing_policy"] = config["history_missing_policy"]
    if "ctp_instrument_max_age_seconds" in config:
        metadata["ctp_instrument_max_age_seconds"] = config["ctp_instrument_max_age_seconds"]
    report = LiveRunReport(config["report_dir"], prefix=row["strategy_id"], metadata=metadata)
    report.begin()
    session, failure, shutdown_failure, status = None, None, None, "failed"
    stop_reason = None
    save = None
    with ExitStack() as resources:
        try:
            resources.enter_context(strategy_state_lock(Path(row["state_file"])))
            store = JsonStateStore(Path(row["state_file"]))
            persisted = store.load()
            credentials = json.loads(args.credentials.read_text())
            bootstrap = credentials.get("bootstrap_signal")
            if bool(persisted) != args.resume and not (args.resume and persisted is None and bootstrap is not None):
                raise ValueError("策略进程恢复标志、身份或状态文件不一致")
            if persisted and persisted.payload["identity"] != identity:
                from .rollover import validate_strategy_identity
                validate_strategy_identity(persisted.payload["identity"], identity)
            if credentials["strategy_id"] != args.strategy_id:
                raise ValueError("客户端凭据不属于本策略")
            connection = UnixExecutionConnection(Path(credentials["socket"]), args.strategy_id, credentials["key"])
            state = connection.call("describe", {})["state"]
            if state["trading_day"] != config["trading_day"]:
                raise ValueError("账户服务交易日不一致")
            service = RemoteAccountService(connection, credentials["configuration"], state)
            driver = RemoteTraderSession(service, config["trading_day"])
            entry, strategy_args = strategy_arguments(row, config, resume=args.resume)
            print(f"策略CTP逐合约时效: {getattr(strategy_args, 'instrument_max_age_seconds', 10)}秒；"
                  f"策略={args.strategy_id}", flush=True)
            import os
            from bomber.framework.market.sources import source_config
            lifecycle = SimpleNamespace(md_front=(required("CTP_MD_ADDRESS") if
                source_config(strategy_args).source == "ctp" else None), transport=SimpleNamespace(
                investor_id=required("CTP_ACCOUNT_ID"), password=required("CTP_PASSWORD"),
                production_mode=os.getenv("CTP_PRODUCTION_MODE", "true").lower() in {"1", "true", "yes", "on"}))
            from datetime import datetime
            context = LiveRunContext(resources=resources, trading_day=datetime.strptime(config["trading_day"], "%Y%m%d").date())
            inputs = entry.prepare_inputs(strategy_args, context, lifecycle)
            upstream = getattr(inputs, "upstream", getattr(inputs, "ctp_upstream", None))
            if getattr(upstream, "config", None) is not None and source_config(strategy_args).source == "ctp":
                upstream.config = replace(upstream.config,
                    flow_path=f"/tmp/{config['account']['client_id']}-{args.strategy_id}-worker-md")
            session = assemble_strategy_session(entry, strategy_args, inputs,
                service=service, driver=driver, strategy_id=args.strategy_id)
            port = session.client
            if hasattr(session, "session_identity") and credentials.get("reference") != session.session_identity:
                raise ValueError("08行情/特征/条款身份与账户服务声明不一致")
            if persisted and persisted.payload["revision"] > state["source_revision"]:
                raise ValueError("策略信号版本领先账户服务")
            if not persisted and state["source_revision"] and bootstrap is None:
                raise ValueError("账户已有策略来源，须使用原策略检查点恢复")
            session.strategy.restore_target_revision(state["source_revision"])
            if persisted and hasattr(session, "restore_signal"):
                session.restore_signal(persisted.payload["signal"])
            elif bootstrap is not None:
                session.restore_signal(bootstrap)
            session.strategy.fills_received = persisted.payload.get("fills_received", 0) if persisted else 0
            port.set_cursor(persisted.payload.get("cursor", 0) if persisted else 0)
            generation = persisted.generation if persisted else 0
            def save(cursor=None):
                nonlocal generation
                saved = store.save({"version": 1, "identity": identity,
                    "revision": port.state["source_revision"], "cursor": port.cursor if cursor is None else cursor,
                    "fills_received": session.strategy.fills_received,
                    **({"signal": session.signal_checkpoint.snapshot_state()}
                       if hasattr(session, "signal_checkpoint") else {})}, expected_generation=generation)
                generation = saved.generation
            port.completed = save
            port.ready_check = session.runner.session_ready
            session.runner.start()
            ready_seconds = strategy_args.ready_timeout if row["kind"] == "option_vega" else 30
            if source_config(strategy_args).source != "ctp":
                ready_seconds = max(ready_seconds, source_config(strategy_args).max_age_seconds)
            until_ready = time.monotonic() + ready_seconds
            while not session.runner.session_ready():
                if stop_event.is_set():
                    raise StopRequested()
                if session.runner.failure:
                    raise RuntimeError(session.runner.failure)
                port.drain_events()
                session.references.refresh()
                poll = getattr(session.runner, "poll_live_inputs", None)
                if callable(poll):
                    poll()
                if time.monotonic() >= until_ready:
                    context = capture_session_failure(session)
                    raise RuntimeError(f"策略未在期限内取得健康行情/参考/账户: {context}")
                time.sleep(0.2)
            port.drain_events()
            save()
            if stop_event.is_set():
                raise StopRequested()
            from bomber.framework.trader.runtime.live.shared import prepare_strategy_history
            prepare_strategy_history(session)
            if stop_event.is_set():
                raise StopRequested()
            session.runner.begin_bars()
            until = time.monotonic() + min(config["seconds"], row.get("stop_after_seconds", config["seconds"]))
            while not stop_event.is_set() and time.monotonic() < until:
                poll_strategy_session(session, save)
                time.sleep(0.2)
            status = "completed"
        except OrderBudgetExhausted:
            stop_reason, status = "order_budget_exhausted", "completed"
        except MarketDataUnavailable as error:
            stop_reason, status = "market_data_unavailable", "completed"
            try:
                context = capture_session_failure(session)
            except Exception as diagnostic_error:
                context = {"diagnostic_error": str(diagnostic_error)}
            context["account_valuation"] = error.readiness
            session.runner.failure_readiness = context
        except StopRequested:
            status = "completed"
        except BaseException as error:
            failure = str(error) or type(error).__name__
            if session and getattr(session.runner, "failure_readiness", None) is None:
                try:
                    capture_session_failure(session)
                except Exception as diagnostic_error:
                    session.runner.failure_readiness = {"diagnostic_error": str(diagnostic_error)}
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
                from bomber.framework.trader.runtime.live.shared import history_snapshot
                report.write(status=status, fields={"failure": failure, "shutdown_failure": shutdown_failure,
                    "stop_reason": stop_reason,
                    **(history_snapshot(session) if session else {}),
                    "failure_readiness": getattr(session.runner, "failure_readiness", None) if session else None,
                    "verification": "independent_strategy_process", "strategy_id": args.strategy_id,
                    "positions": session.client.state["positions"] if session else None,
                    "cursor": session.client.cursor if session else None,
                    "fills_received": session.strategy.fills_received if session else None,
                    "price_sync": getattr(session.client, "price_sync", None) if session else None,
                    "source_revision": session.client.state["source_revision"] if session else None,
                    "last_targets": getattr(session.strategy, "last_targets", None) if session else None,
                    "reference_manifest": getattr(session.references, "manifest", None) if session else None,
                    "bar_counts": {str(i): n for i, n in getattr(session.runner, "bar_counts", {}).items()} if session else {},
                    "last_processed_ns": getattr(session.strategy, "last_processed_ns", None) if session else None},
                    session=session, events=session.client.received_events if session else ())


def main(argv=None):
    return _main(argv)


if __name__ == "__main__":
    main()
