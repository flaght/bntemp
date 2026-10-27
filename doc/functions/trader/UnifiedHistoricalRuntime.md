# UnifiedHistoricalRuntime

用途：将历史Runner与模拟行情adapter组合为一次正式回测，固定撮合先于策略的事件顺序，完成回放和Backend收尾，并提供查询原生报告后的统一停止入口。

- 导入路径：`from bomber.framework.trader import UnifiedHistoricalRuntime`
- 实际定义：`bomber.framework.trader.runtime.historical.UnifiedHistoricalRuntime`。
- [Runtime和结果源码](../../../bomber/framework/trader/runtime/historical.py) · [Runner](../../../bomber/framework/trader/runner.py)
- [行情adapter](../../../bomber/framework/trader/runtime/market_adapter.py) · [模拟Backend](../../../bomber/framework/trader/execution/simulation/backend.py)
- 核对日期：2026-10-27，Asia/Shanghai。

## 1. 这个类负责什么

它是已有组件的生命周期协调器，不是数据读取器或策略实现：

```text
start：先挂adapter，再启动Runner
run：Runner历史回放 → Backend.finish → 汇总运行结果
查询：读取原生订单、成交、仓位及账户报告
stop：停止Runner与adapter，执行客户端负责停止Backend
```

不在内部创建引擎、Profile、合约、Feed、策略、执行组件或绑定。构造时应已经完成这些装配。

类属性mode固定为RuntimeMode.HISTORICAL，engine_name为"bomber.nautilus.NautilusSimExecutionBackend"。该名称是实现标识，不是构造时自动创建或强制校验Backend类型的工厂参数。

## 2. 完整构造参数

```python
def __init__(
    self,
    runtime_id: str,
    runner: UnifiedStrategyRunner,
    market_adapter: NautilusMarketFeedAdapter,
) -> None:
    ...
```

三个参数必填，没有运行区间、报告目录或引擎配置等默认参数。

| 参数 | 含义与要求 |
| --- | --- |
| `runtime_id` | 非空运行标识，strip后不能为空，但保存时保留原字符串 |
| `runner` | 已装配策略、Feed和执行客户端；mode必须是RuntimeMode.HISTORICAL成员 |
| `market_adapter` | 已绑定模拟Backend与行情；必须manage_lifecycle=False |

Runtime要求adapter不接管Feed/Backend生命周期：Feed由Runner连接，模拟Backend由执行客户端启动和停止。否则构造时直接报错。

调用方还应保证adapter与Runner实际使用同一行情链、同一个Backend，Runner和执行组件共享仓位。构造器不逐项验证全部对象关联，不能依靠参数类型注解自动发现这种接错线。

## 3. start与事件顺序

start先调用market_adapter.start，再调用runner.start。前者挂撮合回调并订阅，但不连接；后者启动执行客户端、挂策略回调并连接Feed。由此先后关系固定为：

```text
事件N进入Feed
  → adapter把标准行情交给Backend，推进已有订单撮合
  → 执行回报更新仓位与订单状态
  → Runner将策略行情交给策略
  → 策略基于事件N产生新目标和订单
后续事件 → 再推进这些新订单
```

事件N产生的新订单最早在事件N+1推进时成交；N+1是下一行情事件，不保证是下一分钟，也不保证订单一定能成交。CustomBar伴生事件用于策略，不重复作为第二份撮合行情。

在Runtime之前手动启动Runner，可能已经改变回调挂载顺序，因此应由Runtime统一启动。

## 4. run的执行过程和缓存

```python
result = runtime.run()
```

主要顺序为：

1. 若已有缓存结果，直接返回该结果，不重复回放。
2. 调用start，按正确顺序启动组件。
3. 调用runner.run_replay，取得回放摘要。
4. 若Backend提供可调用finish，调用它完成模拟运行并生成结果。
5. 读取Backend.result和回报数量，生成并缓存UnifiedHistoricalResult。

它不调用Backend.run的批量路径；当前模拟执行通过adapter逐事件推进。Runner的run_replay要求恰好一个聚合回放Feed，可包含多个文件来源。

回放或finish抛异常时会走Runtime.stop再传播。启动失败按start里的具体分支处理，不应将该类解释为对所有组件在任意阶段的失败都提供完整事务回滚；清理动作本身也可能抛错。

正常run完成后不会立即dispose引擎，保留报告查询窗口。再次run返回缓存；stop之后若已有结果，run仍只返回缓存，并不重新启动。

## 5. 返回结果与属性

```python
@dataclass(frozen=True)
class UnifiedHistoricalResult:
    replay_summary: Any
    backend_result: Any
    market_events_processed: int
    execution_reports_received: int
```

| 字段 | 含义 |
| --- | --- |
| `replay_summary` | 聚合Feed回放返回值，例如各类型事件统计 |
| `backend_result` | Backend.result()返回的原生模拟结果，具体字段由Backend决定 |
| `market_events_processed` | adapter成功转发并推进的事件数 |
| `execution_reports_received` | 优先取len(backend.reports)，没有该属性或为None时使用adapter回报计数 |

Feed摘要可能同时统计BAR和CUSTOM_BAR，而adapter只处理标准撮合行情，所以两边事件数不一定相同。执行回报数含订单状态变化，不等于订单数或成交笔数。

| 属性 | 含义 |
| --- | --- |
| `backend` | market_adapter.backend |
| `engine` | Backend有engine则返回该对象，否则None |
| `result` | 尚未run成功时为None，之后为缓存结果；这是属性，不是result()方法 |

backend_result与Runtime返回结果不是同一个层级。比如run_id通常从result.backend_result读取，不能假定result自身具有所有原生统计字段。

## 6. stop、报告与重启限制

正常stop先停止已经启动的Runner，再停止adapter，并将Runtime标记为已停止。Runner中的执行客户端停止模拟Backend，后者负责finish和dispose。

正常停止后重复stop直接返回；Runtime不能重新start。启动之前就调用stop也会将Runtime标记为已停止，不能再用同一实例开始新回测。

推荐在try中run和导出报告，在finally中stop。报告在Backend释放前查询，输入报告、策略检查点、CSV、绩效图等仍由调用入口主动保存；Runtime没有report_dir参数，也不自动写这些文件。

仅存在执行回报或结果不代表策略一定成交，当前入口另行检查模型预热、report_errors和可选require_fills等条件。

## 7. 调用示例

假设runner与adapter已经完整装配，adapter使用manage_lifecycle=False，Runner尚未启动：

```python
from bomber.framework.trader import UnifiedHistoricalRuntime


def run_and_collect(runner, adapter):
    runtime = UnifiedHistoricalRuntime("example-backtest", runner, adapter)
    try:
        result = runtime.run()
        trader = runtime.engine.trader
        orders = trader.generate_orders_report()
        fills = trader.generate_fills_report()
        positions = trader.generate_positions_report()
        assert runtime.result is result
        return result, orders, fills, positions
    finally:
        runtime.stop()
```

上例假设Backend确实拥有可查询的Nautilus引擎；使用测试替身时engine可为None。日期范围应在数据准备和来源中处理，不是传给Runtime.run。

## 8. 常见错误

| 现象 | 重点检查 |
| --- | --- |
| 只接受HISTORICAL Runner | 使用规范枚举成员，LIVE不能交给此Runtime |
| 要求manage_lifecycle=False | 生命周期由Runner和执行客户端持有 |
| 恰好一个回放源报错 | 多个文件放入同一个聚合Feed，不是多个独立replay Feed |
| run后引擎未释放 | 正常保留报告窗口，查询后应stop |
| stop后不能重新启动 | 新回测创建新Runtime及匹配生命周期组件 |
| 第二次run没有再次执行策略 | 正常返回缓存，不是重置后再回放 |
| 统计事件数比来源行数多 | 一行可能产生标准Bar及CustomBar，摘要与adapter统计口径不同 |
| 有策略信号但没有撮合事件 | adapter绑定、来源和Backend是否与Runner执行链一致 |

相关回归：[统一历史运行时测试](../../../tests/run_unified_historical_runtime.py)、[公共策略装配测试](../../../tests/test_strategy_assembly.py)。实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

相关说明：[assemble_strategy](assemble_strategy.md)、[UnifiedStrategyRunner](UnifiedStrategyRunner.md)、[NautilusMarketFeedAdapter](NautilusMarketFeedAdapter.md)、[RuntimeMode](RuntimeMode.md)、[NautilusSimExecutionBackend](NautilusSimExecutionBackend.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
