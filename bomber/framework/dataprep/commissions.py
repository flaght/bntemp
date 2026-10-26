"""回测入口共用的手续费命令行配置。"""
from decimal import Decimal

from bomber.framework.fees import CommissionConfig


def add_commission_arguments(parser):
    parser.add_argument("--commission-multiplier", type=Decimal, default=Decimal(1),
        help="交易手续费系数；标准手续费乘此值后扣费，默认1")
    parser.add_argument("--commission-product-multiplier", action="append", default=[], metavar="PRODUCT=VALUE",
        help="按品种覆盖手续费系数，可重复，例如RB=1.2")
    parser.add_argument("--commission-contract-multiplier", action="append", default=[], metavar="CONTRACT=VALUE",
        help="按合约覆盖手续费系数，可重复，例如rb2704.SHFE=1.5")
    parser.add_argument("--commission-source", choices=("auto", "metadata", "fixed"), default="auto",
        help="auto优先fut_basic、缺失时沿用每手配置；metadata要求期货费用完整；fixed使用每手配置")
    parser.add_argument("--delivery-fee-multiplier", type=Decimal, default=Decimal(1),
        help="交割手续费独立系数预留；普通成交不扣交割费用")


def _overrides(values):
    result = {}
    for item in values:
        key, separator, value = item.partition("=")
        key = key.strip().upper()
        if not separator or not key or key in result:
            raise ValueError(f"手续费系数覆盖须为不重复的代码=数值: {item!r}")
        result[key] = value
    return result


def commission_config_from_args(args):
    return CommissionConfig(
        commission_multiplier=getattr(args, "commission_multiplier", 1),
        commission_product_multipliers=_overrides(getattr(args, "commission_product_multiplier", ())),
        commission_contract_multipliers=_overrides(getattr(args, "commission_contract_multiplier", ())),
        delivery_fee_multiplier=getattr(args, "delivery_fee_multiplier", 1),
        commission_source=getattr(args, "commission_source", "auto"))
