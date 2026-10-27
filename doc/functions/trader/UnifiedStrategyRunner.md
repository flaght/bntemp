# UnifiedStrategyRunner

用途：将策略、行情源和执行客户端装配为统一事件与目标链，负责策略行情分发、目标存储、账户目标汇总、执行路由和执行事件回传。

- 导入路径：`from bomber.framework.trader import UnifiedStrategyRunner`
- 实际定义：`bomber.framework.trader.runner.UnifiedStrategyRunner`。
- [Runner 源码](../../../bomber/framework/trader/runner.py) · [绑定与目标结构](../../../bomber/framework/trader/contracts.py)
- [策略模板](../../../bomber/framework/trader/template.py) · [目标与仓位组件](../../../bomber/framework/trader/portfolio.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个类负责什么

策略以 data_key 识别输入，以 target_key 表达目标，不直接持有 Feed 或柜台客户端。Runner 将这些逻辑名称连接到真实来源和合约：

```text
Feed 行情 → DataBinding → 策略 on_bar/on_custom_bar 等回调
策略目标 → TargetStore → ExecutionRoute → PortfolioCoordinator
         → ExecutionRequest → 执行客户端 → Backend
执行回报 → 客户端先更新状态与归属 → Runner 分发策略订单/成交事件
```

Runner 不直接实现撮合、手续费、保证金或模型推理，也不把目标当作已成交仓位。正式回测需要与 Backend、行情 adapter 和 UnifiedHistoricalRuntime 配合。

## 2. 构造签名与参数

```python
def __init__(
    self,
    mode: RuntimeMode | str,
    position_provider: PositionProvider | None = None,
    *,
    target_store: TargetStore | None = None,
    portfolio_coordinator: PortfolioCoordinator | None = None,
    position_manager: PositionManager | None = None,
    market_health_gate: MarketHealthGate | None = None,
    roll_coordinator: SafeRollCoordinator | None = None,
) -> None:
    ...
```

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `mode` | 必填 | HISTORICAL/LIVE 或对应小写字符串，内部转为 RuntimeMode |
| `position_provider` | None | 策略逻辑仓位查询接口；默认使用内部 PositionManager |
| `target_store` | None | 目标版本及PATCH/REPLACE快照存储，默认新建 |
| `portfolio_coordinator` | None | 多策略贡献到客户端/合约目标的汇总器，默认新建 |
| `position_manager` | None | 账户仓位、在途量和归属状态，默认新建 |
| `market_health_gate` | None | 行情健康及目标准入闸门，默认新建 |
| `roll_coordinator` | None | 动态换月协调状态，默认新建 |

除 mode 和 position_provider 外，其余参数只能按名称传入。实现对这些可选对象采用 `对象 or 默认对象`，通常应传正常真值的组件实例。

构造不会启动网络或执行器。与 build_simulation_execution 组合时，显式传 `position_manager=execution.positions`；否则策略与执行端可能持有不同仓位状态，公共 assemble_strategy 会拒绝这种装配。

position_provider 只替换逻辑 position 查询，不会同时替换持仓管理器、执行回报记账或风控仓位来源。

## 3. 注册来源、客户端和策略

```python
runner.add_data_feed(feed_id, feed)
runner.add_execution_client(client)
runner.add_market_observer(observer)
runner.add_strategy(
    strategy,
    data_bindings=data_bindings,
    execution_routes=execution_routes,
    time_feed_ids=(),
)
```

| 声明 | 指定什么 |
| --- | --- |
| feed_id | Runner 内的行情源名称，必须非空且不重复 |
| client.client_id | 执行客户端身份，必须非空且不重复 |
| DataBinding | 策略data_key到 feed_id、事件类型、真实合约和可选周期 |
| ExecutionRoute | 策略target_key到客户端和真实合约 |
| time_feed_ids | 需要向该策略发独立 on_time 的来源，须具备register_time_handler能力 |
| market observer | 实现on_market_event(event)的旁路观察者，例如风险参考价存储 |

同一策略 strategy_id 不能重复；策略内 data_key、target_key 和时钟来源声明不能重复。注册方法不一定立即验证来源与客户端均已存在，start 会做最终配置检查。

这些注册方法在 Runner 已启动时均禁止修改。一个 Runner 可管理多策略和多来源，但 run_replay 只接受一个具有 replay 方法的聚合回放源。

### 三种绑定分清方向

| 类型 | 方向 |
| --- | --- |
| MarketStreamBinding | Feed → 模拟撮合 adapter |
| DataBinding | Feed → 某个策略的逻辑data_key |
| ExecutionRoute | 某个策略的逻辑target_key → 执行客户端/真实合约 |

当前 HybridTransformer 的 adapter 订阅标准 BAR，策略 DataBinding 订阅 CUSTOM_BAR，执行路由映射真实合约目标。策略 data_key 可以恰好等于合约字符串，但它仍是策略输入别名。

## 4. start、stop 与回放生命周期

start 的主要顺序为：

1. 检查至少有策略，且引用的来源、客户端和时钟能力均有效。
2. 向支持执行事件回调的客户端注册监听，启动客户端。
3. 挂载 Feed 行情/健康/时钟回调，并按 DataBinding 订阅。
4. 为策略绑定运行上下文，执行策略启动回调。
5. 连接所有 Feed，成功后标记 Runner 已启动。

重复 start 在活动状态直接返回。Feed 连接阶段失败时会调用 stop 后再传播；不要将该分支理解为任何更早的客户端或策略启动异常都具备完整自动回滚。

stop 按来源、策略、客户端的逆序分别执行 disconnect、策略stop/unbind、客户端stop，再清除启动标志。它没有统一销毁所有状态或注销全部回调，stop 是否可以无副作用重复、能否再次启动，还取决于具体 Feed、策略和客户端。已经完成或释放的模拟 Backend 不支持复用为新回测。

```python
summary = runner.run_replay()
```

run_replay 只允许 HISTORICAL，未启动时自动 start，再要求恰好一个具有 replay() 的来源并调用它，返回该来源的回放摘要。它不自动调用 Backend.finish 或 stop，也不是原生引擎回测结果接口。

需要正式结果、正确的撮合回调顺序和收尾时，使用 UnifiedHistoricalRuntime。不要把再次调用 run_replay 理解为自动重置账户与策略后的全新回测。

## 5. 行情如何进入策略

Feed 回调调用 `publish(feed_id, event)`：

1. 识别 TradeTick、QuoteTick、Bar、CustomBar 和真实合约身份。
2. 保存该合约最近事件时间，通知全部市场观察者。
3. 如有动态路由，按当前合约行情尝试推进换月。
4. 按 `(feed_id, data_type, instrument_id)` 找到绑定，进一步匹配Bar周期。
5. 调用策略的事件分派，先on_data，再对应的on_trade_tick/on_quote_tick/on_bar/on_custom_bar。

观察者在策略回调前运行，因此 execution.prices 可先更新参考价。Runner 只路由标准事件，不负责解析Feather、列名映射、时区或start/end时间转换。

`publish_time(feed_id, ts_event)` 只发给显式声明该时钟来源的策略；Runner 未启动时忽略。声明time_feed_ids不会自动从每根Bar推导独立时钟事件。

同一Feed的撮合回调与Runner策略回调顺序仍由外围挂载顺序决定，Runner单独不能保证下一事件成交；正式Historical Runtime先挂adapter再启动Runner以固定此顺序。

## 6. 目标如何变成账户请求

策略通常通过 set_target/set_targets 向上下文提交 TargetPortfolio。Runner 对静态路由的处理为：

1. 校验策略及target_key已有路由。
2. 先检查行情健康准入，避免被闸门拒绝的目标占用版本或污染组合状态。
3. TargetStore检查策略revision，物化PATCH或REPLACE完整快照。
4. 通过路由转换为 `(client_id, instrument_id)` 目标。
5. PortfolioCoordinator保存策略贡献并汇总账户净目标，移除的已知腿向下游显式发送0。
6. 向受影响客户端提交ExecutionRequest，携带逻辑目标、组合版本及策略归属等信息。

例如两个策略在同一客户端对同一合约分别贡献 +2 和 -1，静态净额汇总为 +1；实际订单仍由执行客户端结合现有仓位、在途量和风控规划。Runner自身不根据目标直接增加已成交仓位。

submit使用锁串行化目标保存、组合净额与客户端发布，不代表多客户端报单是一个可回滚事务。后续客户端提交失败不保证所有已保存静态目标和已发送请求自动撤回。

PATCH/REPLACE、revision、deadline和实际订单执行是不同概念。Runner会将deadline传递给请求，是否在具体发送时执行过期校验须看对应执行链，不宜仅凭目标字段存在就推断所有路径都已执行截止保护。

## 7. 仓位与执行事件查询

| 方法 | 返回及范围 |
| --- | --- |
| position(strategy_id, target_key) | 通过PositionProvider查询策略逻辑归属仓位，Decimal |
| account_position(strategy_id, target_key) | 固定路由对应的账户净仓；受客户端对账模式限制 |
| working_quantity(strategy_id, target_key) | 固定路由对应的账户有符号在途量 |
| strategy_working_quantity(strategy_id, target_key) | 固定路由该策略/目标归属的在途量 |

账户查询不是策略独占仓位。对于账户对账off的客户端，account_position明确拒绝，要求使用策略position；observe模式返回已取得的账户观测。动态路由可能同时涉及新旧合约，不能用固定路由账户查询方法隐式挑一条腿。

支持执行事件端口的客户端在状态入账后，将标准OrderUpdateEvent/FillEvent回传Runner。Runner检查客户端、策略、路由及归属，必要时按冻结分配计划拆分通知，最终调用策略执行回调。无可信归属事件或尚未启动的策略不会被当作正常事件静默接受。

记录型客户端只记录目标，不应因此伪造成交和策略持仓。

## 8. 进阶接口及当前边界

| 接口或属性 | 作用 |
| --- | --- |
| target_store / portfolio_coordinator / position_manager | 返回实际共享状态对象，供明确的状态管理和检查点装配 |
| market_health_gate / roll_coordinator | 返回行情准入和换月协调组件 |
| market_health_snapshot(strategy_id) | 查询策略关联来源的健康快照 |
| confirm_market_recovery(strategy_id, *, operator, reason) | 显式提交恢复确认，具体门控由MarketHealthGate执行 |
| refresh_dynamic_routes(as_of_ns, *, trigger_instrument_id=None) | 根据截至该时刻可见的合约选择推进动态路由 |
| continue_execution_target(strategy_id, target_key, as_of_ns, *, trigger_instrument_id=None) | 在明确后续合约行情下推进保留的固定路由目标，返回是否发出请求 |

动态路由当前只允许HISTORICAL、单逻辑目标并独占执行客户端，不能与其他策略在该客户端上共享；LIVE注册会明确拒绝。不是改变RuntimeMode就能获得生产级动态换月恢复能力。

continue_execution_target不是publish默认触发的自动重试。它要求固定路由、独占客户端、正确真实行情触发及更新时刻，并受健康、恢复、截止时间、在途量等检查限制；LIVE还要求账户已对账。

Runner不自动建立持久化文件、不完成交易日切换、不安装SessionCalendar，也不保证全部策略回调在独立线程执行。目标提交锁不是整个框架的通用并发调度器。

## 9. 调用示例

### 构造并共享执行仓位

```python
from bomber.framework.trader import RuntimeMode, UnifiedStrategyRunner


def build_runner(execution):
    runner = UnifiedStrategyRunner(
        RuntimeMode.HISTORICAL,
        position_manager=execution.positions,
    )
    runner.add_market_observer(execution.prices)
    return runner
```

执行组件须预先由build_simulation_execution等方式正确创建。若随后使用assemble_strategy，公共装配会负责注册观察者，不需重复手动操作。

### 明确绑定策略和客户端

假设feed、strategy、execution和instrument_id已准备好，来源可提供一分钟CustomBar和对应原生Bar：

```python
from bomber.framework.market.basic.base import DataType
from bomber.framework.trader import DataBinding, ExecutionRoute
from bomber.framework.trader.assembly import StrategyBindings, assemble_strategy

runner = UnifiedStrategyRunner(
    RuntimeMode.HISTORICAL,
    position_manager=execution.positions,
)
bindings = StrategyBindings(
    data=(DataBinding("model-bars", "main-bars", instrument_id,
                      DataType.CUSTOM_BAR, "1-MINUTE"),),
    execution=(ExecutionRoute("position", execution.client.client_id, instrument_id),),
)
assemble_strategy(
    runner, strategy,
    feeds={"main-bars": feed},
    execution=execution,
    bindings=bindings,
)
```

策略须通过data_key="model-bars"处理CustomBar，用target_key="position"发布目标。随后建立标准BAR的MarketStreamBinding与adapter，交给UnifiedHistoricalRuntime启动运行，而不是在adapter挂载前手动runner.start。

## 10. 常见错误与排查

| 现象 | 重点检查 |
| --- | --- |
| 没有配置策略/未知行情源/未知客户端 | 是否在start前注册，绑定名称是否对应 |
| 重复data_key或target_key | 同策略内输入键和路由键分别必须唯一 |
| 策略收到BAR却没有on_custom_bar | DataBinding.data_type与来源事件是否匹配 |
| 策略仓位始终不变 | 是否只记录目标、是否收到实际归属成交、Runner与客户端是否共享状态 |
| LIVE无法run_replay | 历史回放只支持HISTORICAL |
| 多个回放源报错 | 将多文件聚合到一个具有replay方法的Feed |
| 同bar成交或撮合时钟重复推进 | 检查adapter挂载顺序及是否重复转发CustomBar.bar |
| 目标版本倒退或重复 | 检查策略revision与检查点恢复；目标是有版本的快照 |
| 已有目标却没有继续下单 | 目标存储不等于自动重试，检查明确的执行推进与状态条件 |
| stop后无法重新运行 | 具体Backend和Runtime可能已经终止，不能只看Runner启动标志 |

相关回归：[统一策略装配测试](../../../tests/test_strategy_assembly.py)、[历史运行时测试](../../../tests/run_unified_historical_runtime.py)、[动态路由测试](../../../tests/run_dynamic_routes.py)、[行情健康闸门测试](../../../tests/run_market_health_gate.py)。实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

相关说明：[RuntimeMode](RuntimeMode.md)、[MarketStreamBinding](MarketStreamBinding.md)、[NautilusMarketFeedAdapter](NautilusMarketFeedAdapter.md)、[build_simulation_execution](build_simulation_execution.md)、[RiskLimits](RiskLimits.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
