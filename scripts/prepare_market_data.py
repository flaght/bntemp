"""Prepare versioned market Feather partitions, without loading a trading engine."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from time import perf_counter

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bomber.framework.dataprep.contracts import BarReadSpec
from bomber.framework.dataprep.prepared import PreparedDataset, PreparedSource, build_prepared_dataset
from bomber.framework.cli import load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, help="完整核验已有版本后退出")
    parser.add_argument("--config-file", type=Path, help="可复用回测YAML中的数据路径、日期和读取政策")
    parser.add_argument("--config-id", help="回测YAML配置ID；命令行同名参数优先")
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--fut-dir", type=Path)
    parser.add_argument("--opt-dir", type=Path)
    parser.add_argument("--future-product", action="append", help="可重复指定；默认IM")
    parser.add_argument("--option-product", action="append", help="可重复指定；默认MO")
    parser.add_argument("--asset", choices=("both", "future", "option"), default="both")
    parser.add_argument("--start-day", type=date.fromisoformat)
    parser.add_argument("--end-day", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, help="新的版本目录，禁止覆盖已存在版本")
    parser.add_argument("--base-snapshot", type=Path, help="增量构建：复用内容与政策未变的交易日分区")
    parser.add_argument("--option-bar-minutes", type=int, choices=(1, 5))
    parser.add_argument("--bar-timestamp", choices=("start", "end"))
    parser.add_argument("--options-bar-timestamp", choices=("start", "end"))
    parser.add_argument("--option-ohlc-policy", choices=("reject", "expand_range"))
    args = parser.parse_args(argv)
    started = perf_counter()
    try:
        if args.verify:
            result = PreparedDataset(args.verify, verify="full").verify_all()
        else:
            config = {}
            if args.config_file:
                for key, value in load_config(args.config_file, args.config_id).items():
                    name = key.replace("-", "_")
                    if name in config:
                        raise ValueError(f"配置参数重复: {name}")
                    config[name] = value
            if args.config_id and args.config_file is None:
                parser.error("config-id须同时提供config-file")
            for name, convert, default in (
                    ("data_root", Path, None), ("fut_dir", Path, None), ("opt_dir", Path, None),
                    ("start_day", date.fromisoformat, None), ("end_day", date.fromisoformat, None),
                    ("option_bar_minutes", int, 5), ("bar_timestamp", str, "end"),
                    ("options_bar_timestamp", str, None), ("option_ohlc_policy", str, "reject")):
                if getattr(args, name) is None:
                    value = config.get(name, default)
                    setattr(args, name, None if value is None else convert(str(value)))
            if args.option_bar_minutes not in (1, 5):
                parser.error("option-bar-minutes须为1或5")
            # Match the 08 M1 path rule when consuming its YAML: a retained M5
            # opt_dir cannot silently become an M1 preparation source.
            if args.config_file and args.option_bar_minutes == 1:
                if args.data_root is None:
                    parser.error("回测YAML的M1准备须指定data-root")
                args.opt_dir = args.data_root / "kline/opt"
            for name, default in (("future_product", "IM"), ("option_product", "MO")):
                if getattr(args, name) is None:
                    value = config.get(name, default)
                    setattr(args, name, value if isinstance(value, list) else [value])
            if args.start_day is None or args.end_day is None or args.output is None:
                parser.error("准备数据须指定start-day、end-day、output")
            sources = []
            for kind, selected, supplied, suffix, products in (
                    ("future", args.asset in ("both", "future"), args.fut_dir, "fut", args.future_product or ["IM"]),
                    ("option", args.asset in ("both", "option"), args.opt_dir, "opt", args.option_product or ["MO"])):
                if not selected:
                    continue
                directory = supplied or (args.data_root / "kline" / suffix if args.data_root else None)
                if directory is None:
                    parser.error(f"须通过data-root或{suffix}-dir指定{kind}行情目录")
                spec = (BarReadSpec(timestamp_label=args.bar_timestamp, trading_day_policy="day_session")
                    if kind == "future" else BarReadSpec(interval_seconds=args.option_bar_minutes * 60,
                        timestamp_label=args.options_bar_timestamp or args.bar_timestamp,
                        trading_day_policy="day_session", ohlc_policy=args.option_ohlc_policy))
                sources.extend(PreparedSource(kind, directory, product, spec) for product in products)
            result = build_prepared_dataset(sources, args.output, start_day=args.start_day,
                end_day=args.end_day, base_snapshot=args.base_snapshot, progress=lambda value: print(value, flush=True))
            result = {key: result[key] for key in ("snapshot_id", "requested", "source_files", "rows", "partitions", "reused_partitions")}
            result["output"] = str(args.output.resolve())
        print(json.dumps({"status": "passed", "seconds": round(perf_counter()-started, 3), **result},
                         ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
