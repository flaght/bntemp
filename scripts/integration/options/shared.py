"""08正常启动：自动观察TD日、恢复归属、共享执行，运行至收盘或主动停止。"""
from argparse import ArgumentParser
from datetime import datetime
from importlib import import_module
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[3]


def session_config(identity, *, day, account_state, signal_state, report_dir, max_orders=20, shared_identity=None):
    """原策略/风险参数保持；日期不进入argv，下一交易日由TD登录确定。"""
    if shared_identity is not None:
        from copy import deepcopy
        sid = identity["strategy_id"]
        declarations = shared_identity["strategies"]
        if set(declarations) != {sid} or declarations[sid]["kind"] != "option_vega":
            raise ValueError("共享恢复须保留原单个08策略声明")
        account = deepcopy(shared_identity["account"])
        if account["client_id"] != sid or account["max_session_orders"] != max_orders:
            raise ValueError("共享恢复须保留原账户ID及订单上限")
        return {"version": 1, "trading_day": day, "environment": "realtime", "execution_mode": "independent",
            "position_scope": "strategy_instruments", "existing_position_policy": "preserve_external", "seconds": 86400,
            "account_state_file": str(account_state), "report_dir": str(report_dir), "account": account,
            "strategies": [{"strategy_id": sid, "kind": "option_vega", "state_file": str(signal_state),
                "argv": deepcopy(declarations[sid]["argv"])}]}
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
        "position_scope": "strategy_instruments", "existing_position_policy": "preserve_external", "seconds": 86400,
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
        flow_path="/tmp/bomber-vega-observe-td")
    with ExitStack() as resources:
        CtpSharedAccountLifecycle.acquire(resources, transport, environment="simnow")
        driver = CtpNativeTraderDriver(transport.client_id, transport.account_id, transport)
        try:
            driver.start(lambda event: None)
            assert_account_ready(driver)
            return driver.trading_day
        finally:
            driver.stop()


def configure_index_polling(host, seconds):
    """独立的运行频率覆盖，不修改恢复身份/策略argv/数据可见性。"""
    if seconds is None:
        return []
    records = []
    for sid, item in host.strategies.items():
        for feed in item.session.runner.feature_query_feeds:
            cache = feed.cache
            before = cache.poll_ns / 1e9
            cache.set_poll_seconds(seconds)
            row = {"strategy_id": sid, "dataset_id": cache.dataset_id, "key": feed.key,
                "saved_poll_seconds": before, "effective_poll_seconds": cache.poll_ns / 1e9,
                "scope": "process_runtime_only"}
            records.append(row)
            print("指数轮询运行配置:", json.dumps(row, ensure_ascii=False), flush=True)
    if not records:
        raise ValueError("此运行频率覆盖需要已装配的指数查询Feed")
    return records


def run_session(config, *, resume, window, stop_event, clock=None, session_order_budget=None,
                calendar_provenance=None, index_poll_seconds=None):
    from scripts.integration.shared_account.run import build_host
    from bomber.framework.trader.runtime.reports import LiveRunReport
    from bomber.framework.trader.runtime.live.continuous import StopRequested
    clock = clock or (lambda: datetime.now(window.end.tzinfo))
    report = LiveRunReport(config["report_dir"], prefix="option-vega-shared", metadata={
        "config": config, "calendar": calendar_provenance,
        "window": {"trading_day": window.trading_day, "segment": window.segment_id,
                   "start": window.start.isoformat(), "end": window.end.isoformat()}})
    report.begin()
    host, failure, cleanup_error, status = None, None, None, "failed"
    polling = []
    def check_window():
        if stop_event.is_set() or clock() >= window.end:
            raise StopRequested("主动停止或交易窗口已结束")
    try:
        check_window()
        host = build_host(config, resume=resume, session_order_budget=session_order_budget)
        polling = configure_index_polling(host, index_poll_seconds)
        host.ready_seconds = min(180, max(0.2, (window.end-clock()).total_seconds()))
        host.preparation_check = check_window
        host.start()
        print("08共享会话就绪；按日历窗口管理归属仓位，停止保留成交仓位", flush=True)
        until = time.monotonic() + config["seconds"]
        while time.monotonic() < until:
            check_window()
            host.poll()
            if not any(item.active for item in host.strategies.values()):
                raise RuntimeError("08策略已因异常停止，查看逐策略failure")
            stop_event.wait(0.2)
        status = "completed"
    except StopRequested:
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
            snapshot = host.snapshot() if host else {}
            option_reports = {sid: import_module("demos.08_option_vega.run_live").describe(item.session)
                              for sid, item in host.strategies.items()} if host else {}
            report.write(status=status, fields={**snapshot, "failure": failure, "cleanup_error": cleanup_error,
                "stopped_by_operator": stop_event.is_set(), "runtime_index_polling": polling,
                "rollover": getattr(host.account, "rollover_audit", None) if host else None,
                "final_gross": host.account.final_gross if host else None,
                "final_active_orders": host.account.final_active_orders if host else None,
                "option_reports": option_reports,
                "option_report": next(iter(option_reports.values())) if len(option_reports) == 1 else None}, session=host,
                events=host.account.execution.backend.reports if host else ())
    return {"status": status, "failure": failure, "cleanup_error": cleanup_error,
            "trading_day": window.trading_day, "segment": window.segment_id}


def load_identity(state_file):
    from bomber.framework.trader.persistence import JsonStateStore
    saved = JsonStateStore(state_file).load()
    if saved is None:
        raise ValueError("须提供原08归属检查点；不能用空文件绕过原仓")
    identity = saved.payload["component_states"]["session_identity"]["identity"]
    if "strategies" in identity:
        if len(identity["strategies"]) != 1:
            raise ValueError("此快捷入口只管理单个08；多策略使用共享run/server/worker")
        identity = next(iter(identity["strategies"].values()))["reference"]
    if identity.get("strategy_id") != "option-vega-live":
        raise ValueError("原归属检查点须为option-vega-live")
    return saved, identity


def load_calendar(args, identity):
    from dataclasses import replace
    from bomber.framework.dataprep.session_inputs import session_inputs_from_args, load_session_inputs
    if args.trading_calendar is not None:
        raise ValueError("持续管理使用公共Provider；不接受旧日历JSON")
    if args.session_timezone != "Asia/Shanghai":
        raise ValueError("08实时日盘使用Asia/Shanghai，不能改变估值日期口径")
    original_id = identity["calendar_manifest"]["calendar_id"]
    args.calendar_id = args.calendar_id or original_id
    if args.calendar_id != original_id:
        raise ValueError("不能改变原策略交易日历身份")
    calendar = load_session_inputs(replace(session_inputs_from_args(args, product="MO", exchange="CFFEX"),
                                          asset_type="OPTION"))
    if any(s.date_anchor != "TRADING_DAY" or s.start_day_offset or s.end_day_offset
           for s in calendar.segments) or any(s.date_anchor != "TRADING_DAY" or s.start_day_offset or s.end_day_offset
                for override in calendar.overrides for s in override.segments):
        raise ValueError("08当前只支持CFFEX当日日盘，不能借用夜盘时段")
    return calendar


class OptionWindowRuntime:
    def __init__(self, args, calendar, window, stop_event):
        self.args, self.calendar, self.window, self.stop = args, calendar, window, stop_event

    def run(self):
        from bomber.framework.trader.runtime.live.continuous import orders_for_day
        # 每次读取最新一代，午休和跨日均不复用首次装配时的旧身份或目标。
        saved, identity = load_identity(self.args.state_file)
        if self.stop.is_set():
            return {"status": "completed"}
        day = observe_day()
        if day != self.window.trading_day:
            raise RuntimeError(f"当前TD日{day}与calendars窗口{self.window.trading_day}不一致")
        remaining = self.args.max_session_orders - orders_for_day(saved.payload, day)
        if remaining <= 0:
            raise RuntimeError("本交易日报单额度已用完；停止并保存，不重启刷新额度")
        saved_identity = saved.payload["component_states"]["session_identity"]["identity"]
        config = session_config(identity, day=day, account_state=self.args.state_file,
            signal_state=self.args.signal_state, report_dir=self.args.report_dir,
            max_orders=self.args.max_session_orders,
            shared_identity=saved_identity if "strategies" in saved_identity else None)
        return run_session(config, resume=True, window=self.window, stop_event=self.stop,
            session_order_budget=remaining, calendar_provenance=self.calendar.provenance,
            index_poll_seconds=getattr(self.args, "index_poll_seconds", None))


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True, help="原08或已续接共享账户的同一检查点")
    parser.add_argument("--signal-state", type=Path, help="默认在账户文件旁保存共享信号检查点")
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--confirm-simnow", action="store_true")
    parser.add_argument("--max-session-orders", type=int, default=20)
    parser.add_argument("--report-dir", type=Path, default=ROOT/"tests/results/gap08-shared")
    parser.add_argument("--run-forever", action="store_true", help="休市等待，下一交易日自动恢复；默认当天收盘退出")
    from bomber.framework.dataprep.session_inputs import add_session_arguments
    add_session_arguments(parser)
    parser.add_argument("--index-poll-seconds", type=float, default=None,
        help="本进程指数查询间隔(0,30]秒；不改原检查点身份，省略沿用保存值")
    args = parser.parse_args(argv)
    import math
    if args.index_poll_seconds is not None and (not math.isfinite(args.index_poll_seconds) or not 0 < args.index_poll_seconds <= 30):
        parser.error("index-poll-seconds须在(0,30]秒")
    if not args.connect:
        if args.enable_orders or args.confirm_simnow:
            parser.error("离线检查不接受报单授权")
        print("08共享启动入口就绪；未连接、未修改状态。使用--connect及双授权启动。")
        return
    if not (args.enable_orders and args.confirm_simnow) or args.max_session_orders < 1:
        parser.error("连接须双报单授权及正订单上限")
    from dotenv import load_dotenv
    load_dotenv(ROOT/".env")
    from bomber.framework.trader.runtime.live.continuous import ContinuousPositionRuntime, StopSignal
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    args.state_file = args.state_file.resolve()
    args.signal_state = (args.signal_state or args.state_file.with_name(
        args.state_file.name+".vega-signal.json")).resolve()
    args.report_dir = args.report_dir.resolve()
    if args.signal_state == args.state_file:
        parser.error("信号文件与账户文件须不同")
    # 常驻所有权锁与每个窗口实际账户/状态锁分开，禁止两个调度器交替接管同一文件。
    with strategy_state_lock(str(args.state_file)+".continuous-owner"), StopSignal() as stop:
        _, identity = load_identity(args.state_file)
        calendar = load_calendar(args, identity)
        print("会话日历:", json.dumps(calendar.provenance, ensure_ascii=False), flush=True)
        runtime = ContinuousPositionRuntime(calendar,
            lambda window, event: OptionWindowRuntime(args, calendar, window, event),
            run_forever=args.run_forever, stop_event=stop)
        print("持续管理结束:", json.dumps(runtime.run(), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
