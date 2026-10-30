"""导出 Tinysoft fut_1min_patch，匹配旧期货 Feather 的 14 列格式。"""

import argparse
import os
import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


OUTPUT_COLUMNS = (
    "vt_symbol", "symbol", "exchange", "open", "high", "low", "close",
    "date", "time", "datetime", "volume", "value", "open_interest", "vwap",
)
EXCHANGES = {
    "上海期货交易所": "XSGE", "SHFE": "XSGE", "XSGE": "XSGE",
    "大连商品交易所": "XDCE", "DCE": "XDCE", "XDCE": "XDCE",
    "郑州商品交易所": "XZCE", "CZCE": "XZCE", "XZCE": "XZCE",
    "上海国际能源交易中心": "XSIE", "INE": "XSIE", "XSIE": "XSIE",
    "广州期货交易所": "XGFE", "GFEX": "XGFE", "XGFE": "XGFE",
    "中国金融期货交易所": "CCFX", "CFFEX": "CCFX", "CCFX": "CCFX", "XCFX": "CCFX",
}


def required_columns(frame, required):
    if not frame.columns.is_unique or not set(required).issubset(frame.columns):
        raise ValueError(f"存在重复列或缺少字段：{sorted(set(required) - set(frame.columns))}")


def build_specs(info, codes, *, day, metadata_fallback="product"):
    """优先精确合约；缺失时可采用最近同品种的一致条款，不读取未来快照。"""
    if metadata_fallback not in ("product", "error"):
        raise ValueError("metadata_fallback 应为 product 或 error")
    required_columns(info, ("Code", "TradeDate", "exchange", "multiplier"))
    info = info.copy()
    info["Code"] = info["Code"].astype(str).str.strip().str.upper()
    dates = pd.to_datetime(info["TradeDate"], errors="raise")
    if dates.isna().any() or (dates.dt.date > day).any():
        raise ValueError("合约资料包含空日期或导出日之后的快照")
    if info["Code"].duplicated().any():
        raise ValueError("合约资料每个 Code 必须只有一条最新快照")
    info["TradeDate"] = dates
    info = info.set_index("Code")
    specs = {}
    for code in codes:
        # 月均价等合约带字母后缀，例如 L2611F，后缀属于合约身份。
        match = re.fullmatch(r"([A-Za-z]+)(\d{3,4})([A-Za-z]*)", code)
        if match is None:
            raise ValueError(f"非预期期货代码：{code}")
        product, month, suffix = match.groups()
        suffix = suffix.upper()
        metadata_source = "contract"
        if code in info.index:
            row = info.loc[code]
        else:
            if metadata_fallback == "error":
                raise ValueError(f"{day}/{code}: 缺少当日或更早的合约资料")
            # 前缀和后缀都必须一致：B 不匹配 BB，L...F 不借用普通 L 合约。
            family_pattern = re.escape(product) + r"\d{3,4}" + re.escape(suffix)
            candidates = info.loc[info.index.str.fullmatch(family_pattern)].copy()
            if candidates.empty:
                raise ValueError(f"{day}/{code}: 缺少合约资料，品种 {product}"
                                 f"（后缀 {suffix or '无'}）也无历史资料")
            latest = candidates["TradeDate"].max()
            candidates = candidates.loc[candidates["TradeDate"].eq(latest)]
            exchanges = candidates["exchange"].astype(str).str.strip().str.upper().map(EXCHANGES)
            multipliers = pd.to_numeric(candidates["multiplier"], errors="raise")
            if exchanges.isna().any() or not np.isfinite(multipliers).all() or (multipliers <= 0).any():
                raise ValueError(f"{day}/{code}: 同品种后备资料存在无效交易所或乘数")
            if exchanges.nunique() != 1 or multipliers.nunique() != 1:
                raise ValueError(f"{day}/{code}: 最近同品种资料的交易所或乘数不一致，无法自动补充")
            row = candidates.iloc[0]
            metadata_source = "product"
            print(f"{day}/{code}: 缺少精确合约资料；采用品种 {product} "
                  f"（后缀 {suffix or '无'}）"
                  f"在 {latest:%Y-%m-%d} 的一致交易所和乘数，"
                  f"参考合约：{candidates.index.tolist()[:5]}")
        exchange = EXCHANGES.get(str(row["exchange"]).strip().upper())
        if exchange is None:
            raise ValueError(f"{code}: 未知交易所：{row['exchange']}")
        multiplier = float(row["multiplier"])
        if not np.isfinite(multiplier) or multiplier <= 0:
            raise ValueError(f"{code}: 合约乘数无效")
        if exchange == "XZCE":
            symbol = product.upper() + month[-3:]
        elif exchange == "CCFX":
            symbol = product.upper() + month
        else:
            symbol = product.lower() + month
        # 商品品种前缀小写，但保留 F 等后缀大写：l2611F。
        symbol += suffix
        specs[code] = dict(symbol=symbol, exchange=exchange, multiplier=multiplier,
                           metadata_source=metadata_source)
    names = [spec["symbol"] + "." + spec["exchange"] for spec in specs.values()]
    if len(names) != len(set(names)):
        raise ValueError("多个源 Code 映射到同一 CTP 合约，请检查源代码别名")
    return specs


def cumulative_to_interval(values, name, first_row_policy):
    if first_row_policy not in ("error", "zero-baseline"):
        raise ValueError("first_row_policy 应为 error 或 zero-baseline")
    increments = values.diff()
    if (increments.iloc[1:] < 0).any():
        raise ValueError(f"{name} 累计值回退，需检查源数据重置或修订")
    if values.iloc[0] != 0 and first_row_policy == "error":
        raise ValueError(f"{name} 首条累计值为 {values.iloc[0]}，无法确定首分钟增量；"
                         "确认包含完整交易日后，传入 --first-row-policy zero-baseline")
    increments.iloc[0] = values.iloc[0]
    return increments.astype("float64")


def prepare_bars(frame, *, day, spec, source_timezone="Asia/Shanghai",
                 first_row_policy="error", vwap_mode="close",
                 volume_column="volumeAccumulate", turnover_column="turnoverAccumulate"):
    required_columns(frame, ("TradeDate", "ActTime", "openPrice", "highPrice",
                             "lowPrice", "closePrice", volume_column, turnover_column,
                             "openInterestAccumulate"))
    frame = frame.copy()
    dates = pd.to_datetime(frame["TradeDate"], errors="raise")
    if dates.isna().any() or not dates.dt.date.eq(day).all():
        raise ValueError("TradeDate 存在空值或包含其他交易日")
    times = pd.to_datetime(frame["ActTime"], errors="raise")
    if times.isna().any():
        raise ValueError("ActTime 存在空值")
    if times.dt.tz is None:
        times = times.dt.tz_localize(source_timezone)
    # ActTime 必须是实际自然日期；夜盘不使用 TradeDate 拼接时间。
    frame["datetime"] = times.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).astype(
        "datetime64[ns]",
    )
    frame = frame.sort_values("datetime").reset_index(drop=True)
    if frame.empty:
        raise ValueError("合约分钟记录为空")
    if frame["datetime"].duplicated().any():
        raise ValueError("同一合约存在重复时间记录")
    for column in {"openPrice", "highPrice", "lowPrice", "closePrice",
                   volume_column, turnover_column, "openInterestAccumulate"}:
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype("float64")
        if not np.isfinite(frame[column]).all() or (frame[column] < 0).any():
            raise ValueError(f"{column} 存在空值、非有限值或负值")
    if (frame[volume_column] % 1 != 0).any():
        raise ValueError("累计成交量不是整数")
    bars = pd.DataFrame(index=frame.index)
    bars["vt_symbol"] = spec["symbol"] + "." + spec["exchange"]
    bars["symbol"] = spec["symbol"]
    bars["exchange"] = spec["exchange"]
    for name in ("open", "high", "low", "close"):
        bars[name] = frame[name + "Price"]
    bars["datetime"] = frame["datetime"]
    bars["date"] = bars["datetime"].dt.strftime("%Y-%m-%d")
    bars["time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    bars["volume"] = cumulative_to_interval(frame[volume_column], "成交量", first_row_policy)
    bars["value"] = cumulative_to_interval(frame[turnover_column], "成交额", first_row_policy)
    bars["open_interest"] = frame["openInterestAccumulate"]
    if vwap_mode not in ("close", "interval"):
        raise ValueError("vwap_mode 应为 close 或 interval")
    bars["vwap"] = bars["close"]
    if vwap_mode == "interval":
        traded = bars["volume"] > 0
        if ((~traded) & (bars["value"] != 0)).any():
            raise ValueError("成交量为零但成交额非零，不能计算区间 VWAP")
        bars.loc[traded, "vwap"] = (bars.loc[traded, "value"]
                                    / bars.loc[traded, "volume"] / spec["multiplier"])
    return bars.loc[:, list(OUTPUT_COLUMNS)]


def export_day(session, *, day, output_dir, overwrite=False,
               metadata_fallback="product", **conversion_options):
    day = pd.Timestamp(day).date()
    frame = session.run(f'''
        select * from loadTable("dfs://JC_MKT_MIN", "fut_1min_patch")
        where TradeDate = {day:%Y.%m.%d} order by Code, ActTime
    ''')
    if frame.empty:
        print(f"{day}: 无数据，跳过")
        return 0, 0
    required_columns(frame, ("Code",))
    if frame["Code"].isna().any():
        raise ValueError("Code 存在空值")
    frame = frame.copy()
    frame["Code"] = frame["Code"].astype(str).str.strip().str.upper()
    info = session.run(f'''
        select max(TradeDate) as TradeDate,
               atImax(TradeDate, exchange) as exchange,
               atImax(TradeDate, multiplier) as multiplier
        from loadTable("dfs://JC_MKT_MIN", "fut_instrument_base_info")
        where TradeDate <= {day:%Y.%m.%d} group by Code
    ''')
    directory = Path(output_dir).expanduser() / f"{day:%Y%m%d}"
    errors = []

    def record_error(code, stage, exc):
        errors.append(dict(trade_date=str(day), Code=code, stage=stage,
                           error_type=type(exc).__name__, error=str(exc)))
        print(f"{day}/{code}: 跳过 [{stage}] {type(exc).__name__}: {exc}")

    # 把资料映射的失败也限定到单个合约，避免在进入分钟转换前整日中断。
    specs = {}
    for code in frame["Code"].unique():
        try:
            specs.update(build_specs(info, [code], day=day,
                                     metadata_fallback=metadata_fallback))
        except Exception as exc:
            record_error(code, "metadata", exc)

    identities = {}
    for code, spec in specs.items():
        identity = spec["symbol"] + "." + spec["exchange"]
        identities.setdefault(identity, []).append(code)
    for identity, codes in identities.items():
        if len(codes) > 1:
            for code in codes:
                record_error(code, "metadata", ValueError(
                    f"多个源 Code 映射到 {identity}：{codes}"))
                del specs[code]

    directory.mkdir(parents=True, exist_ok=True)
    files = rows = existing = 0
    for code, group in frame.groupby("Code", sort=True):
        if code not in specs:
            continue
        path = directory / f"{specs[code]['symbol']}_{day:%Y%m%d}.feather"
        if path.exists() and not overwrite:
            existing += 1
            print(f"{day}/{code}: 文件已存在，跳过；覆盖请传入 --overwrite")
            continue
        stage = "conversion"
        try:
            bars = prepare_bars(group, day=day, spec=specs[code], **conversion_options)
            stage = "write"
            temporary = path.with_suffix(".feather.partial")
            try:
                bars.to_feather(temporary, compression="zstd")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception as exc:
            record_error(code, stage, exc)
            continue
        files += 1
        rows += len(bars)

    report = directory / "export_errors.csv"
    if errors:
        pd.DataFrame(errors).to_csv(report, index=False, encoding="utf-8-sig")
        print(f"{day}: 跳过明细已保存至 {report}")
    else:
        report.unlink(missing_ok=True)
    print(f"{day}: 导出 {files} 个合约文件，{rows} 条记录；"
          f"异常跳过 {len(errors)} 个合约，已有文件跳过 {existing} 个")
    return files, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-timezone", default="Asia/Shanghai")
    parser.add_argument("--volume-column", default="volumeAccumulate")
    parser.add_argument("--turnover-column", default="turnoverAccumulate")
    parser.add_argument("--first-row-policy", choices=("error", "zero-baseline"), default="error")
    parser.add_argument("--vwap-mode", choices=("close", "interval"), default="close")
    parser.add_argument("--metadata-fallback", choices=("product", "error"), default="product",
                        help="缺少精确合约资料时，默认使用最近同品种的一致交易所和乘数")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.start_day > args.end_day:
        parser.error("开始日期不能晚于结束日期")
    if not os.environ.get("DDB_PASSWORD"):
        parser.error("请设置环境变量 DDB_PASSWORD")
    import dolphindb as ddb
    session = ddb.session()
    total_files = total_rows = 0
    try:
        if not session.connect(host=os.environ.get("DDB_HOST", "10.168.51.20"),
                               port=int(os.environ.get("DDB_PORT", "8912")),
                               userid=os.environ.get("DDB_USER", "laok"),
                               password=os.environ["DDB_PASSWORD"]):
            raise ConnectionError("DolphinDB 连接失败")
        day = args.start_day
        while day <= args.end_day:
            files, rows = export_day(
                session, day=day, output_dir=args.output_dir, overwrite=args.overwrite,
                source_timezone=args.source_timezone, first_row_policy=args.first_row_policy,
                vwap_mode=args.vwap_mode, volume_column=args.volume_column,
                turnover_column=args.turnover_column, metadata_fallback=args.metadata_fallback,
            )
            total_files += files
            total_rows += rows
            day += timedelta(days=1)
    finally:
        session.close()
    print(f"完成：{total_files} 个文件，{total_rows} 条记录")


if __name__ == "__main__":
    main()
