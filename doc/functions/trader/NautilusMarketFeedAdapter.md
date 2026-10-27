# NautilusMarketFeedAdapter

用途：订阅统一 Feed 的标准行情，按 MarketStreamBinding 过滤并串行调用模拟 Backend.process_market_event，衔接行情回放与模拟时钟、撮合推进。

- 导入路径：`from bomber.framework.trader import NautilusMarketFeedAdapter`
- 实际定义：`bomber.framework.trader.runtime.market_adapter.NautilusMarketFeedAdapter`。
- [适配器及绑定源码](../../../bomber/framework/trader/runtime/market_adapter.py) · [Backend 协议](../../../bomber/framework/trader/execution/ports.py)
- [正式历史运行时](../../../bomber/framework/trader/runtime/historical.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个类负责什么

同一 Feed 可以同时供策略和模拟撮合使用。适配器负责其中的撮合侧：

```text
Feed 发出 QuoteTick / TradeTick / Bar
    → 匹配合约、事件类型和 Bar 周期
    → 取得事件推进锁
    → backend.process_market_event(event)
    → 记录成功事件数和该调用返回的回报数
```

不匹配事件直接忽略，停止状态也不推进。它不计算指标、不调用策略、不提交订单、不创建账户或合约，也不将 CustomBar 拆开后再推进一次。

具体行情来源可以是文件回放或流式 Feed。适配器本身不创建网络客户端或额外工作线程，事件由 Feed 分派机制调用；它使用锁防止自身多个回调并发推进同一个模拟引擎。

## 2. 构造签名与参数

```python
def __init__(
    self,
    adapter_id: str,
    feed: MarketDataFeed,
    backend: SimExecutionBackendPort,
    bindings: Sequence[MarketStreamBinding],
    *,
    manage_lifecycle: bool = True,
) -> None:
    ...
```

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `adapter_id` | 必填 | 非空适配器标识；检查 strip 后非空，存储时保留原字符串 |
| `feed` | 必填 | 已配置来源和所需合约元信息的 MarketDataFeed |
| `backend` | 必填 | 可按标准行情事件推进的模拟 Backend，通常为 NautilusSimExecutionBackend |
| `bindings` | 必填 | 非空且无重复的 MarketStreamBinding 序列，内部保存为 tuple |
| `manage_lifecycle` | True | 是否由 adapter 管理 Backend.start/stop 及 Feed.connect/disconnect |

构造不连接、不订阅、不启动 Backend。Profile、模拟合约和来源配置须由调用方完成，不会因为给了 bindings 就自动创建它们。

去重采用 set(bindings)，因此只改变 extra_params 不足以区分两个绑定，详见 MarketStreamBinding 说明。

## 3. 两种生命周期模式

| 行为 | manage_lifecycle=True | manage_lifecycle=False |
| --- | --- | --- |
| start 注册回调及订阅 | 是 | 是 |
| start 调 Backend.start | 是 | 否，由执行客户端等外围组件负责 |
| start 连接 Feed | 是 | 否，由 Runner 等外围组件负责 |
| stop 关闭本 adapter 的事件入口 | 是 | 是 |
| stop 断开 Feed 并停止 Backend | 是 | 否，由外围生命周期负责 |

### 独立管理：默认 True

start 先挂载对应类型回调、订阅，再启动 Backend。在 Feed.connect 之前打开自身事件入口，避免某些 Feed 在连接时立刻发出的首个事件被忽略。

连接 Feed 失败时，会关闭入口、调用 Backend.stop 并标记适配器停止，随后传播异常。不要把这个清理分支泛化为所有构造、订阅或 Backend 启动失败都具有同样的自动清理行为。

stop 先关闭入口，再断开 Feed，随后在事件推进锁下调用 Backend.stop，等待已经进入的推进完成。正常停止后重复 stop 安全；已经停止的 adapter 不能再次 start。

### 外围管理：False

此时 start 只挂回调、订阅并打开入口，stop 只关闭入口并标记停止，不负责断开 Feed 或停止 Backend。它不是“禁用适配器”或“只订阅不转发”。

当前 UnifiedHistoricalRuntime 强制使用 False，因为 Backend 由 SimulationExecutionClient 启动，Feed 由 Runner 连接；否则会产生重复生命周期管理。

start 在活动状态重复调用会直接返回。尚未启动就调用 stop 会直接返回，不会把实例永久标记为停止；成功启动后再停止才进入不可重启状态。

## 4. 订阅、过滤和线程边界

适配器按 bindings 中出现的类型，分别挂载 quote、trade、bar 回调，每种类型只挂一次。收到事件后检查：

- QuoteTick / TradeTick：类型和 instrument_id 必须匹配。
- Bar：bar_type.instrument_id 匹配，并在 BarType 文本中包含对应的 `-周期-`。
- CustomBar 和其他事件：不作为此适配器的撮合输入。

fields、extra_params 传给 Feed.subscribe，不参与本地内容过滤。一个事件即使匹配多个绑定，也只调用一次 Backend。

适配器不按时间排序、不检查重复事件、不调整时间或聚合周期。若来源重发事件，或同时装配多个 adapter 指向同一 Backend，需要外围保证不会重复推进。

锁只约束该 adapter 自己的行情推进回调，不会自动保护调用方从其他线程直接调用 Backend 的全部方法，也不使多个 adapter 共享同一把推进锁。

停止后当前实现没有主动注销已挂载回调或取消订阅；回调通过入口状态停止转发。若 manage_lifecycle=False，Feed 仍可能为其他消费者继续工作，不能把 stop 理解为清除全部订阅。

## 5. 当前历史回测为何必须先挂 adapter

UnifiedHistoricalRuntime.start 先调用 adapter.start，再调用 runner.start，确保同一 Feed 上撮合回调先于策略回调：

```text
事件 N → Backend 推进已有订单撮合
       → 执行回报入账
       → Runner 分发策略行情
       → 策略据事件 N 产生新目标和订单
事件 N+1 → 后续推进这些新订单
```

这是 Runtime 组合顺序提供的约束，adapter 单独存在并不能证明策略回调一定后运行。N+1 表示后续行情事件，不一定是下一分钟，也不保证订单必然成交。

HybridTransformer 由标准 Bar 推进 Backend，CustomBar 进入策略模型链；两类事件不能各推进一次撮合。

## 6. 状态、统计和错误处理

| 接口 | 含义 |
| --- | --- |
| `start()` / `stop()` | 注册并启动入口 / 关闭入口及按配置清理，返回 None |
| `is_started` | 本 adapter 是否开放处理入口，不代表引擎已处理过行情 |
| `events_processed` | Backend 调用成功并返回后累计的事件数 |
| `reports_received` | 累加各次 Backend 返回的回报数量，不是成交笔数 |
| `last_error` | 最近一次 Backend.process_market_event 抛出的异常，初始 None |
| `register_error_handler(handler)` | 注册接收 `(exception, event)` 的同步错误回调，同一回调不重复添加 |

Backend 推进抛异常时，保存 last_error，调用错误回调，然后重新抛出；不会自动重试、跳过坏行或执行完整 stop。未成功的事件不增加计数。错误回调自身若抛异常可能影响原异常传播，应保持可控。

last_error 不覆盖所有生命周期错误，也不会因下一事件成功自动清空。reports_received 只统计本适配器调用返回的回报，不能替代 Backend 的完整回报列表，最终 finish 阶段的回报也可能不在该计数中。

## 7. 调用示例

假设 feed、backend、runner 已按实际合约正确装配；runner 使用同一个执行 Backend，尚未启动：

```python
from bomber.framework.market.basic.base import DataType, InstrumentId
from bomber.framework.trader import (
    MarketStreamBinding, NautilusMarketFeedAdapter, UnifiedHistoricalRuntime,
)

instrument_id = InstrumentId.from_str("rb2704.SHFE")
adapter = NautilusMarketFeedAdapter(
    "rb-historical-clock",
    feed,
    backend,
    (MarketStreamBinding(instrument_id, DataType.BAR, "1-MINUTE"),),
    manage_lifecycle=False,
)
runtime = UnifiedHistoricalRuntime("rb-backtest", runner, adapter)
try:
    result = runtime.run()
    orders = backend.engine.trader.generate_orders_report()
finally:
    runtime.stop()
```

Runtime.run 会完成回放并调用 Backend.finish；报告查询后才停止并释放。adapter 本身没有 run 方法，直接文件回放的加载和分发仍由 Feed/Runtime 负责。

## 8. 常见错误

| 现象 | 重点检查 |
| --- | --- |
| bindings为空或重复 | 至少声明一条有效绑定，并检查 extra_params 不参与去重 |
| 事件未进入 Backend | adapter 是否已 start，合约、事件类型和周期是否匹配 |
| 订阅成功但缺合约报错 | 模拟 Backend 的合约和 Feed 元信息是否分别注册 |
| Historical Runtime 拒绝 adapter | 是否设置 manage_lifecycle=False |
| 单个事件重复撮合 | 是否有多个推进入口，是否重复使用 CustomBar.bar |
| stop后Feed仍连接 | False 模式下由 Runner/Runtime 负责，属于预期职责分工 |
| last_error为None但启动失败 | 该属性只在 Backend.process_market_event 异常分支更新 |
| 统计回报数与成交笔数不同 | 回报含接受、拒绝、部分成交、撤单等多种事件 |

相关回归：[行情适配器测试](../../../tests/run_market_stream_adapter.py)、[统一策略装配测试](../../../tests/test_strategy_assembly.py)、[历史运行时测试](../../../tests/run_unified_historical_runtime.py)。实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

相关说明：[MarketStreamBinding](MarketStreamBinding.md)、[UnifiedStrategyRunner](UnifiedStrategyRunner.md)、[RuntimeMode](RuntimeMode.md)、[NautilusSimExecutionBackend](NautilusSimExecutionBackend.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
