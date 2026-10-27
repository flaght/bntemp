# futures_bar_values

用途：从已完成、未复权的期货CustomBar中提取OHLCV、成交额和持仓量，按合约乘数计算本周期VWAP并校验单位与价格区间，返回新的数值字典。

- 导入路径：`from bomber.framework.market.futures import futures_bar_values`
- [函数源码](../../../bomber/framework/market/futures.py) · [生成伴生字段的Parser](../../../bomber/framework/market/replay/parsers/bar/mapped.py)
- 核对日期：2026-10-27，Asia/Shanghai。

## 1. 这个函数负责什么

策略收到CustomBar后，原生Bar已经按合约精度格式化，而模型可能需要来源数值精度、成交额和持仓量。本函数统一读取这些字段，并用成交额、成交量和乘数恢复本周期成交均价。

它不读文件、不查询合约条款、不选择主力、不复权、不修改输入事件，也不自动挂载到所有Bar。需要此数值口径的策略显式调用。

函数假设输入已经是正确交易合约、已完成且可用的未复权行情。它没有检查ts_event、ts_init、交易时段或资料可见性；这些由来源、准备和策略逻辑负责。

## 2. 签名与参数

```python
def futures_bar_values(
    event: CustomBar,
    *,
    contract_multiplier: Decimal | int | float | str,
) -> dict[str, float]:
    ...
```

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `event` | 必填 | 有factors及底层bar的CustomBar，或满足相同接口的对象 |
| `contract_multiplier` | 必填关键字 | 合约乘数，转Decimal后要求为有限正数，不能漏传或用0 |

当前函数按对象接口访问字段，不做CustomBar的isinstance检查。若输入结构根本不符合约定，可能产生AttributeError等对象错误；字段业务校验通常抛ValueError。

乘数不从event.bar、InstrumentMeta或全局品种配置推导。调用方应显式传入与当前合约、成交额单位一致的乘数。

## 3. 输入字段优先级

| 输出字段 | 获取顺序 |
| --- | --- |
| open/high/low/close/volume | 对每一列，factors中有同名键则使用；否则取event.bar对应原生数值的as_double() |
| value | 必须由factors["value"]提供 |
| openint | 优先factors["openint"]，不存在时兼容factors["open_interest"] |
| vwap | 始终按value/volume/multiplier计算；若factors有vwap则额外检查一致性 |

优先级按键是否存在决定，不是按值是否有效。若factors["close"]存在但为None或NaN，会失败，不会悄悄回退到底层Bar；openint和open_interest同时存在时，即使openint无效，也不会自动改用后者。

只有OHLCV全都在factors齐全时才完全不需要访问底层Bar数值。可以逐列混合来源，但必须确保它们属于相同周期和相同价格口径。

turnover、amount不自动当成value。来源叫amount时，应由FixedInstrumentBarParser(value="amount")明确映射。持仓量来源叫openint时，Parser可映射为open_interest，本函数再统一输出openint。

trade_time、code、合约身份、时间戳和质量标记都不在本函数的返回字段中。模型需要它们时由策略另外加入。

## 4. VWAP公式和单位约定

```text
vwap = 本周期成交额value ÷ 本周期成交量volume ÷ contract_multiplier
```

例如价格约101、成交量2手、乘数10、成交额2020，则VWAP=2020/2/10=101。不能省略乘数，否则会得到1010，明显偏离该分钟价格区间。

value和volume必须属于同一周期，并具有相同单边/双边统计口径；成交额计价单位应与价格和乘数匹配。若来源以万元记录成交额，或使用累计成交额/累计成交量，应先在上游完成单位与增量转换。

函数不自动判断某个品种、交易所或供应商采用什么统计口径，也不根据不合理结果自动乘除10000或2。

OHLC应为与成交额一致的真实未复权价格，不能将复权OHLC与未复权成交额直接混合。VWAP检查能发现一些明显冲突，但通过数值检查不代表已证明来源没有未来信息或单位声明完全正确。

### 已有vwap的处理

即使提供factors["vwap"]，仍必须提供value、volume和乘数，函数不会用已有vwap绕过计算。返回结果保存计算出的VWAP，来源vwap只作为一致性检查。

## 5. 数值校验与容差

所有提取字段先转float并要求有限数，不能是NaN或Infinity。OHLC须严格大于零；volume、value和openint先检查非负，随后volume为零明确失败，因为本周期VWAP无法定义。

value为零、volume为正时，计算VWAP为零，也会因VWAP必须为有限正数而失败。openint允许零，成交量可为正小数，本函数不要求整数手数。

计算使用已数值化字段转字符串后的Decimal除法，再返回float；这不能恢复之前float转换已经丢失的任意高精度。

价格容差为：

```text
price_tolerance = max(1e-8, 1e-6 × high)
```

在此容差下检查high覆盖open、low、close、vwap，low不高于open、high、close、vwap的最小值。若来源同时提供vwap，其与计算值的绝对差也不得大于该容差。

因此不是要求十进制文本逐位完全相等；轻微浮点误差可以通过，错误乘数、成交额单位或OHLC关系通常会失败。

零成交量能够通过其他基础Bar读取或解析规则，并不意味着能通过本函数。当前调用方选择的是必须计算本周期成交VWAP的模型输入口径。

## 6. 返回结果

返回新的dict[str, float]，固定包含八个字段：

```python
{
    "open": 100.0,
    "high": 102.0,
    "low": 99.0,
    "close": 101.0,
    "volume": 2.0,
    "value": 2020.0,
    "openint": 20.0,
    "vwap": 101.0,
}
```

原event.factors不会被添加vwap或改名，底层Bar也不被修改。它不是Bar对象、DataFrame或模型预测结果。

## 7. 调用示例

### 内存字段计算示例

下面用满足相同接口的对象演示，不需要创建原生引擎；OHLCV全部在factors中，因此不访问底层Bar。

```python
from types import SimpleNamespace
from bomber.framework.market.futures import futures_bar_values

event = SimpleNamespace(
    bar=object(),
    factors=dict(open=100, high=102, low=99, close=101,
                 volume=2, value=2020, open_interest=20),
)
values = futures_bar_values(event, contract_multiplier=10)
assert values["vwap"] == 101.0
assert values["openint"] == 20.0
assert "vwap" not in event.factors
```

### 当前模型输入中的使用

假设event为已通过策略合约和时间筛选的真实CustomBar，predictor与config已准备：

```python
values = futures_bar_values(
    event,
    contract_multiplier=config.contract_multiplier,
)
trade_time = event.factors.get("trade_time")
if trade_time is None:
    raise ValueError("模型 CustomBar 必须保留原始 trade_time 标签")
model_bar = {"trade_time": trade_time, "code": config.product, **values}
prediction = predictor.on_bar(model_bar)
```

trade_time和code由策略补充，不是本函数生成。FixedInstrumentBarParser应显式映射成交额与持仓量，并在需要较高源精度时使用preserve_raw_ohlcv=True。

## 8. 常见错误与排查

| 现象 | 重点检查 |
| --- | --- |
| 合约乘数须为有限正数 | 是否漏传、非数值、0、负数、NaN或Infinity |
| 缺少原始字段value | 是否只提供amount/turnover但没显式映射 |
| 缺少openint/open_interest | Parser是否配置持仓量来源列 |
| 字段必须为有限数值 | factors优先字段是否有None、坏字符串、NaN、Infinity或过大数值 |
| 含无效原始值 | 价格非正，或成交量、成交额、持仓量为负 |
| 成交量为零 | 本周期不能定义该口径VWAP，不能自动借用上根价格 |
| VWAP不在OHLC区间 | 检查成交额单位、乘数、增量与累计、单双边口径和复权价格混用 |
| 提供VWAP与计算结果不一致 | 来源VWAP是否真为同周期同口径成交均价 |
| 返回没有时间或品种 | 这只返回八个数值字段，时间与code由调用方补充 |

相关回归：[期货字段提取测试](../../../tests/test_futures_bar_values.py)、[Bar解析器与模型字段回归](../../../tests/test_bar_parsers.py)。实际调用：[HybridTransformer策略](../../../demos/10_hybrid_transformer/strategy.py)。

相关说明：[FixedInstrumentBarParser](FixedInstrumentBarParser.md)、[BarReadSpec](../dataprep/BarReadSpec.md)、[CtpFuturesBasicProfile](../trader/CtpFuturesBasicProfile.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
