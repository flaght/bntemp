# 同账户多策略 SimNow 宿主

同进程支持01主力EMA／03跨期／04横截面策略实例，保留各自Runner、
角色／完整分钟帧守卫，统一使用一个原生TD、账户锁、CTP开平账本与真实成交归属。
不同策略可以声明同一真实合约；策略算法未修改，公共框架不导入demo。

默认仅做无网络配置检查：

```bash
python -u -m scripts.integration.shared_account.run \
  --config scripts/integration/shared_account/example.json
```

示例日期与资料来源日不是柜台核验结果。运行前复制配置，核对当次TD日、最近
资料日、当前合约及限额，并修改日期。账户凭据继续从项目`.env`取得。
路径相对于配置文件目录解析；账户文件和每个策略的信号文件必须不同。
新会话要求全账户空仓、无活动订单和所有状态文件不存在。

若账户已有其他合约持仓，要保留这些仓位并只运行本次策略的合约，须在会话配置
顶层显式增加`"position_scope": "strategy_instruments"`。管理范围取所有策略实际
执行路由的合约并集（由当次参考资料确定，并非按RB／HC名称模糊匹配）。范围内
仍须总仓为零，包括净仓为零的多空双向仓；全账户仍须无活动订单。
默认或`"position_scope": "account"`继续使用全账户空仓规则。

范围外仓位保留在完整账户净仓快照，并另存多空总仓与今昨桶基线，不分配给新策略，
不进入本次CTP开平账本，也不能由本次Backend报单。启动授权前、定期检查、停机及
同日恢复会核对基线；外部持仓／今昨桶变化、范围外活动订单或查询失败会闭闸。
这不是与其他终端同时交易的许可；账户锁仍按整个物理账户取得。归属总量／名义
预算限定本次管理的策略，账户可用资金仍来自全账户权威查询。范围外名义金额和
组合保证金未新增估值模型。报告保留全账户`final_gross`，另列`position_scope`
中的范围外基线和最终管理总仓。

在项目根目录创建实际会话配置（`-n`保留已有配置，不覆盖）：

```bash
cp -n scripts/integration/shared_account/example.json scripts/integration/shared_account/session.json
```

编辑`session.json`，核对`trading_day`、各策略的`--expected-source-day`、环境及限额。
默认示例是`realtime`；第二套回放须明确配置`replay`和`replay_md_trading_day`。
先检查自己的配置：

```bash
python -u -m scripts.integration.shared_account.run \
  --config scripts/integration/shared_account/session.json
```

配置核对后，显式运行原生SimNow共享报单宿主：

```bash
python -u -m scripts.integration.shared_account.run \
  --config scripts/integration/shared_account/session.json \
  --connect --enable-orders --confirm-simnow
```

遇到`TD交易日与共享配置不一致`时，先取得当次TD只读结果：

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

核对输出的`trading_day`与TD前置。共享入口现在在日期检查之前打印实际TD日、
预期TD日、环境及MD/TD前置，组装失败时也将这些值保存在报告`td_preflight`中。
日历自然日不能替代柜台交易日。只读结果若来自回放环境，须核对整组MD/TD前置，
配置`replay`及独立的原始`replay_md_trading_day`；资料日仍按该TD日的实际资料核对。
不要仅修改一个日期而保留错误的实时／回放语义。

例如柜台已确认TD日为20261014，并且已有持仓均在RB／HC当次执行合约之外，
保留会话配置的其他项，将顶层设置为：

```json
"trading_day": "20261014",
"position_scope": "strategy_instruments"
```

各策略的`--expected-source-day`仍须核对实际资料，不能按TD日期直接推断。
可以先用现有只读参考探针验证配置中当前声明的来源日（以下20261012是待验证值）：

```bash
python -u -m scripts.integration.reference_data.readonly \
  --connect --trading-day 20261014 --products RB HC \
  --expected-source-day 20261012
```

若资料日不符，核对探针报告的实际日与资料入库状态，再修改两个策略参数。
之前在TD日期／空仓预检失败的运行尚未建立账户检查点，不需要因此增加`--resume`。
已有正式共享检查点则必须按原管理范围恢复，不能通过改范围或删除文件接管仓位。

若故障报告及原账户文件failed/pending为true，但本地订单和Driver关联均已结束，
可显式进行同日只读对账修复（不需要有效MD行情，但仍核对数据库参考身份）：

```bash
python -u -m scripts.integration.shared_account.run \
  --config scripts/integration/shared_account/resume_acceptance.json \
  --connect --resume --reconcile-only
```

该模式拒绝enable-orders/confirm-simnow，并在Driver层禁单；不启动策略和MD、不授权。
沿用账户及策略文件锁，核对原检查点身份、同TD、无未结束订单/冻结份额、柜台总仓/
今昨桶/归属及范围外基线、可用资金及无未知活动订单。任一不符保持闭闸，不自动撤销
未知订单或分配外部仓位。全部通过后，先将原账户文件逐字节备份为
`shared-account.json.before-reconcile-g原代数.json`，再使用原RuntimeStateManager保存
暂停检查点：清除已核实的服务failed/pending，未执行贡献冻结到实际归属仓位，原信号
版本保留，各源保持suspended，不重发旧目标。策略文件不改。
报告status=reconciled、verification=checkpoint_reconciled_no_strategy_no_orders，
并记录原/新generation、备份文件、orders_submitted=0、authorized=false。
此模式仅修复已结束订单且全量账本一致的服务故障标记，不重建漏成交或处理跨日。
修复后等RB/HC有效行情时段，按原resume命令重新预热；只读修复通过不是报单验收。

CU／AU例子使用新策略ema-cu-gamma/cross-cu-au-delta及独立状态文件，原配置为
session_cu_au.json。用户已授权上调账户总名义限额；日志成交价CU最高110670、AU
最高900.26及乘数5/1000，对两策略各CU1手、横截面AU1手，名义合计2006960元，
超原200万元。session_cu_au_230w.json仅将账户max_gross_notional上调为2300000，
手数、策略argv、单合约限额、身份和状态路径不变。价格会变化，该上限不是放行保证。

已有CU/AU仓位时，不能改配置后直接resume。先用旧配置与显式新预算配置做只读
迁移（要求同TD、原身份、所有订单/冻结份额已结束和全账户对账一致，不报单）：

```bash
python -u -m scripts.integration.shared_account.run \
  --config scripts/integration/shared_account/session_cu_au.json \
  --connect --resume --reconcile-only \
  --budget-config scripts/integration/shared_account/session_cu_au_230w.json
```

只允许上调账户总名义限额；改路径、策略参数、路由、其他预算或下调均拒绝。
核验并备份后，同一检查点原子更新账户预算和会话身份，归属及信号版本保留，贡献
暂停/冻结不重放；报告记录previous_budget和saved_budget。保存失败恢复内存旧预算/
身份，原文件和备份保留。之后只用新配置恢复，旧预算配置不再匹配保存身份。

迁移报告status=reconciled且orders_submitted=0后，在有效行情期间执行：

```bash
python -u -m scripts.integration.shared_account.run \
  --config scripts/integration/shared_account/session_cu_au_230w.json \
  --connect --enable-orders --confirm-simnow --resume
```

`strategies`使用唯一`strategy_id`、`kind`、独立`state_file`及原入口的业务`argv`。
宿主统一管理mode、授权、TD日、环境、会话时长和恢复，策略参数不能覆盖这些项。
同一种策略可配置多个不同ID。`stop_after_seconds`可独立停止一个策略，其他策略继续。
暂停／停止保留已成交归属量，不自动平仓；涉及该策略的共享母单撤销后，其他策略
用后续新鲜行情重新规划剩余目标。内部净额／划转记在账户归属账本，不能算柜台成交。

账户预算`max_gross_quantity/max_gross_notional`按各策略归属绝对量汇总，反向贡献
不能用账户净额绕过。策略本身的每合约限额仍生效。账户端继续检查原生资金、
行情参考价、实际订单／净仓限额及会话订单数。本期未新增保证金模型或组合Greeks。
贡献携带deadline时，账户汇总保存各源期限，逐笔发送和CTP写前保存之后再次校验；
到期源撤单并冻结在实际归属量。无deadline的原策略目标沿用原意图语义。

恢复使用原配置与全部原文件追加`--resume`。首期支持相同TD日、相同路由与配置、
订单已结束且柜台仓位／今昨桶与账户文件一致；限定范围时同时核对管理账本及
范围外基线。每个策略延续账户记录的目标版本，
指标独立重新预热，旧目标不会自动重发。模型文件、旧单策略原生文件不能直接接管。
未决发送、未知／漏成交、活动订单断点与跨日恢复保持闭闸，须先权威对账。

同日恢复验收可使用`resume_acceptance.json`。它与当前`session.json`使用同一账户
文件、策略文件、策略参数、路由及风控，仅将EMA独立停止时间从300秒延至480秒，
总时长仍600秒，使slow=5的实时预热有机会完成。`seconds`与`stop_after_seconds`
属于宿主调度，未写入账户/策略恢复身份，允许改变；策略`argv`、TD日、环境、
参考条款、账户预算及路由仍按原身份严格核对。该结论有入口和完整Host恢复回归，
不靠修改持久化文件绕过身份检查。

已取得20261014当日原会话检查点、原资料与合约仍一致且全账户无活动订单时，
在Linux有效行情时段执行（会允许SimNow报单）：

```bash
python -u -m scripts.integration.shared_account.run \
  --config scripts/integration/shared_account/resume_acceptance.json \
  --connect --enable-orders --confirm-simnow --resume
```

此文件用于恢复已有会话；不能去掉resume作为有仓新会话使用。保留原状态文件及
历史报告；不要删除文件或通过改TD日实现跨日恢复。本次运行继续遵守原20笔会话
订单上限与预算，不为取得测试成交自动扩大限额，也不强制策略产生信号。
若EMA完成预热后仍未提交目标，双来源实际交易验收保持未完成，不能用运行时长替代。

验收先比较恢复开始时的源版本与仓位：原横截面来源版本6、RB+1/HC-1应被保留，
EMA无旧贡献；重新预热前不重放旧订单。新目标出现后版本必须递增，归属仍按各自
策略记录。480秒停止EMA后横截面应继续运行；最终比较两策略归属合计、管理总仓、
五个范围外持仓及活动订单。保存本次summary.json和events.txt供逐笔核对。

公共宿主提供`stop_strategy`及`restart_strategy`。应用工厂重建本策略Session后，
后者只恢复该策略的信号消费版本，复用原端口和TD，不恢复或替换其他策略账本。
账户完整状态只写账户文件；策略文件记录身份和版本，指标恢复政策为`rewarm`。

离线验证：

```bash
python -u -m tests.run_main_ema_acceptance --only shared-account --only live-common
python -u -m tests.run_cross_section_acceptance --only shared-account \
  --only cross-section --only entry --only attribution \
  --only ctp-autosave --only ctp-recovery --only ctp-driver
```

报告区分`completed`与柜台成交验收，不把正常退出或零成交标成交易通过。
账户回报保存在`events.txt`，摘要包含各策略归属仓位、成交回调计数、源目标、
独立停止状态及最终账户总仓／活动订单数。验收须逐笔核对来源，再比较归属和账户汇总。

策略停止先禁止新目标，保留Runner绑定，按冻结所有者撤单并等待相关母单结束，
消费已入账回报后才解绑策略。等待使用账户原`cancel_seconds`；超时或回调失败会
报告停机未完成，不把未消费队列当作成功退出。用户回调不在柜台线程或账户锁内执行。
报告中每个策略增加`stop_requested_at_ns`、`stopped_at_ns`和`execution_events`。
后者的`fills_enqueued`与`fills_delivered`统计本进程该端口收到/派发的归属FillEvent，
不统计手数；停止成功时两者应相等，`pending_events`和`pending_fills`应为0，
`closed=true`。01 EMA的`fills_received`还应与当前策略实例收到的FillEvent数一致。
若同进程重建策略，端口计数仍累计，策略实例计数从新实例开始，须按重建边界核对。
使用`stopped_at_ns`与另一策略后续订单/成交时间比较，确认独立停止后的继续运行。
保持原账户及策略检查点、原配置，用`--resume`复测，保存新目录的停机摘要和
`events.txt`；无需因本次修复重做预算迁移，也不改变交易日、阈值或归属。

本入口是同进程宿主，不提供独立策略进程的RPC接入。跨主机fencing、CTP＋BN联合
剩余范围及真实两策略并行SimNow成交仍单独验收，不能由本期离线结果关闭GAP10。

共享母单故障专项可单独运行，不连接行情、数据库或柜台，不读取或修改交易状态文件：

```bash
python -u -m tests.run_shared_account_fault_acceptance
```

该入口使用真实账户服务、CTP Driver/状态机/归属/持久化及Runner，传输和SDK类型为
替身。报告写入`tests/results/shared-account-faults-<时间戳>/`，包含`summary.json`、
`tests.txt`及源文件摘要，`verification=controlled_transport_offline`。七个用例是
共享账户测试组的子集，不另加到全组测试数量中。
其中母单5手先成交3手分别归属2/1，重复成交不重复记账；非提交者即使已成交自己
全部份额仍可撤母单，等待剩余份额终结，另一策略仅重新规划差额2手。拒单同时通知
两名冻结所有者，无成交、不产生归属仓位，账户闭闸；拒单后的新贡献不能修改来源
或检查点。其余用例核对停止期间迟到成交、撤单超时及回调失败。
这些受控回报不登记为SimNow实际部分成交、撤单或拒单。真实柜台验证仍须保存完整
原生回报、停止时间、每策略归属及最终总仓/活动订单，不能通过篡改回报或策略账本补证。

真实共享母单撤单的观察式验收：

```bash
python -u -m scripts.integration.shared_account.run \
  --config scripts/integration/shared_account/session_cu_au_230w.json \
  --connect --enable-orders --confirm-simnow --resume \
  --acceptance-cancel-owner ema-cu-gamma
```

保持原配置/策略ID/状态文件，原Runner及算法照常运行。该开关只观察已受理、仍有
未成交量、至少两个冻结所有者的母单，并要求指定策略是非提交者、所有相关策略
仍活动。捕获后提前停止指定策略，复用宿主停止和原生按归属撤单；停止会保留其已
成交归属，不隐式平仓，另一策略继续原目标推进。它不提交额外目标、不替换限价、
不扩大数量或预算；原480秒停止及600秒宿主时长仍生效，指定策略正常停止前未捕获
场景就不进行特殊操作。
摘要增加`shared_cancel_acceptance`，保存母单及冻结计划、捕获/停止时点、撤单报告ID、
同单其他策略活动情况和后续目标/成交计数。`passed`只表示捕获母单有撤单回报、
非提交者停止后其他所有者保持活动且本轮正常结束。`partial_fill_observed`另行记录
是否实际出现部分成交；没有部分成交/拒单的运行不能登记相应验收通过。全部成交
抢先于撤单或没有符合条件母单时为`not_exercised`，不把正常completed当作专项通过。
执行完保存完整summary.json及events.txt供逐笔复核；无需重做预算迁移或清理检查点。

自然信号未能形成双来源在途母单时，可使用独立受控入口；先只读预检：

```bash
python -u -m scripts.integration.shared_account.mother_probe \
  --connect --trading-day 20261014 --product CU
```

默认读取原CU/AU和RB/HC共享账户检查点的known_instruments，排除所有原策略合约，
即使物理净仓为0也不接管；缺检查点拒绝继续。`--protect-state`可显式指定要保护的
原共享检查点（可多个）。同时要求全账户没有活动订单，候选合约无多空总仓，
距到期至少7天。基础条款从DolphinDB两次稳定读取取得，交易日来自TD，盘口来自
原生MD。选择CU未持仓月份合约并输出完整候选列表；可用`--instrument`指定列表中
其他合约进行只读预检。没有新鲜盘口则拒绝，不猜价格。账户锁和writer lease与
原入口相同，先停止其他报单宿主再测试。

预检给出买价低于买一（或卖价高于卖一）的10跳建议限价，检查涨跌停、价格步长、
3秒接收/10秒事件时效和2手总名义预算。默认总名义120万，显式上限不得超过230万。
不带`--submit --confirm-simnow`绝不发单。拿到本次实际合约和价格后，提交须同时
明确`--instrument`和`--limit-price`，再次获取行情并验证价格仍被动且在边界内：

```bash
# 合约和限价使用本次预检结果，不照抄示例数字。
python -u -m scripts.integration.shared_account.mother_probe \
  --connect --trading-day 20261014 --product CU \
  --instrument 实际预检合约 --limit-price 实际预检价格 \
  --submit --confirm-simnow
```

此入口在新`shared-mother-probe-<时间戳>`目录使用独立账户和双策略状态文件，
不启动原EMA/横截面算法，不修改原身份/预算/检查点。两个Observer通过真实共享
端口各提交1手，首轮规划暂缓，二者均写前持久化后仅推进一次，形成2手OPEN限价
母单；Driver最多允许发1张，禁止重规划新单。受理后通过原宿主停止非提交者撤单，
保留对方当时的目标/活动状态作为证据，然后正常结束测试。被动价格不能保证零
成交；最多2手意外成交归独立测试账本，不自动平仓，不改写原账本。出现意外成交
时必须保留本目录account.json，先核对新增总仓及原会话范围外基线，再决定后续
处置，不能删文件后直接恢复旧会话。
报告包含plan、原检查点哈希及unchanged检查、原生events.txt、冻结母单归属、
peer_state_after_owner_cancel和unexpected_fill。正式交易状态持久化仍使用原框架，
不是拼接柜台回报或手工改策略仓位。全部成交抢先撤单为not_exercised，不登记撤单
通过；真实部分成交和拒单按实际回报分别判断。
本机/远端受控装配回归可单独运行`python -u -m tests.run_main_ema_acceptance --only shared-mother-probe`；
该组传输为替身，不能代替此入口在Linux上的只读和SimNow实际验收。

零成交共享母单撤单已有真实回报后，部分成交专项使用同一入口：

```bash
python -u -m scripts.integration.shared_account.mother_probe \
  --connect --trading-day 20261014 --product CU \
  --cancel-after partial --partial-wait-seconds 30 --passive-ticks 0
```

这仍是只读预检，不发单。partial模式允许限价位于本方买一/卖一，仍禁止跨对手价，
仍校验价位网格、涨跌停、行情时效、总名义预算、原检查点及空仓测试合约。盘口
一档数量会出现在计划book_volume，但不保证部分成交。提交时使用新计划明确指定
instrument/limit-price并保留上述partial参数，加submit及confirm-simnow；不增加
母单2手和每源1手，不拆单，不发第二张，不自动平仓。
受理后最多等待30秒（可1至60秒），沿用账户周期权威查询和行情健康检查。观察到
同母单PARTIALLY_FILLED且仍有剩余量才停止非提交者，按原冻结归属撤余单。超时
也撤剩余单，但partial_cancel_acceptance为not_exercised；全成交抢先撤单也不
登记该项通过。只有部分成交原生回报发生在停止请求前、随后取得CANCELED、另一
来源保持活动且本轮正常结束才为passed。挂单期间不再要求已批准的价格持续被动，
价格触及限价属于正常成交过程；仍门控行情日/时效/边界和预算，健康故障正常停机。
Native CANCELED回报filled_quantity为0并不撤回此前成交：它是状态回报，须结合
PARTIALLY_FILLED、最终归属/CTP账本/柜台总仓核对，不能据此把实际仓位清零。
新增成交须保留本测试目录account.json及events.txt；先核对新增测试仓位及原会话
范围外基线，再决定处置，不能删除状态或直接继续原会话。GAP10整体仍开放。

三策略两种模式、本机账户服务/worker和16:00 replay、21:00 realtime验收步骤见[三策略运行](THREE_STRATEGIES.md)。新增代码不等于Linux replay已通过；完整跨期原生组仍须Linux验证。
## 四品种独立进程当前交付（REV26）

CU／AL／AG／AU横截面、原CU EMA与跨期采用一个账户服务及三个独立worker。
完整命令和迁移约束见[当前四品种运行流程](FOUR_PRODUCTS_RUNBOOK_20261014.md)。
同合约已有外部仓保存基线，不进入自己的策略/CTP成交账本。已有原归属、消费
游标及订单编号通过同日只读迁移保留；未来Tick最多等待100毫秒后再发布，超限
仍闭闸。215项离线通过，尚待Linux原生及实际三个策略运行报告。

## REV27：组装前参考缓存刷新（2026-10-15）

V3预检失败于EMA Runner构造：首个策略的缓存被后续输入准备及柜台查询耗时
拖过30秒观测限额，尚未连接MD或报单。公共应用工厂现在在控制线程组装每个
01/03/04之前刷新参考，并核对准备时的合约/条款；刷新失败或声明变化拒绝
继续，原时效及交易门控不放宽，08装配路径不变。
本机shared-ipc 23项通过，含真实三个子进程；源码及证据见
`doc/evidence/checksums/GAP10_PREASSEMBLY_REFERENCE_REFRESH_20261015_REV27_SHA256.txt`
和`doc/evidence/live/GAP10_PREASSEMBLY_REFERENCE_REFRESH_20261015.json`。
同步后只复验shared-ipc及原V3只读preflight，通过后启动原V3服务及三个worker。
不重跑discover/prepare，不改名字、不加resume；本次失败未创建交易检查点。

## REV28：账户服务首个故障诊断（2026-10-15）

服务在protocol.poll触发执行状态闭闸时，停止所有者可能再次触发同一错误。
服务报告现在复用宿主snapshot，在停机前保存failure_execution_diagnostics，
包含service_failed、is_reconciled、report_errors、recovery_required、
portfolio_failure及最近audit；采集失败单独记录，不替换首个故障。
闭闸/撤单/锁保留及账本恢复政策不改变。本机shared-ipc 24项通过。
当前V4根因仍须原shared-account-server-1791995438534311731的summary/events；
不通过重复启动或新增V5会话补造根因证据。
