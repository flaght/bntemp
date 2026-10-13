"""回测原生合约构建；期权保证金采用明确标注的基础近似。"""
from decimal import Decimal
import pandas as pd

from bomber.model import Price, Quantity, Symbol, Venue
from bomber.model.currencies import CNY
from bomber.model.enums import AssetClass, OptionKind
from bomber.model.identifiers import InstrumentId
from bomber.model.instruments import FuturesContract, OptionContract
from market.basic.base import InstrumentMeta


def precision(tick):
    return max(0, -tick.normalize().as_tuple().exponent)


def day_ns(day, clock="00:00"):
    return int(pd.Timestamp(f"{day} {clock}", tz="Asia/Shanghai").value)


def make_option(info, margin_init, margin_maint, index_code):
    places = precision(info.tick)
    identifier = InstrumentId.from_str(info.symbol + ".CFFEX")
    return OptionContract(
        instrument_id=identifier, raw_symbol=Symbol(info.symbol), asset_class=AssetClass.INDEX,
        currency=CNY, price_precision=places,
        price_increment=Price.from_str(format(info.tick.normalize(), "f")),
        multiplier=Quantity.from_str(format(info.multiplier.normalize(), "f")), lot_size=Quantity.from_int(1),
        underlying=index_code, option_kind=OptionKind.CALL,
        strike_price=Price.from_str(f"{info.strike:.{places}f}"),
        activation_ns=day_ns(info.list_day), expiration_ns=day_ns(info.last_day, "15:00"),
        margin_init=margin_init, margin_maint=margin_maint,
        ts_event=0, ts_init=0, exchange="CFFEX",
        info={"profile": "OPTION_VEGA_BASIC", "expiry_policy": "PRE_EXPIRY_FLAT"},
    )


def make_future(symbol, row, margin_init, margin_maint):
    tick = row["minChgPriceNum"]
    return FuturesContract(
        instrument_id=InstrumentId.from_str(symbol + ".CFFEX"), raw_symbol=Symbol(symbol),
        asset_class=AssetClass.INDEX, currency=CNY, price_precision=precision(tick),
        price_increment=Price.from_str(format(tick.normalize(), "f")),
        multiplier=Quantity.from_str(format(row["contMultNum"].normalize(), "f")),
        lot_size=Quantity.from_int(1), underlying=str(row["code"]).upper(),
        activation_ns=day_ns(row["listDate"]), expiration_ns=day_ns(row["lastTradeDate"], "15:00"),
        margin_init=margin_init, margin_maint=margin_maint, ts_event=0, ts_init=0, exchange="CFFEX",
    )


def instrument_meta(contract):
    return InstrumentMeta(contract.id, price_precision=contract.price_precision, size_precision=0,
                          price_increment=Decimal(str(contract.price_increment)),
                          multiplier=Decimal(str(contract.multiplier)), currency="CNY", exchange="CFFEX")
