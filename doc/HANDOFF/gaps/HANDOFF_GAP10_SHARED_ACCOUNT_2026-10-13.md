# GAP10 同账户多策略：同进程首期实现

日期：2026-10-13，Asia/Shanghai。状态：公共共享执行、配置化原生入口源码与本机
离线验收已交付；Linux原生01／04装配与真实共享SimNow成交待验。跨进程接入未实现，
GAP10整体不关闭。本次没有连接MD、TD、数据库或第三方端，没有实际报单／撤单。

## 程序结构

```text
trader/execution/account.py               原账户贡献核心；保持旧fanout接口
trader/execution/shared.py                策略身份端口、归属回报队列、贡献期限/预算/暂停
trader/execution/attribution.py           复用冻结分配；记录各所有者目标版本及内部划转来源
trader/execution/ctp/native_driver.py      按冻结所有者撤单；写前保存后复查实际发送期限
trader/runtime/live/shared.py            每策略Runner宿主、独立停止/重建、信号文件
trader/runtime/live/channels/ctp_shared.py 唯一TD、账户检查点、原生全量对账与停机
scripts/integration/shared_account/       应用配置、01/04策略工厂与正式启动命令
```

公共框架不导入demo。01／04仅增加注入共享执行组件和显式策略ID的装配参数，
继续使用原策略类及原Runner。原单策略CLI、旧fanout和model接口保留。
账户持有唯一PositionManager、归属账本、订单状态机、Driver和CTP今昨账本；
每个Runner保留自己的信号窗口、TargetStore和逻辑路由，向账户提交单策略完整贡献。
账户完成最终净额，不把Runner已汇总的多策略请求再次当成单策略贡献。

账户文件保存完整执行事实、各源目标/版本/期限和账户pending。策略信号文件只保存
身份与已接受目标版本，不写账户仓位或订单；指标恢复明确采用实时`rewarm`。
所有真实回报先按既有状态机/归属账本入账，再按冻结计划排入对应策略队列。
控制线程派发回调；回调查询看到最新已提交归属状态，不承诺重建历史部分成交时点的仓位。
跟随model回报不会作为此路径的真实成交来源。

## 生命周期和执行政策

- 一个宿主持有账户连接租约；每个策略另持自己的端口租约。停止一个策略不会断TD。
- 改目标时若旧归属仍在途，按allocation_legs全部所有者撤销共享母单；旧回报继续
  使用原冻结版本，不能重新归属。确认前该合约不发反向／替代订单，之后由新鲜行情
  按完整账户贡献重新规划。其他合约仍按自己的活动订单状态检查。
- 停止、暂停或到期：先阻止该源继续增加风险，撤单，等待其冻结份额全部结束，
  再把执行贡献固定为当前实际归属量。保留仓位，不把退出隐式解释为平仓。
- 内部净额／划转记录`execution_origin=internal_transfer`及双方目标版本，不制造柜台Fill。
- 按逐策略、账户归属绝对总量和名义金额检查预算，反向贡献不能用净额绕过；
  原每合约订单、持仓、资金/行情和会话数量限制仍在。未实现保证金分摊或组合Greeks。
- 保存每个源deadline，汇总取受影响有效期限；普通发送、冻结/内部划转、逐笔Backend
  以及CTP写前保存后检查。到期源不能继承另一个触发者的期限。此改动只覆盖新共享路径，
  不宣布GAP02普通／动态入口期限缺口整体关闭。
- 局部行情／参考故障暂停对应源；身份、归属、队列溢出、发送/撤单不明确及柜台拒单
  按账户故障闭闸。柜台回调不等待策略Runner锁，也不在回调里等待撤单确认。
- 账户恢复首期限定同TD日、同配置/路由、已结束订单及柜台总仓/今昨桶一致。
  未决发送、漏成交、未知订单、活动订单断点和跨日均保持闭闸，不自动重发。
- `restart_strategy`由应用工厂重建本策略Session后接回原端口，延续自己的目标版本；
  不恢复或替换其他策略的目标、仓位和TD。

账户同时使用`bomber-ctp`、旧`bomber-main-ema`锁，以及与Joint一致的物理账户
writer lease。均为同机协作保护，不能宣称跨主机fencing或限制外部手工终端。

## 入口及配置

详见[scripts/integration/shared_account/README.md](../../../scripts/integration/shared_account/README.md)。
默认配置检查不加载SDK、不联网。显式`--connect --enable-orders --confirm-simnow`
后才建立共享原生宿主。示例有EMA与RB/HC横截面，并在300秒停止EMA、保留另一策略继续。
示例日期须运行前重新核对；默认新会话须全账户空仓、无活动订单和不存在的全部状态文件。
显式`position_scope=strategy_instruments`可保留范围外持仓，约束和证据见末尾补充。

报告使用`completed`表示运行结束，交易验收单列。事件文件包含真实原生回报，摘要
记录逐策略仓位、回调数量、源目标、独立停止状态及最终柜台总仓／活动订单数。
零订单、模型成交或运行正常结束均不能替代共享柜台成交验收。

## 本机验证

本机Python：Codex bundled Python 3.12。集中报告：
`tests/results/main-ema-acceptance-1791895694029615000/summary.json`。

| 组 | 结果 | 范围 |
|---|---|---|
| shared-account | 27项通过，exit 0 | 实际公共Client/Planner/归属/状态机、真实公共CTP Driver/开平账本/持久化；传输为假柜台，SDK标识/无关类型声明为替身 |
| live-common | 116项通过，exit 0 | 原公共Live/渠道/恢复/执行组合回归 |

共143个TestCase通过。shared-account包含三个既有CTP driver/autosave/recovery脚本的
调用用例；不另把它们的内部断言当成额外TestCase计数。更早临时结果不合并为唯一测试总量。

已覆盖同向共享母单、部分成交、费用分配、重复回报、反向贡献和内部划转、旧目标
更新后的冻结归属、从非提交者撤销母单、停止前无信号、停止后另一策略继续、
单策略重建不改变另一源版本和仓位、同账户文件双账本恢复不重发、期限在内部划转／
写前保存后到期、策略／账户预算、错误身份/配置/归属、队列溢出和并发保存。
配置无网络检查与Python语法检查通过，`git diff --check`通过。

本机原生`tests.test_cross_section_live`不能导入：`bomber.framework.fixes`缺少可加载的
`Bar`。这是当前本机原生运行依赖限制，不能把替身测试写成01／04原生完整装配通过。
未修改C++或要求重新编译CTP SDK。

## Linux补验及剩余范围

同步本项SHA清单中的文件，并保留现有完整Bomber／CTP与01／04资料依赖，在项目根目录：

```bash
sha256sum -c doc/evidence/checksums/GAP10_SHARED_ACCOUNT_20261013_SHA256.txt
python -u -m tests.run_main_ema_acceptance --only shared-account --only live-common
python -u -m tests.run_cross_section_acceptance --only shared-account \
  --only cross-section --only entry --only attribution \
  --only ctp-autosave --only ctp-recovery --only ctp-driver
```

这批公共文件变化后，旧部署清单的摘要可能不再对应当前代码；不要覆盖旧历史清单，
使用本项新清单核对受影响文件。当前会话发现其他工作正在更新交易会话资料和共享
状态文档，本项没有改写这些并行文档及其证据清单。

原生装配通过后，仍须两策略同SimNow账户实际报单／成交、按冻结来源逐笔核对、
最终账户汇总、独立停止、拒单／部分成交和恢复报告。首次测试不要直接接管现有旧
单策略文件或管理合约内的旧持仓。原配置/全新文件要求仅用于明确的新会话。

独立策略进程接入本机账户服务的协议/客户端尚未实现；当前分别运行旧demo CLI仍会
争同一账户锁。CTP＋BN联合剩余验收、真实第三方多账户、BN写前/漏成交重建、
跨主机单写保护与通用跨日恢复保持原GAP10范围，未由本期实现或本机通过结果关闭。

## 启动路径修正

用户在Linux提供默认`example.json`配置检查通过输出；随后将说明中的占位路径
`/path/to/session.json`原样用于报单命令，文件不存在，程序在读取配置阶段退出。
该失败尚未连接数据库、MD或TD，不能计作柜台运行失败。

README已改为项目内`cp -n example.json session.json`的完整创建、修改、检查和启动
步骤，避免覆盖已有会话配置。CLI遇到不存在的文件现在打印实际绝对路径与修正
方法，并以exit 2退出，不显示Python回溯；不会创建宿主或连接柜台。
本机`SharedConfigTests`四项通过，其中新增缺文件回归验证`build_host`未被调用。
原143项离线结果保留为此前版本证据；本次没有重跑交易核心或连接柜台。
当前修正版本使用`GAP10_SHARED_ACCOUNT_20261013_REV2_SHA256.txt`，原清单保留历史。

## TD交易日预检诊断修正

用户连续提供两次Linux启动输出，报告为`shared-simnow-1791896943523363972`和
`shared-simnow-1791897246899904837`。配置TD日均为20261013、environment为realtime，
两次在TD登录后的日期检查处因不一致退出；尚未进入参考资料准备、策略装配或报单。
旧输出未包含柜台实际交易日，不能猜测为次日，也不能推定所用前置属于哪套环境。
`lshw/dmidecode`缺失提示未使这两次流程停在登录前，当前异常是日期校验。

共享入口现在先打印并保留实际TD日、预期TD日、环境、MD/TD前置和预检报单数，
再执行原日期校验。失败报告即使没有Host也保存`td_preflight`。不自动改日期、
不跳过检查、不改变MD原始交易日或参考资料日语义。排查使用既有只读入口：

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

核对`CTP连接配置`与`CTP只读结果`两行之后再选择一致的配置／前置。
本机shared-account 31项通过，exit 0；报告为
`tests/results/main-ema-acceptance-1791897478995121000/summary.json`。
新增三个用例覆盖实际/预期日可见、匹配时保留replay原始MD日、组装失败报告仍保留
实际TD日。未重跑无变化的live-common，不把不同版本用例数叠加。
当前交付清单为`GAP10_SHARED_ACCOUNT_20261013_REV3_SHA256.txt`，旧版本证据保留。

## 显式管理范围与已有持仓隔离

用户提供的柜台只读结果确认TD日20261014、无活动订单，已有MO／SC／NI／I／AG
五个非零持仓。修正配置交易日后，Linux报告`shared-simnow-1791898310294263646`
在原全账户空仓检查退出，预检报单数0。本机没有连接或操作这些持仓。

复用现有`InstrumentPositionScope`和portfolio builder，不改01／04策略或Runner。
新增顶层显式`position_scope=strategy_instruments`；未配置或account仍要求全账户空仓。
范围从全部策略当次执行路由的真实合约并集取得。预检先要求全账户无活动订单与
可用资金，取得参考资料后检查范围内总仓为零；净仓为零的双向仓也不能接管。
正式账户、Shared服务和Backend须绑定同一范围，Backend禁止范围外报单。

完整PositionManager保留全账户净仓；只有管理合约可进入策略归属、在途委托与CTP
执行账本。范围外总仓/今昨桶另存`external_positions`组件，随原RuntimeStateManager
原子保存，恢复不能改管理范围。授权前、定期检查、停机与同日恢复核对范围外基线；
仓位/分桶变化、范围外活动订单、查询失败立即disarm并标记账户须对账。
在线检查允许管理合约在两次查询之间正常成交，停机/恢复仍比较完整管理总仓与今昨桶。
不自动分配、平仓或撤销未知外部订单；冲突停机保留资源锁并报告未完成。

预算继续按管理策略归属绝对量/名义金额计算；外部仓位不占本次归属预算，但资金
仍来自完整物理账户权威查询。没有增加外部仓位估值、组合保证金或跨终端单写保证。
范围外基线固定，不能将该模式解释为允许其他终端并行交易。报告全账户final_gross
保留，另列管理范围、范围外基线和final_managed_gross。

运行方式和只读参考资料核验见README；配置TD日20261014不能推定资料日为20261013。
之前的预检失败不需要增加resume；已有正式共享文件则按原身份和管理范围恢复。

本次集中测试报告与新证据文件、REV4清单记录以下离线覆盖：已有五类持仓及外部
双向净零仓保存，管理成交不赋予外部仓位归属，停机/恢复不重发，默认拒绝外部旧仓，
管理合约双向仓拒绝启动，外部总仓/今昨桶变化闭闸且原恢复文件不变，外部委托
不自动撤销，Backend拒绝范围外合约，损坏基线拒绝且不改变对象，正常成交查询竞态，
入口配置到所有组件的合约并集绑定。实际公共CTP Driver/Backend/Planner/归属/持久化
运行，传输和行情/资料工厂为测试边界；SDK标识及不相关类型导出为本机替身。
Linux原生01／04装配、参考资料及共享SimNow真实成交仍须补验，GAP10整体不关闭。

当前集中报告：`tests/results/main-ema-acceptance-1791899018152793000/summary.json`，
shared-account 42项、live-common 116项，共158项通过；不叠加以前版本计数。
默认example配置无网络检查和`git diff --check`通过。本次源码与证据使用
`doc/evidence/checksums/GAP10_SHARED_ACCOUNT_20261013_REV4_SHA256.txt`核验，
历史SHA清单与首期证据不覆盖。

## 继续验证：完整宿主同日恢复及运行调度

用户询问GAP10是否尚需验证，本轮继续核对审计关闭条件。整体仍开放：同账户两
活跃来源实际成交、共享母单部分成交/撤单、远端同日恢复待验；跨进程协议、跨主机
保护、BN写前/漏成交重建尚未实施，CTP＋BN及真实第三方多账户须另行实际验收。
本次没有连接远端或柜台，不将上述项宣布通过。

新增完整Host恢复回归模拟当前用户场景：one无目标先独立停止，two已成交且有仓；
保存全账户/两个策略文件，再重建原生账户与真实公共Runner并resume。恢复旧目标
不发单，two版本和归属保留，范围外AG保持未归属；one首次提交新目标后不改变two，
停止one后two以更高版本继续，整个Host停机对账和保存成功。传输为假柜台，未伪造
实盘结果。另增强应用build_host回归，检查仅改变seconds/stop_after_seconds后
账户及各策略身份完全一致，并从原账户文件恢复不发单。

源码及测试确认：seconds与stop_after_seconds是宿主调度，未写入恢复身份。
此前将延长停止时间视为必然身份冲突的说明已修正；策略argv、TD、环境、路由、
参考条款和账户预算仍须一致。不新增宽松恢复开关，不修改账户文件或策略算法。

本地新增`resume_acceptance.json`，相对现有session.json仅把EMA停止时间300→480秒，
总时长600秒和全部原状态路径不变，保留原配置。新文件须同步Linux并显式--resume，
当前账户检查点是20261014，当次TD和参考合约变化则继续闭闸。已有仓位和原文件
不清除，预算和20笔会话报单上限不扩大；自然无信号时双来源验收仍未完成。

集中报告`tests/results/main-ema-acceptance-1791901497492179000/summary.json`：
shared-account 43项通过，exit 0；本次新增检查属于完整Host/应用入口恢复关注点，
此前无变化的live-common 116项不重复，也不与旧版本计数相加。新配置无网络检查
通过。本机原生01/04测试依赖限制仍在，须在Linux补cross-section/entry/attribution
及原生CTP相关组。本轮交付使用REV5清单；原始实盘日志逐笔核对另存专项证据。

## 远端同日恢复与双来源共享成交反馈

用户提供resume_acceptance配置的summary.json与events.txt：status=completed、
failure=None、final_active_orders=0，账户failed/pending均false、draining为空。
本次报告目录和运行机摘要未展示，不补造；本机未连接柜台。
OrderRef16至25共10个订单，10条ACCEPTED、10条FILLED，累计成交13手（含开平），
回报ID和成交ID无重复。OrderRef18的RB卖开2手冻结为横截面版本8的1手与EMA版本1
的1手；同一真实成交分配给两个策略。横截面10次、EMA1次回调与冻结份额数一致，
11个归属回调不能当作11笔柜台成交。

从上次横截面RB+1/HC-1、EMA无仓重算，最终横截面RB-1/HC+1，EMA RB-1，
合计RB-2/HC+1，与执行净仓、柜台总仓一致。横截面反向调整仍保留EMA归属，
五个范围外持仓不变。来源版本横截面6续至12、EMA首次1，新OrderRef从旧15续至16，
本文件未见旧成交重放。逐腿版本匹配本源source_revision；账户request_revision
与策略target_revision是不同版本空间，不要求数值相同。

用户日志中的同日恢复后双来源交易、共享母单全量成交分配与最终汇总核对通过。
仍缺真实部分成交、共享母单撤单/拒单、单策略原生重建；原恢复检查点与启动日志
未展示，不能扩大为通用故障恢复。跨进程协议、跨主机保护、BN写前/漏成交重建和
联合端验收继续开放。结构化证据为GAP10_SHARED_ACCOUNT_RESUME_TWO_SOURCE_20261013.json，
本次REV6清单记录文档和证据；交易运行代码不改，已通过测试不重跑。

## 后续运行失败与故障诊断

用户提供后续报告shared-simnow-1791903142425954554的截断摘要/异常链：
Host.poll发现策略行情或参考资料失效，清理时Account.stop调用Service.poll因执行
状态不明拒绝完成停机，最终status=failed、final_gross/final_active_orders均空。
账面横截面RB-1/HC+1、EMA RB+1，执行RB净0；不能据此认定柜台总仓为零或无活动单。
未提供完整account开头和events.txt，不能推定断线、拒单或回报冲突的具体根因。
之前成功双来源成交证据保留，但后续故障停机/恢复尚未完成，GAP10继续开放。

本次只增强诊断，不放宽恢复或执行闸门：run保留首次failure，单列shutdown_failure，
记录清理前failure_execution_diagnostics；Host区分参考版本变化与行情/账户未就绪，
包含strategy_id，snapshot列is_reconciled/is_armed/recovery_required/service_failed、
report_errors、portfolio_failure及最近执行audit。不调用额外对账或自动恢复旧目标。

新增回归验证原始异常不被停机异常覆盖，清理前回报冲突保留，Host快照可观察故障
标志且不做柜台查询。报告tests/results/main-ema-acceptance-1791903750064234000：
shared-account45项、live-common116项均通过，合计161项；diff检查通过。本机未连接
柜台。当前先取得该失败目录完整summary/events和TD只读三查询，不删除状态文件、
锁文件，不清除恢复标记，不直接重启报单。新诊断代码及证据用REV7清单，历史清单保留。

## 已结束订单的同日只读检查点修复

用户进一步提供柜台无活动订单、RB净0/HC+1及原检查点信息：generation3021，
saved_at_ns=1791903546244387339，failed/pending均true，所有订单终态，Driver next_ref32
且关联为空，源版本横截面14/EMA3。本机未读取远端原文件，不能凭净仓自行清标记。

新增显式--connect --resume --reconcile-only模式，不接受报单授权。复用应用工厂与
原身份/参考条款，但不启动MD、策略或Host；原生Driver禁单。账户和策略文件锁保留。
公共CTP生命周期先恢复/校验原检查点，拒绝本地未结束订单及冻结份额，再通过原Client
核对未知活动单、资金、净仓；完整核对总仓/今昨桶、策略归属与外部基线。任一不一致
不写文件、不撤未知单。全部一致后保存逐字节原文件备份，再由Shared服务冻结全部
贡献到当前实际归属、保留信号版本和暂停状态；只写一次原RuntimeStateManager原子
检查点，清已核实的failed/pending。未执行的旧目标不重新发送，策略文件不改。
正常resume对未决标记的拒绝逻辑不改，没有增加普通启动的宽松恢复参数。

备份命名为shared-account.json.before-reconcile-g3021.json（代数由实际原文件决定）。
报告status=reconciled，verification=checkpoint_reconciled_no_strategy_no_orders，
记录备份、原/新代数、orders_submitted=0、authorized=false、strategies_started=false。
只读修复通过后仍须等待管理合约有效行情并显式resume重新预热；不支持跨日、漏成交
重建或活动订单修复，不将本次修复替代完整GAP10故障恢复验收。

本机报告tests/results/main-ema-acceptance-1791905186158608000：shared-account50项、
live-common116项，共166项通过。新增成功修复/备份/暂停/后续恢复不重发、净零双向
总仓冲突保持原文件、未知活动单不撤单、CLI拒绝报单授权及不启动策略用例通过。
本机无柜台连接或实际报单。修复入口尚待Linux真实只读验收，部署用REV8清单。

## CU／AU额度计算及显式预算迁移

用户已提供原RB/HC只读修复成功报告shared-simnow-1791905355939548901，代数3021→3022，
原文件备份、failed/pending清除，暂停源/归属保留，报单0；该成功与本轮CU/AU独立。
用户随后要求CU/AU新策略ID，session_cu_au.json使用ema-cu-gamma/cross-cu-au-delta及
独立状态文件。原账户200万总名义上限保留，每合约最多1手。EMA完成预热首次目标
被总预算拒绝，横截面继续运行，最终CU-1/AU+1，活动单0；本次仅横截面10次成交，
不作为CU/AU双来源验收。用户片段未展示报告目录/完整account，不补造。

用户明确允许上调，但要求先按实际价格计算。所贴成交CU最高110670、AU最高900.26，
乘数5和1000，对两份CU1手加AU1手合计2006960元；超原限额6960元。
本地session_cu_au_230w.json仅将账户总名义限额2000000→2300000，保留全部策略
参数、单合约限额、最多1手、20笔会话上限及原状态路径。浮动行情仍须实时预算核验。

新增--budget-config，仅与--connect --resume --reconcile-only组合；使用旧配置恢复
原身份，候选新配置只允许上调账户max_gross_notional，其他项必须逐项相同。
复用同日全量权威校验和逐字节备份；暂停贡献冻结后，同代原子保存Shared预算与
SessionIdentityCheckpoint账户预算。保存失败恢复内存旧预算、旧身份与服务状态，
旧文件不覆盖。策略文件、归属及信号版本不改，不启动策略/MD、不授权/报单。
迁移通过后只能使用新配置resume，旧预算配置拒绝。原CLI正常恢复身份检查不放宽。

本机报告tests/results/main-ema-acceptance-1791907106968052000：shared-account53项、
live-common116项，共169项通过。新增迁移后新身份恢复/旧身份拒绝、归属保留不发单、
保存失败回滚及仅上调总名义限额的配置约束验证通过；预算异常新增实际金额/限额
诊断。新配置无网络检查通过，diff检查通过。本机未连接或修改远端账户，预算迁移
仍待Linux只读执行。交付REV9清单，原配置和历史证据保留，GAP10整体仍开放。

## 2026-10-14 预算迁移通过及横截面健康故障

用户提供预算只读迁移报告shared-simnow-1791907272093985739：代数713→714，
max_gross_notional200万→230万，原文件备份，orders_submitted0、authorized=false，
status=reconciled。CU-1/AU+1及范围外持仓保持一致，账户failed/pending均false。
随后新配置resume报告shared-simnow-1791907341703816222，EMA完成5根Bar预热并
获得1次归属成交回调，横截面5次。最终EMA CU-1、横截面CU-1/AU+1，合计CU-2/AU+1
与柜台总仓一致；活动单0，停机无异常，账户failed/pending/draining与回报错误均空。

运行status仍failed：横截面Tick目标推进时行情或参考失效后独立暂停，EMA按480秒
停止。该轮无预算超限；不能归因于CU/AU收盘或直接放宽10秒时效。未提供events.txt
和故障时逐合约时效，不能登记完整CU/AU双来源验收或推定哪一通道失败。

新增公共readiness_failure_context读取本地SessionReadiness.last_result和逐合约深度
年龄/交易日/回退标记、参考条款一致性。False的就绪结果也保留全部issues，不被
布尔过滤丢弃。Host检测故障和04 close_role_gate在故障发生时保存failure_readiness，
报告随策略列出；不访问数据库/柜台、不改变时效阈值或故障门控，策略算法不改。
当前先取得上述报告目录events.txt；若继续复测，原230万配置同日resume并同步诊断
文件，保留原全部状态。后续报告据failure_readiness区分EVENT/RECEIVE/TD/版本问题。
本机集中报告1791908261177042000，shared-account54项/live-common116项均通过，
共170项；原生04依赖限制仍待Linux组装验证。本轮交付REV10，GAP10整体仍开放。

## 2026-10-14 日盘恢复完成与回调计数待核查

用户报告shared-simnow-1791939620243739079为completed，EMA完成预热后configured_stop，
横截面host_stop，无故障就绪信息、账户失败、停机异常或活动订单。用户events.txt
共22条回报、11个订单（18至28），全部成交16手，报告ID与成交ID无重复。冻结归属
EMA两单共4手、横截面九单共12手。由原EMA CU-1、横截面CU-1/AU+1逐笔累计，
最终EMA CU-1、横截面CU+1/AU-1，与账户CU净0/AU空1一致，范围外仓位保持不变。
CU物理平仓2手仅改变触发来源归属，保留对方逻辑仓位；AU反向先平后开符合日志。

不能将此轮登记为所有回调验收通过：EMA订单21、25各有一次FILLED，但停机报告
fills_received仅1（横截面9与其成交数一致）。源码stop_strategy暂停后runner.stop，
端口stop清除handlers；待消费事件可能受停止边界影响，但日志缺消费/停止时点，
尚未证明原因。账户最终归属正确不等于回调完整。实际停止时点未记录，也不能仅凭
目录时间加480秒断定后续横截面成交已发生在EMA停止之后。保留原状态，继续核查
停止边界回调；部分成交、撤单、拒单、跨进程与跨主机等原验收要求保持开放。
逐笔证据：doc/evidence/live/GAP10_CU_AU_COMPLETED_FILL_REVIEW_20261014.json。
本轮仅补证据与交接，无运行代码变更、未连接柜台或报单。

## 2026-10-14 停止边界回调修复（REV11）

原生账户组件及真实Runner已复现：成交入账并排入策略队列，宿主立即runner.stop，
先停止/解绑策略再清除handlers，导致归属正确但on_fill未消费。历史EMA两次成交/
一次回调与此缺口一致，但历史消费时点缺失，尚未证明该轮具体原因。

共享宿主先与Runner发布串行关闭新目标，begin_stop保留绑定并撤销相关母单；
服务poll推进冻结，持续派发已入账事件，等待相关母单终结，包括对方剩余份额。
finish_stop_events与账户回报入账共用client._submit_lock确认终结并关闭队列输入，
释放账户锁后调用策略，清空队列后才runner.stop。不在柜台线程等待Runner锁。
停止中禁止新目标解除暂停；端口stop不重复开启draining。超时保留绑定及队列并
报错；回调失败保留事件/关闭账户，无处理器不得静默消费，封闭端口收到新事件
按异常闭闸。检查点格式、归属、预算、TD日和行情门控不改，同日原状态可resume。

报告增加execution_events的归属FillEvent入队/成功派发数、待处理数、最后事件ID
和关闭标记，以及stop_requested_at_ns/stopped_at_ns。端口计数本进程累计，单策略
重建后新实例计数须按重建边界核对。新增四项回归：排队成交、撤单期间迟到部分
成交、撤单超时保留绑定及禁止新目标、回调失败保留事件。集中报告
tests/results/main-ema-acceptance-1791941843551711000中shared-account58、
live-common116共174项通过，详细证据GAP10_STOP_CALLBACK_OFFLINE_20261014.json。
交付REV11清单，保留REV10。仍须Linux同日原配置resume，核对新摘要和events.txt，
不需再次预算迁移或手工清状态。本机未连接柜台或报单，GAP10整体保持开放。

## 2026-10-14 停止边界回调修复运行验收

用户报告shared-simnow-1791945013262536404正常completed。events.txt共14条回报，
订单45至51七单全部成交10手；冻结归属EMA一单2手、横截面六单8手。与新摘要
EMA接收/派发/回调1/1/1，横截面6/6/6一致；待处理事件及Fill均0，末事件ID一致。
订单48只归EMA，把其CU+1变为-1，保留横截面CU+1；订单50只归横截面，
将其CU+1变为-1并保留EMA-1。最终合计CU-2/AU+1与柜台总仓一致，范围外持仓
不变，活动单0，无账户/停机故障。未发现重复报告或成交ID。

EMA停止时间1791945539705794796之后，横截面audit仍提交目标，直到宿主停止；
本轮所有真实成交均在EMA停止前，不能登记停止后实际成交。停止瞬间已排队回报
及撤单期间迟到部分成交由新增离线测试覆盖，本轮远端取得正常成交后停止、计数
完整和独立策略继续目标推进的验证。该回调缺口登记为代码修复完成、离线边界及
远端正常路径通过，不再要求重复普通启动以随机撞停止瞬间；真实异常柜台场景
按GAP10剩余验收独立推进，不据此关闭整体GAP10。证据：
doc/evidence/live/GAP10_STOP_CALLBACK_ACCEPTANCE_20261014.json。
本轮只补验收记录，运行代码不变。文档清单REV12保留REV11历史摘要。

## 2026-10-14 共享母单故障链补验与拒单后贡献闭闸（REV13）

新增原生组件受控测试：5手双来源母单先成交3手，冻结分配2/1；重复TradeID不
重复记账。one虽份额已全成交，仍以冻结所有者撤two提交的母单，终态前不能封闭
回调输入。撤单确认后two只重规划差额2手，最终归属2/3、账户5，活动单0。
另一用例在实际_CtpPortfolioClient上拒共享母单，两所有者各一次REJECTED，无Fill
及虚构仓位，重复拒单不重通知；物理账户撤授权并标恢复必需。

此补验发现账户已拒单闭闸时，后续新目标虽被物理Client拒绝，但AccountService
可先更新来源版本/协调器及检查点。Shared.submit_targets现提前检查服务失败、回报
错误、未对账、恢复必需或portfolio_failure，禁止上述状态下修改贡献或撤单。
测试核对两端口均拒绝、完整service.snapshot_state不变、无新柜台发送。检查点
格式/预算/归属/策略身份不变；原配置可同日resume，不迁移或清除任何旧状态。

新增tests.run_shared_account_fault_acceptance专用七项入口，报告明确标识
controlled_transport_offline，传输与SDK类型为替身，真实共享/CTP/Runner代码执行；
不连接MD/TD/数据库，不读取交易状态。七项是60项shared-account的子集，不重复
计数。集中报告tests/results/main-ema-acceptance-1791946233968410000，
shared-account60/live-common116共176项通过；专项最终报告
tests/results/shared-account-faults-1791946289775784000七项通过。证据
doc/evidence/live/GAP10_SHARED_MOTHER_FAULTS_OFFLINE_20261014.json。
Linux同步REV13后运行python -u -m tests.run_shared_account_fault_acceptance，
贴出summary.json和tests.txt。真实SimNow部分成交/共享母单撤单/拒单仍待柜台证据，
不通过放大数量或制造非法委托来代替验收；GAP10整体保持开放。本轮未动Dolphin
行情、08策略等其他任务修改。

## 2026-10-14 真实共享母单撤单观察入口（REV14）

Linux REV13校验48文件及七项受控测试全部通过，十个源摘要与本机最终专项报告
一致，用户报告目录shared-account-faults-1791947151717659737已归档为
GAP10_SHARED_MOTHER_FAULTS_LINUX_20261014.json；不当作真实柜台部分成交/拒单。

新增应用层cancel_probe.py，原共享入口增加--acceptance-cancel-owner，须connect
及双报单授权，不得与reconcile-only组合，未知策略ID在组装/连接前拒绝。正常
Runner、算法、限价及账户保护不变；在控制线程观察自然产生的已受理、仍在途、
双来源冻结母单，指定owner须非提交者且所有冻结所有者仍活动。捕获后只调用
host.stop_strategy(owner, reason=acceptance_shared_cancel)，记录停止时点和其他
所有者活动，之后正常peer目标继续。验收组件不发新目标、不改变限价/数量/预算。

摘要shared_cancel_acceptance包括捕获计划、撤单报告ID、部分成交/拒单旗标、
同单peer保持活动、停止后目标/成交计数。passed限于母单CANCELED原生回报及
peer活动、本轮正常结束。抢先全成交或没有符合条件母单为not_exercised，不制造
非法委托或扩大交易来凑结果。部分成交/拒单必须另有真实回报，不能因撤单passed
自动关闭；程序保留原480秒策略停止、600秒宿主时长和原账户检查点身份。

四项新增回归：部分共享母单选择+非提交者撤单+peer重规划；单来源/终结单不
停止；成交抢先撤单不误登记通过；授权/身份约束。集中报告
tests/results/main-ema-acceptance-1791947768601573000，64+116共180项通过。
仅应用验收入口/CLI/文档及相关测试变更，公共交易实现本轮不变。交付REV14，
用户同步后先跑shared-account组，再于有效交易时段用原230万配置同日resume加
--acceptance-cancel-owner ema-cu-gamma。保留所有状态、策略ID及预算；保存新
summary.json/events.txt核对。本机未连接或操作柜台，GAP10整体保持开放。

## 2026-10-14 独立受控双来源母单入口（REV15）

自然观察轮shared-simnow-1791955825637609089正常完成，EMA停止后横截面三单
成交4手，回调3/3/3、最终CU净0/AU空1一致。但各单冻结legs只有横截面，
not_exercised正确；不再靠重复自然启动来保证双来源母单。逐笔证据
GAP10_SHARED_CANCEL_OBSERVER_REMOTE_20261014.json保留。

新增应用入口mother_probe.py及专用测试，不改公共交易实现。connect仅只读TD/MD/
DDB预检，Driver orders禁用；先检查原CU/AU和RB/HC两份共享检查点，保护全部
known_instruments（含净0逻辑对冲），取账户锁后重核文件哈希，候选无总仓、全
账户无活动单、合约生命周期距到期至少7天。DDB基础条款两次稳定读且发布时间
有效；原生MD确认盘口、涨跌停、TD日、3秒接收/10秒事件时效，不猜合约或价格。
默认选择CU其他未持仓月份合约，输出候选和2手被动限价计划，默认总名义120万、
显式上限230万。submit须双授权并指定本次实际instrument和limit-price，再验证
当前盘口仍满足被动价格及预算。

新报告目录独立账户及双Observer策略文件，原EMA/横截面算法不启动，不改原信号。
复用原scope builder/SharedAccount/Runner/CTP Driver及原子持久化，各源先提交1手
并写前保存，暂停首次规划后统一推进一次，唯一2手OPEN母单；验收Planner只给本次
明确限价，原Driver最多1张单，禁止重发/重规划。通过原宿主停止非提交者撤单，
保存对方当时目标/活动状态，再正常结束测试，不自动平仓。若意外成交，保留新
测试账本和实际持仓，先核对范围外基线及新增仓位，不能删检查点直接恢复原会话。
记录原哈希unchanged、实际TD/MD日、原生回报、冻结归属、peer_state_after_owner_cancel、
unexpected_fill及fresh account.json路径；抢先全成交不登记撤单通过。

新增八项测试覆盖纯预检边界、禁止接管原净0合约/总仓双向、被动价格/预算/时效，
实际scoped builder/双Runner/原生Driver/持久化装配和唯一母单撤销、外部持仓不变、
意外部分成交归属保留。SDK类型/传输为替身；未宣称真实网络装配。集中报告
tests/results/main-ema-acceptance-1791958560895722000，64+8+116共188项通过。
详细证据GAP10_CONTROLLED_MOTHER_PROBE_OFFLINE_20261014.json。交付REV15，
下一步Linux先只读预检取得具体合约/限价，再提供明确提交命令；原状态不得删除。
本机未连接或操作柜台，GAP10整体保持开放。

## 2026-10-14 受控入口只读身份检查修复（REV16）

Linux新组八项通过，但只读预检shared-mother-probe-1791958758853479920误拒。
实际/预期TD同为20261014，报单0，原文件unchanged。根因为新入口使用原
is_simnow_session，此属性还要求_enable_simnow_orders，默认禁单的只读模式
必然false。新增Driver.is_simnow_authenticated仅校验连接及broker/investor登录
身份；原is_simnow_session仍等价于enable_simnow_orders且身份成立，原发单门控
不放宽。入口只读验证新身份/TD，提交另要求原发单会话条件。摘要分别保留身份
及提交会话旗标，避免混为交易日不符。

新增真实Driver/替身传输回归：禁单登录身份通过、提交仍拒绝、TD错误及断开后
拒绝、发单数0。最终集中报告tests/results/main-ema-acceptance-1791958839136309000
中共享64/受控母单9/公共116，共189项通过，仍非SDK联网验收。证据
GAP10_MOTHER_READONLY_IDENTITY_FIX_20261014.json，交付REV16。Linux同步后
先重跑受控组，再用原connect只读命令取得真实盘口/合约计划，不启用submit，
不修改原账户状态或预算。本轮没有连接柜台，真实撤单待验，GAP10整体仍开放。

## 2026-10-14 真实零成交母单撤单与部分成交专项入口（REV17）

用户提供shared-mother-probe-1791959032663749879停机摘要及原生events.txt，
同一cu2612.SHFE两手母单ACCEPTED后，由非提交者mother-probe-one停止，随后
CANCELED，冻结双来源各1手、token及deadline一致，peer仍活动。发单1张、成交0、
最终活动单0、原检查点哈希不变、正常停机。零成交共享母单撤单阶段通过，逐笔
证据GAP10_SHARED_MOTHER_CANCEL_SIMNOW_20261014.json。它不覆盖真实部分成交、
拒单或撤单后peer实际重规划成交，GAP10整体保持开放。

应用入口增加--cancel-after partial及--partial-wait-seconds（默认30秒、1至60秒）。
只读预检可用--passive-ticks 0取得本方买一/卖一计划，仍禁止跨对手价；提交必须
显式指定本次instrument/limit-price及原双授权。两来源各1手、最多一张2手母单，
复用原共享端口、Runner、冻结归属与持久化，不启动原策略或修改原检查点。
受理后等待真实PARTIALLY_FILLED才停止非提交者撤余单，继续周期账户权威查询和
行情健康检查。已批准限价在等待期间不要求持续被动，仍检查行情时效/日/边界/预算。

partial_cancel_acceptance独立判定：同单部分成交在停止请求前、随后原生CANCELED、
peer活动及本轮正常结束才passed；无成交超时或抢先全成交为not_exercised；仅在
超时撤单期间才迟到的部分成交不能登记该项通过。Native CANCELED的filled_quantity
为0是状态回报，不撤回先前成交，必须结合PARTIALLY_FILLED和最终归属/柜台总仓。
新增仓位保留在独立测试account.json，不自动平仓；unexpected_fill沿用原字段，
本模式下表示测试合约发生过成交，不能单独用该旗标判断专项通过或失败。出现成交
后先核对原会话范围外基线，不删除新账本或直接恢复原会话。

最终集中报告tests/results/main-ema-acceptance-1791959866540020000，共享64、
母单15、公共116，共195项通过。新增回归覆盖部分成交触发、零成交超时、抢先全
成交、超时撤单迟到成交、行情故障正常撤单，以及原受理模式边界。传输/SDK类型
仍为替身，本机没有连接柜台；详细证据GAP10_PARTIAL_CANCEL_PROBE_OFFLINE_20261014.json。
REV17交付后Linux先校验文件、跑母单组，再进行partial只读预检取得新盘口计划；
不能复用旧110470价格或将离线通过当成真实部分成交验收。

## 2026-10-14 部分成交等待的行情故障诊断（REV18）

Linux REV17校验58项及母单组15项通过。真实轮1791960391664245707只有
ACCEPTED/CANCELED、零成交，等待时行情检查失败后正常停止撤单，最终活动单0，
原检查点哈希不变。后续只读1791960519672603845通过，容器时间显示UTC；这不能
确定前一轮是行情接收间隔、事件时间、回退还是交易日问题，不猜测或放宽时效。
应用price_plan异常增加具体issues、接收单调时钟年龄、事件年龄、原时间戳、
实际/预期日和原3秒/10秒阈值，保留原失败关闭及撤单流程。错误通过原failure
字段进入停机报告。回归分别检查错日、回退、接收超时、旧事件及未来事件的原因
字段，原15项通过，报告tests/results/main-ema-acceptance-1791960631420831000。
证据GAP10_PARTIAL_PROBE_HEALTH_DIAGNOSTICS_20261014.json，本机未联网/发单。
交付REV18；真实部分成交尚未覆盖，GAP10仍开放。

## 2026-10-14 三策略两种执行模式（REV19）

用户明确01/03/04：同进程netted允许抵消；独立进程independent各自真实订单及
归属仓位，不把柜台总仓覆盖进策略。已补03共享装配、独立同日CTP规划/归属风险、
Unix账户服务/客户端/策略worker、写前请求/同代事件日志/确认补交/连接租约/停止
排空/同日重启、只读观察与三策略配置生成。集中离线206项通过，完整03原生组
缺本地Bar构建未跑；Linux三策略replay/realtime未验证，不能宣布三需求验收完成。
同日已终结订单恢复支持；跨日、未知活动单和未决发送仍拒绝，不自动恢复/清仓。

固定需求与进度见GAP10_THREE_STRATEGY_REQUIREMENTS_2026-10-14.md，证据
GAP10_THREE_STRATEGIES_OFFLINE_20261014.json；完整运行手册THREE_STRATEGIES.md。
北京时间16:00 replay、21:00 realtime由用户Linux执行并回传；实时以验收通过为门槛。
本地未连接数据库/行情/柜台，保留旧配置/状态。GAP10整体仍开放。
## 2026-10-14 REV26：四品种独立会话

已交付SHFE/INE同合约原仓基线隔离、保留原成交/游标/OrderRef的同日范围迁移、
实际盘口预算上界及有界未来Tick等待。原策略算法、请求/回报归属及同代恢复结构
沿用。配置/实现边界及完整命令见
[四品种运行流程](../../../scripts/integration/shared_account/FOUR_PRODUCTS_RUNBOOK_20261014.md)。
215项集中离线通过，报告main-ema-acceptance-1791990691556189000。Linux原生
calendar-live及实际三个独立worker同时运行/成交/单独停止恢复仍待核验。
SHFE/INE独立昨仓、旧横截面已有交易历史的扩品种迁移不在本批自动恢复范围内。
GAP10继续开放。

## 2026-10-16 REV31：IM/IF共享执行与额度正常收尾

原01/03/04入口在共享SimNow装配中允许CFFEX，保持各自Runner与真实成交归属。
新增im-if只读观察/配置生成profile，固定01 IM240根、03 IM main+secondary120帧、
04 IM/IF21帧，角色合约来自本次观察，账户预算按实际盘口逐腿计入，额度200笔。
配置加载现在允许并解析三个策略原有历史参数；历史仍只预热，不产生历史订单。

额度不足在实际订单批次冻结/发单前识别；双腿不因仅剩一笔而先发一腿。额度停止
不会设置执行不明标记；暂停贡献、撤已知单并交付成交，账户socket保留至三个worker
确认停止和事件游标后关闭。取消失败/确认超时等真故障仍保持failed。计数随订单
写前保存，普通恢复不重置，显式新窗口机制保留。正常停止保留仓位，不自动平仓。

本机254项通过，含实际server._run、Unix socket及三客户端额度收尾回归；柜台/SDK
为替身。完整原生entry/calendar-live/cross-live、01历史调整及连续共享组受本机
原生Bar/指标/dotenv缺失限制，待Linux复验。未联网、未修改旧运行账本。
逐终端命令见scripts/integration/shared_account/IM_IF_JOINT_ACCEPTANCE.md；证据为
doc/evidence/live/GAP10_IM_IF_SHARED_QUOTA_OFFLINE_20261016_REV31.json。
本批不能登记IM/IF真实共享报单通过，GAP10整体仍开放。

## 2026-10-16 REV32：逐策略参考合约发现

用户回传预检三-readonly-1792128375630950926：TD正确，EMA配置预期IM2612、
实际加载IM2610，orders_submitted=0。REV31探针只读角色表、忽略因子表同角色
合约优先规则，生成的统一合约不能表示原01/04和无因子的03实际路由。
现复用LiveFuturesReferences按三种原有口径读取，观察记录strategy_contracts和
reference_manifests，订阅实际路由盘口并据此计算预算及生成各自预期合约。
contracts继续表示角色表，未修改因子优先规则、策略算法或任何运行账本。

旧的im-if角色表摘要不再用于生成配置；缺因子及跨读取同角色合约变化闭闸。
仅有失败预检、没有检查点的配置可在原状态锁下备份并按原文件名重建；已有
检查点不自动替换。新增shared-discovery专项3项及IPC/配置29项通过，共享账户
81项回归通过；真实数据库/行情/柜台未连接，新的现场观察及预检仍待Linux执行。
证据：doc/evidence/live/GAP10_STRATEGY_REFERENCE_DISCOVERY_20261016_REV32.json。
