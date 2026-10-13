"""此策略回测资料/行情加载；不修改DataHub，不依赖examples策略。"""
from datetime import date
from decimal import Decimal
from pathlib import Path
import os
import re
import pandas as pd

from .model import Contract

VENUES = {"CCFX": "CFFEX", "XCFX": "CFFEX", "CFFEX": "CFFEX"}


def symbol(value):
    # 本策略只支持整数行权价的股指期权；保留中间连字符。
    return str(value).strip().split(".")[0].upper()


def positive(value, name):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{name}必须为有限正数")
    return result


def load_calendar(path, start_day=None, end_day=None):
    frame = pd.read_csv(path) if Path(path).suffix.lower() == ".csv" else pd.read_feather(path)
    if not {"date", "is_trading_day"}.issubset(frame):
        raise ValueError("交易日历需date、is_trading_day列")
    flags = frame.is_trading_day.astype(str).str.lower()
    if not flags.isin(("true", "false", "1", "0")).all():
        raise ValueError("is_trading_day只接受True/False或1/0")
    all_dates = pd.to_datetime(frame.date, errors="raise")
    if all_dates.isna().any() or all_dates.duplicated().any():
        raise ValueError("交易日历存在空日期或重复日期")
    if start_day is not None and start_day < all_dates.min().date():
        raise ValueError("交易日历未覆盖开始日期")
    if end_day is not None and end_day > all_dates.max().date():
        raise ValueError("交易日历未覆盖结束日期")
    days = tuple(sorted(all_dates.loc[flags.isin(("true", "1"))].dt.date))
    if not days:
        raise ValueError("交易日历没有交易日")
    return days



def infer_market_calendar(roots, start_day, end_day):
    """信任已经做过完整性检查的行情，扫描全部日期而不读取区间外价格。"""
    days = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            raise FileNotFoundError(root)
        for folder in root.iterdir():
            if not folder.is_dir() or not re.fullmatch(r"\d{8}", folder.name):
                continue
            if any(path.is_file() for path in folder.glob(f"*_{folder.name}.feather")):
                days.add(date.fromisoformat(f"{folder.name[:4]}-{folder.name[4:6]}-{folder.name[6:]}"))
    calendar = tuple(sorted(days))
    if not calendar:
        raise ValueError("行情目录没有可用于推导交易日的日期文件")
    if start_day < calendar[0] or end_day > calendar[-1]:
        raise ValueError(f"请求区间{start_day}..{end_day}超出行情日期范围{calendar[0]}..{calendar[-1]}")
    return calendar

def load_contracts(opt_path, fut_path, option_product, future_product, index_code, option_tick=None):
    options = pd.read_feather(opt_path)
    futures = pd.read_feather(fut_path)
    # 兼容已导出的symbol格式与源表Code格式；不要求额外的品种code列。
    if "Code" not in options and "symbol" in options:
        options = options.rename(columns={"symbol": "Code"})
    elif {"Code", "symbol"}.issubset(options) and options.columns.is_unique:
        if not options["Code"].map(symbol).eq(options["symbol"].map(symbol)).all():
            raise ValueError("opt_basic的Code与symbol不一致")
    option_required = {"Code", "contractType", "strikePrice", "contMultNum", "varTicker", "exchangeCD", "listDate", "lastTradeDate", "expDate"}
    future_required = {"symbol", "code", "exchangeCD", "contMultNum", "minChgPriceNum", "listDate", "lastTradeDate"}
    for frame, required, name in ((options, option_required, "opt_basic"), (futures, future_required, "fut_basic")):
        if not frame.columns.is_unique or not required.issubset(frame):
            raise ValueError(f"{name}重复列或缺少字段: {required - set(frame)}")
    future_rows = {}
    for row in futures.to_dict("records"):
        if str(row["code"]).upper() != future_product:
            continue
        key = symbol(row["symbol"])
        if key in future_rows:
            raise ValueError(f"fut_basic重复合约: {key}")
        if VENUES.get(str(row["exchangeCD"]).upper()) != "CFFEX":
            raise ValueError(f"{key}: 只支持CFFEX股指品种")
        row["contMultNum"] = positive(row["contMultNum"], key + "/multiplier")
        row["minChgPriceNum"] = positive(row["minChgPriceNum"], key + "/tick")
        for field in ("listDate", "lastTradeDate"):
            row[field] = pd.Timestamp(row[field]).date()
        if row["listDate"] > row["lastTradeDate"]:
            raise ValueError(f"{key}: 上市/最后交易日异常")
        future_rows[key] = row
    contracts = {}
    for row in options.to_dict("records"):
        key = symbol(row["Code"])
        match = re.fullmatch(rf"{re.escape(option_product)}(\d{{4}})-C-(\d+)", key)
        if match is None or str(row["contractType"]).upper() not in ("CO", "C", "CALL", "认购"):
            continue
        if str(row["varTicker"]).strip().zfill(6) != index_code:
            raise ValueError(f"{key}: 标的不是{index_code}")
        if VENUES.get(str(row["exchangeCD"]).upper()) != "CFFEX":
            raise ValueError(f"{key}: 交易所无效")
        if "currencyCD" in row and str(row["currencyCD"]).upper() != "CNY":
            raise ValueError(f"{key}: 币种必须为CNY")
        future = future_product + match[1]
        strike = positive(row["strikePrice"], key + "/strike")
        if strike != Decimal(match[2]):
            raise ValueError(f"{key}: 行权价与代码不一致")
        tick = row.get("minChgPriceNum", row.get("price_increment"))
        if tick is None or pd.isna(tick):
            tick = option_tick
        if tick is None:
            raise ValueError("opt_basic没有tick，请设置--option-tick或OPT_PRICE_INCREMENT")
        contract = Contract(key, future, float(strike), positive(row["contMultNum"], key + "/multiplier"),
            pd.Timestamp(row["listDate"]).date(), pd.Timestamp(row["lastTradeDate"]).date(),
            pd.Timestamp(row["expDate"]).date(), positive(tick, key + "/tick"))
        if not contract.list_day <= contract.last_day <= contract.expiry_day:
            raise ValueError(f"{key}: 上市/最后交易/到期日期异常")
        # 同一代码的静态条款重复可合并；修订冲突不能按最终版本回灌历史。
        if key in contracts and contracts[key] != contract:
            raise ValueError(f"{key}: 存在冲突条款版本，需按实际可用时间整理资料")
        contracts[key] = contract
    if not contracts or not future_rows:
        raise ValueError("没有目标品种的认购期权或期货基础资料")
    return contracts, future_rows



def filter_hedgeable_options(candidates, contracts, future_rows, day):
    """期权与同月期货上市月份不同；缺资料或未在上市期的对冲腿不进入当日选约。"""
    eligible, skipped = [], set()
    for key in candidates:
        future = contracts[key].future
        if future not in future_rows:
            skipped.add(future)
            continue
        row = future_rows[future]
        if not row["listDate"] <= day <= row["lastTradeDate"]:
            skipped.add(future)
            continue
        eligible.append(key)
    return tuple(eligible), tuple(sorted(skipped))

def inventory(root, start_day, end_day, predicate):
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(root)
    paths = {}
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not re.fullmatch(r"\d{8}", folder.name):
            continue
        day = pd.Timestamp(folder.name).date()
        if not start_day <= day <= end_day:
            continue
        suffix = "_" + folder.name + ".feather"
        for path in folder.glob("*.feather"):
            if not path.name.endswith(suffix):
                continue
            key = symbol(path.name[:-len(suffix)])
            if predicate(key):
                identity = (day, key)
                if identity in paths:
                    raise ValueError(f"重复文件: {identity}")
                paths[identity] = path
    return paths


def read_frame(path, expected_symbol, day, bar_timestamp):
    frame = pd.read_feather(path).copy()
    required = {"datetime", "open", "high", "low", "close", "volume"}
    if not frame.columns.is_unique or not required.issubset(frame) or frame.empty:
        raise ValueError(f"{path}: 空文件、重复列或缺少{required - set(frame)}")
    if "symbol" in frame and not frame.symbol.map(symbol).eq(expected_symbol).all():
        raise ValueError(f"{path}: symbol与文件不一致")
    if "trade_date" in frame and not pd.to_datetime(frame.trade_date).dt.date.eq(day).all():
        raise ValueError(f"{path}: trade_date与目录不一致")
    stamps = pd.to_datetime(frame.datetime, errors="raise")
    if stamps.isna().any():
        raise ValueError(f"{path}: 空时间")
    if stamps.dt.tz is None:
        stamps = stamps.dt.tz_localize("Asia/Shanghai")
    frame["datetime"] = stamps.dt.tz_convert("Asia/Shanghai")
    if not frame.datetime.dt.date.eq(day).all():
        raise ValueError(f"{path}: datetime日期与目录不一致")
    if bar_timestamp == "start":
        frame["datetime"] += pd.Timedelta(minutes=1)
    frame = frame.sort_values("datetime").reset_index(drop=True)
    if frame.datetime.duplicated().any():
        raise ValueError(f"{path}: 重复分钟")
    for field in ("open", "high", "low", "close", "volume"):
        frame[field] = pd.to_numeric(frame[field], errors="raise")
        if frame[field].isna().any() or not frame[field].map(lambda v: Decimal(str(v)).is_finite()).all():
            raise ValueError(f"{path}: {field}非有限值")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any() or (frame.volume < 0).any():
        raise ValueError(f"{path}: 非正价格或负成交量")
    if not (frame.volume % 1 == 0).all():
        raise ValueError(f"{path}: volume需为分钟整数成交量")
    if ((frame.high < frame[["open", "close"]].max(axis=1)) | (frame.low > frame[["open", "close"]].min(axis=1))).any():
        raise ValueError(f"{path}: OHLC关系异常")
    return frame


def resolve_paths(args):
    root_value = args.data_root or os.environ.get("CTP_DATA_DIR")
    root = Path(root_value).expanduser() if root_value else None
    role_value = os.environ.get("FUT_ROLE_DATA_DIR") or os.environ.get("ROLE_DIR")
    roles = Path(role_value).expanduser() if role_value else root / "role" if root else None
    kline_value = os.environ.get("KLINE_DATA_DIR") or os.environ.get("KLINE_DIR")
    kline = Path(kline_value).expanduser() if kline_value else root / "kline" if root else None
    def bar_dir(explicit, env, kind):
        value = explicit or os.environ.get(env)
        if value:
            path = Path(value).expanduser()
            # 兼容FUT_KLINE_DATA_DIR指向kline父目录。
            return path / kind if (path / kind).is_dir() else path
        return kline / kind if kline else None
    args.fut_dir = bar_dir(args.fut_dir, "FUT_KLINE_DATA_DIR", "fut")
    args.opt_dir = bar_dir(args.opt_dir, "OPT_KLINE_DATA_DIR", "opt")
    args.index_dir = bar_dir(args.index_dir, "INDEX_KLINE_DATA_DIR", "index")
    args.opt_basic = args.opt_basic or (roles / "opt_basic.feather" if roles else None)
    args.fut_basic = args.fut_basic or (roles / "fut_basic.feather" if roles else None)
    calendar = args.calendar or os.environ.get("CTP_CALENDAR_PATH")
    args.calendar = Path(calendar).expanduser() if calendar else None
    if any(getattr(args, name) is None for name in ("fut_dir", "opt_dir", "index_dir", "opt_basic", "fut_basic")):
        raise ValueError("请配置CTP_DATA_DIR或各资料/行情目录")
    return args
