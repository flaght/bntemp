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
