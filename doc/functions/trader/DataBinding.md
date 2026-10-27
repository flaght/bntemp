# DataBinding

用途：将某个 Feed 的真实合约行情映射到策略内部的逻辑 data_key，并声明事件类型和可选 Bar 周期。

- 导入路径：`from bomber.framework.trader import DataBinding`
- 实际定义：`bomber.framework.trader.contracts.DataBinding`，是 frozen 数据类。
- [声明源码](../../../bomber/framework/trader/contracts.py) · [订阅和分发实现](../../../bomber/framework/trader/runner.py)
- 核对日期：2026-10-27，Asia/Shanghai。

## 1. 这个类负责什么

它回答“策略以哪个名字接收哪份行情”。例如来源 main-bars 发出 rb2704.SHFE 的一分钟 CustomBar，策略在 on_custom_bar(data_key, event) 中用 model-bars 识别这条输入。

构造不读取数据或订阅来源。Runner注册策略时保存声明，在start阶段调用Feed.subscribe，之后按来源、事件类型、合约和周期分发。

## 2. 构造参数

```python
@dataclass(frozen=True)
class DataBinding:
    data_key: str
    feed_id: str
    instrument_id: InstrumentId
    data_type: DataType
    bar_spec: str | None = None
```

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `data_key` | 必填 | 策略内的输入名称，strip后不能为空，但存储时不去空格 |
| `feed_id` | 必填 | Runner内注册的行情源名称，strip后不能为空 |
| `instrument_id` | 必填 | 明确的InstrumentId对象，不是品种代码 |
| `data_type` | 必填 | DataType枚举，例如BAR、CUSTOM_BAR、TRADE_TICK、QUOTE_TICK |
| `bar_spec` | None | BAR和CUSTOM_BAR必须提供周期，构造时转大写 |

本类不将字符串instrument_id自动转成对象，这一点不同于MarketStreamBinding。应显式用InstrumentId.from_str创建身份，避免Runner内绑定键与行情对象身份无法匹配。

字段注解不代表完整运行时类型验证。data_type应传枚举而不是字符串；BAR周期构造阶段只检查不为None，然后upper，不会像MarketStreamBinding一样去首尾空格并检查空白周期。调用方应直接传规范的1-MINUTE等非空描述。

## 3. 分发规则与回调

Runner首先匹配 `(feed_id, data_type, instrument_id)`，再检查BarType文本中的周期，然后将data_key传给策略事件入口。

| 事件类型 | 策略回调 |
| --- | --- |
| BAR | on_bar(data_key, bar) |
| CUSTOM_BAR | on_custom_bar(data_key, custom_bar) |
| TRADE_TICK | on_trade_tick(data_key, tick) |
| QUOTE_TICK | on_quote_tick(data_key, tick) |

StrategyTemplate还会先执行通用on_data。事件本身不被DataBinding改名、转时区、复权或重新聚合。

Tick一般省略bar_spec。当前构造器不主动禁止Tick携带周期，但Runner会尝试按BarType过滤，普通Tick不具备该结构，可能导致收不到事件；不要依赖未被构造器拒绝就认为组合受支持。

同一策略内data_key唯一。如果需要同一合约同时接收BAR与CUSTOM_BAR，应使用两个不同data_key；不同策略可以各自使用相同逻辑名称。

data_key不会自动从event.symbol推导，也不负责确认数据是否属于主力。当前HybridTransformer使用str(instrument_id)作为data_key，是该策略自己的明确约定，策略还会进一步检查其与事件身份一致。

## 4. 与其他声明的区别

| 声明 | 方向 |
| --- | --- |
| DataBinding | Feed → 策略输入 |
| MarketStreamBinding | Feed → 模拟Backend行情推进 |
| ExecutionRoute | 策略目标 → 执行客户端和合约 |

DataBinding没有fields或extra_params参数，不声明模型列名，也不直接把CustomBar送入撮合。额外字段应由Parser放入factors，策略再读取。

## 5. 调用示例

```python
from bomber.framework.market.basic.base import DataType, InstrumentId
from bomber.framework.trader import DataBinding

instrument_id = InstrumentId.from_str("rb2704.SHFE")
binding = DataBinding(
    data_key="model-bars",
    feed_id="main-bars",
    instrument_id=instrument_id,
    data_type=DataType.CUSTOM_BAR,
    bar_spec="1-minute",
)
assert binding.bar_spec == "1-MINUTE"
```

将它放入StrategyBindings.data或runner.add_strategy(data_bindings=...)。Runner还须注册main-bars对应的Feed；Feed必须真正生成并分发匹配的CustomBar。仅有标准Bar不会自动变成CustomBar。

## 6. 常见错误

| 现象 | 重点检查 |
| --- | --- |
| data_key/feed_id不能为空 | 是否为空字符串或只有空格 |
| Bar数据绑定必须提供bar_spec | BAR与CUSTOM_BAR均需周期 |
| 构造成功却收不到数据 | Feed名称、DataType、InstrumentId和周期是否匹配 |
| BAR回调有数据，CUSTOM_BAR没有 | 来源Parser是否产生伴生事件，绑定类型是否正确 |
| 同策略多个输入报重复键 | 不同绑定使用不同data_key |
| 配置5-MINUTE却仍是原始数据 | 周期只用于订阅和匹配，不提供聚合 |

未知来源通常在Runner.start检查；合约元信息注册、文件存在及字段格式由Feed/Reader/Parser负责，本类不做这些校验。

相关回归：[策略装配测试](../../../tests/test_strategy_assembly.py)、[历史运行时测试](../../../tests/run_unified_historical_runtime.py)。相关说明：[StrategyBindings](StrategyBindings.md)、[MarketStreamBinding](MarketStreamBinding.md)、[FixedInstrumentBarParser](../market/FixedInstrumentBarParser.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
