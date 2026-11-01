"""读取显式期权最终结算资料；基础条款决定实际最后交易日。"""
from __future__ import annotations

from pathlib import Path

from .session import current_session, read_feather


def load_option_settlements(path, options, *, start_day, end_day,
                            available_time="16:00", fee_per_contract=0):
    """支持标准 CSV/Feather/Parquet，兼容旧 opt_eod 的到期日行。

    标准列：symbol, settlement_price, last_trade_date；available_at 可选。
    缺少 available_at 时调用方显式声明本地结算可用时刻（默认当日16:00）。
    这里只接入官方最终结算价，不根据日内价格猜测结算值。
    """
    import pandas as pd
    from bomber.framework.trader.execution.option_settlement import OptionSettlement
    from bomber.framework.trader.instrument_factory import day_ns

    path = Path(path).expanduser().resolve()
    session = current_session()
    if session is not None:
        session.fingerprint(path)
    if path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
    elif path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    elif path.suffix.lower() == ".feather":
        frame = read_feather(path)
    else:
        raise ValueError("Settlement file must be CSV, Feather or Parquet")
    aliases = {"tradeID": "symbol", "lastTradingDate": "last_trade_date", "settlePrice": "settlement_price"}
    for raw, canonical in aliases.items():
        if raw in frame and canonical in frame:
            raise ValueError(f"Conflicting settlement columns: {raw}, {canonical}")
    frame = frame.rename(columns=aliases)
    required = {"symbol", "last_trade_date", "settlement_price"}
    if not frame.columns.is_unique or required - set(frame):
        raise ValueError(f"Settlement file requires unique columns: {sorted(required)}")
    frame["last_trade_date"] = pd.to_datetime(frame.last_trade_date, errors="raise").dt.date
    if "TradeDate" in frame:
        # 日度 settlePrice 不能作为到期终值；只取 TradeDate == lastTradingDate。
        frame = frame.loc[pd.to_datetime(frame.TradeDate, errors="raise").dt.date.eq(frame.last_trade_date)]
    result = {}
    for number, row in enumerate(frame.to_dict("records"), 2):
        symbol = str(row["symbol"]).strip().upper().removesuffix(".CFFEX")
        if symbol not in options:
            continue
        info = options[symbol]
        if not start_day <= info.last_trade <= end_day:
            continue
        if row["last_trade_date"] != info.last_trade:
            raise ValueError(f"{path}:{number}: {symbol} settlement date conflicts with opt_basic")
        available = row.get("available_at")
        if available is None or pd.isna(available):
            available_ns = day_ns(info.last_trade, available_time)
        else:
            stamp = pd.Timestamp(available)
            stamp = stamp.tz_localize("Asia/Shanghai") if stamp.tzinfo is None else stamp
            available_ns = int(stamp.value)
        fee = row.get("fee_per_contract")
        if fee is None or pd.isna(fee):
            fee = fee_per_contract
        item = OptionSettlement(symbol + ".CFFEX", day_ns(info.last_trade, "15:00"),
            available_ns, row["settlement_price"], fee, str(path))
        if symbol in result and result[symbol] != item:
            raise ValueError(f"Conflicting final settlements for {symbol}")
        result[symbol] = item
    return result
