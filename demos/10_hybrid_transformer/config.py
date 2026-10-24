"""读取按品种声明的信号函数、参数与持仓周期。"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping



@dataclass(frozen=True)
class ProductSignalConfig:
    signal_id: str
    signal_method: str
    signal_params: Mapping[str, object]
    hold_bars: int

    def __post_init__(self) -> None:
        for field in ('signal_id', 'signal_method'):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{field} 须为非空字符串')
            object.__setattr__(self, field, value.strip())
        if not isinstance(self.signal_params, Mapping):
            raise ValueError('signal_params 须为参数字典')
        if type(self.hold_bars) is not int or self.hold_bars < 1:
            raise ValueError('hold_bars 须为正整数')
        object.__setattr__(self, 'signal_params', MappingProxyType(dict(self.signal_params)))


def load_product_signal(path: str, product: str) -> ProductSignalConfig:
    """不按品种猜测信号参数，未配置的品种明确报错。"""
    with Path(path).open(encoding='utf-8') as source:
        data = json.load(source)
    
    if not isinstance(data, dict):
        raise ValueError('信号配置须为 品种 -> 配置 的 JSON 对象')
    products = {}

    for key, value in data.items():
        code = key.strip().upper()
        if not code.isalpha() or code in products:
            raise ValueError(f'品种名无效或重复: {key}')
        products[code] = value
    code = product.strip().upper()
    if code not in products:
        raise ValueError(f'信号配置中没有品种 {code}')
    entry = products[code]
    fields = {'signal_id', 'signal_method', 'signal_params', 'hold_bars'}
    if not isinstance(entry, dict) or fields - entry.keys():
        raise ValueError(f'{code} 须配置 signal_id、signal_method、signal_params、hold_bars')
    return ProductSignalConfig(**{key: entry[key] for key in fields})