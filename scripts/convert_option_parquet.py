"""将品种级期权 Parquet 拆成按交易日、合约存储的标准 Feather。

独立运行，只需要 pandas、numpy 和 pyarrow，不导入 Bomber 内核。
一次读取源表所需列；源表较大时请在有足够内存的机器上运行。
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re
import tempfile

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


DEFAULT_INPUT = Path("/work/backTest/data/market_data/options/5min/MO.parquet")
TIMEZONE = "Asia/Shanghai"
PRICES = ("open", "high", "low", "close")
INPUT_COLUMNS = ("trade_date", "code", *PRICES, "volume", "open_interest", "timestamp")
OUTPUT_COLUMNS = ("symbol", "trade_date", "datetime", *PRICES, "volume", "open_interest")
OPTION_CODE = re.compile(r"(?:MO|IO|HO)\d{2}(?:0[1-9]|1[0-2])-[CP]-[1-9]\d*(?:\.CFFEX)?")


def shanghai_time(values: pd.Series, field: str) -> pd.Series:
    """无时区值按北京时间解释；有时区值转换到北京时间。"""
    try:
        stamps = pd.to_datetime(values, errors="raise")
        if stamps.isna().any():
            raise ValueError("存在空时间")
        if stamps.dt.tz is None:
            stamps = stamps.dt.tz_localize(TIMEZONE)
        else:
            stamps = stamps.dt.tz_convert(TIMEZONE)
        return stamps.astype(f"datetime64[ns, {TIMEZONE}]")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field} 无法解析为北京时间: {exc}") from exc


def invalid_ohlc_mask(bars: pd.DataFrame) -> pd.Series:
    """标记高低价未包住开收盘价的行，不修改任何价格。"""
    return (bars.high.lt(bars[["open", "low", "close"]].max(axis=1))
            | bars.low.gt(bars[["open", "high", "close"]].min(axis=1)))


def prepare_bars(frame: pd.DataFrame, *, timestamp_label: str = "end",
                 interval_minutes: int = 5, strict_ohlc: bool = False) -> pd.DataFrame:
    """标准化字段；默认保留 OHLC 区间异常，严格模式才拒绝此类行。"""
    if timestamp_label not in {"start", "end"} or interval_minutes < 1:
        raise ValueError("时间标签须为 start/end，周期须为正整数分钟")
    if not frame.columns.is_unique or set(INPUT_COLUMNS) - set(frame):
        raise ValueError(f"源表须包含不重复的列: {', '.join(INPUT_COLUMNS)}")
    if frame.empty:
        raise ValueError("请求范围内没有数据")

    bars = frame.loc[:, list(INPUT_COLUMNS)].rename(
        columns={"code": "symbol", "timestamp": "datetime"}).copy()
    codes = bars.symbol.astype("string").str.strip().str.upper()
    valid_codes = codes.str.fullmatch(OPTION_CODE, na=False)
    if not valid_codes.all():
        raise ValueError(f"无法识别的中金所期权合约: {codes[~valid_codes].head(5).tolist()}")
    bars["symbol"] = codes.str.replace(r"\.CFFEX$", "", regex=True)
    days = shanghai_time(bars.trade_date, "trade_date")
    if not days.eq(days.dt.normalize()).all():
        raise ValueError("trade_date 必须是交易日期，不能带非零时分秒")
    bars["trade_date"] = days.dt.strftime("%Y-%m-%d")
    source_time = shanghai_time(bars.datetime, "timestamp")
    if "act_time" in frame:
        act_time = shanghai_time(frame.act_time, "act_time")
        if not act_time.eq(source_time).all():
            raise ValueError("act_time 与 timestamp 的北京时间不一致")
    completed = source_time + pd.Timedelta(
        minutes=interval_minutes if timestamp_label == "start" else 0)
    if not completed.dt.strftime("%Y-%m-%d").eq(bars.trade_date).all():
        raise ValueError("Bar 结束时间与 trade_date 不属于同一天")
    interval_ns = interval_minutes * 60 * 1_000_000_000
    if completed.astype("int64").mod(interval_ns).ne(0).any():
        raise ValueError(f"Bar 结束时间必须对齐 {interval_minutes} 分钟边界")
    bars["datetime"] = completed

    for field in (*PRICES, "volume"):
        values = pd.to_numeric(bars[field], errors="raise")
        if not np.isfinite(values.to_numpy(dtype=float)).all() or values.lt(0).any():
            raise ValueError(f"{field} 必须是有限非负数")
        if field == "volume":
            if values.mod(1).ne(0).any() or values.gt(np.iinfo(np.int64).max).any():
                raise ValueError("volume 必须是 int64 范围内的非负整数")
            bars[field] = values.astype("int64")
        else:
            bars[field] = values.astype("float64")
    # 此字段不参与 OHLCV 撮合。源值可能是持仓变化，也可能含缺失，
    # 格式转换保留其数值和缺失状态，不能擅自填零、累加或删除整根 Bar。
    bars["open_interest"] = pd.to_numeric(bars.open_interest, errors="raise")
    if strict_ohlc:
        invalid_ohlc = invalid_ohlc_mask(bars)
        if invalid_ohlc.any():
            raise ValueError(f"存在 OHLC 大小关系错误: {bars.loc[invalid_ohlc].head(3).to_dict('records')}")
    duplicate = bars.duplicated(["symbol", "datetime"], keep=False)
    if duplicate.any():
        raise ValueError(f"存在重复的合约、时间: "
                         f"{bars.loc[duplicate, ['symbol', 'datetime']].head(5).to_dict('records')}")
    return bars.loc[:, list(OUTPUT_COLUMNS)].sort_values(
        ["trade_date", "symbol", "datetime"], kind="stable").reset_index(drop=True)


def convert(source: Path | str, output_dir: Path | str, *, start_day: date | None = None,
            end_day: date | None = None, symbols: tuple[str, ...] = (),
            timestamp_label: str = "end", interval_minutes: int = 5,
            overwrite: bool = False, strict_ohlc: bool = False) -> dict:
    source, output_dir = Path(source).expanduser().resolve(), Path(output_dir).expanduser().resolve()
    if start_day and end_day and end_day < start_day:
        raise ValueError("结束日期不能早于开始日期")
    schema = pq.read_schema(source)
    missing = set(INPUT_COLUMNS) - set(schema.names)
    if missing:
        raise ValueError(f"源 Parquet 缺少字段: {sorted(missing)}")
    columns = [*INPUT_COLUMNS, *(["act_time"] if "act_time" in schema.names else [])]
    print(f"读取: {source}", flush=True)
    frame = pd.read_parquet(source, columns=columns)
    input_rows = len(frame)
    days = shanghai_time(frame.trade_date, "trade_date").dt.strftime("%Y-%m-%d")
    selected = pd.Series(True, index=frame.index)
    if start_day:
        selected &= days.ge(str(start_day))
    if end_day:
        selected &= days.le(str(end_day))
    if symbols:
        requested = {str(item).strip().upper().removesuffix(".CFFEX") for item in symbols}
        codes = frame.code.astype("string").str.strip().str.upper().str.replace(
            r"\.CFFEX$", "", regex=True)
        selected &= codes.isin(requested)
    frame = frame.loc[selected].reset_index(drop=True)
    bars = prepare_bars(frame, timestamp_label=timestamp_label, interval_minutes=interval_minutes,
                        strict_ohlc=strict_ohlc)
    del frame
    invalid_ohlc = invalid_ohlc_mask(bars)
    invalid_ohlc_rows = int(invalid_ohlc.sum())
    if invalid_ohlc_rows:
        print(f"[数据提示] OHLC 区间异常 {invalid_ohlc_rows:,} 行，原值保留，不删除或修正", flush=True)
        print("[数据提示] OHLC 样本: " + str(bars.loc[invalid_ohlc,
            ["symbol", "trade_date", "datetime", *PRICES]].head(5).to_dict("records")), flush=True)
    del invalid_ohlc
    interest = bars.open_interest.to_numpy(dtype=float, na_value=np.nan)
    negative_interest = np.isfinite(interest) & (interest < 0)
    missing_interest = np.isnan(interest)
    infinite_interest = np.isinf(interest)
    interest_issues = negative_interest | missing_interest | infinite_interest
    interest_counts = {
        "negative_open_interest_rows": int(negative_interest.sum()),
        "missing_open_interest_rows": int(missing_interest.sum()),
        "infinite_open_interest_rows": int(infinite_interest.sum()),
    }
    if interest_issues.any():
        print("[数据提示] open_interest 原值保留: "
              + ", ".join(f"{key}={value}" for key, value in interest_counts.items()), flush=True)
        print("[数据提示] 样本: " + str(bars.loc[interest_issues,
            ["symbol", "trade_date", "datetime", "open_interest"]].head(5).to_dict("records")), flush=True)
    del interest, negative_interest, missing_interest, infinite_interest, interest_issues
    groups = bars.groupby(["trade_date", "symbol"], sort=False, observed=True)
    # 先检查所有目标，避免普通的文件名冲突导致中途只转换一部分。
    if not overwrite:
        for day, symbol in groups.size().index:
            label = day.replace("-", "")
            target = output_dir / label / f"{symbol}_{label}.feather"
            if target.exists():
                raise FileExistsError(f"目标已存在: {target}；确认替换时使用 --overwrite")
    count = 0
    for (day, symbol), group in groups:
        label = day.replace("-", "")
        target = output_dir / label / f"{symbol}_{label}.feather"
        target.parent.mkdir(parents=True, exist_ok=True)
        # 同目录暂存后发布，异常时不留下半个 Feather 文件。
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            group.reset_index(drop=True).to_feather(temporary, compression="zstd")
            if overwrite:
                temporary.replace(target)
            else:
                # 原子创建，运行期间出现的同名文件也不会被覆盖。
                target.hardlink_to(temporary)
        finally:
            temporary.unlink(missing_ok=True)
        count += 1
        if count % 1000 == 0:
            print(f"已写入 {count:,} / {groups.ngroups:,} 个文件", flush=True)
    summary = {"input_rows": input_rows, "output_rows": len(bars), "files": count,
        "contracts": int(bars.symbol.nunique()), "days": int(bars.trade_date.nunique()),
        "first_day": bars.trade_date.min(), "last_day": bars.trade_date.max(),
        "zero_volume_rows": int(bars.volume.eq(0).sum()),
        "zero_price_rows": int(bars.loc[:, list(PRICES)].eq(0).any(axis=1).sum()),
        "invalid_ohlc_rows": invalid_ohlc_rows,
        **interest_counts,
        "interval_minutes": interval_minutes, "timestamp_label": "end", "output_dir": str(output_dir)}
    print("转换完成: " + ", ".join(f"{key}={value}" for key, value in summary.items()), flush=True)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="期权行情根目录，例如 /work/backTest/data/kline/opt")
    parser.add_argument("--start-day", type=date.fromisoformat)
    parser.add_argument("--end-day", type=date.fromisoformat)
    parser.add_argument("--symbol", action="append", default=[], help="可重复指定；默认全部合约")
    parser.add_argument("--timestamp-label", choices=("start", "end"), default="end",
                        help="源 timestamp 的标签；输出统一为结束时间，默认不移动时间")
    parser.add_argument("--interval-minutes", type=int, default=5)
    parser.add_argument("--overwrite", action="store_true", help="替换已有同名文件")
    parser.add_argument("--strict-ohlc", action="store_true",
                        help="OHLC 高低价未包住开收盘价时停止；默认统计并保留原值")
    args = parser.parse_args(argv)
    convert(args.input, args.output_dir, start_day=args.start_day, end_day=args.end_day,
            symbols=tuple(args.symbol), timestamp_label=args.timestamp_label,
            interval_minutes=args.interval_minutes, overwrite=args.overwrite,
            strict_ohlc=args.strict_ohlc)


if __name__ == "__main__":
    main()
