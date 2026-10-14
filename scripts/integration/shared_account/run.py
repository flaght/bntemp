"""配置化同账户多策略SimNow宿主；默认仅校验配置，显式授权后连接。"""

import argparse
from contextlib import ExitStack
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from .config import read_config, validate_budget_increase

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--confirm-simnow", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reconcile-only", action="store_true", help="同日权威对账并修复已结束订单的检查点；不启动策略、不报单")
    parser.add_argument("--budget-config", type=Path, help="只读迁移至仅上调账户总名义限额的新配置")
    parser.add_argument("--acceptance-cancel-owner", help="观察自然共享母单，由指定非提交者停止自身触发撤单验收")
    args = parser.parse_args(argv)
    if args.acceptance_cancel_owner and (not args.connect or args.reconcile_only):
        parser.error("acceptance-cancel-owner仅用于显式授权的连接交易运行")
    if args.budget_config is not None and not args.reconcile_only:
        parser.error("budget-config仅用于reconcile-only")
    if args.reconcile_only and (not args.connect or not args.resume or args.enable_orders or args.confirm_simnow):
        parser.error("reconcile-only须connect和resume，不接受报单授权开关")
    if args.connect and not args.reconcile_only and not (args.enable_orders and args.confirm_simnow):
        parser.error("连接共享报单宿主须显式enable-orders和confirm-simnow")
    if not args.connect and (args.enable_orders or args.confirm_simnow or args.resume):
        parser.error("默认仅校验配置，不接受报单或恢复开关")
    return args


def validate_td_preflight(config, driver, *, md_front, td_front, diagnostics):
    """先保留TD实际返回值，再校验；组装失败也能写入报告，不猜测交易日。"""
    diagnostics.update(environment=config["environment"], expected_td_trading_day=config["trading_day"],
        observed_td_trading_day=driver.trading_day, md_front=md_front, td_front=td_front,
        preflight_orders_submitted=driver.submitted_orders)
    diagnostics["position_scope_policy"] = config.get("position_scope", "account")
    print(f"共享账户TD预检: environment={diagnostics['environment']} "
          f"actual_TD={diagnostics['observed_td_trading_day']} "
          f"expected_TD={diagnostics['expected_td_trading_day']} MD={md_front} TD={td_front} "
          f"submitted_orders={diagnostics['preflight_orders_submitted']}", flush=True)
    if diagnostics["observed_td_trading_day"] != diagnostics["expected_td_trading_day"]:
        raise RuntimeError(f"TD交易日与共享配置不一致: actual={diagnostics['observed_td_trading_day']} "
            f"expected={diagnostics['expected_td_trading_day']} environment={diagnostics['environment']} "
            f"TD={td_front}；请核对TD前置和会话环境，并用只读结果确认配置交易日")


def build_host(config, *, resume, diagnostics=None, read_only=False, strategy_processes=False):
    from bomber.framework.trader.execution.ctp import CtpNativeTraderDriver, CtpTdApiTransport
    from bomber.framework.trader.execution.builders import build_ctp_portfolio_execution
    from bomber.framework.trader.execution.shared import SharedAccountExecutionService, SharedRiskBudget
    from bomber.framework.trader.execution.risk import RiskLimits
    from bomber.framework.trader.runtime.live.channels.ctp import (build_simnow_transport, required,
        assert_flat_account, assert_account_ready, validate_replay_environment)
    from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle
    from bomber.framework.trader.runtime.live.shared import SharedAccountLiveHost, SharedStrategySession
    from bomber.framework.trader.runtime.live.contracts import LiveRunContext
    from bomber.framework.trader.runtime.live.recovery import InstrumentPositionScope
    from .factories import strategy_arguments, instrument_specs, assemble_strategy_session

    resources = ExitStack()
    diagnostics = {} if diagnostics is None else diagnostics
    driver = host = None
    try:
        account_config = config["account"]
        transport = build_simnow_transport(CtpTdApiTransport, client_id=account_config["client_id"],
            flow_path=f"/tmp/{account_config['client_id']}-td", timeout_seconds=15)
        md_front = required("CTP_MD_ADDRESS")
        if config["environment"] == "replay":
            validate_replay_environment(md_front, transport.front, transport.production_mode)
        CtpSharedAccountLifecycle.acquire(resources, transport, environment="simnow")
        driver_holder = {}
        def disconnected(reason):
            client = driver_holder.get("client")
            if client is not None:
                client.mark_disconnected(reason)
        driver = CtpNativeTraderDriver(transport.client_id, transport.account_id, transport,
            enable_simnow_orders=not read_only, max_session_orders=account_config["max_session_orders"],
            disconnect_handler=disconnected)
        driver.start(lambda report: None)
        validate_td_preflight(config, driver, md_front=md_front, td_front=transport.front,
                              diagnostics=diagnostics)
        if resume:
            assert_account_ready(driver)
        elif config.get("position_scope", "account") == "account":
            assert_flat_account(driver, transport)
        else:
            assert_account_ready(driver)
        prepared, specs = [], {}
        for row in config["strategies"]:
            entry, args = strategy_arguments(row, config,
                resume=(Path(row["state_file"]).is_file() if strategy_processes else resume))
            context = LiveRunContext(driver.trading_day, resources)
            lifecycle = SimpleNamespace(transport=transport, md_front=md_front)
            # 每个MD拥有独立flow目录；同策略类型也可以多实例。
            inputs = entry.prepare_inputs(args, context, lifecycle)
            upstream_config = getattr(inputs.upstream, "config", None)
            if upstream_config is not None:
                from dataclasses import replace
                inputs.upstream.config = replace(upstream_config,
                    flow_path=f"/tmp/{account_config['client_id']}-{row['strategy_id']}-md")
            for instrument, spec in instrument_specs(row["kind"], inputs.references).items():
                if instrument in specs and (spec.tick, spec.multiplier) != (specs[instrument].tick, specs[instrument].multiplier):
                    raise ValueError("共享合约的跳价和乘数声明冲突")
                specs[instrument] = spec
            prepared.append((row, entry, args, inputs))
        position_scope = (InstrumentPositionScope(specs) if config.get("position_scope") == "strategy_instruments"
                          else None)
        if position_scope is not None:
            from bomber.framework.trader.runtime.live.channels.ctp import occupied_positions
            diagnostics["managed_instruments"] = sorted(position_scope.instruments)
            diagnostics["external_gross"] = position_scope.external(occupied_positions(transport))
            if not resume:
                assert_flat_account(driver, transport, position_scope=position_scope)
            print(f"共享账户管理合约: {diagnostics['managed_instruments']} "
                  f"范围外总仓={diagnostics['external_gross']}；不产生策略归属", flush=True)
        driver.stop()  # 只读预检完成后安装唯一正式执行和写前钩子。
        budget = SharedRiskBudget(account_config["max_gross_quantity"], account_config["max_gross_notional"])
        limits = {i: RiskLimits(max_order_quantity=budget.max_gross_quantity,
            max_abs_position=budget.max_gross_quantity, max_order_notional=budget.max_gross_notional,
            max_abs_position_notional=budget.max_gross_notional, max_market_age_ns=120_000_000_000,
            contract_multiplier=spec.multiplier) for i, spec in specs.items()}
        execution = build_ctp_portfolio_execution(driver, trading_day=config["trading_day"],
            price_increments={i: spec.tick for i, spec in specs.items()},
            multipliers={i: spec.multiplier for i, spec in specs.items()}, instrument_limits=limits,
            session_check=lambda: driver.is_simnow_session, orders=True,
            limit_offset_ticks=account_config["limit_offset_ticks"],
            instrument_scope=position_scope,
            generic_close_venues=("DCE", "CZCE"), generic_close_today_first_venues=("DCE", "CZCE"))
        driver_holder["client"] = execution.client
        service = SharedAccountExecutionService(execution.client, prices=execution.prices,
            instrument_limits=limits, budget=budget, instrument_scope=position_scope,
            execution_mode=config.get("execution_mode", "netted"))
        sessions = []
        for row, entry, args, inputs in prepared:
            session = assemble_strategy_session(entry, args, inputs, service=service,
                driver=driver, strategy_id=row["strategy_id"])
            session.client.ready_check = session.runner.session_ready
            sessions.append(SharedStrategySession(session, Path(row["state_file"]),
                identity={"strategy_id": row["strategy_id"], "kind": row["kind"], "argv": row["argv"],
                    "trading_day": config["trading_day"], "environment": config["environment"],
                    "routes": session.client.configuration(),
                    "reference": str(session.references.spec)}))
        identity = {"trading_day": config["trading_day"], "environment": config["environment"],
            "replay_md_trading_day": config.get("replay_md_trading_day"), "account": account_config,
            "strategies": {item.strategy_id: item.identity for item in sessions}}
        if position_scope is not None:
            identity["managed_instruments"] = sorted(position_scope.instruments)
        account = CtpSharedAccountLifecycle(execution, service, transport=transport,
            state_file=config["account_state_file"], identity=identity, resources=resources,
            expected_day=config["trading_day"], position_scope=position_scope)
        host = SharedAccountLiveHost(account, service, sessions, resources=resources, resume=resume)
        return host
    except BaseException:
        if driver is not None:
            driver.stop()
        resources.close()
        raise


def main(argv=None):
    args = parse_args(argv)
    new_account_config = None
    try:
        config = read_config(args.config)
        if args.acceptance_cancel_owner and args.acceptance_cancel_owner not in {
                row["strategy_id"] for row in config["strategies"]}:
            raise ValueError("撤单验收所有者须为配置中已声明的策略ID")
        if args.budget_config is not None:
            new_account_config = validate_budget_increase(config, read_config(args.budget_config))
    except FileNotFoundError:
        print(f"配置文件不存在：{args.config.resolve()}。请先复制example.json为实际会话配置，"
              "核对交易日、资料日及限额后，再用--config指定该文件。未建立任何连接。", file=sys.stderr)
        raise SystemExit(2) from None
    if not args.connect:
        print(f"共享配置通过：{len(config['strategies'])}个策略；未连接行情、数据库或柜台")
        return
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    from bomber.framework.trader.runtime.reports import LiveRunReport
    report = LiveRunReport(config["report_dir"], prefix="shared-simnow", metadata={"config": config})
    report.begin()
    host = None
    diagnostics = {}
    failure_execution_diagnostics = None
    status, failure, shutdown_failure = "failed", None, None
    reconciliation = None
    from .cancel_probe import SharedMotherCancelProbe
    cancel_probe = SharedMotherCancelProbe(args.acceptance_cancel_owner) if args.acceptance_cancel_owner else None
    try:
        if args.reconcile_only:
            host = build_host(config, resume=True, diagnostics=diagnostics, read_only=True)
            from bomber.framework.trader.runtime.live.local import strategy_state_lock
            for item in host.strategies.values():
                host.resources.enter_context(strategy_state_lock(item.state_file))
            reconciliation = (host.account.reconcile_checkpoint(new_account_config=new_account_config)
                if new_account_config is not None else host.account.reconcile_checkpoint())
            status = "reconciled"
            return
        host = build_host(config, resume=args.resume, diagnostics=diagnostics)
        host.start()
        started = time.monotonic()
        until = started + config["seconds"]
        while time.monotonic() < until and any(item.active for item in host.strategies.values()):
            host.poll()
            if cancel_probe is not None:
                cancel_probe.poll(host)
            for row in config["strategies"]:
                if row.get("stop_after_seconds") is not None and time.monotonic() - started >= float(row["stop_after_seconds"]):
                    host.stop_strategy(row["strategy_id"], reason="configured_stop")
            time.sleep(0.2)
        status = "failed" if any(item.failure not in {None, "configured_stop", "acceptance_shared_cancel"}
                                 for item in host.strategies.values()) else "completed"
    except BaseException as error:
        failure = str(error)
        if host is not None:
            try:
                failure_execution_diagnostics = host.snapshot().get("execution_diagnostics")
            except Exception as snapshot_error:
                failure_execution_diagnostics = {"snapshot_error": str(snapshot_error)}
        raise
    finally:
        try:
            if host is not None:
                if args.reconcile_only:
                    host.account.client.stop()
                    host.account.driver.stop()
                    host.resources.close()
                else:
                    host.stop()
        except Exception as error:
            status, shutdown_failure = "failed", str(error)
            if failure is None:
                failure = shutdown_failure
            raise
        finally:
            snapshot = host.snapshot() if host is not None else {}
            if cancel_probe is not None:
                snapshot["shared_cancel_acceptance"] = cancel_probe.result(host, runtime_status=status)
            report.write(status=status, fields={**snapshot, "failure": failure,
                "shutdown_failure": shutdown_failure,
                "failure_execution_diagnostics": failure_execution_diagnostics,
                "reconciliation": reconciliation,
                "td_preflight": diagnostics,
                "verification": ("checkpoint_reconciled_no_strategy_no_orders" if args.reconcile_only
                    else "runtime_completed_real_fill_acceptance_separate"),
                "final_gross": host.account.final_gross if host else None,
                "position_scope": host.account.scope_diagnostics() if host else None,
                "final_active_orders": host.account.final_active_orders if host else None},
                session=host, events=host.account.execution.backend.reports if host else ())


if __name__ == "__main__":
    main()
