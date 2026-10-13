"""只读核对回测失败分钟的期货及指数文件，不启动引擎或连接柜台。"""
from __future__ import annotations

import argparse
from datetime import date, time
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bomber.framework.dataprep.bars import read_bar_frame
from bomber.framework.dataprep.catalog import scan_bar_files
from bomber.framework.dataprep.contracts import BarReadSpec
from bomber.framework.dataprep.paths import resolve_paths


def inspect_file(path, key, *, at, timestamp_label="end", max_age_seconds=300):
    """沿用回测的标准化与质量校验，保留原始标签和完成分钟证据。"""
    import pandas as pd

    result = read_bar_frame(path, BarReadSpec(timestamp_label=timestamp_label,
        trading_day_policy="day_session"), key)
    frame = result.frame
    decision = pd.Timestamp(f"{key.trading_day} {at}", tz="Asia/Shanghai")
    previous = frame.loc[frame.datetime <= decision]
    later = frame.loc[frame.datetime > decision]
    latest = None if previous.empty else previous.iloc[-1]
    age = None if latest is None else (decision - latest.datetime).total_seconds()
    status = ("NO_BAR_BY_DECISION" if latest is None else
        "FRESH_AT_DECISION" if age <= max_age_seconds else "STALE_AT_DECISION")
    sample = frame.loc[(frame.datetime >= decision - pd.Timedelta(minutes=10)) &
        (frame.datetime <= decision + pd.Timedelta(minutes=2))]
    return {
        "status": status, "path": str(result.path), "symbol": key.symbol,
        "trading_day": str(key.trading_day), "timestamp_label": timestamp_label,
        "decision_time": decision.isoformat(), "rows": len(frame),
        "source_first": frame.source_timestamp.iloc[0].isoformat(),
        "source_last": frame.source_timestamp.iloc[-1].isoformat(),
        "completed_first": frame.datetime.iloc[0].isoformat(),
        "completed_last": frame.datetime.iloc[-1].isoformat(),
        "rows_by_decision": len(previous),
        "latest_by_decision": None if latest is None else latest.datetime.isoformat(),
        "age_seconds": age, "max_market_age_seconds": max_age_seconds,
        "first_after_decision": None if later.empty else later.datetime.iloc[0].isoformat(),
        "has_1358": bool(frame.datetime.eq(decision.normalize() + pd.Timedelta(hours=13, minutes=58)).any()),
        "nearby_rows": [{"source_time": row.source_timestamp.isoformat(),
            "completed_time": row.datetime.isoformat(), "close": str(row.close),
            "volume": str(row.volume)} for row in sample.itertuples(index=False)],
    }


def inspect_market(root, kind, symbol, day, **kwargs):
    if root is None or not root.is_dir():
        return {"status": "DIRECTORY_MISSING", "directory": str(root)}
    try:
        files = {key: path for key, path in scan_bar_files(root, kind).items()
            if key.symbol == symbol}
        key = next((key for key in files if key.trading_day == day), None)
        previous = max((key for key in files if key.trading_day < day),
            key=lambda key: key.trading_day, default=None)
        result = ({"status": "FILE_MISSING", "symbol": symbol, "trading_day": str(day)}
            if key is None else inspect_file(files[key], key, **kwargs))
        result["directory"] = str(root)
        result["previous_available_file"] = None if previous is None else str(files[previous])
        return result
    except (ValueError, FileNotFoundError, OSError, ImportError) as error:
        return {"status": "INPUT_ERROR", "directory": str(root), "error": str(error)}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=date.fromisoformat, required=True)
    parser.add_argument("--symbol", default="IM2609")
    parser.add_argument("--at", default="09:37")
    parser.add_argument("--index-code", default="000852")
    parser.add_argument("--bar-timestamp", choices=("start", "end"), default="end")
    parser.add_argument("--max-market-age-seconds", type=int, default=300)
    for name in ("data-root", "fut-dir", "index-dir"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args(argv)
    args.symbol = args.symbol.strip().split(".")[0].upper()
    if not args.symbol or len(args.index_code) != 6 or not args.index_code.isdigit():
        parser.error("合约代码不能为空，指数代码须为六位数字")
    try:
        if time.fromisoformat(args.at).strftime("%H:%M") != args.at:
            raise ValueError
    except ValueError:
        parser.error("at须为HH:MM")
    if args.max_market_age_seconds <= 0:
        parser.error("max-market-age-seconds须为正整数")
    return args


def main(argv=None):
    args = parse_args(argv)
    # 与回测入口一样加载.env，已有运行环境变量优先。
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    paths = resolve_paths(vars(args), validate=False)
    kwargs = dict(at=args.at, timestamp_label=args.bar_timestamp,
        max_age_seconds=args.max_market_age_seconds)
    report = {"scope": "file_coverage_not_replay_delivery", "future": inspect_market(
        paths.fut, "future", args.symbol, args.day, **kwargs), "index": inspect_market(
        paths.index, "index", args.index_code, args.day, **kwargs)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(any(report[key]["status"] != "FRESH_AT_DECISION" for key in ("future", "index")))


if __name__ == "__main__":
    sys.exit(main())
