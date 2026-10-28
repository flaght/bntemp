# StrategyPositionSource

用途：用枚举声明策略仓位的事实来源，区分归属执行成交与独立参考价模拟账本。

- 导入路径：`from bomber.framework.trader.execution.policy import StrategyPositionSource`
- 也由`bomber.framework.trader.execution`导出。
- [枚举与政策源码](../../../bomber/framework/trader/execution/policy.py) · [模拟执行Backend](../../../bomber/framework/trader/execution/model.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 定义与成员

```python
class StrategyPositionSource(str, Enum):
    FILLS = "fills"
    MODEL = "model"
```

| 成员 | value | 含义 |
| --- | --- | --- |
| FILLS | fills | 根据可信归属的执行成交维护策略仓位 |
| MODEL | model | 独立模型执行账本，按参考价模拟成交，不关联柜台持仓 |

枚举没有自身默认成员。ExecutionPolicy将默认position_source设为FILLS；execution_policy_from_args在mode=model时默认MODEL，否则FILLS。

FILLS描述成交事实来源，不是“把整个账户的总仓直接算作该策略仓位”。MODEL也不表示神经网络预测结果，使用Transformer预测并在SimNow执行的Hybrid仍选择FILLS。

## 2. 构造、读取与序列化

```python
from bomber.framework.trader.execution.policy import StrategyPositionSource

source = StrategyPositionSource("fills")
assert source is StrategyPositionSource.FILLS
assert source.name == "FILLS"
assert source.value == "fills"
assert source == "fills"
```

传枚举value取得对应单例；无参数调用不会自动选默认值。'FILLS'、'model '、None等不是合法value，会抛ValueError。

这是str和Enum的组合，可与对应字符串相等，但用于报告、CLI和检查点时应明确取.value；不要依赖str(member)作为保存值。它没有仓位数量、策略ID、合约、交易日或成交列表字段。

## 3. 两种来源的运行差别

FILLS需要执行链路正确归属订单与成交，选中的执行回报才推进策略账本。strict/observe/off分别决定如何处理物理账户关联，不由这个枚举单独决定。

MODEL通常由build_model_execution装配ReferencePriceExecutionBackend，使用参考价和本地执行状态产生标记为模型执行的回报，并用独立PositionManager维护仓位。它不是交易所撮合，也不代表物理账户已经同步成交。

单独选枚举不会创建上述组件。若模型目标再分发给真实账户，账户端的成交、持仓和恢复应独立管理；不能把账户跟随失败改写成模型账本已成交或未成交。

## 4. 政策及CLI约束

| 来源 | ExecutionPolicy允许的对账政策 | execution_policy_from_args额外要求 |
| --- | --- | --- |
| fills | strict、observe、off | mode不能为model |
| model | 仅off | mode必须为model |

枚举构造本身不检查account_reconciliation或mode，这些分别在ExecutionPolicy和参数解析层检查。

Recording只记录目标，没有成交账本；虽然某些入口解析政策为fills，也不能据此声称Recording产生了执行成交。Recording报告可使用strategy_position_source=none作说明，这个报告字符串并不是该枚举的第三个成员。

## 5. 与其他对象的区别

| 对象 | 回答的问题 |
| --- | --- |
| StrategyPositionSource | 策略仓位依据哪类执行事实维护？ |
| AccountReconciliationMode | 物理账户仓位如何关联或观测？ |
| ExecutionPolicy | 两个维度组合是否允许，恢复政策是否一致？ |
| MarketSourceConfig | 行情从哪个来源输入，如何解释分钟？ |
| PositionManager | 当前仓位、在途量和策略归属等实际状态是什么？ |

行情来自CTP或DolphinDB，并不能决定仓位来源；Live事件环境也可使用模型模拟账本。RuntimeMode与这里的MODEL不是同一个概念。

相关回归：[归属成交、模型账本与政策测试](../../../tests/test_execution_composition.py)。

相关说明：[ExecutionPolicy](ExecutionPolicy.md)、[execution_policy_from_args](execution_policy_from_args.md)、[RuntimeMode](RuntimeMode.md)、[MarketSourceConfig](../market/MarketSourceConfig.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
