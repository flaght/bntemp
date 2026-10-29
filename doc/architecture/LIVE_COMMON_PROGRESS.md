# Live 公共能力提取进度

更新日期：2026-10-29，Asia/Shanghai。范围：01_main_ema、03_calendar_spread、04_cross_section、08_option_vega、10_hybrid_transformer，以及08的共享账户集成入口。

工作项编号沿用此前的整理清单，不是demo编号。实际实施顺序为 **03 常驻 → 02 历史 → 01 生命周期及04检查点**。“完成”表示约定提取范围已有代码和本地离线回归结果；原生引擎及真实数据库／柜台验收单独列出。

## 当前状态

| 工作项 | 状态 | 已完成范围与对应实现 |
| --- | --- | --- |
| **01 公共启动、恢复与停机流程** | **已实现，离线通过** | [live/lifecycle.py](../../bomber/framework/trader/runtime/live/lifecycle.py)统一准备、就绪／历史核验、检查点提交、授权及收尾；[local.py](../../bomber/framework/trader/runtime/live/local.py)、[ctp_recovery.py](../../bomber/framework/trader/runtime/live/channels/ctp_recovery.py)及共享宿主接入。03／08重复启动、01／04重复Recording收尾已移除；10模型预热前移准备阶段 |
| **02 历史预热、缺口与历史／实时衔接** | **已实现，离线通过** | [live/history.py](../../bomber/framework/trader/runtime/live/history.py)提供交易分钟窗口、同步覆盖、缓冲和放行；01／03／04共用衔接，03／04共用同步加载，10共用分钟选择。继续复用原HistoryService，保留各策略价格和模型口径 |
| **03 run-forever与交易窗口边界** | **首批迁移完成，08高层接入待统一** | [live/continuous.py](../../bomber/framework/trader/runtime/live/continuous.py)提供公共run_continuous与窗口上下文；01／10已迁移并删除各自continuous.py。08已有公共ContinuousPositionRuntime常驻链路，尚未接入新增高层入口；03／04本轮未新增常驻模式 |
| **04 检查点接入与运行一致性** | **已实现，离线通过** | [persistence.py](../../bomber/framework/trader/persistence.py)补充组件协议、接入对象核验、稳定目标核验及事务元数据。Live提交前核对目标／组合版本与路由贡献，复用原CAS、原子保存、恢复回滚及commit_restore，未改变JSON schema |
| **05 可配置模块式预检** | **设计方向已确认，未实现；优先级5** | 拟按local、exclusive、reference、account、recovery划分，启动配置指定required／optional；复用既有校验、查询和资源，不新增策略专属预检脚本 |
| **06 小范围配置／日志重复清理** | **待处理** | Hybrid配置定义及03／04相似进度日志等小范围重复，尚未作为独立工作项实施 |

详细接口和文件对应：[公共生命周期](../functions/trader/LiveSessionLifecycle.md)、[历史协调](../functions/trader/HistoryFrameLoader.md)、[常驻入口](../functions/trader/run_continuous.md)、[检查点接入](../functions/trader/RuntimeStateManager.md)。

## 五个策略的接入边界

| 策略 | 当前接入及保留职责 |
| --- | --- |
| 01 EMA | 公共生命周期、历史缓冲／放行和常驻入口；EMA精确断点、复权口径、收盘分钟保留在[history.py](../../demos/01_main_ema/history.py)和[recovery.py](../../demos/01_main_ema/recovery.py) |
| 03 Calendar | 公共启动／恢复／停机、同步历史覆盖和缓冲；[recovery.py](../../demos/03_calendar_spread/recovery.py)保留双腿方向、手数和验收，[history.py](../../demos/03_calendar_spread/history.py)保留原始价差预热 |
| 04 Cross section | 公共生命周期、检查点核验和同步历史覆盖；[recovery.py](../../demos/04_cross_section/recovery.py)保留组合业务校验，[history.py](../../demos/04_cross_section/history.py)保留共同日历及历史因子一致性 |
| 08 Vega | [recovery.py](../../demos/08_option_vega/recovery.py)接入公共独立会话流程；[共享宿主](../../bomber/framework/trader/runtime/live/shared.py)延迟到策略恢复及准备成功后提交暂存账户状态。选约、期权／指数输入、原仓归属和进程停机顺序保留；没有同类行情历史预热链路 |
| 10 Hybrid | [run_live.py](../../demos/10_hybrid_transformer/run_live.py)接入公共生命周期与常驻；模型预热在提交／授权前执行。模型身份、原始字段、批次期限和换约约束仍由业务组件校验 |

08的`--run-forever`早已存在于[单策略快捷入口](../../scripts/integration/options/shared.py)和[多策略／多进程入口](../../scripts/integration/shared_account/continuous.py)，二者使用同一个公共ContinuousPositionRuntime。待统一的是新增高层run_continuous的接入，不是新增常驻功能，也不需要在08目录另写continuous.py。

## 已验证结果

以下为本会话已执行的最近一轮结果，Python 3.12，本地离线；本次文档更新未重新运行测试。各组有重叠，不累计为唯一测试数量。

| 验证入口 | 结果 | 重点范围 |
| --- | --- | --- |
| [tests.run_live_lifecycle_offline](../../tests/run_live_lifecycle_offline.py) | **309项通过** | 生命周期、真实JSON代次、对象绑定、目标／组合版本、暂存提交、失败保留原文件、08装配／共享账户／跨日、原错误与停机错误同时保留、10历史字段失败先于提交 |
| [tests.run_live_history_offline](../../tests/run_live_history_offline.py) | **82项通过** | 交易分钟、同步覆盖、去重冲突、缓冲并发、缺口、停止／失败不放行，以及各策略历史接线 |
| [tests.run_live_continuous_offline](../../tests/run_live_continuous_offline.py) | **91项通过** | 休市与跨日、窗口暂停、停止信号、当日报单额度、组件窗口续接及故障不盲目重试 |

复现命令，在pro根目录执行：

```bash
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_lifecycle_offline
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_history_offline
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_continuous_offline
```

离线入口复用项目既有SDK实体及外部传输夹具，实际Python Runtime、状态管理器、账户核心和策略流程参与执行；不构造或代替原生EMA运算。

## 开发待办

| 工作项 | 剩余内容 |
| --- | --- |
| 03的08高层接入 | 保留原仓首次导入、共享账户宿主及server／worker顺序，评估并统一到新增run_continuous入口；目前原常驻链路继续保留 |
| 05模块式预检，优先级保持5 | 确定最小启动配置，按依赖阶段复用已有校验与资源；不重复登录、查库或提前释放独占锁 |
| 06配置／日志小范围清理 | 先核对外部兼容引用，再整理重复，避免扩大为新的大模块 |

该表记录剩余范围，不代表新的执行顺序或本次继续实施；未重新调整此前优先级。

## 验收进展与待办

**最新结果：用户反馈本轮远端集中离线验收“全部选定离线模块通过”。离线回归阶段已通过，下一步为真实柜台只读、在线Recording及受控订单验收。** 下列排障过程保留为历史记录，其中“待复验”描述为当时状态。

远端首轮集中验收在`shared-mother-probe`停止：单母单探针调用常规预算轮询，1单额度用尽后母单先被撤销，导致未捕获。已调整专用探针等待循环，保留Driver硬限制及账户／行情健康检查；本地该组17项通过，包含延迟受理和部分成交。远端须同步修复后复验，完整远端验收仍未完成。

远端后续反馈`shared-ipc`49项、`shared-discovery`4项通过，随后在`ctp-depth-health`停止。原因是提取实际深度回调的测试Handler遗漏初始化`_depth_handlers`；真实Feed构造器已有该初始化。已补齐测试装配，并在成功发布用例中断言无错误日志；本地该组8项通过，待远端复验。

`cross-live`远端复验已通过：83项，测试耗时0.886秒，模块耗时2.145秒。此前DCE／CZCE平今优先记账用例因简化回报缺少`client_order_id`报错，现已改用标准`ExecutionReport`，同时核验原今昨仓、盈亏及费用审计订单ID。本次日志确认该选定组通过；完整集中离线验收的通过结果仍待确认。

`live-common`远端复验已通过：129项，测试耗时0.855秒，模块耗时0.975秒。此前EMA恢复测试在`sys.modules`补丁内加载整个策略模块，退出补丁后卸载新导入的NumPy，下一项再次导入其原生扩展时报错。已改为直接加载该组核验的两个实际恢复函数；策略和运行时代码未变。本地该组129项及公共生命周期汇总309项也通过，两组有重叠。完整集中离线验收的通过结果仍待确认。

最新远端验收在`history-provider`停止：历史后行情失效已被公共生命周期正确拒绝，但测试仍匹配旧错误文本。已更新为公共闭闸提示，并补充实时决策、下单授权均保持关闭的断言。未改变运行行为。本地公共历史82项及生命周期21项通过；依赖原生EMA的完整41项待远端复验。

本轮根据GAP-03远端日志执行[集中排查](LIVE_COMMON_ACCEPTANCE_AUDIT.md)：修复失效的行情工厂测试替换点、收集预期参数拒绝日志，并增加`--keep-going`及4项验收入口回归。现共48组，本机17组直接通过、13组使用既有SDK夹具通过，18组因原生引擎／夹具接口／socket权限限制仍须远端验证。GAP-03的22项通过。远端下一轮使用`python -u -m tests.run_main_ema_acceptance --keep-going`，一次收齐失败组；不再逐组遇错即停后反复同步。

- 远端集中离线回归已收到通过反馈；测试数量不按重叠组累加。
- 在目标运行机验证真实DolphinDB读取、历史落库与实时衔接，以及SimNow午休／跨日／同文件恢复／正常停机。
- 同步部署时包含公共Live文件和相应应用依赖，移除01／10旧continuous.py；保留原检查点及已有策略约束。

本专项没有据此关闭其他GAP或宣称完成真实交易验收。2026-09-22的[项目进度快照](PROJECT_PROGRESS.md)及旧交接继续作为历史证据保留。

[项目文档导航](../README.md) · [架构目录](README.md) · [函数索引](../functions/README.md)
