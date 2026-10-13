# Bomber 架构文档与版本关系

此目录按架构内容归类，保存集成架构的两个版本、统一框架实现台账、进度快照和行情分层技术评估。版本早晚和用途在下面说明。

## 集成架构的两个版本

[ARCHITECTURE.md](ARCHITECTURE.md) 与 [ARCHITECTURE_FIXED.md](ARCHITECTURE_FIXED.md) 都是“Bomber 与 NautilusTrader 集成架构”，具有相同的 22 个一级编号章节。两份是同一设计的原稿与修订稿，保留在同一目录进行对照。

| 内容 | 原稿与修订稿的具体差异 |
| --- | --- |
| 开头说明 | 修订稿增加统一框架实现状态入口和执行后端边界澄清 |
| 运行模式及数据源 | 第 4.1 节修订离线执行、导出、在线参考查询、盘前文件优先和盘中数据库回补规则 |
| 历史预热与追赶 | 第 5.4 节补充来源选择、缺失策略及恢复时不重放历史报单和成交 |
| DolphinDB 职责 | 第 6.2 节扩展为在线数据、导出和中断回补，新增第 6.2.1 节分阶段来源表 |
| 旧适配器和实施建议 | 第 20.3 节及第 22 节对查询边界和后续接入方案作相应修订 |
| 图表 | 修订稿调整部分图表方向、显示配置和数据来源标签 |

原稿开头已明确指向修订稿的数据源边界，因此阅读集成架构优先从 ARCHITECTURE_FIXED 开始。它仍保留部分历史段落，具体来源规则须结合其正式澄清和历史启动专项解读；名称中的 FIXED 不表示所有内容或生产能力均已完成验收。

## 同目录其他文档

| 文档 | 内容与阅读位置 |
| --- | --- |
| [STRATEGY_ARCHITECTURE](STRATEGY_ARCHITECTURE.md) | 统一策略框架的运行结构、模块职责和实现验证状态台账，区别于系统级目标设计 |
| [PROJECT_PROGRESS](PROJECT_PROGRESS.md) | 截至 2026-09-22 的台账摘要，保留当时进度；后续状态查实现台账和专项证据 |
| [MARKET_DATA_CYTHON_EVALUATION](MARKET_DATA_CYTHON_EVALUATION.md) | 行情订阅与调度保留 Python、原生对象和 SDK 热路径采用 Cython/C++ 的分层选型讨论 |

相关入口：[项目文档导航](../README.md)、[GAP 评估](../audits/ARCHITECTURE_GAP_REVIEW_2026-10-09.md)、[历史启动与恢复规则](../HANDOFF/history/HANDOFF_HISTORY_STARTUP_POLICY_2026-10-09.md)。
