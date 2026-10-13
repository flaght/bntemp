# 策略与整体框架交接：优化前基线（2026-10-07）

2026-10-08 公共参考数据源工厂已新增，原文件函数签名保留，角色／因子解析新增非文件入口，01可选择DolphinDB；策略公式及回测组装未改。详见[数据源说明](../../../bomber/framework/dataprep/sources/README.md)和并行约定PF2026100802。当前为代码完成待远程验证，回测侧需验证外部因子及角色场景兼容。

2026-10-08第四轮补充：参考数据源23/23、公共回归32/32、角色研究／信号及01entry21/21已有远程通过证据；最新报告 `tests/results/main-ema-acceptance-1791443131209031509`。角色研究价冲突修复按PF2026100803完成默认离线回归，原文件因子／角色及条款兼容已验证。真实数据库、当前策略实时Recording及柜台成交仍待验收，可选角色研究--real迁移探针仍待实际文件验证；无性能测量或收益结论。

最新日期澄清PF2026100804：原始数据库因子date为此前交易日来源日，实盘数据库装配默认source，与期限结构同日；已对齐回测文件仍用trading/aligned，run_backtest和strategy未修改。公共在线组合器新增可选日期口径并保留原直接调用默认；该修正版references29项、entry22项及公共回归待远程复验，旧通过证据继续保留。

最新验证补充：该日期修正版已取得远程29／32／22项全部通过及真实DolphinDB RB只读通过证据，详见PF2026100804和01接入验收；RB主力rb2701.SHFE，指定TD日2026-10-08、角色／因子日2026-09-30、累计因子1.181055、tick1、multiplier10。原回测输入兼容已验证，公共参考读取接入已核验；当前策略实时Recording及真实柜台交易仍待验收。本次只更新记录，没有性能优化或运行代码变更。

本文汇总当前 `pro` 源码、九个策略 README、用户运行反馈及 `log1.log`，供后续开发接手。当前任务是记录现状，未进行性能优化，也未修改策略行为。

后续回测优化与实盘接入采用[并行工作协调约定](../coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md)。接手时先读取该约定及未关闭的公共变更记录；涉及公共框架变化必须在修改前通知用户，完成或回退后再通知。本文件中的基线和待验证事项继续有效，后续工作顺序按并行约定执行。

## 1. 接手范围与协作约定

| 项目 | 当前约定 |
| --- | --- |
| 本机项目 | `/Users/kerry/work/bomber1/pro`；不要误改另一个工作区 `/Users/kerry/work/code1/nautilus_trader` |
| 策略目录 | `demos/01_main_ema` 至 `demos/09_option_delta`；`examples` 和 `doc/source_snapshots` 是参考或历史材料 |
| 用户运行环境 | Linux，最近路径为 `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro`，Python 环境为 `uv-nautilus` |
| 本机限制 | 当前本机不是运行环境。只读取、生成和修改源码及文档，不执行 Python、测试、回测或编译 |
| 编码与说明 | 注释使用中文；配置由参数和既有环境文件提供，不硬编码运行机路径 |
| 策略边界 | 数据准备进入公共 `dataprep`；具体算法在信号模块及 `strategy.py`；运行装配在 `run_backtest.py` |
| 文件结构 | 02–09 的核心 Python 文件为 `run_backtest.py`、对应信号文件、`strategy.py`；01 当前只有入口和策略两个文件，EMA 直接调用核心指标 |
| 文档格式 | 各策略 README 统一为“1. 使用方法 / 2. 策略详细说明 / 3. 使用核心组件描述” |
| 使用方法格式 | 提供完整常用参数命令、仅必填参数的默认命令；参数注明必选/可选、默认值及输出内容 |
| 文档内容 | 策略 README 作为样例说明，不加入迁移过程、目录只保留几个文件等开发历史，不要求额外 export 或写“从 pro 根目录执行” |
| 组件说明 | 按 dataprep、datahub、market、trader 分模块，统一使用接口/用途表格 |

本交接优先于旧 `HANDOFF_DEMOS_2026-10-04.md` 中已经变化的说明；实际接口最终以当前源码为准。旧文档不要作为当前实现直接照抄。

## 2. 当前总体进展

| 工作 | 当前状态 |
| --- | --- |
| 公共输入层 | 已由 inputs 更名为 dataprep；九个入口已接入公共能力，策略目录不再维护独立 local_input.py |
| 策略迁移 | 九个例子均已有对应回测实现；09 已从仅选约改为真实模拟交易 |
| 基础条款对齐 | DataHub 已具备对称的 FutureBasic / OptionBasic 及 Provider；静态输入适配共用条款校验 |
| 角色与因子 | 01、02、04、06 使用外部主力累计因子；05 保留三角色因果换约因子构造；03、07、08、09 不用连续角色复权 |
| 回测时间日志 | 九个入口已有阶段耗时；当前尚未将回测内部各执行环节分开计时 |
| 历史与实盘统一 | 公共框架已有 live/CTP/Binance 等基础组件，但九个 demo 尚未完成统一 run_live 的开发和验收 |
| 验证 | 最新日志证明 01–07 所记录样本完成回测；不能据此认定九策略全部验收、交易逐笔正确或可实盘 |
| 下一阶段 | 先复核策略和框架基线，再补充执行链计时、开展性能优化；08、09 的问题与验证需继续跟进 |

## 3. 整体框架与事件流

```text
命令行参数 / 既有 .env / 本地资料
    ↓
dataprep：解析路径、索引、校验、标准化、准备场景
    ├─→ datahub：角色、累计因子、研究价、计划或版本化条款查询
    └─→ market：Feed + Reader + Parser，按可用时间回放真实事件
              ├─→ 行情适配器 → 模拟 Backend → 撮合及成交回报
              └─→ Runner / DataBinding → StrategyTemplate → 信号计算
                                                    ↓
                                      set_target / set_targets
                                                    ↓
                              执行路由 → 目标合并与订单规划 → 风控
                                                    ↓
                                模拟客户端 / Backend → 原生订单
                                                    ↓
                                  实际仓位、在途数量及审计报表
```

原生行情先推进模拟执行，再供策略产生后续目标，具体同时间戳顺序需保持现有 Feed 注册与分派语义。不能为了提速把未来事件预先送入执行器。

### 3.1 dataprep：输入准备

| 模块 / 接口 | 职责与注意事项 |
| --- | --- |
| contracts | DataPaths、BarFileKey、BarReadSpec、InstrumentSpec、InputPlan、InputContext、HistoricalInputBundle 等公共值契约 |
| paths.resolve_futures_args / resolve_option_args | 统一解析显式参数、配置、环境和默认文件；只检查当前场景必需输入 |
| catalog.scan_bar_files / inventory / bar_path | 以资产、真实合约、交易日定位文件；不根据文件存在与否擅自替策略改选月份 |
| session.input_session / InputSession | 单次运行共享缓存；嵌套入口复用会话，退出清理；不是跨运行持久缓存 |
| session.read_feather | 记录来源版本，按路径、文件大小、修改时间及投影缓存；返回帧副本，已读取来源变化时报错 |
| bars.read_bar_frame / BarReadSpec | 时间、身份、字段及坏值政策校验，输出 event_ns / available_ns 等标准列 |
| bars.PreparedFrameReader / add_prepared_bar_source / add_bar_source | 将准备结果注册到既有 Feed；不启动回测，不把研究复权价变成可成交价格 |
| metadata | 静态基础条款校验及既有 InstrumentSpec / 原始行接口适配 |
| basic | 读取版本化条款，调用方明确提供 ReferenceRecord 时间规则，组装两类 Provider |
| references / factors | 角色组装、研究价及外部累计因子适配；不同场景因子来源见第 4 节 |
| calendar | 显式交易日历，或基于已知行情文件日期推导日历；后者不能证明不存在缺失交易日 |
| scenarios.role_futures | 角色期货研究及输入计划组装，prepare_role_research 为当前常用入口 |
| scenarios.fixed_contracts | 根据调用方选定的真实合约和日期准备固定输入 |
| scenarios.option_chain | 期权、指数、期货分资产准备；不负责 Delta 选约、Vega 配仓或对冲资格判断 |
| schedules.load_target_csv | 校验外部目标计划，组装 TargetScheduleStore |
| session.write_input_reports | 输出 input_manifest.json、input_coverage.json、input_issues.json |

`DataRequirements` 是需求契约类型，不是策略计算函数。当前入口常直接使用场景接口，不代表统一声明式运行工厂已经完成。

### 3.2 datahub：参考资料

| 模块 / 接口 | 职责与边界 |
| --- | --- |
| sector_roles.SectorRoleStore / SectorRoleAssignment | 多品种角色及累计因子快照；snapshot(timestamp) 后查 instrument(product, role)、factor(product, role) |
| role_prices / core | 单品种多角色研究价、换约及因子查询，05 使用该类研究能力 |
| target_schedule | 外部组合目标计划及时间查询 |
| temporal | ReferenceRecord、AsOfQuery 及发布、生效、来源年龄门控 |
| future_basic / option_basic | 不可变条款模型及对称 Provider，共用 basic.py 的字段校验和查询基类 |

文件读取放在 dataprep，行情回放放在 market，原生交易合约和订单放在 trader。现有静态策略不自动获得版本化 Provider 的盘中条款更新能力；静态入口仍拒绝非空 available_ns / source_version 等不支持的版本信息。

### 3.3 market：行情与回放

| 模块 / 接口 | 职责与边界 |
| --- | --- |
| basic.base | Bar、CustomBar、InstrumentMeta、DataType、MarketDataFeed 等行情公共契约 |
| replay.base.FileReplayFeed | 读取已注册 Source，逐行解析事件，按 ts_init 排序并回放；load_events 在运行阶段发生 |
| FixedInstrumentBarParser | 把严格校验的真实 OHLCV 转为原生 Bar，使用准备后的时间，不二次移动分钟标签 |
| CustomBar | 承载研究字段和选约因子；不作为第二份行情重复推进模拟撮合 |
| basic/custom_bar.pyx、fast_factory.pyx | Cython 扩展；须在用户运行环境按 market/basic/README.md 编译 |
| native / 在线 Feed | 已有原生行情基础能力，不代表九个回测策略已完成实盘接入验收 |

### 3.4 trader：策略、执行与生命周期

| 模块 / 接口 | 职责与边界 |
| --- | --- |
| StrategyTemplate | on_bar / on_custom_bar、逻辑目标提交、实际仓位及在途查询 |
| UnifiedStrategyRunner / DataBinding | 行情绑定、策略分派、目标提交协调 |
| ExecutionRoute / DynamicExecutionRoute | 固定真实合约或日程角色映射；动态路由协调旧、新合约仓位 |
| PositionManager / NetTargetOrderPlanner | 实际及在途仓位管理，依据目标差额规划订单 |
| MarketReferencePriceStore / PreTradeRiskManager / RiskLimits | 真实参考价格、时效、数量及名义金额约束 |
| execution.simulation.client / backend / gateway | 模拟执行、原生订单转换、成交回报；Backend 内核为 Bomber BacktestEngine |
| CtpFuturesBasicProfile | 账户、基础手续费和保证金配置；不等价于完整交易所逐日结算与平今/平昨模型 |
| instrument_factory | 原生期货/期权及行情元信息工厂；不要再复制进每个 demo |
| runtime.market_adapter.NautilusMarketFeedAdapter | 把真实标准事件串行送入模拟 Backend，CustomBar 不重复撮合 |
| runtime.historical.UnifiedHistoricalRuntime | 启动、回放、Backend.finish、结果及停止的回测生命周期 |
| execution.live / execution.ctp 等 | 实盘执行、账户读取、恢复和传输基础模块；本交接未重新审计这些模块或确认在线运行状态 |

## 4. 数据与交易契约

| 项目 | 当前口径 |
| --- | --- |
| 常见数据布局 | role 下 fut_basic.feather、opt_basic.feather、fut_contract.feather、fut_adjustment_factors.feather；kline 下 fut/opt/index，按 YYYYMMDD 和真实合约存储 |
| 路径 | 入口自动读取既有 .env，显式参数优先；不要求 README 命令额外 export 数据根目录 |
| 角色表文件 | 默认 fut_contract.feather，不存在时兼容 fut_contract_data.feather；两者同时存在时明确指定 |
| 角色命名 | 角色表 second 映射为 secondary；03 默认 secondary / far，没有第三个 near 腿 |
| 分钟标签 | 九个入口默认 end；源文件实际为 start 时必须传 start，公共层只加一次周期 |
| 事件 / 可用时间 | event_ns 表达完整分钟，available_ns 映射 ts_init；Feed 按可用时间排序。无显式可用列时记录完成时刻可用的假设 |
| 交易日 | 商品采用上游文件交易日归属；股指日盘用 day_session。缺日历时不能精确证明夜盘节假日归属 |
| 成交量 | 分钟增量；累计量须按明确规则上游转换。公共层不猜测、不重复差分、不补价 |
| 真实与复权价 | close × 因子只用于研究；下单、风控、名义金额和成交使用真实价格 |
| 因子字段 | pcr_factor 为单期因子；pcr_cumfactor 为累计因子。无 role 的旧表仅表示主力，不推断次主力因子 |
| 外部主力因子 | 01、02、04、06 使用当天已对齐累计因子，trading / aligned；不再要求 --factor-date-basis。若有 available_ns 仍按实际发布门控 |
| 合约冲突 | 使用外部因子的同角色，以因子表 symbol 为准，写覆盖诊断；不得把主力因子 symbol 覆盖到次主力执行角色 |
| 无效因子 | 请求范围内必须为有限正数；缺失、身份不合法或未发布仍报错，不回退为 1 |
| 期权 tick | opt_basic.tickNum 优先，缺失或为空时兼容 minChgPriceNum / price_increment；非空无效值报错，不绕过 |
| 期货 tick | minChgPriceNum 优先，其次 tickNum / price_increment；执行合约需要有效价位及乘数 |
| 目标语义 | set_targets 是最终仓位意图，不是新增订单数量。历史合约的零目标用于清仓，不表示同时建立所有合约仓位 |
| 多腿成交 | 批量目标不保证同时或原子成交；提交成功不代表成交完成 |
| metadata | 审计和诊断信息；不是生成订单的必需行情。精简前需检查报表和测试的依赖 |

## 5. 九个策略当前实现

| 目录 / 信号文件 | 信号与交易对象 | 仓位、换约和退出 |
| --- | --- | --- |
| 01_main_ema / 无独立信号文件 | 单品种主力真实价 × 当天累计因子更新快慢 EMA；慢线预热后快 ≥ 慢做多，否则做空；按关系建仓，不必等待首次交叉 | 固定 quantity，方向变化才提交；动态主力路由换约，保留指标；无末日强平 |
| 02_sector_chain / sector_signal.py | 可配置两个 leader + 一个 comparison；三个主力同步，比较品种当前单帧收益率与领先收益平滑参考比较；默认交易 RB 次主力 | 主力因子仅生成信号，次主力真实行情成交；等待执行腿报价，可配置延迟；相同方向/合约去重；动态换约，无末日强平 |
| 03_calendar_spread / calendar_signal.py | 同品种次主力与 far，真实价差 leg1 − leg2；此前窗口均值及总体标准差计算 z-score，不复权 | 两腿等手数反向；偏离入场、回归退出；角色变化先平旧实际仓位并等待无在途，再启用新组合和重新预热；无末日强平 |
| 04_cross_section / cross_section_signal.py | 多品种主力复权动量排名，完整同步帧；每侧 max(1,floor(N×group_fraction)) 个，强组多、弱组空 | target_notional 是每侧预算，组内均分，按真实价格×乘数向下取整；旧合约及中间组归零；窗口换约不重置，无末日强平 |
| 05_role_cross / role_signal.py | 单品种 main/secondary/far 三角色各自因果复权研究价；主力相对三角色均价的差 D 穿越零线 | 只交易主力；首帧不下单、D=0 不主动平仓；动态主力换约；不读取外部主力因子；无末日强平 |
| 06_triangle_spread / spread_signal.py | 三品种主力复权价，首帧归一化对数价差；anchor 减两个加权 hedge，再算历史窗口 z-score | 主腿预算 N、对冲腿按权重分 N；真实金额取整，任一腿不足一手报错；偏离入场/回归退出，完整目标换约；无专门先平后开状态机或末日强平 |
| 07_scheduled_targets / schedule_signal.py | CSV 完整组合目标，不计算价格方向；独立计划时钟，精确时点触发 | REPLACE，未列旧目标归零；错过不补单，审计 SUBMITTED/MISSED/NOT_REACHED；真实合约换约和最后清仓由计划显式给定 |
| 08_option_vega / vega_signal.py | 默认 MO/IM/000852；按 Delta 档选虚值认购，Black76 反解 IV/Greeks；固定 Vega 预算卖出认购 | 实际已成交期权 Delta 分月由同月期货对冲；临近到期或时间价值不足退出；最后日默认 14:50 全平并检查；价格及 IV 默认年龄 300 秒 |
| 09_option_delta / delta_signal.py | 默认 MO，BS 或 Black76；按期限及绝对 Delta 选虚值 Call/Put；当前是双腿买入回测，不再仅选约 | 每腿 +quantity；新旧组合先平后开，实际仓位及在途门控；不完整选约不新增单腿；临近到期/最后日退出；指数和定价期货不交易 |

各入口参数及完整命令以对应 README 为准。策略窗口大多以完整同步帧计数，不等于自然分钟或交易日；缺失行情不前填。

### 5.1 必须保留的策略差异

1. 02 是三品种信号、一品种执行，并非三腿套利；可用于化工、农产品、油脂等参数组合，但代码不验证产业关系。
2. 02 比较品种用当前单帧收益，leader 用平滑收益；日志交易频繁，不能为性能目的擅自改为同窗口累计收益。
3. 03 leg1/leg2 是配置的两个角色，不按到期日重新选 near。select_roles 已兼容旧 near_role/far_role，但入口、信号、策略应整套同步。
4. 04 默认每侧 100000、两品种各 50000，单手金额较大时目标可为零。这是预算取整结果，不强制一手，也不转移剩余预算。
5. 05 初始因子 1 来自独立三角色研究构造；不能因为 01/02 用外部因子就把 05 三角色都替换为同一个主力因子。
6. 08 的定价与对冲标的需匹配：MO/IM/000852，IO/IF/000300，HO/IH/000016；不能仅将 MO 的 IM 换成 IF 来避开缺失行情。
7. 09 BS 指 Black–Scholes，指数用于定价和虚值判断；Black76 用同月期货定价，两种模式都只下单 Call/Put。买入双腿不保证组合 Delta 为零。

## 6. 输出与验收口径

| 输出 | 用途 |
| --- | --- |
| orders.csv / fills.csv | 委托和实际模拟成交；区分目标、订单、成交三层 |
| positions.csv | 实际真实合约持仓记录；检查历史合约残留和退出 |
| account.csv 或 account_<venue>.csv | 单/多交易所账户资金；文件形式按入口配置 |
| summary.json | 参数、输入范围、信号/目标/交易统计、结果摘要 |
| signals.csv 等策略文件 | 选约、调仓、换约及等待行为，具体按各 README |
| 07 schedule_audit.csv | 时点执行审计，不代替成交证明 |
| 09 candidates.csv / decisions.csv / selections.csv | 候选拒绝原因、选约决策和结果；额外保留完整交易报表 |
| input_manifest.json | 输入文件版本、路径来源及读取记录 |
| input_coverage.json / input_issues.json | 日期覆盖、依赖日期、时间假设及数据问题 |
| tearsheet.html | 可选绩效图，--no-tearsheet 关闭 |

01–07 并非默认末日强平，末尾持仓不能一律判为错误。08/09 有明确全平验收，需要实际仓位及在途订单清零，不能只看零目标。--require-fills 仅要求产生成交，不验证盈利、所有腿成交或所有换约正确。

## 7. 运行证据与待验证修复

### 7.1 log1.log 中的最新成功样本

| 策略 | 天数 | Bar 数 | 订单 / 成交 | 研究加载秒 | 回测秒 | 总耗时秒 |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| 01 | 92 | 32153 | 13549 / 13549 | 12.095 | 116.948 | 137.660 |
| 02 | 95 | 135046 | 32885 / 32885 | 36.838 | 978.189 | 1028.320 |
| 03 | 95 | 71393 | 3548 / 3548 | 不走同类研究加载 | 36.461 | 40.857 |
| 04 | 95 | 268592 | 4560 / 4560 | 98.930 | 80.167 | 191.616 |
| 05 | 95 | 98440 | 3 / 3 | 10.047 | 17.552 | 31.385 |
| 06 | 95 | 99553 | 3233 / 3248 | 37.581 | 32.998 | 74.861 |
| 07 | 18 | 6196 | 100 / 100 | 不走同类研究加载 | 1.066 | 1.819 |

01 为 2026-01-05 至 2026-05-26；02–06 为 2026-01-05 至 2026-05-29；07 为 2026-05-06 至 2026-05-29。06 成交数量多于订单数量，不能假设所有订单只有一次成交。完整证据见 [log1.log](../../../log1.log)。

此前用户报告公共输入 26 项、角色场景 2 项、Delta 31 项、Vega 21 项及角色/计划/跨期规则脚本通过，属于当时版本；不能代替本轮新代码回归。

### 7.2 尚未闭环的问题

| 项目 | 证据 / 修复 / 下一步 |
| --- | --- |
| 08 早盘缺行情 | 2026-09-10 IM2609 文件 216 行，最早 09:56，09:30–09:37 无行情；09:37 最新价格仍是前日 15:01，年龄 66960 秒。属于真实输入缺口，不能靠继续增大 300 秒限制修复 |
| 08 后续验收 | 需补齐/更换有完整数据的匹配日期或标的组合，再验证对冲、场次恢复、缺价超时及末日清仓；暂无新成功结果 |
| 09 工厂参数 | 用户环境 make_option 曾缺 profile_id；本机当前工厂签名已有 kind 和 profile_id。需同步 trader/instrument_factory.py 与策略三文件，确认实际加载路径 |
| Vega 测试失败 | 用户最近报告中文错误信息与旧英文断言不一致，以及 120→300 秒后超时夹具不匹配；本机已改中文断言并按配置构造超时，未收到重跑结果 |
| Delta 测试失败 | 三处夹具引用不存在的 StrategyTests；当前已改为 FrameTests.setUpClass()，未收到重跑结果 |
| 基础条款测试 | 新增 test_basic_alignment.py；Provider 和静态适配对齐仍需用户环境回归 |
| 混合版本部署 | 曾出现 datahub.option_basic 缺文件、select_roles 不接受 leg1_role、make_option 不接受 profile_id；应按公共模块和策略整体同步，避免只传一个入口 |
| 调试残留 | 当前静态搜索发现 01 strategy.py 仍有 import pdb，未发现 demos 中有效 pdb.set_trace / breakpoint 调用；后续可清理无用导入，不把历史调试输出当当前自动断点 |

## 8. 优化前性能结论

### 8.1 已确认的数据准备开销

`dataprep/references.py:load_sector_research` 扫描所选品种截至 end_day 的所有合约文件，未按 start_day 或实际角色先裁剪。`_collect_closes(minute=False)` 读取、标准化完整文件后才取最后一条 close；存在超出实际交易需求的读取和处理。

01/02/04/06 的研究加载平均每品种约 12 秒；04 的 98.930 秒研究加载超过 80.167 秒回测运行。该部分是公共准备层的明确优化方向，但裁剪必须保留必要的前置角色记录、因子日期、锚点和旧合约退出行情。

### 8.2 已确认的执行结构与重点疑点

| 观察 | 结论边界 |
| --- | --- |
| 02 相对 06：Bar 1.36 倍、订单 10.17 倍、回测 29.64 倍 | 行情数量解释不了差异；高频交易的执行链是重点，但不能仅按订单数宣布原因已经查明 |
| 02 完整帧 32748、信号 32704、目标 16447 | 约每两个同步帧提交一次目标，需确认频繁方向切换是否符合策略预期 |
| 01 Bar 少于 03，订单及时间却更多；05 近十万 Bar 只有三单且很快 | 单看品种数、日期或 Bar 数不是有效工作量指标 |
| Backend.process_market_event 每事件 add_data([event]) → run(streaming=True) → clear_data | 存在单事件反复推进的公共开销；当前不能直接改成一次性批量喂未来行情 |
| Feed.load_events 在 replay 内逐行创建原生事件并排序 | 已准备数据不代表事件解析成本已计入准备阶段 |
| UnifiedHistoricalRuntime.run 还包含 Backend.finish | “运行回测”包含解析、排序、事件分派、策略、撮合、回报和引擎结束，尚无法归因到单一函数 |
| 02 外部报表写出及图生成 6.591 秒 | 外部图表生成不是 978 秒的主要解释；引擎内部收尾统计仍未单独测量 |

**尚未证实**有历史订单全表扫描、二次复杂度、内存泄漏或某个核心函数独占耗时。不得把推测写成已测量结论，也不能用本机依赖源码替代运行机实际版本的性能证据。

### 8.3 下一阶段实施顺序

1. 保留当前日志作为基线，记录运行机 Python、Bomber/扩展版本、实际加载路径及相同输入版本。
2. 先给 02 拆分累计耗时：Feed.load_events、策略事件处理、目标路由/规划/风控、Backend.process_market_event、成交回报、Backend.finish。计时要处理嵌套，不能重复相加。
3. 每 5000 根 Bar 输出区间耗时及累计订单/成交，判断是否随交易历史增长而减速；使用汇总输出避免逐事件日志成为新瓶颈。
4. 将 01/03/05/06 作为对照，比较行情吞吐与交易密度，不把信号模块耗时等同完整执行耗时。
5. 优化研究读取范围和复用，先确认前置依赖日期、缓存副本成本及标准化是否重复，不直接删除校验。
6. 依据测量结果优化执行热路径。若研究批量推进，须保持事件可见性、订单时间、同帧顺序、回报处理及旧仓换约语义。
7. 同输入前后比较信号、目标、真实合约、订单/成交方向与数量、终态持仓及输入诊断；仅耗时变短不算完成验收。
8. 完成历史策略稳定性和性能验证后，再进入 run_live / run_backtest 统一；不要在同一次性能修改中更换策略公式。

## 9. 接手阅读与运行环境回归

| 内容 | 阅读入口 |
| --- | --- |
| 各策略参数、公式、输出 | [01](../../../demos/01_main_ema/README.md)、[02](../../../demos/02_sector_chain/README.md)、[03](../../../demos/03_calendar_spread/README.md)、[04](../../../demos/04_cross_section/README.md)、[05](../../../demos/05_role_cross/README.md)、[06](../../../demos/06_triangle_spread/README.md)、[07](../../../demos/07_scheduled_targets/README.md)、[08](../../../demos/08_option_vega/README.md)、[09](../../../demos/09_option_delta/README.md) |
| 公共输入 API | [API_USAGE](../../../bomber/framework/dataprep/API_USAGE.md)、[dataprep README](../../../bomber/framework/dataprep/README.md)；其中历史实施状态部分需结合本交接看，不能视为最新验收记录 |
| 参考条款对齐 | [DataHub README](../../../bomber/framework/datahub/README.md)、[条款结构对齐](../../data/BASIC_METADATA_ALIGNMENT.md) |
| 原生扩展构建 | [market/basic 编译说明](../../../bomber/framework/market/basic/README.md) |
| 当前性能样本 | [log1.log](../../../log1.log) |
| 历史交接 | [2026-10-04 交接](HANDOFF_DEMOS_2026-10-04.md)，仅用于历史背景 |

下列命令只供用户运行环境执行，本机未执行：

```bash
python tests/test_dataprep_common.py
python tests/test_dataprep_role_scene.py
python tests/test_dataprep_external_factors.py
python -m unittest tests.test_basic_alignment tests.test_datahub_option_basic
python tests/test_calendar_spread_selection.py
python tests/test_demo_sector_chain_execution_clock.py
python tests/test_demo_cross_section_signal.py
python tests/test_demo_role_cross_three_roles.py
python tests/test_demo_triangle_spread_signal.py
python tests/test_demo_scheduled_targets.py
python tests/test_demo_option_vega.py
python tests/test_demo_option_delta.py
```

测试通过后再按各 README 的真实数据命令逐策略验收。08 先修复匹配行情覆盖，09 先确认公共工厂及修复测试已同步。不要通过跳过缺价检查、使用昨日报价或强制一手来让样例表面跑通。

## 10. 相对旧交接必须更新的认知

| 旧说法 | 当前事实 |
| --- | --- |
| 每个策略自行 local_input | 公共文件准备已迁移到 dataprep |
| option_delta 仅选约，没有交易 | 已实现 Call + Put 同时买入、换仓、退出及交易报表；新版本完整运行仍待验证 |
| opt_basic 无 tick，需 --option-tick | 已有 tickNum，按条款读取，不再要求手工指定 |
| 期权入口必须传 --bar-timestamp | 当前默认 end，源数据为 start 时才显式改变 |
| 黑色系专用策略 | 已改名 02_sector_chain，品种组合可配置 |
| 跨期策略另有 near 合约 | 当前两腿 leg1=secondary、leg2=far；旧参数只作兼容 |
| 所有复权因子自算或默认 1 | 01/02/04/06 读取外部主力累计因子；05 的三角色研究有独立因果构造 |
| 只有 option_basic，无 future_basic | 当前两者模型、Provider、文件适配和导出已对齐 |
| 新公共输入代表实盘也统一完成 | 第一阶段输入整理已推进，第二阶段统一实盘运行尚未完成 |

本文件记录的是优化前可交接状态：01–07 有最新样本运行证据；08 输入缺口与09新交易链验证仍待闭环；性能分析已确定阶段和疑点，尚未完成函数级归因。

### 2026-10-08补充：PF2026100807共同组装接口

用户授权将01回测及实盘改为相同的积木组装步骤。新增trader/assembly.py与execution/builders.py；01两个入口显式声明数据绑定、执行路由和共享仓位，策略文件不变。run_backtest支持原直接脚本及规范模块导入，dotenv只在main运行时读取；历史输入、裁剪、因子trading/aligned、行情先撮合后策略及报表行为保留。实盘新增ManagedLiveRuntime与CTP控制器及报告，日期、健康／授权和停机规则按在线模式保留。

公共模块直接从各自路径导入，基础Runner、RuntimePort、DirectLiveRuntime、UnifiedHistoricalRuntime及原公共包导出未改；其他demos尚未批量改入口。新增历史事件顺序和当前EMA回测组装测试，整体运行待用户远程验证。本轮不等同于回测性能优化或全部在线通道已统一。共享文件及受影响另一工作线需按[PF2026100807协调记录](../coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100807-回测与实盘采用一致的组装结构)整组同步，执行[五组验收命令](../../../demos/01_main_ema/SIMNOW_ACCEPTANCE.md#统一组装结构pf2026100807)并用原通过样本对比回测报告；当前对话已通知，跨任务待同步。

随后远程报告main-ema-acceptance-1791463560272015358五组全部通过：assembly10、routing10、CTP7、entry38共65项单元测试及原runtime委托探针退出0，新增公共模块和两入口摘要一致。公共组装、当前EMA历史组装（内存Backend）及完整假在线入口已有通过证据；本轮只更新文档，源码未再改，不重复五组。原完整Nautilus回测样本报表对比、其他demo接入和重构后真实网络验证仍独立待完成，跨任务待同步。
