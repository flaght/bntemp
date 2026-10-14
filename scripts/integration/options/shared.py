"""08正常启动：自动观察TD日、恢复归属、共享执行，运行至收盘或主动停止。"""
from argparse import ArgumentParser
from datetime import datetime, time as day_time
from importlib import import_module
import json
from pathlib import Path
import signal
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]


def session_config(identity, *, day, account_state, signal_state, report_dir, max_orders=20):
    """原策略/风险参数保持；日期不进入argv，下一交易日由TD登录确定。"""
    from .simnow_migrate import runtime_argv
    from scripts.integration.shared_account.config import PROTECTED
    argv = runtime_argv(identity, day, signal_state, "stream_cffex_1min", "end", "cumulative", report_dir)
    filtered, index = [], 0
    switches = {"--connect", "--enable-orders", "--confirm-simnow", "--resume"}
    while index < len(argv):
        flag = argv[index]
        if flag in PROTECTED or flag in {"--valuation-day", "--max-session-orders", "--report-dir"}:
            index += 1 if flag in switches else 2
        else:
            filtered.extend(argv[index:index+2]); index += 2
    if identity["sources"]["market"]["channel"] == "dolphindb":
        market = identity["sources"]["market"]
        for key, flag in (("table", "--market-bar-table"), ("timestamp_label", "--market-bar-timestamp"),
                          ("volume_mode", "--market-volume-mode")):
            if key in market:
                filtered[filtered.index(flag)+1] = str(market[key])
    config = identity["config"]
    quantity = config["max_total_option_lots"] + config["max_future_lots"] * 4
    return {"version": 1, "trading_day": day, "environment": "realtime", "execution_mode": "independent",
        "position_scope": "strategy_instruments", "seconds": 86400,
        "account_state_file": str(account_state), "report_dir": str(report_dir),
        "account": {"client_id": identity["strategy_id"], "max_gross_quantity": quantity,
            "max_gross_notional": identity["max_notional"], "max_session_orders": max_orders,
            "limit_offset_ticks": identity["limit_offset_ticks"]},
        "strategies": [{"strategy_id": identity["strategy_id"], "kind": "option_vega",
            "state_file": str(signal_state), "argv": filtered}]}


def observe_day():
    """只读登录，正式build_host随后再次核对；不从自然日推测柜台日。"""
    from contextlib import ExitStack
    from bomber.framework.trader.execution.ctp import CtpNativeTraderDriver, CtpTdApiTransport
    from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport, assert_account_ready
    from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle
    transport = build_simnow_transport(CtpTdApiTransport, client_id="option-vega-observe",
        flow_path="/tmp/bomber-vega-observe-td", timeout_seconds=30)
    with ExitStack() as resources:
        CtpSharedAccountLifecycle.acquire(resources, transport, environment="simnow")
        driver = CtpNativeTraderDriver(transport.client_id, transport.account_id, transport)
        try:
            driver.start(lambda event: None)
            assert_account_ready(driver)
            return driver.trading_day
        finally:
            driver.stop()


def run_session(config, *, resume):
    from scripts.integration.shared_account.run import build_host
    from bomber.framework.trader.runtime.reports import LiveRunReport
    from bomber.framework.trader.runtime.live.readiness import wait_for_session_ready
    report = LiveRunReport(config["report_dir"], prefix="option-vega-shared", metadata={"config": config})
    report.begin()
    host, failure, cleanup_error, status = None, None, None, "failed"
    requested = [False]
    def stop_requested(*args):
        requested[0] = True
    previous = {sig: signal.signal(sig, stop_requested) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        host = build_host(config, resume=resume)
        host.ready_seconds = 180
        host.start()
        print("08共享会话就绪；持仓已恢复，运行至15:00或Ctrl+C；停止保留持仓", flush=True)
        until = time.monotonic() + config["seconds"]
        paused = False
        while not requested[0] and time.monotonic() < until:
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            if now.strftime("%Y%m%d") != config["trading_day"] or now.time() >= day_time(15):
                break
            if day_time(11, 30, 40) <= now.time() < day_time(13):
                # 日盘午休停止决策，账户查询/撤单回报仍推进；不沿用午前报价。
                host.account.poll(); host.service.poll()
                for item in host.strategies.values():
                    item.session.client.drain_events()
                paused = True
            else:
                if paused:
                    for item in host.strategies.values():
                        item.session.runner.accept_bars = False
                        wait_for_session_ready(item.session, 180, lost=lambda: requested[0] or None)
                        item.session.strategy.restored = True
                        item.session.runner.begin_bars()
                    paused = False
                host.poll()
                if not any(item.active for item in host.strategies.values()):
                    raise RuntimeError("08策略已因异常停止，查看逐策略failure")
            time.sleep(0.2)
        status = "completed"
    except BaseException as error:
        failure = str(error) or type(error).__name__
        raise
    finally:
        try:
            if host is not None:
                host.stop()
        except Exception as error:
            cleanup_error, status = str(error), "failed"
            raise
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            snapshot = host.snapshot() if host else {}
            report.write(status=status, fields={**snapshot, "failure": failure, "cleanup_error": cleanup_error,
                "rollover": getattr(host.account, "rollover_audit", None) if host else None,
                "final_gross": host.account.final_gross if host else None,
                "final_active_orders": host.account.final_active_orders if host else None,
                "option_report": import_module("demos.08_option_vega.run_live").describe(
                    next(iter(host.strategies.values())).session) if host else None}, session=host,
                events=host.account.execution.backend.reports if host else ())


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True, help="原08或已续接共享账户的同一检查点")
    parser.add_argument("--signal-state", type=Path, help="默认在账户文件旁保存共享信号检查点")
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--confirm-simnow", action="store_true")
    parser.add_argument("--max-session-orders", type=int, default=20)
    parser.add_argument("--report-dir", type=Path, default=ROOT/"tests/results/gap08-shared")
    args = parser.parse_args(argv)
    if not args.connect:
        if args.enable_orders or args.confirm_simnow:
            parser.error("离线检查不接受报单授权")
        print("08共享启动入口就绪；未连接、未修改状态。使用--connect及双授权启动。")
        return
    if not (args.enable_orders and args.confirm_simnow) or args.max_session_orders < 1:
        parser.error("连接须双报单授权及正订单上限")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if not day_time(9, 29) <= now.time() < day_time(14, 56):
        parser.error("08日盘启动时间须在09:29至14:56之间，为当前分钟就绪留出时间")
    from dotenv import load_dotenv
    load_dotenv(ROOT/".env")
    from bomber.framework.trader.persistence import JsonStateStore
    state_file = args.state_file.resolve()
    saved = JsonStateStore(state_file).load()
    if saved is None:
        parser.error("首次使用此恢复入口须提供原08归属检查点；空仓新会话使用共享配置入口")
    identity = saved.payload["component_states"]["session_identity"]["identity"]
    if "strategies" in identity:
        if len(identity["strategies"]) != 1:
            parser.error("此快捷入口只管理单个08；多策略使用共享run/server/worker")
        identity = next(iter(identity["strategies"].values()))["reference"]
    if identity.get("strategy_id") != "option-vega-live":
        parser.error("原归属检查点须为option-vega-live")
    day = observe_day()
    if day != now.strftime("%Y%m%d"):
        parser.error("当前TD日不是本地日盘日期，不能运行实时08行情")
    signal_state = (args.signal_state or state_file.with_name(state_file.name+".vega-signal.json")).resolve()
    if signal_state == state_file:
        parser.error("信号文件与账户文件须不同")
    config = session_config(identity, day=day, account_state=state_file, signal_state=signal_state,
        report_dir=args.report_dir.resolve(), max_orders=args.max_session_orders)
    run_session(config, resume=True)


if __name__ == "__main__":
    main()
