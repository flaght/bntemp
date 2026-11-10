"""按交易日导出期货日线，每日一个 YYYY-MM-DD.feather，固定 14 列。"""

import argparse
import os
import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

OUTPUT_COLUMNS = (
    "trade_date", "code", "secShortName", "contractObject", "preClosePrice",
    "preSettlePrice", "openPrice", "highestPrice", "lowestPrice", "closePrice",
    "settlePrice", "turnoverVol", "turnoverValue", "openInt",
)
FLOAT_COLUMNS = (
    "preClosePrice", "preSettlePrice", "openPrice", "highestPrice", "lowestPrice",
    "closePrice", "settlePrice", "turnoverValue", "openInt",
)
EXCHANGES = {
    "上海期货交易所": "XSGE", "SHFE": "XSGE", "XSGE": "XSGE",
    "大连商品交易所": "XDCE", "DCE": "XDCE", "XDCE": "XDCE",
    "郑州商品交易所": "XZCE", "CZCE": "XZCE", "XZCE": "XZCE",
    "上海国际能源交易中心": "XSIE", "INE": "XSIE", "XSIE": "XSIE",
    "广州期货交易所": "XGFE", "GFEX": "XGFE", "XGFE": "XGFE",
    "中国金融期货交易所": "CCFX", "CFFEX": "CCFX", "CCFX": "CCFX", "XCFX": "CCFX",
}


def build_daily_spec(info, code, *, day, metadata_fallback="product"):
    """日线仅需交易所确定代码大小写，不使用任一来源的 multiplier。"""
    if metadata_fallback not in ("product", "error"):
        raise ValueError("metadata_fallback 应为 product 或 error")
    required = {"Code", "TradeDate", "exchange"}
    if not info.columns.is_unique or not required.issubset(info.columns):
        raise ValueError("合约资料存在重复列或缺少 Code、TradeDate、exchange")
    match = re.fullmatch(r"([A-Z]+)(\d{3,4})([A-Z]*)", code)
    if match is None:
        raise ValueError(f"非预期期货代码：{code}")
    product, month, suffix = match.groups()
    reference = info.copy()
    reference["Code"] = reference["Code"].astype(str).str.strip().str.upper()
    reference["TradeDate"] = pd.to_datetime(reference["TradeDate"], errors="raise")
    if reference["TradeDate"].isna().any() or (reference["TradeDate"].dt.date > day).any():
        raise ValueError("合约资料存在空日期或导出日之后的快照")
    exact = reference.loc[reference["Code"].eq(code)]
    if len(exact) > 1:
        raise ValueError(f"{code}: 合约资料存在重复 Code")
    if exact.empty:
        if metadata_fallback == "error":
            raise ValueError(f"{code}: 缺少精确合约资料")
        family = re.escape(product) + r"\d{3,4}" + re.escape(suffix)
        selected = reference.loc[reference["Code"].str.fullmatch(family)]
        if selected.empty:
            raise ValueError(f"{code}: 合约及同品种历史资料均缺失")
        selected = selected.loc[selected["TradeDate"].eq(selected["TradeDate"].max())]
    else:
        selected = exact
    venues = selected["exchange"].astype(str).str.strip().str.upper().map(EXCHANGES)
    if venues.isna().any() or venues.nunique() != 1:
        raise ValueError(f"{code}: 交易所未知或后备资料交易所不一致")
    exchange = venues.iloc[0]
    if exchange == "XZCE":
        symbol = product + month[-3:] + suffix
    elif exchange == "CCFX":
        symbol = product + month + suffix
    else:
        symbol = product.lower() + month + suffix
    if exact.empty:
        print(f"{day}/{code}: 使用同品种资料映射交易所 {exchange}")
    return dict(symbol=symbol, exchange=exchange)


def first_present(*values):
    return next((value for value in values if pd.notna(value)), np.nan)


def prepare_daily_row(row, *, day, spec, daily=None, previous=None, short_name="",
                      ohlc_policy="strict", ohlc_warnings=None):
    """保留源日汇总的原始价格与量额；源 multiplier 不参与计算。"""
    if ohlc_policy not in ("strict", "warn"):
        raise ValueError("ohlc_policy 应为 strict 或 warn")
    day = pd.Timestamp(day).date()
    if pd.isna(row["TradeDate"]) or pd.Timestamp(row["TradeDate"]).date() != day:
        raise ValueError("源 TradeDate 为空或与导出日不符")
    code = str(row["Code"]).strip().upper()
    for reference, current in ((daily, True), (previous, False)):
        if reference is None:
            continue
        if str(reference["Code"]).strip().upper() != code:
            raise ValueError("日行情参考合约与源 Code 不一致")
        reference_day = reference["TradeDate"]
        if pd.isna(reference_day):
            raise ValueError("日行情参考 TradeDate 为空")
        reference_day = pd.Timestamp(reference_day).date()
        if (current and reference_day != day) or (not current and reference_day >= day):
            raise ValueError("日行情参考日期不符合当日/前一交易日关系")
    daily = {} if daily is None else daily
    previous = {} if previous is None else previous
    product = row["productID"]
    if pd.isna(product) or not str(product).strip():
        raise ValueError("productID 为空")
    name = row.get("secShortName", "")
    if pd.isna(name) or not str(name).strip():
        name = short_name
    result = {
        "trade_date": pd.Timestamp(day), "code": spec["symbol"],
        "secShortName": "" if pd.isna(name) else str(name).strip(),
        "contractObject": str(product).strip().upper(),
        "preClosePrice": first_present(row.get("preClosePrice"), previous.get("closePrice")),
        "preSettlePrice": first_present(row.get("preSettlePrice"), daily.get("preSettlePrice"),
                                         previous.get("settlePrice")),
        "openPrice": row["openPrice"], "highestPrice": row["highPrice"],
        "lowestPrice": row["lowPrice"], "closePrice": row["closePrice"],
        "settlePrice": first_present(row.get("settlePrice"), daily.get("settlePrice")),
        "turnoverVol": row["volume"], "turnoverValue": row["turnover"],
        "openInt": row["openInterestAccumulate"],
    }
    nullable = {"preClosePrice", "preSettlePrice", "settlePrice"}
    for column in (*FLOAT_COLUMNS, "turnoverVol"):
        value = result[column]
        if pd.isna(value) and column in nullable:
            result[column] = np.nan
            continue
        value = float(value)
        if not np.isfinite(value) or value < 0:
            raise ValueError(f"{column} 存在空值、非有限值或负值")
        if column == "turnoverVol":
            if not value.is_integer():
                raise ValueError("日成交量不是整数")
            result[column] = int(value)
        else:
            result[column] = value
    issues = []
    if result["highestPrice"] < max(result["openPrice"], result["closePrice"]):
        issues.append("highPrice 小于 openPrice 或 closePrice")
    if result["lowestPrice"] > min(result["openPrice"], result["closePrice"]):
        issues.append("lowPrice 大于 openPrice 或 closePrice")
    if result["highestPrice"] < result["lowestPrice"]:
        issues.append("highPrice 小于 lowPrice")
    if issues:
        detail = (f"日线 OHLC 高低价关系异常：{'；'.join(issues)}；"
                  f"open={result['openPrice']}, high={result['highestPrice']}, "
                  f"low={result['lowestPrice']}, close={result['closePrice']}, "
                  f"volume={result['turnoverVol']}")
        if ohlc_policy == "strict":
            raise ValueError(detail)
        warning = dict(trade_date=str(day), Code=code, code=result["code"],
                       reason="；".join(issues), openPrice=result["openPrice"],
                       highPrice=result["highestPrice"], lowPrice=result["lowestPrice"],
                       closePrice=result["closePrice"], volume=result["turnoverVol"],
                       turnover=result["turnoverValue"])
        if ohlc_warnings is not None:
            ohlc_warnings.append(warning)
        else:
            print(f"{day}/{code}: 警告并保留原值：{detail}")
    return result


def reference_for(frame, code):
    if frame.empty:
        return None
    selected = frame.loc[frame["Code"].astype(str).str.strip().str.upper().eq(code)]
    if len(selected) > 1:
        raise ValueError(f"日行情参考存在重复 Code：{code}")
    return None if selected.empty else selected.iloc[0]


def load_names(path):
    if path is None:
        return {}
    names = pd.read_feather(path)
    if not {"code", "secShortName"}.issubset(names.columns):
        raise ValueError("简称参考 Feather 必须包含 code、secShortName")
    names = names.loc[names["secShortName"].notna(), ["code", "secShortName"]].copy()
    names["code"] = names["code"].astype(str).str.strip().str.upper()
    names = names.drop_duplicates()
    if names["code"].duplicated().any():
        raise ValueError("简称参考中同一 code 存在不同 secShortName")
    return names.set_index("code")["secShortName"].to_dict()


def export_day(session, *, day, source_table="fut_eod_patch", output_dir, algo_id=None,
               overwrite=False, names=None, metadata_fallback="product", ohlc_policy="strict"):
    day = pd.Timestamp(day).date()
    if ohlc_policy not in ("strict", "warn"):
        raise ValueError("ohlc_policy 应为 strict 或 warn")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source_table):
        raise ValueError("源表名只能包含字母、数字和下划线")
    if source_table == "fut_eod_patch" and algo_id is not None:
        raise ValueError("fut_eod_patch 没有 algoID；导出全部合约时不要传 --algo-id")
    directory = Path(output_dir).expanduser()
    path = directory / f"{day:%Y-%m-%d}.feather"
    if path.exists() and not overwrite:
        print(f"{day}: 文件已存在，跳过；覆盖请加 --overwrite")
        return 0, 0
    algorithm = "" if algo_id is None else f" and algoID = {int(algo_id)}"
    source = session.run(f'''
        select * from loadTable("dfs://JC_MKT_MIN", "{source_table}")
        where TradeDate = {day:%Y.%m.%d}{algorithm} order by Code
    ''')
    if source.empty:
        print(f"{day}: 无数据，跳过")
        return 0, 0
    required = {"Code", "TradeDate", "productID", "openPrice", "highPrice", "lowPrice",
                "closePrice", "volume", "turnover", "openInterestAccumulate"}
    if not source.columns.is_unique or not required.issubset(source.columns):
        raise ValueError(f"源表重复列或缺少字段：{sorted(required - set(source.columns))}")
    source = source.copy()
    source["Code"] = source["Code"].astype("string").str.strip().str.upper()
    if algo_id is not None and ("algoID" not in source or not source["algoID"].eq(algo_id).all()):
        raise ValueError("查询结果 algoID 与指定值不一致")
    if source_table == "fut_eod_patch":
        # 主源本身已有昨结算、今结算，不重复查询当天同一张表。
        columns = [name for name in ("Code", "TradeDate", "preSettlePrice", "settlePrice")
                   if name in source.columns]
        current = source.loc[:, columns].drop_duplicates()
    else:
        current = session.run(f'''
            select Code, TradeDate, preSettlePrice, settlePrice
            from loadTable("dfs://JC_MKT_MIN", "fut_eod_patch")
            where TradeDate = {day:%Y.%m.%d}
        ''')
    previous_day = session.run(f'''
        select max(TradeDate) as previous_trade_date
        from loadTable("dfs://JC_MKT_MIN", "fut_eod_patch")
        where TradeDate < {day:%Y.%m.%d}
    ''')
    previous = pd.DataFrame()
    if not previous_day.empty and pd.notna(previous_day["previous_trade_date"].iloc[0]):
        previous_date = pd.Timestamp(previous_day["previous_trade_date"].iloc[0]).date()
        if previous_date >= day:
            raise ValueError("昨日日行情参考日期必须早于导出日")
        previous = session.run(f'''
            select Code, TradeDate, closePrice, settlePrice
            from loadTable("dfs://JC_MKT_MIN", "fut_eod_patch")
            where TradeDate = {previous_date:%Y.%m.%d}
        ''')
    info = session.run(f'''
        select max(TradeDate) as TradeDate, atImax(TradeDate, exchange) as exchange
        from loadTable("dfs://JC_MKT_MIN", "fut_instrument_base_info")
        where TradeDate <= {day:%Y.%m.%d} group by Code
    ''')
    records, errors, ohlc_warnings = [], [], []
    for code, group in source.groupby("Code", sort=True, dropna=False):
        try:
            if pd.isna(code):
                raise ValueError("Code 为空")
            if len(group) != 1:
                # 不同算法可以选中同一合约；仅算法标签或调整系数不同不重复写行情。
                relevant = [name for name in group.columns if name in required or name in
                            ("secShortName", "preClosePrice", "preSettlePrice", "settlePrice")]
                unique = group.loc[:, relevant].drop_duplicates()
                if len(unique) != 1:
                    raise ValueError("同一交易日同一 Code 的多条行情值不一致；"
                                     "请检查 algoID 或用 --algo-id 明确选择算法")
            spec = build_daily_spec(info, code, day=day, metadata_fallback=metadata_fallback)
            record = prepare_daily_row(
                group.iloc[0], day=day, spec=spec, daily=reference_for(current, code),
                previous=reference_for(previous, code),
                short_name=(names or {}).get(spec["symbol"].upper(), ""),
                ohlc_policy=ohlc_policy, ohlc_warnings=ohlc_warnings,
            )
            records.append(record)
        except Exception as exc:
            error = dict(trade_date=str(day), Code=str(code),
                         error_type=type(exc).__name__, error=str(exc))
            error.update({name: group.iloc[0][name] for name in
                          ("openPrice", "highPrice", "lowPrice", "closePrice", "volume", "turnover")})
            errors.append(error)
            print(f"{day}/{code}: 跳过 {type(exc).__name__}: {exc}")
    # 同一天不能把多个源代码别名写成重复的目标 code。
    duplicate_codes = pd.Series([row["code"] for row in records], dtype="str")
    duplicates = set(duplicate_codes.loc[duplicate_codes.duplicated(keep=False)])
    for row in records:
        if row["code"] in duplicates:
            errors.append(dict(trade_date=str(day), Code=row["code"],
                               error_type="ValueError", error="多个源 Code 映射到相同目标 code"))
    records = [row for row in records if row["code"] not in duplicates]
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / f"{day:%Y-%m-%d}.errors.csv"
    if errors:
        pd.DataFrame(errors).to_csv(report, index=False, encoding="utf-8-sig")
    else:
        report.unlink(missing_ok=True)
    warning_report = directory / f"{day:%Y-%m-%d}.ohlc_warnings.csv"
    if ohlc_warnings:
        exported = {record["code"] for record in records}
        for warning in ohlc_warnings:
            warning["exported"] = warning["code"] in exported
        pd.DataFrame(ohlc_warnings).to_csv(warning_report, index=False, encoding="utf-8-sig")
        print(f"{day}: OHLC 原始值及警告已保存至 {warning_report}")
    else:
        warning_report.unlink(missing_ok=True)
    if not records:
        print(f"{day}: 无有效日线记录，异常跳过 {len(errors)} 条，未写行情文件")
        return 0, 0
    bars = pd.DataFrame(records, columns=OUTPUT_COLUMNS).sort_values("code").reset_index(drop=True)
    bars["trade_date"] = bars["trade_date"].astype("datetime64[ns]")
    bars["turnoverVol"] = bars["turnoverVol"].astype("int64")
    bars[list(FLOAT_COLUMNS)] = bars[list(FLOAT_COLUMNS)].astype("float64")
    temporary = path.with_suffix(".feather.partial")
    try:
        bars.to_feather(temporary, compression="zstd")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    missing = {name: int(bars[name].isna().sum()) for name in
               ("preClosePrice", "preSettlePrice", "settlePrice")}
    print(f"{day}: 导出 {path}，{len(bars)} 条日线，异常跳过 {len(errors)} 条；"
          f"OHLC 警告 {len(ohlc_warnings)} 条；"
          f"价格缺失 {missing}，简称空值 {int(bars.secShortName.eq('').sum())}")
    return 1, len(bars)


def connect_session(session):
    host = os.environ.get("DDB_REFERENCE_HOST") or os.environ.get("DDB_HOST", "10.168.51.20")
    port = int(os.environ.get("DDB_REFERENCE_PORT") or os.environ.get("DDB_PORT", "8912"))
    userid = os.environ.get("DDB_REFERENCE_USERNAME") or os.environ.get("DDB_USER", "laok")
    password = os.environ.get("DDB_REFERENCE_PASSWORD") or os.environ.get("DDB_PASSWORD")
    if not password:
        raise ValueError("请设置 DDB_REFERENCE_PASSWORD 或 DDB_PASSWORD")
    if not session.connect(host=host, port=port, userid=userid, password=password):
        raise ConnectionError(f"DolphinDB 连接失败：{host}:{port}，用户={userid}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-table", default="fut_eod_patch",
                        help="默认 fut_eod_patch，按日导出全部期货合约")
    parser.add_argument("--algo-id", type=int, help="源表有 algoID 时按该算法筛选")
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--names-file", type=Path, help="含 code、secShortName 的旧日线 Feather")
    parser.add_argument("--env-file", help="指定 .env；省略时从脚本目录向上查找")
    parser.add_argument("--metadata-fallback", choices=("product", "error"), default="product")
    parser.add_argument("--ohlc-policy", choices=("strict", "warn"), default="strict",
                        help="strict 跳过 OHLC 关系异常记录；warn 保留原值并写警告 CSV")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env_file)
    if args.start_day > args.end_day:
        parser.error("开始日期不能晚于结束日期")
    if args.source_table == "fut_eod_patch" and args.algo_id is not None:
        parser.error("fut_eod_patch 没有 algoID，请移除 --algo-id")
    print(f"日线源表：{args.source_table}；每日一个文件，成交量/额直接保留日汇总；"
          f"OHLC 策略={args.ohlc_policy}")
    names = load_names(args.names_file)
    import dolphindb as ddb
    session = ddb.session()
    files = rows = 0
    try:
        connect_session(session)
        day = args.start_day
        while day <= args.end_day:
            count, size = export_day(
                session, day=day, source_table=args.source_table, algo_id=args.algo_id,
                output_dir=args.output_dir, names=names, overwrite=args.overwrite,
                metadata_fallback=args.metadata_fallback,
                ohlc_policy=args.ohlc_policy,
            )
            files += count
            rows += size
            day += timedelta(days=1)
    finally:
        session.close()
    print(f"完成：{files} 个文件，{rows} 条日线")


if __name__ == "__main__":
    main()
