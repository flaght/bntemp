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
| [StrategyBindings](trader/StrategyBindings.md) | `bomber.framework.trader.assembly` | 集中声明策略数据、执行路由、角色守卫和时钟来源 | data、execution、RoleGuard、装配声明 |
| [DataBinding](trader/DataBinding.md) | `bomber.framework.trader.contracts` | 将Feed真实合约行情映射到策略逻辑输入 | data_key、feed_id、CUSTOM_BAR、行情分发 |
| [ExecutionRoute](trader/ExecutionRoute.md) | `bomber.framework.trader.contracts` | 将策略逻辑目标静态映射到执行客户端和真实合约 | target_key、client_id、静态路由、账户目标 |
| [assemble_strategy](trader/assemble_strategy.md) | `bomber.framework.trader.assembly` | 校验共享状态并注册策略、Feed、观察者及执行客户端 | 组件注册、共享仓位、固定角色、装配顺序 |
| [UnifiedHistoricalRuntime](trader/UnifiedHistoricalRuntime.md) | `bomber.framework.trader.runtime.historical` | 协调正式历史回测的启动、回放、完成、报告窗口和停止 | 下一事件成交、UnifiedHistoricalResult、finish、生命周期 |
| [RecordingExecutionLifecycle](trader/RecordingExecutionLifecycle.md) | `bomber.framework.trader.runtime.live.local` | 管理只记录目标的Live会话就绪、检查和验收 | Recording、目标请求、无成交、生命周期、历史预热 |
| [CtpMarketSessionLifecycle](trader/CtpMarketSessionLifecycle.md) | `bomber.framework.trader.runtime.live.channels.ctp_market` | 提供独立CTP行情配置、复用本地执行生命周期并保存停机MD观测日 | MD、凭据、realtime、replay、交易日、多继承 |
| [build_simnow_transport](trader/build_simnow_transport.md) | `bomber.framework.trader.runtime.live.channels.ctp` | 读取SimNow交易配置与请求超时，调用工厂创建Transport | 环境变量、BrokerID、9999、CTP_TD_TIMEOUT_SECONDS、flow_path |
| [CtpTdApiTransport](trader/CtpTdApiTransport.md) | `bomber.framework.trader.execution.ctp` | 管理原生TD连接握手、分片查询、报撤单传输与资源释放 | connect、activate、查询超时、迟到回报、资金、持仓、订单 |
| [FixedInstrumentBarParser](market/FixedInstrumentBarParser.md) | `bomber.framework.market.replay.parsers.bar` | 将固定合约的逐行 OHLCV 转为标准 Bar，按需生成伴生 CustomBar | 字段映射、固定合约、available_ns、原始精度、CustomBar、成交额、持仓量 |
| [futures_bar_values](market/futures_bar_values.md) | `bomber.framework.market.futures` | 提取期货CustomBar数值并按成交额、成交量、乘数计算及校验VWAP | 原始精度、openint、value、未复权、VWAP、合约乘数 |
| [source_config](market/source_config.md) | `bomber.framework.market.sources` | 取得已解析行情配置或按参数创建回退配置 | MarketSourceConfig、CTP、DolphinDB、配置优先级 |
| [execution_policy_from_args](trader/execution_policy_from_args.md) | `bomber.framework.trader.execution.policy` | 解析账户对账与策略仓位来源政策并校验组合 | strict、observe、off、fills、model、ExecutionPolicy |
| [HybridLiveReport](demos/HybridLiveReport.md) | `demos.10_hybrid_transformer.run_live` | 写出Hybrid信号、批次、策略状态及公共运行报告 | signals.csv、batches.csv、strategy_state、summary |
| [HybridSessionLifecycle](demos/HybridSessionLifecycle.md) | `demos.10_hybrid_transformer.run_live` | 管理Hybrid的CTP会话、恢复校验、分钟轮询与到期验收 | 同日恢复、零信号、hold目标、expire_due、SimNow |
| [ManagedLiveRuntime](trader/ManagedLiveRuntime.md) | `bomber.framework.trader.runtime.live.runtime` | 协调Live准备、装配、启动、轮询、停机和报告 | 回调、ExitStack、seconds、run、start、stop |
| [prepare_inputs](demos/prepare_inputs.md) | `demos.10_hybrid_transformer.run_live` | 读取参考资料、加载日历并创建上游行情 | LiveRunContext、DolphinDB、来源日、CachedRoleReferences |
| [assemble](demos/assemble.md) | `demos.10_hybrid_transformer.run_live` | 装配Hybrid分钟、执行、模型、策略、绑定和持久化会话 | Recording、SimNow、模型加载、SessionReadiness、manager |
| [ExecutionPolicy](trader/ExecutionPolicy.md) | `bomber.framework.trader.execution.policy` | 声明账户对账与仓位来源组合，校验恢复一致性 | strict、observe、off、fills、model、snapshot_state |
| [MarketSourceConfig](market/MarketSourceConfig.md) | `bomber.framework.market.sources` | 声明行情来源、分钟语义及来源身份 | file、CTP、DolphinDB、BN-WS、时效、identity |
| [StrategyPositionSource](trader/StrategyPositionSource.md) | `bomber.framework.trader.execution.policy` | 枚举策略仓位的执行事实来源 | FILLS、MODEL、归属成交、参考价模拟、独立账本 |

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
│   ├── RuntimeMode.md
│   ├── StrategyBindings.md
│   ├── DataBinding.md
│   ├── ExecutionRoute.md
│   ├── assemble_strategy.md
│   ├── UnifiedHistoricalRuntime.md
│   ├── RecordingExecutionLifecycle.md
│   ├── CtpMarketSessionLifecycle.md
│   ├── build_simnow_transport.md
│   ├── CtpTdApiTransport.md
│   ├── execution_policy_from_args.md
│   ├── ManagedLiveRuntime.md
│   ├── ExecutionPolicy.md
│   └── StrategyPositionSource.md
├── market/
│   ├── FixedInstrumentBarParser.md
│   ├── futures_bar_values.md
│   ├── source_config.md
│   └── MarketSourceConfig.md
└── demos/
    ├── HybridLiveReport.md
    ├── HybridSessionLifecycle.md
    ├── prepare_inputs.md
    └── assemble.md
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
