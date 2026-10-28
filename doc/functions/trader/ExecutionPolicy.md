# ExecutionPolicy

用途：声明执行账本的仓位来源以及物理账户对账政策，校验两者组合，并支持恢复时的政策一致性检查。

- 导入路径：`from bomber.framework.trader.execution.policy import ExecutionPolicy`
- 也由`bomber.framework.trader.execution`导出。
- [类、枚举和参数解析源码](../../../bomber/framework/trader/execution/policy.py) · [执行客户端](../../../bomber/framework/trader/execution/live/client.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 类型与完整构造参数

这是frozen数据类，不是执行客户端。构造后普通字段赋值会抛FrozenInstanceError；构造不连接账户、不查询持仓、不提交订单。

```python
def __init__(self,
             account_reconciliation=AccountReconciliationMode.STRICT,
             position_source=StrategyPositionSource.FILLS):
    ...
```

| 字段 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| account_reconciliation | AccountReconciliationMode | STRICT，即strict | 如何关联或观测物理账户仓位 |
| position_source | StrategyPositionSource | FILLS，即fills | 策略仓位事实来源 |

两个参数可按位置或关键字传入，支持对应枚举或合法字符串。__post_init__将字符串规范化为枚举；精确匹配，不自动清除空格或转换大小写，None和未知值会失败。

## 2. 两个维度与合法组合

| account_reconciliation | position_source=fills | position_source=model |
| --- | --- | --- |
| strict | 合法，关联柜台权威仓位并校验 | 拒绝 |
| observe | 合法，账户观测与策略归属成交分开 | 拒绝 |
| off | 合法，不查询柜台仓位来关联策略账本 | 合法，独立参考价模拟账本 |

model只能配off，否则构造抛ValueError。模型策略账本若需要跟随物理账户，应在独立账户执行端配置观测或对账，不能把两套仓位事实直接混合。

在BackendExecutionClient里strict对账失败会传播；observe记录账户观测或观测错误，不用观测覆盖独立归属成交；off跳过这项仓位查询。off仍可能有TD连接、资金/活动订单检查和真实报单，不是“关闭所有交易”。具体行为须由执行客户端及生命周期消费配置。

## 3. 与mode及入口默认值的区别

ExecutionPolicy没有mode字段，也不检查CLI模式。直接ExecutionPolicy('off', 'model')合法，但不因此创建模拟Backend。

execution_policy_from_args额外要求mode=model与position_source=model同时选择，且显式接管已有仓位只能用strict。公共解析默认：非model为strict+fills，model为off+model。Hybrid入口显式设off+fills，且不开放mode=model。

因此，直接构造默认值、参数解析默认值、某个demo覆盖值必须分别理解。仅创建政策对象，不会自动把参数传播到已构造的执行组件。

## 4. snapshot_state与restore_state

```python
def snapshot_state(self):
    ...

def restore_state(self, state):
    ...
```

snapshot_state无参数，每次返回新字典：version=1、account_reconciliation和position_source的字符串value，不包含账户余额、持仓、订单或模型权重。

restore_state接收保存状态，与当前snapshot_state做完整相等比较；一致返回None，不一致抛ValueError。它不修改当前政策，不迁移旧版本，也不会从保存值自动重建配置；缺字段、多字段或政策变化都会被拒绝。

只有外围将对象注册为状态组件时才参与持久化。例如attach_ctp_persistence默认注册execution_policy，通用attach_execution_persistence也会按执行Backend选择来源。对象自身不读写磁盘。

## 5. 调用示例

```python
from bomber.framework.trader.execution.policy import (
    ExecutionPolicy, AccountReconciliationMode, StrategyPositionSource,
)

default = ExecutionPolicy()
assert default.account_reconciliation is AccountReconciliationMode.STRICT

hybrid = ExecutionPolicy("off", "fills")
assert hybrid.position_source is StrategyPositionSource.FILLS
assert hybrid.snapshot_state() == {
    "version": 1, "account_reconciliation": "off", "position_source": "fills",
}
assert hybrid.restore_state(hybrid.snapshot_state()) is None

model = ExecutionPolicy("off", "model")
assert model.position_source is StrategyPositionSource.MODEL
```

ExecutionPolicy('strict', 'model')会在构造时失败；用strict政策恢复off检查点也会失败。

## 6. 常见问题

| 现象 | 检查 |
| --- | --- |
| 有政策对象却没模拟成交 | 政策不创建Backend，需相应执行装配 |
| 使用预测模型却应选fills | 来源描述仓位事实，不描述预测算法 |
| off仍检查资金或有报单 | off仅控制柜台持仓关联，其他检查仍有效 |
| restore_state没有改变政策 | 该方法只校验一致性 |
| 配置字段无法赋值 | frozen数据类，修改应创建新对象；恢复仍须保持政策一致 |

相关回归：[执行政策与账本测试](../../../tests/test_execution_composition.py)。

相关说明：[StrategyPositionSource](StrategyPositionSource.md)、[execution_policy_from_args](execution_policy_from_args.md)、[MarketSourceConfig](../market/MarketSourceConfig.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
