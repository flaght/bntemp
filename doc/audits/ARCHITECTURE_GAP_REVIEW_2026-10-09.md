# 当前 GAP 完成情况与评估快照

更新日期：**2026-10-13，Asia/Shanghai**。保留原文件名，合并此前 10 月 11—12 日评估，补充 10 月 13 日用户明确要求的同账户多策略执行缺口。范围包括当前源码、PF2026101101 后续模型证据、PF2026101201—1207 特征／日历／08 策略交接与验收记录，以及 PF2026101208 后的 04 原生入口与账户锁核对。

本次增量更新文档及证据，不改运行代码、不连接数据库或柜台；已运行的本地测试属于会话清理实施阶段，具体范围另列。实际数据库／柜台结论依据用户提供的远程输出，不代表本次独立重跑。文件名、计划日期或源码存在不能单独作为完成依据。

## 完成情况总览

**11 项整体 GAP 中，已关闭 2 项：GAP01、GAP03；其余 9 项未整体关闭。**

**同一物理账户同时运行多个策略，是正式交易系统必须提供的核心功能，列为 GAP10 高优先级实施项。** 用户于 2026-10-13 明确要求：不能将“一个账户只能运行一个策略”作为正常产品能力边界。现已实现配置化同进程共享原生宿主，保留各策略独立Runner、目标及归属账本；用户提供的当前SimNow运行已确认范围外持仓隔离、横截面14笔成交及EMA独立停止。EMA停止前未完成预热，因此同日恢复和两个活跃来源的真实成交仍待验。原独立demo CLI继续争账户锁；跨进程接入和跨主机保护尚未实现，GAP10整体保持开放。

“未关闭”不代表没有实现。本次最明显的新增完成项是：**04 模型运行及同文件恢复、GAP08 通用特征离线验收与真实 DolphinDB／CSV 链路、08 Replay 工程目标及模型／原生期权成交、公共交易日期接口**。这些完成项各有范围，不能直接替代完整实时运行、多账户成交、通用恢复或通用会话验收。

| 类别 | 项目 | 如何理解 |
| --- | --- | --- |
| 已完成并整体关闭 | GAP01、GAP03 | 达到原定关闭标准；新增模式和场景仍按各自范围验收 |
| 已有实现及局部通过，整体未完成 | GAP04、05、06、07、08、10 | 下表明确已经完成的部分及剩余条件 |
| 关键修复／设计决定仍未完成 | GAP02、09、11 | 期限主链、NT／框架状态职责及部署目标仍需落实 |

## 各 GAP：已完成与未完成

| GAP | 已完成／已通过的部分 | 尚未完成 | 整体状态 |
| --- | --- | --- | --- |
| 01 成交归属 | 通用冻结分配、内部净额、回报去重及持久化；原 EMA 实际成交／最终权威核对；04 PB 开平及新增 MO 期权归属成交证据 | 新多账户真实回报、共享母单撤单等新增能力另行验收，不扩大原关闭范围 | **已关闭** |
| 02 目标截止时间 | 根因、统一时钟和共享贡献问题已分析；部分续执行／Joint 有局部期限检查 | 普通／动态入口、贡献期限、逐笔实际发送、在途到期维护与恢复统一落实 | **未完成：修复仍缺** |
| 03 数据源边界与一致性 | 来源政策、参考时效、失效闭闸及真实数据库→导出→本地一致性；控制线程缓存公共化 | 日常资料日核验继续执行；新历史和特征能力分别归 04／08 | **已关闭** |
| 04 历史预热与实时接续 | 历史 Provider／工厂、逐日因子；真实 IM 指标重启／回补；统一静态会话及CU两源窗口／分钟时间点对照 | 统一3份历史源配置；CU分钟源目前缺失；当前正式历史→实时接续、生产恢复及MongoDB历史Provider待验 | **部分完成，保持开放** |
| 05 多腿协调与续执行 | 04 完整分钟帧、Tick 保留目标续执行、可选完整组／手数上限；真实 Recording、PB 开平及其他双侧成交 | 旧目标活动单取消／确认／重规划；共享母单按归属撤单；通用腿状态、敞口限制、超时／补偿；原四合约覆盖 | **部分完成** |
| 06 在线重启与恢复 | 公共同 TD 恢复、01／04 受限入口、预热暂停旧目标、CTP 同代保存锁；**04 模型同文件恢复通过** | 01 模型恢复、08 恢复、当前版本原生完整恢复；跨日／换约／挂单断点、漏成交／未知订单及双端恢复 | **部分完成** |
| 07 执行语义与风控 | 渠道／健康／范围门控、strict／observe／off、fills／model；原生资金与账户锁；08 特定 MO 期权原生开仓／归属／状态读回 | 逐通道能力声明、TIF／套保、费用／保证金／组合风险；实时非映射、非零期货对冲及最新版本完整原生回归 | **部分完成** |
| 08 自定义特征／DataHub | 配置生成不可变 Payload、Schema／版本、文件／DolphinDB／MongoDB Provider、EXACT／WINDOW／ASOF、有限等帧；252 项＋D0 离线、真实 DDB／CSV 链路；08 工程消费链 | 实时非映射同分钟／期限／恢复验收；真实现货／期现消费；真实 MongoDB、Parquet 往返、历史可见性及量额语义 | **首期公共能力已验，整体未关闭** |
| 09 状态权威职责 | 唯一策略账本来源、执行基准／物理观测分离、来源标记和政策恢复校验已实现 | 正式统一 NT Cache／Portfolio／ExecutionEngine 与框架状态职责，验证 CTP／BN 读取和镜像一致性 | **部分职责已定，整体未完成** |
| 10 CTP＋BN／多账户／同账户多策略执行 | Joint／fanout基础；配置化同进程共享原生宿主、逐策略端口与生命周期、显式持仓范围；当前横截面14笔SimNow成交及最终账户核对；共享恢复/拆分/撤单离线覆盖 | 同日远端恢复、两个活跃来源的实际成交、共享母单部分成交/撤单；CTP＋BN联合成交/恢复、真实第三方多账户；跨进程协议、跨主机保护、BN写前/漏成交重建仍缺 | **部分完成，整体未关闭** |
| 11 Windows／Linux | Linux 原生 CTP 与账户／策略文件锁路径已有 | 正式决定首期平台；若要求 Windows，补 SDK／构建／锁／生命周期兼容 | **未完成：部署口径未统一** |

依据：[整体清单](/Users/kerry/work/bomber1/pro/doc/audits/ARCHITECTURE_GAPS_2026-10-09.md)、[目标架构](/Users/kerry/work/bomber1/pro/doc/architecture/ARCHITECTURE_FIXED.md)、[实现台账](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md)。清单部分旧段落仍保留实施前状态，以本文及最新专项证据判断当前完成范围。

## 已经完成的具体能力

### 1. 公共 Live、独立策略账本与受限恢复

行情与执行渠道独立装配，来源和失败原因可审计；参考查询与缓存刷新在控制线程，行情回调不查询数据库。策略账本可以选择 fills 或 model，物理账户政策可以选择 strict／observe／off。多账户分发及进程内共享连接已有公共实现，跟随回报不会累计进主策略仓位。

已登记的模型验收：

| 场景 | 结果 | 已验证内容 |
| --- | --- | --- |
| 01 真实 CTP MD 驱动模型 | **通过**，model-1791673733149458028 | 9 根 Bar、模型 SELL 1，策略与执行仓位 RB=-1 |
| 04 真实 CTP MD 驱动模型 | **通过**，cross-section-model-1791706100508175757 | 9 同步帧、6 调仓、12 模型成交；最终 AU=0、AG=+1、SC=-1 |
| 04 同文件模型重启恢复 | **通过**，cross-section-model-1791707067358178583 | 原目标版本 6→7；第 4 帧预热后才执行新目标；6 模型成交；最终 AU=0、AG=-1、SC=+1 写回原文件 |
| 01 模型恢复／01 与 04 同时运行／真实多账户回报 | **未验收** | 单独运行通过不能证明并行或第三方账户执行 |

上述回报是 reference-price 模型成交，物理账户未关联，不作为柜台成交。04 后续通过已经替代旧“15:18 无行情失败”为当前运行结论，失败记录仅作历史保留；MD 停机报告修正版是否在成功运行机加载仍无完整核验结果。

依据：[模型最新交接](/Users/kerry/work/bomber1/pro/doc/HANDOFF/live/HANDOFF_MODEL_REPLAY_2026-10-11.md)、[结构化模型证据](/Users/kerry/work/bomber1/pro/doc/evidence/live/MODEL_REPLAY_20261011.json)、[执行服务交接](/Users/kerry/work/bomber1/pro/doc/HANDOFF/live/HANDOFF_STRATEGY_EXECUTION_2026-10-11.md)。

### 2. GAP08 通用特征：公共首期已实现并完成离线验收

当前扩展路径为：

`业务 JSON → DatasetSpec／不可变 Payload → 通用 Provider → 版本缓存／DataHub → 策略需求与对齐帧`。

普通已计算特征通过 Schema 与来源映射接入，不再为每个指数或品种新增专用 Provider／Feed／Channel。支持多数据集、多频率、显式业务键、字段重命名、源身份与 Schema 指纹；EXACT、WINDOW、ASOF、可见时间／接收时间、修订、有限等待及超时跳过已有实现。特征查询是需求声明加轮询，不是原生推送订阅。

PF1203 REV2 已登记 **9 组、252 项测试及 D0 全部通过**：features 35、definitions 10、index 17、legacy 14、entry 9、feature-query-live 9、option-vega 33、option-vega-live 12、live-common 113。早期 market/frames 与 08 CLI 混版已取得修正后通过证据，不能继续列为当前未解决阻塞。

随后 PF1204 REV3 受影响远程组通过：feature-query-live 9、option-vega 34、option-vega-live 14、option-calendar 18、live-common 120，合计执行 195 项，10 个部署摘要全部匹配。两批属于不同版本及覆盖，不相加为唯一测试总数，也不自动覆盖 PF1205—1207 的全部新代码；后续局部测试与实际工程运行分别记录。

依据：[声明式特征交接](/Users/kerry/work/bomber1/pro/doc/HANDOFF/gaps/HANDOFF_GAP08_DECLARATIVE_2026-10-12.md)、[252 项证据](/Users/kerry/work/bomber1/pro/doc/evidence/live/GAP08_DECLARATIVE_OFFLINE_PASS_20261012.json)、[REV3 远程证据](/Users/kerry/work/bomber1/pro/doc/evidence/live/OPTION_RECORDING_REV3_REMOTE_20261012.json)、[Schema](/Users/kerry/work/bomber1/pro/bomber/framework/datahub/features/schema.py)、[来源配置装配](/Users/kerry/work/bomber1/pro/bomber/framework/dataprep/features/definition.py)。

### 3. 真实 DolphinDB 查询、标准化与 CSV 往返

真实 `dfs://min_bar/index_1min` 元信息与样例已核验；DATE＋TIME、上海时区、分钟结束标签、标准事件及来源身份转换已通过。报告 feature-readonly-1791777601420035517 返回 3 条，标准 CSV 导出／逐记录读回通过；原表最早记录截图解释了该区间为何只返回 11:25—11:27，没有时间过滤异常证据。

后续真实读取 2026-10-12 的 14:18—14:24 共 7 条，时间标签与 CTP 请求边界一致。来源持续更新得到用户确认，但查询在 15:00 后完成，不能证明早先各帧截止前已经可见。

updateTime 已确认是最后更新时间，当前保持 observed；下载历史数据不能回填成历史当时已可用。volume／turnover 的累计规则与单位仍未确认，不自动差分；08 当前消费指数 close，这一通过不代表所有量额字段已满足其他策略语义。

依据：[CSV 往返证据](/Users/kerry/work/bomber1/pro/doc/evidence/live/GAP08_DOLPHINDB_CSV_PASS_20261012.json)、[覆盖核验](/Users/kerry/work/bomber1/pro/doc/evidence/live/GAP08_DOLPHINDB_COVERAGE_20261012.json)、[原表配置](/Users/kerry/work/bomber1/pro/bomber/framework/dataprep/features/configs/features.json)。

### 4. 08 期权工程链：目标、模型及原生柜台成交已取得证据

08 显式 Replay 工程时钟把已完成 CTP Bar 映射到估值分钟，指数查询对应映射分钟。原始数据与接收时间保留；这是工程联通验证，不是实时非映射策略或同步历史研究验证。

| 验收项 | 已完成结果 | 证据边界 |
| --- | --- | --- |
| 工程行情与选约评价 | 10 帧消费、同分钟指数送达、5 秒期限内接收；初次 ZERO_ALLOCATION | 该次没有非零目标，不以运行 passed 代替目标链通过 |
| 工程 Recording 非零目标 | Delta 档 0.25，MO2610-C-7300=-1；Recording 实际收到匹配请求，10 帧、指数跳过 0 | 0 报单／成交符合 Recording；不能称实时验收 |
| 工程 Model 成交与保存 | 1 次模型成交，策略／执行仓 -1；JsonStateStore 读回 generation 14、校验通过 | 模型未连接 TD；文件读回不是重启恢复 |
| 工程原生 SimNow 成交与保存 | **1 单 1 成交**：MO2610-C-7300 SELL OPEN 1，限价 37.2、成交 37.4；策略／执行／柜台净仓 -1，活动单 0，清理无错；generation 15 读回校验通过 | 特定期权原生链已通过；没有非零期货对冲、实时非映射或重启恢复证据 |

原生状态文件为远端 `demos/08_option_vega/results/replay-ctp-20261012-114149.json`；原会话结束保留空 1 手，不代表当前柜台仍是该状态。不能将模型文件作原生接管，也不能将工程映射会话直接迁移为另一交易日实时恢复。

依据：[08 最新交接](/Users/kerry/work/bomber1/pro/doc/HANDOFF/live/HANDOFF_OPTION_CALENDAR_RECORDING_2026-10-12.md)、[Recording 目标证据](/Users/kerry/work/bomber1/pro/doc/evidence/live/OPTION_REPLAY_TEST_CLOCK_RECORDING_TARGET_20261012.json)、[Model 成交与读回](/Users/kerry/work/bomber1/pro/doc/evidence/live/OPTION_REPLAY_TEST_CLOCK_MODEL_FILL_20261012.json)、[原生成交与读回](/Users/kerry/work/bomber1/pro/doc/evidence/live/OPTION_REPLAY_CTP_NATIVE_FILL_20261012.json)。

### 5. 公共交易日期接口与健康／就绪修复

[calendars/trading.py](/Users/kerry/work/bomber1/pro/bomber/framework/calendars/trading.py)已提供 CalendarProvider／Snapshot、NativeCalendarProvider、ExportedCalendarProvider，08 Live 读取编译日历，回测消费带 manifest／SHA 的日期导出。当前公开维护 china.cffex／china.sse、1991—2026，未知覆盖拒绝；不是所有交易所全年日历已经验证。

公共就绪等待已按控制线程刷新参考及特征后再检查，避免启动等待期间缓存过期；报告保留诊断。CTP 健康增加经校验原始深度观测，派生事件继续去重，无成交不制造 Bar；静默、回退、溢出及派发错误仍拦截。相关受影响远程回归随 PF1204 REV3 通过。

**静态交易会话核心已实施，完整专项未关闭。** `calendars.SessionCalendar`统一普通时段、跨午夜、交易日归属及分钟窗口；旧Runtime名称保留兼容，ImDayWindow委托同一服务。DolphinDB与Feather Provider实际接入，CU的5段规则、10月12/13日10个窗口、600个最近分钟时间点及930个区间分钟时间点真实对照通过。三份旧demo日历JSON已本地移除，CU的7条停夜盘例外已迁移CSV/Feather；例外表尚未远程导入。

清理后本地`trading-hours`43项通过，清理版本`im-runtime`远程复验结果尚未提供。实际第二套TD/MD只读为20261012、净仓0/活动订单0；CU Replay Recording只展示3/5根实时Bar预热，没有最终summary，且`history_enabled=False`。这些不是数据库历史预热验收。三个`history_*.json`的统一配置、CU分钟资料、正式历史接续、真实期权系列会话和历史规则修订as-of仍待完成。详见[专项台账](/Users/kerry/work/bomber1/pro/doc/data/TRADING_SESSION_GENERALIZATION_2026-10-09.md)与[结构化证据](/Users/kerry/work/bomber1/pro/doc/evidence/live/TRADING_SESSIONS_CU_PASS_20261013.json)，不新增GAP编号或扩大既有关闭范围。

## 已实现，但还需要验收的部分

| 能力 | 当前已有实现／局部证据 | 还需取得的验收 |
| --- | --- | --- |
| 08 原始时间实时特征消费 | 实际 CTP 深度和指数查询已接通；此前实时 Recording 有 SKIPPED_INDEX／STALE 失败记录，修正已交付 | 同交易日非映射下非零目标请求、实际接收期限、超时跳过及恢复就绪 |
| 01／04／08 各类恢复 | 04 模型已通过；01／04 原生同 TD 入口及 08 保存／读回已有 | 01 模型恢复、08 真实恢复、当前原生完整恢复；工程映射路径明确不接受 resume |
| 一策略多账户分发 | fanout、账户倍率、独立账本、pending 保存／闭闸已有，离线覆盖 | 真实多账户异步回报、部分成交／失败隔离／结果不明确对账 |
| 同账户多策略同进程宿主 | 正式配置入口、唯一TD、逐策略端口/归属、范围外持仓基线及原子检查点已实现；横截面实际成交/独立停止已验 | 两活跃策略真实成交与共享母单、远端同日恢复待验；独立策略进程协议与跨主机保护尚未实现 |
| MongoDB 特征、Parquet／现货 | 通用 Provider 和文件格式接口、配置样例及离线夹具已有 | 真实 MongoDB、真实 Parquet 往返、现货／期现策略消费；示例表名不是实际源表存在证据 |
| CTP＋BN 联合交易 | Joint、独立渠道、同机租约、整体 HALTED、显式剩余量规划及离线已过 | 双端当前实际成交、最终仓位、失败恢复，不能以单 CTP 多品种或 fanout 代替 |

10 月 13 日实时 Recording 在专项中是下一步计划，**截至本次更新没有通过结果**。不因日历日期或预写命令标记完成。PF1206 价格审计与 PF1207 冻结组件包装已有局部测试／实际运行证据，但未展示最新版本完整远程离线结果；不能将旧 REV3 195 项自动写成最新全部通过。

## 尚未完成的核心实现与决定

### 高优先级：同一账户多策略的正式执行入口（GAP10，关联 GAP01／02／05／06／07／09）

**功能要求。** 多个策略应能同时使用同一个物理账户交易，各自保存策略 ID、信号窗口、目标版本、订单关联、成交归属及已成交仓位。物理账户仓位是账户事实；策略仓位是各自归属账本。一个策略的目标更新、暂停、停止或恢复，不应覆盖另一个策略的目标或仓位。该功能与“一策略分发到多个账户”分别实现、分别验收，不能由后者替代。

**当前限制与证据。** 原01／04独立CLI仍各自获取同一账户的`bomber-ctp`锁，off不会解除该锁。新增`scripts.integration.shared_account.run`在一个进程内装配多个原Runner，唯一账户服务持有TD和写入锁，并非同时运行两份独立CLI。用户运行`shared-simnow-1791900354405023844`已通过TD/参考资料/显式合约范围检查，横截面14个订单各成交1手，最终RB多1、HC空1，五个范围外持仓不变，活动订单0。EMA仅预热4/5根Bar就按300秒配置停止，未产目标。本次证明共享宿主与单来源原生成交，未证明双来源真实冻结分配；跨进程协议仍缺。详见[逐笔核对](../evidence/live/GAP10_SHARED_ACCOUNT_SIMNOW_20261013_REVIEW.md)及[专项交接](../HANDOFF/gaps/HANDOFF_GAP10_SHARED_ACCOUNT_2026-10-13.md)。

代码依据：[账户锁与生命周期](/Users/kerry/work/bomber1/pro/bomber/framework/trader/runtime/live/channels/ctp.py:250)、[04 原生入口装配](/Users/kerry/work/bomber1/pro/demos/04_cross_section/run_live.py)、[01 原生入口装配](/Users/kerry/work/bomber1/pro/demos/01_main_ema/run_live.py)、[进程内账户服务](/Users/kerry/work/bomber1/pro/bomber/framework/trader/execution/account.py)、[执行来源及账户关联政策](/Users/kerry/work/bomber1/pro/bomber/framework/trader/execution/policy.py)。

**实现方向。** 账户级执行服务统一持有 TD 连接、账户锁、订单编号、资金／活动订单查询和账户检查点；多个策略通过明确路由接入服务，保留各自账本与策略检查点。账户锁用于确保同一账户服务只有一个有效写入者，服务可承载多个策略；不能仅删除锁或按 strategy_id 分别加锁后放任多个 TD 进程独立报单。目标汇总／净额、账户风险预算、母子订单关联和成交分配由账户层统一协调，策略继续负责自己的信号和仓位意图。

可配置的同进程多策略宿主与共享原生入口已提供。针对分别启动各demo的使用方式，还需独立策略进程接入本机账户服务的协议／客户端。进程内租约不能代替跨进程通信，不能以本机`/tmp`文件锁宣称跨主机保护已覆盖。

**待办与关闭标准。** 此专项达到下列标准后，才能登记同账户多策略功能完成；不自动关闭 GAP10 的 CTP＋BN 联合和第三方多账户剩余范围。

- [x] 同进程正式配置／启动入口允许至少两个不同策略ID接入一个SimNow账户，统一管理TD与写入锁；当前真实运行已确认宿主装配。
- [x] 独立目标、归属、信号版本及检查点结构已实施且离线通过；范围外物理持仓不直接分配给策略。双来源实际成交另见未完成项。
- [ ] 订单与成交回报通过稳定身份和冻结归属分配正确策略，覆盖同合约、同向／反向贡献、部分成交、重复／乱序回报。若采用内部净额／转移政策，明确来源及记账规则，不把内部处理冒充柜台真实成交。
- [ ] 一策略改目标、撤单、暂停、退出或释放租约时，保留其他策略的目标、仓位及有效连接；共享母单取消与剩余目标重规划按归属处理，解决 GAP05 已列出的撤单缺口。
- [ ] 同时执行策略级和账户级资金／数量／名义金额／敞口限制，传播并执行各策略贡献期限；局部策略异常可按政策隔离，账户身份、订单或回报不明时统一闭闸并对账。
- [ ] 账户服务与策略进程的重启／断线／未决发送可恢复目标贡献、订单关联及成交去重；单策略恢复不会重置其他策略，跨进程请求具有版本、幂等和连接身份校验。
- [ ] 取得两个策略并行向同一 SimNow 账户真实报单／成交、逐策略归属及最终账户汇总一致的报告，并覆盖停止一个策略后另一个继续、拒单／部分成交和重启恢复。仅两份模型结果、单策略运行或进程内无网络测试不能替代该验收。

### 其他核心缺口

1. **GAP02 统一执行期限。** 当前普通／动态 Runner 仍调用 apply(intent) 而未传处理时间；OrderIntent 未建立独立期限传播。账户贡献仍未保存各源 deadline，顺序分发、写前保存及真正发单仍需复查。新增“特征帧等待超时跳过”约束的是数据决策帧，不替代订单期限生命周期。依据：[Runner](/Users/kerry/work/bomber1/pro/bomber/framework/trader/runner.py:290)、[期限分析](/Users/kerry/work/bomber1/pro/doc/HANDOFF/gaps/HANDOFF_GAP02_2026-10-09.md)。
2. **GAP05 旧挂单与共享母单处置。** 已观察到目标为 0 而旧挂单仍在途；取消、确认和重规划未形成通用闭环。AccountExecutionService 合并贡献后保留触发 strategy_id，其撤单委托下层；CTP 仍仅按 order.strategy_id 匹配，未按冻结 allocation_legs 的所有者识别。多策略共享母单撤单可能漏掉其他触发者的归属单或影响其他所有者；这是源码推演，未登记新的柜台事故。依据：[账户服务](/Users/kerry/work/bomber1/pro/bomber/framework/trader/execution/account.py)、[CTP 撤单](/Users/kerry/work/bomber1/pro/bomber/framework/trader/execution/ctp/native_driver.py:410)。
3. **GAP04／06 通用数据与交易恢复。** IM 指标进程重启通过仍是 239／240 根、显式 allow；13:00 缺口和主力／因子偏差按用户约定暂缓。01 IM 正式历史→实时尚无新增通过记录；08 新期权成交不自动开放或验收 01 IM 报单。跨日、换约、挂单和漏成交恢复仍缺，MongoDB 历史 Provider 仍占位。依据：[历史重启](/Users/kerry/work/bomber1/pro/doc/HANDOFF/history/HANDOFF_HISTORY_RESTART_2026-10-09.md)、[IM 接续交接](/Users/kerry/work/bomber1/pro/doc/HANDOFF/history/HANDOFF_DOLPHINDB_INTRADAY_MONDAY_2026-10-12.md)。
4. **GAP09 状态职责。** 策略独立账本与物理观测政策已明确；总体设计的 NT 主导执行和当前 CTP 框架状态主链仍需统一。BN 内部 NT 状态与框架消费状态的读取、镜像和未知外部回报一致性仍待决定／验证。
5. **GAP11 平台决定及独立会话专项。** 原生CTP仍Linux，账户与策略文件锁仍依赖fcntl；Windows目标未统一。静态会话／分钟核心与CU真实对照已经完成，真实期权资料、日期例外部署及历史规则版本仍按独立专项推进。

## 后续推进与文档使用

- **继续验证同账户多策略同日恢复和双来源真实成交。** 同进程正式入口已实现，恢复验收配置沿用原状态文件，仅延长EMA停止时间使其完成预热；共享部分成交／撤单、跨进程协议与跨主机保护仍分别跟踪，不以多开户、删除账户锁或模型结果替代。
- 先完成 08 实时非映射目标链及接收／期限验证，再分别验非零期货对冲和恢复；保留所有历史状态、失败报告及工程映射证据。
- 执行期限、旧挂单／共享母单和通用多腿协调仍是核心实现待办；按最终变更选择受影响回归。
- 01 模型恢复、两策略并行、真实多账户、CTP＋BN 双端联合按各自身份与状态独立验收。
- 特征离线 252 项、REV3 195 项、真实 DDB 查询／CSV、04 模型恢复、08 已过工程链不因本次文档更新重复；新代码和未覆盖场景单列验证。
- 本文与专项台账、共享GAP清单同步更新当前范围；旧摘要保留历史版本。旧“GAP08只有门面”“04模型仍无行情”“模型恢复全部待验”“日历接口尚不存在”“会话Provider尚未实施”不再适用于相应已完成部分。
