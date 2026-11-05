"""读取显式期权最终结算资料；基础条款决定实际最后交易日。"""
from __future__ import annotations

from pathlib import Path

from .contracts import InputIssue
from .session import current_session, read_feather, record_issue


def add_option_settlement_arguments(parser):
    """现金结算回测入口共用的显式资料与费用参数。"""
    from decimal import Decimal
    parser.add_argument("--option-settlements", type=Path,
        help="最终结算CSV/Parquet/Feather，或按到期日保存的期权日行情目录")
    parser.add_argument("--settlement-available-time", default="16:00",
        help="结算文件未提供available_at时显式采用的本地可用时刻，默认16:00")
    parser.add_argument("--exercise-fee-per-contract", type=Decimal, default=Decimal(0),
        help="每手现金结算费用；文件逐合约fee_per_contract优先")


def load_option_settlements(path, options, *, start_day, end_day,
                            available_time="16:00", fee_per_contract=0):
    """支持标准结算文件，或 YYYY-MM-DD.feather 原始期权日行情目录。

    标准列：symbol, settlement_price, last_trade_date；available_at 可选。
    日行情目录列：code, trade_date, settlPrice；仅读取基础条款指定的到期日。
    缺少 available_at 时调用方显式声明本地结算可用时刻（默认当日16:00）。
    这里只接入官方最终结算价，不根据日内价格猜测结算值。
    """
    import pandas as pd

    path = Path(path).expanduser().resolve()
    if path.is_dir():
        return _load_daily_directory(path, options, start_day=start_day, end_day=end_day,
            available_time=available_time, fee_per_contract=fee_per_contract)
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
    return _prepare_settlements(frame, path, options, start_day=start_day, end_day=end_day,
        available_time=available_time, fee_per_contract=fee_per_contract)


def _symbol(value):
    return str(value).strip().upper().removesuffix(".CFFEX")


def _load_daily_directory(directory, options, *, start_day, end_day, available_time, fee_per_contract):
    import pandas as pd

    by_day = {}
    for symbol, info in options.items():
        if start_day <= info.last_trade <= end_day:
            by_day.setdefault(info.last_trade, {})[symbol] = info
    result = {}
    for day, expected in sorted(by_day.items()):
        path = directory / f"{day.isoformat()}.feather"
        if not path.is_file():
            record_issue(InputIssue("OPTION_SETTLEMENT_DAY_MISSING",
                "Missing expiry-day daily file; final value is required only for actual remaining positions",
                "WARNING", str(path), trading_day=day))
            continue
        frame = read_feather(path)
        required = {"code", "trade_date", "settlPrice"}
        if not frame.columns.is_unique or required - set(frame):
            raise ValueError(f"{path}: option daily data requires unique columns: {sorted(required)}")
        if not pd.to_datetime(frame.trade_date, errors="raise").dt.date.eq(day).all():
            raise ValueError(f"{path}: trade_date differs from daily filename")
        symbols = frame.code.map(_symbol)
        selected = frame.loc[symbols.isin(expected)].copy()
        if "exchangeCD" in selected and not selected.exchangeCD.map(
                lambda value: str(value).strip().upper() in {"CCFX", "CFFEX"}).all():
            raise ValueError(f"{path}: selected option must belong to CFFEX")
        selected = selected.rename(columns={"code": "symbol", "settlPrice": "settlement_price"})
        selected["last_trade_date"] = day
        loaded = _prepare_settlements(selected, path, expected, start_day=start_day, end_day=end_day,
            available_time=available_time, fee_per_contract=fee_per_contract)
        for symbol in sorted(set(expected) - set(loaded)):
            record_issue(InputIssue("OPTION_SETTLEMENT_CONTRACT_MISSING",
                "Expiry-day daily file has no final value for this option",
                "WARNING", str(path), symbol, day))
        result.update(loaded)
    return result


def _prepare_settlements(frame, path, options, *, start_day, end_day, available_time, fee_per_contract):
    import pandas as pd
    from bomber.framework.trader.execution.option_settlement import OptionSettlement
    from bomber.framework.trader.instrument_factory import day_ns

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
        # 普通交易日的 settlePrice 不能作为到期终值；只取最后交易日的记录。
        frame = frame.loc[pd.to_datetime(frame.TradeDate, errors="raise").dt.date.eq(frame.last_trade_date)]
    result = {}
    for number, row in enumerate(frame.to_dict("records"), 2):
        symbol = _symbol(row["symbol"])
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
        try:
            item = OptionSettlement(symbol + ".CFFEX", day_ns(info.last_trade, "15:00"),
                available_ns, row["settlement_price"], fee, str(path))
        except (ValueError, TypeError, ArithmeticError) as error:
            raise ValueError(f"{path}:{number}: {symbol}: {error}") from error
        if symbol in result and result[symbol] != item:
            raise ValueError(f"Conflicting final settlements for {symbol}")
        result[symbol] = item
    return result
