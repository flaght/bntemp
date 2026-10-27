# StrategyBindings

用途：把一个策略的数据绑定、执行路由和可选角色守卫、时钟来源集中为一份装配声明，供 assemble_strategy 使用。

- 导入路径：`from bomber.framework.trader.assembly import StrategyBindings`
- 实际定义：`bomber.framework.trader.assembly.StrategyBindings`，是 frozen 数据类。
- [声明及装配源码](../../../bomber/framework/trader/assembly.py) · [Runner 注册逻辑](../../../bomber/framework/trader/runner.py)
- 核对日期：2026-10-27，Asia/Shanghai。

## 1. 这个类负责什么

它将“策略看哪些行情、目标送到哪里、是否需要固定角色守卫”集中保存。构造对象不会读取文件、连接 Feed、创建账户或启动策略；assemble_strategy 才把声明注册到具体 Runner。

它不包含策略对象、Feed 对象或 Backend。实际对象分别通过 assemble_strategy 的 strategy、feeds、execution 参数提供。

## 2. 构造字段

```python
@dataclass(frozen=True)
class StrategyBindings:
    data: tuple
    execution: tuple
    role_guard: RoleGuard | None = None
    time_feed_ids: tuple = ()
```

| 字段 | 常用类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `data` | tuple[DataBinding, ...] | 必填 | 策略逻辑 data_key 对应的来源、合约、事件类型和周期 |
| `execution` | tuple[ExecutionRoute或DynamicExecutionRoute, ...] | 必填 | 逻辑 target_key 到执行客户端及真实合约的路由 |
| `role_guard` | RoleGuard / None | None | 仅固定角色在线 Runner 使用的显式守卫资料 |
| `time_feed_ids` | tuple[str, ...] | () | 向策略提供独立 on_time 的 Feed 标识 |

data 和 execution 是必填字段，单条声明也要写为 `(binding,)`，避免漏逗号变成单个对象。

数据类没有 __post_init__，不会自动把列表转元组，不检查路由、字段对象类型或来源是否存在。冻结只限制字段重新赋值，不保证内部对象递归不可变或所有组合均有效。

## 3. data 与 execution 如何配合

```python
bindings = StrategyBindings(
    data=(DataBinding("model-bars", "main-bars", instrument_id,
                      DataType.CUSTOM_BAR, "1-MINUTE"),),
    execution=(ExecutionRoute("position", client_id, instrument_id),),
)
```

含义是：策略在 data_key="model-bars" 下收到 main-bars 来源的 CustomBar；它发布 target_key="position" 时，由指定 client_id 管理该真实合约的目标。

data_key、target_key 和 feed_id 是不同名称，不要求彼此相同。观察合约与交易合约也可以不同，普通 Runner 不强制两者一致；具体业务应明确这种关系。

同一策略可以有多个数据绑定及目标路由，但 data_key 和 target_key 分别不能重复，检查由 Runner.add_strategy 执行。声明本身不负责合并、去重或推断缺失绑定。

当前 HybridTransformer 为每个合约生成独立 DataBinding 和 ExecutionRoute，角色选择由策略与研究参考资料完成，不是由 StrategyBindings 自动选择主力。

## 4. role_guard 与 time_feed_ids

RoleGuard 的字段为 references、product、role、processed_ns，其中 processed_ns 是返回已处理时间戳的可调用对象。

固定角色 FixedRoleLiveRunner 必须提供 role_guard；普通 UnifiedStrategyRunner 不接受该守卫。固定角色装配还要求：

- 恰好一条 DataBinding 和一条静态 ExecutionRoute，不声明独立 time_feed_ids。
- 数据类型为 BAR，数据与执行合约一致。
- data_key 为真实合约字符串，route.client_id 与 execution.client.client_id 一致。
- Runner 如持有 references，须与 guard.references 是同一个对象。

守卫由固定角色 Runner 使用，不是创建声明时就校验或查询角色。普通历史回测保持 role_guard=None。

time_feed_ids 与普通行情订阅不同：它指向有 register_time_handler 能力的来源，Runner.start 会检查该能力，回调进入策略 on_time。仅填写一个名称不会自动创建定时器，也不会把每根 Bar 复制为时钟事件。

## 5. 调用示例

以下只创建声明，需在完整框架环境中运行：

```python
from bomber.framework.market.basic.base import DataType, InstrumentId
from bomber.framework.trader import DataBinding, ExecutionRoute
from bomber.framework.trader.assembly import StrategyBindings

instrument_id = InstrumentId.from_str("rb2704.SHFE")
bindings = StrategyBindings(
    data=(DataBinding("model-bars", "main-bars", instrument_id,
                      DataType.CUSTOM_BAR, "1-MINUTE"),),
    execution=(ExecutionRoute("position", "rb-sim", instrument_id),),
)
assert bindings.data[0].instrument_id == bindings.execution[0].instrument_id
assert bindings.role_guard is None
assert bindings.time_feed_ids == ()
```

随后调用 `assemble_strategy(runner, strategy, feeds={"main-bars": feed}, execution=execution, bindings=bindings)`，其中 execution.client.client_id 应与上面的 rb-sim 一致。构造声明成功不等于这些外部对象已经存在。

## 6. 常见错误

| 现象 | 重点检查 |
| --- | --- |
| 遍历绑定时报类型错误 | 单元素元组是否写了逗号，data/execution是否为正确集合 |
| 重复data_key或target_key | 每个策略内对应的逻辑名称必须唯一 |
| 未知Feed或客户端 | feeds键和client_id是否与声明对应，最终在Runner.start检查 |
| 固定角色守卫组合错误 | Runner类型、条目数、BAR类型、身份和references是否一致 |
| 声明后策略未启动 | 仍需assemble_strategy及相应Runtime生命周期 |

相关回归：[策略装配测试](../../../tests/test_strategy_assembly.py)。实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

相关说明：[DataBinding](DataBinding.md)、[ExecutionRoute](ExecutionRoute.md)、[assemble_strategy](assemble_strategy.md)、[UnifiedStrategyRunner](UnifiedStrategyRunner.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
