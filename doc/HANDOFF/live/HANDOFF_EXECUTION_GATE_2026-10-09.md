# PF2026100904 受控客户端闭闸原因诊断

最新PF2026100906已确认并修复真实资金查询/目标并发，见 [并发修复交接](HANDOFF_ACCOUNT_QUERY_RACE_2026-10-09.md)。此文保留PF0904诊断版本记录；新版不再将正常进行中的资金查询直接视为永久闭闸。

日期：2026-10-09。状态：诊断版 controlled／entry 远程回归通过，待真实受控重试和闭闸原因实证。本机只修改和静态核对源码、文档、摘要，没有运行 Python、测试、编译或联网。公共模块修改前已在当前对话说明仅增加诊断，保持现有闸门行为。

## 本次柜台反馈

用户提供 `simnow-1791509261596693303` 的失败日志。模式 simnow，当前TD／MD20261009、cu2611.SHFE、资料来源2026-10-08；已接管空1手，PositionDate1、PositionCost543900.0。第5根Bar产生目标时，在 ControlledLiveExecutionClient.submit_targets 首道门控抛“P3客户端处于只读模式或DEMO环境未核实”。最终status=failed、报单0、订单回调0、成交0、策略／账户均-1、未归属0、权威多0空1、活动订单0、清理错误空。状态文件为用户日志中的 `demos/01_main_ema/results/gap01-cu-20261009-012740-86497.json`；保留失败证据，不删除或复用以掩盖失败。

旧报错合并三项条件：_armed为false、demo_environment_check失败／抛错、_heartbeat_in_flight为true。摘要未包含授权审计，不能确认实际触发分支。生命周期每10秒发起资金查询，查询期间有分钟目标进入时也会产生该报错，这是代码中可确定的同类触发路径；尚不能把它作为本次真实原因。

## 本次修改及边界

- controlled.py：闭闸前捕获 armed、demo_verified、account_query_in_flight、reconciled、会话核验异常类型，写入 ORDER_BLOCKED 审计及 PermissionError。保留原错误前缀，三个条件和撤销授权规则不变。
- runtime/ctp.py：summary.json 写入最后30条结构化 execution_audit，包含时间、动作和详情，以区分授权、撤销、断线、资金查询和报单阻止。报告在停机后采集；ORDER_BLOCKED 的 armed 是阻止当时的值，不能用停机后的授权状态反推先前状态。
- 集中来源摘要纳入 controlled.py 和相关P3测试；controlled／entry 验证三种阻止条件、核验抛错、资金查询内目标重入，且检查无报单和原闭闸行为；entry检查审计保留及截断长度。

本次不把DEMO标志硬置true、不自动重新授权、不删除资金查询门控、不根据不确定原因修改交易行为。即使异步诊断用例通过，也只能证明分支可观测，实际失败根因仍要依据新运行日志。

## 部署与复验

按 [PF2026100904_SHA256.txt](../../evidence/checksums/PF2026100904_SHA256.txt) 同步两个公共模块、集中入口、两份测试及文档，在远程项目根目录执行：

```bash
sha256sum -c doc/evidence/checksums/PF2026100904_SHA256.txt
python -u -m tests.run_main_ema_acceptance --only controlled --only entry
```

若有未明确授权、非DEMO、查询期间目标等分支失败，先修复；不扩展已通过的其余测试。通过后在既有接管／两笔SimNow反手授权范围内使用一个不存在的新状态文件重试 GAP01 第17节命令，入口仍核对当前账户事实。收集包含具体门控字段的异常和execution_audit，依据实际分支修复根因。没有源代码改动就不重复Recording或旧离线模块；门控诊断不是本次柜台成交通过证据。

当前GAP01：离线与当前Recording通过；实际受控成交尝试失败、无单无成交、原空仓保留，待诊断和最终成交核对。跨任务待同步；没有向其他工作对话发送消息。

## 远程两组回归通过

用户提供完整集中输出，报告 `tests/results/main-ema-acceptance-1791511580392948743`，解释器 `/workspace/worker/env/uv-nautilus/bin/python`，项目 `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro`。controlled 的 P3a～P3f 通过，退出0、耗时1.135秒；entry 46个测试全部OK，测试耗时1.697秒、模块退出0、耗时2.957秒。部署核对 runtime/ctp.py 摘要为 `5b37710b540b39d878fd00fc3f7a4ec8a3de7b1057fb503385be887bf233ad59`，entry测试摘要为 `6f13b057b8baaafe7f5e64966f87709790c0ee066ae1649b8a53596a8711c5ea`；与当前交付一致。

新增门控条件和核验异常诊断用例通过，资金查询期间的目标也确实保持阻止且撤销授权，证明原行为未被改成放行。报告审计保留／截断测试通过，假通道完整entry报告已出现DEMO_ARMED、REPORT和DISARMED等结构化记录。假rb2704成交及fake前置不是当前真实CU柜台成交，不能确定上一轮simnow失败实际触发的是哪个条件。

本轮无后续源码修改，不重复已通过组。在有效连续交易时段，以不存在的新状态文件重试既有两笔反手授权命令；若运行接近10:15日盘休市，选择10:30后的连续窗口，避免600秒验证跨休市造成停流。保留失败状态／报告，收集新PermissionError字段和execution_audit，或实际成交及最终权威仓位证据。新状态文件采用GAP01交接第17节的Asia/Shanghai时间戳及进程ID生成，不复用旧文件。

## 诊断版真实重试：跨10:15休市停流

用户提供真实报告simnow-1791511797592893319。报告创建纳秒换算为北京时间2026-10-09 10:09:57，停机审计最后一条为10:15:18；请求600秒，实际跨过上期所CU10:15～10:30休市窗口（[官方交易时间](https://www.shfe.cn/specialtopic/investor/trade/)）。本次显式失败条件为receive_age_seconds=12.199521687超过10秒，TD／MD日期一致、health=READY／reason=HEALTHY、simnow_session=True。较宽的行情健康超时与执行接收时效是不同检查，READY并不能覆盖10秒执行门控。

只形成3根完整Bar，慢线要求5根，未产生策略目标，因此没有复现上一轮首道报单门控；不能以本次查询正常推定上一轮资金查询是确切根因。最后30条审计为周期ACCOUNT_REFRESHED及停机DISARMED risk_mode=HALTED／stop，本次没有ORDER_BLOCKED证据。

最终failed、报单／订单回调／成交均0，策略／账户均-1、未归属0、权威多0空1、活动订单0、清理错误空。状态文件gap01-cu-20261009-020956-86497.json保留。该失败与休市后行情停更一致，不需要修改代码或放宽时效；在10:30后的连续窗口用新状态文件重跑，已通过离线组不重复。当前实际成交和上一轮授权门控分支仍待实证。
