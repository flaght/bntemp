# RuntimeMode

用途：声明运行时使用历史回放还是实时事件环境，供 Runner 和 Runtime 执行模式约束。

- 导入路径：`from bomber.framework.trader import RuntimeMode`
- 实际定义：`bomber.framework.trader.contracts.RuntimeMode`，继承 str 和 Enum。
- [枚举源码](../../../bomber/framework/trader/contracts.py) · [Runner 模式判断](../../../bomber/framework/trader/runner.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 枚举成员和含义

当前只有两个成员，没有默认模式，Runner 构造时必须明确传入：

| 枚举成员 | value | 含义 |
| --- | --- | --- |
| `RuntimeMode.HISTORICAL` | `"historical"` | 使用历史数据回放环境 |
| `RuntimeMode.LIVE` | `"live"` | 使用实时事件运行环境 |

枚举本身没有时钟、网络、账户、线程或数据路径，不会因构造 LIVE 就连接真实柜台。实际能力由所选 Feed、执行客户端、Backend 和 Runtime 决定。

## 2. 构造与字符串转换

```python
from bomber.framework.trader import RuntimeMode

historical = RuntimeMode("historical")
live = RuntimeMode.LIVE
assert historical is RuntimeMode.HISTORICAL
assert live.value == "live"
assert live.name == "LIVE"
```

字符串按值精确匹配，`"HISTORICAL"`、`"LIVE"`、`"backtest"`、`"paper"`、`"simulation"` 都不是当前合法 value，不会自动去空格或转小写。

需要写入配置时明确使用 `.value`，不要假定 str(enum) 一定返回小写 value。枚举为 str 子类，但保留 Enum 的成员语义。

UnifiedStrategyRunner 内部调用 RuntimeMode(mode)，因此接受合法成员或对应小写字符串。部分 Runtime 直接以 `is RuntimeMode.HISTORICAL` 检查 runner.mode，不会替任意自定义 Runner 把普通字符串转成枚举；应提供规范成员。

## 3. 当前代码实际使用它限制什么

| 位置 | 约束 |
| --- | --- |
| `UnifiedStrategyRunner.run_replay()` | 只允许 HISTORICAL，LIVE 调用会报 RuntimeError |
| `UnifiedHistoricalRuntime` | 要求 runner.mode 为 HISTORICAL |
| `SimpleReplayRuntime` | 要求 HISTORICAL，但本身只做轻量回放，不提供模拟撮合 |
| Runner 添加 DynamicExecutionRoute | 当前只允许 HISTORICAL；LIVE 因恢复、持久化等能力边界明确拒绝 |
| 部分实时专用 Runner/Runtime | 要求 LIVE，并执行自身来源、生命周期和状态约束 |
| 固定目标继续执行的部分逻辑 | LIVE 额外检查账户已对账；其他健康、恢复和过期条件仍由对应实现判断 |

HISTORICAL 不代表已经具备账户、手续费和撮合统计；例如 SimpleReplayRuntime 与正式 UnifiedHistoricalRuntime 都属历史模式，但后者还组合了模拟 Backend。

LIVE 也不代表一定发送真实订单。它可以配记录型客户端，或按明确装配消费实时行情并进行模拟执行；真实订单能力取决于实际执行客户端和连接边界。

## 4. 与 Backend.kind、Profile 和日历的区别

| 概念 | 回答的问题 |
| --- | --- |
| `RuntimeMode` | 运行使用历史回放还是实时事件环境？ |
| `ExecutionBackendKind` | 具体执行后端是 SIMULATION 还是 LIVE？ |
| `CtpFuturesBasicProfile` 等 | 模拟账户及交易规则是什么？ |
| `SessionCalendar` | 哪些时段可交易，分钟和夜盘属于哪一天？ |

这些是不同维度，枚举不会自动替调用方选择或构造其余组件。把 Runner 的 mode 改成 LIVE 也不会把模拟账户升级为真实账户，更不会自动安装交易日历、恢复状态或持久化。

模式不直接决定行情时间标签 start/end，也不改变纳秒单位或某根 Bar 的 ts_event/ts_init；这些由行情来源、准备规则和实际运行时处理。

## 5. 调用示例

```python
from bomber.framework.trader import RuntimeMode, UnifiedStrategyRunner

historical_runner = UnifiedStrategyRunner(RuntimeMode.HISTORICAL)
live_runner = UnifiedStrategyRunner("live")
assert historical_runner.mode is RuntimeMode.HISTORICAL
assert live_runner.mode is RuntimeMode.LIVE
```

这里只构造 Runner，不会启动行情或交易。真实装配还需要策略、Feed、客户端、状态对象和对应的运行时。

不存在 RuntimeMode.SIMULATION 或 RuntimeMode.BACKTEST。需要描述历史模拟撮合时，通常组合 HISTORICAL Runner 与模拟 Backend；不要自行添加未经实现支持的模式字符串。

## 6. 常见错误与入口

| 现象 | 重点检查 |
| --- | --- |
| RuntimeMode构造ValueError | 是否把成员名称的大写字符串误当成小写value，是否有空格 |
| LIVE无法run_replay | 使用正确事件运行方式，文件历史回放应选择HISTORICAL |
| Runtime拒绝自定义Runner.mode | 是否提供了规范枚举成员而非未转换字符串 |
| HISTORICAL没有成交统计 | 是否仅配置轻量回放或记录客户端，是否真正装配模拟撮合 |
| LIVE不能注册动态换月路由 | 当前通用Runner明确不支持该组合，不由换一个枚举值解决 |

相关实现：[Runtime 协议](../../../bomber/framework/trader/runtime/base.py)、[轻量回放](../../../bomber/framework/trader/runtime/replay.py)、[正式历史运行时](../../../bomber/framework/trader/runtime/historical.py)。相关回归：[历史运行时测试](../../../tests/run_unified_historical_runtime.py)、[策略装配测试](../../../tests/test_strategy_assembly.py)。

相关说明：[UnifiedStrategyRunner](UnifiedStrategyRunner.md)、[NautilusMarketFeedAdapter](NautilusMarketFeedAdapter.md)、[NautilusSimExecutionBackend](NautilusSimExecutionBackend.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
