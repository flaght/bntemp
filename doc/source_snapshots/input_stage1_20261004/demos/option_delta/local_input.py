from pathlib import Path
import os
import re
from math import isfinite
import pandas as pd
from .model import Option


OPTION_COLUMNS=("close","open_interest","volume")


def symbol(value): return str(value).strip().split(".")[0].upper()


def contract_date(value,code,field):
    try:
        stamp=pd.Timestamp(value)
        if pd.isna(stamp): raise ValueError("空日期")
        return stamp.date()
    except (TypeError,ValueError) as exc:
        raise ValueError(f"{code}: {field}日期无效") from exc


def load_options(path,product,index_code,option_tick=None):
    frame=pd.read_feather(path)
    if not frame.columns.is_unique: raise ValueError("opt_basic重复列")
    if "symbol" not in frame and "Code" in frame: frame=frame.rename(columns={"Code":"symbol"})
    if "Code" in frame and "symbol" in frame and not frame.Code.map(symbol).eq(frame.symbol.map(symbol)).all():
        raise ValueError("opt_basic的Code与symbol不一致")
    required={"symbol","contractType","strikePrice","contMultNum","varTicker","exchangeCD","listDate","lastTradeDate","expDate"}
    if not required.issubset(frame): raise ValueError(f"opt_basic缺字段: {sorted(required-set(frame))}")
    options={}
    expiries={}
    for row in frame.to_dict("records"):
        code=symbol(row["symbol"])
        match=re.fullmatch(rf"{re.escape(product)}(\d{{4}})-([CP])-(\d+)",code)
        if not match: continue
        if str(row["exchangeCD"]).strip().upper() not in ("CCFX","XCFX","CFFEX"): raise ValueError(f"{code}: 非中金所")
        if str(row["varTicker"]).strip().zfill(6)!=index_code: raise ValueError(f"{code}: 标的指数不匹配")
        if str(row["contractType"]).strip().upper() not in (("C","CO","CALL","认购") if match[2]=="C" else ("P","PO","PUT","认沽")):
            raise ValueError(f"{code}: 认购/认沽标识冲突")
        strike,mult=float(row["strikePrice"]),float(row["contMultNum"])
        tick=row.get("minChgPriceNum",row.get("price_increment",option_tick))
        if tick is None or pd.isna(tick): tick=option_tick
        if tick is None: raise ValueError("opt_basic缺少tick，请设置--option-tick或OPT_PRICE_INCREMENT")
        tick=float(tick)
        if not all(isfinite(n) and n>0 for n in (strike,mult,tick)) or strike!=int(match[3]): raise ValueError(f"{code}: 条款无效")
        listed=contract_date(row["listDate"],code,"listDate")
        expiry=contract_date(row["lastTradeDate"],code,"lastTradeDate")
        expiration=contract_date(row["expDate"],code,"expDate")
        if not listed<=expiry<=expiration: raise ValueError(f"{code}: 日期无效")
        month="20"+match[1]
        if expiry.strftime("%Y%m")!=month or expiration.strftime("%Y%m")!=month:
            raise ValueError(f"{code}: 合约月份与到期日期不一致")
        if month in expiries and expiries[month]!=expiry:
            raise ValueError(f"{code}: 同月合约最后交易日期不一致")
        expiries[month]=expiry
        option=Option(code,match[2],strike,listed,expiry,tick,mult,month)
        if code in options and options[code]!=option: raise ValueError(f"{code}: 静态条款冲突")
        options[code]=option
    if not options: raise ValueError(f"没有{product}期权资料")
    return options


def load_futures(path,product):
    frame=pd.read_feather(path)
    required={"symbol","code","exchangeCD","listDate","lastTradeDate"}
    if not frame.columns.is_unique or not required.issubset(frame): raise ValueError("fut_basic缺字段或重复列")
    rows={}
    for row in frame.to_dict("records"):
        if str(row["code"]).strip().upper()!=product: continue
        key=symbol(row["symbol"])
        if not re.fullmatch(re.escape(product)+r"\d{4}",key): continue
        if str(row["exchangeCD"]).strip().upper() not in ("CCFX","XCFX","CFFEX"): raise ValueError(f"{key}: 非中金所")
        row["listDate"]=contract_date(row["listDate"],key,"listDate")
        row["lastTradeDate"]=contract_date(row["lastTradeDate"],key,"lastTradeDate")
        if row["listDate"]>row["lastTradeDate"]: raise ValueError(f"{key}: 上市日期晚于到期日")
        if key in rows: raise ValueError(f"fut_basic重复合约: {key}")
        rows[key]=row
    return rows


def inventory(root,start,end,predicate):
    root=Path(root)
    if not root.is_dir(): raise FileNotFoundError(root)
    result={}
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not re.fullmatch(r"\d{8}",folder.name): continue
        day=pd.Timestamp(folder.name).date()
        if not start<=day<=end: continue
        suffix="_"+folder.name+".feather"
        for path in folder.glob("*.feather"):
            if not path.name.endswith(suffix): continue
            key=symbol(path.name[:-len(suffix)])
            if predicate(key):
                if (day,key) in result: raise ValueError(f"重复行情文件: {day}/{key}")
                result[day,key]=path
    return result


def timestamps(values):
    result=pd.to_datetime(values,errors="raise")
    if result.isna().any(): raise ValueError("行情时间为空")
    return result.dt.tz_localize("Asia/Shanghai") if result.dt.tz is None else result.dt.tz_convert("Asia/Shanghai")


def read_frame(path,key,day,kind,bar_timestamp):
    if kind not in ("option","index","future") or bar_timestamp not in ("start","end"):
        raise ValueError("行情类型或分钟时间口径无效")
    frame=pd.read_feather(path).copy()
    required={"datetime"}|(set(OPTION_COLUMNS) if kind=="option" else {"close"})
    if not frame.columns.is_unique or frame.empty or not required.issubset(frame):
        raise ValueError(f"{path}: 空文件、重复列或缺少{sorted(required-set(frame))}；期权选约需要close、open_interest和分钟volume")
    if "symbol" in frame and not frame.symbol.map(symbol).eq(key).all(): raise ValueError(f"{path}: 合约身份不一致")
    frame["datetime"]=timestamps(frame.datetime)
    if not frame.datetime.dt.date.eq(day).all(): raise ValueError(f"{path}: 日期与目录不一致")
    if bar_timestamp=="start": frame["datetime"]+=pd.Timedelta(minutes=1)
    if not frame.datetime.eq(frame.datetime.dt.floor("min")).all():
        raise ValueError(f"{path}: 分钟快照时间必须对齐整分钟")
    if not frame.datetime.dt.date.eq(day).all(): raise ValueError(f"{path}: 分钟时间转换后跨日期")
    if frame.datetime.duplicated().any(): raise ValueError(f"{path}: 重复分钟")
    if kind=="option":
        for name in OPTION_COLUMNS: frame[name]=pd.to_numeric(frame[name],errors="coerce")
        # Feather可能保存微秒精度；Timestamp.value始终给出纳秒，匹配ts_event。
        frame["bar_ns"]=frame.datetime.map(lambda stamp:stamp.value)
    else:
        frame["close"]=pd.to_numeric(frame.close,errors="raise")
        if not frame.close.map(lambda n:isfinite(n) and n>0).all(): raise ValueError(f"{path}: 标的价格无效")
    return frame.sort_values("datetime").reset_index(drop=True)


def resolve_paths(args):
    root=args.data_root or os.environ.get("CTP_DATA_DIR")
    root=Path(root).expanduser() if root else None
    roles=os.environ.get("FUT_ROLE_DATA_DIR") or os.environ.get("ROLE_DIR")
    roles=Path(roles).expanduser() if roles else root/"role" if root else None
    kline=os.environ.get("KLINE_DATA_DIR") or os.environ.get("KLINE_DIR")
    kline=Path(kline).expanduser() if kline else root/"kline" if root else None
    for name,env,kind in (("opt_dir","OPT_KLINE_DATA_DIR","opt"),("index_dir","INDEX_KLINE_DATA_DIR","index"),("fut_dir","FUT_KLINE_DATA_DIR","fut")):
        value=getattr(args,name) or os.environ.get(env)
        path=Path(value).expanduser() if value else kline/kind if kline else None
        if value and (path/kind).is_dir(): path=path/kind
        setattr(args,name,path)
    if roles is not None:
        args.opt_basic=args.opt_basic or roles/"opt_basic.feather"
        args.fut_basic=args.fut_basic or roles/"fut_basic.feather"
    if args.opt_basic is None or args.opt_dir is None or args.index_dir is None: raise ValueError("配置CTP_DATA_DIR或各资料/行情目录")
    if args.model=="black76" and (args.fut_dir is None or args.fut_basic is None): raise ValueError("Black76需同月期货行情和fut_basic")
    return args
