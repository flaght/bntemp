"""独立CTP账户运行：配置、账本恢复和IPC；不读取策略代码或业务配置。"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import secrets
import time
from types import SimpleNamespace

from bomber.framework.cli import load_environment

from bomber.framework.account_config import read_account_config, account_identity


def rollover(account, payload):
    from bomber.framework.trader.runtime.live.channels.ctp_rollover import observe_rollover
    from bomber.framework.trader.runtime.live.channels.ctp_owned import restore_external_baseline
    old = payload['component_states']['session_identity']['identity']
    new = account.manager.state_components['session_identity'].identity
    candidate = {**deepcopy(old), 'trading_day':new['trading_day']}
    if old['trading_day'] >= new['trading_day'] or candidate != new:
        raise ValueError('账户跨日只能前进交易日，不能隐式更改执行身份、额度或登记策略')
    shared = payload['component_states']['shared_account']
    if shared.get('failed') or shared.get('pending'):
        raise ValueError('账户存在未决发送或故障，保留原账本')
    journal = shared['shared']['event_journal']
    if any(row['status'] != 'completed' for row in journal['requests'].values()):
        raise ValueError('账户存在未决执行请求')
    account.rollover_audit = observe_rollover(payload, driver=account.driver, transport=account.transport,
        day=account.expected_day, multipliers={i:r.contract_multiplier for i,r in account.service.instrument_limits.items()},
        scope=account.position_scope, preserve_external=True, generic_close_venues=account.generic_close_venues)
    payload['component_states']['session_identity']['identity'] = new
    return restore_external_baseline(account, payload, refresh=True)


def build_host(config, *, resume, diagnostics=None, session_order_budget=None):
    from bomber.framework.market.basic.base import InstrumentId
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.execution.ctp import CtpNativeTraderDriver, CtpTdApiTransport
    from bomber.framework.trader.execution.builders import build_ctp_portfolio_execution
    from bomber.framework.trader.execution.shared import SharedAccountExecutionService, SharedRiskBudget
    from bomber.framework.trader.execution.registry import ExecutionRegistry, limits_from_wire
    from bomber.framework.trader.execution.ipc import ExecutionEventJournal
    from bomber.framework.trader.execution.risk import RiskLimits
    from bomber.framework.trader.execution.policy import AccountReconciliationMode
    from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport, assert_account_ready
    from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle
    from bomber.framework.trader.runtime.live.recovery import InstrumentPositionScope
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    if not resume:
        raise ValueError('账户服务须从已核验的账户执行登记恢复；首次使用独立迁移入口')
    saved = JsonStateStore(config['account_state_file']).load()
    if saved is None:
        raise ValueError('公共账户账本不存在；请配置已有公共账户账本，不自动以空归属接管旧仓')
    registry = ExecutionRegistry(saved.payload['component_states'].get('execution_registry'))
    settings = config['account']
    identity = account_identity(config, registry.state['ports'])
    previous = saved.payload['component_states']['session_identity']['identity']
    if {**previous,'trading_day':identity['trading_day']} != identity:
        raise ValueError('公共账户身份、额度或登记策略与账本不一致')
    remaining = settings['max_session_orders'] if session_order_budget is None else session_order_budget
    if type(remaining) is not int or not 0 < remaining <= settings['max_session_orders']:
        raise ValueError('账户剩余报单额度无效')
    resources, driver = ExitStack(), None
    try:
        resources.enter_context(strategy_state_lock(config['account_state_file']))
        transport = build_simnow_transport(CtpTdApiTransport, client_id=settings['client_id'],
            flow_path=f"/tmp/{settings['client_id']}-td")
        CtpSharedAccountLifecycle.acquire(resources, transport, environment='simnow')
        holder = {}
        def disconnected(reason):
            if 'client' in holder:
                holder['client'].mark_disconnected(reason)
        driver = CtpNativeTraderDriver(transport.client_id, transport.account_id, transport,
            enable_simnow_orders=True, max_session_orders=remaining, disconnect_handler=disconnected)
        driver.new_order_budget_window = session_order_budget is not None
        driver.start(lambda report:None)
        if driver.trading_day != config['trading_day']:
            raise ValueError(f"账户TD交易日不一致: actual={driver.trading_day} expected={config['trading_day']}")
        assert_account_ready(driver)
        if diagnostics is not None:
            diagnostics.update(actual_TD=driver.trading_day, submitted_orders=driver.submitted_orders)
        driver.stop()
        specifications = registry.state['instruments']
        scope = InstrumentPositionScope(specifications)
        budget = SharedRiskBudget(settings['max_gross_quantity'], settings['max_gross_notional'])
        limits = {InstrumentId.from_str(i):RiskLimits(max_order_quantity=budget.max_gross_quantity,
            max_abs_position=budget.max_gross_quantity, max_order_notional=budget.max_gross_notional,
            max_abs_position_notional=budget.max_gross_notional, max_market_age_ns=120_000_000_000,
            contract_multiplier=Decimal(row['multiplier'])) for i,row in specifications.items()}
        execution = build_ctp_portfolio_execution(driver, trading_day=config['trading_day'],
            price_increments={InstrumentId.from_str(i):Decimal(row['tick']) for i,row in specifications.items()},
            multipliers={i:r.contract_multiplier for i,r in limits.items()}, instrument_limits=limits,
            session_check=lambda:driver.is_simnow_session, orders=True,
            limit_offset_ticks=settings['limit_offset_ticks'], instrument_scope=scope,
            account_reconciliation=AccountReconciliationMode.OBSERVE,
            generic_close_venues=('DCE','CZCE','CFFEX'), generic_close_today_first_venues=('DCE','CZCE','CFFEX'))
        holder['client'] = execution.client
        service = SharedAccountExecutionService(execution.client, prices=execution.prices,
            instrument_limits=limits, budget=budget, instrument_scope=scope, execution_mode='independent')
        for sid,row in registry.state['ports'].items():
            service.strategy_client(sid, routes={key:InstrumentId.from_str(i) for key,i in row['routes'].items()},
                budget=SharedRiskBudget(**row['budget']),
                instrument_limits={InstrumentId.from_str(i):limits_from_wire(r) for i,r in row['limits'].items()})
        service.event_journal = ExecutionEventJournal(service._ports)
        account = CtpSharedAccountLifecycle(execution, service, transport=transport,
            state_file=config['account_state_file'], identity=identity, resources=resources,
            expected_day=config['trading_day'], position_scope=scope, preserve_external=True, rollover_transform=rollover)
        account.manager.state_components['execution_registry'] = registry
        return SimpleNamespace(account=account, service=service, resources=resources,
            configure=lambda sid,configuration,specs:registry.configure(account,sid,configuration,specs),
            snapshot=lambda:dict(execution_diagnostics=dict(account=service.snapshot_state())))
    except BaseException:
        if driver is not None:
            driver.stop()
        resources.close()
        raise


def capture_execution_failure(host):
    """复用宿主诊断，在停机操作改变账户状态前保存首个故障。"""
    if host is None:
        return None
    try:
        return host.snapshot()["execution_diagnostics"]
    except Exception as error:
        return {"diagnostic_error": str(error)}


def serve_account(args, config, stop_event):
    from bomber.framework.trader.runtime.live.continuous import StopRequested, wait_for_open
    from bomber.framework.trader.execution.ipc import AccountExecutionProtocol, UnixExecutionServer, encode
    from bomber.framework.trader.runtime.reports import LiveRunReport
    load_environment(getattr(args, 'env_file', None) or config.get('account_env_file'))
    report = LiveRunReport(config["report_dir"], prefix="shared-account-server", metadata={"config": config})
    report.begin()
    host = server = None
    diagnostics, failure, shutdown_failure, status = {}, None, None, "failed"
    failure_execution_diagnostics = None
    stop_reason = None
    execution_stop_started = None
    try:
        if args.socket.exists():
            raise RuntimeError("执行socket已存在；禁止接管其他账户服务")
        host = build_host(config, resume=args.resume, diagnostics=diagnostics,
            session_order_budget=args.remaining_orders)
        keys = {sid: secrets.token_hex(32) for sid in host.service._ports}
        protocol = AccountExecutionProtocol(host.service, keys=keys, trading_day=config["trading_day"],
            configure=host.configure)
        host.account.start(resume=args.resume)
        if any(row["status"] != "completed" for row in protocol.journal.requests.values()):
            raise RuntimeError("恢复存在未决跨进程请求，须权威核查，拒绝自动重复发单")
        host.service.start()
        def publish_server():
            nonlocal server
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
                            "reference": None,
                            "check_reference": False,
                            "allow_reconfigure": True}))
                        output.flush(); os.fsync(output.fileno())
                        os.replace(temporary, path)
                    finally:
                        temporary.unlink(missing_ok=True)
            discovery_file = getattr(args, 'discovery_file', None)
            if discovery_file is not None:
                import os
                from tempfile import NamedTemporaryFile
                window = args.discovery_window
                discovery_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with NamedTemporaryFile(dir=discovery_file.parent, delete=False) as output:
                    temporary = Path(output.name)
                    try:
                        os.fchmod(output.fileno(), 0o600)
                        output.write(encode(dict(config=str(args.discovery_config),
                            credentials_dir=str(args.credentials_dir), socket=str(args.socket),
                            trading_day=config['trading_day'], start=window.start.isoformat(), end=window.end.isoformat())))
                        output.flush(); os.fsync(output.fileno())
                        os.replace(temporary, discovery_file)
                    finally:
                        temporary.unlink(missing_ok=True)


        def poll_preparation():
            if server is not None:
                server.poll()
                protocol.poll()
            host.account.poll()

        activate_at = getattr(args, "activate_at", None)
        if activate_at is not None:
            # 让worker提前连接并预热，账户仍未授权；已停止的行情恢复沿用原无租约校验。
            if host.service.execution_stop is None:
                publish_server()
            wait_for_open(activate_at, stop_event, poll=poll_preparation)
        if stop_event.is_set():
            raise StopRequested()
        host.account.authorize()
        if server is None:
            publish_server()

        print(f"账户服务就绪：socket={args.socket}；策略客户端文件目录={args.credentials_dir}", flush=True)
        until = time.monotonic() + config["seconds"] + 120  # 给分终端启动和策略先停机留出时间。
        stop_at = getattr(args, 'stop_at', None)
        while (not stop_event.is_set() and (stop_at is None or datetime.now(stop_at.tzinfo) < stop_at)
                and (time.monotonic() < until or execution_stop_started is not None)):
            server.poll()
            protocol.poll()
            host.account.poll()
            if host.service.execution_stop is not None:
                stop_reason = host.service.execution_stop["reason"]
                execution_stop_started = execution_stop_started or time.monotonic()
                journal = protocol.journal
                acknowledged = all(journal.ack[sid] == len(rows) for sid, rows in journal.events.items())
                stopped = protocol.closed_tokens.keys() <= protocol.stopped_acknowledged
                if not protocol.leases and not host.service._active_instruments() and acknowledged and stopped:
                    break
                if time.monotonic() - execution_stop_started > 30:
                    raise RuntimeError(f"账户停止后回报/策略停止未在期限内确认: {stop_reason}，保留检查点")
            time.sleep(0.02)
        status = "completed"
    except StopRequested:
        status = "completed"
    except BaseException as error:
        failure = str(error) or type(error).__name__
        failure_execution_diagnostics = capture_execution_failure(host)
        raise
    finally:
        cleanup_errors = []
        try:
            if host:
                if server:
                    for sid in tuple(protocol.leases):
                        try:
                            protocol.stop_owner(sid)
                        except Exception as error:
                            cleanup_errors.append(str(error))
                try:
                    host.account.stop()
                    host.service.stop()
                except Exception as error:
                    cleanup_errors.append(str(error))
                if not cleanup_errors:
                    host.resources.close()
        finally:
            if server:
                server.close()
            if cleanup_errors:
                shutdown_failure, status = "; ".join(cleanup_errors), "failed"
            report.write(status=status, fields={"failure": failure, "shutdown_failure": shutdown_failure,
                "stop_reason": stop_reason,
                "execution_stop": host.service.execution_stop if host else None,
                "failure_execution_diagnostics": failure_execution_diagnostics,
                "td_preflight": diagnostics, "account": host.service.snapshot_state() if host else None,
                "final_gross": host.account.final_gross if host else None,
                "final_active_orders": host.account.final_active_orders if host else None,
                "verification": "independent_process_account_service"},
                session=host, events=host.account.execution.backend.reports if host else ())
        if cleanup_errors and failure is None:
            raise RuntimeError(shutdown_failure)

