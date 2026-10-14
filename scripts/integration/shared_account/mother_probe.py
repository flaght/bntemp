"""独立双来源限价母单撤单验收；默认只读预检，显式授权才提交一张2手单。"""

import argparse
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLIENT_ID = "shared-mother-probe"


def decimal_arg(value):
    try:
        return Decimal(value)
    except (ArithmeticError, ValueError):
        raise argparse.ArgumentTypeError("请输入有效数字") from None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--trading-day", required=True)
    parser.add_argument("--product", choices=("CU", "AU"), default="CU")
    parser.add_argument("--instrument", help="提交时须明确指定预检输出的合约，例如cu2612.SHFE")
    parser.add_argument("--side", choices=("BUY", "SELL"), default="BUY")
    parser.add_argument("--limit-price", type=decimal_arg, help="提交时须明确指定预检输出的限价")
    parser.add_argument("--passive-ticks", type=int, default=10)
    parser.add_argument("--max-notional", type=decimal_arg, default=Decimal("1200000"))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--confirm-simnow", action="store_true")
    parser.add_argument("--report-dir", type=Path, default=Path("scripts/integration/shared_account/results"))
    parser.add_argument("--protect-state", nargs="+", type=Path, default=[
        Path("scripts/integration/shared_account/results/shared-account-cu-au.json"),
        Path("scripts/integration/shared_account/results/shared-account.json")])
    args = parser.parse_args(argv)
    try:
        args.day = datetime.strptime(args.trading_day, "%Y%m%d").date()
        if args.day.strftime("%Y%m%d") != args.trading_day:
            raise ValueError()
    except ValueError:
        parser.error("交易日须为有效YYYYMMDD")
    if args.submit != args.confirm_simnow or args.submit and (
            not args.connect or not args.instrument or args.limit_price is None):
        parser.error("提交须connect、submit、confirm-simnow并明确instrument和limit-price")
    if not 1 <= args.timeout <= 60 or not 1 <= args.passive_ticks <= 100:
        parser.error("timeout须1至60秒，passive-ticks须1至100")
    if not args.max_notional.is_finite() or not 0 < args.max_notional <= Decimal("2300000"):
        parser.error("测试总名义限额须为正且不超过230万")
    if args.limit_price is not None and (not args.limit_price.is_finite() or args.limit_price <= 0):
        parser.error("限价须为有限正数")
    if args.instrument and re.fullmatch(args.product.lower() + r"\d{4}\.SHFE", args.instrument) is None:
        parser.error("合约须与品种匹配并使用CTP小写代码及.SHFE后缀")
    return args


def protected_state(paths):
    instruments, hashes = set(), {}
    for path in paths:
        path = path.resolve()
        data = path.read_bytes()
        state = json.loads(data)["payload"]["component_states"]["shared_account"]
        known = state["known_instruments"]
        if not isinstance(known, list) or not all(isinstance(i, str) for i in known):
            raise ValueError("原共享检查点的合约范围无效")
        instruments.update(known)
        hashes[str(path)] = hashlib.sha256(data).hexdigest()
    return instruments, hashes


def select_spec(specs, args, gross, protected):
    eligible = []
    for spec in specs:
        instrument = f"{spec.symbol.lower()}.{spec.venue}"
        if (spec.product == args.product and spec.venue == "SHFE" and spec.currency == "CNY"
                and spec.listed <= args.day and (spec.last_trade - args.day).days >= 7
                and instrument not in protected and not any(gross.get(instrument, (0, 0)))):
            eligible.append(spec)
    eligible.sort(key=lambda spec: (spec.last_trade, spec.symbol))
    args.eligible_instruments = [f"{spec.symbol.lower()}.{spec.venue}" for spec in eligible]
    if args.instrument:
        eligible = [spec for spec in eligible if f"{spec.symbol.lower()}.{spec.venue}" == args.instrument]
    if not eligible:
        raise RuntimeError("没有未持仓且不属于原策略范围的合约；禁止接管旧归属")
    return eligible[0]


def price_plan(raw, observation, args, spec):
    if (observation.trading_day != args.trading_day or observation.timestamp_regressed
            or not 0 <= time.monotonic_ns() - observation.received_monotonic_ns <= 3_000_000_000
            or not 0 <= time.time_ns() - observation.ts_event <= 10_000_000_000):
        raise RuntimeError("测试行情交易日、时间或接收时效不符")
    try:
        values = {name: Decimal(str(raw.get(name))) for name in
                  ("BidPrice1", "AskPrice1", "LastPrice", "LowerLimitPrice", "UpperLimitPrice")}
    except (ArithmeticError, ValueError):
        raise RuntimeError("测试行情价格字段缺失或无效") from None
    if any(not value.is_finite() or not 0 < value < Decimal("1e12") for value in values.values()):
        raise RuntimeError("测试行情缺少有效盘口或涨跌停价格")
    bid, ask, low, high = (values[k] for k in ("BidPrice1", "AskPrice1", "LowerLimitPrice", "UpperLimitPrice"))
    if not low <= bid <= ask <= high or not low <= values["LastPrice"] <= high:
        raise RuntimeError("测试盘口与涨跌停边界不一致")
    suggested = bid - spec.tick * args.passive_ticks if args.side == "BUY" else ask + spec.tick * args.passive_ticks
    price = args.limit_price if args.limit_price is not None else suggested
    if (not low <= price <= high or price % spec.tick != 0
            or args.side == "BUY" and price >= bid or args.side == "SELL" and price <= ask):
        raise RuntimeError("限价须在涨跌停范围及价位网格内，买价低于买一/卖价高于卖一")
    notional = max(price, values["LastPrice"]) * spec.multiplier * 2
    if notional > args.max_notional:
        raise RuntimeError(f"测试2手总名义金额{notional}超过限额{args.max_notional}")
    return {"instrument": f"{spec.symbol.lower()}.{spec.venue}", "side": args.side,
        "quantity": 2, "per_source_quantity": 1, "limit_price": str(price),
        "tick": str(spec.tick), "multiplier": str(spec.multiplier), "notional": str(notional),
        "quote": {k: str(v) for k, v in values.items()}, "quote_ts_event": observation.ts_event,
        "observed_md_trading_day": observation.trading_day,
        "unexpected_fill_policy": "最多2手意外成交归独立测试账本；保留持仓，不自动平仓或改原检查点"}


class ProbePlanner:
    """只在本验收装配中使用；普通CTP规划完成后限定唯一OPEN2母单及明确限价。"""
    def __init__(self, planner, price, validate):
        self.planner, self.price, self.validate = planner, price, validate
        self.deferred = True

    def plan(self, request):
        if self.deferred:
            return ()
        self.validate()
        orders = tuple(self.planner.plan(request))
        if len(orders) != 1 or orders[0].quantity != 2 or orders[0].position_effect.value != "OPEN":
            raise RuntimeError("受控验收只能提交唯一2手开仓母单")
        return (replace(orders[0], price=self.price),)


def exercise(host, instrument, price, validate, *, timeout):
    from bomber.framework.trader.contracts import ExecutionRequest
    from .cancel_probe import SharedMotherCancelProbe
    items = list(host.strategies.values())
    planner = ProbePlanner(host.account.client.planner, price, validate)
    host.account.client.planner = planner
    sign = 1 if host.probe_side == "BUY" else -1
    contribution_window_ns = int((timeout + host.account.cancel_seconds + 5) * 1_000_000_000)
    for item in items:
        port = item.session.client
        key = next(iter(port.routes))
        now = host.service.clock_ns()
        port.submit_targets(ExecutionRequest(item.strategy_id, 1, port.client_id, now,
            {instrument: Decimal(sign)}, "DIRECT", deadline_ns=now + contribution_window_ns,
            metadata={"signal_source": "controlled_shared_mother_cancel_acceptance",
                "position_attribution": ({"strategy_id": item.strategy_id, "target_key": key,
                    "instrument_id": str(instrument), "quantity": str(sign)},)}))
    planner.deferred = False
    host.service.continue_targets(items[1].strategy_id, instrument, host.service.clock_ns())
    probe = SharedMotherCancelProbe(items[0].strategy_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        host.service.poll()
        for item in items:
            item.session.client.drain_events()
        probe.poll(host)
        if probe.mother is not None:
            break
        if any(row.report_type.value in {"FILLED", "REJECTED"}
               for row in host.account.execution.backend.reports):
            break
        time.sleep(0.02)
    for item in items:
        item.session.client.drain_events()
    probe.controlled_peer_state = {
        item.strategy_id: {"active": item.active,
            "source": host.service.snapshot_state()["sources"].get(item.strategy_id)}
        for item in items[1:]}
    return probe


def read_specs(args):
    from bomber.framework.dataprep.sources import DolphinDbReferenceConfig, ReferenceQuery, ReferenceSourceFactory, DataSourcePurpose
    from bomber.framework.dataprep.metadata import future_spec_from_basic, parse_future_terms
    config = DolphinDbReferenceConfig.from_env(read_timeout_seconds=15)
    with ReferenceSourceFactory.create_for(DataSourcePurpose.LIVE_REFERENCE, "dolphindb", config) as source:
        query = ReferenceQuery(products=(args.product,), active_on=args.day)
        first, second = source.futures_basic(query), source.futures_basic(query)
        if first.fingerprint != second.fingerprint or not second.rows:
            raise RuntimeError("读取期间期货条款改变或为空，禁止测试")
        specs = {}
        for row in second.rows:
            if row.get("available_ns") is not None:
                published = Decimal(str(row["available_ns"]))
                if (not published.is_finite() or published != published.to_integral_value()
                        or not 0 <= published <= time.time_ns()):
                    raise RuntimeError("测试条款尚未发布或发布时间无效")
            spec = future_spec_from_basic(parse_future_terms(row))
            if spec.symbol in specs and specs[spec.symbol] != spec:
                raise RuntimeError("同一测试合约存在冲突条款版本")
            specs[spec.symbol] = spec
        args.reference_evidence = {"source": second.source, "fingerprint": second.fingerprint,
                                   "observed_at_ns": time.time_ns()}
        return tuple(specs.values())


def make_feed(transport, spec, resources, report_dir):
    from bomber.framework.market.basic.base import InstrumentId, InstrumentMeta, DataType
    from bomber.framework.market.stream.ctp.feed import CtpLiveDataFeed, CtpMdConfig
    from bomber.framework.trader.runtime.live.channels.ctp import build_md_config, required
    class ProbeFeed(CtpLiveDataFeed):
        def __init__(self, config):
            super().__init__(config)
            self.raw_quote = None
        def on_depth_market_data(self, raw):
            with self._lock:
                previous = self._depth_observations.get(instrument)
            super().on_depth_market_data(raw)
            with self._lock:
                observation = self._depth_observations.get(instrument)
                if (observation is not None and (previous is None
                        or observation.received_monotonic_ns > previous.received_monotonic_ns)):
                    self.raw_quote = (dict(raw), observation)
        def on_api_error(self, error):
            with self._lock:
                self.raw_quote = None
            super().on_api_error(error)
        def quote(self):
            with self._lock:
                if (self.raw_quote is None or not self._ready.is_set()
                        or self._depth_observations.get(instrument) != self.raw_quote[1]):
                    raise RuntimeError("未取得有效测试行情")
                return self.raw_quote
    instrument = InstrumentId.from_str(f"{spec.symbol.lower()}.{spec.venue}")
    lifecycle = SimpleNamespace(transport=transport, md_front=required("CTP_MD_ADDRESS"))
    feed = ProbeFeed(build_md_config(CtpMdConfig, lifecycle, flow_path=str(report_dir / "md-flow")))
    feed.register_instrument(InstrumentMeta(instrument,
        price_precision=max(0, -spec.tick.normalize().as_tuple().exponent),
        price_increment=spec.tick, multiplier=spec.multiplier, currency=spec.currency, exchange=spec.venue))
    feed.subscribe(instrument, DataType.QUOTE_TICK)
    resources.callback(feed.disconnect)
    feed.connect()
    return instrument, feed


def build_host(args, driver, transport, spec, instrument, validate, resources, state_file):
    from bomber.framework.trader.execution.builders import build_ctp_portfolio_execution
    from bomber.framework.trader.execution.shared import SharedAccountExecutionService, SharedRiskBudget
    from bomber.framework.trader.execution.risk import RiskLimits
    from bomber.framework.trader.runtime.live.recovery import InstrumentPositionScope
    from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle
    from bomber.framework.trader.runtime.live.shared import SharedAccountLiveHost, SharedStrategySession
    from bomber.framework.trader.runner import UnifiedStrategyRunner
    from bomber.framework.trader.template import StrategyTemplate
    from bomber.framework.trader.contracts import ExecutionRoute
    scope = InstrumentPositionScope((instrument,))
    limits = {instrument: RiskLimits(max_order_quantity=2, max_abs_position=2,
        max_order_notional=args.max_notional, max_abs_position_notional=args.max_notional,
        max_market_age_ns=10_000_000_000, contract_multiplier=spec.multiplier)}
    execution = build_ctp_portfolio_execution(driver, trading_day=args.trading_day,
        price_increments={instrument: spec.tick}, multipliers={instrument: spec.multiplier},
        instrument_limits=limits, session_check=lambda: driver.is_simnow_session, orders=True,
        instrument_scope=scope, limit_offset_ticks=0)
    service = SharedAccountExecutionService(execution.client, prices=execution.prices,
        instrument_limits=limits, budget=SharedRiskBudget(2, args.max_notional), instrument_scope=scope)
    class Observer(StrategyTemplate):
        def __init__(self, sid):
            super().__init__(sid)
            self.fills_received = 0
        def on_fill(self, event):
            self.fills_received += 1
    def ready():
        try:
            validate()
            return execution.client.is_reconciled
        except Exception:
            return False
    items = []
    for sid in ("mother-probe-one", "mother-probe-two"):
        port = service.strategy_client(sid, routes={"main": instrument},
            budget=SharedRiskBudget(1, args.max_notional / 2))
        port.ready_check = ready
        runner = UnifiedStrategyRunner("live", position_manager=execution.positions)
        runner.add_execution_client(port)
        strategy = Observer(sid)
        runner.add_strategy(strategy, data_bindings=(), execution_routes=(ExecutionRoute("main", service.client_id, instrument),))
        runner.session_ready, runner.begin_bars = ready, lambda: None
        runner.fixed_spec, runner.failure, runner.accept_bars = spec, None, False
        session = SimpleNamespace(runner=runner, strategy=strategy, client=port,
                                  references=SimpleNamespace(spec=spec, refresh=lambda: None))
        items.append(SharedStrategySession(session, state_file.parent / f"{sid}.json", {"id": sid, "instrument": str(instrument)}))
    account = CtpSharedAccountLifecycle(execution, service, transport=transport, state_file=state_file,
        identity={"probe": "shared-mother-cancel-v1", "day": args.trading_day,
                  "instrument": str(instrument), "side": args.side, "limit_price": str(args.limit_price)},
        resources=resources, expected_day=args.trading_day, position_scope=scope)
    host = SharedAccountLiveHost(account, service, items, resources=resources)
    host.probe_side = args.side
    return host


def main(argv=None):
    args = parse_args(argv)
    if not args.connect:
        print("受控验收参数通过；未连接数据库、行情或柜台。connect默认只读预检。")
        return
    from dotenv import load_dotenv
    from bomber.framework.trader.execution.ctp import CtpNativeTraderDriver, CtpTdApiTransport
    from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport, occupied_positions
    from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle
    from bomber.framework.trader.runtime.reports import LiveRunReport
    load_dotenv(PROJECT_ROOT / ".env")
    protected, hashes = protected_state(args.protect_state)
    report = LiveRunReport(args.report_dir, prefix="shared-mother-probe")
    report.begin()
    resources = ExitStack()
    host = driver = probe = None
    status, failure, shutdown_failure, plan = "failed", None, None, None
    observed_td_day = None
    raised = False
    try:
        transport = build_simnow_transport(CtpTdApiTransport, client_id=CLIENT_ID,
            flow_path=str(report.path / "td-flow"), timeout_seconds=args.timeout)
        CtpSharedAccountLifecycle.acquire(resources, transport, environment="simnow")
        _, locked_hashes = protected_state(args.protect_state)
        if locked_hashes != hashes:
            raise RuntimeError("获取账户锁前原检查点已变化，禁止继续测试")
        holder = {}
        def disconnected(reason):
            if "host" in holder:
                holder["host"].account.client.mark_disconnected(reason)
        driver = CtpNativeTraderDriver(CLIENT_ID, transport.account_id, transport,
            enable_simnow_orders=args.submit, max_session_orders=1, disconnect_handler=disconnected)
        resources.callback(driver.stop)
        driver.start(lambda row: None)
        observed_td_day = driver.trading_day
        if driver.trading_day != args.trading_day or not driver.is_simnow_session:
            raise RuntimeError("TD交易日或SimNow身份不符")
        if driver.reconcile_active_orders().orders:
            raise RuntimeError("全账户有活动订单，禁止独立测试")
        gross = occupied_positions(transport)
        spec = select_spec(read_specs(args), args, gross, protected)
        print(f"只读预检选择: {spec.symbol.lower()}.{spec.venue}；可用且不属原策略的合约={args.eligible_instruments}", flush=True)
        instrument, feed = make_feed(transport, spec, resources, report.path)
        deadline = time.monotonic() + args.timeout
        while True:
            try:
                plan = price_plan(*feed.quote(), args, spec)
                break
            except (RuntimeError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("测试合约未取得新鲜有效盘口，请在有行情时只读预检")
                time.sleep(0.05)
        print(f"测试计划（2来源各1手，最多1张母单）: {json.dumps(plan, ensure_ascii=False)}", flush=True)
        if not args.submit:
            status = "prechecked"
            return
        driver.stop()
        def validate():
            raw, observation = feed.quote()
            price_plan(raw, observation, args, spec)
            if "host" in holder:
                holder["host"].service.prices.update(instrument, Decimal(str(raw["LastPrice"])), observation.ts_event)
        host = build_host(args, driver, transport, spec, instrument, validate, resources, report.path / "account.json")
        holder["host"] = host
        validate()
        host.start()
        probe = exercise(host, instrument, args.limit_price, validate, timeout=min(args.timeout, 10))
        status = "completed"
    except BaseException as error:
        raised = True
        failure = str(error)
        raise
    finally:
        try:
            if host is not None:
                host.stop()
        except Exception as error:
            status, shutdown_failure = "failed", str(error)
        finally:
            unchanged = all(Path(path).exists() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                            for path, digest in hashes.items())
            if not unchanged:
                status, failure = "failed", "原策略检查点发生变化，须核对并发运行"
            snapshot = host.snapshot() if host is not None else {}
            events = tuple(host.account.execution.backend.reports) if host else ()
            snapshot.update(plan=plan, failure=failure, shutdown_failure=shutdown_failure,
                expected_trading_day=args.trading_day, observed_td_trading_day=observed_td_day,
                protected_state_unchanged=unchanged, protected_hashes=hashes,
                orders_submitted=driver.submitted_orders if driver else 0,
                verification="controlled_live_mother_probe" if args.submit else "readonly_plan_no_orders",
                shared_cancel_acceptance=probe.result(host, runtime_status=status) if probe else None,
                unexpected_fill=any(row.report_type.value in {"FILLED", "PARTIALLY_FILLED"} for row in events),
                final_gross=host.account.final_gross if host else None,
                final_active_orders=host.account.final_active_orders if host else None,
                new_state_file=str(report.path / "account.json") if args.submit else None)
            snapshot["controlled_sources_submitted"] = len(host.service.snapshot_state()["sources"]) if host else 0
            snapshot["original_strategy_signals_overridden"] = False
            snapshot["reference_evidence"] = getattr(args, "reference_evidence", None)
            snapshot["eligible_instruments"] = getattr(args, "eligible_instruments", [])
            snapshot["peer_state_after_owner_cancel"] = getattr(probe, "controlled_peer_state", None)
            try:
                report.write(status=status, fields=snapshot, session=host, events=events)
            finally:
                if host is None or shutdown_failure is None:
                    resources.close()
            if status == "failed" and not raised:
                raise RuntimeError(failure or shutdown_failure)


if __name__ == "__main__":
    main()
