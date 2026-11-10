"""导出 opt_eod_patch 期权日线，固定 14 列，每日一个 YYYY-MM-DD.feather。"""

import argparse
import os
import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv


OUTPUT_COLUMNS = (
    "trade_date", "code", "secShortName", "exchangeCD", "preSettlePrice",
    "preClosePrice", "openPrice", "highestPrice", "lowestPrice", "closePrice",
    "settlePrice", "turnoverVol", "turnoverValue", "openInt",
)
FLOAT_COLUMNS = OUTPUT_COLUMNS[4:-1]
INDEX_PATTERN = r"(HO|IO|MO)(\d{4})-([CP])-(\d+(?:\.\d+)?)"
EXCHANGES = {
    "CCFX": "CCFX", "CFFEX": "CCFX", "XCFX": "CCFX",
    "XSHG": "XSHG", "SSE": "XSHG", "SH": "XSHG",
    "XSHE": "XSHE", "SZSE": "XSHE", "SZ": "XSHE",
    "XSGE": "XSGE", "SHFE": "XSGE", "XDCE": "XDCE", "DCE": "XDCE",
    "XZCE": "XZCE", "CZCE": "XZCE", "XSIE": "XSIE", "INE": "XSIE",
    "XGFE": "XGFE", "GFEX": "XGFE",
}


def first_present(*values):
    return next((value for value in values if pd.notna(value)), np.nan)


def infer_exchange(row, reference=None):
    code = str(row["Code"]).strip().upper()
    inferred = None
    if re.fullmatch(INDEX_PATTERN, code):
        # 股指期权的标的指数也带 SH 前缀，但期权本身在中金所。
        inferred = "CCFX"
    elif re.fullmatch(r"OP\d+", code):
        underlyer = str(row.get("underlyerID", "")).strip().upper()
        if re.fullmatch(r"SH\d{6}", underlyer):
            inferred = "XSHG"
        elif re.fullmatch(r"SZ\d{6}", underlyer):
            inferred = "XSHE"
    supplied = []
    for value in (row.get("exchangeCD"), (reference or {}).get("exchangeCD")):
        if pd.isna(value) or not str(value).strip():
            continue
        venue = EXCHANGES.get(str(value).strip().upper())
        if venue is None:
            raise ValueError(f"未知 exchangeCD：{value}")
        supplied.append(venue)
    venues = set(supplied + ([inferred] if inferred else []))
    if len(venues) != 1:
        raise ValueError("无法唯一确定交易所；请检查 Code、underlyerID 或简称参考中的 exchangeCD")
    return venues.pop()


def short_name(row, reference=None):
    for value in ((reference or {}).get("secShortName"), row.get("secShortName")):
        if pd.notna(value) and str(value).strip():
            return str(value).strip()
    value = row.get("instrumentName", "")
    name = "" if pd.isna(value) else str(value).strip()
    index = re.fullmatch(INDEX_PATTERN, str(row["Code"]).strip().upper())
    if index and name:
        _, month, side, strike = index.groups()
        source = re.fullmatch(r"(.*?)(?:指数)?" + re.escape(month) + r"([购沽]).+", name)
        if source:
            label = "购" if side == "C" else "沽"
            if source.group(2) != label:
                raise ValueError("instrumentName 的购沽方向与 Code 不一致")
            return f"{source.group(1)}{label}{int(month[-2:])}月{strike}"
    return name


def prepare_daily_row(row, *, day, previous=None, name_reference=None):
    day = pd.Timestamp(day).date()
    if pd.isna(row["Code"]):
        raise ValueError("Code 为空")
    code = str(row["Code"]).strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]*", code):
        raise ValueError(f"无效期权代码：{code}")
    if pd.isna(row["TradeDate"]) or pd.Timestamp(row["TradeDate"]).date() != day:
        raise ValueError("源 TradeDate 为空或与导出日不符")
    if previous is not None:
        if str(previous["Code"]).strip().upper() != code:
            raise ValueError("昨收参考 Code 与源合约不一致")
        if (pd.isna(previous["TradeDate"])
                or pd.Timestamp(previous["TradeDate"]).date() >= day):
            raise ValueError("昨收参考日期必须早于导出日")
    previous = {} if previous is None else previous
    result = dict(
        trade_date=pd.Timestamp(day), code=code,
        secShortName=short_name(row, name_reference),
        exchangeCD=infer_exchange(row, name_reference),
        preSettlePrice=first_present(row.get("preSettlePrice"), previous.get("settlePrice")),
        preClosePrice=first_present(row.get("preClosePrice"), previous.get("closePrice")),
        openPrice=row["openPrice"], highestPrice=row["highPrice"], lowestPrice=row["lowPrice"],
        closePrice=row["closePrice"], settlePrice=row["settlePrice"],
        turnoverVol=row["volume"], turnoverValue=row["turnover"],
        openInt=row["openInterestAccumulate"],
    )
    nullable = {"preClosePrice", "preSettlePrice", "settlePrice"}
    for column in (*FLOAT_COLUMNS, "openInt"):
        value = result[column]
        if pd.isna(value) and column in nullable:
            result[column] = np.nan
            continue
        value = float(value)
        if not np.isfinite(value) or value < 0:
            raise ValueError(f"{column} 存在空值、非有限值或负值")
        if column in ("turnoverVol", "openInt") and not value.is_integer():
            raise ValueError(f"{column} 不是整数")
        result[column] = int(value) if column == "openInt" else value
    return result


def load_names(path):
    if path is None:
        return {}
    frame = pd.read_feather(path)
    if not frame.columns.is_unique or not {"code", "secShortName"}.issubset(frame.columns):
        raise ValueError("简称参考须包含唯一的 code、secShortName 列，可选 exchangeCD")
    columns = [name for name in ("code", "secShortName", "exchangeCD") if name in frame]
    frame = frame.loc[:, columns].copy()
    if frame["code"].isna().any():
        raise ValueError("简称参考 code 存在空值")
    frame["code"] = frame["code"].astype(str).str.strip().str.upper()
    frame = frame.drop_duplicates()
    if frame["code"].duplicated().any():
        raise ValueError("简称参考中同一 code 存在冲突记录")
    return frame.set_index("code").to_dict("index")


def export_day(session, *, day, output_dir, overwrite=False, names=None):
    day = pd.Timestamp(day).date()
    directory = Path(output_dir).expanduser()
    path = directory / f"{day:%Y-%m-%d}.feather"
    if path.exists() and not overwrite:
        print(f"{day}: 文件已存在，跳过；覆盖请加 --overwrite")
        return 0, 0
    source = session.run(f'''
        select * from loadTable("dfs://JC_MKT_MIN", "opt_eod_patch")
        where TradeDate = {day:%Y.%m.%d} order by Code
    ''')
    if source.empty:
        print(f"{day}: 无数据，跳过")
        return 0, 0
    required = {"Code", "TradeDate", "openPrice", "highPrice", "lowPrice", "closePrice",
                "settlePrice", "volume", "turnover", "openInterestAccumulate"}
    if not source.columns.is_unique or not required.issubset(source.columns):
        raise ValueError(f"源表重复列或缺少字段：{sorted(required - set(source.columns))}")
    source = source.copy()
    source["Code"] = source["Code"].astype("string").str.strip().str.upper()
    previous_day = session.run(f'''
        select max(TradeDate) as previous_trade_date
        from loadTable("dfs://JC_MKT_MIN", "opt_eod_patch")
        where TradeDate < {day:%Y.%m.%d}
    ''')
    references = {}
    if not previous_day.empty and pd.notna(previous_day["previous_trade_date"].iloc[0]):
        reference_day = pd.Timestamp(previous_day["previous_trade_date"].iloc[0]).date()
        if reference_day >= day:
            raise ValueError("昨收参考日期必须早于导出日")
        previous = session.run(f'''
            select Code, TradeDate, closePrice, settlePrice
            from loadTable("dfs://JC_MKT_MIN", "opt_eod_patch")
            where TradeDate = {reference_day:%Y.%m.%d}
        ''')
        if not previous.empty:
            previous = previous.copy()
            previous["Code"] = previous["Code"].astype("string").str.strip().str.upper()
            references = {code: group for code, group in previous.groupby("Code", dropna=False)}
    records, errors = [], []
    for code, group in source.groupby("Code", sort=True, dropna=False):
        try:
            if pd.isna(code):
                raise ValueError("Code 为空")
            if len(group) != 1:
                raise ValueError("同一交易日同一 Code 存在重复记录")
            previous = references.get(code)
            if previous is not None and len(previous) != 1:
                raise ValueError("前一交易日同一 Code 存在重复记录")
            record = prepare_daily_row(
                group.iloc[0], day=day,
                previous=None if previous is None else previous.iloc[0],
                name_reference=(names or {}).get(code),
            )
            records.append(record)
        except Exception as exc:
            errors.append(dict(trade_date=str(day), Code=str(code),
                               error_type=type(exc).__name__, error=str(exc)))
            print(f"{day}/{code}: 跳过 {type(exc).__name__}: {exc}")
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / f"{day:%Y-%m-%d}.errors.csv"
    if errors:
        pd.DataFrame(errors).to_csv(report, index=False, encoding="utf-8-sig")
    else:
        report.unlink(missing_ok=True)
    if not records:
        print(f"{day}: 无有效期权日线，异常跳过 {len(errors)} 个合约，未写行情文件")
        return 0, 0
    bars = pd.DataFrame(records, columns=OUTPUT_COLUMNS).sort_values("code").reset_index(drop=True)
    bars["trade_date"] = bars["trade_date"].astype("datetime64[ns]")
    bars[list(FLOAT_COLUMNS)] = bars[list(FLOAT_COLUMNS)].astype("float64")
    bars["openInt"] = bars["openInt"].astype("int64")
    temporary = path.with_suffix(".feather.partial")
    try:
        bars.to_feather(temporary, compression="zstd")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    missing = {column: int(bars[column].isna().sum()) for column in
               ("preClosePrice", "preSettlePrice", "settlePrice")}
    print(f"{day}: 导出 {path}，{len(bars)} 条期权日线；异常跳过 {len(errors)} 个合约；"
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
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--env-file", help="指定 .env；省略时从脚本目录向上查找")
    parser.add_argument("--names-file", type=Path, help="旧日线 Feather：code、secShortName，可选 exchangeCD")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env_file)
    if args.start_day > args.end_day:
        parser.error("开始日期不能晚于结束日期")
    names = load_names(args.names_file)
    import dolphindb as ddb
    session = ddb.session()
    files = rows = 0
    try:
        connect_session(session)
        day = args.start_day
        while day <= args.end_day:
            count, size = export_day(session, day=day, output_dir=args.output_dir,
                                     overwrite=args.overwrite, names=names)
            files += count
            rows += size
            day += timedelta(days=1)
    finally:
        session.close()
    print(f"完成：{files} 个文件，{rows} 条期权日线")


if __name__ == "__main__":
    main()
