"""Run actual 08 Recording and review its exact report; never authorizes TD orders."""
import argparse
from datetime import datetime, timedelta
from importlib import import_module
import json
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trading-day", required=True)
    parser.add_argument("--option-slot", default="13:58")
    parser.add_argument("--seconds", type=int, default=4200)
    parser.add_argument("--ready-timeout", type=int, default=180)
    parser.add_argument("--report-dir", type=Path, default=Path("tests/results/gap08-recording"))
    parser.add_argument("--summary", type=Path, help="仅复核已有summary.json，不连接")
    parser.add_argument("--allow-no-target", action="store_true", help="只验决策评价，不声称非零目标链通过")
    args = parser.parse_args(argv)
    worker_exit_code = None
    root = Path(__file__).resolve().parents[3]
    review = review_recording
    if args.summary:
        path = args.summary.resolve()
        destination = path.parent / "recording-acceptance.json"
    else:
        try:
            day = datetime.strptime(args.trading_day, "%Y%m%d").date()
            hour, minute = map(int, args.option_slot.split(":"))
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            decision = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now.date() != day or now + timedelta(seconds=args.ready_timeout+65) >= decision:
                raise ValueError("请在目标交易日选约时点前至少ready-timeout+65秒启动")
            if args.seconds < 60 or args.ready_timeout < 1 or now+timedelta(seconds=args.seconds) <= decision+timedelta(seconds=10):
                raise ValueError("运行时长须覆盖选约分钟及其等待时间")
        except ValueError as error:
            parser.error(str(error))
        output = args.report_dir.resolve() / f"acceptance-{time.time_ns()}"
        output.mkdir(parents=True)
        destination = output / "recording-acceptance.json"
        command = [sys.executable, "-u", "-m", "demos.08_option_vega.run_live",
            "--connect", "--mode", "recording", "--market-source", "ctp", "--ctp-price-type", "last",
            "--simnow-environment", "realtime", "--expected-trading-day", args.trading_day,
            "--valuation-day", args.trading_day, "--calendar-id", "china.cffex",
            "--reference-source", "dolphindb", "--option-product", "MO", "--future-product", "IM",
            "--index-code", "000852", "--index-source", "dolphindb-dfs",
            "--index-feature-config", str(root/"bomber/framework/dataprep/features/configs/features.json"),
            "--index-dataset", "index.minute", "--index-schema-version", "1",
            "--index-database", "dfs://min_bar", "--index-table", "index_1min",
            "--index-bar-timestamp", "end", "--index-availability", "observed",
            "--index-price-precision", "4", "--index-size-precision", "4",
            "--index-poll-seconds", ".2", "--index-query-timeout", "2", "--frame-wait-seconds", "5",
            "--option-slot", args.option_slot, "--ready-timeout", str(args.ready_timeout), "--seconds", str(args.seconds),
            "--max-option-lots", "1", "--max-total-option-lots", "2", "--max-future-lots", "1",
            "--max-notional", "2000000", "--report-dir", str(output/"runtime")]
        print("启动真实Recording；不连接TD、不报单", flush=True)
        try:
            process = subprocess.run(command, cwd=root, timeout=args.seconds+args.ready_timeout+180)
            worker_exit_code = process.returncode
            paths = list((output/"runtime").glob("*/summary.json"))
            if len(paths) != 1:
                raise ValueError(f"运行退出码={process.returncode}，未取得唯一summary.json")
            path = paths[0]
        except (subprocess.TimeoutExpired, ValueError) as error:
            destination.write_text(json.dumps({"status": "failed", "errors": [str(error)]}, ensure_ascii=False, indent=2))
            print(f"验收失败: {error}\n验收报告: {destination}", flush=True)
            return 1
    try:
        result = review(json.loads(path.read_text()), trading_day=args.trading_day,
            option_slot=args.option_slot, require_target=not args.allow_no_target)
    except (ValueError, KeyError, TypeError) as error:
        result = {"status": "failed", "errors": [f"报告内容不完整或无效: {error}"]}
    result["runtime_summary"] = str(path)
    result["worker_exit_code"] = worker_exit_code
    if worker_exit_code not in (None, 0):
        result["status"] = "failed"
        result.setdefault("errors", []).append(f"Recording进程退出码={worker_exit_code}")
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(f"Recording验收: {result}\n验收报告: {destination}", flush=True)
    return int(result["status"] != "passed")


from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo


def review_recording(summary, *, trading_day, option_slot="13:58", require_target=True):
    errors = []
    def require(condition, message):
        if not condition:
            errors.append(message)
    require(summary.get("status") == "passed", "运行本身未通过")
    require(summary.get("mode") == "recording" and summary.get("execution_origin") == "recording", "不是Recording来源")
    require(summary.get("simnow_environment") == "realtime", "真实实时验收不能使用Replay")
    market_source = summary.get("market_source")
    require(market_source in {"ctp", "dolphindb"} and summary.get("index_source") == "dolphindb-dfs",
            "行情来源不符合CTP/分钟推送+DFS指数验收范围")
    require(summary.get("trading_day") == trading_day and summary.get("valuation_day") == trading_day, "交易日/估值日不符")
    market_identity = summary.get("market_sources", {}).get("market", {})
    stream_verified = False
    if market_source == "ctp":
        require(summary.get("observed_md_trading_day") == trading_day, "没有观测到当天CTP MD交易日")
    elif market_source == "dolphindb":
        require(summary.get("observed_md_trading_day") is None, "分钟推送仍关联CTP MD")
        require(market_identity.get("channel") == "dolphindb" and bool(market_identity.get("table")), "分钟推送来源身份缺失")
        require(market_identity.get("timestamp_label") in {"start", "end"}
            and market_identity.get("volume_mode") in {"per-bar", "cumulative"}, "分钟标签或成交量语义缺失")
        streams = [row for row in summary.get("market_streams", []) if row.get("source_id") == "vega-market"]
        required_columns = {"minTime", "date", "Code", "open", "high", "low", "close", "volume"}
        stream_verified = len(streams) == 1 and streams[0].get("received_bars", 0) > 0 \
            and streams[0].get("conversion_errors") == 0 \
            and required_columns <= set(streams[0].get("resolved_columns", {}).get(market_identity.get("table"), []))
        require(stream_verified, "分钟推送缺实际接收/结构证据或存在转换错误")
    for key, label in (("orders_submitted", "柜台报单"), ("fills_received", "成交")):
        if key not in summary:
            errors.append(f"报告缺少{label}计数证据")
        else:
            require(summary[key] == 0, f"Recording出现{label}")
    require(summary.get("account_positions") is None and summary.get("observed_td_trading_day") is None, "Recording关联了物理账户")
    require(summary.get("calendar_manifest", {}).get("backend") == "compiled", "Live没有使用公共编译日历")
    queries = summary.get("feature_queries", [])
    # Reports are written after stop: connected=False is normal, retain query evidence.
    query_ok = bool(queries) and all(q.get("last_query_ns") is not None and not q.get("failure")
        and q.get("cached_records", 0) > 0 for q in queries)
    delivered = query_ok and all(q.get("emitted_versions") for q in queries)
    require(query_ok, "指数查询无成功证据")
    require(delivered, "指数尚未送达策略分钟帧")
    require(not summary.get("cleanup_errors"), "资源清理失败")
    entries = [row for row in summary.get("entry_audit", [])
        if row.get("date") == datetime.strptime(trading_day, "%Y%m%d").date().isoformat()
        and row.get("slot") == option_slot]
    require(len(entries) == 1, "选约时点没有唯一决策审计")
    frames = {row["valuation_ns"]: row for row in summary.get("frame_audit", [])}
    target_verified = False
    if len(entries) == 1:
        entry = entries[0]
        stamp = entry["signal_ns"]
        slot = datetime.fromtimestamp(stamp/1e9, ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
        require(slot == option_slot, "决策实际业务时间不符")
        frame = frames.get(stamp, {})
        require(frame.get("status") == "CONSUMED", "选约帧没有实际消费")
        observations = frame.get("observations", {})
        index_key = summary.get("index_code", "000852")
        require(index_key in observations, "选约帧缺指数")
        require(observations.get(index_key, {}).get("source_id") == "index-bars", "指数来源绑定不符")
        def source_times(row):
            if market_source == "dolphindb" and row.get("source_id") == "market-bars":
                return (stamp - 60_000_000_000,) if market_identity.get("timestamp_label") == "start" else (stamp,)
            return (stamp, stamp-1)
        require(all(row.get("source_ns") in source_times(row) and
            row.get("received_ns", -1) >= stamp and row.get("received_ns", -1) <= frame.get("deadline_ns", -1)
            for row in observations.values()), "选约帧业务/接收时间不一致")
        require(frame.get("request_ns", -1) >= frame.get("deadline_ns", 0), "分钟帧未等到接收期限")
        targets = {key: value for key, value in entry.get("targets", {}).items() if Decimal(str(value)) != 0}
        require(index_key not in targets, "指数进入交易目标")
        require(all(key in observations for key in targets), "目标腿没有同分钟行情")
        require(all(observations.get(key, {}).get("source_id") == "market-bars" for key in targets), "交易腿来源绑定不符")
        require(any(key.startswith("IM") for key in observations), "选约帧缺同月期货")
        require(all(entry.get("future_bindings", {}).get(key) in observations for key in targets), "目标期权缺明确绑定的同月期货行情")
        require(entry.get("outcome") in {"TARGET", "NO_VALID_ENTRY", "ZERO_ALLOCATION"}, "选约未完成评价")
        recorded = [request for request in summary.get("recorded_requests", []) if request.get("signal_ns") == stamp]
        actual_targets = [{key.rsplit(".", 1)[0]: Decimal(str(value)) for key, value in request.get("targets", {}).items()
            if Decimal(str(value)) != 0} for request in recorded]
        matched = any(all(values.get(key) == Decimal(str(value)) for key, value in targets.items()) for values in actual_targets)
        target_verified = entry.get("outcome") == "TARGET" and bool(targets) and matched
        require(all(summary.get("index_code", "000852") not in values for values in actual_targets), "实际Recording请求包含指数目标")
    if require_target:
        require(target_verified, "没有非零Recording目标，目标链验收仍未通过")
    skipped = [row for row in frames.values() if row.get("status") == "SKIPPED_INDEX"]
    require(not any(entry.get("signal_ns") == row["valuation_ns"] for entry in entries for row in skipped), "超时跳过帧产生了选约决策")
    return {"status": "failed" if errors else "passed", "scope": "target_chain" if require_target else "decision_only",
        "trading_day": trading_day, "option_slot": option_slot, "errors": errors,
        "target_chain_verified": target_verified, "entry_audit": entries,
        "runtime_failure": summary.get("failure"), "session_readiness": summary.get("session_readiness"),
        "index_query_verified": query_ok, "index_bar_delivery_verified": bool(delivered),
        "market_stream_verified": stream_verified,
        "frames": summary.get("frames"), "skipped_index_frames": summary.get("skipped_index_frames"),
        "timeout_seen_online": bool(skipped), "timeout_note": "已观测同分钟指数缺失后跳过" if skipped else "无线上超时样本时，超时拒绝仅由离线测试覆盖"}


if __name__ == "__main__":
    sys.exit(main())
