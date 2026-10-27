# ExecutionRoute

用途：把策略逻辑目标键静态映射到一个执行客户端和一个真实合约，让Runner将目标转换为账户执行请求。

- 导入路径：`from bomber.framework.trader import ExecutionRoute`
- 实际定义：`bomber.framework.trader.contracts.ExecutionRoute`，是 frozen 数据类。
- [声明源码](../../../bomber/framework/trader/contracts.py) · [目标解析与组合汇总](../../../bomber/framework/trader/runner.py)
- 核对日期：2026-10-27，Asia/Shanghai。

## 1. 这个类负责什么

例如策略发布target_key="position"、数量1，路由声明将这个目标交给rb-sim客户端，并作用于rb2704.SHFE合约。

它只描述方向，不创建订单、不查询主力、不决定买卖价，也不会在构造时调用客户端。数量、时间、版本由目标请求提供，仓位差量和订单由执行客户端规划。

## 2. 构造参数

```python
@dataclass(frozen=True)
class ExecutionRoute:
    target_key: str
    client_id: str
    instrument_id: InstrumentId
```

三个字段都必填，没有默认值。

| 参数 | 含义 |
| --- | --- |
| `target_key` | 策略使用的逻辑目标名称，strip后不能为空 |
| `client_id` | Runner已注册执行客户端的身份，strip后不能为空 |
| `instrument_id` | 固定真实合约的InstrumentId对象，需由调用方明确提供 |

名称只检查非空，不自动去空格；instrument_id也不自动由字符串或品种代码解析。当前数据类不在构造时验证合约注册或客户端是否存在，Runner.start会检查客户端引用。

## 3. 目标不是新增下单数量

```text
策略目标 position=1
    → route解析为(client_id, instrument_id)目标1
    → 合并同一客户端/合约上的策略贡献
    → 执行客户端对照仓位和在途量规划订单
```

如果有效仓位已经为1，再表达相同目标通常不需要额外买入1。若有效仓位为-1而目标为+1，规划器可能拆平仓与开仓意图；ExecutionRoute不负责这部分行为。

同一策略内target_key不能重复。不同target_key即使指向相同客户端/合约，也会在账户目标层相加，不保证形成两笔独立订单；多个策略的贡献也可在同一账户腿上汇总。

ExecutionRoute没有方向、数量、订单类型、价格、合约乘数或风控阈值字段。RiskLimits和具体执行组件负责相应规则。

## 4. 静态身份与换月

这里的instrument_id固定不变，即使target_key写成"main"或"RB:main"也不会自动选择当前主力或换月。

需要动态选择真实合约时，应使用明确的DynamicExecutionRoute及对应解析器、换月协调器，并遵守Runner当前只允许历史、单目标和独占客户端等边界；不能把动态对象当成本类的字符串字段塞入。

当前HybridTransformer采用逐合约静态路由：每个symbol拥有独立target_key，旧合约平仓目标和新合约目标可以同时存在。选择哪一份目标由策略持仓批次逻辑和角色资料决定，不是路由自己迁移仓位。

## 5. 调用示例

```python
from bomber.framework.market.basic.base import InstrumentId
from bomber.framework.trader import ExecutionRoute

route = ExecutionRoute(
    target_key="position",
    client_id="rb-sim",
    instrument_id=InstrumentId.from_str("rb2704.SHFE"),
)
assert route.target_key == "position"
assert route.client_id == "rb-sim"
```

将它放到StrategyBindings.execution或runner.add_strategy(execution_routes=...)；实际注册的客户端应有client_id="rb-sim"，且Backend应具备对应合约。

当前入口批量创建的方式为：

```python
routes = tuple(
    ExecutionRoute(target_keys[symbol], execution.client.client_id, item)
    for symbol, item in sorted(instruments.items())
)
```

上例依赖已准备好的target_keys、execution和instruments。构造成功不会提前验证策略之后提交的目标键是否匹配，Runner提交时会拒绝无路由target_key。

## 6. 常见错误

| 现象 | 重点检查 |
| --- | --- |
| target_key/client_id不能为空 | 是否空白，名称是否符合约定 |
| 未知客户端 | 注册的client.client_id是否与路由完全相同 |
| 目标没有执行路由 | 策略实际提交的target_key是否一致 |
| 合约已换月但仍指向旧合约 | 本类是静态路由，主力名称不具有自动解析能力 |
| 多目标却只看到净额订单 | 相同客户端/合约的目标贡献可能被汇总 |
| 构造路由后没有成交 | 还需装配执行组件、启动客户端、处理行情并通过风控 |

相关回归：[策略装配测试](../../../tests/test_strategy_assembly.py)、[动态路由测试](../../../tests/run_dynamic_routes.py)。相关说明：[DataBinding](DataBinding.md)、[StrategyBindings](StrategyBindings.md)、[build_simulation_execution](build_simulation_execution.md)、[UnifiedStrategyRunner](UnifiedStrategyRunner.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
