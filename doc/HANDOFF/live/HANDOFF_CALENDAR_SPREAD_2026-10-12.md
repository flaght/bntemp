# 03跨期Live实现交接（PF2026101209）

更新：2026-10-13，Asia/Shanghai。当前PF1303_CALENDAR／PF1209_REV6补目标变化撤旧单，
阶段控制器10项已在远端OK（0.012秒），修复后26项Live组远端OK（0.641秒），
包含真实无因子资料的原生生命周期snapshot回归；报告目录
calendar-spread-acceptance-1791858789863169356。此结果为离线回归，
带仓SimNow恢复及修复后干净停机仍待在线报告。
此前REV3用户远端22项全部OK（0.330秒、报告passed），
selection通过。实际CU资料/MD已取得，
Recording首次时效失败后复验通过：5个完整双腿帧、1次目标、阶段passed；
在线Model首轮300秒预热不足后，原文件恢复复验passed：14帧、8次目标、8模型
成交，最终双腿/在途/未归属归零。SimNow已实际双腿开仓并对账，但停机摘要因
缺factor_date_basis属性失败。随后原文件带仓恢复成功（generation=8），快照修复
及干净停机已验证；cu2701平今1手成交109590，cu2612平仓未成交，最新目标
双腿0未完成，因此整体验收仍failed。当前归属/执行/柜台均为cu2612多1、cu2701零，
活动单/在途/未归属0，cleanup_errors为空；继续必须沿用原SimNow状态文件。
再下一轮generation=19恢复，最终双腿均已归零，停机无错；但验收前cu2701仍有
旧卖单working=-1，与零目标冲突，报告仍failed。REV6仅03装配在目标变化时
撤销旧策略订单，等待柜台回报释放在途再由新Tick推进；公共源码不改。新增真实
原生撤单/晚到成交回归，27项Live组已在Linux全部OK（0.803秒），报告
calendar-spread-acceptance-1791869615627255753，在线修复仍待验。
本机遵循协作文档的源码工作限制，没有执行Python用例、回测、编译或网络连接，
没有报单、撤单、操作账户或写入策略交易状态文件。

## 实现范围

03新增固定交易日、固定双腿Live入口，支持Recording、Model和受控SimNow，
两入口复用CalendarSpreadStrategy / CalendarSpreadSignal。默认secondary / far，
即次主力second减far的真实未复权收盘价；没有recent腿、第三腿或因子查询。
角色资料严格来自参考交易日之前，角色/条款的available_ns和来源日时效仍由
公共LiveFuturesReferences核验，行情线程只读LiveReferenceCache。

公共积木保持原实现：显式MD/TD渠道、分钟聚合、Runner、执行builder、归属、
今昨仓账本、风控、持久化与管理生命周期，没有跨demo生产导入。
CalendarReferences只是将公共资料快照投影为本策略配置的两个角色。

Recording仅连接MD，不构造TD、不查询物理仓位、不产生成交；Model用当前参考价
产生明确model来源的标准成交。SimNow仅Broker9999并要求双授权和独立状态文件，
默认strict核验管理双腿空仓，范围外仓位保留。observe/off政策可显式配置；
策略仓位始终来源于归属成交，不将物理账户的未知仓位归给策略。

分钟缓冲按时间戳收齐两腿后发布，缺腿不补价，重复或已消费分钟不重复推进。
启动跳过不完整分钟。完整帧提交前同时登记两腿参考价；执行请求使用实际墙钟，
signal_bar_ns保留收盘分钟。Replay复用公共接收时间聚合，原始深度时间与MD日
仍参与健康检查，参考资料继续按本次参考交易日和墙钟核验。
保留目标由新鲜真实Tick推进；原生反手由公共CTP Planner先平旧方向再开新方向，
两腿不保证原子成交。首腿拒单、回报冲突或渠道失效后停止后续风险请求。

会话不跨交易日自动换约。角色或条款变化、MD/TD日期变化时闭闸停止，停机
保留已成交仓位，原生停机使用公共撤单/核验流程。固定会话不能恢复到新合约或
新交易日；跨日带仓迁移和自动动态换约尚未实现，不以本次入口作为此能力证据。

## 恢复及共享策略影响

SessionIdentityCheckpoint保存模式、交易日、行情环境/原始MD日、品种、角色、
合约、参考内容指纹、策略参数和执行政策，恢复必须相同。不兼容旧01/04/08文件。
公共恢复核验归属、完成订单和目标版本；原生还核验同日CTP账本及strict今昨仓。
失败恢复不覆盖原文件。

恢复的窗口、Bar缓存清空，旧目标不由Tick续发；从恢复目标读取上一方向以保持
阈值中间区间的规则。lookback个新同步价差之后，第一个有效新信号强制发布完整
目标，即使方向不变或变为零；目标revision延续，实际成交仓位保留。
恢复前已有挂单仍按公共契约拒绝，需要先核验结束，不能盲目重发。

共享03/strategy.py的实际变更：

- 包导入支持Live的模块入口，同时保留原脚本的顶层导入。
- 换约敞口检查由account_position / working_quantity改为position /
  strategy_working_quantity，符合已确立的策略归属政策。回测单策略独占时结果应一致。
- 新增恢复预热钩子，默认未调用时行为不变；on_order / on_fill仅统计回调。
- 价差窗口、阈值、双腿手数、调仓规则与历史换约流程保持。

修改前已在当前对话通知上述影响，登记于协作文档。框架生产文件未修改。

## 文件及部署

新增03/run_live.py、live_runtime.py、live_references.py、recovery.py；修改strategy.py
与README。新增tests/test_calendar_spread_live.py、run_calendar_spread_acceptance.py
及tests/calendar_spread_live.sha256；新增本文，更新架构、交接索引与协作登记。
原始共享策略保存在本机/tmp/calendar-spread-before-20261012/strategy.py。
pro不是Git checkout，回退须保留现有运行文件并恢复对应源码，不能默认git revert。

REV2独立验收入口用标准库在导入SDK和测试前核验内嵌EXPECTED_SHA256，
记录运行目录、Python路径、入口自身SHA、分组日志与summary.json，
不依赖远端doc目录或外置.sha256文件。原外置摘要保留为初版历史材料。
内嵌摘要保留原03/公共源码基线、假TdApi，增加3项标准库回归测试的摘要；
入口自身SHA只记录运行审计，避免内嵌摘要自引用。
源码变化后需作为新修订审阅更新摘要，不能删除不符项绕过校验。

在Linux uv-nautilus的pro目录执行：

```bash
python -u -m tests.run_calendar_spread_acceptance
```

默认执行entry、live与selection三组。entry静态计数3项标准库回归，覆盖无外置
摘要、真实源码缺失与内容混版诊断。live静态计数22个方法，涵盖共享信号、归属Model
双腿成交/反转、Recording无成交、乱序分钟/缺腿/重复/未来、参考失效、恢复首个
零目标/版本、改变参数保留原文件、双腿深度缺失、原生假回报/拒单和CLI授权。
该数量是源码计数，不是通过结果。selection继续执行原六个手工断言函数。

可选复核公共基线（公共源码未改，不要求重复此前已通过的公共组）：

```bash
python -u -m tests.run_calendar_spread_acceptance --only public
```

结果文件在tests/results/calendar-spread-acceptance-<ns>/，passed只指本次实际
运行的离线夹具。初版交付时没有原生导入成功或测试通过证据；后续结果见下方
REV3记录，不借用其他策略旧结果。

## 在线验收

完整启动参数、环境与报告解释见[03 README](../../../demos/03_calendar_spread/README.md#4-live版本)。
先Recording检查两个角色和同步分钟，再Model及同文件resume，最后在明确授权
的SimNow账户上核验真实报单、部分成交、平今/平昨与停机撤单。
默认passed要求至少lookback+1个同步帧；需要本次成交证据时额外加--require-fills。
Recording不接受此参数。未加此参数的passed不表示产生目标或成交。
Model参考价成交不模拟费用、滑点和盘口队列，不作为CTP真实成交证据。

远端原生离线、实际DolphinDB资料、CTP真实MD、Model同文件恢复、SimNow成交
与跨任务源码同步均待验。文档日期和样例合约不代表本次柜台实际结果。

## REV2：远端缺外置摘要的预检修复

用户首次在Linux uv-nautilus执行原命令，check_sources在CHECKSUMS.read_text处
抛出FileNotFoundError，缺文件为tests/calendar_spread_live.sha256。这发生在原生
测试导入之前；没有测试数、成交或通过报告，不能视为策略执行失败或成功。

改验收入口为内嵌摘要，并新增tests/test_calendar_spread_acceptance.py。
保持实际生产源码及原22个Live测试不变，不跳过版本校验或删除摘要不符项。
已同步初版其余代码的运行机只需更新以下两个文件，再执行原命令：

```text
tests/run_calendar_spread_acceptance.py
tests/test_calendar_spread_acceptance.py
```

默认首组entry不导入SDK。源码预检若仍发现缺文件或混版，明确列出expected /
actual，按实际清单补同步；此修复不宣称运行机其余191个源码文件均已同步。
REV2完整文件与摘要见PF2026101209_REV2_FILES.txt / PF2026101209_REV2_SHA256.txt，
历史PF1209清单不改写。本机仍仅静态审阅及SHA核验，没有Python、测试或编译；
3项新回归及原生/在线验收继续待运行。

## REV3：Linux夹具错误修复

用户提供live组输出：22项，0.229秒，FAILED(errors=3)；selection输出
calendar selection and signal: OK。其余19项没有失败或错误；entry组输出、
报告目录和入口摘要未提供，不补造对应结果。

三个错误均来自新增测试夹具：两个原生CTP用例没有给启用报单的Driver传入
必需的disconnect_handler，构造即失败；未来发布时间负例实际抛出
SectorDataUnavailable（LookupError），用例错误地预期RuntimeError。
公共代码按既有契约拒绝配置不全和未发布资料，没有生产代码修复或放宽门控。

修正tests/test_calendar_spread_live.py：假Driver配置真实闭闸回调，先建立会话
关联再启动Runner，连接或回报期间断线调用runner.close_role_gate；原成交用例
增加断线后accept_bars=False的断言。发布时间负例精确断言
SectorDataUnavailable及尚未发布信息，过期缓存负例继续按公共缓存RuntimeError
检查。用例仍为22项。验收入口更新为PF2026101209_REV3及新测试SHA，其他
源码摘要保持REV2基线，历史清单不改写。

运行机仅更新tests/test_calendar_spread_live.py及tests/run_calendar_spread_acceptance.py，
复测失败live组即可，无需重复已通过的selection：

```bash
python -u -m tests.run_calendar_spread_acceptance --only live
```

完整新文件摘要为PF2026101209_REV3_SHA256.txt。本机按约定仅静态审阅/SHA，
未运行Python、测试、编译或网络。REV3修复后的原生用例、实际MD、Model同文件
恢复和SimNow成交仍待验，不将源码修复记为测试通过。

## REV3远端Live离线验收通过

用户随后提供Linux uv-nautilus复测输出：live组22项，0.330秒，OK；
独立验收入口报告passed，目录为：

```text
/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/tests/results/calendar-spread-acceptance-1791817491340659580
```

此次结果关闭上述3项夹具错误的复测待验：22项离线组包含真实项目Runner、
Model/Recording、参考缓存、恢复，以及注入假TdApi的订单/成交/拒单/断线链路。
没有真实CTP或DolphinDB连接，不作为柜台成交证据。摘录未包含本次完整命令、
入口SHA或entry组汇总，不补造这些字段；按上轮交付REV3接续记录，源码版本
本身未从本段摘录单独核验。此前selection已经通过，不要求重复。

结构化记录见[离线结果](../../evidence/live/CALENDAR_SPREAD_OFFLINE_20261012.json)。
本次仅更新文档和证据，生产/测试/内嵌摘要不改；无需为文档更新重跑通过组。
独立文档摘要PF2026101209_REV3_EVIDENCE_SHA256.txt，不覆盖历史部署摘要。
实际DolphinDB角色资料、CTP双腿MD、在线Recording、在线Model及同文件恢复、
受控SimNow成交与跨日自动换约仍按各自范围待验/待实现。

## PF2026101210：CU realtime在线验收入口

用户授权按Recording→Model→SimNow顺序，指定CU、realtime，并明确本机不运行。
新增scripts/integration/calendar_spread/acceptance.py：只读取得实际TD，关闭只读
连接后读取无因子的两个角色与条款，使用实际来源日和双腿核验各阶段。依次
装配原03命令，每阶段单独报告和状态文件；Recording必须出现目标，Model和
SimNow必须有归属成交并完成双腿最新目标，SimNow停机活动订单必须为零。
任一失败不启动后续阶段；中断给原入口撤单/持久化时间，超时不强杀伪称已清理。

验收参数显式lookback3、数量1、entry2/exit0.5、调仓1、名义限额1000000、
会话订单上限20，时长Recording600秒/Model900秒/SimNow900秒。生产默认120
窗口、原执行政策和内嵌离线摘要不变，旧22项通过证据保留，不重跑旧组。
新增tests/test_calendar_spread_online_acceptance.py共6项标准库回归，覆盖阶段
顺序、失败阻止推进、授权只给SimNow、缺目标/未完成/无停机核验均拒绝通过。
当前只是源码与静态SHA，本机无Python/测试/编译/联网，在线结果待用户Linux返回。

同步新增的两个Python文件后按03 README的CU命令执行；不需要修改.env凭据或
手工猜夜盘交易日，但运行机必须已经配置同组realtime MD/TD和DolphinDB。
新交付摘要PF2026101210_SHA256.txt，历史PF1209源码/证据清单保留。

## PF1210 REV2：实际CU Recording逐合约时效失败

用户实际报告时间约2026-10-12 23:17:43—23:17:55（由报告时间戳按上海时区换算）。
参考日20261013、来源日20261012、双腿cu2612.SHFE/cu2701.SHFE，DolphinDB角色及
条款ready=True，观察到MD日20261013。Recording不连接TD，因此报告observed_td
仍None，不用预期日补造TD观测。

启动后约12秒，两个深度事件年龄10.033407117秒，接收年龄分别9.004604016 /
9.005893448秒；逐合约阈值10秒，timestamp_regressed=False，没有日期不符或
资料失效证据。全流就绪issues仅包含两个INSTRUMENT_UNHEALTHY，没有RECEIVE
超时。完整帧、目标和成交均0，cleanup_errors空。Recording退出1，阶段控制器
正确拒绝进入Model/SimNow。证据CALENDAR_SPREAD_RECORDING_FAILURE_20261012.json，
原始目录calendar-spread-online/cu-1791818256256683782/recording/runtime/
calendar-spread-recording-1791818263387350940。

当前证据不能判定源端持续更新间隔、成交活跃程度、休市或转换错误，不能据此
改成MID、切主力或放宽全流接收门禁。公共Feed已经在原始深度回调更新接收记录，
其更新不依赖新TradeTick产生；不把缺完整分钟与原始深度失效混为同一事实。

只改阶段控制器与标准库测试：新增透传现有--instrument-max-age-seconds，
默认仍realtime10/replay30，校验有限(0,120]；全流接收10秒独立保持。
acceptance.json保留完整readiness/原failure/Bar计数。新增3项回归，标准库组现9项。
原03生产入口、算法、公共Feed/健康规则及22项原生基线没有改变，后者不要求重跑。
当前本机仅静态/SHA，没有Python、测试、编译或网络执行。

运行机先按03 README的Recording-only命令显式30秒复验，此轮不启动报单。
若仍出现全流接收10秒失败，则定位原始MD接收间隔，不继续扩大阈值；真正取得
完整双腿帧/目标后再接续既定Model→SimNow计划。新摘要PF2026101210_REV2_SHA256.txt，
旧源码摘要及失败证据保留，Recording未通过状态不改写为修复通过。

## PF1210 REV2实际CU Recording复验通过

用户后续报告：只读预检TD=20261013，角色来源日20261012，双腿cu2612.SHFE /
cu2701.SHFE；运行、阶段和总控制器均passed，Recording进程退出0。报告目录：

```text
/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/tests/results/calendar-spread-online/cu-1791818623717906555/recording/runtime/calendar-spread-recording-1791818630876421423
```

真实CTP行情驱动5个完整双腿帧，提交1次目标，z=3.5355339059327413，方向-1，
cu2612=-1、cu2701=+1。cu2612收到9根Bar、cu2701收到5根，4个不完整帧过期；
没有补价或将缺腿分钟当完整帧。停机readiness=True、issues空、cleanup_errors空，
参考指纹与此前失败版相同，未请求因子。Recording所有仓位和成交数仍0，不能
把目标当成交。MD日实测20261013；Recording的observed_td=None符合MD-only，
前面的只读TD值单独列证据，不混用。

当前按此前命令请求逐合约30秒记录；本段报告未单列时效参数值、完整命令或
入口SHA，不补造参数核验。标准库9项测试汇总也未展示，不声明此组通过。
证据CALENDAR_SPREAD_RECORDING_PASS_20261012.json。历史失败证据保持，
本次通过仅覆盖当次真实参考、MD、同步帧和目标链路，不代表所有时段持续可用。

本轮仅更新文档/证据，无生产、测试或摘要入口修改，不需部署/重跑已通过组。
文档证据摘要PF2026101210_REV2_RECORDING_PASS_SHA256.txt，旧摘要不改写。
既定计划下一步直接运行Model，沿用已经核对的日期、双腿和验收参数，使用
不存在的独立模型状态文件、off对账、--require-fills；不重新执行Recording。
Model通过并核验归属仓位达到双腿最新目标后，再进入受控SimNow。原文件保留
用于同日恢复；实际Model恢复及柜台成交仍待验。本机继续无Python/测试/联网。

## 实际Model首轮：300秒同步预热不足

用户执行命令使用--seconds300（此前建议900），其他参数保持lookback3、数量1、
阈值2/0.5、调仓1、逐合约30秒、名义1000000及--require-fills。模型会话启动并
保持readiness=True、issues空、资料ready；期间cu2612 Bar4/cu2701 Bar3，完整帧3，
尚缺评价当前价差所需第4帧，因此last_z=None，目标/模型回报/成交均0，归属仓位0。
停机清理无错误；验收在同步预热检查处失败，不是拒单、资料错误或Model成交故障。

状态文件为demos/03_calendar_spread/results/cu-model-20261012-154511.json，
报告calendar-spread-model-1791819943385721451。保留此文件和失败报告，下一轮
使用原文件加--resume及--seconds900，其余参数保持。不新建文件绕过恢复；
公共恢复允许空目标/零版本的合法检查点，同日、参数/资料身份仍要求一致。
恢复后重新收集窗口，前一轮3个帧不跨进程续用；完整帧并不保证出现入场信号，
成交要求仍保留。Model通过并核验双腿目标实际完成后才进入SimNow。

本次只记录真实结果并补运行说明，不修改代码、验收门槛或交易公式。证据
CALENDAR_SPREAD_MODEL_WARMUP_FAILURE_20261012.json；新独立摘要
PF2026101210_REV2_MODEL_WARMUP_SHA256.txt。原离线/Recording通过结果不重复验证，
本机没有Python、测试、编译或网络操作；模型恢复与SimNow成交仍待运行。

## 2026-10-13 PF2026101301_CALENDAR遗漏检查

用户要求检查遗漏。本机按既定约束静态审阅，确认Model300秒只有3同步帧不是
公式或资料错误；原空目标/零版本检查点可按公共allow_empty规则恢复。上一条
仅说明resume/900秒而未给完整命令，现已在03 README补齐，原文件及所有策略/
风险参数保持，午夜日历日期不改变已核验的TD20261013和角色来源日20261012。

发现并修复验收缺口：直接入口--require-fills原只检查存在成交，单腿成交也可
通过。CalendarControl.verify现要求完整最新双腿目标、两个归属仓位均达到目标，
执行在途量及未归属仓位均零；summary提供working_quantities/unassigned_positions，
阶段review也检查这两个完整字段。仅收紧显式成交验收，默认Recording、下单
授权、信号、净仓规划、今昨仓、风险额度与状态文件格式均不改。修改前在当前
对话通知了跨模式验收影响；没有改框架生产文件，不需另一demo适配。

3项新原生回归补空检查点恢复、单腿成交、目标匹配但在途未归零；Live组25项。
标准库增加1项工作/未归属检查，组10项。全部仅源码计数，没有本机执行；安排
Linux只复验live与标准库阶段组，新结果不得借用旧22项通过。旧Recording无需
重复，Model原文件无迁移，可在新回归通过后按README完整--resume命令复验。

实际源码清单：demos/03_calendar_spread/{recovery.py,run_live.py}、
tests/{test_calendar_spread_live.py,run_calendar_spread_acceptance.py}、
scripts/integration/calendar_spread/acceptance.py、tests/test_calendar_spread_online_acceptance.py。
独立入口内嵌仅更新受影响3文件摘要并标PF1209_REV4，其他公共基线不改。
新整体交付PF2026101301_CALENDAR_FILES.txt / PF2026101301_CALENDAR_SHA256.txt，
使用Calendar专属后缀避开并行工作同号清单，历史摘要与证据保留。
跨日自动换约仍未实现；Model/同文件恢复/SimNow实际成交仍待验，不因本次静态
检查宣布线上完成。本机无Python、测试、编译、网络或账户操作。

## PF1301_CALENDAR阶段控制器回归通过

用户提供tests.test_calendar_spread_online_acceptance汇总：10项、0.012秒、OK。
该标准库组覆盖阶段推进、失败阻止后续、授权只交SimNow、缺目标/双腿未完成/
在途或未归属量不为零拒绝通过、时效参数透传及停机核验。此次摘录未包含前面
25项Live组汇总、实际执行命令或报告目录，不推断对应结果；不将标准库测试
当成在线Model或柜台成交通过。已通过10项无需重复。

本轮只登记文档与CALENDAR_SPREAD_STAGE_OFFLINE_20261013.json，代码与内嵌摘要
保持PF1301_CALENDAR/PF1209_REV4，不需部署。独立文档证据SHA为
PF2026101301_CALENDAR_STAGE_PASS_SHA256.txt，历史部署清单不改写。
25项Live组结果确认后，继续README原文件resume/900秒Model；Recording无需重复，
Model成交和恢复、SimNow成交仍待验。本机没有Python/测试/编译或联网操作。

## 2026-10-13 Model原文件恢复与模型成交通过

用户实际报告status=passed、initial_position_mode=resume、resume_ready=True，
原文件cu-model-20261012-154511.json保持。14个同步帧、8次目标提交、16个标准
模型回报（ACCEPTED/FILLED）、8次归属成交。可见revision3将cu2701由-1先平再
开到+1，cu2612由+1先平再开到-1，z=4.9497474683；revision8在z=0时双腿平仓。
最终latest targets、策略归属仓位、模型执行仓位、在途量及未归属仓位全0，
readiness正常，portfolio_failure=None、cleanup_errors空，pending/expired均0。
参考指纹与前次一致，MD20261013，当前源码新增工作/归属字段已在实际报告出现。

报告目录：demos/03_calendar_spread/results/calendar-spread-model-1791855106176839622。
证据CALENDAR_SPREAD_MODEL_RESUME_PASS_20261013.json。该结果完成原空仓/空目标
文件的同日恢复、实时行情重新预热、模型归属成交、反手及清仓验收，不能扩展为
带非零仓位/非零旧目标的在线恢复通过；那一范围只有此前离线用例，实际仍待验。
模型回报execution_origin=model、account_positions=None，不是CTP柜台成交。
25项Live组汇总、实际完整命令及入口SHA未出现在本段，不补造对应结果。

本次仅更新文档/证据，无代码或摘要入口改动，不需部署、重复Recording或模型
通过运行；原模型文件保留。独立证据摘要
PF2026101301_CALENDAR_MODEL_PASS_SHA256.txt，历史部署摘要与失败证据保留。
按照用户已授权顺序下一步受控SimNow，使用独立不存在的cu-simnow状态文件，
strict核验管理双腿空仓、活动订单、CNY资金及实际TD=20261013。柜台不满足就绪
条件时拒绝启动，不将模型仓位或状态用于柜台接管。具体命令见03 README。
本机无Python、测试、编译或联网，柜台成交、带仓恢复与跨日换约仍待验/待实现。

## PF2026101302_CALENDAR：真实SimNow成交及报告兼容修复

用户SimNow日志：entry z=-2.4494897428，cu2612 BUY OPEN 1手109970，
cu2701 SELL OPEN 1手109460，均有原生CTP订单、交易所委托号及TradeID的FILLED。
14完整帧、6次目标、2次归属成交；最终最新目标、归属、执行及strict柜台净仓
均cu2612=+1/cu2701=-1，在途/未归属0，final_active_orders0，行情就绪，
portfolio_failure=None。范围外AG空1/SC多1/NI多1/I空1保留，不属于本策略目标。
原状态文件cu-simnow-20261013-015114.json，报告calendar-spread-simnow-1791856284618016959。

运行最终failed的唯一cleanup_error是CalendarReferences缺factor_date_basis。
静态定位：原生CtpSessionLifecycle.snapshot无条件读取此报告属性，03投影仅定义
factor_date=None；CalendarControl本想在super.snapshot后移除因子字段，却在
父层读取时先异常。这个异常发生在报告阶段，日志已明确活动订单0且仓位对账
一致；不能把Runtime的“停机查询/撤单未完成”通用错误当作实际有活动单，也
不能把failed旧报告改为passed。保留真实开仓及失败证据。

修复仅03投影增加factor_date_basis=None兼容原生契约，并在03报告中剔除两种
未使用的因子字段；仍factor_roles=()，不引入因子查询。没有改公共框架、交易
公式、限额、权限或状态格式。旧文件无需迁移，仍持多1/空1，不另开新文件、
不自动清仓或接管未知仓位。修改前在当前对话说明范围。

新增真实CalendarReferences+真实CalendarCtpLifecycle.snapshot的无网络回归，
覆盖装配失败session=None及有session的报告，确保无因子字段且manifest明确
adjustment_factors_used=False。Live组静态计数26项，未在本机运行；独立入口
更新PF1209_REV5及3个受影响摘要。预检另发现共享trading_sessions.py已有并行
更新；静态审阅确认03及其公共执行/输入链路不调用该模块，其模块定义不在导入
时实例化日历，因此此次兼容补丁无日历行为变化。保持该文件版本预检并将摘要
接续到当前共享基线（SHA5defb3c3...），同步时一并核对。没有修改共享模块源码。
阶段10项代码未变，不需重跑。同步以下5个源码文件，在Linux先仅复测live组：

```text
demos/03_calendar_spread/live_references.py
demos/03_calendar_spread/recovery.py
tests/test_calendar_spread_live.py
tests/run_calendar_spread_acceptance.py
bomber/framework/trader/runtime/trading_sessions.py
```

```bash
python -u -m tests.run_calendar_spread_acceptance --only live
```

通过后按03 README使用原cu-simnow-20261013-015114.json、--resume和所有原参数
续跑，执行公共同日归属、今昨仓、无活动单及CNY资金核验。首次新目标仍等待
重新预热；本次--require-fills仍要求新成交及完整双腿目标完成。若指标保持原
方向而没有新成交，不降低验收门槛或强制交易。现有仓位不是错误也不提前归零。
跨日/参数/角色变化会拒绝恢复，不修改旧文件绕过。

证据CALENDAR_SPREAD_SIMNOW_SNAPSHOT_FAILURE_20261013.json；独立源码/文档清单
PF2026101302_CALENDAR_FILES.txt及SHA256.txt，使用Calendar后缀避免并行工作编号
冲突。历史失败报告和摘要保留。本机无Python、测试、编译、网络或账户操作；
报告修复后的干净停机、带仓恢复和实际平今/平昨仍待验。
