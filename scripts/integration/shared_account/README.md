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
订单已结束且柜台全量仓位／今昨桶与账户文件一致。每个策略延续账户记录的目标版本，
指标独立重新预热，旧目标不会自动重发。模型文件、旧单策略原生文件不能直接接管。
未决发送、未知／漏成交、活动订单断点与跨日恢复保持闭闸，须先权威对账。

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
