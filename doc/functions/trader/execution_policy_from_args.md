# execution_policy_from_args

用途：从入口参数解析物理账户对账政策和策略仓位来源，验证组合约束，返回ExecutionPolicy配置。

- 导入路径：`from bomber.framework.trader.execution.policy import execution_policy_from_args`
- [函数、枚举和策略结构源码](../../../bomber/framework/trader/execution/policy.py)
- [执行客户端实际对账](../../../bomber/framework/trader/execution/live/client.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 签名与返回值

```python
def execution_policy_from_args(args):
    ...
```

args须有mode属性，其他配置通过getattr取得。返回frozen ExecutionPolicy，字段为account_reconciliation和position_source，分别规范化为枚举。

函数不查询账户、不连接柜台、不更新仓位、不创建客户端或开放报单。只有将返回政策传给对应执行组件及生命周期，才会产生相应运行行为。

## 2. 参数字段与默认优先级

| args属性 | 用途 | 未提供或假值时 |
| --- | --- | --- |
| mode | 是否精确等于"model" | 必须存在；其他值均按非model分支 |
| account_reconciliation | strict/observe/off | model模式默认off，否则strict |
| strategy_position_source | fills/model | model模式默认model，否则fills |
| adopt_existing_position | 是否显式接管已有柜台仓位 | 缺失按None，无接管约束 |

后两个政策字段采用`属性值 or 默认`，None或空串会回退。合法值由枚举精确匹配，不自动去空格或改大小写。

本函数只检查mode是否为model，不验证全部CLI模式白名单；例如未知mode不会在这里自动报错，完整入口仍需argparse等验证。

## 3. account_reconciliation三种政策

| 值 | 含义 |
| --- | --- |
| strict | 关联柜台权威仓位并进行一致性核验，错误会影响就绪 |
| observe | 取得物理账户观测但与策略执行账本分开，不能直接用观测覆盖归属成交事实 |
| off | 不用柜台持仓快照关联策略执行仓位，策略仓位按明确来源维护 |

在BackendExecutionClient中，strict/observe会尝试backend.reconcile，strict失败直接传播；observe保存观测或观测错误，独立执行账本仍由自身状态维护。off不做这项仓位查询。

**off不表示完全不连接TD或不查询资金、订单。** CTP会话生命周期仍可检查可用资金、活动订单、交易日及回报；只是不能把这些等同于“策略持仓已关联柜台总仓”。

## 4. position_source与组合约束

| 值 | 含义 |
| --- | --- |
| fills | 策略仓位依据可信归属的执行成交维护 |
| model | 独立模型/参考价模拟执行账本，不关联柜台持仓 |

model来源必须同时满足mode="model"且account_reconciliation="off"。非model模式显式选model，或mode=model显式选fills，均失败。

当adopt_existing_position为真值时，只允许strict政策。不是在这里执行接管，而是先阻止错误政策组合；其他形式的接管参数还可能由对应入口和生命周期另行检查。

model一词表示仓位事实来源，不意味着任何使用机器学习预测的策略都应选择model。Hybrid虽然使用模型预测，SimNow执行仓位仍选择fills。

## 5. 公共默认值与Hybrid默认值不同

```text
公共函数，mode为非model且未给政策：strict + fills
公共函数，mode=model且未给政策：off + model
Hybrid parse_args显式默认：off + fills
```

Hybrid入口调用parser.set_defaults(account_reconciliation="off", strategy_position_source="fills")，所以Recording/SimNow分支不会自动采用公共strict默认。Hybrid目前也不提供mode=model这个CLI选项，不能据函数支持就认为该demo支持。

build_runtime把account_reconciliation传给HybridSessionLifecycle，assemble又传给build_ctp_execution，以保持会话和客户端一致。Recording分支使用记录客户端，没有成交账本，不能仅凭解析结果fills就声称它已产生真实成交。

## 6. 返回对象与恢复

```python
policy.account_reconciliation
policy.position_source
state = policy.snapshot_state()
```

snapshot_state返回version=1及两个枚举value。restore_state要求保存结构与当前政策完全一致，变化会抛ValueError，避免恢复时静默切换账本来源或对账政策。

该对象不读取恢复文件。是否将它注册进RuntimeStateManager由外围装配决定；仅调用解析函数不等于已经持久化政策。

## 7. 调用示例

```python
from types import SimpleNamespace
from bomber.framework.trader.execution.policy import execution_policy_from_args

generic = execution_policy_from_args(SimpleNamespace(mode="simnow"))
assert generic.account_reconciliation.value == "strict"
assert generic.position_source.value == "fills"

hybrid = execution_policy_from_args(SimpleNamespace(
    mode="simnow", account_reconciliation="off", strategy_position_source="fills",
))
assert hybrid.snapshot_state() == {
    "version": 1, "account_reconciliation": "off", "position_source": "fills",
}
model = execution_policy_from_args(SimpleNamespace(mode="model"))
assert model.position_source.value == "model"
```

## 8. 常见错误

| 现象 | 检查 |
| --- | --- |
| model来源组合ValueError | mode、position_source和off是否同时一致 |
| 接管仅适用于strict | 是否将adopt_existing_position与observe/off混用 |
| 无mode属性 | 参数对象应至少提供mode |
| off仍查询资金/活动订单 | off只控制持仓对账，不豁免会话其他检查 |
| Hybrid默认不是strict | 入口显式覆盖了公共默认值 |
| 恢复政策不一致 | 当前配置与检查点应一致，不能静默换账本来源 |

相关回归：[执行组成与政策测试](../../../tests/test_execution_composition.py)。实际入口：[Hybrid实时入口](../../../demos/10_hybrid_transformer/run_live.py)。

相关说明：[HybridSessionLifecycle](../demos/HybridSessionLifecycle.md)、[RecordingExecutionLifecycle](RecordingExecutionLifecycle.md)、[RiskLimits](RiskLimits.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
