"""从已核验迁移报告恢复08；正常停于15:00或Ctrl+C，保留持仓。"""
from argparse import ArgumentParser
from datetime import datetime,timedelta
from importlib import import_module
import hashlib
import json
import logging
from pathlib import Path
import signal
from zoneinfo import ZoneInfo


def main(argv=None):
    parser=ArgumentParser(description=__doc__)
    parser.add_argument("--migration-report",type=Path,required=True)
    args=parser.parse_args(argv)
    report=json.loads(args.migration_report.read_text())
    if report.get("status")!="prepared" or report.get("orders_enabled") is not False or report.get("source_modified"):
        raise ValueError("迁移准备未通过，不能启动恢复")
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[3]/".env")
    from bomber.framework.trader.persistence import JsonStateStore
    entry=import_module("demos.08_option_vega.run_live")
    live_args=entry.parse_args(report["resume_argv"])
    saved=JsonStateStore(live_args.state_file).load()
    if saved.generation < report["output_generation"] or saved.payload["component_states"]["session_identity"]["identity"]!=report["new_identity"]:
        raise ValueError("新状态与迁移身份或版本不一致")
    if saved.generation==report["output_generation"] and hashlib.sha256(live_args.state_file.read_bytes()).hexdigest()!=report["output_sha256"]:
        raise ValueError("初始迁移状态摘要不一致")
    now=datetime.now(ZoneInfo("Asia/Shanghai"))
    stop_at=datetime.strptime(live_args.expected_trading_day,"%Y%m%d").replace(hour=15,tzinfo=now.tzinfo)
    if now.date()!=stop_at.date() or now>=stop_at:
        raise ValueError("只恢复声明交易日收盘前会话；跨日须重新核对与迁移")
    if now+timedelta(seconds=live_args.ready_timeout+65)>=stop_at:
        raise ValueError("距收盘不足ready-timeout+65秒，不能完成新鲜行情预热；保留迁移文件，不连接")
    logging.basicConfig(level=getattr(logging,live_args.log_level),format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    runtime=entry.build_runtime(live_args)
    original_poll=runtime.controller.poll
    stop_requested=False
    def request_stop(signum,frame):
        nonlocal stop_requested
        stop_requested=True
        print("已请求正常停机：撤销本会话活动订单、核验并保存；保留已成交仓位。",flush=True)
    def poll(session):
        return False if stop_requested or datetime.now(now.tzinfo)>=stop_at else original_poll(session)
    runtime.controller.poll=poll
    signal.signal(signal.SIGINT,request_stop);signal.signal(signal.SIGTERM,request_stop)
    print(f"恢复迁移文件：{live_args.state_file}；当日不重新选约；自动停机：{stop_at.isoformat()}",flush=True)
    try:runtime.run()
    finally:runtime.stop()


if __name__=="__main__":main()
