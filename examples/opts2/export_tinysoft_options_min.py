"""将 Tinysoft 五分钟期权行情导出为旧 CTP 文件的 14 列结构。"""

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv


MINUTE_TABLE = "opt_5min_tiny"
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


def validate_bar_prices(bars, *, policy="reject", issues=None):
    """检查回测所需的正价格及 OHLC 关系；零成交量不构成价格异常。"""
    if policy not in ("reject", "warn"):
        raise ValueError("ohlc_policy 应为 reject 或 warn")
    high_bad = bars["high"] < bars[["open", "close"]].max(axis=1)
    low_bad = bars["low"] > bars[["open", "close"]].min(axis=1)
    inverted = bars["high"] < bars["low"]
    zero = bars[["open", "high", "low", "close"]].eq(0).any(axis=1)
    invalid = high_bad | low_bad | inverted | zero
    found = []
    for position in np.flatnonzero(invalid.to_numpy()):
        row = bars.iloc[position]
        reasons = []
        for mask, reason in ((high_bad, "HIGH_BELOW_OPEN_OR_CLOSE"),
                             (low_bad, "LOW_ABOVE_OPEN_OR_CLOSE"),
                             (inverted, "HIGH_BELOW_LOW"), (zero, "ZERO_PRICE")):
            if mask.iloc[position]:
                reasons.append(reason)
        found.append(dict(
            trade_date=row["trade_date"], Code=row["symbol"], datetime=row["datetime"],
            source_row=int(position) + 1, issue=";".join(reasons), policy=policy,
            action="reject_contract" if policy == "reject" else "warn_preserve",
            **{name: row[name] for name in ("open", "high", "low", "close", "volume",
                                           "turnover", "volume_accumulate", "turnover_accumulate")},
        ))
    if not found:
        return
    if issues is not None:
        issues.extend(found)
    first = found[0]
    detail = (f"价格/OHLC 检查失败 {len(found)} 行；首条 {first['Code']} "
              f"{first['datetime']}：open={first['open']}, high={first['high']}, "
              f"low={first['low']}, close={first['close']}, volume={first['volume']}；"
              f"{first['issue']}")
    if policy == "reject":
        raise ValueError(detail)
    if issues is None:
        print(f"警告并保留原值：{detail}")


def prepare_bars(frame, *, day, source_timezone="Asia/Shanghai",
                 ohlc_policy="reject", validation_issues=None):
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

    validate_bar_prices(bars, policy=ohlc_policy, issues=validation_issues)
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
            from loadTable("dfs://JC_MKT_MIN", "{MINUTE_TABLE}")
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
        from loadTable("dfs://JC_MKT_MIN", "{MINUTE_TABLE}")
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
               source_timezone="Asia/Shanghai", ohlc_policy="reject"):
    day = pd.Timestamp(day).date()
    if ohlc_policy not in ("reject", "warn"):
        raise ValueError("ohlc_policy 应为 reject 或 warn")
    sql = f"""
        select *
        from loadTable("dfs://JC_MKT_MIN", "{MINUTE_TABLE}")
        where TradeDate = {day:%Y.%m.%d}
        order by Code, ActTime
    """
    frame = session.run(sql)
    if frame.empty:
        print(f"{day}: 无数据，跳过")
        return 0, 0

    day_dir = Path(output_dir).expanduser() / day.strftime("%Y%m%d")
    if not frame.columns.is_unique or "Code" not in frame:
        raise ValueError(f"{day}: 查询结果包含重复列名或缺少 Code")
    frame = frame.copy()
    frame["Code"] = frame["Code"].astype("string").str.strip()
    errors = []
    existing = 0
    prepared = []
    validation_issues = []

    def record_error(symbol, stage, exc):
        errors.append(dict(trade_date=str(day), Code=str(symbol), stage=stage,
                           error_type=type(exc).__name__, error=str(exc)))
        print(f"{day}/{symbol}: 跳过 [{stage}] {type(exc).__name__}: {exc}")

    for symbol, group in frame.groupby("Code", sort=True, dropna=False):
        try:
            bars = prepare_bars(group, day=day, source_timezone=source_timezone,
                                ohlc_policy=ohlc_policy, validation_issues=validation_issues)
        except Exception as exc:
            record_error(symbol, "conversion", exc)
            continue
        path = day_dir / f"{symbol}_{day:%Y%m%d}.feather"
        if path.exists() and not overwrite:
            existing += 1
            print(f"{day}/{symbol}: 文件已存在，跳过；覆盖请传入 --overwrite")
            continue
        prepared.append(bars)

    # 在补充日行情之前落盘，后续数据库查询失败时也能保留价格检查结果。
    day_dir.mkdir(parents=True, exist_ok=True)
    validation_report = day_dir / "bar_validation.csv"
    if validation_issues:
        pd.DataFrame(validation_issues).to_csv(validation_report, index=False, encoding="utf-8-sig")
        print(f"{day}: {len(validation_issues)} 条价格/OHLC 问题行已保存至 {validation_report}")
    else:
        validation_report.unlink(missing_ok=True)

    # 合并有效合约后统一补昨值，避免每个期权重复查询日行情。
    if prepared:
        frame = pd.concat(prepared, ignore_index=True)
        frame = fill_previous_prices(session, frame, day=day)
    else:
        frame = pd.DataFrame(columns=OUTPUT_COLUMNS)
    day_dir.mkdir(parents=True, exist_ok=True)

    files = rows = 0
    written_symbols = set()
    for symbol, group in frame.groupby("symbol", sort=True):
        bars = group.reset_index(drop=True)
        path = day_dir / f"{symbol}_{day:%Y%m%d}.feather"
        temporary = path.with_suffix(".feather.partial")
        try:
            try:
                bars.to_feather(temporary, compression="zstd")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception as exc:
            record_error(symbol, "write", exc)
            continue
        files += 1
        rows += len(bars)
        written_symbols.add(symbol)

    if validation_issues:
        for issue in validation_issues:
            issue["exported_this_run"] = issue["Code"] in written_symbols
            issue["existing_file_kept"] = (
                day_dir / f"{issue['Code']}_{day:%Y%m%d}.feather"
            ).exists() and issue["Code"] not in written_symbols
        pd.DataFrame(validation_issues).to_csv(validation_report, index=False, encoding="utf-8-sig")

    report = day_dir / "export_errors.csv"
    if errors:
        pd.DataFrame(errors).to_csv(report, index=False, encoding="utf-8-sig")
        print(f"{day}: 跳过明细已保存至 {report}")
    else:
        report.unlink(missing_ok=True)
    print(f"{day}: 导出 {files} 个合约文件，{rows} 条记录；"
          f"异常跳过 {len(errors)} 个合约，已有文件跳过 {existing} 个；"
          f"价格/OHLC 问题 {len(validation_issues)} 行")
    return files, rows


def connect_session(session):
    """优先读取 DDB_REFERENCE_*，兼容旧 DDB_* 配置。"""
    host = os.environ.get("DDB_REFERENCE_HOST") or os.environ.get("DDB_HOST", "10.168.51.20")
    port = int(os.environ.get("DDB_REFERENCE_PORT") or os.environ.get("DDB_PORT", "8912"))
    userid = os.environ.get("DDB_REFERENCE_USERNAME") or os.environ.get("DDB_USER", "laok")
    password = os.environ.get("DDB_REFERENCE_PASSWORD") or os.environ.get("DDB_PASSWORD")
    if not password:
        raise ValueError("请设置 DDB_REFERENCE_PASSWORD 或 DDB_PASSWORD")
    connected = session.connect(host=host, port=port, userid=userid, password=password)
    if not connected:
        raise ConnectionError(f"DolphinDB 连接失败：{host}:{port}，用户={userid}")


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-day", type=date.fromisoformat, default=date(2026, 1, 5))
    parser.add_argument("--end-day", type=date.fromisoformat, default=date(2026, 4, 29))
    parser.add_argument("--output-dir", type=Path, default=Path("./temp"))
    parser.add_argument("--source-timezone", default="Asia/Shanghai")
    parser.add_argument("--ohlc-policy", choices=("reject", "warn"), default="reject",
                        help="reject 拒绝存在价格/OHLC 异常的合约当日文件；warn 保留原值并记录")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.start_day > args.end_day:
        parser.error("开始日期不能晚于结束日期")
    if not (os.environ.get("DDB_REFERENCE_PASSWORD") or os.environ.get("DDB_PASSWORD")):
        parser.error("请设置环境变量 DDB_REFERENCE_PASSWORD 或 DDB_PASSWORD")

    import dolphindb as ddb

    session = ddb.session()
    total_files = total_rows = 0
    try:
        connect_session(session)

        print(f"分钟源表={MINUTE_TABLE}；OHLC 策略={args.ohlc_policy}；"
              "保留五分钟频率；prev_close / prev_settlement 优先从 opt_eod_patch 补充；"
              "昨收可回退到前一交易日五分钟行情，仍缺失的值保留 NaN")
        # 按自然日查询，非交易日无数据则跳过，无需 ultron 日历。
        day = args.start_day
        while day <= args.end_day:
            files, rows = export_day(
                session, day=day, output_dir=args.output_dir,
                overwrite=args.overwrite, source_timezone=args.source_timezone,
                ohlc_policy=args.ohlc_policy,
            )
            total_files += files
            total_rows += rows
            day += timedelta(days=1)
    finally:
        session.close()
    print(f"完成：{total_files} 个文件，{total_rows} 条记录")


if __name__ == "__main__":
    main()
