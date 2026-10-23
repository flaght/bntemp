"""已完成、未复权期货 CustomBar 的数值提取与 VWAP 校验。"""

from __future__ import annotations

from decimal import Decimal, DecimalException
from math import isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bomber.framework.market.basic.base import CustomBar


def _number(value: object, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"期货 CustomBar 字段 {field} 必须为有限数值") from exc
    if not isfinite(result):
        raise ValueError(f"期货 CustomBar 字段 {field} 必须为有限数值")
    return result


def futures_bar_values(
    event: CustomBar, *, contract_multiplier: Decimal | int | float | str,
) -> dict[str, float]:
    """提取 OHLCV、value、openint，计算并校验 vwap，返回新的数值字典。

    OHLCV 优先使用 factors 中的原始精度，缺失时使用原生 Bar；成交额
    value 和持仓量 openint（兼容 open_interest）必须由 factors 提供。
    volume 和 value 须为同周期、相同单边/双边统计口径的成交量和成交额，
    成交额单位须与价格、合约乘数匹配；累计值和单位换算由上游处理。
    不自动将 turnover 映射为 value，也不生成成交额或修复数据口径。

    保留正价格、正成交量的输入约束；零成交 bar 无法定义此口径 VWAP，
    因此拒绝。OHLC 必须与成交额使用相同的未复权价格口径，不能直接使用
    复权连续行情。本函数仅由需要此口径的调用方显式使用，不注册全局转换。
    时间标签、品种、合约筛选和模型字段由调用方处理；输入不合规时抛出
    ValueError，不修改输入数据。
    """
    try:
        multiplier = Decimal(str(contract_multiplier))
    except (DecimalException, TypeError, ValueError) as exc:
        raise ValueError("合约乘数须为有限正数，且与成交额单位一致") from exc
    if not multiplier.is_finite() or multiplier <= 0:
        raise ValueError("合约乘数须为有限正数，且与成交额单位一致")

    factors = event.factors
    missing = {"value"} - set(factors)
    if "openint" not in factors and "open_interest" not in factors:
        missing.add("openint/open_interest")
    if missing:
        raise ValueError(f"期货 CustomBar 缺少原始字段: {sorted(missing)}")

    # 显式分支，避免原始值存在时仍转换原生 Bar 的精度受限数值。
    values = {
        key: _number(
            factors[key] if key in factors else getattr(event.bar, key).as_double(), key)
        for key in ("open", "high", "low", "close", "volume")
    }
    values["value"] = _number(factors["value"], "value")
    interest_field = "openint" if "openint" in factors else "open_interest"
    values["openint"] = _number(factors[interest_field], interest_field)
    if (min(values["open"], values["high"], values["low"], values["close"]) <= 0
            or min(values["volume"], values["value"], values["openint"]) < 0):
        raise ValueError("期货 CustomBar 含无效原始值")
    if values["volume"] == 0:
        raise ValueError("成交量为零，不能计算本周期 VWAP")

    try:
        values["vwap"] = float(Decimal(str(values["value"])) /
                               Decimal(str(values["volume"])) / multiplier)
    except (DecimalException, OverflowError) as exc:
        raise ValueError("期货 CustomBar 无法计算有限 VWAP；检查数值与合约乘数") from exc
    if not isfinite(values["vwap"]) or values["vwap"] <= 0:
        raise ValueError("期货 CustomBar 含无效 VWAP")
    price_tolerance = max(1e-8, 1e-6 * values["high"])
    if (values["high"] + price_tolerance < max(
            values["open"], values["low"], values["close"], values["vwap"])
            or values["low"] - price_tolerance > min(
                values["open"], values["high"], values["close"], values["vwap"])):
        raise ValueError("计算 VWAP 不在本周期 OHLC 区间；检查成交额单位与合约乘数")
    if "vwap" in factors:
        supplied_vwap = _number(factors["vwap"], "vwap")
        if abs(supplied_vwap - values["vwap"]) > price_tolerance:
            raise ValueError("提供的 VWAP 与成交额/成交量/乘数计算结果不一致")
    return values
