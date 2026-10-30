"""将 Tinysoft 五分钟期权行情导出为旧 CTP 文件的 14 列结构。"""

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


OUTPUT_COLUMNS = (
    "symbol", "trade_date", "datetime", "open", "high", "low", "close",
    "volume", "turnover", "volume_accumulate", "turnover_accumulate",
    "open_interest", "prev_close", "prev_settlement",
)

SOURCE_TO_TARGET = {
    "Code": "symbol",
    "TradeDate": "trade_date",
    "ActTime": "datetime",
    "openPrice": "open",
    "highPrice": "high",
    "lowPrice": "low",
    "closePrice": "close",
    "volume": "volume",
    "turnover": "turnover",
    "volumeAccumulate": "volume_accumulate",
    "turnoverAccumulate": "turnover_accumulate",
    "openInterestAccumulate": "open_interest",
}

COUNT_COLUMNS = ("volume", "volume_accumulate")
FLOAT_COLUMNS = (
    "open", "high", "low", "close", "turnover", "turnover_accumulate",
    "open_interest", "prev_close", "prev_settlement",
)
PREVIOUS_COLUMNS = ("prev_close", "prev_settlement")
# 源表同时包含 HO2601-C-2650 等代码和 OP10009633 等代码。
# 两类都保留源 Code，不将 OP 代码改写成其他合约，也不按格式过滤掉。
OPTION_PATTERN = (
    r"(?:[A-Za-z]+\d{3,4}-[CPcp]-\d+(?:\.\d+)?|OP\d+)"
)


def prepare_bars(frame, *, day, source_timezone="Asia/Shanghai"):
    """保留源频率；成交量和成交额直接映射，不差分、不重新累计。

    源数据缺少昨收/昨结算时保留 NaN；若调用前已补入同名字段则保留。
    """
    day = pd.Timestamp(day).date()
    if not frame.columns.is_unique:
        raise ValueError(f"{day}: 查询结果包含重复列名")
    missing = set(SOURCE_TO_TARGET) - set(frame.columns)
    if missing:
        raise ValueError(f"{day}: 缺少源字段：{sorted(missing)}")

    bars = frame.loc[:, list(SOURCE_TO_TARGET)].rename(
        columns=SOURCE_TO_TARGET,
    ).copy()
    if bars["symbol"].isna().any():
        raise ValueError(f"{day}: Code 存在空值")
    bars["symbol"] = bars["symbol"].astype(str).str.strip()
    invalid = ~bars["symbol"].str.fullmatch(OPTION_PATTERN)
    if invalid.any():
        sample = bars.loc[invalid, "symbol"].unique().tolist()[:10]
        raise ValueError(f"{day}: 非预期合约代码：{sample}")

    trade_dates = pd.to_datetime(bars["trade_date"], errors="raise")
    if trade_dates.isna().any() or not trade_dates.dt.date.eq(day).all():
        raise ValueError(f"{day}: TradeDate 存在空值或包含其他交易日")
    bars["trade_date"] = trade_dates.dt.strftime("%Y-%m-%d")

    timestamps = pd.to_datetime(bars["datetime"], errors="raise")
    if timestamps.isna().any():
        raise ValueError(f"{day}: ActTime 存在空值")
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(
            source_timezone, ambiguous="raise", nonexistent="raise",
        )
    # 文件中保留北京时间 +08:00，不保存 UTC 或额外的 timestamp 列。
    bars["datetime"] = timestamps.dt.tz_convert("Asia/Shanghai").astype(
        "datetime64[ns, Asia/Shanghai]",
    )

    for column in PREVIOUS_COLUMNS:
        bars[column] = frame[column] if column in frame else np.nan

    for column in (*COUNT_COLUMNS, *FLOAT_COLUMNS):
        values = pd.to_numeric(bars[column], errors="raise")
        if column in PREVIOUS_COLUMNS:
            # 这两个字段允许缺失，禁止用零或当日价格冒充。
            valid_values = values.dropna()
        else:
            if values.isna().any():
                raise ValueError(f"{day}: {column} 存在空值")
            valid_values = values
        if not np.isfinite(valid_values.to_numpy(dtype=float)).all():
            raise ValueError(f"{day}: {column} 存在非有限值")
        if (valid_values < 0).any():
            raise ValueError(f"{day}: {column} 存在负值")
        if column in COUNT_COLUMNS:
            if (values % 1 != 0).any():
                raise ValueError(f"{day}: {column} 不是整数")
            bars[column] = values.astype("int64")
        else:
            bars[column] = values.astype("float64")

    duplicate = bars.duplicated(["symbol", "datetime"], keep=False)
    if duplicate.any():
        sample = bars.loc[duplicate, ["symbol", "datetime"]].head(10)
        raise ValueError(f"{day}: 存在重复合约时间记录：{sample.to_dict('records')}")

    return bars.loc[:, list(OUTPUT_COLUMNS)].sort_values(
        ["symbol", "datetime"],
    ).reset_index(drop=True)


def fill_previous_close(session, bars, *, day, previous_day=None):
    """用源表上一有数据的交易日末条价格填补缺失昨收。

    这是分钟行情推导的日收尾价，依赖源表前一交易日数据完整。
    同一合约若在该日缺失，不跨到更旧日期取价；不覆盖已有昨收，
    也不使用当日价格或推算昨结算。
    """
    missing = bars["prev_close"].isna()
    if not missing.any():
        return bars

    # 同一合约当天已有真实昨收时，优先补到该合约的其他行。
    bars = bars.copy()
    known = bars.loc[~missing, ["symbol", "prev_close"]].drop_duplicates()
    if known["symbol"].duplicated().any():
        raise ValueError("同一合约当日存在多个不同的 prev_close")
    bars.loc[missing, "prev_close"] = bars.loc[missing, "symbol"].map(
        known.set_index("symbol")["prev_close"],
    )
    missing = bars["prev_close"].isna()
    if not missing.any():
        return bars

    day = pd.Timestamp(day).date()
    if previous_day is None:
        previous = session.run(f"""
            select max(TradeDate) as previous_trade_date
            from loadTable("dfs://JC_MKT_MIN", "opt_5min_patch")
            where TradeDate < {day:%Y.%m.%d}
        """)
        if previous.empty or pd.isna(previous["previous_trade_date"].iloc[0]):
            print(f"{day}: 源表无更早交易日，缺失 prev_close 保留 NaN")
            return bars
        previous_day = previous["previous_trade_date"].iloc[0]
    previous_day = pd.Timestamp(previous_day).date()
    if previous_day >= day:
        raise ValueError(f"{day}: 昨收参考日期必须早于当前交易日")

    # atImax 按最大 ActTime 取对应 closePrice，不依赖数据库存储顺序。
    reference = session.run(f"""
        select atImax(ActTime, closePrice) as prev_close
        from loadTable("dfs://JC_MKT_MIN", "opt_5min_patch")
        where TradeDate = {previous_day:%Y.%m.%d}
        group by Code
    """)
    if reference.empty:
        print(f"{day}: {previous_day} 无昨收参考，缺失 prev_close 保留 NaN")
        return bars
    reference = reference.loc[:, ["Code", "prev_close"]].copy()
    if reference["Code"].isna().any():
        raise ValueError(f"{day}: 昨收参考 Code 存在空值")
    reference["Code"] = reference["Code"].astype(str).str.strip()
    if reference["Code"].duplicated().any():
        raise ValueError(f"{day}: 昨收参考存在重复 Code")
    reference["prev_close"] = pd.to_numeric(reference["prev_close"], errors="raise")
    values = reference["prev_close"].dropna()
    if not np.isfinite(values.to_numpy(dtype=float)).all() or (values < 0).any():
        raise ValueError(f"{day}: 昨收参考存在非有限值或负值")

    filled = bars.copy()
    prices = reference.set_index("Code")["prev_close"]
    filled.loc[missing, "prev_close"] = filled.loc[missing, "symbol"].map(prices)
    filled["prev_close"] = filled["prev_close"].astype("float64")
    unresolved = filled.loc[filled["prev_close"].isna(), "symbol"].nunique()
    print(f"{day}: 使用 {previous_day} 日末价格补昨收，"
          f"{unresolved} 个合约仍缺失 prev_close")
    return filled


def _fill_daily_reference(bars, reference, *, reference_day, columns):
    """按 Code 映射日行情参考值，保持分钟记录数量和已有值。"""
    if reference.empty:
        return bars
    required = {"Code", "TradeDate", *columns}
    if not reference.columns.is_unique or not required.issubset(reference.columns):
        raise ValueError(f"{reference_day}: 日行情参考存在重复列或缺少字段："
                         f"{sorted(required - set(reference.columns))}")
    reference = reference.loc[:, ["Code", "TradeDate", *columns]].copy()
    if reference["Code"].isna().any():
        raise ValueError(f"{reference_day}: 日行情参考 Code 存在空值")
    reference["Code"] = reference["Code"].astype(str).str.strip()
    if reference["Code"].duplicated().any():
        raise ValueError(f"{reference_day}: 日行情参考存在重复 Code")
    dates = pd.to_datetime(reference["TradeDate"], errors="raise")
    if dates.isna().any() or not dates.dt.date.eq(reference_day).all():
        raise ValueError(f"{reference_day}: 日行情参考包含错误 TradeDate")
    for column in columns:
        reference[column] = pd.to_numeric(reference[column], errors="raise")
        valid = reference[column].dropna()
        if not np.isfinite(valid.to_numpy(dtype=float)).all() or (valid < 0).any():
            raise ValueError(f"{reference_day}: 日行情参考 {column} 存在非法值")
        missing = bars[column].isna()
        if missing.any():
            lookup = reference.set_index("Code")[column]
            bars.loc[missing, column] = bars.loc[missing, "symbol"].map(lookup).astype(
                "float64",
            )
        bars[column] = bars[column].astype("float64")
    return bars


def fill_previous_prices(session, bars, *, day):
    """优先使用 opt_eod_patch 补昨收和昨结算，仅昨收可回退到分钟行情。

    D 日 preSettlePrice -> 昨结算；上一有数据交易日 closePrice -> 昨收，
    settlePrice -> 缺失的昨结算。不读取 D 日 closePrice / settlePrice。
    """
    day = pd.Timestamp(day).date()
    filled = bars.copy()
    for column in PREVIOUS_COLUMNS:
        known = filled.loc[filled[column].notna(), ["symbol", column]].drop_duplicates()
        if known["symbol"].duplicated().any():
            raise ValueError(f"{day}: 同一合约当日存在多个不同的 {column}")
        filled[column] = filled[column].fillna(
            filled["symbol"].map(known.set_index("symbol")[column]),
        ).astype("float64")
    if not filled[list(PREVIOUS_COLUMNS)].isna().any().any():
        return filled

    if filled["prev_settlement"].isna().any():
        current = session.run(f"""
            select Code, TradeDate, preSettlePrice as prev_settlement
            from loadTable("dfs://JC_MKT_MIN", "opt_eod_patch")
            where TradeDate = {day:%Y.%m.%d}
        """)
        filled = _fill_daily_reference(
            filled, current, reference_day=day, columns=("prev_settlement",),
        )

    previous_day = None
    if filled[list(PREVIOUS_COLUMNS)].isna().any().any():
        previous = session.run(f"""
            select max(TradeDate) as previous_trade_date
            from loadTable("dfs://JC_MKT_MIN", "opt_eod_patch")
            where TradeDate < {day:%Y.%m.%d}
        """)
        if not previous.empty and pd.notna(previous["previous_trade_date"].iloc[0]):
            previous_day = pd.Timestamp(previous["previous_trade_date"].iloc[0]).date()
            if previous_day >= day:
                raise ValueError(f"{day}: 日行情参考日期必须早于当前交易日")
            reference = session.run(f"""
                select Code, TradeDate, closePrice as prev_close,
                       settlePrice as prev_settlement
                from loadTable("dfs://JC_MKT_MIN", "opt_eod_patch")
                where TradeDate = {previous_day:%Y.%m.%d}
            """)
            filled = _fill_daily_reference(
                filled, reference, reference_day=previous_day, columns=PREVIOUS_COLUMNS,
            )

    # 已知日行情上一交易日时，分钟后备查询也限定为同一天，避免更旧价格。
    filled = fill_previous_close(session, filled, day=day, previous_day=previous_day)
    missing = {column: filled.loc[filled[column].isna(), "symbol"].nunique()
               for column in PREVIOUS_COLUMNS}
    print(f"{day}: 日行情补全后仍缺失的合约数：{missing}")
    return filled


def export_day(session, *, day, output_dir, overwrite=False,
               source_timezone="Asia/Shanghai"):
    day = pd.Timestamp(day).date()
    sql = f"""
        select *
        from loadTable("dfs://JC_MKT_MIN", "opt_5min_patch")
        where TradeDate = {day:%Y.%m.%d}
        order by Code, ActTime
    """
    frame = session.run(sql)
    if frame.empty:
        print(f"{day}: 无数据，跳过")
        return 0, 0

    frame = prepare_bars(frame, day=day, source_timezone=source_timezone)
    frame = fill_previous_prices(session, frame, day=day)
    day_dir = Path(output_dir).expanduser() / day.strftime("%Y%m%d")

    # 先检查当天所有目标文件，避免导出到一半才发现已有文件。
    if not overwrite:
        for symbol in frame["symbol"].unique():
            path = day_dir / f"{symbol}_{day:%Y%m%d}.feather"
            if path.exists():
                raise FileExistsError(f"文件已存在：{path}；需要覆盖时传入 --overwrite")
    day_dir.mkdir(parents=True, exist_ok=True)

    files = rows = 0
    for symbol, group in frame.groupby("symbol", sort=True):
        bars = group.reset_index(drop=True)
        path = day_dir / f"{symbol}_{day:%Y%m%d}.feather"
        temporary = path.with_suffix(".feather.partial")
        try:
            bars.to_feather(temporary, compression="zstd")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        files += 1
        rows += len(bars)

    print(f"{day}: 导出 {files} 个合约文件，{rows} 条记录")
    return files, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-day", type=date.fromisoformat, default=date(2026, 1, 5))
    parser.add_argument("--end-day", type=date.fromisoformat, default=date(2026, 4, 29))
    parser.add_argument("--output-dir", type=Path, default=Path("./temp"))
    parser.add_argument("--source-timezone", default="Asia/Shanghai")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.start_day > args.end_day:
        parser.error("开始日期不能晚于结束日期")
    if not os.environ.get("DDB_PASSWORD"):
        parser.error("请先设置环境变量 DDB_PASSWORD")

    import dolphindb as ddb

    session = ddb.session()
    total_files = total_rows = 0
    try:
        connected = session.connect(
            host=os.environ.get("DDB_HOST", "10.168.51.20"),
            port=int(os.environ.get("DDB_PORT", "8912")),
            userid=os.environ.get("DDB_USER", "laok"),
            password=os.environ["DDB_PASSWORD"],
        )
        if not connected:
            raise ConnectionError("DolphinDB 连接失败")

        print("保留五分钟频率；prev_close / prev_settlement 优先从 opt_eod_patch 补充；"
              "昨收可回退到前一交易日五分钟行情，仍缺失的值保留 NaN")
        # 按自然日查询，非交易日无数据则跳过，无需 ultron 日历。
        day = args.start_day
        while day <= args.end_day:
            files, rows = export_day(
                session, day=day, output_dir=args.output_dir,
                overwrite=args.overwrite, source_timezone=args.source_timezone,
            )
            total_files += files
            total_rows += rows
            day += timedelta(days=1)
    finally:
        session.close()
    print(f"完成：{total_files} 个文件，{total_rows} 条记录")


if __name__ == "__main__":
    main()
