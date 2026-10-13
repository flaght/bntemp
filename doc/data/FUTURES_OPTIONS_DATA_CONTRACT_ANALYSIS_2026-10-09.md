# 期货／期权数据适配：需求与格式分析

日期：2026-10-09。状态：数据契约草案，尚未冻结或实施。依据当前源码、[会话通用化专项](TRADING_SESSION_GENERALIZATION_2026-10-09.md)、[GAP 快照](../audits/ARCHITECTURE_GAP_REVIEW_2026-10-09.md)及[真实 IM 数据交接](../HANDOFF/history/HANDOFF_HISTORY_IM_EVIDENCE_2026-10-09.md)。本次没有连接数据库／柜台，没有改变原 GAP 状态；下列新增字段不表示现有 Provider 已能消费。

## 1. 结论与实施边界

现有期货／期权基础条款、参考源和历史分钟 Provider 可以复用。当前优先补齐：明确的合约身份与期权关系、权威交易日历、实际会话区间、行情的时间和成交量语义、覆盖与版本证据。

按两个范围推进，避免将会话通用化直接变成全部交易业务改造：

| 范围 | 必需数据 | 完成后的能力 |
| --- | --- | --- |
| A：会话通用化 | 交易日历、产品／系列／合约绑定、已展开区间、覆盖与例外完整性、版本可见性；基础条款提供合约生命周期 | 历史、Live、continuous、恢复预检使用同一窗口和交易分钟规则 |
| B：期货／期权数据适配 | A 加基础条款、真实合约行情、期权标的／定价／对冲绑定；策略需要时加角色、因子和定价输入 | 标准数据可供历史预热、实时接续、期权链研究与执行装配读取 |

费用、保证金、涨跌停、行权／结算属于进一步的执行与账户能力。应先声明支持范围，数据齐全也不自动代表柜台交易或到期处理已验收。

推荐首期客户端读取**已展开会话区间**。若上游没有区间，应通过受控资料生成任务使用模板、日历、绑定、例外展开；不能把模板字段藏进每个产品的代码分支。原始资料与展开结果均保留版本证据。

## 2. 当前可复用部分与具体缺口

| 当前组件／资料 | 已有 | 缺口或需核对的语义 |
| --- | --- | --- |
| `FutureBasic` | symbol、exchange、currency、product、上市日、最后交易日；可选乘数和价位 | 执行场景不能省乘数和价位；交易数量规则、资产类别、实际生命周期时刻需补充 |
| `OptionBasic` | 合约、交易所、币种、underlying、CALL／PUT、行权价、乘数、上市／末交易／到期日；可选价位及行权风格 | underlying 当前是文本，缺标的类型和完整身份；缺期权产品／系列标识、会话绑定、定价与对冲关系 |
| `ReferenceDataSource` | 基础条款、角色、因子；文件／DolphinDB 读取及共同标准化 | 目前未注册通用会话数据集；数据库范围查询不等于历史 as-of 查询 |
| `ReferenceRecord`／DataHub | event_ns、available_ns、source_ns、来源交易日及查询门面 | 当前 Record 没有完整有效期／修订字段；现有内存 Provider 拒绝同键重复 event_ns，不能直接承载完整会话修订历史 |
| `HistoryBar`／历史 Provider | 真实合约 ID、UTC 纳秒、原始／调整收盘价、可选 OHLCV 与累计因子 | 未承载 trading_day、OI、turnover、行情发布时间／修订；当前价格校验要求正数，不能据此宣称支持任意价格域 |
| `BarReadSpec`／回放 | OHLCV、可扩展研究字段、来源标签和时区配置 | 仅接受增量 volume；回放完成时间与 HistoryBar 标签相差 1ns，必须显式桥接 |
| DolphinDB 行情流 | 固定 Tick／Bar Schema，Bar 可传 OI、turnover 等自定义字段 | 目前 Schema 必须精确等于既定列集合；流式 Bar 直接用 date+minTime，未显式声明起点／终点标签 |
| 会话与历史窗口 | CU、IM 现有实现与 IM 探针 | 按品种选类、重复时段算法；尚无统一版本化的区间 Provider |
| 公共期权加载／合约工厂 | 当前股指期权示例可用 | `load_options_basic` 固定股指格式／CFFEX；部分工厂固定 CFFEX、INDEX、CNY 和末交易时刻；不能只换数据便支持商品期权 |

默认参考库配置为 `dfs://bomber_daily`，表为 `fut_basic`、`opt_basic`、`fut_contract`、`fut_adjustment_factors`；这是配置默认值，需核对部署。已登记的真实历史读取为 `dfs://min_bar/cffex_1min`，`date + minTime`、Asia/Shanghai、分钟起点标签；证据只覆盖登记的固定合约／日期样本，不代表其他表相同。

## 3. 所有数据的公共约定

### 3.1 身份、数值与存储

- 保留 `raw_symbol`；新增显式映射后的 `instrument_id`，例如 `IM2611.CFFEX`。原始大小写、后缀、郑商所月份等身份差异通过来源映射解决，不能全局截断或按代码猜完整身份。
- `exchange` 使用已登记的标准交易所值；源 `exchangeCD` 的别名通过映射处理。交易所、asset_type、product／series、具体合约分别存储。
- 日期采用 DATE／`YYYY-MM-DD`，完整时刻内部采用 UTC 的 INT64 纳秒。保留 IANA 源时区、源时间单位和转换方式，不能用浮点秒承载纳秒。
- 价格、乘数、因子等内部用 Decimal；JSONL 示例／导出使用十进制字符串。源 DOUBLE 需说明精度和转换规则；成交量单位不能默认等于金额或标的数量。
- NULL 表示未知／不适用，0 表示真实零值，二者不得互换。执行价位、乘数和数量规则必须有效；行情合法价格域按资产／市场声明，不能普遍套用所有价格必须大于零。
- 数据库不必重命名源表；Provider 负责映射。离线现有 Feather 可继续使用；新数据包建议 `manifest.json` 加类型明确的 Feather／Parquet 或 JSONL。格式选择不改变业务契约，也不表示现有读取器已支持新格式。

### 3.2 版本与可见性

参考规则使用公共版本外壳，避免每张表重新解释时间：

| 字段 | 标准类型 | 语义 |
| --- | --- | --- |
| dataset、schema_version、record_key | STRING | 数据集、格式版本、稳定业务键 |
| effective_from_ns、effective_to_ns | INT64，后者可空 | 业务有效期 `[from,to)`；空终点表示未声明结束 |
| available_ns | INT64 | 真实发布可用时刻；若只有观测，按下述模式记录 |
| visibility_mode | ENUM | `EXPLICIT` 或 `OBSERVED` |
| observed_at_ns | INT64 | 本次成功观测时刻，与历史发布时间区分 |
| revision | 非负 INT64 | 同键修订序号；需声明序号在哪个 source 内可比较 |
| source、source_version、content_sha256 | STRING | 来源、上游版本证据或观测版本、内容指纹 |

规则可以提前发布，所以 available_ns 可以早于 effective_from_ns。查询目标业务时刻须处于有效期，所用版本须在决策 as-of 时刻已可见。最新可见有效版本按明示的修订政策选择；同键同版本内容冲突拒绝。

源缺真实发布时间时，`OBSERVED` 的 available_ns 不早于观测／稳定导出完成时刻，不补造旧发布时间。这样的包可验证观测后的行为，不能证明观测前的历史可见性。若没有历史修订档案，应在 manifest 明确覆盖不足。

会话规则允许描述未来区间；不要把 session_start 当成 `ReferenceRecord.event_ns` 后强套现有只读过去事件的查询逻辑。需要单独的“目标业务日／时刻 + 决策 as-of”规则选择接口。

行情额外区分事件时间、完成时间、源发布时间和接收时间；参考规则提前发布不意味着一个完整 OHLC Bar 可以提前于其 bucket_end 可用。

## 4. 基础条款与期权关系

### 4.1 期货条款 `future_basic`

| 标准字段 | 类型／要求 | 现有来源 |
| --- | --- | --- |
| instrument_id、raw_symbol、exchange | STRING，必需 | symbol／Code、exchangeCD，显式身份映射 |
| asset_type、asset_class、product | ENUM／STRING，必需 | FUTURE；资产类别需补；code／contractObject |
| currency | STRING，必需 | currencyCD；没有源字段时由明确市场配置声明 |
| list_date、last_trade_date | DATE，必需 | listDate、lastTradeDate |
| multiplier、price_increment | Decimal，执行必需 | contMultNum、minChgPriceNum |
| quantity_unit、quantity_increment、min_order_quantity | STRING／Decimal，执行必需 | 需补源字段或版本化市场规则，不能把一手准入等同于交易所规则 |
| contract_month | STRING，可选 | 用于期限结构；不能把月份推断当合约身份权威 |
| activation_ns、last_trade_ns | INT64，按用途补充 | 精确上市／最后可交易时刻，不默认所有市场 15:00 |

`last_trade_ns` 与会话区间结合限制合约可交易时间。日期级条款不能独立确定行权截止或到期结算时刻。

### 4.2 期权条款 `option_basic`

除上述共用身份、数量、币种、价位及生命周期字段外：

| 标准字段 | 类型／要求 | 现有来源及补充 |
| --- | --- | --- |
| option_product、option_series_id | STRING，必需 | 需明确提供；产品与系列分别定义，系列应有稳定标的／到期等分组含义 |
| underlying_type、underlying_id | ENUM／STRING，必需 | FUTURE／INDEX／ETF 等；varTicker 只保留原值，需补完整身份和类型 |
| option_kind | ENUM，必需 | contractType 规范为 CALL／PUT |
| strike | Decimal，必需 | strikePrice；精度独立于权利金最小价位 |
| multiplier、price_increment | Decimal，执行必需 | contMultNum、tickNum；兼容 minChgPriceNum／price_increment，别名冲突拒绝 |
| expiry_date、expiry_ns | DATE／INT64 | expDate 可复用；精确时刻按定价／到期处理需要补充 |
| exercise_style | ENUM，定价／行权用途必需 | 原 exerciseStyle 需标准化，不默认所有期权相同 |
| settlement_type | ENUM，结算用途必需 | CASH／PHYSICAL 等；由权威条款提供 |
| exercise_deadline_ns、settlement_rule_id | INT64／STRING，结算用途必需 | 可拆到独立生命周期／结算规则表 |

### 4.3 关系表 `instrument_bindings`

法律标的、定价输入、对冲腿和会话继承关系单独表达：

```text
binding_id, subject_type, subject_id
relation_type                    # UNDERLYING / PRICING_INPUT / HEDGE / SESSION_INHERIT
target_type, target_id
mapping_policy_id                # 固定合约、按月或其他显式规则
effective_from_ns, effective_to_ns, available_ns, revision, source_version
```

一个期权可以用指数做法律标的、用期货做定价输入或对冲。是否如此由绑定资料决定，不由代码前缀或“同月”默认决定。自动换月的关系也需要版本与有效期；映射出来的目标条款和行情必须存在。关系目标缺失、冲突或继承循环明确失败。

## 5. 会话首期交付格式

客户端最小交付为下列四张逻辑表及 manifest；模板和例外可在上游保留。实际库表名待源核对。

### 5.1 `calendar_days`

```text
calendar_id: STRING
exchange: STRING
date: DATE
is_trading_day: BOOL
reason: STRING?                  # 周末／休市等已确认原因
公共版本外壳
```

覆盖内每个自然日期有明确记录，包括非交易日。键为 calendar_id + date + 版本。calendar_id 不能只由交易所代码自动代替，因为同一交易所下具体资料的覆盖或规则适用范围仍须声明。

### 5.2 `session_bindings`

```text
binding_id: STRING
exchange: STRING
asset_type: ENUM                 # FUTURE / OPTION
scope_type: ENUM                 # PRODUCT / SERIES / INSTRUMENT
scope_id: STRING
calendar_id: STRING
session_profile_id: STRING
inherit_from_binding_id: STRING? # 仅显式声明继承时使用
公共版本外壳
```

合约专用绑定优先于系列，再优先于产品；同层级有效绑定冲突失败。该顺序是本草案建议，冻结时必须明确实现。期权不默认继承标的时段；继承要有可追溯目标。

### 5.3 `session_intervals`

```text
binding_id: STRING
schedule_version: STRING
trading_day: DATE
session_id: STRING
session_kind: ENUM               # DAY / NIGHT；首期仅连续交易区间
session_start_ns: INT64          # 实际自然日期时间转换后的 UTC 纳秒
session_end_ns: INT64
source_timezone: STRING
origin_rule_ids: STRING[]        # 模板／绑定／例外依据，若上游有这些对象
公共版本外壳
```

每行一个 `[start,end)` 实际交易区间；夜盘自然日期与所属 trading_day 分开。不得自然日加一计算假期后的交易日。集合竞价、申报时段、行权时段以后分别扩展，不能直接当成连续交易分钟。

模板／例外优先级由上游生成政策声明。完整展开版本先解析替换、取消和继承，客户端一次选择完整 schedule_version，再读取其所有区间；不能把品种级和合约级完整日程直接合并，或逐区间拼接不同修订。

### 5.4 `session_day_status`

```text
binding_id, trading_day, schedule_version
status                          # COMPLETE / CLOSED / NOT_LISTED / EXPIRED / UNKNOWN
interval_count
reason
公共版本外壳
```

每个交付范围内的 binding + trading_day 明确状态。`COMPLETE` 表示全部预期区间已交付，不能自动证明有行情；`CLOSED` 表示已知不交易；`UNKNOWN` 必须拒绝窗口生成。停夜盘但保留日盘是 COMPLETE 加替换后的日程。空区间只有明确 CLOSED 等可解释状态才有效。

manifest 至少声明：calendar 日期覆盖、binding 集合、交易日覆盖、实际自然时间覆盖、例外是否完整、历史版本覆盖、源与导出版本、文件类型／hash、空数据含义。跨午夜区间可能落在交易日覆盖之外的自然日期，必须同时检查自然时间覆盖。

上游若采用模板方案，仍需提供 `session_templates` 的 timezone、分段、时钟时间、夜盘锚点／自然日期定位规则，以及 `session_overrides` 的对象范围、业务日期、取消／完整替换内容、原因和版本；最终生成相同四张交付表。

## 6. 行情格式：期货与期权共用

### 6.1 分钟 Bar `market_bars_1m`

```text
instrument_id, trading_day
bucket_start_ns, bucket_end_ns   # 时间桶 [start,end)，首期 60 秒
open, high, low, close           # 原始真实价格，Decimal
volume                          # 本桶增量，单位由 metadata 声明
open_interest                   # 桶末存量，可空；期权链策略可要求必填
turnover                        # 本桶增量成交额，可空，币种／单位必需声明
price_basis                     # RAW；调整价格为另一个有血缘的字段／数据集
available_ns, received_ns        # 源可用／本地接收，未知源发布时间不伪造
quality_status                  # VALID / NO_TRADES / SYNTHETIC / INVALID 等
revision, source, schema_version
```

完整执行回放要求 OHLCV；仅信号研究可按 DataRequirements 要求 close 或 close+OI+volume。无成交分钟、无报价分钟和丢失分钟应区分；一根会话内应检查的分钟不代表一定有成交。前值填充如获明确允许，也须标记 SYNTHETIC 并保留原价格年龄，不能自动用来撮合。

建议新标准以时间桶为权威：

```text
源起点标签 -> bucket_start_ns = 源时间
源结束整点 -> bucket_end_ns = 源时间
bucket_end_ns = bucket_start_ns + 60_000_000_000

适配 HistoryBar.ts_event = bucket_end_ns - 1
适配现有回放 event_ns     = bucket_end_ns
完整 Bar 可消费条件       = bucket_end_ns <= 决策时间
                           且 available_ns <= 决策时间（如果有真实可用时间）
```

每次读取的配置须声明 date 是自然日期还是交易日、minTime 是起点还是终点、时区与单位。若 date 是交易日，夜盘不能直接 date+minTime，必须由完整源时间或已核验会话定位自然日期。`updateTime` 是更新／入库字段，先核实语义才能用于 available_ns；不能拿它替代桶时间。

历史、回放、实时及 checkpoint 的比较必须在同一桶身份／完成边界上进行；新契约修订与 checkpoint 兼容政策一并实施。查询与去重不能用差 1ns 的标签当成两根 Bar。

成交量若是累计值，需要额外声明 `volume_semantics=CUMULATIVE`、计数周期／reset 键、计数单位、首次样本和断线重建政策。普通情况下相邻计数差产生增量；查询从盘中开始时要读前置基线或拒绝首桶差分，不能用首样本累计量当该分钟量。OI 是存量，不套用成交量差分。

当前 IM 原始读取证据未映射累计成交量；只读 OHLC 成功不代表已满足执行 OHLCV。HistoryBar 当前也不能直接携带本节全部新增字段，需通过新 DTO／元信息适配。

### 6.2 Tick／一档行情 `market_ticks`

做实时报价、盘口回放或从 Tick 合成 Bar 时，需另一套事件 Schema：

```text
instrument_id, trading_day, action_date
ts_event_ns, available_ns?, received_ns
source_event_id? / source_sequence?  # 有源序号时保留；不能只按时间戳去重
last_price?, cumulative_volume?, cumulative_turnover?
bid_price_1?, bid_size_1?, ask_price_1?, ask_size_1?
open_interest?, prev_settlement?, upper_limit?, lower_limit?
source, quality_status
```

源 CTP 的 ActionDay／TradingDay／UpdateTime／UpdateMillisec 均保留；缺自然日时用已声明的来源规则／会话定位，不能在历史回放用主机今天代替。交易日不能直接视为夜盘自然日。多个合法 Tick 可以有相同毫秒时间；快照差分得到的成交事件不等于逐笔真实成交，要保留其推导口径。

空买卖盘、无最后成交和源哨兵值规范为明确缺失／质量状态。只需报价的消费场景不强制要求 LastPrice／Volume。首期是否消费 Tick 由实际数据要求决定，不必为仅分钟预热先补全盘口。

## 7. 策略相关的追加资料

| 数据 | 何时必需 | 最小内容与约束 |
| --- | --- | --- |
| `contract_roles` | 主力／近月／换月策略 | product、role、真实 instrument_id、source_trading_day、effective_trading_day／有效期、available_ns、revision；main/second/recent/far 映射为 main/secondary/near/far |
| `adjustment_factors` | 连续信号价格 | 合约、产品、序列／角色、因子适用日、来源日、累计因子、生成算法／price_basis、可见版本；原始价格保留 |
| 标的行情 | 期权定价／链选择 | underlying_id 的行情，或 PRICING_INPUT 绑定对应行情，时间、可见性、陈旧预算明确 |
| 利率／股息等 | 所选模型需要时 | 模型对应的 rate／curve、tenor、计息／年化口径、股息或 carry、有效期、发布时间；固定参数可通过显式策略配置声明 |
| IV／Greeks | 使用外部风险指标时 | 合约、估值时刻、可用时刻、模型／版本、输入行情版本、单位；若内部计算则无需上游提供 |
| 费用／保证金／交易限制 | 模拟真实性、预交易风控 | broker／exchange 范围、投机／套保等条件、开平今昨、金额／比例单位、生效时刻；期权保证金不能普遍以权利金比例替代 |
| 行权／结算 | 持仓穿越到期时 | 截止时刻、结算价、现金／实物及转换关系、数量转换／舍入、通知事件和版本 |

累计因子不能默认适用于所有合约或所有角色。当前 IM 主力／因子身份偏差在交接中已按用户要求暂缓，不作为固定合约数据适配的前置阻塞；这里仅保留正式连续序列接入时的身份契约。固定合约原始行情、会话服务和期权条款可以先推进。

Greeks 单位必须明确，例如每单位标的还是每手、Vega 对波动率 1 还是一个百分点、Theta 每自然日还是每年。模型选择需与标的类型、行权风格和定价输入一致，不能默认将现有股指示例的公式用于全部商品期权。

## 8. 下一轮源核对的交付清单

每张现有表先提供以下信息即可，不要求先按本草案重建库：

1. 数据库路径、表名、Schema（字段、数据库类型、空值规则）；流表另提供实际订阅列。
2. 少量原始样例：日盘首／末分钟、休息前后、夜盘开盘／跨午夜、周末／假期附近；期权至少一个指数标的系列和拟接入的另一种标的系列，CALL／PUT、上市／末交易／到期日期。
3. date、TradingDay、ActionDay、minTime、updateTime／receivedTime 的准确含义、时区、时间单位与 Bar 标签。
4. volume／turnover 是增量还是累计，计数周期、单边／双边及数量／金额单位，盘中首样本与重连政策；OI 的采样口径。
5. 日历和行情覆盖范围、无成交分钟生成政策、数据缺口、例外规则来源与完整性；是否已有已展开会话区间。
6. 期权产品／系列、标的类型／ID、价位／乘数、行权风格，以及定价／对冲／会话关系已有字段。
7. 发布时间、版本／修订档案及历史可见性证据；没有则如实声明 OBSERVED 和覆盖不足。

连接沿用既有环境配置；不将凭据写入交付样例。按已有源字段建立映射表：源字段／类型 → 标准字段／类型 → 转换规则 → 缺失政策 → 使用场景。

## 9. 实施与验收顺序

1. 冻结会话交付方式及时间／量语义，登记源覆盖和身份映射。先用已有 CU／IM 数据与一个明确绑定的期权系列。
2. 实现规则 DTO、覆盖／版本校验、文件及 DolphinDB Provider，再实现公共 SessionCalendarPort。既有 DataHub 门面可复用，完整会话修订选择需扩展。
3. 迁移 CU／IM 旧类、history/windows、Live、continuous、恢复预检；同一输入生成同一交易分钟序列。
4. 标准化 Bar 时间桶及可用时间，统一历史／回放／实时适配；验证分钟成交量后再用于严格执行回放。保持真实价格与复权信号分离。
5. 将股指专用期权加载和合约工厂改为条款／市场配置驱动，接入显式关系表；支持范围仍按已实现模型和市场能力声明。
6. 验证日盘、跨午夜、休息、假期、取消夜盘、合约级替换、生命周期边界、未来修订不可见、覆盖缺失拒绝。历史最近 N 根应跨所需交易日，而非固定 240。
7. 验证历史末桶与实时首桶无重复／漏桶、checkpoint 口径兼容、低流动性期权缺行情可诊断。新同模型产品只增加数据／配置，不改公共产品分支。

当前交付为分析文档；源 Schema、绑定与历史版本完整性尚待核对。没有将本草案登记为已实施或验收通过。
