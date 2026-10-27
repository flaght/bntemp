# build_simulation_execution

用途：为已有模拟 Backend 装配目标订单规划器、前置风控、模拟执行客户端，以及共享的仓位和参考价存储，返回统一执行组件集合。

- 导入路径：`from bomber.framework.trader.execution.builders import build_simulation_execution`
- [函数与返回结构源码](../../../bomber/framework/trader/execution/builders.py)
- [订单规划器](../../../bomber/framework/trader/execution/planner.py) · [模拟客户端](../../../bomber/framework/trader/execution/simulation/client.py) · [风控实现](../../../bomber/framework/trader/execution/risk.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个函数负责什么

已有 `NautilusSimExecutionBackend` 负责模拟订单和撮合，但策略产生的是目标仓位，需要经过订单规划、风控和回报记账才能接入该后端。本函数将这些组件按共享状态关系装配好：

```text
目标请求 ExecutionRequest
    → NetTargetOrderPlanner：目标减有效仓位，生成 OrderIntent
    → PreTradeRiskManager：先检查整个订单批次
    → SimulationExecutionClient：预留在途量并提交 Backend
    → 模拟执行回报
    → 客户端状态机：更新仓位、在途量及策略归属
```

它不创建 Backend、模拟交易所 Profile、合约或 Feed，也不读取文件、启动客户端或推进回测。构造客户端时会向 Backend 注册回报回调，因此不只是创建一个没有关联的配置字典。

## 2. 函数签名与参数

```python
def build_simulation_execution(
    backend,
    *,
    instrument_limits,
    positions=None,
    prices=None,
):
    ...
```

| 参数 | 常用类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `backend` | 模拟执行 Backend，通常为 NautilusSimExecutionBackend | 必填 | 使用已有对象，读取 backend_id，并注册执行回报处理器 |
| `instrument_limits` | Mapping[InstrumentId, RiskLimits] | 必填关键字 | 按真实合约配置前置风控，不是品种代码或逻辑目标键 |
| `positions` | PositionManager / None | None | 显式传入则复用原对象；否则创建新仓位管理器 |
| `prices` | MarketReferencePriceStore / None | None | 显式传入则复用原对象；否则创建空参考价存储 |

仅检查是否为 None 来决定是否新建 positions/prices，不会把传入对象的状态复制到另一个对象。backend 的对象类型及全部协议方法也没有在此做完整的提前校验，错误可能在构造客户端或运行时出现。

instrument_limits 必须显式传入，但允许空字典。底层也会将 None 处理为空映射；这不表示禁止交易，而是未启用默认数量、金额和时效上限，详见 RiskLimits 说明。

本函数没有 default_limits、account_id、planner 或自定义 risk_manager 参数。需要改变这些装配选择时，应明确构造相应组件；不能假定传额外关键字会生效。

## 3. 创建了哪些对象，哪些状态共享

内部装配逻辑为：

1. 取得或创建 positions。
2. 取得或创建 prices。
3. 创建 NetTargetOrderPlanner(positions)。
4. 创建 PreTradeRiskManager(backend.backend_id, positions, prices, instrument_limits=...)。
5. 创建 SimulationExecutionClient，并注入同一个 Backend、positions 和上述规划器、风控。
6. 返回 ExecutionComponents。

因此以下关系成立：

```python
assert execution.client.backend is execution.backend
assert execution.client.position_manager is execution.positions
assert execution.client.planner.position_manager is execution.positions
assert execution.client.risk_manager.position_manager is execution.positions
assert execution.client.risk_manager.price_store is execution.prices
```

客户端 client_id 使用 backend.backend_id。因为未显式传 account_id，模拟客户端的 account_id 也默认使用同一个标识。

RiskManager 会复制限额映射的外层字典，RiskLimits 值是冻结配置对象。构建后再往原字典追加或替换限额，不会自动更新已经创建的风控映射。

同一 Backend 通常装配一套匹配的执行客户端。重复调用会创建新的客户端、状态机和回报回调；不会自动复用第一次返回的客户端，也不会检查它们是否共享了同一份仓位。需要共享时应显式复用返回结果和状态对象。

## 4. 返回结果 ExecutionComponents

返回一个 frozen 数据类，不是客户端本身，也不是回测结果。

| 字段 | 本函数中的值 | 用途 |
| --- | --- | --- |
| `client` | 新 SimulationExecutionClient | 接收目标请求并组织订单执行 |
| `positions` | 共享 PositionManager | 保存账户仓位、有符号在途量及归属状态 |
| `prices` | 共享 MarketReferencePriceStore | 保存风控参考价与行情事件时间 |
| `backend` | 原传入 Backend | 模拟行情推进、订单转换和撮合 |
| `ledger` | None | 此通用净仓装配未创建 CTP 今昨仓账本 |
| `driver` | None | 未创建真实柜台驱动 |

frozen 只限制组件集合字段重新赋值，client、positions、prices 等内部对象仍有自己的可变状态。ExecutionComponents 没有统一的 start/run/stop 方法，生命周期由 Runtime 或调用方管理。

## 5. 目标、已成交仓位与在途量

有效仓位由 PositionManager 定义为：

```text
有效仓位 = 账户已成交净仓位 + 有符号在途数量
预计仓位 = 有效仓位 + 本次买入数量 - 本次卖出数量
```

例如已成交多头 2 手，另有买入 1 手尚未完成，有效仓位为 3。目标改为 4 时，规划器通常只再规划买入 1 手，避免把已有在途订单重复计算成新的缺口。

默认 NetTargetOrderPlanner 产生市价 OrderIntent，不会按 RiskLimits 自动拆小单或缩减目标。目标等于有效仓位时不产生订单；跨越零轴时拆成先平后开的两张意图。例如 +2 变为 -1，会规划 SELL 2 CLOSE/reduce-only，再规划 SELL 1 OPEN。

这里的顺序是规划与提交顺序，不是等待第一张完全成交后才提交第二张的协议。需要确认平仓后再开仓或 CTP 今昨仓差异时，应使用对应的规划与执行链。

模拟客户端先检查整个规划批次，任何风控违规会在提交订单前抛出 RiskRejected。通过后才进入归属预留、在途量调整和逐笔提交；这并不提供真实多腿成交事务，后续某笔 Backend 提交失败时，之前已经提交的订单不会被自动全部回滚。

执行回报用于确认成交增量、释放剩余在途量、更新账户及策略归属；提交目标或通过风控本身不代表已成交。

## 6. 参考价和行情时间如何接入

默认创建的 prices 是空的，本函数不从 Backend 自动拉取最新价格。启用金额或时效限制后，缺少参考价会拒绝订单。

正式 assemble_strategy 会调用 `runner.add_market_observer(execution.prices)`，Runner 在策略处理行情前更新观察者。手动组装时需显式完成同样的连接，或在确定语义的测试中调用 prices.update。

参考价来源为 QuoteTick 的买卖中间价、TradeTick 的成交价、Bar 的 close；CustomBar 取内部原生 Bar 的 close，而不是较高原始精度的 factors["close"]。时间使用行情 ts_event，不是 ts_init。

客户端评估批次时采用：

```text
now_ns = max(请求 ts_event, 本批订单各合约已存在参考价的 ts_event)
```

这适应触发下一分钟 Tick 才完成上一分钟 Bar 的历史聚合场景，避免把已处理的参考价误判为未来数据。它不是墙钟超时监控；历史回测中的行情时效也由这套事件时间比较决定。

## 7. 当前 HybridTransformer 的配置

实际入口为每个合约构造限额：

```python
execution = build_simulation_execution(backend, instrument_limits={
    instrument_id: RiskLimits(
        max_order_quantity=2 * config.max_contracts,
        max_abs_position=config.max_contracts,
        max_order_notional=max_notional,
        max_abs_position_notional=max_notional,
        max_market_age_ns=120_000_000_000,
        contract_multiplier=multipliers[instrument_id],
    ) for instrument_id in multipliers
})
```

持仓手数上限与单笔订单手数上限分开设置，前者限制预计净仓，后者限制每张规划订单。单笔上限设为持仓上限两倍并不表示规划器必然用一张两倍订单反手；当前默认规划器会拆平仓和开仓意图。

120000000000 纳秒为 120 秒；金额计算显式乘各合约 multiplier。所有金额和持仓阈值都按各自合约判断，不是将整个多合约组合合计后限制为 max_notional。

## 8. 调用示例

### 给已有 Backend 装配一个真实合约的执行组件

调用前提：backend 已准备好对应模拟场所和合约；本函数不验证或补齐它们。

```python
from decimal import Decimal
from bomber.framework.market.basic.base import InstrumentId
from bomber.framework.trader import RiskLimits
from bomber.framework.trader.execution.builders import build_simulation_execution


def build_for_contract(backend):
    instrument_id = InstrumentId.from_str("rb2704.SHFE")
    limits = RiskLimits(
        max_order_quantity=2,
        max_abs_position=3,
        max_order_notional=Decimal("100000"),
        max_abs_position_notional=Decimal("150000"),
        max_market_age_ns=120_000_000_000,
        contract_multiplier=10,
    )
    return build_simulation_execution(
        backend,
        instrument_limits={instrument_id: limits},
    )
```

以上是示例阈值，不会从合约资料自动推导。应确保真实合约、策略计算和风控采用相同乘数。

### 在已有状态上装配并共享给 Runner

以下为组装片段，假设 backend、limits_by_instrument、strategy、feeds 和 bindings 已正确准备：

```python
from bomber.framework.trader import (
    MarketReferencePriceStore, PositionManager, RuntimeMode, UnifiedStrategyRunner,
)
from bomber.framework.trader.assembly import assemble_strategy

positions = PositionManager()
prices = MarketReferencePriceStore()
execution = build_simulation_execution(
    backend,
    instrument_limits=limits_by_instrument,
    positions=positions,
    prices=prices,
)
runner = UnifiedStrategyRunner(
    RuntimeMode.HISTORICAL,
    position_manager=execution.positions,
)
assemble_strategy(runner, strategy, feeds=feeds, execution=execution, bindings=bindings)
```

这只完成组件和策略注册，仍需建立行情 adapter 与 Runtime。不要另外给 Runner 新建另一份 PositionManager，assemble_strategy 会拒绝状态不共享的装配。

## 9. 常见错误与使用边界

| 现象 | 重点检查 |
| --- | --- |
| 缺少 instrument_limits 参数 | 它是必填关键字，即使有意不用限额也要明确传入 |
| 已配置阈值却没有命中 | 映射键是否为正确的 InstrumentId，是否误用了品种名、字符串或逻辑目标键 |
| 以为空字典会阻止交易 | 缺省 RiskLimits 关闭数量、金额和时效上限；映射不是合约白名单 |
| `模拟执行客户端尚未启动` | 组件构建不启动交易，需由 Runtime/Runner 或明确的手动生命周期启动 |
| 缺少风控参考价 | 是否连接 execution.prices 行情观察者，或是否只有合约注册而未处理行情 |
| Runner 与执行积木须共享 PositionManager | 构造 Runner 时传 execution.positions |
| 目标超限时未自动降到阈值 | 风控是拒绝，不是裁剪或自动拆单 |
| 金额相差合约乘数倍 | contract_multiplier 是否显式设置且与合约一致 |
| 返回对象没有 run/stop | 使用 Runtime 生命周期或具体 client/backend 接口 |
| 需要今昨仓或双向账户 | 此函数固定装配通用净目标规划器，不创建 CTP ledger/driver |

RiskRejected 表示本地前置风控失败，不是交易所发来的 REJECTED 回报。客户端还可能因回报冲突、恢复未完成、Backend 提交失败等拒绝继续执行，不能将所有失败归为限额超限。

相关回归：[公共策略装配测试](../../../tests/test_strategy_assembly.py)、[风控分阶段测试](../../../tests/run_risk_manager.py)、[统一历史运行时测试](../../../tests/run_unified_historical_runtime.py)。实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

相关说明：[RiskLimits](RiskLimits.md)、[NautilusSimExecutionBackend](NautilusSimExecutionBackend.md)、[CtpFuturesBasicProfile](CtpFuturesBasicProfile.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
