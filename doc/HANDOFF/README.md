# Bomber 交接记录分类索引

2026-10-29公共Live模块：[当前进度与接手事项](../architecture/LIVE_COMMON_PROGRESS.md)。生命周期、历史协调和检查点接入已实现，01／10常驻迁移完成；08已有公共常驻能力，高层接入待统一。最近离线回归309／82／91项通过，原生与真实外部系统验收另列；05模块式预检尚未实现、优先级保持5。

2026-10-14最新08：[共享执行接入与启动跨日恢复](live/HANDOFF_OPTION_VEGA_SHARED_ROLLOVER_2026-10-14.md)。已接入option_vega工厂、指数查询/封帧及选约去重；CFFEX已终结订单可按当日权威今昨仓/成本恢复。原仓快捷入口运行至收盘或主动停止；离线通过，服务器实际恢复仍待核对。

2026-10-13会话专项：[静态Provider／CU真实两源对照及旧日历清理](../data/TRADING_SESSION_GENERALIZATION_2026-10-09.md)已完成；历史源配置统一、可用CU分钟源和正式历史接续仍待完成。当前Replay为实时Bar预热进度，未取得最终summary；详见[证据](../evidence/live/TRADING_SESSIONS_CU_PASS_20261013.json)。

最新Live反馈：[01/04模型运行与04恢复验收通过（2026-10-11）](live/HANDOFF_MODEL_REPLAY_2026-10-11.md)。01恢复、双策略同时运行及第三方真实成交待验收。

策略独立执行：[多账户装配（2026-10-11）](live/HANDOFF_STRATEGY_EXECUTION_2026-10-11.md)。

交接记录按任务主题查找，保留实施过程、版本和验收边界。架构和实现状态的主入口见[项目文档导航](../README.md)；校验清单与补丁见[证据材料](../evidence/README.md)。

同一主题的早期记录、修订和撤回方案均保存在对应主题目录，状态在下表说明。同一主题存在多轮记录时，先读最新专项，再查修订证据。文件名的日期通常是初稿日期，不表示文内最后一次更新；历史账户、合约和交易日仅属于当次运行记录。

## 协作与公共变更登记

| 文档 | 内容和阅读说明 |
| --- | --- |
| [HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07](coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md) | 回测与实盘工作线的公共变更登记、通知流程和 PF 版本记录；后续接手先读。 |
| [HANDOFF_2026-09-23](coordination/HANDOFF_2026-09-23.md) | 9 月 23 日总交接；后续接手优先查新主题交接。 |

## 策略与公共框架

| 文档 | 内容和阅读说明 |
| --- | --- |
| [HANDOFF_DEMOS_2026-10-04](demos/HANDOFF_DEMOS_2026-10-04.md) | 九个 demo 的实现、框架约定、数据配置及初期输入层迁移。 |
| [HANDOFF_DEMOS_FRAMEWORK_2026-10-07](demos/HANDOFF_DEMOS_FRAMEWORK_2026-10-07.md) | 策略与公共框架基线、数据契约及参考源迁移；后续增量继续查实盘与协调记录。 |

## 实盘渠道与策略持仓

| 文档 | 内容和阅读说明 |
| --- | --- |
| [HANDOFF_CALENDAR_SPREAD_2026-10-12](live/HANDOFF_CALENDAR_SPREAD_2026-10-12.md) | CU Recording/Model passed；SimNow实成交cu2612多1/cu2701空1、对账一致/活动单0，但报告缺factor_date_basis失败。PF1302_CALENDAR修复/26项回归及原文件有仓恢复待验；跨日换约未实现。 |
| [HANDOFF_CROSS_SECTION_FOUR_VENUES_2026-10-12](live/HANDOFF_CROSS_SECTION_FOUR_VENUES_2026-10-12.md) | PF2026101208：04支持SHFE/INE/DCE/CZCE源码、郑商所代码、DCE实时日期与通用平仓/今昨仓恢复；远程离线及真实柜台待验。 |
| [HANDOFF_OPTION_VEGA_2026-10-11](live/HANDOFF_OPTION_VEGA_2026-10-11.md) | 08期权Vega共享回测/Live装配、归属仓位、原始估值时间、分钟帧、独立指数与恢复；远程两组43项离线通过，公共组、本次真实行情和柜台待验。 |
| [HANDOFF_MODEL_REPLAY_2026-10-11](live/HANDOFF_MODEL_REPLAY_2026-10-11.md) | 01/04模型在线运行、04同文件恢复的远程passed报告；包含旧仓位/目标版本接续证据、MD报告修正及待验收边界。 |
| [HANDOFF_STRATEGY_EXECUTION_2026-10-11](live/HANDOFF_STRATEGY_EXECUTION_2026-10-11.md) | PF2026101101独立执行账本、账户仓位政策、模型/归属成交来源及多账户公共模块；最新验收见模型专项。 |
| [HANDOFF_LIVE_CHANNELS_2026-10-10](live/HANDOFF_LIVE_CHANNELS_2026-10-10.md) | PF1035 行情与交易渠道显式分层、装配及运行机回归结果。 |
| [HANDOFF_LIVE_POSITION_RECOVERY_2026-10-10](live/HANDOFF_LIVE_POSITION_RECOVERY_2026-10-10.md) | PF1036 Replay 持仓接管、单次同交易日恢复及 04 公共恢复模块。 |
| [HANDOFF_CTP_POSITION_SCOPE_2026-10-10](live/HANDOFF_CTP_POSITION_SCOPE_2026-10-10.md) | PF1037 管理合约范围，04保留范围外RB仓位；本机80项通过，原生及实际柜台待验。 |
| [HANDOFF_LIVE_POSITION_RECOVERY_REV2_EVIDENCE_2026-10-10](live/HANDOFF_LIVE_POSITION_RECOVERY_REV2_EVIDENCE_2026-10-10.md) | PF1036 REV2 的 ema-recovery 9 项 Linux 测试结果；实际柜台恢复另行验收。 |
| [HANDOFF_LIVE_REV3_DEPENDENCIES_2026-10-10](live/HANDOFF_LIVE_REV3_DEPENDENCIES_2026-10-10.md) | PF1036 REV3 补齐部署依赖并同步 Replay Tick 聚合字段。 |
| [HANDOFF_LIVE_REV3_EVIDENCE_2026-10-10](live/HANDOFF_LIVE_REV3_EVIDENCE_2026-10-10.md) | REV3 continuous、live-common 及后续选定组的 Linux 离线结果；柜台接管和恢复未签收。 |
| [HANDOFF_LIVE_COMMON_2026-10-10](live/HANDOFF_LIVE_COMMON_2026-10-10.md) | PF1033/1034 的 01/04 公共 Live 机制抽取、时钟夹具及后续分层说明。 |
| [HANDOFF_LIVE_TRADING_2026-10-07](live/HANDOFF_LIVE_TRADING_2026-10-07.md) | CTP/Binance 接入、主力 EMA 与 04 横截面装配的累计记录；含多轮历史状态。 |
| [HANDOFF_SIMNOW_CU_NIGHT_AND_NEXT_STRATEGY_2026-10-09](live/HANDOFF_SIMNOW_CU_NIGHT_AND_NEXT_STRATEGY_2026-10-09.md) | CU SimNow 实际交易总结与其他策略接手步骤；保留当次账户和成交证据。 |
| [HANDOFF_FRESH_EMA_2026-10-09](live/HANDOFF_FRESH_EMA_2026-10-09.md) | PF0908 全新 EMA 从空仓首次开仓证据，GAP-01 按既定范围关闭。 |
| [HANDOFF_CONTINUOUS_EMA_2026-10-09](live/HANDOFF_CONTINUOUS_EMA_2026-10-09.md) | PF0910 CU EMA 跨休市、日夜盘和重启续接设计；历史来源规则另见 history。 |
| [HANDOFF_ORDER_CALLBACK_2026-10-09](live/HANDOFF_ORDER_CALLBACK_2026-10-09.md) | PF0903 统一 on_order 入口、旧 on_order_update 兼容及回归记录。 |
| [HANDOFF_ACCOUNT_QUERY_RACE_2026-10-09](live/HANDOFF_ACCOUNT_QUERY_RACE_2026-10-09.md) | PF0906 资金查询与目标提交并发修复；无成交运行本身不能证明交易闭环通过。 |
| [HANDOFF_CTP_BINANCE_NEXT](live/HANDOFF_CTP_BINANCE_NEXT.md) | 9 月 25 日两端交易接入交接，保留早期实现及验收状态。 |
| [HANDOFF_CTP_SIMNOW_NEXT](live/HANDOFF_CTP_SIMNOW_NEXT.md) | 9 月 25 日 CTP EMA 交接，原文已指向同期两端交接。 |
| [HANDOFF_SIMNOW_BINANCE_DEMO](live/HANDOFF_SIMNOW_BINANCE_DEMO.md) | 早期模拟盘专项，后续状态查 live 与 GAP-10。 |
| [HANDOFF_EXECUTION_GATE_2026-10-09](live/HANDOFF_EXECUTION_GATE_2026-10-09.md) | PF0904 门控诊断版本；资金查询并发行为被 PF0906 后续修正。 |
| [HANDOFF_FIXED_TARGET_ACCEPTANCE_2026-10-09](live/HANDOFF_FIXED_TARGET_ACCEPTANCE_2026-10-09.md) | PF0907 固定目标验收方案已撤回；替代入口是全新 EMA 空仓验收。 |

## 历史行情预热与恢复

| 文档 | 内容和阅读说明 |
| --- | --- |
| [HANDOFF_HISTORY_STARTUP_POLICY_2026-10-09](history/HANDOFF_HISTORY_STARTUP_POLICY_2026-10-09.md) | 用户补充的盘前文件优先、数据库回退、盘中 Provider 和显式缺失策略。 |
| [HANDOFF_HISTORY_PROVIDER_2026-10-09](history/HANDOFF_HISTORY_PROVIDER_2026-10-09.md) | PF0911 历史工厂、请求、文件与 DolphinDB Provider、EMA 启动恢复实施。 |
| [HANDOFF_HISTORY_PROVIDER_ACCEPTANCE_2026-10-09](history/HANDOFF_HISTORY_PROVIDER_ACCEPTANCE_2026-10-09.md) | PF0911 远程离线回归进度及其覆盖范围。 |
| [HANDOFF_HISTORY_IM_SCHEMA_2026-10-09](history/HANDOFF_HISTORY_IM_SCHEMA_2026-10-09.md) | IM 分钟行情表、字段和成交量语义的初步核对。 |
| [HANDOFF_HISTORY_IM_READONLY_2026-10-09](history/HANDOFF_HISTORY_IM_READONLY_2026-10-09.md) | PF0912 固定 IM 原始分钟查询、时间转换和只读验证步骤。 |
| [HANDOFF_HISTORY_IM_EVIDENCE_2026-10-09](history/HANDOFF_HISTORY_IM_EVIDENCE_2026-10-09.md) | 真实 IM 查询证据、因子和角色映射反馈；不替代实时交易验收。 |
| [HANDOFF_HISTORY_WARMUP_2026-10-09](history/HANDOFF_HISTORY_WARMUP_2026-10-09.md) | PF0913 历史因子处理、固定 IM 的 240 分钟窗口与允许缺失预热。 |
| [HANDOFF_HISTORY_RESUME_2026-10-09](history/HANDOFF_HISTORY_RESUME_2026-10-09.md) | PF0914 指标文件恢复与首根新事件验证；修正 EMA double 恢复语义。 |
| [HANDOFF_HISTORY_RESTART_2026-10-09](history/HANDOFF_HISTORY_RESTART_2026-10-09.md) | PF0915 两次独立进程的指标停机重启探针；与账户归属恢复分别验收。 |
| [HANDOFF_IM_RUNTIME_2026-10-09](history/HANDOFF_IM_RUNTIME_2026-10-09.md) | PF0916 正式 IM Recording 历史与实时分钟接续，首期不开放 CFFEX 报单。 |
| [HANDOFF_DOLPHINDB_INTRADAY_MONDAY_2026-10-12](history/HANDOFF_DOLPHINDB_INTRADAY_MONDAY_2026-10-12.md) | 编制于 10 月 9 日的 10 月 12 日日盘接续计划，包含已有证据和待办。 |

## GAP 专项交接

| 文档 | 内容和阅读说明 |
| --- | --- |
| [HANDOFF_GAP01_2026-10-09](gaps/HANDOFF_GAP01_2026-10-09.md) | 策略归属仓位闭环的实施、回归与首次开仓证据；按既定范围关闭。 |
| [HANDOFF_GAP02_2026-10-09](gaps/HANDOFF_GAP02_2026-10-09.md) | 到期处理的根因、共享账户约束及修复范围分析；未关闭。 |
| [HANDOFF_GAP03_2026-10-09](gaps/HANDOFF_GAP03_2026-10-09.md) | 参考来源策略、时效和导出一致性；按声明的快照范围关闭。 |
| [HANDOFF_GAP08_2026-10-12](gaps/HANDOFF_GAP08_2026-10-12.md) | 通用Schema、版本缓存、文件/DolphinDB/MongoDB特征Provider、对齐/等待帧；源码交付，测试及真实场景待验，未关闭。 |
| [HANDOFF_GAP10_2026-10-09](gaps/HANDOFF_GAP10_2026-10-09.md) | CTP 与 Binance 联合会话的离线交付及柜台待验范围；未关闭。 |

## 会话通用化关联文档

会话通用化交接的正文与专题设计讨论同一项数据和会话规则工作，现与[需求接口稿](../data/TRADING_SESSION_GENERALIZATION_2026-10-09.md)一起存放在 data，见[字段与资料交接稿](../data/HANDOFF_TRADING_SESSIONS_GENERALIZATION_2026-10-09.md)。该专项保持独立范围，不因文档位置改变而新增 GAP 或改变关闭状态。

## 接手顺序

接手 01/04 Live 工作时，先查协调登记和渠道分层，再读持仓恢复及 REV2/REV3 证据，最后进入对应 demo 的 README 和 SIMNOW_ACCEPTANCE。接手历史数据时，按启动规则、Provider、Schema、只读、预热、指标恢复、进程重启、正式 Recording 的顺序阅读。

GAP 清单保存整体验收标准，各专项交接保存证据范围。历史交接中的“待验”不能覆盖后续已登记结果，最新离线成功也不能直接覆盖仍待完成的柜台验收。
