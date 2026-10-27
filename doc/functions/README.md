# 函数说明索引

本目录集中说明函数的用途、参数、输入文件、返回结果和使用约束。按模块分类，每个函数单独一篇文档，文件名使用函数名，方便按名称和参数检索。

## 函数列表

| 函数 | 所属模块 | 用途 | 查询关键词 |
| --- | --- | --- | --- |
| [input_session](dataprep/input_session.md) | `bomber.framework.dataprep.session` | 管理单次运行的输入会话，复用缓存、文件版本和诊断，异常时尝试输出失败报告 | 装饰器、InputSession、嵌套调用、读取缓存、输入诊断、report_dir |
| [BarReadSpec](dataprep/BarReadSpec.md) | `bomber.framework.dataprep.contracts` | 声明 Bar 字段、时间、交易日、可用时间和数值校验规则 | OHLCV、timestamp_label、field_mapping、available_column、value_policy、成交量 |
| [PreparedFrameReader](dataprep/PreparedFrameReader.md) | `bomber.framework.dataprep.bars` | 将已准备 DataFrame 适配为逐行读取接口，复用标准化行情 | Reader、DataFrame、PreparedFrame、内存回放、行号、避免二次读取 |
| [prepare_role_research](dataprep/prepare_role_research.md) | `bomber.framework.dataprep.scenarios.role_futures` | 准备期货合约角色、行情时间范围和累计复权因子快照 | 主力、次主力、角色表、分钟时间标签、复权、factor_availability |
| [venue_for_product（venue）](dataprep/venue_for_product.md) | `bomber.framework.dataprep.metadata` | 从基础资料查找品种唯一交易所并统一来源编码 | 交易所、exchangeCD、品种、Venue、VENUES、CONFLICTING_METADATA |
| [SessionCalendar](calendars/SessionCalendar.md) | `bomber.framework.calendars` | 组合交易时段、交易日和日期例外，查询交易窗口与完整交易分钟 | SessionBinding、夜盘归属、交易日历、时区、时段例外、分钟查询 |
| [next_minutes](calendars/next_minutes.md) | `bomber.framework.calendars.sessions.SessionCalendar` | 按品种时段和交易日历查找后续交易分钟 | 持仓到期、跨休市、夜盘、节假日、结束时间 |
| [NautilusSimExecutionBackend](trader/NautilusSimExecutionBackend.md) | `bomber.framework.trader` | 管理模拟引擎、交易场所、行情推进、订单和执行回报 | BacktestEngine、模拟撮合、Profile、OrderIntent、回测生命周期、finish、stop |
| [CtpFuturesBasicProfile](trader/CtpFuturesBasicProfile.md) | `bomber.framework.trader` | 配置 CTP 基础净持仓模拟账户、固定每手手续费和期货合约条款 | CNY、NETTING、保证金、合约乘数、手续费、make_instrument |
| [build_simulation_execution](trader/build_simulation_execution.md) | `bomber.framework.trader.execution.builders` | 装配共享仓位与参考价的模拟客户端、目标订单规划及前置风控 | ExecutionComponents、SimulationExecutionClient、目标、在途量、共享状态 |
| [RiskLimits](trader/RiskLimits.md) | `bomber.framework.trader.execution.risk` | 声明单合约数量、预计净仓、名义金额和行情时效阈值 | 风控、合约乘数、参考价、RiskRejected、持仓上限、max_market_age_ns |
| [MarketStreamBinding](trader/MarketStreamBinding.md) | `bomber.framework.trader.runtime.market_adapter` | 声明送入模拟撮合的合约、事件类型、周期和订阅参数 | BAR、TRADE_TICK、QUOTE_TICK、bar_spec、行情绑定 |
| [NautilusMarketFeedAdapter](trader/NautilusMarketFeedAdapter.md) | `bomber.framework.trader.runtime.market_adapter` | 过滤并串行转发Feed行情，推进模拟Backend | manage_lifecycle、模拟时钟、回调顺序、事件统计 |
| [UnifiedStrategyRunner](trader/UnifiedStrategyRunner.md) | `bomber.framework.trader.runner` | 装配策略、行情及执行客户端，汇总和路由目标并分发执行事件 | DataBinding、ExecutionRoute、组合目标、PositionManager、run_replay |
| [RuntimeMode](trader/RuntimeMode.md) | `bomber.framework.trader.contracts` | 声明历史回放或实时事件环境，执行对应模式约束 | HISTORICAL、LIVE、historical、live、运行模式 |
| [FixedInstrumentBarParser](market/FixedInstrumentBarParser.md) | `bomber.framework.market.replay.parsers.bar` | 将固定合约的逐行 OHLCV 转为标准 Bar，按需生成伴生 CustomBar | 字段映射、固定合约、available_ns、原始精度、CustomBar、成交额、持仓量 |

## 目录约定

```text
doc/functions/
├── README.md
├── dataprep/
│   ├── BarReadSpec.md
│   ├── PreparedFrameReader.md
│   ├── input_session.md
│   ├── prepare_role_research.md
│   └── venue_for_product.md
├── calendars/
│   ├── SessionCalendar.md
│   └── next_minutes.md
├── trader/
│   ├── NautilusSimExecutionBackend.md
│   ├── CtpFuturesBasicProfile.md
│   ├── build_simulation_execution.md
│   ├── RiskLimits.md
│   ├── MarketStreamBinding.md
│   ├── NautilusMarketFeedAdapter.md
│   ├── UnifiedStrategyRunner.md
│   └── RuntimeMode.md
└── market/
    └── FixedInstrumentBarParser.md
```

新增说明时按实际模块建立分类，例如 `dataprep`、`datahub`、`market`、`trader`；同名函数来自不同模块时，在所属模块的分类目录内区分，不把多份解释堆在同一个文件中。

## 如何查询

在项目根目录执行：

```bash
rg --files doc/functions
rg -n 'prepare_role_research|factor_availability' doc/functions
```

每篇说明应包含：

1. 完整导入路径、源码链接及核对日期。
2. 函数用途和适用场景。
3. 完整签名、参数类型、默认值、参数之间的约束。
4. 输入数据格式与时间约定。
5. 返回结果及调用示例。
6. 常见错误、容易混淆的概念及相关调用入口。

修改函数行为时同步更新对应说明，新增文档时同步更新本索引。示例声明的假设、源码默认值和某个策略入口的默认值应分别写明。

[返回项目文档导航](../README.md)
