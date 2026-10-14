# 当前 GAP 完成情况与评估快照

更新日期：**2026-10-14，Asia/Shanghai**。保留原文件名，合并 10 月 12—14 日的源码及最新专项证据，替换此前零散增量与过时结论。范围包括整体 GAP、共享账户 REV19／三策略两模式、08 实时与 DolphinDB 分钟流、03 跨期、交易会话通用化及 08 共享／跨日恢复交付。

原审计快照仅更新本文；其通过结论来自已登记的用户远程输出和专项证据。本次新增 M01 统一来源装配及本机离线验证，详见下方实施补充；未连接数据库或柜台。计划时点、源码存在、completed 或单独状态文件读回不能自动等同完整验收。

## 当前最小核：开发与验收基线（2026-10-14）

本节依据用户本次明确确认，从当前迭代起作为**最小核需求基线 v1：M01—M06**。它回答“当前必须交付什么”，后面的整体 GAP 回答“完整架构还差什么”。最小核完成不要求全部 11 项 GAP 同时关闭；整体 GAP 的已关闭数量也不能代替下面六条验收。

后续用户可以继续补充。新增需求须在本节记录编号、确认日期、语义和验收范围；修改已有条款须写明替代关系与受影响项，不静默改变旧含义。实现状态随证据更新，需求范围不得因临时排错或某个 demo 的限制自行改变。当前基线只写入需求与进度，不表示本次已实施新增代码。

**核心原则：策略算法复用；运行模式、行情源、执行客户端、进程布局、仓位来源和特征来源由公共接口与装配层选择。** 新来源或第三方客户端开发适配器；已有来源切换修改运行配置与绑定，不把 CTP／BN／DolphinDB／LXGBN 分支塞进策略算法。策略的行情字段、频率、业务标的及特征需求仍须由装配满足，不能把任意不同输入强行当作等价数据。

| 编号 | 用户确认的最小核要求 | 当前进度 | 最小完成标准 |
| --- | --- | --- | --- |
| M01 策略与行情解耦 | 同一策略无需修改算法即可用于回测／Live；Live 行情可接原生 CTP、BN-WS、DolphinDB | StrategyTemplate／Runner／标准事件和三类 Feed 已有；CTP 与 DDB 有策略实测，BN-WS 有在线 Recording 记录；01／03／04／08 已接公共行情配置与工厂，文件回测／CTP／DDB 正式入口统一；BN-WS公共能力支持匹配BINANCE标的，当前国内业务输入明确拒绝；服务器原生环境专项21项通过，新增组合实际运行待验 | 同一策略类、参数和输入契约保持；通过装配选择文件／CTP／BN-WS／DDB，以标准事件运行；已有来源切换不修改策略或另造策略版本。每个纳入最小核的来源取得匹配策略输入的运行证据 |
| M02 执行客户端可替换 | 下单可选择 CTP、BN 或自定义第三方客户端，例如 LXGBN；行情源不决定下单端 | 公共执行端口、Planner 与标准回报已有；CTP 原生成交已验，BN 适配／离线链路已有；LXGBN 仅有对接规范，具体适配器未找到 | 执行端由显式配置／工厂／路由选择；报单、撤单、受理、拒单、部分／全部成交及稳定 ID 转为标准回报；策略不调用厂商 SDK。CTP／BN 实际链路和首期第三方适配分别验收，不能以接口存在代替厂商接通 |
| M03 多策略与进程布局 | 多个策略可在同进程运行，也可各自独立进程；同一账户多策略是核心能力 | 同进程双策略真实成交／归属／停止已验；IPC／worker 与三策略两模式已交付、局部离线通过；完整 Linux 三策略联调待验 | 01／03／04 原策略在两种布局中分别启动，订单／回报／仓位按策略隔离；停止、断线或重启一策略不重置其他策略。共享物理账户由唯一执行服务管理，独立进程不等于多个 TD 写入者 |
| M04 策略独立仓位 | 策略管理自己的仓位，可以不接入真实账户仓位 | position 读取归属；strict／observe／off 与 fills／model 已实现，模型及原生归属已有实际证据；新跨进程组合仍待验 | 策略不会被整账户净仓覆盖；off 不查询物理持仓且报告为 None；fills 只由选定真实执行来源推进，model 明确标记模拟来源；恢复保留自己的版本／归属。真实执行端所需资金、可平量、活动订单等校验由适配器负责，不强迫策略使用账户仓位 |
| M05 自定义特征两种运行方式 | 同一特征定义支持回测与 Live | 配置生成 Schema／不可变 Payload、版本／对齐、文件回放与数据库刷新已实现；离线、DDB／CSV 和实际指数策略消费已验；新现货等业务场景待配置／验收 | 同一 Schema、计算版本、字段和绑定契约；回测读标准导出、用模拟可见时钟，Live 读实际源／用真实可见时钟；策略使用相同查询及对齐接口。普通已计算特征新增声明／映射，不新增专用 Provider；迟到与缺失行为显式、可审计 |
| M06 Live 的 DolphinDB 预热 | **需要历史窗口的 Live 版本必须接入 DolphinDB 获取数据进行预热，不能只靠启动后等待实时 Bar** | 公共 HistoryService／DolphinDB Provider 和 EMA 历史钩子已有，真实 IM 指标预热／重启回补已验；多策略公共装配、统一源配置及正式当前历史→实时验收未完成，CU 历史分钟源仍缺 | 窗口、频率、真实合约／多腿、会话、时间标签及价格口径可配置；从 DDB 实际读取所需已完成数据，经公共预热接口初始化指标／窗口，再无重复无缺口衔接当前行情。读取、覆盖、使用数和接续结果有实际报告 |

### M01 统一来源装配实施补充（2026-10-14）

本项已修改运行代码：01／03／04／08的Live与回测共用 `MarketSourceConfig`、`--market-config` / `--market-source`
和公共Feed工厂，保留原策略类、参数及标准分钟输入。01／03／04新增DDB分钟源装配和逐合约时效门禁，
08复用既有分钟帧及指数特征契约；同进程共享宿主和独立worker也按解析配置选择MD，而非固定CTP。
行情选择不改变执行端。BN-WS支持匹配BINANCE标的的公共装配，当前国内合约业务输入提前明确拒绝。

本机专项21项、原08回归81项、公共Live116项、共享账户/CLI68项、IPC8项（含本机3个实际子进程）
及共享持续调度9项离线通过。专项使用行情实体/EMA替身，实际配置解析、Feed装配、Runner、策略目标和共享机制运行；
这些是本机离线证据，未连接数据库或柜台，不是新增组合真实运行或完整原生回测通过。
首轮IPC socket受沙箱限制，获准仅绑定本机Unix socket后单项通过；持续调度依赖原生类型，复用既有SDK替身入口通过。

用户已同步远程服务器，并按 `python -m unittest -v tests.test_market_sources` 返回专项结果：
**21项，1.112秒，OK**。登记为服务器原生环境离线专项通过；行情与参考仍是夹具，不能扩大为真实DDB/CTP连接或完整回测通过。
返回为控制台尾部，尚无远程源码摘要；证据见[M01远程专项结果](../evidence/live/M01_MARKET_SOURCES_NATIVE_REMOTE_20261014.json)。

**M01状态为“正式配置入口可用＋本机及服务器离线通过”；实际来源矩阵验收仍开放。** M02执行厂商接入、M05的08特征回测、
M06历史窗口及当前历史→实时接续保持原待办范围。本补充是运行代码交付，不将此前只读审计与本次实现混为同一验收。

使用与命令见[公共来源配置说明](../../demos/market_sources/README.md)，
专项测试见[来源装配回归](../../tests/test_market_sources.py)，版本摘要及本机证据见
[M01离线记录](../evidence/live/M01_MARKET_SOURCES_OFFLINE_20261014.json)。

### M06 预热的明确语义

1. **历史来源与实时来源独立选择。** 实时可以是 CTP、BN-WS 或 DDB；预热可以从 DDB 取得该策略对应的历史输入。DDB 作为预热来源不强制实时也选 DDB，更不决定执行客户端。
2. **策略不直接连接数据库。** 公共 Provider／历史服务负责读取和标准化，启动协调器负责窗口、缺口、实时缓冲与阶段切换；策略提供复用的预热／指标更新入口。需要新的预热钩子时在公共接口与装配层接入，不复制一份 Live 策略算法。
3. **预热只初始化状态，不重放历史交易。** 历史数据不产生历史报单／成交，不重复提交旧目标，也不把行情初始化当成交易仓位重建。账户／策略归属恢复与指标预热分别处理；完成预热后才按新的完整实时输入正常决策。
4. **衔接须覆盖查询期间到达的行情。** 明确历史截止点，提前订阅／缓冲实时输入，按合约及时间合并去重；未完成 Bar 不进入窗口。多腿及外部特征按各自需求对齐，不用缺腿、复制价格或跨分钟拼接补造完整帧。
5. **缺数据不能伪称完成。** 保留显式 fail／allow 政策与策略自身最小预热条件；默认严格，只有显式配置允许不足时按有效数据继续，报告真实缺口及实际条数。连接失败、身份／字段错误、价格版本冲突不能被“允许缺行情”掩盖。
6. **文件是可选缓存，DDB 能力是最小核要求。** 保留此前已确认的盘前文件优先、缺少／不足时 DDB 补足，以及盘中恢复读 DDB 的规则；同时要能配置并实际验证 DDB 预热路径。仅文件成功、实时重新预热或数据源构造成功，都不能代替 M06 的 DDB 验收。回测继续消费标准文件，避免将 Live 数据库访问带入回测算法。
7. **当前范围是已声明并有数据的输入。** 不要求一次支持全部品种，但必须为当前验收策略提供真实历史表／字段映射和需求窗口。CU 源缺失明确阻塞该场景，不能把 IM 历史或 CU 仅枚举的分钟时间点当成 CU 历史价格。

M06 对应 GAP04，恢复接续部分关联 GAP06；它是本次新增的最小核要求，**当前不能标记完成**。源码依据为 [历史服务](/Users/kerry/work/bomber1/pro/bomber/framework/dataprep/history/service.py)、[EMA 历史装配](/Users/kerry/work/bomber1/pro/demos/01_main_ema/history.py)；既有文件／数据库规则见 [启动与恢复政策](/Users/kerry/work/bomber1/pro/doc/HANDOFF/history/HANDOFF_HISTORY_STARTUP_POLICY_2026-10-09.md)。

### 已确认的政策、需补资料与防止范围漂移

- 仓位政策与是否真实下单是两个选择：off＋fills 仍可发真实订单；model 的模拟回报不等于柜台成交。策略归属、执行账户和信号状态分别保存。
- 首期已确认同进程 netted 允许抵消，独立进程 independent 分别真实发单；进程布局与执行净额政策分开记录，不以“多进程”要求为由删除账户写入锁或归属校验。
- 需要补齐的外部资料主要是 **LXGBN 的具体 SDK／协议**，以及 **预热数据库的真实表、字段／合约映射、时间／价格口径和需求窗口**。第三方规范目前是草案；可复用 [对接说明](/Users/kerry/work/bomber1/pro/doc/integration/DXBM_LXG_INTEGRATION_SPEC.md)，确认本次 LXGBN 是否为该接口后落实实际适配。
- 所有交易所／期权业务、复杂订单、自动补偿、跨主机、全面跨日换约和 Windows 等完整架构扩展继续单独跟踪，不自动成为六条最小核的新增门槛。当前最小核所依赖的基本订单关联、回报去重、期限、停止／恢复和数据可见性仍须满足，不能因缩小范围而绕过。
- 每次实施／验收标明 M01—M06 中对应项、源码版本、运行组合和结果。状态分为“接口／代码已有”“正式入口可用”“离线通过”“实际场景通过”“尚缺”，不以单个 demo 成功推称所有组合完成，不以整体 GAP 未关闭推称最小核毫无进展。

最小核下一步按 **统一来源／执行装配 → 三策略两种布局验收 → DDB 预热及当前行情接续 → BN／第三方实际接入**推进，数据和适配资料核对可并行。M04／M05 已验的能力保持，不为本次需求澄清重复重写。

### 08持续仓位管理补充（2026-10-14）

新增公共 `runtime/live/continuous.py`，复用 `calendars.SessionCalendar` / 会话Provider，支持每日至收盘与常驻休市等待、下一窗口自动恢复。08快捷入口及多08共享入口已接入；同进程共用账户服务，不同进程由唯一server和逐策略worker执行，worker先正常停机保存再关闭TD。JSON仍为现有归属/OMS/成交去重持久化，不新增仓位所有权算法。实际公共账户恢复与新鲜帧跨首日两窗口、下一交易日的离线验证通过；本机没有连接真实柜台，服务器跨日证据仍待返回。本批不扩展01/03/04的滚动历史预热和非CFFEX独立昨仓，M02/M05/M06等剩余项保持开放。详见 [本次交付](../HANDOFF/live/HANDOFF_OPTION_VEGA_CONTINUOUS_POSITION_2026-10-14.md)。

### 当前策略需求核对：需求覆盖与实现达标分别判断

2026-10-14再次按当前源码核对01／03／04／08。**M01—M06作为需求定义基本覆盖当前策略；现有实现尚未全部达标。** 对08的MO／IM、DolphinDB分钟、指数特征和CTP执行，主要运行组件已具备，共享执行与启动跨日恢复已交付，但当前版本真实原仓持续管理仍待验收。本节为静态源码及既有证据核对，没有新增运行或柜台通过结果，不修改六条需求的范围。

| 最小核项 | 当前策略适配结论 | 核对依据与剩余工作 |
| --- | --- | --- |
| M01 策略／行情解耦 | 08主要需求具备；完整来源矩阵仍部分完成 | 08回测和Live共用`OptionVegaStrategy`及`assembly.py`，市场／指数来源由入口装配。标准事件接口可复用；BN不提供MO／IM行情，不能把不匹配业务标的的来源强行用于08，也不能据此判08算法不解耦。四入口已接统一行情配置与工厂；新增适用业务组合仍须实际验收。 |
| M02 执行可替换 | 08的CTP执行具备；全部最小要求未完成 | 公共执行端口存在，但当前共享应用仍直接构建CTP执行；BN实际链路及LXGBN适配没有达到此六条基线的完整验收。不能把“行情改为DDB、下单仍为CTP”扩大为共享入口已可配置任意执行端。 |
| M03 多策略／两种布局 | 接口与代码已具备，正式组合验收未齐 | 工厂已包含01／03／04／08；共享宿主、独立worker、账户服务与IPC已交付。当前三策略Linux完整联调及08共享实际持续运行仍待结果；源文件存在、替身socket测试和原双策略成交不能代替新组合通过。 |
| M04 独立策略仓位 | 核心语义具备，须按明确模式使用 | 策略`position`按strategy_id读取归属，不被整个账户净仓覆盖；off／observe／strict与model已有公共政策。08使用independent时符合只由本策略真实成交推进的预期。跨日CFFEX原仓恢复有离线证据，实际柜台恢复仍待验；并非所有交易所／未知断点均已支持。 |
| M05 同定义特征回测／Live | 公共能力具备；08正式回测尚未完成相同特征链路接入 | Live指数经声明式`index.minute` Provider／查询缓存／DataHub，再由`FeatureQueryBarFeed`投影成Bar。08回测仍直接读取指数Feather Bar，未调用特征文件Provider或按`visible_ns`回放。相同策略价格输入不等于Schema／修订／可见时间一致；须复用已有特征文件与模拟时钟回放，明确历史发布时间或可见性假设，再验迟到／缺失行为。不能直接断言现有回测有未来函数，但不能将它登记为M05完整通过。 |
| M06 DDB历史窗口预热 | 08当前不需要指标历史窗口；01／03／04组合未达标 | 08按新鲜期权／期指／指数分钟估值选约，无EMA或滚动价格窗口，等待同分钟输入不是历史预热。01有历史服务及钩子；共享工厂仍显式拒绝`history_minutes`和`run_forever`，宿主／worker没有调用统一历史准备阶段。03／04仍从实时Bar积累lookback，尚未接入对应DDB窗口；改小lookback或等待实时Bar不能满足M06。 |

**两处直接可定位的接入缺口：**

1. 共享工厂 [strategy_arguments](/Users/kerry/work/bomber1/pro/scripts/integration/shared_account/factories.py:11) 将执行模式固定为SimNow，并在第21行拒绝历史窗口。共享 [账户装配](/Users/kerry/work/bomber1/pro/scripts/integration/shared_account/run.py:136) 直接调用CTP builder。因此公共端口可扩展，当前应用入口仍不是M02／M06完整的可配置装配。补齐历史准备、实时缓冲与去重阶段；不能仅删除参数拒绝而没有预热生命周期。
2. 08 [回测行情准备](/Users/kerry/work/bomber1/pro/demos/08_option_vega/run_backtest.py:168) 将指数与交易腿一起按普通Bar准备；Live的 [指数特征装配](/Users/kerry/work/bomber1/pro/demos/08_option_vega/run_live.py:253) 则走声明式特征链。公共 [特征文件Provider](/Users/kerry/work/bomber1/pro/bomber/framework/dataprep/features/file.py) 和 [模拟可见时钟回放](/Users/kerry/work/bomber1/pro/bomber/framework/datahub/features/replay.py:17) 已有，可直接复用，避免新增专用指数Provider或复制策略算法。

**M04与netted的语义需明确区分。** 当前 [归属规划](/Users/kerry/work/bomber1/pro/bomber/framework/trader/execution/attribution.py:159) 在independent模式不跨策略划转；netted模式允许内部抵消并更新归属，单独记录transfer，不生成柜台成交。M04中“fills只由真实执行来源推进”应按执行政策解释：independent为归属成交，netted还包含已确认政策允许的审计划转；两者均不得伪造FillEvent或让整账户仓位覆盖策略。此处解释已确认的两种政策，不撤销netted能力；08当前以independent符合用户的真实成交仓位管理预期。

**实施优先级：** 08继续核对新入口实际恢复／新鲜输入／正常停止；01／03／04优先补共享DDB预热与实时接续；08补同Schema及可见性规则的特征回测接入。随后验统一行情工厂的实际业务组合、补执行工厂，并接通BN／LXGBN。当前08不因CU历史源缺失而增加无用预热，也不以08不需要窗口为由免除其他策略的M06要求。其余完整GAP仍独立跟踪，不要求先关闭全部GAP才能运行当前限定范围策略。

## 完成情况总览

**整体仍为 11 项 GAP：GAP01、GAP03 已按既定范围关闭，其余 9 项未整体关闭。** 本轮新增了多个已完成的子项，不新增整体关闭数量。

**同一物理账户同时运行多个策略是正式交易系统核心要求，保持 GAP10 高优先级。** 当前同进程共享宿主已经取得双策略实际成交、冻结分配、独立停止和同日恢复证据；同机跨进程及三策略两种执行模式已交付并通过局部离线验证。不能再将“账户只能运行一个策略”作为产品能力边界；完整 Linux 三策略及真实独立订单模式仍待验收。

| 类别 | 当前项目 | 判断 |
| --- | --- | --- |
| 已按整体标准关闭 | GAP01、GAP03 | 原关闭结论保持；新增模式独立验收 |
| 已有实现与局部验收，整体仍开放 | GAP04、05、06、07、08、10 | 本轮重点进展在共享执行、实时特征消费、原生恢复及通用会话 |
| 核心修复／设计决定未完成 | GAP02、09、11 | 普通期限主链、NT／框架状态职责、部署平台仍需落实 |

本轮最重要的状态变化：

- **共享母单按冻结所有者撤单已实现且真实零成交撤单通过**，旧“只按触发策略撤单”的代码缺口已修复；真实部分成交后撤余单、拒单及 peer 后续实际重规划仍未覆盖。
- **同机跨进程协议／客户端与 independent 模式已实现**，旧“协议尚不存在”已过时；跨主机保护和完整三策略线上验证仍缺。
- **08 实时非映射目标链与期权原生成交已通过相应范围**；DolphinDB 分钟推送＋指数查询也取得实时 Recording 目标链验收，不再停留于工程映射。
- **静态会话核心及 CU 两来源窗口／分钟序列对照已完成**；完整会话专项、历史行情读取与实时接续仍未关闭。
- **08 共享执行接入及启动跨日恢复已交付、离线通过**，真实原仓恢复及新来源继续管理尚待验收。

## 各 GAP：已完成与尚未完成

| GAP | 当前已完成／已验证 | 尚未完成 | 整体状态 |
| --- | --- | --- | --- |
| 01 成交归属 | 原 EMA 闭环；04 PB 开平、MO 期权原生成交；共享母单双来源全量分配及归属合计核对；停止回调修复 | 新 independent／真实多账户及异常共享成交按新增范围验收 | **已关闭，保留原范围** |
| 02 目标截止时间 | 根因已分析；新共享路径保存源期限，逐单检查，CTP 写前保存后复查实际发送期限 | 普通／动态／其他分发及执行路径统一期限、在途到期维护与恢复；不能以共享局部实现关闭全项 | **未完成** |
| 03 数据源边界与一致性 | 来源政策、参考时效、失效闭闸与数据库→导出→本地一致性；控制线程缓存 | 每次运行继续核验来源日／可见性；新历史与特征分别归 04／08 | **已关闭** |
| 04 历史预热与实时接续 | 历史 Provider／逐日因子、真实 IM 指标重启回补；统一静态会话及 CU 两源窗口／分钟时间点对照 | 3 份历史源 JSON 统一、CU 历史分钟、当前正式历史→实时接续、MongoDB 历史 Provider；暂缓缺口保留 | **部分完成** |
| 05 多腿协调与续执行 | 04 固定组合／Tick 续执行；03 双腿 Live；新共享目标替换撤单／确认及 peer 重规划离线、真实零成交母单撤单 | 通用腿生命周期、裸腿数量／时间限制、超时／补偿；真实部分成交／拒单／peer 续成；03 修正版在线目标完成 | **部分完成** |
| 06 在线重启与恢复 | 04 模型恢复；03 空检查点模型恢复、带仓原生恢复／平今局部证据；共享同日双来源恢复、只读终态检查点修复；08 跨日交付 | 当前三策略／08 共享跨日实际恢复；01 模型恢复；未知／挂单断点／漏成交；自动换约与一般跨日恢复 | **部分完成** |
| 07 执行语义与风控 | 政策、范围／来源健康、资金门控；四交易所适配；MO 实时期权、CU 双腿及共享归属预算／原生开平证据 | 四交易所完整实际组合验收、TIF／套保／费用／保证金／组合风险；非零期货对冲；最新变更原生回归 | **部分完成** |
| 08 自定义特征／DataHub | 配置化 Schema／版本／Provider／对齐与有限等帧；252 项＋D0 离线、DDB／CSV；实时 CTP＋指数及 DDB 分钟流 Recording 目标链 | 真实现货／期现、MongoDB／Parquet、历史可见版本与量额语义；恢复后持续消费、在线迟到／超时样本 | **首期多层验收完成，整体开放** |
| 09 状态权威职责 | 唯一策略来源、物理账户／策略归属分离；netted 与 independent 显式政策；账户宿主统一执行事实 | 正式统一总体架构的 NT 主链／框架主链口径及 CTP／BN 镜像、读取和未知外部回报一致性 | **职责部分明确，未关闭** |
| 10 同账户多策略／多端／多账户 | 同进程双来源实测、归属／停止／母单撤销；同机 IPC／客户端、01／03／04 工厂与两模式、可靠事件日志局部离线；08 共享接入 | Linux 三策略 netted／independent、真实部分成交／拒单／worker 停止重启；CTP＋BN、真实第三方多账户、跨主机、BN 写前／漏成交 | **高优先级，部分完成** |
| 11 Windows／Linux | Linux CTP、账户／状态锁和 Unix 服务路径已有 | 正式统一部署目标；若需 Windows，补 SDK／构建／锁／跨进程传输及生命周期兼容 | **未完成** |

依据：[整体清单](/Users/kerry/work/bomber1/pro/doc/audits/ARCHITECTURE_GAPS_2026-10-09.md)、[目标架构](/Users/kerry/work/bomber1/pro/doc/architecture/ARCHITECTURE_FIXED.md)、[实现台账](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md)。历史索引仍有实施前状态，不以其旧“未实现”段落否定最新源码及证据。

## GAP10：共享执行已从端口扩展到正式宿主与同机进程服务

### 已完成的结构与政策

公共 SharedAccountExecutionService／策略端口、Shared Host 和 CTP Shared 生命周期已接入。一个账户持有唯一 TD、物理账户写入锁、PositionManager、冻结归属、订单机、CTP 今昨账本和账户文件；策略各自保留 Runner、信号窗口、目标版本、路由与信号文件。公共框架不导入 demo，应用工厂装配原 01／03／04，另增加 08 接入。

两种模式显式选择：

| 模式 | 当前语义 | 验收状态 |
| --- | --- | --- |
| netted | 同账户目标汇总，允许策略间抵消；内部划转另审计，不伪造柜台成交／手续费 | 双策略共享实际正常成交及分配已验；完整 01／03／04 三策略仍待验 |
| independent | 各策略独立真实订单及归属，不跨策略抵消；账户层统一风险、编号、持久化与连接 | 代码、实际 OS socket 进程及替身柜台离线通过；Linux 三策略原生订单／停止恢复待验 |

同机 Unix JSON 协议、能力身份、请求幂等／冲突检查、写前日志、同代事件日志、确认游标、租约到期及重连补交已有实现。策略进程不争物理 TD 锁；回报补交按至少一次传输处理，自定义外部回调副作用仍须按事件 ID 幂等。不是跨主机服务；未知活动单、未决发送及未覆盖跨日条件仍闭闸，不盲目重发。

依据：[共享交接最新 REV19](/Users/kerry/work/bomber1/pro/doc/HANDOFF/gaps/HANDOFF_GAP10_SHARED_ACCOUNT_2026-10-13.md)、[三策略固定需求](/Users/kerry/work/bomber1/pro/doc/HANDOFF/gaps/GAP10_THREE_STRATEGY_REQUIREMENTS_2026-10-14.md)、[运行手册](/Users/kerry/work/bomber1/pro/scripts/integration/shared_account/THREE_STRATEGIES.md)、[公共共享核心](/Users/kerry/work/bomber1/pro/bomber/framework/trader/execution/shared.py)、[同机协议](/Users/kerry/work/bomber1/pro/bomber/framework/trader/execution/ipc.py)。

### 已取得的真实证据与限制

| 场景 | 已完成结果 | 不能扩大到的范围 |
| --- | --- | --- |
| 同日双来源恢复／共享成交 | 10 单、13 手；RB 卖开 2 手冻结为横截面与 EMA 各 1 手；最终归属合计 RB-2／HC+1 与柜台一致，活动单 0，范围外仓位保持 | 原启动／检查点及运行摘要未全部展示；不是未知订单或漏成交通用重建 |
| 停止回调修复正常路径 | 报告 1791945013262536404：7 单、10 手；EMA 1 与横截面 6 次归属回调均入队／派发／接收一致，pending 为 0；EMA 停止后 peer 仍提交目标 | 所有真实成交在 EMA 停止前；停止瞬间排队／迟到成交边界为离线覆盖 |
| 非提交所有者撤共享母单 | 报告 1791959032663749879：CU 2 手双来源各 1 手，ACCEPTED→CANCELED，非提交者停止、peer 活动、0 成交、活动单 0、原文件哈希不变 | 没有真实部分成交、拒单、撤单后 peer 新成交 |
| 共享故障专项 Linux | REV13 48 文件核验、7 项故障回归通过，10 源摘要一致 | 受控传输／替身柜台，无真实柜台部分成交或拒单 |
| REV19 三策略／IPC 离线 | 65 shared-account＋15 母单＋10 IPC／应用＋116 公共，共 206 项；socket 使用 3 个实际 OS 进程 | 完整 03 原生 28 项因本机 Bar 依赖未执行；Linux 三策略 replay／realtime 未验 |

零成交母单撤单之后的 partial 试验只见受理／撤单、0 成交，行情检查失败正常撤销，不登记真实部分成交通过。新增诊断保留原 3 秒接收／10 秒事件预算，未因测试需求放宽门禁。

依据：[双来源成交](/Users/kerry/work/bomber1/pro/doc/evidence/live/GAP10_SHARED_ACCOUNT_RESUME_TWO_SOURCE_20261013.json)、[停止回调验收](/Users/kerry/work/bomber1/pro/doc/evidence/live/GAP10_STOP_CALLBACK_ACCEPTANCE_20261014.json)、[真实母单撤单](/Users/kerry/work/bomber1/pro/doc/evidence/live/GAP10_SHARED_MOTHER_CANCEL_SIMNOW_20261014.json)、[故障 Linux 证据](/Users/kerry/work/bomber1/pro/doc/evidence/live/GAP10_SHARED_MOTHER_FAULTS_LINUX_20261014.json)、[三策略离线](/Users/kerry/work/bomber1/pro/doc/evidence/live/GAP10_THREE_STRATEGIES_OFFLINE_20261014.json)。

## GAP02／05：新共享路径已有修复，不能继续写成全部未实现

当前普通及动态 Runner 仍调用 TargetStore.apply(intent) 而未传处理时间，GAP02 主链整体缺口保留。新共享路径已经保存各源 deadline、内部划转前检查、取有效贡献期限、逐 Backend 发送前核验；CTP Driver 在写前保存之后读取 actual_send_deadline_ns 复查。不能再笼统说“所有共享贡献没有期限”，也不能认为共享实现自动覆盖普通、动态、Joint、旧 fanout 及所有在途到期恢复。

CTP cancel_strategy 已按 order.strategy_id **或 allocation_legs 所有者**识别母单，旧按触发者单独筛选的问题已修复。共享目标替换、暂停／停止／到期先撤未完母单、确认前不替换同合约订单；来源贡献最终冻结到实际归属，保留持仓而不隐式平仓。停止流程先关新目标、排空已入账及迟到事件，再解绑；超时保留绑定及队列，回调失败保留事件并关闭账户。

这补齐了旧评估指出的多项机制。剩余是受限路径外的统一实现、真实部分成交／拒单／peer 续成与通用腿状态、敞口、超时／补偿。03 独立入口目标变化也已补撤旧策略单，REV6 27 项 Linux 通过，但在线修正版双腿最新目标完成尚无通过记录。

依据：[普通入口](/Users/kerry/work/bomber1/pro/bomber/framework/trader/runner.py:290)、[共享期限与协调](/Users/kerry/work/bomber1/pro/bomber/framework/trader/execution/shared.py)、[原生发送及所有者撤单](/Users/kerry/work/bomber1/pro/bomber/framework/trader/execution/ctp/native_driver.py)、[03 REV6 回归](/Users/kerry/work/bomber1/pro/doc/evidence/live/CALENDAR_SPREAD_LIVE_REV6_PASS_20261013.json)。

## GAP08／07：实时特征消费与期权成交已有正式增量

首期配置化特征 Schema、不可变 Payload、版本／可见性、文件／DolphinDB／MongoDB Provider、EXACT／WINDOW／ASOF 和有限等帧保持已实现。PF1203 REV2 的 252 项＋D0、PF1204 REV3 的 195 项属于不同版本覆盖，不相加为唯一数量。真实 DDB 元信息、标准化读取与 CSV 往返已有证据，observed 不伪造历史首次可用时间。

| 新场景 | 已登记结果 | 当前剩余 |
| --- | --- | --- |
| 10 月 13 日 CTP 实时＋DFS 指数 Recording | 报告 1791861210699461223，15 帧、11:19 非零 MO 目标；返回验收 target_chain=passed，同分钟及接收期限核验通过 | 无线上指数超时样本，不声称所有迟到／恢复场景已验 |
| 10 月 13 日实时非映射 SimNow | 报告 1791869412309900311，1 单 1 归属成交，MO2611-C-7400 空 1；策略／执行／账户一致，活动单 0、清理无错 | 价与交易所 ID、原文件读回未全部提供；无实际平仓及非零期货对冲 |
| 后续同日 resume | resume_ready=true，但原始深度事件超龄／全流 STALE，运行 failed，停机保留原空 1 且活动单 0 | 证明恢复启动局部通过，持续健康运行及完整恢复未通过 |
| 10 月 14 日 DDB 分钟推送＋DFS 指数 Recording | 报告 1791956075214033439；返回原 summary 复核 target_chain=passed，16 帧、13:40 MO2611-C-7300 空 1 目标，skipped=0 | 此来源下原生新成交、持续至收盘、迁移／恢复未验；原进程退出码未独立核对 |

DolphinDB market 使用 stream_cffex_1min，指数为 index.minute 查询；market 与 execution 分离，不因行情改源自动选择交易端。Recording 不连接 TD、不产生柜台成交。该目标链已经超过旧“真实实时消费待验”的状态，但 GAP08 的真实现货／期现、MongoDB／Parquet、量额单位及完整历史版本、恢复消费仍未齐。

CTP 原始 FULL／REDUCED／FULL 样本中首次全链接收间隔约 31.55 秒，缩减订阅后改善，第二次全链仍有间隔；只能确认关联，尚未证明柜台、网络、回调或调度的具体根因。切换 DDB 后本次目标链成功不等于原 CTP 连续性问题已定位或消除。

依据：[实时 Recording](/Users/kerry/work/bomber1/pro/doc/evidence/live/OPTION_VEGA_REALTIME_RECORDING_20261013.json)、[实时成交及恢复失败](/Users/kerry/work/bomber1/pro/doc/evidence/live/OPTION_VEGA_REALTIME_SIMNOW_FILL_20261013.json)、[DDB 实时验收](/Users/kerry/work/bomber1/pro/doc/evidence/live/OPTION_VEGA_DOLPHIN_RECORDING_ACCEPTANCE_20261014.json)、[原始 ABA 诊断](/Users/kerry/work/bomber1/pro/doc/evidence/live/OPTION_VEGA_RAW_ABA_20261014.json)、[配置化特征交接](/Users/kerry/work/bomber1/pro/doc/HANDOFF/gaps/HANDOFF_GAP08_DECLARATIVE_2026-10-12.md)。

## GAP06：模型、原生及跨日恢复分别评估

| 恢复范围 | 当前完成情况 | 尚未完成 |
| --- | --- | --- |
| 04 模型同文件 | 原 9 帧／6 次成交、版本 6→7、第 4 帧新目标、归属和执行一致，通过保持有效 | 不能代替原生账户恢复 |
| 03 CU 模型原空检查点 | 14 帧／8 目标／8 模型成交、最终双腿及在途／未归属 0，passed | 原始文件为零仓空目标，不扩大为非零旧仓／目标恢复 |
| 03 CU 原生带仓 | 恢复代数 8、平远腿今仓；再次恢复代数 19 后双腿最终 0，停机撤单／对账干净 | 两次整体 failed，最新目标仍有旧挂单在途；REV6 修正版在线最新目标完成待验，平昨／跨日换约未验 |
| 共享同日终态修复／恢复 | 真实只读核验备份 3021→3022、不发单冻结贡献；后续双来源正常成交及检查点延续 | 限同日、终态与权威事实一致；不是漏成交、未知订单或未决发送自动重建 |
| 08 共享／启动跨日 | 原生归属导入、备份原代、权威今昨数量／成本校验、同日去重、次日清缓存／不重放旧目标、CFFEX 独立平仓已交付，局部离线通过 | Linux 原生 state／recovery、原仓实际跨日启动及新来源继续管理尚未验收 |

08 跨日范围限 realtime、向前 TD、固定策略集合／执行政策、原订单终态、无未知活动单、总仓及成本条款一致；非零持仓／目标路由不得移除。独立执行的 SHFE 等昨仓分配仍拒绝，不能将 CFFEX 局部方案扩大成所有交易所跨日恢复。不是跨夜常驻、自动换约、漏成交归属重建或多份检查点合并。

旧 CTP→DDB 显式迁移与新共享恢复均有代码／离线证据；真实准备和 resume 仍待返回。不新建状态丢掉原仓，不把旧柜台快照当作当前事实。

依据：[03 带仓恢复](/Users/kerry/work/bomber1/pro/doc/evidence/live/CALENDAR_SPREAD_SIMNOW_RESUME_INCOMPLETE_20261013.json)、[03 旧挂单失败](/Users/kerry/work/bomber1/pro/doc/evidence/live/CALENDAR_SPREAD_SIMNOW_STALE_ORDER_20261013.json)、[共享只读修复](/Users/kerry/work/bomber1/pro/doc/evidence/live/GAP10_SHARED_ACCOUNT_RECONCILE_REMOTE_20261013.json)、[08 共享跨日交接](/Users/kerry/work/bomber1/pro/doc/HANDOFF/live/HANDOFF_OPTION_VEGA_SHARED_ROLLOVER_2026-10-14.md)、[跨日离线证据](/Users/kerry/work/bomber1/pro/doc/evidence/live/OPTION_VEGA_SHARED_ROLLOVER_20261014.json)。

## GAP04 与独立会话专项：核心已实施，行情接续仍未完成

calendars.SessionCalendar 已统一时段、跨午夜、交易日归属、窗口和分钟枚举；trading_sessions.py 旧名称为兼容适配，load_sessions 不再按 CU／IM 分支选择两套算法；ImDayWindow 仍为限定日探针，但委托同一服务。静态七字段时段、DDB／Feather Provider、显式绑定和日期例外已接入。

CU 真实 5 段、10 月 12／13 日共 10 窗口、最近 600 与区间 930 个完整交易分钟时间点两来源一致。**时间点枚举不是读取了 600／930 根行情。** 清理后 trading-hours 43 项本地通过；清理前 IM 9、history-provider 41、gap03 22、entry 55 的远端结果有效于其版本，清理后 IM 复验仍待提供。

3 份旧 demo 日历 JSON 已清理，7 条停夜盘例外已迁移 CSV／Feather，Live 例外表尚未部署。3 份 history JSON 尚未统一，CU 历史分钟源缺失；当前 CU Replay 仅见 history_enabled=false 的实时 3／5 根预热进度，没有最终 summary。原 IM 239／240 根指标停机重启保持通过，13:00 缺口与主力／因子偏差按用户约定暂缓，MongoDB 历史 Provider 仍占位。

真实期权系列会话、合约级覆盖、规则历史版本／as-of 与统一历史→实时仍待完成。该专项保持独立，局部完成不调整整体 GAP 数量。

依据：[会话专项当前台账](/Users/kerry/work/bomber1/pro/doc/data/TRADING_SESSION_GENERALIZATION_2026-10-09.md)、[CU 实际对照](/Users/kerry/work/bomber1/pro/doc/evidence/live/TRADING_SESSIONS_CU_PASS_20261013.json)、[统一会话源码](/Users/kerry/work/bomber1/pro/bomber/framework/calendars/sessions.py)、[Runtime 兼容入口](/Users/kerry/work/bomber1/pro/bomber/framework/trader/runtime/trading_sessions.py)。

## 当前优先事项与未改变的设计缺口

1. **验三策略两模式。** 以 REV19 完整源码／配置一致版本核验 Linux 原生组、共享服务及三个原策略 worker，分别观察 netted 抵消／independent 独立订单、归属、游标、停止重启及最终柜台事实。16:00 replay／21:00 realtime 是计划，未收到通过报告不登记完成，未通过不自动切环境。
2. **继续共享异常及一般期限。** 真实部分成交后撤余单、拒单与 peer 续成仍缺；普通／动态主链及非共享路径统一 deadline 仍为 GAP02 核心待办。通用多腿敞口和补偿不能由正常成交或零成交撤单替代。
3. **验 08 原仓与跨日。** 按新共享入口实际禁单核验原归属、TD、今昨成本和条款，再验证新日持续消费／停止；保留原文件与备份。历史原空仓位不得凭本地记录推定为当前柜台事实。
4. **补历史及会话数据。** 统一历史源配置、确认可用分钟源、部署完整例外并验当前历史→实时；随后真实期权系列及规则版本。
5. **收敛 GAP09／11，保留 GAP10 其他范围。** 同账户两模式的账本职责更明确，但 NT／框架状态整体决定仍未完成。原生 CTP 限 Linux，新增 Unix socket 不形成 Windows 兼容。CTP＋BN 双端实测、第三方多账户、跨主机保护与 BN 写前／漏成交恢复仍独立开放。

本次更新清理了旧“仅端口／没有跨进程”“母单撤单未实现”“实时非映射全部待验”“会话代码未开始”等失效结论，保留各自实际证据与限制。未修改共享清单、专项历史、状态文件或部署摘要；已通过且未受新增代码影响的验收不因文档更新重复，未覆盖的新版本／场景单列验证。
