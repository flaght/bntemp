# MarketStreamBinding

用途：声明某个真实合约、某类行情及其周期应由 NautilusMarketFeedAdapter 订阅并送入模拟 Backend。

- 导入路径：`from bomber.framework.trader import MarketStreamBinding`
- 实际定义：`bomber.framework.trader.runtime.market_adapter.MarketStreamBinding`，是 frozen 数据类。
- [绑定与适配器源码](../../../bomber/framework/trader/runtime/market_adapter.py) · [订阅结构](../../../bomber/framework/market/basic/base.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个类负责什么

它描述的是 Feed 到模拟撮合端的行情需求。例如“将 rb2704.SHFE 的一分钟标准 Bar 送给 Backend”。构造绑定不会连接行情、订阅网络或推进撮合；adapter.start() 才注册回调并执行订阅。

它不指定策略、data_key、目标仓位或交易客户端，也不生成行情。用于策略输入的是 DataBinding，用于目标订单方向的是 ExecutionRoute，三者不能互换。

## 2. 构造参数

```python
binding = MarketStreamBinding(
    instrument_id,
    data_type,
    bar_spec=None,
    fields=(),
    extra_params={},
)
```

上面表示调用参数；extra_params 的真实默认值由 default_factory=dict 为每个实例独立创建。

| 参数 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `instrument_id` | InstrumentId / str | 必填 | 真实合约身份；字符串会通过 InstrumentId.from_str 转换 |
| `data_type` | DataType | 必填 | 仅接受 QUOTE_TICK、TRADE_TICK、BAR |
| `bar_spec` | str / None | None | BAR 必填，例如 1-MINUTE；Tick 不得设置非空周期 |
| `fields` | tuple[str, ...] | () | 额外字段订阅声明，构造时转为 tuple |
| `extra_params` | Mapping[str, Any] | 独立空字典 | Feed 特定订阅参数，构造时浅复制为 dict |

data_type 应传枚举成员，不能指望字符串 "BAR" 自动变成 DataType.BAR。bar_spec 会去首尾空格并转大写；BAR 的空周期会报错，Tick 配置非空周期也会报错。

只支持原生 QuoteTick、TradeTick 和 Bar。CUSTOM_BAR、CUSTOM_DATA、ORDER_BOOK 不在当前模拟行情桥的支持范围，不能把 CustomBar 当成第二份标准行情重复推进模拟时钟。

## 3. 订阅和过滤有什么区别

```python
request = binding.to_subscription()
```

返回 SubscriptionRequest，包含合约、事件类型、周期、fields 和 extra_params。这个方法只生成声明，不触发 Feed 订阅。

adapter.start() 采用同样的字段调用 feed.subscribe，并将 extra_params 展开成关键字。不要在 extra_params 内再放 bar_spec、fields 等已明确传入的同名参数，否则可能产生重复关键字错误。

adapter 的本地事件匹配依据合约、事件类型以及 BarType 中的周期文本；fields 和 extra_params 不是事件过滤条件，也不负责从 CustomBar.factors 中提取模型字段。来源是否支持额外订阅参数，由具体 Feed 决定。

bar_spec="5-MINUTE" 不会把一分钟行情聚合为五分钟；必须已有对应周期的标准事件及来源支持。

## 4. 冻结、去重与可变参数

数据类字段不能直接重新赋值，但 extra_params 内部仍是可变 dict，冻结不等于递归不可变。其内容是浅复制，嵌套对象也不保证隔离。

绑定比较和哈希包含 instrument_id、data_type、bar_spec、fields，**不包含 extra_params**。因此，只改变 extra_params 的两个绑定仍可能被判为重复，adapter 会在构造时拒绝。

fields 不同的绑定可以不相等，但同一事件若匹配多个绑定，adapter 使用 any 判断，只转发一次，不为每条匹配规则重复推进。

## 5. 调用示例

```python
from bomber.framework.market.basic.base import DataType, InstrumentId
from bomber.framework.trader import MarketStreamBinding

bar_binding = MarketStreamBinding(
    "rb2704.SHFE",
    DataType.BAR,
    bar_spec=" 1-minute ",
)
assert bar_binding.instrument_id == InstrumentId.from_str("rb2704.SHFE")
assert bar_binding.bar_spec == "1-MINUTE"
assert bar_binding.to_subscription().data_type is DataType.BAR

trade_binding = MarketStreamBinding("rb2704.SHFE", DataType.TRADE_TICK)
```

当前 HybridTransformer 为全部预注册合约创建：

```python
bindings = tuple(
    MarketStreamBinding(item, DataType.BAR, "1-MINUTE")
    for item in instruments.values()
)
```

策略另用 DataBinding 订阅 CUSTOM_BAR。原生 Bar 推进模拟撮合，CustomBar 携带成交额、持仓量和模型输入，两条链路分别注册。

## 6. 常见错误

| 现象 | 重点检查 |
| --- | --- |
| BAR绑定必须提供bar_spec | 是否省略周期或只传空格 |
| 只有BAR绑定可以提供bar_spec | Tick 绑定应省略周期 |
| 不支持 CUSTOM_BAR | 伴生因子交给策略链，不作为第二份撮合行情 |
| bindings不能重复 | 是否仅改变了不参与比较的 extra_params |
| 没收到期望事件 | 合约身份、枚举类型、事件 BarType 周期和来源订阅是否一致 |
| fields 没变成模型字段 | 它是订阅参数；模型字段由 Reader/Parser 和策略 DataBinding 组织 |

数据本身的时间、OHLCV、交易日和合约有效性不由本对象校验；实际订阅和解析仍须使用相应组件。

相关回归：[行情适配器分阶段测试](../../../tests/run_market_stream_adapter.py)。实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

相关说明：[NautilusMarketFeedAdapter](NautilusMarketFeedAdapter.md)、[UnifiedStrategyRunner](UnifiedStrategyRunner.md)、[FixedInstrumentBarParser](../market/FixedInstrumentBarParser.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
