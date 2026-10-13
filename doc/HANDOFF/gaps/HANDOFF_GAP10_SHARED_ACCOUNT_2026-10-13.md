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
示例日期须运行前重新核对；新会话须全账户空仓、无活动订单和不存在的全部状态文件。

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
单策略文件或有仓账户。原配置/全新文件要求仅用于明确的新会话。

独立策略进程接入本机账户服务的协议/客户端尚未实现；当前分别运行旧demo CLI仍会
争同一账户锁。CTP＋BN联合剩余验收、真实第三方多账户、BN写前/漏成交重建、
跨主机单写保护与通用跨日恢复保持原GAP10范围，未由本期实现或本机通过结果关闭。
