# assemble_strategy

用途：按照StrategyBindings，将一份策略、Feed集合和执行组件注册进已有Runner，同时校验共享仓位与固定角色守卫关系。

- 导入路径：`from bomber.framework.trader.assembly import assemble_strategy`
- [函数及绑定声明源码](../../../bomber/framework/trader/assembly.py) · [Runner注册逻辑](../../../bomber/framework/trader/runner.py)
- [固定角色在线Runner](../../../bomber/framework/trader/live_roles.py)
- 核对日期：2026-10-27，Asia/Shanghai。

## 1. 这个函数负责什么

它统一执行重复的组件注册步骤：让Runner观察共享风控参考价、注册来源和执行客户端，再按绑定声明注册策略。普通Runner和固定角色在线Runner在同一入口分支处理。

它不构造Feed、Backend或执行组件，不读取文件、连接网络、启动策略，也不注册模拟引擎的原生合约。成功返回仅表示这次装配调用完成，不代表回测已经运行或所有来源能力都已检查。

## 2. 完整签名与参数

```python
def assemble_strategy(runner, strategy, *, feeds, execution, bindings):
    ...
```

| 参数 | 常用类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `runner` | UnifiedStrategyRunner或受支持子类 | 必填 | 目标注册容器，尚未启动 |
| `strategy` | StrategyTemplate及其子类 | 必填 | 实际策略对象 |
| `feeds` | feed_id到Feed对象的映射 | 必填关键字 | 键名与DataBinding.feed_id、time_feed_ids对应 |
| `execution` | ExecutionComponents等同接口对象 | 必填关键字 | 使用其中positions、prices、client；通常由builder创建 |
| `bindings` | StrategyBindings | 必填关键字 | 数据、路由、可选守卫和时钟声明 |

函数没有配置默认值，也不通过策略config猜品种、目标名称或交易客户端。动态路由由普通Runner自行注册处理，只有显式role_guard才走固定角色分支。

## 3. 检查和注册顺序

首先检查：

```python
runner.position_manager is execution.positions
```

要求对象身份相同，不只是两份仓位数值恰好相等。否则规划器、风控、策略查询和回报入账可能读到不同状态，函数会在注册前抛ValueError。

然后检查Runner是否为FixedRoleLiveRunner及是否提供role_guard，两者必须匹配，并检查固定角色的额外条件。之后按顺序：

1. `runner.add_market_observer(execution.prices)`。
2. 按feeds映射顺序调用`runner.add_data_feed(feed_id, feed)`。
3. `runner.add_execution_client(execution.client)`。
4. 普通分支调用`runner.add_strategy(...)`；固定角色分支调用`runner.add_role_strategy(...)`。
5. 返回原runner对象。

注册参考价观察者使Runner之后处理行情时先更新风控价，再交给策略。函数本身不向prices填入任何价格，行情尚未到达时它仍可能为空。

普通分支将bindings.data、bindings.execution、bindings.time_feed_ids直接交给Runner；最终来源引用、客户端引用和时钟能力等检查部分在Runner.start进行。

## 4. 固定角色守卫分支

FixedRoleLiveRunner必须提供RoleGuard；普通Runner即使处于LIVE模式也不能随意附加这个守卫。分支条件依据Runner类型，而不只是mode字符串。

| 检查 | 要求 |
| --- | --- |
| 绑定数量 | 一条数据、一条执行路由 |
| 独立时钟 | time_feed_ids为空 |
| 声明类型 | 数据为DataBinding，执行为静态ExecutionRoute |
| 行情类型 | 只接受BAR |
| 合约身份 | 数据和执行instrument_id相同 |
| 数据键 | data_key等于真实合约字符串 |
| 客户端 | route.client_id与execution.client.client_id相同 |
| 参考服务 | Runner若持有references，须与guard.references是同一对象 |

这些检查通过后，将guard.references、product、role、processed_ns和绑定周期传给add_role_strategy。后者还会检查角色名称和处理时间读取器等。守卫不是自动启用LIVE动态换约的开关。

## 5. 返回结果和副作用边界

返回传入的同一个Runner，可以用`assembled is runner`检查；不返回回测结果、策略副本或新的Runtime。

这不是整体事务。如果前面的来源注册成功，而后面的客户端/策略注册失败，函数没有统一回滚已经注册的条目。也不是可随意重复调用的幂等操作；同名Feed、客户端或策略通常会被Runner拒绝。

共享仓位、守卫等前置条件尽早检查，但不能据此认为来源文件存在性、合约条款、行情列和撮合能力都已验收。各组件仍在各自阶段验证。

## 6. 调用示例

假设strategy、feed、execution、instrument_id已准备好，Feed会产生一分钟CustomBar及标准Bar：

```python
from bomber.framework.market.basic.base import DataType
from bomber.framework.trader import (
    DataBinding, ExecutionRoute, RuntimeMode, UnifiedStrategyRunner,
)
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
assembled = assemble_strategy(
    runner, strategy,
    feeds={"main-bars": feed},
    execution=execution,
    bindings=bindings,
)
assert assembled is runner
```

接下来创建行情adapter和UnifiedHistoricalRuntime，再调用runtime.run。不要在adapter挂载之前直接runner.start，破坏当前正式回测规定的撮合回调先于策略回调的顺序。

assemble_strategy本身不安装adapter，不选择BAR/CUSTOM_BAR的撮合用途，也不改变已经挂载的Feed回调顺序。

## 7. 常见错误与排查

| 现象 | 重点检查 |
| --- | --- |
| Runner与执行积木须共享PositionManager | Runner构造时应传execution.positions |
| 固定角色Runner须显式声明RoleGuard | Runner类型与guard是否匹配 |
| 固定角色数据与真实路由不匹配 | 检查条目数、BAR类型、data_key、身份和客户端 |
| 来源、客户端或策略重复 | 是否对同一Runner重复装配或先手动注册了相同条目 |
| Runner启动后不能添加 | assemble应在生命周期启动之前执行 |
| assemble成功但start失败 | 部分完整引用和能力检查在start才执行 |
| 构建后没有回测结果 | 这是注册函数，仍需Runtime启动与运行 |

相关回归：[策略装配测试](../../../tests/test_strategy_assembly.py)，包含共享状态、下一事件成交和错误仓位对象拒绝；实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

相关说明：[StrategyBindings](StrategyBindings.md)、[DataBinding](DataBinding.md)、[ExecutionRoute](ExecutionRoute.md)、[UnifiedHistoricalRuntime](UnifiedHistoricalRuntime.md)、[build_simulation_execution](build_simulation_execution.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
