import argparse
import os
import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {
    "date",
    "minTime",
    "Code",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
}

FUTURE_PATTERN = r"[A-Za-z]+\d{3,4}(?:\.[A-Za-z]+)?"

FUTURE_COLUMNS = ("vt_symbol", "symbol", "exchange", "open", "high", "low", "close",
                  "date", "time", "datetime", "volume", "value", "open_interest", "vwap")

OPTION_PATTERN = (r"[A-Za-z]+\d{4}-[CPcp]-\d+(?:\.\d+)?(?:\.[A-Za-z]+)?")

# 使用不可变元组，防止循环中修改公共字段列表。
COMMON_COLUMNS = (
    "symbol",
    "trade_date",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "volume_accumulate",
    "turnover_accumulate",
)

OPTIONAL_COLUMNS = (
    "open_interest",
    "prev_close",
    "prev_settlement",
    "bidPrice1",
    "askPrice1",
    "bidVolume1",
    "askVolume1",
)


def build_datetime(frame, timezone):
    """date + minTime，保留北京时间，不转换为 UTC。"""
    days = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    times = frame["minTime"]

    if pd.api.types.is_timedelta64_dtype(times):
        offsets = times
    elif pd.api.types.is_datetime64_any_dtype(times):
        # DolphinDB TIME 可能返回 1970-01-01 HH:MM:SS。
        parsed = pd.to_datetime(times, errors="raise")
        offsets = parsed - parsed.dt.normalize()
    else:
        offsets = pd.to_timedelta(times.astype(str), errors="raise")

    if days.isna().any() or offsets.isna().any():
        raise ValueError("date 或 minTime 存在空值")

    invalid_time = ((offsets < pd.Timedelta(0))
                    | (offsets >= pd.Timedelta(days=1)))
    if invalid_time.any():
        raise ValueError("minTime 超出一天的范围")

    return (days + offsets).dt.tz_localize(
        timezone,
        ambiguous="raise",
        nonexistent="raise",
    )


def convert_cumulative(values, name, first_row_policy):
    """把交易日累计值转换为分钟增量。"""
    increments = values.diff()

    if (increments.iloc[1:] < 0).any():
        raise ValueError(f"{name} 累计值回退，请检查重置或数据修订")

    first = values.iloc[0]
    if first == 0:
        increments.iloc[0] = 0
    elif first_row_policy == "zero-baseline":
        increments.iloc[0] = first
    else:
        raise ValueError(f"{name} 首条累计值为 {first}，无法确定首分钟增量；"
                         "确认完整覆盖当日交易后，"
                         "使用 --first-row-policy zero-baseline")

    return increments


def prepare_bars(group, day, source_timezone, first_row_policy):
    bars = group.copy()

    if not bars.columns.is_unique:
        raise ValueError("原始查询结果包含重复列名")

    bars["datetime"] = build_datetime(bars, source_timezone)
    bars = bars.sort_values("datetime").reset_index(drop=True)

    if bars["datetime"].duplicated().any():
        raise ValueError("同一合约或指数存在重复分钟")

    numeric_columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
    ]
    numeric_columns.extend(name for name in OPTIONAL_COLUMNS
                           if name in bars.columns)

    for column in numeric_columns:
        bars[column] = pd.to_numeric(bars[column], errors="raise")
        values = bars[column].to_numpy(dtype=float)

        if not np.isfinite(values).all():
            raise ValueError(f"{column} 存在空值或非有限值")

    price_columns = ["open", "high", "low", "close"]
    if (bars[price_columns] <= 0).any().any():
        raise ValueError("OHLC 存在非正价格")

    if (bars[["volume", "turnover"]] < 0).any().any():
        raise ValueError("成交量或成交额为负")

    if "open_interest" in bars.columns:
        if (bars["open_interest"] < 0).any():
            raise ValueError("持仓量为负")

    invalid_ohlc = ((bars["high"] < bars[["open", "close"]].max(axis=1))
                    | (bars["low"] > bars[["open", "close"]].min(axis=1))
                    | (bars["high"] < bars["low"]))
    if invalid_ohlc.any():
        raise ValueError("OHLC 高低价关系异常")

    bars["volume_accumulate"] = bars["volume"]
    bars["turnover_accumulate"] = bars["turnover"]

    for column in ("volume", "turnover"):
        bars[column] = convert_cumulative(
            bars[f"{column}_accumulate"],
            column,
            first_row_policy,
        )

    if not np.equal(bars["volume"], np.floor(bars["volume"])).all():
        raise ValueError("分钟成交量不是整数")

    bars["volume"] = bars["volume"].astype("int64")
    bars["trade_date"] = pd.Timestamp(day).strftime("%Y-%m-%d")

    # 每个合约重新生成独立列表，不修改 COMMON_COLUMNS。
    columns = list(COMMON_COLUMNS)
    columns.extend(name for name in OPTIONAL_COLUMNS if name in bars.columns)

    # 仅保存回测所需字段；不保存 timestamp。
    return bars.loc[:, columns].reset_index(drop=True)



def load_future_specs(path):
    """用fut_basic的真实合约代码、交易所、乘数构建导出映射。"""
    frame = pd.read_feather(path)
    required = {"symbol", "exchangeCD", "contMultNum"}
    if not frame.columns.is_unique or not required.issubset(frame):
        raise ValueError(f"fut_basic重复列或缺少字段：{sorted(required - set(frame))}")
    aliases = {"SHFE":"XSGE", "DCE":"XDCE", "CZCE":"XZCE", "INE":"XSIE",
               "GFEX":"XGFE", "CFFEX":"CCFX", "XCFX":"CCFX"}
    allowed = {"XSGE", "XDCE", "XZCE", "XSIE", "XGFE", "CCFX"}
    specs = {}
    for row in frame.to_dict("records"):
        name = str(row["symbol"]).strip().split(".")[0]
        exchange = str(row["exchangeCD"]).strip().upper()
        exchange = aliases.get(exchange, exchange)
        multiplier = float(row["contMultNum"])
        if not re.fullmatch(FUTURE_PATTERN, name) or exchange not in allowed:
            raise ValueError(f"fut_basic合约或交易所无效：{name}/{exchange}")
        if not np.isfinite(multiplier) or multiplier <= 0:
            raise ValueError(f"{name}合约乘数无效")
        key = name.upper()
        spec = {"symbol":name, "exchange":exchange, "multiplier":multiplier}
        if key in specs and specs[key] != spec:
            raise ValueError(f"fut_basic重复合约条款冲突：{name}")
        specs[key] = spec
    return specs


def prepare_future_bars(group, day, source_timezone, first_row_policy, spec):
    if "open_interest" not in group:
        raise ValueError("期货行情缺少open_interest，不能用零值冒充持仓量")
    minute = prepare_bars(group, day, source_timezone, first_row_policy)
    # 旧期货文件使用上海本地无时区datetime；仅去掉时区，不转为UTC。
    clocks = minute["datetime"].dt.tz_localize(None)
    bars = pd.DataFrame(index=minute.index)
    bars["vt_symbol"] = spec["symbol"] + "." + spec["exchange"]
    bars["symbol"] = spec["symbol"]
    bars["exchange"] = spec["exchange"]
    for name in ("open", "high", "low", "close"):
        bars[name] = minute[name].astype("float64")
    bars["date"] = clocks.dt.strftime("%Y-%m-%d")
    bars["time"] = clocks.dt.strftime("%H:%M:%S")
    bars["datetime"] = clocks
    bars["volume"] = minute["volume"].astype("float64")
    bars["value"] = minute["turnover"].astype("float64")
    bars["open_interest"] = minute["open_interest"].astype("float64")
    # 暂时保留旧字段，以收盘价占位；不把它当成真实成交均价。
    bars["vwap"] = bars["close"]
    return bars.loc[:, list(FUTURE_COLUMNS)]

def export_day(
    session,
    database,
    table,
    day,
    kind,
    source_timezone,
    first_row_policy,
    output_dir,
    overwrite=False,
    future_specs=None,
):
    day = pd.Timestamp(day).date()
    output_dir = Path(output_dir)

    # 数据库和表名通过变量上传，日期使用原生日期字面量。
    session.upload({
        "exportDbPath": database,
        "exportTableName": table,
    })

    day_literal = day.strftime("%Y.%m.%d")
    sql = f"""
        select *
        from loadTable(exportDbPath, exportTableName)
        where date = {day_literal}
        order by Code, minTime
    """

    print(f"查询：{database}/{table} 日期={day}")
    frame = session.run(sql)

    if frame is None or frame.empty:
        print(f"{day}: 无数据")
        return 0, 0

    if not frame.columns.is_unique:
        raise ValueError("查询结果包含重复列名")

    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"缺少字段：{sorted(missing)}")

    if frame["Code"].isna().any():
        raise ValueError("Code 存在空值")

    actual_dates = pd.to_datetime(frame["date"], errors="raise").dt.date
    if not actual_dates.eq(day).all():
        raise ValueError(f"请求日期={day}，"
                         f"实际日期={frame['date'].unique().tolist()}")

    frame = frame.copy()
    frame["symbol"] = frame["Code"].astype(str).str.strip()

    if kind == "option":
        selected = frame["symbol"].str.fullmatch(OPTION_PATTERN)
        skipped = int((~selected).sum())

        if skipped:
            print(f"{day}: 跳过 {skipped} 条非期权记录")

        frame = frame.loc[selected].copy()

    elif kind == "future":
        selected = frame["symbol"].str.fullmatch(FUTURE_PATTERN)
        skipped = int((~selected).sum())
        if skipped:
            print(f"{day}: 跳过 {skipped} 条非期货记录")
        frame = frame.loc[selected].copy()
        if future_specs is None:
            raise ValueError("期货导出需要fut_basic合约资料")
        names = frame["symbol"].str.split(".", regex=False).str[0].str.upper()
        missing_symbols = sorted(set(names) - set(future_specs))
        if missing_symbols:
            raise ValueError(f"fut_basic缺少期货合约：{missing_symbols}")
        frame["symbol"] = names.map(lambda key: future_specs[key]["symbol"])

    elif kind == "index":
        # 保留字符串前导零；整数形式的指数代码恢复为六位。
        numeric_codes = frame["symbol"].str.fullmatch(r"\d{1,6}")
        frame.loc[numeric_codes, "symbol"] = (frame.loc[numeric_codes,
                                                        "symbol"].str.zfill(6))
    else:
        raise ValueError(f"不支持的数据类型：{kind}")

    if frame.empty:
        print(f"{day}: 无符合条件的数据")
        return 0, 0

    directory = output_dir / day.strftime("%Y%m%d")
    directory.mkdir(parents=True, exist_ok=True)

    file_count = 0
    row_count = 0

    for symbol, group in frame.groupby("symbol", sort=True):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", symbol):
            raise ValueError(f"无效或不安全的文件代码：{symbol!r}")

        path = directory / f"{symbol}_{day:%Y%m%d}.feather"
        if path.exists() and not overwrite:
            raise FileExistsError(f"{path} 已存在，覆盖请加 --overwrite")

        try:
            if kind == "future":
                bars = prepare_future_bars(group, day, source_timezone, first_row_policy,
                                           future_specs[symbol.upper()])
            else:
                bars = prepare_bars(
                    group=group,
                    day=day,
                    source_timezone=source_timezone,
                    first_row_policy=first_row_policy,
                )
        except ValueError as exc:
            raise ValueError(f"{day}/{symbol}: {exc}") from exc

        temporary = path.with_suffix(".feather.partial")
        try:
            bars.to_feather(temporary, compression="zstd")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

        file_count += 1
        row_count += len(bars)

    print(f"{day}: 导出 {file_count} 个文件，{row_count} 条记录")
    return file_count, row_count


def main():
    parser = argparse.ArgumentParser(description="导出期货、期权、指数分钟行情")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--database", default="dfs://min_bar")
    parser.add_argument("--kind", choices=("future", "option", "index"), required=True)
    parser.add_argument("--table", default=None)
    parser.add_argument("--fut-basic", type=Path, help="期货导出使用的合约资料文件")
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-timezone", default="Asia/Shanghai")
    parser.add_argument(
        "--first-row-policy",
        choices=("error", "zero-baseline"),
        default="error",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(args.env_file)

    if args.start_day > args.end_day:
        parser.error("开始日期不能晚于结束日期")

    # 接受 min_bar 或 dfs://min_bar，不重复添加前缀。
    database = args.database.strip()
    if not database.startswith("dfs://"):
        database = f"dfs://{database}"
    if database.count("dfs://") != 1 or database == "dfs://":
        parser.error("数据库路径无效，应为 dfs://min_bar 这样的格式")

    table = args.table or {
        "future": "cffex_1min",
        "option": "cffex_1min",
        "index": "index_1min",
    }[args.kind]

    env_names = ("DDB_HOST", "DDB_PORT", "DDB_USER", "DDB_PASSWORD")
    missing = [name for name in env_names if not os.environ.get(name)]
    if missing:
        parser.error(f"请设置连接环境变量：{', '.join(missing)}")

    future_specs = None
    if args.kind == "future":
        role_value = os.environ.get("FUT_ROLE_DATA_DIR") or os.environ.get("ROLE_DIR")
        root_value = os.environ.get("CTP_DATA_DIR")
        roles = Path(role_value).expanduser() if role_value else Path(root_value).expanduser()/"role" if root_value else None
        basic = args.fut_basic or (roles/"fut_basic.feather" if roles else None)
        if basic is None:
            parser.error("期货导出请设置--fut-basic或FUT_ROLE_DATA_DIR/ROLE_DIR/CTP_DATA_DIR")
        future_specs = load_future_specs(basic)

    import dolphindb as ddb
    session = ddb.session()
    total_files = 0
    total_rows = 0


    try:
        connected = session.connect(
            host=os.environ["DDB_HOST"],
            port=int(os.environ["DDB_PORT"]),
            userid=os.environ["DDB_USER"],
            password=os.environ["DDB_PASSWORD"],
        )
        if not connected:
            raise ConnectionError("DolphinDB 连接失败")

        # 逐自然日查询；非交易日无数据则跳过，不依赖 ultron。
        day = args.start_day
        while day <= args.end_day:
            files, rows = export_day(
                session=session,
                database=database,
                table=table,
                day=day,
                kind=args.kind,
                source_timezone=args.source_timezone,
                first_row_policy=args.first_row_policy,
                output_dir=args.output_dir,
                overwrite=args.overwrite,
                future_specs=future_specs,
            )
            total_files += files
            total_rows += rows
            day += timedelta(days=1)

    finally:
        session.close()

    print(f"完成：{total_files} 个文件，{total_rows} 条记录")


if __name__ == "__main__":
    main()
