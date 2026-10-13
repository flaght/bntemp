# 品种专用实现与公共层场景固定审查

日期：2026-10-09。范围：`bomber/`（重点 `framework/`）、`demos/` 及依赖它们的 `scripts/integration/`。只做源码扫描和调用关系核对，不修改代码，不运行 Python、测试、数据库或柜台连接。结论是本次读取时的源码快照。

## 数量与统计口径

2026-10-13实施更新：下文是原扫描快照，旧行号及“当前实现”描述不作为最新源码状态。PS-01—04的静态会话算法已集中至`calendars.SessionCalendar`；CopperSessions/IndexSessions为兼容名称，ImDayWindow委托同一服务。DolphinDB／Feather来源与日期例外契约已接入，CU实际窗口／分钟时间点对照通过，3份demo日历已移除。历史源配置、真实期权资料、历史规则版本及完整在线接续仍未完成；市场身份和交易授权范围未因会话改造扩大。最新进度见[专项台账](../data/TRADING_SESSION_GENERALIZATION_2026-10-09.md)。原统计用于追溯，不能再次计为尚未修正的同一算法缺口。

“同类问题”指：公共能力的算法或实现类型由具体品种决定，新增品种需要复制类、修改公共分支或多个调用入口；仅增加产品配置不能接入。

| 分类 | 本次确认数量 | 说明 |
| --- | --- | --- |
| 公共框架直接按品种实现 | 2 个文件、4 个设计点 | 3 个品种专用类，另有 1 个硬编码选择函数；不是 4 个独立业务缺陷 |
| 调用层直接绑定 CU／IM | 6 个文件 | 3 个 EMA 入口／监督／预检文件，3 个 IM 联调探针；专用探针本身可以保留，但不能充当通用框架实现 |
| 品种专用实现的公共导出 | 1 个文件 | history/__init__.py 导出 ImDayWindow；仅计接口暴露，不再计一个独立算法问题 |
| 其他公共层场景固定 | 3 个文件 | 合约工厂、期权资料加载、单合约一手接管；与单品种硬编码分开统计 |

因此，严格同类涉及 **8 个有行为实现的文件**，加公共导出共 **9 个文件**。其中真正进入公共框架的直接品种专用实现是 **2 个文件**。另外确认 **3 个公共模块**有类似的场景覆盖面固定；不能把这些数字相加后宣称存在同样数量的独立 Bug。

## 公共框架中的四个设计点

| 编号 | 位置 | 当前实现 | 为什么属于本次问题 |
| --- | --- | --- | --- |
| PS-01 | [trading_sessions.py:19](/Users/kerry/work/bomber1/pro/bomber/framework/trader/runtime/trading_sessions.py:19) | CopperSessions 验证 product=CU，代码写死日盘和夜盘区间 | JSON 提供日期、节假日和禁夜日期，却不能声明会话区间／夜盘归属规则；换一类时段需要改算法实现 |
| PS-02 | [trading_sessions.py:92](/Users/kerry/work/bomber1/pro/bomber/framework/trader/runtime/trading_sessions.py:92) | IndexSessions 名称看似股指通用，实际只接受 product=IM，代码固定日盘区间 | 其他品种即使能使用相同会话模型，也不能仅换产品配置复用 |
| PS-03 | [trading_sessions.py:155](/Users/kerry/work/bomber1/pro/bomber/framework/trader/runtime/trading_sessions.py:155) | load_sessions 仅按 CU／IM 分支返回两个类 | 新增品种必须修改公共函数；缺少通过配置或注册表选择会话模型的机制 |
| PS-04 | [history/windows.py:7](/Users/kerry/work/bomber1/pro/bomber/framework/dataprep/history/windows.py:7) | ImDayWindow 再次实现 IM 日盘分钟枚举，固定 240 分钟上限 | 和 IndexSessions 重复会话区间及分钟生成，形成第二份规则来源；历史窗口本应消费同一会话接口 |

这两个文件目前没有硬编码某个合约月份；问题集中在**品种与会话算法绑定**。同时，日盘、夜盘、节假日和交易日归属必须被准确表达，不能为了通用化直接删掉这些规则或用一个统一时段套所有品种。

ImDayWindow 当前要求调用方声明一个已核验交易日，不自行判断全年节假日，这属于探针的明确边界。问题是该限定模型以 IM 专用类形式进入公共 history 包，并和 Runtime 日历重复实现，而不是声明交易日这一保护本身。

## 六个直接调用文件

| 文件 | 关键位置／行为 | 本次评价 |
| --- | --- | --- |
| [run_live.py](/Users/kerry/work/bomber1/pro/demos/01_main_ema/run_live.py:113) | IM 模式专门分支；持续模式仅 CU；历史窗口仅 CU／IM；按 IM 分配 CFFEX，否则 SHFE／INE；按品种选择日历 | 数据源、市场能力、运行模式和验收范围被揉进多处产品判断；仅替换日历类无法完成扩展 |
| [continuous.py](/Users/kerry/work/bomber1/pro/demos/01_main_ema/continuous.py:269) | 监督器直接构造 CopperSessions | 与可参数化的 EMA 算法相比，持续运行固定为 CU 日历 |
| [check_recovery.py](/Users/kerry/work/bomber1/pro/demos/01_main_ema/check_recovery.py:58) | 从检查点读取产品配置，却仍直接构造 CopperSessions | 恢复检查没有按保存的产品／会话声明选择日历；通用化时必须一起调整 |
| [warmup.py](/Users/kerry/work/bomber1/pro/scripts/integration/history/warmup.py:17) | 强制 IM 前缀和 CFFEX；固定 MainEmaConfig(IM, CFFEX)；使用 ImDayWindow | 当前 IM 专项验收工具可以限定样本，不应作为新增品种必须复制的正式运行机制 |
| [resume.py](/Users/kerry/work/bomber1/pro/scripts/integration/history/resume.py:31) | 同样固定 IM／CFFEX、EMA 和 ImDayWindow | 同上；是品种绑定的验收消费端 |
| [restart.py](/Users/kerry/work/bomber1/pro/scripts/integration/history/restart.py:21) | 重建固定 IM EMA，使用 ImDayWindow | 同上；跨进程探针与通用恢复能力需要区分 |

公共接口另有 [history/__init__.py](/Users/kerry/work/bomber1/pro/bomber/framework/dataprep/history/__init__.py:7) 直接导出 ImDayWindow。以上按文件去重，不按一个文件中出现 CU／IM 的次数累加。

## 三个相似但不同的公共层限制

| 位置 | 现状 | 判定及后续边界 |
| --- | --- | --- |
| [instrument_factory.py:40](/Users/kerry/work/bomber1/pro/bomber/framework/trader/instrument_factory.py:40) | make_option、make_future、instrument_meta 名称通用，但固定 CFFEX／CNY；合约固定 INDEX 类别及相关到期时间 | 属于股指期货／期权场景被放入公共工厂；需要明确专用工厂命名／注册范围，或由合约条款和市场 Profile 提供身份及生命周期规则。已有 make_profile_future 可接受 Profile，不能说所有合约构造都硬编码 |
| [metadata.py:172](/Users/kerry/work/bomber1/pro/bomber/framework/dataprep/metadata.py:172) | load_options_basic 固定 CFFEX、六位指数标的及股指期权代码形式 | 公共函数实际面向股指期权；商品期权等格式不能自动适用。应声明／注册 Schema 和资产类型，而不是放宽校验去猜代码。load_cffex_futures 的名称已明确市场，不单独视为错误 |
| [ctp_positions.py:33](/Users/kerry/work/bomber1/pro/bomber/framework/trader/runtime/ctp_positions.py:33) | 公共仓位接管要求 SHFE／INE、±1 手、单向投机、单条有效明细及全账户唯一持仓 | 属于首期验收准入和业务范围限制，不是 CU 专用算法；通用接管能力不足。后续可将场景政策与账本转换分开，但不能直接删除保护或扩大既有交易授权 |

这里的 3 个文件是额外公共模块，不与前面的 2 个框架文件重叠。

## 不计入同类问题的情况

- 配置文件写 product=CU／IM、真实合约、日期范围和休市日：属于数据。关键是能否通过更换配置复用实现。
- 示例默认值、测试夹具、样本报告和只读工具限定某个合约：可以合理存在；不按字符串命中数量计问题。
- CTP 的今昨仓、平今平昨，以及 Binance 的账户模式和原生接口适配：属于不同市场／协议的实际差异。专用 Adapter 合理，但其能力范围需要声明。
- 联调中的一手、DEMO、指定市场和数量上限：属于已授权验收范围；不能为了通用性移除这些约束。
- [期权 Delta 入口](/Users/kerry/work/bomber1/pro/demos/09_option_delta/run_backtest.py:28) 的 IO／IF／指数等 PAIRS 对应表：是现有业务映射，可后续配置化，但不同于为每个品种复制会话算法；本次不计入严格同类。
- 多数历史回测入口、角色与因子 Store、EMA 信号本身已经按 product 或真实合约参数化；未发现它们需要为 CU／IM 各复制一套信号算法。

## 影响与处理方向

当前更换品种不仅要补数据，还要修改日历类、load_sessions、Live 准入／市场选择、监督与恢复入口，甚至复制只读探针。相同交易规则散布在 Runtime、dataprep 和 Demo，容易出现一处更新而另一处仍按旧时段枚举分钟的情况。

建议将这一问题作为**公共会话／市场配置与场景边界**单独跟踪，关联 GAP-04（历史窗口）、GAP-06（持续恢复）、GAP-07（市场能力）与 GAP-08（数据对齐）。它属于架构可扩展性问题，不能靠逐个追加 IF、IH、IC 或新商品类完成。

实施时应先建立同一会话读取／窗口／分钟枚举接口，由配置或受控注册的会话模型提供区间、日历覆盖、夜盘锚点与交易日归属。产品到交易所、会话模型和已验收能力范围分别声明。历史、Live、监督器和恢复预检消费同一服务；专用验收探针留在 scripts/tests，公共包不依赖探针样本。

验收标准可采用：新增一个已有会话模型覆盖的品种，只增加产品／会话配置和数据，不修改公共算法或入口产品分支；不同会话模型仍正确处理夜盘、假期、边界和分钟完整性；未验收交易能力保持明确拒绝。

本次只生成分析清单，未实施上述调整，也未改变原 GAP 的关闭状态。
