# Bomber 项目文档导航

这里按主题组织系统设计、数据契约、策略方案、外部接口、架构审查与交接记录。同一主题的原稿、修订稿和历史记录放在一起，版本与验收状态在索引中说明。先根据下面的阅读入口选择主文档，再进入对应专项。

整理日期：2026-10-10，Asia/Shanghai。原有 16 份专题文档、37 份交接文档和 54 份校验及补丁材料已分类迁移。新旧路径见 [迁移说明](MIGRATION_2026-10-10.md)。

2026-10-13会话专项更新：**静态会话核心、CU真实DolphinDB／Feather对照及旧日历清理已完成；完整专项仍未关闭**。历史源配置尚未统一，CU分钟源缺失，当前Replay只有实时Bar预热进度；正式历史预热／接续及期权／规则版本仍待验收。最新状态见[专项台账](data/TRADING_SESSION_GENERALIZATION_2026-10-09.md)，实际输出见[结构化证据](evidence/live/TRADING_SESSIONS_CU_PASS_20261013.json)。

## 从哪里开始阅读

| 需要了解什么 | 首选入口 | 阅读说明 |
| --- | --- | --- |
| 项目整体结构和实现进度 | [统一策略架构与状态台账](architecture/STRATEGY_ARCHITECTURE.md) | 框架模块、运行链路、实现及验证进度的主要入口 |
| 系统目标和职责边界 | [系统目标架构](architecture/ARCHITECTURE_FIXED.md) | 区分目标设计与已经实现的能力 |
| 当前架构缺口 | [GAP 评估快照](audits/ARCHITECTURE_GAP_REVIEW_2026-10-09.md) → [完整跟踪清单](audits/ARCHITECTURE_GAPS_2026-10-09.md) | 前者便于掌握评估时状态，后者保留逐项验收标准及过程 |
| 九个策略及公共输入层 | [策略框架交接](HANDOFF/demos/HANDOFF_DEMOS_FRAMEWORK_2026-10-07.md) → [输入层实现记录](strategies/DEMOS_INPUT_STAGE1_IMPLEMENTATION_2026-10-04.md) | 具体入口和参数继续查各 demo 的 README |
| 当前 Live 渠道及策略恢复 | [渠道分层](HANDOFF/live/HANDOFF_LIVE_CHANNELS_2026-10-10.md) → [持仓接管与恢复](HANDOFF/live/HANDOFF_LIVE_POSITION_RECOVERY_2026-10-10.md) → [REV3 验证证据](HANDOFF/live/HANDOFF_LIVE_REV3_EVIDENCE_2026-10-10.md) | 按源码版本和验收范围阅读，离线结果与实际柜台结果分别确认 |
| 历史预热和盘中回补 | [历史启动规则](HANDOFF/history/HANDOFF_HISTORY_STARTUP_POLICY_2026-10-09.md) → [Provider 实施](HANDOFF/history/HANDOFF_HISTORY_PROVIDER_2026-10-09.md) → [历史数据交接索引](HANDOFF/README.md#历史行情预热与恢复) | 后续规则补充了早期架构中的“在线历史仅本地”表述 |
| 与 DXBM LXG 系统对接 | [接口规范](integration/DXBM_LXG_INTEGRATION_SPEC.md) | 双方评审草案，包含建议契约与验收标准 |
| M02 执行客户端替换 | [设计与代码改动清单](architecture/M02_EXECUTION_CLIENT_DESIGN_2026-10-19.md) | 关联 D5／GAP10；LXGBN Windows 宿主调用项目 SDK；已实现离线链路，实机待验 |
| 接续某个具体任务 | [交接记录索引](HANDOFF/README.md) | 按主题找交接，再核对对应版本和证据 |
| 同步或定位旧文件 | [证据材料索引](evidence/README.md)和[路径对照](MIGRATION_2026-10-10.md#新旧路径对照) | 校验清单从项目根目录使用；原始材料已备份 |

## 文档分类

数量按原有材料统计，不包含新增导航、迁移说明和备份。

| 目录 | 内容 | 文档或材料数量 |
| --- | --- | --- |
| [architecture](architecture/README.md) | 集成架构原稿与修订稿、框架状态、进度快照、行情分层评估 | 5 |
| [data](#数据契约与会话规则) | DataHub、基础条款、期货期权数据及会话通用化两份关联稿 | 5 |
| [strategies](#策略与公共输入设计) | 示例适配、输入层设计和实现交接 | 3 |
| [integration](#系统对接) | DXBM LXG 外部系统接口 | 1 |
| [audits](#架构审查与缺口) | GAP 清单、评估快照、品种专用代码审查 | 3 |
| [HANDOFF](HANDOFF/README.md) | 按策略、实盘、历史、GAP、协调细分，历史版本随主题保存 | 36 |
| [evidence](evidence/README.md) | 校验清单、补丁与同步脚本 | 54 |
| [source_snapshots](source_snapshots/README.md) | 第一阶段迁移前源码快照及校验清单 | 原始目录保留 |
| [phase1_baseline_2026-10-04](phase1_baseline_2026-10-04/README.md) | 第一阶段基线压缩包及文件摘要 | 原始目录保留 |

## 架构与实现台账

| 文档 | 内容和关系 |
| --- | --- |
| [ARCHITECTURE](architecture/ARCHITECTURE.md) | Bomber 与 NautilusTrader 集成架构原稿，与下一份是同一设计的不同版本 |
| [ARCHITECTURE_FIXED](architecture/ARCHITECTURE_FIXED.md) | 同一集成架构的修订稿，主要修订数据来源、历史预热与回补规则；两份的差异见[架构目录说明](architecture/README.md) |
| [STRATEGY_ARCHITECTURE](architecture/STRATEGY_ARCHITECTURE.md) | 统一策略框架的模块职责、运行链路和实现验证台账；与上述系统级设计有不同侧重点 |
| [PROJECT_PROGRESS](architecture/PROJECT_PROGRESS.md) | 从状态台账整理的截至 2026-09-22 的进度摘要，作为同一主题的历史快照保存 |
| [MARKET_DATA_CYTHON_EVALUATION](architecture/MARKET_DATA_CYTHON_EVALUATION.md) | 行情组件的 Python、Cython/C++ 职责划分和冷热路径设计，属于架构技术选型 |

## 数据契约与会话规则

| 文档 | 用途和状态 |
| --- | --- |
| [DATAHUB_MINIMAL_CORE](data/DATAHUB_MINIMAL_CORE.md) | 参考研究数据最小查询核、时间可见性和分阶段验收 |
| [BASIC_METADATA_ALIGNMENT](data/BASIC_METADATA_ALIGNMENT.md) | 期货期权基础条款在 datahub、dataprep、trader 各层的结构与接口对齐 |
| [FUTURES_OPTIONS_DATA_CONTRACT_ANALYSIS](data/FUTURES_OPTIONS_DATA_CONTRACT_ANALYSIS_2026-10-09.md) | 数据需求及格式草案，区分会话通用化与完整数据适配范围 |
| [TRADING_SESSION_GENERALIZATION](data/TRADING_SESSION_GENERALIZATION_2026-10-09.md) | 当前主台账：静态核心／CU真实对照／旧日历清理完成；历史配置、实际预热接续、期权与规则版本待完成 |
| [HANDOFF_TRADING_SESSIONS_GENERALIZATION](data/HANDOFF_TRADING_SESSIONS_GENERALIZATION_2026-10-09.md) | 同专项交接：实际七字段数据、迁移证据和后续条件；完整目标契约与首期实现分别标记 |

## 策略与公共输入设计

| 文档 | 用途和状态 |
| --- | --- |
| [STRATEGY_EXAMPLES_FRAMEWORK_ANALYSIS](strategies/STRATEGY_EXAMPLES_FRAMEWORK_ANALYSIS.md) | 早期 example 系列到统一框架的能力映射与迁移规划；旧“当前缺口”须结合新台账阅读 |
| [DEMOS_INPUT_RUNTIME_DESIGN](strategies/DEMOS_INPUT_RUNTIME_DESIGN_2026-10-04.md) | 公共输入与运行层两阶段设计，含工作包、契约和验收矩阵 |
| [DEMOS_INPUT_STAGE1_IMPLEMENTATION](strategies/DEMOS_INPUT_STAGE1_IMPLEMENTATION_2026-10-04.md) | 第一阶段实现、命名及目录迁移记录，后续补充了远程回归和单策略结果；各版本单独验收 |

## 系统对接

| 文档 | 用途和状态 |
| --- | --- |
| [DXBM_LXG_INTEGRATION_SPEC](integration/DXBM_LXG_INTEGRATION_SPEC.md) | v0.8 双方评审草案：订单命令、回调、策略归属、恢复和能力声明；不代表适配器已实现 |

## 架构审查与缺口

| 文档 | 用途和状态 |
| --- | --- |
| [ARCHITECTURE_GAP_REVIEW](audits/ARCHITECTURE_GAP_REVIEW_2026-10-09.md) | 2026-10-09 的评估快照，明确区分当前实现、已验版本和未完成验收 |
| [ARCHITECTURE_GAPS](audits/ARCHITECTURE_GAPS_2026-10-09.md) | 完整问题清单、验收标准及关闭记录，包含历史状态 |
| [PRODUCT_SPECIALIZATION_AUDIT](audits/PRODUCT_SPECIALIZATION_AUDIT_2026-10-09.md) | CU/IM 等品种专用实现及公共层场景固定的源码审查，为会话通用化提供依据 |

## 内容相近的文档如何阅读

| 关联文档 | 正文关系 | 归类与阅读方式 |
| --- | --- | --- |
| ARCHITECTURE 与 ARCHITECTURE_FIXED | 同一集成架构的原稿和修订稿，章节骨架一致，业务规则有修订 | 同放 architecture；先读修订稿，需要追溯差异时对照原稿 |
| 两份交易会话通用化文档 | 同一专项的两个整理稿：接口与验收说明、字段与交接细节互补 | 同放 data，联合阅读，保留各自说明 |
| 输入层设计与第一阶段实现记录 | 同一工作流的设计、实施和多轮反馈，不能按内容重叠判作重复副本 | 同放 strategies；先查设计约束，再查对应版本交付 |
| GAP 完整清单与评估快照 | 同一架构评估的逐项标准、过程记录与阶段汇总 | 同放 audits，结合日期和具体专项证据阅读 |
| 历史恢复、停机重启与 Live 持仓恢复 | 前两者验证指标和历史事件接续，后者处理柜台仓位和归属 | 分别放在 history 与 live；“恢复”这个词不表示同一种能力 |
| 早期 CTP Binance 交接与后续 Live 交接 | 同一实盘接入工作线的阶段记录 | 同放 HANDOFF/live，历史或撤回状态写在索引中 |

分类先看正文的主题、职责和文档关系，再判断版本和状态；状态不作为把同一主题拆到不同目录的依据。正文存在差异的稿件保留全文，避免丢失版本信息。

## 文档状态如何判断

目标架构回答“系统应当如何工作”，状态台账回答“已实现和已验证到哪里”，专项交接回答“某次修改和证据覆盖什么范围”。日期可能表示初稿或计划执行日；例如文件名含 2026-10-12 的盘中回补交接编制于 2026-10-09，记录的是计划，不是完成结果。

旧段落保留了当时状态。阅读时优先看同一文档的最新结论，再核对后续专项与修订证据；离线回归、数据库只读、Recording 和实际 SimNow 成交的结论各自只覆盖已声明范围。

## 后续文档维护

- 系统集成架构的原稿和修订稿同放 architecture；统一框架的实现状态继续维护在 STRATEGY_ARCHITECTURE。
- 新设计按主题存入 data、strategies 或 integration；审查结果存入 audits。
- 同一主题的原稿、修订稿和历史总结放在一起，在索引中注明版本关系及阅读顺序；撤回状态和原文证据保留。
- 同步校验清单放入 evidence/checksums，补丁及删除清单放入 evidence/patches；文件摘要更新必须注明范围。
- 新增或移动文档时，同时更新本文或交接索引，并核对 Markdown 链接和项目根目录命令中的路径。
