# 同账户多策略 SimNow 宿主

首期实现：一个进程内运行多个01主力EMA／04横截面策略实例，保留各自Runner、
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

本入口是同进程宿主，不提供独立策略进程的RPC接入。跨主机fencing、CTP＋BN联合
剩余范围及真实两策略并行SimNow成交仍单独验收，不能由本期离线结果关闭GAP10。
