"""显式读取CFFEX股指期货最终交割结算价；不以普通收盘价代替。"""
from decimal import Decimal
import re

from .option_settlements import _load_settlements


def add_future_settlement_arguments(parser):
    from pathlib import Path
    parser.add_argument("--future-settlements", type=Path,
        help="股指期货最终交割结算CSV/Parquet/Feather，或YYYY-MM-DD.feather日行情目录")
    parser.add_argument("--future-settlement-fee-per-contract", type=Decimal, default=Decimal(0),
        help="每手期货到期结算费用，文件fee_per_contract优先；不重复收取普通交易费")


def load_future_settlements(path, futures, *, start_day, end_day, available_time="16:00",
        fee_per_contract=0):
    """文件格式复用期权读取器；仅读取fut_basic.last_trade对应日期。

    标准文件列symbol,last_trade_date,settlement_price；日行情固定code,trade_date,settlePrice。
    只接受IF/IH/IM现金交割品种；商品期货实物交割不在此接口范围。
    """
    if any(not re.fullmatch(r"(?:IF|IH|IM)\d{4}", key) for key in futures):
        raise ValueError("期货现金交割读取器仅支持CFFEX股指IF/IH/IM")
    return _load_settlements(path, futures, start_day=start_day, end_day=end_day,
        available_time=available_time, fee_per_contract=fee_per_contract,
        asset_kind="future")
