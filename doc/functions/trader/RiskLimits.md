# RiskLimits

用途：声明某个真实合约的单笔数量、预计净仓位、订单金额、预计持仓金额及行情时效阈值，供 PreTradeRiskManager 在提交订单前检查。

- 导入路径：`from bomber.framework.trader import RiskLimits`
- 实际定义：`bomber.framework.trader.execution.risk.RiskLimits`，是 frozen 数据类，本身不执行检查。
- [阈值与风控源码](../../../bomber/framework/trader/execution/risk.py)
- [有效仓位定义](../../../bomber/framework/trader/portfolio.py) · [模拟客户端检查时机](../../../bomber/framework/trader/execution/simulation/client.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个类负责什么

RiskLimits 保存阈值；PreTradeRiskManager 根据实际 OrderIntent、仓位、在途量和参考行情执行比较。创建一个 RiskLimits 对象不会自动绑定合约、观察行情、撤单、平仓或阻止任何订单。

通常通过 `{InstrumentId: RiskLimits(...)}` 映射绑定真实合约，再交给 build_simulation_execution 或显式构造的风控管理器。它不属于模拟账户 Profile，也不定义交易所实际保证金或手续费。

## 2. 完整参数与默认值

```python
from decimal import Decimal
from bomber.framework.trader import RiskLimits

limits = RiskLimits(
    max_order_quantity=None,
    max_abs_position=None,
    max_order_notional=None,
    max_abs_position_notional=None,
    max_market_age_ns=None,
    contract_multiplier=Decimal(1),
)
```

| 参数 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `max_order_quantity` | Decimal / int / float / str / None | None | 每张 OrderIntent 的数量上限，期货通常以手计 |
| `max_abs_position` | 同上 | None | 预计有效净仓位的绝对值上限，多空使用同一上限 |
| `max_order_notional` | 同上 | None | 单张订单名义金额上限 |
| `max_abs_position_notional` | 同上 | None | 预计净持仓名义金额的绝对值上限 |
| `max_market_age_ns` | int / None | None | 参考价事件时间距离评估时刻允许的最大纳秒数 |
| `contract_multiplier` | Decimal / int / float / str | Decimal(1) | 金额计算使用的合约乘数，不是杠杆或保证金比例 |

前四个字段为 None 表示关闭对应上限，否则转换为 Decimal 并要求大于零。乘数同样转 Decimal，要求大于零。不能用数量或金额上限 0 表示禁止交易；需要暂停订单应使用风控模式和相应生命周期控制。

max_market_age_ns 为 None 表示关闭时效检查，负数不允许；0 允许，表示评估时刻必须与参考价时间相同。当前代码主要做数值比较，不全面检查所有字段的运行时类型和有限性，传入数据应保证类型明确、有限且单位一致。

frozen 限制字段重新赋值。需要调整阈值可新建实例或 dataclasses.replace，再明确更新使用它的风控配置；仅替换调用方变量不会自动更新已经注册的映射。

## 3. 持仓上限包含在途量

风控检查的起点是 PositionManager.effective_position：

```text
current = 账户已成交净仓位 + 有符号在途数量
signed_order = 买入 quantity；卖出 -quantity
projected = current + signed_order
```

例如已成交多头 2 手，在途买入 1 手，current=3；再买入 1 手后 projected=4。若 max_abs_position=3，会拒绝，即使成交账面当前只有 2 手。

这是净数量模型，不是将全部买卖在途绝对量相加，也不等同于双向仓位总额或任意成交次序下的最坏情形风险分析。

检查一个批次时，同一合约各订单按顺序累计 projected。两张各 2 手的买单，单张都满足上限 2，但从零仓开始第二张会将预计仓位推到 4；max_abs_position=3 时整批不通过。

阈值按每个中间步骤判断，而非只看批次最后净额。某一步超限，后续卖单再抵消也不会删除已经记录的违规。

## 4. 数量、订单金额与持仓金额的差别

当前比较公式为：

```text
订单数量                  = order.quantity
预计仓位绝对值            = abs(projected)
订单名义金额              = order.quantity × valuation_price × contract_multiplier
预计持仓名义金额          = abs(projected) × reference_price × contract_multiplier
```

订单估值价格 valuation_price 在 order.price 不为空时使用该值，否则使用行情参考价。默认净目标规划器生成市价意图，因此通常用参考价；限价单可按其限价计算订单金额。

预计持仓金额始终按 reference_price 计算，不改用这张订单的限价。即使限价已知，启用任一金额限制时当前实现仍要求存在参考行情。

所有上限用严格大于判断违规，等于上限允许。金额单位随行情报价及乘数口径确定；普通 CNY 期货通常得到 CNY 名义金额，代码不会自动做币种换算。

### 明确算例

假设参考价 3000、乘数 10、已成交及在途合计为多头 2 手：

| 订单 | 单张订单金额 | 预计净仓 | 预计持仓金额 |
| --- | --- | --- | --- |
| 市价买入 1 手 | 30000 | +3 | 90000 |
| 市价卖出 1 手 | 30000 | +1 | 30000 |
| 限价 3100 买入 1 手 | 31000 | +3 | 90000，仍按参考价3000估值 |

如果 max_order_notional=50000，市价买入 2 手的金额为60000，会失败；这与最终预计仓位是否满足另一项上限分开检查。

contract_multiplier 默认1，不会主动从 Backend、InstrumentMeta 或 FuturesContract 读取。实际合约乘数10却漏传时，金额校验会按1计算。当前 HybridTransformer 显式传入 multipliers[instrument_id]。

这些金额都是名义敞口，不是保证金占用、账户可用余额、手续费、损失或最大回撤。本类也没有账户总金额、每日亏损和组合相关性阈值。

## 5. 行情参考价与时效

MarketReferencePriceStore 从统一事件提取：

| 事件 | 参考价格 | 时间 |
| --- | --- | --- |
| QuoteTick | bid 与 ask 的中间价 | event.ts_event |
| TradeTick | price | event.ts_event |
| Bar | close | event.ts_event |
| CustomBar | 内部原生 Bar 的 close | CustomBar.ts_event |

较旧事件时间不会覆盖较新的参考价；相同事件时间可更新。CustomBar 的原始高精度 factors 不是这里的默认估值来源。

启用 max_market_age_ns、max_order_notional、max_abs_position_notional 中任意一项，就需要参考价。缺价产生 MARKET_PRICE_MISSING，而不是把它当作零金额通过。

启用时效阈值后：

```text
age = now_ns - reference.ts_event
允许范围：0 <= age <= max_market_age_ns
```

age 为负或超过上限都产生 MARKET_STALE，等于上限允许。120000000000 纳秒等于120秒，不是120毫秒。

只设置数量限制可以在无参考价时评估。只设置金额限制、没有设置 max_market_age_ns，则不会自动检查参考价有多旧。

now_ns 由调用风控的一方提供，不会在 RiskLimits 中读取系统时间。当前 SimulationExecutionClient 采用请求时间和本批已有参考价事件时间的最大值，属于事件时间风控；单独调用 risk.evaluate 时仍须正确传 now_ns。

## 6. 合约映射、默认值与多合约范围

PreTradeRiskManager 根据 order.instrument_id 找限额；没有找到时采用 default_limits。build_simulation_execution 没有显式提供 default_limits，因此缺省为 RiskLimits()：五项上限全部关闭、乘数为1。

由此可见：

- instrument_limits 是覆盖配置，不是允许交易的合约白名单。
- 键应为准确的 InstrumentId，而不是品种、目标键或看似相同的普通字符串。
- 空映射不会阻止所有交易；未匹配合约也不会自动继承其他合约的限额。
- 每个合约独立判断，不会把 RB 和 HC 的持仓金额加起来与某个上限比较。

若两个合约都配置持仓金额上限100000，不能据此认为组合总额也最多100000。组合、账户和合约准入限制需要由相应层另行实现。

## 7. 减仓与 Kill Switch 的关系

阈值之外，PreTradeRiskManager 还有 NORMAL、REDUCE_ONLY、HALTED 模式，它们不属于 RiskLimits 的字段。

| 模式或订单 | 风控行为 |
| --- | --- |
| NORMAL | 执行通常阈值与订单开平约束 |
| REDUCE_ONLY | 只允许严格降低绝对仓位且不穿过零轴的订单 |
| HALTED | 拒绝全部新订单，也包括新提交的平仓意图 |
| reduce_only=True 或 CLOSE/CLOSE_TODAY/CLOSE_YESTERDAY | 即使模式为 NORMAL，也要求严格减仓且不越过零轴 |

平仓减仓不自动豁免数量、金额和时效限制。例如大仓位只减一点后仍超过预计持仓上限，或平仓订单本身超过单笔量上限，当前逻辑仍可能拒绝。

风控管理器的 set_mode 只改变检查模式，不自动撤销已提交订单或平仓；撤单和停机行为取决于外围客户端、Backend 及运行时。REDUCE_ONLY 的覆盖模式只能收紧当前模式，不能借请求参数放松已设置的 HALTED。

## 8. 判定结果和错误定位

RiskLimits 本身只保存配置。实际检查使用：

```python
decision = risk.evaluate(orders, now_ns=now_ns)
# 返回 RiskDecision(allowed, violations)，不修改仓位或发送订单。

risk.check(orders, now_ns=now_ns)
# 不通过则抛出 RiskRejected，结构化详情在 exc.violations。
```

| 违规码 | 含义 |
| --- | --- |
| ORDER_QUANTITY | 单张订单数量超限 |
| POSITION_LIMIT | 预计净仓绝对值超限 |
| ORDER_NOTIONAL | 订单名义金额超限 |
| POSITION_NOTIONAL | 预计持仓名义金额超限 |
| MARKET_PRICE_MISSING | 已启用价格相关检查，但缺参考价 |
| MARKET_STALE | 参考价时间差为负或超过阈值 |
| REDUCE_ONLY | 不满足只减仓要求 |
| KILL_SWITCH | 当前风控处于 HALTED |

每条 RiskViolation 记录 code、instrument_id、message。一张订单可同时违反多项规则；缺少参考价时该订单不再计算依赖价格的具体金额违规。

订单 backend_id 与风控 client_id 不一致或 now_ns 为负等属于调用错误，会直接抛 ValueError；构造阈值失败也不属于一次实际订单拒绝。

## 9. 调用示例

### 为模拟执行组件配置限额

假设已有正确装配的 backend：

```python
from bomber.framework.market.basic.base import InstrumentId
from bomber.framework.trader import RiskLimits
from bomber.framework.trader.execution.builders import build_simulation_execution

instrument_id = InstrumentId.from_str("rb2704.SHFE")
execution = build_simulation_execution(backend, instrument_limits={
    instrument_id: RiskLimits(
        max_order_quantity=2,
        max_abs_position=3,
        max_order_notional="50000",
        max_abs_position_notional="90000",
        max_market_age_ns=120_000_000_000,
        contract_multiplier=10,
    ),
})
```

本示例每个字段独立发挥作用：参考价3000时，一张2手订单虽然数量允许，金额60000仍超过50000而被拒绝。阈值不是自动裁剪目标或自动拆小单的指令。

### 直接评估，不启动交易

以下示例使用内存仓位和参考价，不创建模拟引擎，不提交订单；需具备对应框架运行环境。

```python
from bomber.framework.market.basic.base import InstrumentId
from bomber.framework.trader import (
    MarketReferencePriceStore, OrderIntent, OrderSide, PositionManager,
    PreTradeRiskManager, RiskLimits, RiskViolationCode,
)

instrument_id = InstrumentId.from_str("rb2704.SHFE")
positions = PositionManager()
prices = MarketReferencePriceStore()
prices.update(instrument_id, 3000, 1_000_000_000)
risk = PreTradeRiskManager(
    "example-sim",
    positions,
    prices,
    instrument_limits={instrument_id: RiskLimits(
        max_order_quantity=2,
        max_abs_position=3,
        max_order_notional=50000,
        max_market_age_ns=120_000_000_000,
        contract_multiplier=10,
    )},
)
order = OrderIntent(
    strategy_id="example-strategy",
    backend_id="example-sim",
    instrument_id=instrument_id,
    side=OrderSide.BUY,
    quantity=2,
)
decision = risk.evaluate((order,), now_ns=1_000_000_000)
assert not decision.allowed
assert {v.code for v in decision.violations} == {RiskViolationCode.ORDER_NOTIONAL}
assert positions.effective_position("example-sim", instrument_id) == 0
```

相关回归：[风控分阶段测试](../../../tests/run_risk_manager.py)，涵盖数量、金额、批次仓位、时效、减仓模式、合约乘数及缺价拒绝；[统一历史运行时测试](../../../tests/run_unified_historical_runtime.py) 包含模拟客户端的实际风控调用时机。

相关说明：[build_simulation_execution](build_simulation_execution.md)、[NautilusSimExecutionBackend](NautilusSimExecutionBackend.md)、[CtpFuturesBasicProfile](CtpFuturesBasicProfile.md)。实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
