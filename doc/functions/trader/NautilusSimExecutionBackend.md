# NautilusSimExecutionBackend

用途：以 Nautilus `BacktestEngine` 为内核，管理模拟交易场所、合约、行情推进、订单转换、执行回报和引擎释放。

- 导入路径：`from bomber.framework.trader import NautilusSimExecutionBackend`
- 实际定义：`bomber.framework.trader.execution.simulation.backend.NautilusSimExecutionBackend`，是类。
- [Backend 源码](../../../bomber/framework/trader/execution/simulation/backend.py) · [内部订单网关](../../../bomber/framework/trader/execution/simulation/gateway.py)
- [模拟执行客户端](../../../bomber/framework/trader/execution/simulation/client.py) · [历史运行时](../../../bomber/framework/trader/runtime/historical.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个类负责什么

它把模拟执行分成可以统一管理的几个步骤：

1. 创建或接管一个 `BacktestEngine`。
2. 注册交易场所 Profile、真实合约和可选的原生策略。
3. 一次性运行历史行情，或逐个行情事件推进模拟时钟和撮合。
4. 接收中立的 `OrderIntent`，交由内部网关创建原生订单。
5. 收集接受、拒绝、成交、撤单等统一 `ExecutionReport` 并通知订阅方。
6. 完成回测，保留结果供查询，最后释放引擎资源。

类属性 `kind = ExecutionBackendKind.SIMULATION`。这里的模拟执行会调用引擎撮合，不只是记录目标；它也不连接真实 CTP 或 Binance 柜台。

输入文件读取、模型推理、目标仓位规划、策略级风控和策略仓位管理由外围组件负责。仅构造这个 Backend，还没有注册模拟交易场所、合约或行情，也不会自动开始回测。

## 2. 构造签名与参数

```python
def __init__(
    self,
    backend_id: str,
    config: BacktestEngineConfig | None = None,
    *,
    engine: Any | None = None,
) -> None:
    ...
```

| 参数 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `backend_id` | `str` | 必填 | 当前执行后端的标识，订单的 `backend_id` 必须与它完全一致 |
| `config` | `BacktestEngineConfig` / `None` | `None` | 创建内部回测引擎的配置；未传时采用 `BacktestEngineConfig()` |
| `engine` | 引擎对象 / `None` | `None` | 注入已有引擎，主要用于明确的引擎接管或测试替身；只能按名称传入 |

`backend_id.strip()` 不能为空，但存储时保留原字符串，不会自动裁去空格。建议直接传明确且无首尾空格的标识。

提供 `engine` 时直接使用该对象，`config` 不再用于创建或重配它。无论引擎由 Backend 创建还是外部注入，`stop()` 都会调用它的 `dispose()`，因此不能把注入理解为只借用、不负责释放。

构造时会初始化空的 Profile 集合、回报列表和结果缓存；内部订单网关按需创建。没有文件路径参数，也不在这里读取凭据。

## 3. 当前入口的构造语句

HybridTransformer 中的代码为：

```python
backend = NautilusSimExecutionBackend(
    f"{product.lower()}-transformer-sim",
    BacktestEngineConfig(
        trader_id=TraderId(f"{product}-HYBRID-001"),
        logging=LoggingConfig(log_level="WARNING"),
        run_analysis=True,
    ),
)
```

| 配置 | 在当前入口中的作用 |
| --- | --- |
| `f"{product.lower()}-transformer-sim"` | 执行后端身份，例如 `rb-transformer-sim`；用于执行客户端和订单路由 |
| `trader_id` | 引擎内 Trader 的身份，例如 `RB-HYBRID-001`；不是交易所代码或真实柜台账户 |
| `logging` | 配置引擎日志；示例设为 `WARNING` |
| `run_analysis=True` | 请求引擎执行回测分析；不代表自动创建本项目的 CSV 或策略报告目录 |

`backend_id`、`trader_id` 和 `Venue("SHFE")` 表达不同身份。一个 Backend 可以配置多个不同 Venue，但同一 Venue 只能注册一个 Profile。

完整引擎配置直接传给 `BacktestEngine`，此封装层不会逐项改写。Backend 的 `run()` 不提供 `start/end` 等参数；当前入口在数据准备和 Feed 回放层组织请求日期范围。

## 4. 账户规则、合约与行情如何装配

### add_profile：建立模拟交易场所

```python
backend.add_profile(profile)
```

`profile` 满足 `VenueSimulationProfilePort`：提供 `venue`、`profile_id` 和 `build_backend_config()`。Backend 将配置展开给 `engine.add_venue(**config)`，立即注册模拟 Venue，而不是等到 `run()` 才注册。

同一 `str(profile.venue)` 重复注册会抛出 `ValueError`。账户起始资金、币种、订单管理模式、手续费、保证金和可插拔撮合模型等由 Profile 声明，不是 Backend 构造参数。

当前策略采用 `CtpFuturesBasicProfile`，主要配置如下：

| 项目 | 当前基础 Profile 的行为 |
| --- | --- |
| 订单管理 | `NETTING`，按净持仓管理 |
| 账户类型 | `MARGIN`，保证金账户，基础币种 CNY |
| 起始资金 | `starting_balance`，默认 `1000000` CNY |
| 手续费 | `PerContractFeeModel`，每手固定金额，由 `commission_per_contract` 指定，默认 `1` CNY |
| 保证金 | `StandardMarginModel`，与具体合约声明的保证金参数配合 |
| 行情执行 | 开启 `bar_execution` 和 `trade_execution` |
| 其他 | 启用 reduce-only 支持及市价订单确认；`use_position_ids=False` |

这是基础模拟模型，源码明确不提供真实 CTP 的双向持仓、今昨仓划分、平今平昨差异费用和完整交易日结算规则。指定 `venue=Venue("SHFE")` 不会自动补齐这些规则或安装 `SessionCalendar`。

如需其他市场或模型，使用适合的 Profile，例如 `GenericVenueProfile`；填单、费用、延迟等具体行为以所选 Profile 和引擎配置为准。

### add_instrument：注册具体合约

```python
backend.add_instrument(instrument)
```

传入引擎可识别的原生合约对象，不是合约名字符串或基础表的一行。当前入口通过 `build_instrument(basic, profile, product, symbol)` 创建合约，再逐个注册。

合约对象声明身份、价格精度、最小价位、乘数、有效期及保证金等。行情和订单的 `instrument_id` 必须与已注册合约一致。只注册交易所 Profile 不等于该交易所的所有合约都已注册。

### add_data / add_strategy：静态引擎配置

`add_data(data, **kwargs)` 将事件集合转为列表；空列表抛出 `ValueError`，其他参数透传给 `engine.add_data`。输入应为引擎可识别的标准行情事件，不是 Feather 路径、DataFrame 或模型特征表。

`add_strategy(strategy)` 透传给 `engine.add_strategy`，用于原生引擎策略。统一框架中的 `HybridTransformerStrategy` 通过 Runner 装配，不直接传给本方法。Backend 也会自行注册一个内部原生 Strategy 作为订单网关，通常不需调用方手动创建它。

上述静态配置方法在 Backend 启动、完成或释放后均禁止使用。引擎自身对合约、事件和策略的校验仍然生效，Backend 不会把所有错误统一转换为 `InputError`。

## 5. 两种行情运行方式

### 一次性运行：add_data + run

先注册全部必要对象和历史事件，再调用：

```python
backend.add_data(events)
result = backend.run()
```

`run()` 自动调用 `start()`，然后执行 `engine.run()`，成功后将 Backend 标记为已完成，缓存并返回 `engine.get_result()`。同一 Backend 不能再次 `run()`，也不能继续逐事件推进；新回测应创建新实例。

直接使用 Backend 时，调用方在 `finally` 中负责 `stop()`。`run()` 本身不会自动释放资源，发生异常也没有在此方法内自动清理。

### 逐事件运行：process_market_event

```python
new_reports = backend.process_market_event(event)
```

每次调用按顺序执行：

1. 检查未完成、未释放，并记录当前回报列表长度。
2. 调用 `start()`，确保内部网关已注册。
3. 调用 `engine.add_data([event])`。
4. 调用 `engine.run(streaming=True)` 推进事件。
5. 调用 `engine.clear_data()` 清除这批已消费行情。
6. 标记已进入流式运行，返回本次调用期间新增的回报元组。

模拟时钟由 `BacktestEngine` 推进；这不是每根行情重新创建引擎。清除已消费行情也不代表清空订单、仓位或累计回报。

流式行情结束后调用 `finish()` 完成引擎运行并生成结果，查询完报告后再 `stop()`。不要把已注册的批量行情和逐事件推送随意混用，或将同一行情重复推进。

当前 `NautilusMarketFeedAdapter` 支持 QuoteTick、TradeTick 和标准 Bar 的绑定，并用锁串行推进。额外模型字段随 CustomBar 供策略消费，不应作为第二份行情重复送入撮合。Backend 本身没有为所有公开方法提供线程锁，直接并发调用不受此封装保证。

## 6. 订单如何变成模拟成交

统一策略主链中，责任分工如下：

| 组件 | 责任 |
| --- | --- |
| 策略、Runner、执行路由 | 生成和分配目标仓位请求 |
| `NetTargetOrderPlanner` | 根据目标与现有仓位等状态规划订单意图 |
| `PreTradeRiskManager` | 执行已配置的下单前风控 |
| `SimulationExecutionClient` | 组织规划、风控、在途量、回报状态和仓位更新 |
| `NautilusSimExecutionBackend` | 接收订单意图，管理引擎并分发模拟执行回报 |
| 内部 `NautilusOrderGateway` | 将意图转换为原生订单，将原生订单事件转换为统一回报 |
| `BacktestEngine` 与所选 Profile | 按行情和模型规则推进撮合及账户状态 |

`build_simulation_execution(backend, instrument_limits=...)` 会组装共享的仓位管理器、参考价存储、规划器、风控及模拟客户端。单独构造 Backend 不会自动拥有这套策略级风控；直接 `backend.submit_order(...)` 不经过客户端的规划和风控步骤。

### submit_order 的输入和行为

```python
backend.submit_order(order)
```

`order` 为 `OrderIntent`，主要包含策略标识、后端标识、合约身份、方向、数量、订单类型、价格、开平意图及元数据。当前订单类型为 `MARKET` 和 `LIMIT`；限价单需要价格。

Backend 要求 `order.backend_id == backend.backend_id`，随后把意图交给内部网关。网关尚未由引擎启动时先排队，启动后再提交；因此启动前可以提交订单，但调用返回 `None` 不代表已接受或成交。

网关从引擎缓存查询合约，用合约方法构造数量和价格，再创建原生市价单或限价单。找不到合约会报错。`CLOSE`、`CLOSE_TODAY`、`CLOSE_YESTERDAY` 在这里都转为减少持仓的约束，不提供今昨仓差异结算；`OPEN` 同时设置 `reduce_only=True` 会被拒绝。

`cancel_strategy(strategy_id)` 删除该策略尚在网关队列中的意图，并对网关已关联且仍开放的原生订单发起撤单。它不平仓，也不等于停止策略；网关尚不存在时没有撤单动作。

### 回报与成交数量

`register_report_handler(handler)` 注册接收单条 `ExecutionReport` 的回调，重复注册同一回调不会重复添加。Backend 先追加回报，再按注册顺序同步调用回调；回调异常没有在这里被吞掉，应避免在回调中执行可能失败的重操作。

网关生成 `ACCEPTED`、`REJECTED`、`PARTIALLY_FILLED`、`FILLED`、`CANCELED`。原生过期事件映射为 `CANCELED` 并附原因 `expired`。

成交回报的 `filled_quantity` 使用该次事件的 `last_qty`，是本次新增成交量，不是累计成交量；元数据包含策略身份、开平意图，以及成交时的 trade_id、venue_order_id、commission 等。`SimulationExecutionClient` 使用回报状态机和增量更新仓位、在途量。

`backend.reports` 只汇集经内部网关映射的统一回报，不应直接解释为引擎所有原生策略的全部订单报告。直接通过 `add_strategy` 添加的原生策略所下订单，应同时查看引擎原生报告。

## 7. 当前 HybridTransformer 的事件顺序

当前入口通过 `NautilusMarketFeedAdapter`、Runner 和 `UnifiedHistoricalRuntime` 组织回放，并非调用 `backend.run()` 运行全部行情：

```text
事件 N 到达 Feed
    ↓
Market adapter 先将标准行情交给 Backend，推进模拟时钟和已有订单撮合
    ↓
产生的执行回报经 SimulationExecutionClient 更新执行状态和仓位
    ↓
Runner 将本次策略行情交给 HybridTransformerStrategy
    ↓
模型与策略产生目标，执行客户端规划并提交新订单
    ↓
等待后续行情事件推进撮合
```

`UnifiedHistoricalRuntime.start()` 先注册 adapter 回调，再启动 Runner。这使事件 N 产生的新订单最早在事件 N+1 推进时成交，避免策略在看完整根 bar 后又利用同一根 bar 的撮合路径成交。

这是当前 Runtime 和回调装配顺序的约束，不是直接调用任意 Backend API 都自动提供的承诺。N+1 表示下一次推进的行情事件，不一定等于下一分钟，也不保证订单一定能成交。最后一个事件后产生的订单不能假定已有后续行情可撮合。

## 8. 生命周期和常用方法

| 接口 | 返回值与关键行为 |
| --- | --- |
| `start()` | 返回 `None`；确保网关存在并封闭静态配置阶段，原生组件在引擎首次 run 时实际启动 |
| `run()` | 一次性运行，返回并缓存原生回测结果，之后不能再次推进 |
| `process_market_event(event)` | 返回本次调用新增的 `tuple[ExecutionReport, ...]` |
| `finish()` | 流式运行调用 `engine.end()`，缓存结果并完成运行，但保留引擎供查询；重复完成不会再次 end |
| `result()` | 返回已缓存结果；尚未生成结果时为 `None`，不主动重新计算 |
| `stop()` | 先调用 finish，再 dispose 引擎；成功释放后重复调用安全 |
| `profiles` | 已注册 Profile 的元组 |
| `reports` | 累计统一执行回报的元组 |
| `is_started` | Backend 自身的启动标志，不等同于原生组件是否正在处理事件 |
| `is_disposed` | 是否已经成功执行引擎释放 |
| `engine` | 持有的原生引擎，可在释放前查询 trader 报告等 |

注意以下边界：

- `start()` 不要求调用方先显式调用 `run()`；`run` 和逐事件方法都会自动 start。已完成或已释放后不能重新 start。
- 仅创建或仅 start、尚未推进过流式行情时，`finish()` 不生成结果，也不会把实例标记为已完成；不能用它替代无条件封闭配置。
- 批量 `run()` 成功后会标记完成，但当前实现没有同步清除 `_started`；因此 `is_started` 不能单独判断还能否继续运行。
- 流式 `finish()` 成功后会清除 `_started`，并标记完成；静态配置仍然不能修改。
- `stop()` 的幂等性以成功释放为前提；如果 finish 或 dispose 抛出异常，异常会向外传播。

推荐顺序是“装配 → 运行 → 完成 → 查询并导出报告 → 释放”。正式 Runtime 管理启动和停止时，跟随 Runtime 的生命周期即可。

## 9. 返回结果和报告如何读取

正常使用真实引擎时，`run()` 或完成后的 `result()` 返回 `engine.get_result()` 结果；封装接口标注 `Any`，注入的测试引擎可以返回替身结果。

`UnifiedHistoricalRuntime.run()` 另外返回 `UnifiedHistoricalResult`，包含 `replay_summary`、`backend_result`、`market_events_processed`、`execution_reports_received`。两种结果对象不能混用。

原生明细在引擎尚未释放时读取：

```python
trader = backend.engine.trader
orders = trader.generate_orders_report()
fills = trader.generate_fills_report()
positions = trader.generate_positions_report()
account = trader.generate_account_report(venue)
```

这些查询返回明细表；写 CSV、输入诊断、策略状态及 summary.json 是调用入口另外实现的工作。`finish()` 专门保留了查询窗口，`stop()` 之后不应依赖引擎资源继续生成报告。

## 10. 调用示例

### 构造与基础 CTP 模拟配置

以下代码需要已安装可用的 Bomber 引擎和对应依赖；仅配置并释放一个后端，不读取资料或运行回测：

```python
from decimal import Decimal
from bomber.backtest.config import BacktestEngineConfig
from bomber.config import LoggingConfig
from bomber.model import Venue
from bomber.model.identifiers import TraderId
from bomber.framework.trader import NautilusSimExecutionBackend, CtpFuturesBasicProfile

backend = NautilusSimExecutionBackend(
    "rb-transformer-sim",
    BacktestEngineConfig(
        trader_id=TraderId("RB-HYBRID-001"),
        logging=LoggingConfig(log_level="WARNING"),
        run_analysis=True,
    ),
)
try:
    profile = CtpFuturesBasicProfile(
        starting_balance=Decimal("1000000"),
        commission_per_contract=Decimal("1"),
        venue=Venue("SHFE"),
    )
    backend.add_profile(profile)
    # 在首次启动前注册所需真实合约；之后再选择批量或逐事件运行方式。
finally:
    backend.stop()
```

这里的初始资金和费用是演示配置，不由基础资料表自动推导。

### 一次性运行的生命周期骨架

调用前提：`backend` 已注册交易场所和合约；`events` 为与合约匹配的非空标准行情事件集合；需要交易时已装配策略或订单来源。

```python
def run_batch(backend, events):
    try:
        backend.add_data(events)
        result = backend.run()
        trader = backend.engine.trader
        return (
            result,
            trader.generate_orders_report(),
            trader.generate_fills_report(),
            trader.generate_positions_report(),
        )
    finally:
        backend.stop()
```

仅提供行情而没有策略或订单来源，不会自动生成交易信号。

### 当前统一 Runtime 的运行与清理

调用前提：`runner` 已装配策略、Feed 和 `SimulationExecutionClient`，`adapter` 使用同一个 Backend 且设置 `manage_lifecycle=False`。完整装配参考实际入口。

```python
from bomber.framework.trader import UnifiedHistoricalRuntime


def run_historical(runner, adapter):
    runtime = UnifiedHistoricalRuntime("hybrid-runtime", runner, adapter)
    try:
        result = runtime.run()  # 回放逐事件推进，内部完成 backend.finish()。
        backend = runtime.backend
        orders = backend.engine.trader.generate_orders_report()
        fills = backend.engine.trader.generate_fills_report()
        return result, orders, fills
    finally:
        runtime.stop()
```

## 11. 常见错误与排查

| 现象 | 重点检查 |
| --- | --- |
| `backend_id不能为空` | 传入非空字符串，避免只含空格 |
| `模拟Venue已注册` | 是否对同一个 Venue 重复 add_profile |
| 静态配置不能修改 | 是否已经 start、run、逐事件推进、完成或释放；合约须提前注册 |
| `订单Backend不匹配` | `OrderIntent.backend_id` 是否与 Backend 的原始标识完全一致 |
| 找不到模拟合约 | 是否注册了准确的合约身份，订单和行情使用的交易所后缀是否一致 |
| 目标提交却没有订单 | 查执行路由、目标差量、客户端启动、风控及回报冲突；Backend 不负责从信号自动规划订单 |
| 有订单却没有成交 | 查网关是否启动、是否有后续行情、订单价格/数量和 Profile 模型、原生拒单报告 |
| `result()` 为 `None` | 是否只 start 或尚未 finish；当前 Runtime 是否实际处理过模拟行情 |
| 不能再次运行或启动 | 实例是否已经完成或释放；新回测创建新 Backend |
| 原生订单表与 `backend.reports` 数量不同 | 原生表含所有相关原生订单，统一回报只由内部网关映射，且一笔订单可有多个状态回报 |
| 同 bar 成交或重复推进 | 查 adapter 与 Runner 回调注册顺序，是否重复发送标准 Bar 与模型 CustomBar |
| 无法导入 `bomber.backtest` 或原生扩展 | `pro` 是本地 framework overlay，须配置可用的已安装 Bomber 引擎及匹配的 Python 环境 |

相关回归：[模拟 Backend 分步测试](../../../tests/run_simulation_backend.py)、[CTP 基础模拟](../../../tests/run_ctp_simulation.py)、[统一历史运行时](../../../tests/run_unified_historical_runtime.py)、[行情桥接](../../../tests/run_market_stream_adapter.py)。

相关实现：[Profile 配置](../../../bomber/framework/trader/execution/simulation/profile.py)、[CTP 基础 Profile](../../../bomber/framework/trader/execution/simulation/ctp.py)、[执行组件装配](../../../bomber/framework/trader/execution/builders.py)、[行情适配器](../../../bomber/framework/trader/runtime/market_adapter.py)。

相关说明：[CtpFuturesBasicProfile](CtpFuturesBasicProfile.md)、[venue_for_product](../dataprep/venue_for_product.md)、[SessionCalendar](../calendars/SessionCalendar.md)。完整入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
