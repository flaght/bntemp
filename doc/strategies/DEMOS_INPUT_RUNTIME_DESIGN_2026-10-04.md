# Demos 公共输入层与统一运行设计执行方案

日期：2026-10-04。适用项目：`/Users/kerry/work/bomber1/pro`。文档状态：开发设计，尚未开始实现。

本文用于将 demos 的重复输入和运行装配拆成公共模块，供开发者按工作包逐步实现。执行顺序已经确定：第一阶段完成公共 local_input 能力及九个策略迁移，第二阶段统一 backtest 和 live 的装配与入口。两个阶段共用数据和策略契约，但分别验收。

本文中的新模块、类型、方法名都是拟新增设计，不代表已有实现。现有类明确列在“已有组件”中。一次模块开发应交付实现、相关验证和迁移说明；不能仅靠声明接口或创建入口文件标记完成。本文只保存方案，不修改策略或框架代码。

## 1 开发目标和范围

### 1.1 最终使用方式

日常新增或调整策略时，开发者只需定义业务参数、数据需求、信号和目标持仓规则，并组合公共输入场景。公共层承担数据路径解析、文件清单、基本资料、时间标准化、质量检查、参考数据装配、运行生命周期、执行和报告。

每个策略最终保留 `run_backtest.py` 和 `run_live.py` 两个薄入口。两个入口创建同一个策略类，差别由数据服务、时钟、执行组件和运行政策提供。策略不自行打开交易连接，也不根据运行模式复制两套交易算法。

公共输入采用“基础组件 → 场景组装 → 策略所需服务”的方式。它不是将九份 local_input 拼接成一个大文件，也不是在公共函数中按策略名字写大量分支。

### 1.2 第一阶段交付

- 公共路径、文件清单、合约资料、Bar 标准化、日历、角色研究和目标计划能力。
- 角色期货、固定合约、期权链三个可组合场景；外部目标计划作为独立输入组件。
- 九个策略使用公共实现，策略目录只保留需求声明或很薄的兼容组装。
- 统一输入诊断和覆盖报告；保留当前回测组装方式及策略业务行为。
- 明确字段、时间、身份、质量和可见性契约，为第二阶段提供稳定输入。

### 1.3 第二阶段交付

- 公共策略定义、工厂、Feed 与执行组装、时钟、报告和生命周期。
- 九个回测入口迁移为薄入口，并保持已确认的业务语义。
- 在线输入与 Recording 能力按场景逐项实现；支持的策略增加可运行的 live 入口。
- 策略状态恢复、动态换月、多腿和受控柜台执行作为明确工作包，按能力验收。
- 对尚不支持的组合，在启动连接前给出具体拒绝原因。

第二阶段分为“统一装配”“在线数据和目标”“恢复和柜台执行”三个里程碑。入口统一完成不等于九策略生产实盘完成。

### 1.4 本次不改变的业务规则

此次规整保持信号、选约、数量计算、调仓时点、日常退出和目标更新语义。费用、保证金、模拟撮合模型也不因公共化而默认变化。任何必要的业务修正应另列变更项和新基线，不混入等价迁移。

option_delta 保持研究选约，不补充买卖方向、组合、数量、对冲或退出。它的 live 可以是实时选约，不能由入口名称自动变成交易策略。

## 2 当前基线和已有组件

用户已调整此前 examples 与其他策略输入的引用问题。当前 main_ema、cross_section、triangle_spread 都有自身 local_input，本文不将旧引用问题作为待开发事项。公共化的目标是去掉这些实现副本，而不是继续复制文件。

仍需在开发起点核验包导入和正式启动命令。例如 cross_section 当前存在 `.cross_section` 相对导入，main_ema、role_cross 采用兄弟包导入，部分文件混用顶层框架包与 `bomber.framework`。本次只读检查未执行 import 或 help 验证，因此不将这些入口记为已运行通过。

| 已有组件 | 保留用途 | 本次新增工作的边界 |
|---|---|---|
| market 的 RowReader、FeatherReader、RowParser、FileReplayFeed | 文件读取、解析与事件回放 | 公共输入提供读取规格和源计划，不重写 Feed |
| FixedInstrumentBarParser、MappedBarParser、BarColumns | 标准 Bar 与扩展字段映射 | 补齐统一时间口径和研究快照适配，避免重复解析 |
| market 的标准行情类型和 InstrumentMeta | 行情及订阅契约 | 不自创另一套交易行情事件类型 |
| RolePriceStore、SectorRoleStore、MinimalDataHub | 角色、因果复权和研究快照 | 公共输入负责文件适配和 Store 装配，保留因果算法 |
| ReferenceRecord、AsOfQuery、DataHub | 参考数据可见性检查 | 按需要复用，不要求阶段一重写所有旧 Store |
| TargetScheduleStore | 外部目标计划 | 公共 CSV 适配器继续构造现有对象 |
| StrategyTemplate、DataBinding、ExecutionRoute | 同一策略、逻辑数据键与目标路由 | 第二阶段提供声明到现有对象的组装 |
| UnifiedStrategyRunner、UnifiedHistoricalRuntime | 策略生命周期与原生模拟回测 | 第二阶段集中使用，保留撮合与回调顺序 |
| DirectLiveRuntime、RecordingExecutionClient | 在线生命周期与目标记录 | 作为在线开发基础，不视为成交或仓位服务 |
| ControlledLiveExecutionClient、RuntimeStateManager | 受控执行及平台状态保存 | 按当前能力接入；另补策略内部状态与目标版本恢复 |

已确认的限制：Runner 当前禁止 LIVE 使用 DynamicExecutionRoute；标准实时分钟聚合与期权整分钟时间要求不一致；实时 CTP Tick 到期权所需 CustomBar 的完整路径尚需实现。真实数据回放和柜台验收必须由具备数据、原生运行时和连接环境的开发环境完成。

当前 pro 目录不是 Git checkout。工作包回滚不能默认依赖 git revert，应先确定版本管理或保存带清单及哈希的来源快照。

## 3 模块归属和依赖规则

建议公共输入放在项目顶层 `dataprep` 包，作为应用输入装配层。公共运行放在 `trader/application`。直接面向所有策略的公共能力不放在某个 demo 目录下。

依赖方向如下：

```text
demos 的需求声明和策略工厂
          ↓
dataprep 场景组装         trader/application 运行组装
          ↓                        ↓
market 读取和行情契约   datahub 参考数据   trader 运行与执行
```

规则：

1. dataprep 可以使用 market 的读取和解析接口、datahub 的参考数据对象；market 和 datahub 不反向依赖 dataprep 或 demos。
2. dataprep 不创建 Runner、交易客户端、账户或订单，不计算策略信号和绩效。
3. 合约条款标准化属于 dataprep；原生可交易 Instrument 的构造属于 trader 的公共工厂，复用已有 Profile。该工厂通过值参数或输入无关的 Protocol 接收条款，避免底层执行依赖应用场景。工厂可以在第一阶段抽取，供旧入口继续使用。
4. 场景可组合公共组件；场景名称描述数据形态，不采用 main_ema_loader 等策略专名。
5. 策略定义声明数据与目标；运行组装将声明转成 DataBinding、ExecutionRoute 等现有对象。
6. dataprep 的基础配置和契约尽量使用标准库类型；pandas、pyarrow 与原生交易类型在适配模块中加载，避免纯契约测试依赖整个原生运行时。

### 3.1 拟新增目录

```text
dataprep/
  __init__.py                 稳定公共导出
  contracts.py                输入需求、读取规格、结果和诊断契约
  paths.py                    配置解析和路径来源
  catalog.py                  文件清单、身份索引和覆盖计划
  bars.py                     行情读取、标准化和质量校验
  metadata.py                 期货期权静态条款标准化
  calendar.py                 交易日历及交易时段服务
  references.py               角色记录、锚点和现有 Store 装配
  schedules.py                CSV 目标计划读取
  scenarios/
    role_futures.py           角色期货场景
    fixed_contracts.py        固定真实合约场景
    option_chain.py           期权指数与可选同月期货场景

trader/
  instrument_factory.py       公共原生合约工厂 第一阶段按需抽取
  application/                第二阶段增加
    contracts.py              策略定义和运行配置
    assembly.py               通用注册和生命周期组装
    backtest.py               历史回放与模拟执行
    live.py                   在线数据和执行方式
    reporting.py              公共报告及诊断扩展
    recovery.py               策略恢复与预热协调

demos/<strategy>/
  strategy.py                 或现有 cross_strategy.py
  signal 或 selection 模块   按业务需要保留
  definition.py               第二阶段按需放策略声明 可合入现有配置模块
  run_backtest.py
  run_live.py                  对应能力完成后提供可用入口
  local_input.py              第一阶段迁移兼容层 收口后可删除
  README.md
  results/
```

目录表示职责划分，不要求一次建立所有文件。第一批不引入插件发现、反射加载、动态注册器或配置 DSL；通过显式函数调用完成场景组装。文件合并可以调整，但依赖方向和公共契约必须保持。

## 4 第一阶段输入契约

### 4.1 拟新增类型

| 类型 | 核心内容 | 约束 |
|---|---|---|
| DataPaths | role、fut、opt、index 根目录及资料文件路径，路径来源 | 不可变；未要求的资产路径可以为空 |
| BarFileKey | asset_kind、symbol、venue、trading_day | 匹配身份独立于文件名；保留原始代码与路径 |
| InstrumentSpec | 资产类型、合约代码、品种、交易所、币种、tick、乘数、上市与到期条款；期权含类型、行权价和标的 | 不携带账户、订单或保证金策略；未知字段保持未知 |
| BarReadSpec | 字段映射、必需字段、时区、周期、源 start/end 标签、交易日政策、成交量语义、值校验政策 | 转换规则显式，不能根据数据值猜测 |
| DataRequirements | 资产、品种或合约集合、角色、数据类型、字段、周期、参考服务及同步需求 | 不含文件路径、账号和引擎对象 |
| HistoricalInputRequest | DataRequirements、数据配置、运行区间、预热要求、参考资料发布时间规则 | 将回测区间和辅助资料覆盖分开 |
| SourceSpec | 文件身份、原始路径、已规范化行引用、读取规格、parser 规格及用途 | 区分研究与撮合用途；Feed 消费规范化结果，禁止研究载体进入撮合 |
| BindingSpec | 逻辑 data_key、真实合约身份、数据类型和周期 | 第二阶段再转换为现有 DataBinding，不把 data_key 等同于 target_key |
| InputPlan | 待读资料、必需与可选文件、依赖区间、绑定规格、覆盖要求 | 只描述计划，不产生信号或启动 Feed |
| BarLoadResult | 标准化 DataFrame 或读取视图、身份、首末时间、行数、质量问题 | 不再复制全部行情为另一套事件对象 |
| InputContext | 规范元数据、参考服务、日历、来源清单、诊断 | 与运行模式和执行账户无关 |
| HistoricalInputBundle | InputContext、历史 SourceSpec、BindingSpec、覆盖报告 | 第一阶段交付；不是可自动变成在线流的历史快照 |
| CoverageReport | 请求与实际覆盖、必需缺失、可选缺失、分钟缺口、预热和锚点覆盖、日历来源 | 文件覆盖与分钟覆盖分别记录 |
| InputIssue | code、severity、来源、资产身份、交易日、字段、时间、说明 | 能关联到文件和原始行，便于报告与排错 |

InstrumentSpec 的来源版本及可用时间应被保留。只有最终静态快照且没有历史发布时间资料时，应记录其来源假设，不能声称已完成严格历史版本验证。index 资料用于信号时可以缺交易乘数；准备执行路由前，期货与期权的 tick、乘数、交易所等必需条款必须完整。

第一阶段保留各策略现有 Option、Contract 等业务对象，通过薄转换构造它们；不强制九个策略同步重写业务模型。

A1 首批只冻结 DataPaths、BarFileKey、BarReadSpec、DataRequirements 和 InputIssue。元数据、计划及 Bundle 随对应工作包加入，避免在没有消费者时一次实现全部类型。场景输出中集合的键、空值含义和必需字段必须确定，不能以任意字典掩盖契约。

### 4.2 拟新增公共接口

下列签名表达输入与输出契约，不是要求立即照抄的代码实现。

| 接口 | 输出 | 职责 |
|---|---|---|
| resolve_paths(overrides, env, required_kinds, project_root) | DataPaths | 确定唯一有效路径及来源，校验所需目录 |
| scan_bar_files(paths, kinds, date_ranges) | 文件索引 | 按身份扫描一次，发现重复文件和无法确认的身份 |
| select_bar_files(index, required, optional) | 文件计划与 CoverageReport | 按需求查找，不根据缺文件替换业务候选 |
| load_futures_basic(path, publication_policy) | 规范期货资料目录 | 校验身份、交易所、乘数、tick、生命周期和版本冲突 |
| load_options_basic(path, tick_override, publication_policy) | 规范期权资料目录 | 校验条款；不内置 MO 的交易方向或期限选择 |
| read_bar_frame(file, spec) | BarLoadResult | 字段、身份、时间和质量标准化 |
| load_calendar(path, required_range) | 日历服务及来源 | 校验交易日和覆盖范围 |
| infer_calendar_from_inventory(index, coverage_assumptions) | 标记为推导的日历 | 兼容当前数据，保留推导前提和不确定覆盖 |
| load_role_assignments(path, products, required_roles, publication_policy) | 角色记录 | 按需求读取角色，保持此前日终资料可见性 |
| load_roll_anchor_closes(files, time_spec) | ObservedClose 集合 | 加载角色换约所需日终锚点 |
| load_role_minute_closes(files, time_spec) | 分钟研究价格 | role_cross 等确需分钟研究快照时使用 |
| build_role_store(assignments, closes, roll_policy) | 现有 RolePriceStore | 委托现有因果复权算法 |
| build_sector_store(role_stores, signal_roles, source_day_policy) | 现有 SectorRoleStore | 按多个品种角色组装现有研究接口 |
| load_target_csv(path, timezone) | 现有 TargetScheduleStore | 解析时点和完整目标快照，不执行计划 |
| plan_role_futures / plan_fixed_contracts / plan_option_chain | InputPlan | 组合相应公共能力，说明所需依赖 |
| prepare_role_futures / prepare_fixed_contracts / prepare_option_chain | HistoricalInputBundle | 执行计划、规范化并产生报告，不启动引擎 |

同一计划的准备过程应复用扫描索引和基本资料缓存。场景之间复用输入组件，不相互导入某个策略的 local_input。既有读取签名可以暂时保留，通过兼容层转发到这些公共函数。

第一版 SourceSpec 引用 BarLoadResult 的已规范化行，由公共 PreparedFrameReader 实现现有 RowReader 接口，再通过 Feed.add_source 送入现有 Parser。原始路径只用于身份、来源和错误上下文。可复用 option_vega 当前 FrameReader 的模式；不得在验证和时间转换后，又通过 add_bar_feather 原样读取一次源文件，否则会丢失时间转换、质量标记和读取缓存。Parser 负责生成标准事件，不再次移动分钟标签。后续若改为按需规范化 Reader，必须维持同样的单次转换与诊断契约，并单独验证覆盖检查的读取成本。

### 4.3 输入准备流程

1. 策略或薄需求模块给出 DataRequirements 和业务候选集合。
2. resolve_paths 解析最终数据配置，并记录每个值的来源。
3. 读取基本资料和日历，得到规范身份、生命周期及可见的参考资料。
4. 场景生成 InputPlan，分别列出运行行情、预热、角色来源日和换约锚点。
5. 公共目录索引匹配必需和可选文件，生成文件覆盖报告。
6. 按 BarReadSpec 读取、转换和验证，生成分钟覆盖及质量报告。
7. 按需求构造参考 Store、SourceSpec 与 BindingSpec，返回 HistoricalInputBundle。
8. 旧回测入口消费 Bundle，继续使用现有 Feed、模拟客户端和 Runtime。

第一阶段可以调整入口的数据准备调用，但不同时搬迁完整 Runner 组装。输入等价通过后，再进入第二阶段。

## 5 路径和配置规则

统一采用“显式调用或 CLI 参数 → 配置文件 → 具体环境变量 → CTP 根目录派生 → 旧环境变量兼容 → 有文档的默认值”的优先顺序。同一层出现矛盾别名应报错；覆盖低优先级值时，诊断记录最终来源。

第一阶段先统一数据配置对象，不强制新增配置文件格式。第二阶段需要落盘配置时，建议 TOML，分 strategy、data、runtime、execution、report 组；连接密钥继续从环境读取，不写入配置快照或报告。

| 配置 | 规则 |
|---|---|
| CTP_DATA_DIR | 根目录可派生 role、kline/fut、kline/opt、kline/index；派生不覆盖显式配置 |
| FUT_ROLE_DATA_DIR | 只代表参考资料目录，不能当作行情目录 |
| FUT_KLINE_DATA_DIR、OPT_KLINE_DATA_DIR、INDEX_KLINE_DATA_DIR | 对应具体资产行情目录 |
| FUT_KLINE_DATA_DIR 或兼容 KLINE_DIR 指向父目录 | 仅在明确目录模式下解析 fut 子目录，并记录解析结果；禁止各策略自行猜测 |
| ROLE_DIR、KLINE_DIR、KLINE_DATA_DIR | 迁移期保留兼容映射和提示，不能存在多套不同解析规则 |
| contract_struct、fut_basic、opt_basic | 显式文件优先；角色表默认优先 fut_contract.feather；若只有旧 fut_contract_data.feather 可兼容；两者同时存在需明确选择 |
| targets 和配置文件相对路径 | 明确 project_root 为解析基准；若兼容当前目录，应记录实际解析位置，避免 demos/demos 拼接 |

只有一个资产场景被要求时，不强制检查无用目录。例如 option_delta 的 BS 场景不检查期货行情目录；scheduled_targets 不要求角色表。

第一阶段保留既有参数名字。默认文件名、路径基准或缺失处理与旧版不同的部分，应作为显式兼容规则记录，不能隐藏为纯等价重构。

## 6 时间和行情质量契约

### 6.1 时间字段

| 时间概念 | 含义 |
|---|---|
| trading_day | 交易所交易日；可与夜盘自然日期不同 |
| source timestamp | 原始文件或行情源标签 |
| event_ns | 完成 Bar 的统一结束标签，或其他事件的发生时间 |
| available_ns | 数据实际或按已声明回放假设最早可供决策的时间 |
| source_ns | 参考数据真实源时间，用于年龄和日终发布检查 |
| ts_init | 现有回放排序和事件投递使用的初始化时间；转换时必须与可用时间政策一致 |

拟采用的目标规范：一分钟 Bar 的 event_ns 为精确整分钟结束时刻。start 标签加一个周期，end 标签保持原值；不得转换两次。实际到达时间另行保留，不能将网络迟到的 Bar 伪装成准时可用。

现 FileReplayFeed 按 ts_init 排序，所以仅修改研究价格的 event_ns 不够。标准 Bar、CustomBar、日终锚点和时钟必须使用同一转换后的时间规范，且事件不得早于完成时间交给策略。历史没有实际到达记录时，默认“完成时刻可用”的回放假设应写入报告。

现 MappedBarParser 固定设置 ts_init 等于 ts_event，FixedInstrumentBarParser 沿用这一行为。A2 应补可选的可用时间字段映射，默认保持完成时刻可用；不能只在 DataFrame 中存 available_ns 而不传到事件投递时间。携带实际到达轨迹的来源必须同时声明所需的迟到及乱序能力：若按可用时间投递会使现有策略收到 event_ns 回退，而当前同步无法处理，第一阶段诊断后拒绝该运行，不标记九策略已支持任意延迟轨迹。对应完整迟到处理由 B3 实现。

现 ObservedClose 仅携带源事件时间。非零发布延迟的换约锚点还需在参考服务外层保留发布时间并门控因子可见性；仅增加 Bar 的 ts_init 不足以保护参考查询。A5 对未发布锚点应拒绝快照或报告能力未实现，不能静默使用提前算好的因子。

当前实时聚合采用结束减 1 纳秒；统一到整分钟属于显式时间契约变更。实施时先确认真实文件 start/end 含义，保存原始标签；将新旧结果映射到同一完成分钟后比较。任何由此改变的交易时点应列明，不用时间容差掩盖整分钟或跨帧差异。

### 6.2 交易日和参考资料

- 股指日盘可以校验自然日期与交易日一致；商品夜盘按交易日列、文件目录和交易日历校验，不能统一要求 datetime.date 等于目录日。
- 角色日终资料只能在已发布且来源交易日早于当前交易日时使用；不能用“自然日减一”替代交易日。
- 日终换约锚点与分钟研究快照是两种数据需求。black_sector 等的日终锚点不能替代 role_cross 的分钟快照。
- 预加载 Store 可供 as-of 查询，不意味着本分钟真实 Bar 已送达；角色策略的实际多腿事件同步仍必须保留。
- 日历不足以判断期权剩余交易日时，应指出所需覆盖，不能将未知剩余天数当作零。
- 显式日历优先。基于文件日期推导日历仅在来源和完整性前提可说明时使用，报告标记 inferred；不得由“某天无文件”推断该天一定休市。

### 6.3 数据值和缺失政策

| 政策 | 使用场景 | 处理方式 |
|---|---|---|
| execution_strict | 送撮合的期货或期权 OHLCV | 无效价格、OHLC 关系或成交量立即失败，说明身份和原始行 |
| research_audited | option_delta 等候选研究快照 | 保留候选坏值及质量标记，由研究核记录拒绝原因；不伪造有效价格 |
| required_missing | 指数、必需执行腿、持仓旧腿、必要参考资料 | 明确报错，不截短区间冒充成功 |
| optional_missing | 可选候选合约行情 | 在覆盖与候选审计中记录，不替换目标月份或角色 |

结构、身份、冲突条款、无效时间和重复分钟属于公共硬错误。完全相同的参考记录是否合并沿用对应现有数据集政策；冲突价格或冲突版本不能按读取顺序随意选择。

volume 明确声明为分钟增量。若源为累计量，只能按显式源规格在合约及交易日内差分，保留首条、重置和负差分政策；不能猜测或重复差分。open_interest 是时点值，不差分。默认不前填价格、不补零成交 Bar、不复制旧报价。

研究 CustomBar 若沿用内部载体价格实现，真实研究 close 必须独立保留，载体不能进入执行回放、参考成交价或研究结果。zero_volume_bar 与最后真实成交更新时间是不同概念；缺少后者时不声称检测了真实成交时效。

### 6.4 同步边界

第一阶段仅规范事件和保留现有同步行为，不将不同策略的同步算法一并重写。第二阶段再公共化完整分钟帧、封口、晚到、重复和乱序处理。

严格组合以所声明的必需腿完整到齐为准；期权链可包含没有本分钟行情的候选，必须留下缺候选审计，不能要求整个静态全集每分钟都有 Bar。任何场景都不得拿下一帧价格补当前帧。

option_delta 当前在下一时间戳处理上一帧，停止时可 flush 研究报告。迁移时先保持此行为；在线帧封口采用时钟或水位线时，另列时序变更和对应验证。

## 7 场景组装和九策略映射

### 7.1 三种场景

role_futures 组合期货资料、角色记录、因果锚点、真实行情和可选分钟研究快照。它接收“需要哪些品种和角色”，不写收益排名、EMA 或零轴穿越。

fixed_contracts 组合真实合约集合、期货资料、原始价格行情和所需覆盖。calendar_spread 的角色选约规则在策略的声明或 selection 模块中完成，输出真实合约集合后复用此场景。场景不依据当日整天文件存在情况替用户换合约。

option_chain 组合期权静态全集、指数、按需求声明的期货和日历。研究规格与执行规格复用读取核心，但必需字段和坏值政策不同。期限、Call/Put、Delta 排名、可对冲候选政策仍属于策略，不嵌入公共加载器。

目标计划通过 schedules 独立加载，再与 fixed_contracts 组合。它不是第四种行情类型。

### 7.2 策略输入清单

| 策略 | 场景和公共输入 | 留在策略的规则 |
|---|---|---|
| main_ema | role_futures；主力、日终因果锚点、真实主力及必要旧合约 Bar、期货资料 | EMA、预热、方向、目标手数 |
| cross_section | role_futures；多品种主力、因子、同步真实 Bar | 收益排名、代码打破平局、调仓周期、名义金额配仓 |
| black_sector | role_futures；多个信号品种角色与执行角色 | 领头比较映射、两层平滑、方向、执行延迟 |
| role_cross | role_futures；main、secondary、far、分钟研究 close 和真实 Bar | 三角色穿越、首次预热、实际事件到达确认 |
| calendar_spread | 角色参考组件加 fixed_contracts；两个不同真实合约及旧组合行情 | 角色对选择、原价 Z-score、缺角色政策、换约清窗 |
| triangle_spread | role_futures；三品种主力、因果因子、真实 Bar | 初始归一化、权重、Z-score、金额与手数分配 |
| scheduled_targets | schedules 加 fixed_contracts；计划、固定期货资料和 Bar | 独立时钟、完整目标替换、错过不追单 |
| option_vega | option_chain；期权期货指数 OHLCV、静态条款、交易日历 | 同月可对冲选约、Gamma/Vega 配仓、实际仓位对冲、日常退出 |
| option_delta | option_chain；期权 close/OI/分钟 volume、指数 close；Black76 才加同月期货 | 先定期限、Delta 区间、排序、候选拒绝；仅研究 |

Vega 的同月可对冲性由策略政策读取公共上市状态判断；Delta 基于可见静态全集选定期限，不能因该月行情缺失而换到其他月份。calendar 的 next-available 保留为显式研究选项并标注当日文件可用性前视风险，严格默认继续采用 raise。

策略侧组装结果单独保留，例如 calendar 的按日 CalendarSelection 序列、Vega 的 options_by_day 和 skipped_hedges。输入规划可以使用对应的真实合约并集安排读取，但不能丢掉按日生效、候选和拒绝原因。此类 assembly_data 属于策略声明或工厂输出，与 InputContext 中的公共事实 Provider 分开；公共场景不依赖 CalendarSelection 或 OptionVegaConfig 等业务类型。

## 8 覆盖检查 缓存和性能

输入规划分别列出运行区间、预热区间、角色来源日、换约锚点和期权日历依赖范围。不能简单按 start/end 裁剪掉前一交易日或必要旧合约。

覆盖报告至少记录：请求与实际交易日、每资产及合约的文件覆盖、必需字段、分钟首末时间、缺口、必需旧合约、日历来源和范围、推导假设、输入标准化版本。缺文件与无行情分钟不能混为一种缺口。

第一阶段先使用单次运行内缓存，不做持久磁盘缓存：

- 同一根目录扫描一次，按资产、合约和交易日索引。
- 同一基本资料读取一次，按代码、品种和交易所索引。
- 同一文件读取所需列，日终摘要尽量只读 timestamp/close。
- 缓存键包括绝对路径、文件大小、修改时间、读取列、时间规格和质量政策；文件变化后拒绝使用旧结果或重新读取。
- DataFrame 不能被另一个场景原地修改；时间转换返回新视图或受控副本。
- 输入文件稳定性检查与审计哈希分开：来源快照和回归基线保存内容哈希，大型日常扫描可用清单加文件属性，注明证据强度。

公共读取不额外复制全部事件。第一阶段仍使用 FileReplayFeed 当前加载方式，不顺带开发流式回放引擎。性能记录覆盖扫描次数、读取次数、耗时和峰值内存；约束是相同请求不得重复全目录扫描、不得重复读取同一规格。固定耗时上限由基线环境实测确定。

## 9 第二阶段策略定义与运行配置

### 9.1 StrategyDefinition

拟新增 StrategyDefinition 至少包含以下声明：

| 字段 | 内容 |
|---|---|
| name 和 strategy_id 生成规则 | 策略类型与实例身份分开 |
| config_factory | 校验业务参数并生成现有策略配置 |
| data_requirements | 逻辑键、资产、角色、字段、周期及同步政策 |
| target_requirements | 真实固定目标、逻辑角色目标或组合目标；无交易策略可为空 |
| reference_requirements | 角色、因子、合约目录、日历等服务 |
| clock_requirements | 依赖 Bar、独立时点或两者 |
| build_strategy(config, services) | 构造同一 StrategyTemplate；只注入所需服务 |
| warmup 和 recovery_requirements | 预热范围、内部状态、重复事件及版本恢复要求 |
| diagnostic_exports | 信号、选约、日程等公开诊断接口 |
| capabilities_required | 在线角色更新、动态换月、CustomBar、实际仓位反馈等能力 |

services 是限制过的依赖集合，例如角色 Provider、日历、规范条款和报告回调。不得把完整 Runner、原始账户、文件扫描器或柜台 SDK 对象传入策略。

数量预算和对冲规则属于策略配置，执行风控上限属于平台配置。DataBinding 与 ExecutionRoute 分开：指数只用于信号可没有目标路由；option_delta 可以没有执行客户端；scheduled_targets 通过独立时钟触发目标，不要求用 Bar 产生信号。

### 9.2 运行配置

| 配置组 | 内容 |
|---|---|
| strategy | 业务参数及实例身份 |
| data | 输入路径或在线源、时间规格、质量政策、参考发布政策 |
| runtime | historical/live、预热、结束或停机政策、状态目录 |
| execution | 模拟模型或 recording/受控柜台、账户标识、平台风险限制 |
| report | 根目录、诊断导出、日志和审计要求 |

RuntimeMode 仍沿用现有 HISTORICAL/LIVE，在线执行方式作为另一配置，不将 SimNow 和 Recording 混成运行时间模式。所有报告保存脱敏后的最终配置、来源和组件版本。

正式开发入口建议从项目根目录使用 `python -m demos.<strategy>.run_backtest` 与对应 run_live。已有直接脚本入口若承诺兼容，应保留薄转发并独立验收；不能以某个工作目录偶然可导入作为支持标准。框架选择一个规范导入源，禁止同一进程混用顶层 market/trader 与另一份 bomber.framework 类定义。

## 10 第二阶段公共运行流程

### 10.1 公共组装

assembly 接收 StrategyDefinition、最终配置、InputContext 和按模式选择的组件，完成能力预检、原生合约构造、Feed/Client/观察者注册、Binding/Route/Clock 注册、策略创建和资源关闭。复用现有 Runner，不重写目标、组合净额和订单状态机。

run_backtest 和 run_live 只负责选择定义与配置、调用公共执行函数、处理退出状态和展示结果路径。允许必要的入口兼容代码，不设没有业务意义的行数指标。

### 10.2 回测流程

1. 解析配置并调用第一阶段输入场景。
2. 使用公共 instrument_factory 创建原生 Instrument 和 InstrumentMeta；模拟保证金及费用通过执行配置传入。
3. 由 SourceSpec 和 BindingSpec 构建 FileReplayFeed、Parser、DataBinding 与时钟。
4. 交易策略装配 SimulationExecutionClient、参考价格、风控、模拟 backend 和 UnifiedHistoricalRuntime。
5. 研究策略装配无执行回放路径，允许使用现有 SimpleReplayRuntime 或等价生命周期封装。
6. 注册并创建同一个策略类，运行、审计、输出报告、关闭资源。

交易回测必须保留“先推进已有订单撮合，再投递策略；本事件产生的新委托最早由后续事件成交”的现有顺序。不能将所有策略简化成直接 runner.run_replay，从而丢失原生撮合时钟与回调顺序。

同时间戳多腿不保证原子成交。等价验收使用同一 backend 和同一事件序列；时间规范调整另列基线。回测结束清仓采用明确运行政策，保留 option_vega 当前终点清仓要求；其他策略未要求终点全平时，不自动添加清仓。

### 10.3 在线流程

第二阶段拟新增 OnlineInputBundle，复用 InputContext 与 DataRequirements，补充 Feed 规格、订阅集合、参考服务更新政策和在线诊断。历史 Bundle 不能简单删除日期参数后冒充在线 Bundle。

在线流程为：配置与能力预检 → 元数据及参考资料准备 → 持久状态读取 → 可信历史预热或策略恢复 → 账户与在途状态对账 → 行情和独立时钟启动 → 进入对应执行状态 → 持续诊断和保存 → 按停机政策关闭。

上述顺序表达进入执行状态的条件，不要求在未连接柜台时凭空完成查询。需要账户的执行方式先连接交易查询通道，在恢复和预热期间保持执行闸门关闭；行情可按受控流程连接并缓存。恢复、订阅准备、行情健康和权威对账全部通过后才允许目标进入执行。research/recording 无账户时明确标记对账不适用，不伪造成功记录。

在线参考服务应支持按交易日发布的角色和因子、合约条款增量更新、期权链与订阅变化、持仓及旧合约订阅保留。不能将按历史文件 first/last Bar 构建的一次性 Store 当作跨日在线服务。

在线合约集合变化必须同代更新 Feed 订阅、Runner 的 DataBinding、ExecutionRoute、风控条款和参考服务。现 Runner 启动后不允许重新 add_strategy，新目标没有路由也会被拒绝，因此 B4 需要公开的受控更新接口或明确受限的会话预注册方案。切换期间暂停相关目标执行；新代数据和路由准备成功后一起生效，失败保持已核验的旧代服务并拒绝新目标。保留持仓及在途旧合约，不能先删除旧路由或旧订阅。数据源新增订阅本身不算在线更新能力完成。

标准 TradeTick 聚合不足以直接满足期权链输入：OI、分钟增量成交量、完整帧、尾部封口、无成交分钟和会话边界都需要明确实现。缺少必要字段或服务时，在预检中拒绝该组合。

## 11 在线能力和恢复边界

### 11.1 能力预检

以“策略所需能力包含于组件所提供能力”为判断依据，避免一个 supports_live 布尔值掩盖差异。能力既描述已实现功能，也标明验证级别；匹配通过后仍需进行运行时健康和账户检查。

| 能力 | 典型需求 | 实现与验收要点 |
|---|---|---|
| completed_minute_bar | 全部分钟策略 | 完成时间、到达时间、会话末尾与零成交政策 |
| option_snapshot_fields | option_delta | close、OI、分钟 volume、bar_ns 和候选缺失审计 |
| role_asof_update | 角色策略 | 前交易日角色发布、因果锚点、按日更新 |
| independent_clock | scheduled_targets 和定时策略 | 无 Bar 时也触发；时区、交易日、去重和错过政策 |
| live_role_roll | main_ema、black_sector、role_cross | Runner 当前禁用路径需单独完成恢复、持久化与换月执行 |
| multi_leg_transition | calendar 和组合策略 | 旧仓与在途确认、部分成交、缺腿和切换状态 |
| actual_position_feedback | option_vega 等 | 权威或已核对的实际仓位、成交与在途量 |
| subscription_update | 角色和期权链在线更新 | 新订阅生效、旧持仓订阅保留、更新失败诊断 |
| strategy_recovery | 连续运行 | 窗口、方向、时点、pending 状态及目标 revision |
| controlled_gateway | 会真实发送委托的在线运行 | 当前柜台能力、账户身份、对账、风控和执行回报 |

当前 LIVE 动态路由禁止规则必须保留，直到该能力的工作包完成；不能为让新入口跑通而删除检查。生产报单能力也不能由 SimNow 验收自动升级。

在线独立时钟保留计划逻辑时点，同时审计实际触发时间。截止触发时已可用的同一时点 Bar 先投递；之后才到达的 Bar 不倒插、不回改已提交计划。若计划需要尚未可用的参考行情，使用预先声明的有限等待期限以及拒绝或 MISSED 政策，不无限等待以制造“先 Bar 后目标”，也不补造错过的成交。数据、帧封口与时钟由公共调度统一排序，避免网络线程和时钟线程竞态导致重复目标。

### 11.2 在线执行方式

| 执行方式 | 含义 | 对验收的限制 |
|---|---|---|
| research | 实时选约与诊断，不产生持仓目标 | option_delta 默认；没有订单、成交或交易 PnL |
| recording | 记录账户目标请求，不发送委托 | 不更新真实仓位，不能证明成交后对冲或完整交易生命周期 |
| controlled test gateway | 对已接入测试柜台受控执行 | 需要单独柜台及账户证据；受当前品种和恢复能力约束 |
| production | 生产账户执行 | 后续独立能力及验收，当前不通过入口参数自动开启 |

option_vega 在 recording 中最多证明无成交情况下的目标路径。成交后对冲应另用可验证的成交反馈夹具、只读真实仓位服务或测试柜台验收，不能用 Recording 的零仓位假装完成闭环。

### 11.3 策略恢复和预热

平台恢复保存目标、组合、仓位、订单及 CTP 账本；策略恢复保存 EMA/Z-score 窗口、方向、归一化基准、已执行时点、待处理信号或帧等业务状态。两部分需要共享版本与明确恢复边界。

检查点使用共同 generation 和 cutoff，校验策略实例、配置与组件版本、角色映射、目标 revision 和已处理事件进度，确保平台目标和策略窗口属于同一决策边界。保存中断、不一致快照或不兼容 schema 必须明确拒绝恢复，不能拼接不同代状态后开放执行。恢复完成仍需本次权威账户和活动订单再对账。

策略目标 revision 必须与恢复后的 TargetStore 协调。StrategyTemplate 当前从零初始化 revision；如果只恢复旧目标而不恢复版本，下一次目标可能被判作旧更新。不得直接操作私有属性作为长期恢复接口。

拟新增公开 checkpoint/restore 契约，或按可信历史重建内部状态。预热期间由公共上下文禁止交易输出；在线切换有明确 cutoff 和事件去重规则。基于历史重建指标不等于恢复柜台持仓，仍需账户与活动订单核对。

没有恢复能力时，只能支持明确标记的受限会话启动条件。当前入口能否跨日、带仓重启或恢复在途订单，要逐策略逐组件记录，不能由默认清零掩盖未知状态。

## 12 公共报告和错误处理

每次运行保存唯一 run_id，建议仍位于 `demos/<strategy>/results/<run_id>/`。公共文件包括 summary、脱敏有效配置、input_manifest、input_coverage 和 input_issues；交易运行再提供 orders、fills、positions、account。策略诊断通过公开导出接口提供 signals、selections、candidates 或 schedule_audit 等内容。

summary 至少标记运行模式、执行方式、research_only、组件及策略版本、数据规范版本、时间和日历假设、验证级别、结束状态、残仓和在途订单。没有成交时不能创建看似有效的交易绩效。

错误分类建议包括 PATH_NOT_FOUND、DUPLICATE_FILE、MISSING_FIELD、IDENTITY_MISMATCH、INVALID_TIMESTAMP、CONFLICTING_METADATA、MISSING_REQUIRED_BAR、INSUFFICIENT_CALENDAR、REFERENCE_NOT_AVAILABLE、UNSUPPORTED_CAPABILITY。已有异常可作为底层 cause；不要为每一错误新增层层类继承。

遇到输入或能力失败应输出已获得的诊断，然后失败退出。迁移期不采用“新公共输入失败后自动回退旧输入”，以免掩盖真实质量或可见性错误。

## 13 第一阶段工作包和迁移顺序

| 工作包 | 依赖 | 交付内容 | 完成条件 |
|---|---|---|---|
| A0 基线与规范 | 无 | 正式启动方式、规范包来源、九策略参数及输入输出基线、数据规范版本、来源快照 | 每个策略有可比较基线或明确未运行原因；旧引用已调整事项不重复处理 |
| A1 契约和路径 | A0 | contracts、paths、诊断结构、配置优先级 | 路径覆盖、冲突、按需目录及兼容变量验证通过 |
| A2 文件清单和 Bar | A1 | catalog、bars、时间与质量规格、缓存 | 身份、字段、start/end、夜盘、坏值和覆盖契约通过 |
| A3 合约资料和日历 | A1，可与 A2 并行 | metadata、calendar、按需原生 Instrument 工厂 | tick/乘数/上市期、期权条款、未知字段和日历范围验证通过 |
| A4 固定合约和目标计划 | A2、A3 | fixed_contracts、schedules、scheduled_targets 试点 | CSV 计划与目标时点不变，真实合约输入及旧入口组装通过 |
| A5 角色研究场景 | A2、A3 | references、role_futures、main_ema 试点 | 上一交易日角色、因果锚点、研究价与真实 Bar 等价 |
| A6 期权链场景 | A2、A3 | option_chain、研究与执行两套读取规格 | Delta 必需字段及候选审计、Vega OHLCV/日历需求分别通过 |
| A7 九策略迁移 | 对应场景完成 | 逐策略薄组装与兼容转发、README 更新 | 业务不变性矩阵与输入对比通过，无公共逻辑副本 |
| A8 阶段一收口 | A4 至 A7 | 接口说明、废弃列表、验证报告、第二阶段输入契约 | 新策略可只声明需求并组合场景；报告区分夹具与真实回放 |

推荐试点顺序：scheduled_targets 验证固定期货和目标计划；main_ema 验证角色及因果复权。随后迁移 cross_section、black_sector、triangle_spread、role_cross、calendar_spread、option_delta、option_vega。

A3 与 A2 可以由不同开发者并行；A6 可以在公共基础冻结后与 A5 并行。角色核心、时间契约和公共条款等共享文件由单一工作包负责，避免多个策略迁移同时修改公共基础。

阶段一只将输入准备公共化，允许旧入口继续进行 Runner 装配。阶段一收口后才开始正式迁移运行层。

## 14 第二阶段工作包和能力扩展

| 工作包 | 依赖 | 交付内容 | 完成条件 |
|---|---|---|---|
| B0 策略定义与运行契约 | A8 | StrategyDefinition、配置分组、公开诊断和能力声明 | 九策略能描述数据、目标、时钟和恢复需求，不接文件或账户 |
| B1 公共回测组装 | B0 | assembly、backtest、reporting、公共原生合约与 Feed 工厂 | 交易、研究、独立时钟三种路径通过；撮合顺序保持 |
| B2 回测入口迁移 | B1 | 九个薄 run_backtest、兼容参数和统一命令 | 相同输入和模拟配置下决策、目标、成交及结束状态可比较 |
| B3 在线基础与帧 | B0，A8 | OnlineInputBundle、完成分钟、CustomBar、在线时钟、参考服务更新和 preflight | 在线字段及时间契约通过；未实现能力启动前被拒绝 |
| B4 恢复与动态换月 | B3 | 策略 checkpoint 或可信预热、revision、订阅更新、动态路由与组合切换 | 重启、断线、部分成交及旧合约持仓场景有证据；现有限制按能力解除 |
| B5 在线入口和 Recording | B1、B3；涉及换月或恢复的策略依赖 B4 对应子项 | 可用 run_live、目标及研究报告 | 同一策略类在线运行；声明边界内的能力有真实在线或夹具证据 |
| B6 受控测试柜台 | B5、B4 的恢复对账子项；按需依赖换月及组合子项 | 受控客户端、账户与订单查询、回报、停机及再对账 | 对具体柜台、账户、策略和品种完成执行验收 |
| B7 第二阶段收口 | B2、已承诺的 B5/B6 | 能力矩阵、运行手册、未支持项和验证报告 | 每种入口明确支持范围；占位文件和 Protocol 不计完成 |

B3 可与 B2 的后半部分并行，不破坏第一阶段冻结的输入契约。生产能力作为 B6 后的独立计划，不纳入默认“统一入口完成”。

B4 可拆为 B4a 策略及平台一致恢复、B4b 角色换月与绑定订阅同代更新、B4c 多腿组合切换。固定合约受控执行只依赖所需恢复和对账能力，不能被无关多腿开发阻塞；使用角色或组合的策略分别依赖对应子项。

回测组装试点建议 scheduled_targets、triangle_spread、option_delta、main_ema，依次覆盖独立时钟、组合、研究和动态角色目标。在线可先验证单日固定角色范围内的 main_ema 数据与策略，使用显式受限的静态目标映射；在 live_role_roll 完成前，完整主力动态换月方案仍应拒绝启动。该受限试点不得标记 main_ema 全部 live 能力完成。

受限试点限定一个交易日，使用此前交易日已可见角色并固定真实主力映射，记录交易日、合约和角色版本。角色变化或交易日切换即停止或拒绝继续执行，不沿用旧映射。该范围进入配置、能力声明与 summary；验收包含跨交易日、角色变化和完整动态路由请求仍被拒绝的负向用例。受限执行配置与正常完整策略能力声明分开，不能修改默认声明来冒充完整支持。

每个工作包估时应在 A0 建立实际环境和基线后给出。当前缺少真实数据及完整原生环境的统一运行证据，不预设所有工作包可在固定天数内完成。

## 15 业务不变性和验收矩阵

### 15.1 逐策略比较项

| 策略 | 迁移必须比较的行为 |
|---|---|
| main_ema | 角色来源日、因果因子、EMA 初始化与相等边界、目标变化、真实主力路由 |
| cross_section | 完整截面、lookback 与调仓根数、代码排序平局、强多弱空、其余归零、不足一手仍一手 |
| black_sector | 双层平滑次序、默认 secondary 信号和 main 执行、相等保持、只交易执行品种、延迟 Bar 计数 |
| role_cross | 仅 main/secondary/far、首帧预热、零轴边界、三腿实际到达、无穿越保持 |
| calendar_spread | 原价差、前窗口统计、合约不同、开平阈值、旧仓及在途归零、换约清窗、缺旧腿失败 |
| triangle_spread | 初帧基准、复权和对数公式、权重、前窗口 Z、主对冲金额、向下取整、不足一手报错 |
| scheduled_targets | 独立时钟、同时间戳先处理 Bar、REPLACE 清未列目标、错过不追单、MISSED/NOT_REACHED |
| option_vega | 月份与 Call 候选、现金 Gamma/Vega 配仓、完整目标替换、按实际成交仓位对冲、保护性对冲、日常退出与终点政策 |
| option_delta | close 定价、不要求盘口、先定期限且不跨月补腿、核心与回退区间、Delta/OI/volume/代码排序、零成交标记、仅研究 |

比较证据保存有效配置、输入清单及基线哈希、标准化时间和事件顺序、角色映射及因子、决策与目标序列、结束状态。同一模拟环境再比较 orders/fills/positions；不同在线成交结果不强求与历史模拟成交价相同。

时间、身份、整数手数、排序、候选集合、目标符号、角色映射和 revision 按语义精确比较。研究浮点因子及指标预先规定数值容差，例如初始以相对 1e-10、绝对 1e-12 为建议，并按现有数学实现评审；如果越过阈值导致决策变化，仍判失败。忽略 run_id、报告生成时间和耗时等非业务字段。

### 15.2 公共输入最低测试集合

| 类别 | 必须覆盖的场景 |
|---|---|
| 路径 | 显式覆盖、新旧环境变量、根目录派生、父子目录、相对路径、冲突默认文件 |
| 文件索引 | 大小写匹配、重复身份、空目录、必需缺失、可选缺失、旧持仓合约缺失 |
| 元数据 | 不同交易所、tick 与精度、乘数、期权类型及标的、上市前和到期后、冲突条款 |
| 时间 | start/end 同分钟、转换一次、规范化行经 Feed 后标签保持、相同 event 与不同 available 按 ts_init 投递、不提前决策、不支持的乱序轨迹明确拒绝、夜盘交易日、日终和锚点不提前 |
| 行情 | 必需列、重复或乱序分钟、无效 OHLCV、研究坏候选及标记经 Feed 后保留、零成交、OI 不差分 |
| 日历 | 显式与推导来源、缺交易日、远期范围不足、自然日和交易日区别 |
| 角色 | 所需角色子集、前交易日、换月锚点、日终与分钟需求、缺锚点、未来查询拒绝 |
| 计划 | 同时点多行、完整快照、整数手数、重复目标、时区、空计划 |
| 缓存 | 不重复扫描、规格隔离、文件变更失效、DataFrame 不被场景修改污染 |

优先扩展现有 tests 中的策略和输入测试，新增专门公共契约用例；测试以可观察行为和因果性为主，不机械复制实现。不在文档阶段运行或宣称通过这些未来测试。

### 15.3 验证级别

| 级别 | 环境和证据 | 可标记的能力 |
|---|---|---|
| V0 | 导入、语法、help、配置、能力拒绝 | 入口与契约可装配 |
| V1 | 最小合成资料、Bar 和目标计划；真实策略生命周期 | 输入语义、因果性和业务行为 |
| V2 | 真实 Feather、原生行情类型、FileReplayFeed 和模拟 backend | 所测区间真实回放和模拟执行 |
| V3 | 真在线行情、参考服务、记录目标与诊断 | 在线行情到策略目标或研究结果 |
| V4 | 测试柜台账户查询、订单成交撤单回报及恢复 | 该柜台范围的受控执行 |
| V5 | 后续独立生产账户验收 | 该账户及范围生产运行 |

V0/V1 通过不能标记 V2；Recording 不能标记柜台成交；一次单合约验证不能标记所有策略、多腿或跨日恢复。option_delta 的交易执行级别不适用，继续只报告研究链路的验证。

无真实环境时，工作包可交付“实现与 V1 已验证、V2 待运行”，但阶段真实回放验收项保持待办，不虚构完成记录。

## 16 迁移兼容 回滚和开发约束

1. 每个工作包独立交付，先建立基线再替换实现。迁移记录说明改动文件、接口、兼容影响、实际验证范围和未验证项。
2. 旧 local_input.py、contract_input.py 可暂留显式函数转发，保持既有签名和返回对象；内部不再复制公共算法。避免通配导入。
3. 策略不依赖另一个策略的输入文件；常量与业务模型不从文件读取模块反向导入。
4. 旧参数 alias 和脚本兼容有明确废弃条件：内部调用及文档迁移、承诺命令通过、没有未迁移使用者后才删除。
5. 使用版本控制时按工作包回滚；当前没有 Git 的环境按来源快照或发布包恢复。快照保留在项目内，忽略凭据、缓存和运行结果，保留文件清单及哈希。
6. 回滚不能删除或伪造已产生的实盘状态和报告。在线状态 schema 变更必须版本化；不兼容恢复要明确拒绝。
7. 不修改 examples、备份目录或项目外路径来完成此次 demos 公共化；只在明确工作包范围中修改公共包、demos、测试和文档。
8. 不为跑通错误而扩大行情时效、前填旧价、跳过旧仓或取消动态路由限制。错误应定位到数据、能力或业务规则对应模块。

## 17 首个开发任务和交付模板

下一步建议先开发 A0 与 A1，随后开发 A2/A3，并以 scheduled_targets 做第一个输入试点。

首个任务的具体范围：确认项目根入口及规范 import；列九策略数据需求；保存可用测试基线；新增 dataprep 契约、路径解析和诊断；实现按需目录检查与统一环境变量优先级。此任务不迁移 Runner、不新增交易规则，也不承诺实盘能力。

每个工作包交付记录使用以下字段：

| 字段 | 必填内容 |
|---|---|
| 工作包 | A 或 B 编号、版本和依赖 |
| 实现范围 | 新公共接口与已迁移策略 |
| 文件清单 | 新增、修改、保留兼容和待删除文件 |
| 行为变化 | 无业务变化，或单独列出已确认的时间/配置/业务差异 |
| 验证 | 实际命令、环境、输入区间、V 级别、结果与证据路径 |
| 未验证项 | 原生依赖、真实行情、在线服务或柜台条件 |
| 回滚 | 来源快照或版本及恢复方式 |
| 下一依赖 | 后续工作包可开始的条件 |

## 18 参考文件

- [策略交接](../HANDOFF/demos/HANDOFF_DEMOS_2026-10-04.md)
- [DataHub 职责](../data/DATAHUB_MINIMAL_CORE.md)
- [策略框架](../architecture/STRATEGY_ARCHITECTURE.md)
- [CTP 与 Binance 交接](../HANDOFF/live/HANDOFF_CTP_BINANCE_NEXT.md)
- [文件回放实现](../../bomber/framework/market/replay/base.py)
- [标准行情解析](../../bomber/framework/market/replay/parsers/bar/mapped.py)
- [实时分钟聚合](../../bomber/framework/market/stream/aggregation.py)
- [角色因果复权](../../bomber/framework/datahub/role_prices.py)
- [统一 Runner](../../bomber/framework/trader/runner.py)
- [策略模板](../../bomber/framework/trader/template.py)
- [历史模拟运行时](../../bomber/framework/trader/runtime/historical.py)
- [在线生命周期](../../bomber/framework/trader/runtime/direct.py)
- [平台状态保存](../../bomber/framework/trader/persistence.py)

本方案以当前源码和交接作为设计依据，未执行项目测试、真实行情回测或在线连接。后续完成状态以各工作包的实现及验收记录为准。

## 16 第一阶段代码生成状态

2026-10-04 按本方案生成 A0 至 A8 的第一阶段代码并迁移九个 demo 输入调用。用户明确要求当前环境不执行代码，因此验收条件尚未判定通过。实际交付接口、显式兼容变化、源树前提和后续验收顺序见 [第一阶段实现交接](DEMOS_INPUT_STAGE1_IMPLEMENTATION_2026-10-04.md)，API 见 [公共输入开发说明](../../bomber/framework/dataprep/README.md)。本方案中的第二阶段内容仍为后续工作。
