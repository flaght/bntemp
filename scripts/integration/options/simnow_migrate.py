"""08隔日/CTP→DolphinDB迁移准备；禁单查询与原子另存，不启动策略。"""
from argparse import ArgumentParser
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from importlib import import_module
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile
import time
from types import SimpleNamespace as NS

from .simnow_preflight import CLIENT, BUCKETS, checkpoint_summary, inspect_counterparty

ROOT = Path(__file__).resolve().parents[3]


def normalize(value):
    return json.loads(json.dumps(value, default=str))


def validate_original(saved):
    """沿公共解码/归属校验验证原事实，不凭净仓重建归属或订单历史。"""
    from bomber.framework.trader import persistence as storage
    from bomber.framework.trader.portfolio import TargetStore, PortfolioCoordinator, PositionManager
    from bomber.framework.trader.execution.order_state import OrderReportStateMachine
    from bomber.framework.trader.execution.attribution import PositionAttributionLedger
    from bomber.framework.trader.runtime.live.recovery import (StrategyPositionBinding,
        InstrumentPositionScope, validate_owned_positions, restored_target_revision, validate_finished_orders)
    from bomber.framework.market.basic.base import InstrumentId
    from bomber.framework.trader.execution.ctp.ledger import CtpPositionLedger
    summary = checkpoint_summary(saved)
    payload = saved.payload
    targets = TargetStore(); targets.restore(storage._decode_targets(payload["targets"]))
    if set(targets.all()) != {CLIENT}:
        raise ValueError("迁移要求原08完整目标与版本")
    portfolio = PortfolioCoordinator(); portfolio.restore(storage._decode_portfolio(payload["portfolio"]))
    positions = PositionManager(); state = storage._decode_positions(payload["positions"]); positions.restore(state)
    orders = {key: storage._decode_orders(value) for key, value in payload["orders"].items()}
    if set(orders) != {CLIENT}:
        raise ValueError("原订单账本客户端不一致")
    PositionAttributionLedger.validate_checkpoints(state.attribution, orders)
    machine = OrderReportStateMachine(CLIENT); machine.restore(orders[CLIENT])
    target = targets.all()[CLIENT]
    binding = StrategyPositionBinding(CLIENT, CLIENT, {key: InstrumentId.from_str(key) for key in target.targets})
    scope = InstrumentPositionScope(binding.targets.values())
    session = NS(runner=NS(position_manager=positions, target_store=targets, portfolio_coordinator=portfolio),
        client=NS(order_state_machine=machine))
    owned = validate_owned_positions(positions, binding, account_scope=scope)
    validate_finished_orders(session); restored_target_revision(session, binding)
    ledger = CtpPositionLedger(); ledger.restore(storage._decode_ctp_ledger(payload["ctp_ledgers"][CLIENT]))
    for instrument, quantity in owned.items():
        if ledger.snapshot(instrument).net_position != quantity:
            raise ValueError("原归属明细与双向账本不一致")
    checkpoint = storage._decode_ctp_driver(payload["ctp_drivers"][CLIENT])
    if checkpoint.orders or checkpoint.driver_id != CLIENT or checkpoint.trading_day != summary["ledger_trading_day"]:
        raise ValueError("原Driver活动订单或交易日不一致")
    return summary


def runtime_argv(identity, day, state_file, table, label, volume, report_dir):
    """保留原策略与风险参数；只改变声明的交易日及市场来源。"""
    if identity["mode"] != "simnow" or identity["environment"] != "realtime" or identity.get("flatten_day"):
        raise ValueError("此迁移只支持实时SimNow、无主动清仓日的持仓保留会话")
    if identity["policy"] != {"version":1,"account_reconciliation":"strict","position_source":"fills"}:
        raise ValueError("迁移须保留strict/归属成交政策")
    index = identity["sources"]["index"]
    if index["channel"] != "dolphindb-dfs":
        raise ValueError("本路径只迁移市场来源，指数保留DFS")
    argv = ["--connect","--mode","simnow","--enable-orders","--confirm-simnow",
        "--state-file",str(state_file),"--expected-trading-day",day,"--valuation-day",day,
        "--simnow-environment","realtime","--account-reconciliation","strict","--strategy-position-source","fills",
        "--market-source","dolphindb","--market-bar-table",table,"--market-bar-timestamp",label,
        "--market-volume-mode",volume,"--market-bar-max-age-seconds","120",
        "--reference-source","dolphindb","--reference-timeout","30","--reference-refresh-seconds","5",
        "--calendar-id",identity["calendar_manifest"]["calendar_id"],
        "--option-product","MO","--future-product","IM","--index-code","000852",
        "--index-source","dolphindb-dfs","--ready-timeout","180","--seconds","86400",
        "--max-session-orders","4","--report-dir",str(report_dir)]
    for key, flag in (("max_notional","--max-notional"),("frame_wait_seconds","--frame-wait-seconds"),
                      ("limit_offset_ticks","--limit-offset-ticks"),("ctp_price_type","--ctp-price-type")):
        argv.extend((flag,str(identity[key])))
    for key, value in identity["config"].items():
        argv.extend(("--"+key.replace("_","-"), ",".join(map(str,value)) if isinstance(value,list) else str(value)))
    for key in ("database","table","availability","poll_seconds","query_timeout","price_precision","size_precision"):
        argv.extend(("--index-"+key.replace("_","-"),str(index[key])))
    argv.extend(("--index-bar-timestamp",index["timestamp_label"]))
    return argv


def transform_payload(saved, new_identity, observation, multipliers):
    original = saved.payload["component_states"]["session_identity"]["identity"]
    new_identity = normalize(new_identity)
    changed = {key for key in original.keys() | new_identity.keys() if original.get(key) != new_identity.get(key)}
    allowed = {"valuation_day","td_day","calendar","calendar_manifest","reference_fingerprint","contracts","sources","labels"}
    if changed-allowed:
        raise ValueError(f"迁移不得改变原策略/风险/执行政策: {sorted(changed-allowed)}")
    if not original["td_day"] < new_identity["td_day"] or new_identity["td_day"] != observation["observed_td_trading_day"]:
        raise ValueError("迁移须向前跨日并使用当前柜台TD日")
    if original["sources"]["index"] != new_identity["sources"]["index"]:
        raise ValueError("此迁移不得更改指数来源身份")
    if new_identity["sources"]["market"]["channel"] != "dolphindb":
        raise ValueError("迁移目标须为DolphinDB分钟市场")
    if not observation["gross_matches_original_state"] or observation["active_orders"]:
        raise ValueError("迁移须再次确认原总仓一致且无活动订单")
    old_target, = saved.payload["targets"]
    old_keys = set(old_target["targets"])
    new_keys = set(multipliers)
    owned = {key:Decimal(str(value)) for key,value in observation["checkpoint"]["owned_positions"].items()}
    if not owned or not set(owned) <= new_keys:
        raise ValueError("原非零持仓腿不在新参考路由中")
    for key in owned:
        symbol = key.rsplit(".",1)[0]
        if symbol in original["contracts"] and original["contracts"][symbol] != new_identity["contracts"].get(symbol):
            raise ValueError("非零持仓期权的条款或对冲绑定发生变化")
    if any(Decimal(str(value)) for key,value in old_target["targets"].items() if key not in new_keys):
        raise ValueError("移除合约仍有非零旧目标")
    payload = deepcopy(saved.payload)
    payload["component_states"]["session_identity"] = {"version":1,"identity":new_identity}
    target_values = {key:str(owned.get(key,Decimal(0))) for key in sorted(new_keys)}
    payload["targets"][0]["targets"] = target_values
    payload["portfolio"]["contributions"][CLIENT] = [
        {"key":{"client_id":CLIENT,"instrument_id":key},"quantity":value} for key,value in target_values.items()]
    payload["portfolio"]["known_keys"] = [{"client_id":CLIENT,"instrument_id":key} for key in sorted(new_keys)]
    positions = payload["positions"]
    def keep_target(row):
        return row["target_key"] in new_keys or Decimal(str(row.get("quantity",0))) != 0
    positions["strategy_positions"] = [row for row in positions["strategy_positions"] if keep_target(row)]
    positions["strategy_revisions"] = [row for row in positions["strategy_revisions"] if row["target_key"] in new_keys]
    # 非零真实合约归属、订单/成交去重历史与外部账户事实保留。
    buckets = observation["authoritative_buckets"]
    ledger_rows = []
    for key in sorted(owned):
        quantities = tuple(Decimal(str(v)) for v in buckets[key]["quantities"])
        costs = tuple(Decimal(str(v)) for v in buckets[key]["position_costs"])
        if (sum(quantities[:2]),sum(quantities[2:])) != tuple(map(Decimal,observation["managed_gross"][key])):
            raise ValueError("新权威分桶与总仓不一致")
        row = {"instrument_id":key}
        multiplier = Decimal(str(multipliers[key]))
        if multiplier <= 0:
            raise ValueError("持仓合约乘数须为正")
        for name,qty,cost in zip(BUCKETS,quantities,costs):
            if qty < 0 or qty != qty.to_integral_value() or (qty and cost <= 0):
                raise ValueError("新权威分桶数量或成本无效")
            row[name] = str(qty)
            row[name+"_basis"] = str(cost/(qty*multiplier) if qty else Decimal(0))
        ledger_rows.append(row)
    payload["ctp_ledgers"][CLIENT] = {"trading_day":new_identity["td_day"],"positions":ledger_rows}
    signal = payload["component_states"]["vega_signal"]
    signal["option_goals"] = {key:str(owned.get(key+".CFFEX",Decimal(0))) for key in new_identity["contracts"]}
    signal["future_goals"] = {key.rsplit(".",1)[0]:str(owned.get(key,Decimal(0))) for key in new_keys if key.startswith("IM")}
    # 本次只携带已有仓位，标记当日不再选新约；原历史日期留在迁移审计。
    signal["sent_days"] = [new_identity["valuation_day"]]
    signal["flatten_requested"] = False
    return payload, {"changed_identity_fields":sorted(changed),"added_zero_routes":sorted(new_keys-old_keys),
        "removed_zero_routes":sorted(old_keys-new_keys),"carry_only_day":new_identity["valuation_day"],
        "prior_sent_days":saved.payload["component_states"]["vega_signal"]["sent_days"]}


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--connect",action="store_true")
    parser.add_argument("--source-state",type=Path,required=True)
    parser.add_argument("--source-sha256",required=True)
    parser.add_argument("--expected-trading-day",required=True)
    parser.add_argument("--output-state",type=Path,required=True)
    parser.add_argument("--market-bar-table",default="stream_cffex_1min")
    parser.add_argument("--market-bar-timestamp",choices=("start","end"),default="end")
    parser.add_argument("--market-volume-mode",choices=("cumulative","per-bar"),default="cumulative")
    parser.add_argument("--report-dir",type=Path,default=ROOT/"tests/results/option-vega-simnow-migration")
    args = parser.parse_args(argv)
    if not args.connect:
        parser.error("须--connect；迁移准备只读TD/参考，不报单、不启动策略")
    if len(args.source_sha256)!=64 or any(c not in "0123456789abcdef" for c in args.source_sha256):
        parser.error("source-sha256须为预检确认的64位摘要")
    source,output = args.source_state.resolve(),args.output_state.resolve()
    if source == output or output.exists():
        parser.error("output-state须为独立、尚不存在的文件")
    path = args.report_dir/f"sample-{time.time_ns()}";path.mkdir(parents=True,exist_ok=False)
    report = {"scope":"carry_position_migration_preparation","status":"failed","failure":None,
        "source_state":str(source),"output_state":str(output),"orders_enabled":False,
        "strategy_started":False,"source_modified":False,"cleanup_errors":[]}
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT/".env")
        from bomber.framework.trader.persistence import JsonStateStore
        from bomber.framework.trader.runtime.live.local import strategy_state_lock
        from bomber.framework.trader.runtime.live.channels.ctp import account_lock,build_simnow_transport
        from bomber.framework.trader.execution.ctp.native_driver import CtpNativeTraderDriver
        from bomber.framework.trader.execution.ctp.td_transport import CtpTdApiTransport
        from bomber.framework.trader.runtime.live.contracts import LiveRunContext
        from bomber.framework.trader.runtime.live.channels.ctp_recovery import validate_ctp_account
        entry = import_module("demos.08_option_vega.run_live")
        runtime = import_module("demos.08_option_vega.live_runtime")
        recovery = import_module("demos.08_option_vega.recovery")
        with ExitStack() as resources:
            resources.enter_context(strategy_state_lock(source));resources.enter_context(strategy_state_lock(output))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if digest != args.source_sha256:
                raise ValueError("原状态摘要与已确认预检不一致")
            saved = JsonStateStore(source).load();summary = validate_original(saved)
            report.update(source_sha256=digest,source_generation=saved.generation,checkpoint=summary)
            transport = build_simnow_transport(CtpTdApiTransport,client_id=CLIENT,
                flow_path=f"/tmp/bomber-vega-migration-{os.getpid()}",timeout_seconds=30)
            driver = CtpNativeTraderDriver(CLIENT,transport.account_id,transport,enable_test_orders=False,enable_simnow_orders=False)
            resources.callback(driver.stop);resources.enter_context(account_lock(transport.account_id))
            observation = {"checkpoint":summary,"cleanup_errors":[]}
            inspect_counterparty(driver,args.expected_trading_day,saved,observation)
            report["counterparty"] = observation
            output.parent.mkdir(parents=True,exist_ok=True)
            temporary = resources.enter_context(tempfile.TemporaryDirectory(prefix=".vega-migrate-",dir=output.parent))
            candidate = Path(temporary)/"state.json"
            live_argv = runtime_argv(saved.payload["component_states"]["session_identity"]["identity"],
                args.expected_trading_day,candidate,args.market_bar_table,args.market_bar_timestamp,
                args.market_volume_mode,ROOT/"tests/results/gap08-dolphin-simnow-resume")
            live_args = entry.parse_args(live_argv)
            context = LiveRunContext(args.expected_trading_day,resources)
            inputs = entry.prepare_inputs(live_args,context,NS())
            session = runtime.assemble(live_args,inputs,driver)
            identity = session.manager.state_components["session_identity"].snapshot_state()["identity"]
            multipliers = {str(i): session.feeds["market-bars"].get_instrument_meta(i).multiplier
                for i in session.references.instrument_ids.values()}
            migrated,changes = transform_payload(saved,identity,observation,multipliers)
            JsonStateStore(candidate).save(migrated,expected_generation=0)
            session.manager.restore();recovery.validate_restored_session(session,args.expected_trading_day)
            # 重新登录/核对；Driver禁单、Client未arm，行情/策略未启动。
            session.client.start()
            validate_ctp_account(session,transport,session.recovery_binding)
            if session.client.is_armed or driver.submitted_orders:
                raise RuntimeError("迁移准备不得开闸或报单")
            if not session.references.manifest.get("ready") or not session.runner.reference_is_current():
                raise RuntimeError("迁移准备期间参考资料已变化或失效")
            final = session.manager.save()
            session.client.stop()
            if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                report["source_modified"] = True
                raise ValueError("准备期间原文件变化")
            resume_argv = list(live_argv)
            resume_argv[resume_argv.index("--state-file")+1] = str(output)
            resume_argv.append("--resume")
            prepared_bytes = candidate.read_bytes()
            report.update(changes=changes,new_identity=identity,
                output_generation=final.generation,output_sha256=hashlib.sha256(prepared_bytes).hexdigest(),
                resume_argv=resume_argv,resume_command=shlex.join(["python","-u","-m",
                    "scripts.integration.options.simnow_resume","--migration-report",str((path/"summary.json").resolve())]),
                validation="公共恢复/归属/目标版本/订单去重/当前柜台总仓与今昨仓核对通过；未启动行情或策略")
        # 连接与参考资源关闭成功后独占发布，不暴露未核验候选。
        with strategy_state_lock(output), tempfile.NamedTemporaryFile(dir=output.parent) as publish:
            publish.write(prepared_bytes);publish.flush();os.fsync(publish.fileno())
            os.chmod(publish.name,0o600);os.link(publish.name,output)
            directory = os.open(output.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        report["status"] = "prepared"
    except Exception as error:
        report["failure"] = str(error)
        report["status"] = "failed"
    finally:
        (path/"summary.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+"\n")
        print("OPTION_VEGA_SIMNOW_MIGRATION:",json.dumps(report,ensure_ascii=False,default=str),flush=True)
        print("报告:",path/"summary.json",flush=True)
    return int(report["status"]!="prepared")


if __name__=="__main__":raise SystemExit(main())
