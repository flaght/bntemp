# Demos 公共输入层第一阶段实现交接

2026-10-05 命名调整：公共包由 `inputs` 改为 `dataprep`，九个 demo、测试导入与文档链接已同步；测试文件改为 test_dataprep_common.py、test_dataprep_role_scene.py。内部接口和报告格式保持兼容，诊断 schema_version 仍为 inputs-v1。迁移前源码快照保持原样。本次仍仅编辑文件与静态文本核对，未执行代码或测试。

日期：2026-10-04。对应方案：[公共输入与运行层设计执行方案](DEMOS_INPUT_RUNTIME_DESIGN_2026-10-04.md)。本轮完成第一阶段代码生成和九个 demo 的输入调用迁移；按照用户要求，不执行生成代码，也不运行导入、语法检查、测试、help、回放、撮合或任何外部连接。

因此本交付状态为“代码已生成，运行验收待完成”。下表的完成指代码落盘，不表示方案中的验证通过条件已经满足。阶段二的运行组装与 run_live 尚未开发。

## 工作包交付

| 工作包 | 代码交付 | 本轮证据 | 待运行验收 |
|---|---|---|---|
| A0 基线 | 迁移前源码副本与 SHA256 清单 | 原始 demos、相关 Parser 已留存 | 原生环境的输入事件及输出基线 |
| A1 契约路径 | dataprep/contracts.py、paths.py、session.py | 文件与调用点静态核对 | 优先级、按需校验、路径来源报告 |
| A2 行情 | catalog.py、bars.py，公共 PreparedFrameReader 与 Parser 可用时间映射 | 九入口不再直接读原始 Feather 或重复扫描 | start/end、单位、夜盘、身份、坏值、缓存及内存 |
| A3 条款日历 | metadata.py、calendar.py、trader/instrument_factory.py | 参数及旧工厂逻辑静态比对 | tick、乘数、生命周期、期权条款、日历范围与原生类型 |
| A4 固定合约 | schedules.py、scenarios/fixed_contracts.py，目标计划与跨期消费 Bundle | 输入准备与执行组装职责分开 | 独立时钟、计划内容、双腿及旧组合缺行情 |
| A5 角色 | references.py、scenarios/role_futures.py | 日终锚点与分钟 close 区分，复用现有 Store | 因果因子、角色发布时间、实际事件同步 |
| A6 期权 | scenarios/option_chain.py，研究和执行字段政策 | Delta 保留候选坏值，Vega 执行严格 OHLCV | 候选全集、DTE、同期价格、Greeks、清仓 |
| A7 迁移 | 九个 demo 的薄 local_input 与公共输入调用 | 策略核心文件除角色常量导入外保持原文 | 每个策略的输入和业务输出对照 |
| A8 文档 | dataprep/README.md、本记录、九 README 补充、28 个新契约夹具方法 | 所有内容已保存 | 验收记录、性能数据、输入不变性签收 |

原始源码保存在 `doc/source_snapshots/input_stage1_20261004/`，清单为 `doc/source_snapshots/input_stage1_20261004.sha256`。这是用于审阅和对照的静态副本，不是回测结果。当前 pro 目录没有 Git 仓库，未创建提交。恢复时只按清单逐文件复制对应旧文件；新增公共模块与新文档另行处理，避免覆盖后续修改。原始测试文件未包含在该快照内。

## 开发入口和兼容层

详细 API、字段政策、路径优先级和组装示例见 [公共输入开发说明](../../bomber/framework/dataprep/README.md)。

| Demo | 场景及主要迁移 | 保留的策略规则 |
|---|---|---|
| main_ema | 角色研究场景，main，公共真实 Bar 和期货工厂 | EMA、预热、手数、动态主力路由 |
| cross_section | 多品种 main 场景，公共文件清单与真实 Bar | 收益排名、平局顺序、调仓周期及手数 |
| black_sector | 信号角色加执行角色，公共条款和行情 | 两层平滑、领头比较映射、方向及延迟 |
| role_cross | 三角色分钟研究，常量转入 model.py | main/secondary/far 均价穿越，只执行主力 |
| calendar_spread | 公共角色读取后由现有选择规则定双腿，再准备 fixed_contracts Bundle | Z-score、不同合约、缺角色政策、换约清窗及旧仓检查 |
| triangle_spread | 三品种 main 场景，公共文件清单与真实 Bar | 初始归一化、对数价差、权重、金额及退出 |
| scheduled_targets | 公共 CSV 适配加 fixed_contracts Bundle | 独立时钟、REPLACE、错过时点不追单 |
| option_vega | 期权链执行规格，公共条款、日历、原生合约工厂 | 同月可对冲筛选、Vega 配仓、Delta 对冲及退出 |
| option_delta | 期权链研究规格，公共 FrameReader | 先定期限、Call/Put、绝对 Delta 排名，仅研究无下单 |

`local_input.py` 不删除，作为数据需求声明、薄组装和兼容导入路径。calendar_spread 增加同名场景文件，旧 contract_input.py 转发公共能力。scheduled_targets 的 contract_input.py、option_vega 的 instruments.py 同样变成兼容转发。公共代码没有依赖具体 demo 的 model 或 strategy。

run_backtest 仍保留原本的账户、路由、风控、Runner 和报表组装。所有脚本统一以 `demos.<目录>` 引入策略组件，直接脚本模式在导入前将 pro 根目录加入 sys.path。role_cross 的输入与运行组件统一使用项目顶层 market/trader；相同环境的正式启动方式需要后续验收。

## 必须明确的兼容变化

这些是输入契约整理的显式变化，不能在回归报告中标为完全无行为影响。

1. 角色默认文件名改为 fut_contract.feather，仅其缺失时回退旧名；两个默认文件同时存在必须显式选择。main_ema 也接受公共新环境变量和显式目录，不再硬性要求旧 ROLE_DIR/KLINE_DIR 同时存在。
2. 七个期货入口增加 bar_timestamp 参数和 --bar-timestamp，默认 end；源数据为 start 时需明确指定。角色研究和真实执行 Bar 使用同一规格。两个期权入口保留原有显式参数。
3. 公共执行读取增加一致的空文件、重复列、身份、整分钟、OHLC、有限值及整数分钟 volume 校验。此前被某个入口忽略的坏输入可能更早失败。
4. main 场景仅要求 main；其他角色按调用场景声明，不再强制所有角色加载。角色参考的冲突重复记录拒绝，完全相同的所需事实可合并。
5. 带 available_ns 的角色不会提前使用；现有角色 Store 无法提供首个完成分钟的发布后快照时失败。跨期场景拒绝显式角色发布时间，待支持门控后再开放。携带 available_ns 或 source_version 的静态条款也拒绝，避免预加载最终版本产生前视。
6. 有条件推导的交易日历及没有 trade_date 的夜盘文件归属明确写入假设。公共层不具备交易所场次表，不能证明所有夜盘或节假日归属正确。
7. 元数据期权类型与代码不一致改为冲突错误。Vega 仍筛选认购，同月可对冲资格、Delta 静态全集选月规则均留在原业务层。
8. 标准化行通过 Feed.add_source 进入原 Parser；不能重新 add_bar_feather 丢失转换。可用时间映射保持默认 ts_init=ts_event，并提供显式扩展字段；这不表示九策略任意迟到同步已公共化。
9. 成功目录增加三份 input_*.json；覆盖将运行区间与研究依赖日期分开。缺候选继续使用策略自己的审计，不根据文件缺失换月或补价。

## 文件变更清单

新增：

- dataprep/__init__.py、contracts.py、paths.py、session.py、catalog.py、bars.py、metadata.py、calendar.py、references.py、schedules.py、futures.py。
- dataprep/scenarios/__init__.py、fixed_contracts.py、role_futures.py、option_chain.py。
- dataprep/README.md、trader/instrument_factory.py。
- demos/calendar_spread/local_input.py、demos/role_cross/model.py。
- tests/test_dataprep_common.py、tests/test_dataprep_role_scene.py。
- 本文和迁移前源码副本及 SHA256 清单。

修改：

- 九个 demos/<目录>/run_backtest.py 和九个 README.md。
- 既有八个 demos/<目录>/local_input.py；两个 contract_input.py；option_vega/instruments.py。
- role_cross/role_signal.py：仅将 SIGNAL_ROLES 的导入转到 model，常量值不变。
- option_delta/parser.py：复用公共 Reader，携带 available_ns、input_quality 和 zero_volume_bar，保留研究载体行为。
- market/replay/parsers/bar/mapped.py、fixed.py：新增可选可用时间映射，旧参数调用兼容。
- tests/test_demo_option_delta.py、test_demo_option_vega.py：路径解析夹具显式关闭实路径存在检查；Vega 的读文件 mock 改为公共 pandas 读取位置。业务断言保留。
- 设计执行方案补充实施状态链接；原交接文档增加当前阶段入口说明。

## 已做与未做的核对

本轮只通过文件列表、文本搜索、源码阅读与 diff 核对调用点。确认当前 demo 输入兼容层没有独立 Feather 读取或全目录扫描副本，回测入口不再使用 add_bar_feather；业务核心与源码快照逐文件文本对照。没有运行 Python、AST、compile、pytest、unittest、脚本 help 或任何策略。

新夹具共 28 个方法，分为 26 个公共输入契约方法和 2 个真实 DataHub 角色场景方法。覆盖路径优先级、默认文件冲突、文件身份、必需或可选缺失、单次扫描与读取、开始结束时间、时区和单位、延迟、夜盘、坏值、重复分钟、成交量、源变更、Bundle 消费、静态条款、角色需求、日历以及区间外研究依赖。测试尚未执行，不能引用为通过证据。

静态发现的环境前提：当前 `datahub/__init__.py` 引入 `.option_basic`，但本机项目文件列表未包含 `datahub/option_basic.py`。这是已有源树问题，本阶段没有补写该服务或改造 DataHub。运行环境同步前必须核对实际源码包完整性。完整原生 bomber 环境和真实行情同样尚未验收。

## 运行环境验收顺序

以下命令仅供后续开发者参考，本轮未执行。必须在正确 pro 项目与可用依赖环境中运行，不要将测试夹具替代完整原生回放。

```bash
python -m unittest tests.test_dataprep_common
python -m unittest tests.test_dataprep_role_scene
python -m unittest tests.test_demo_option_delta tests.test_demo_option_vega
pytest tests/test_demo_scheduled_targets.py tests/test_demo_role_cross_three_roles.py tests/test_calendar_spread_selection.py
```

随后核对九入口直接脚本和模块启动；检查路径来源、分钟标签、资料版本、真实文件日期及条款上市期。输入对比应覆盖 datetime/event_ns/available_ns、真实 symbol、OHLCV、研究 close、角色来源日与因子。start/end 差异必须先映射到同一完成分钟，不以放大时间容差消除差异。

按 scheduled_targets、main_ema、role_cross、calendar_spread、cross_section、triangle_spread、black_sector、option_delta、option_vega 的顺序用相同区间和参数比较迁移前后。交易策略对照目标、换约路由、orders/fills、最终持仓；Delta 对照期限、候选拒绝、决策和选约，不要求交易绩效。

对严格校验新增拒绝的旧样本，判断是否需修复源数据或声明明确规格，记录原因，不删除校验冒充等价。记录目录扫描、读取次数、预加载耗时和峰值内存，再决定是否需要后续内存优化。所有契约与业务对比签收后，才将第一阶段标记为运行验收完成。

## 第二阶段接续边界

下一阶段复用 DataRequirements、InputContext、HistoricalInputBundle、BindingSpec 和 SourceSpec，开发策略定义及公共运行工厂，再分别提供 run_backtest 和 run_live。在线订阅、动态路由切换、帧封口、晚到、恢复和账户协调仍按原设计执行，本轮没有占位 run_live，也没有改动执行与恢复模块。

## 运行环境首次反馈

2026-10-05 用户提供 Linux 运行环境终端输出，项目目录为 `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro`。以下证据来自用户输出，本机未执行或独立复验。

| 测试文件 | 用户运行结果 |
|---|---|
| test_dataprep_common.py | 26 项通过 |
| test_dataprep_role_scene.py | 2 项通过 |
| test_demo_option_delta.py | 31 项执行，1 个错误：resolve_paths 不接受 validate 参数 |
| test_demo_option_vega.py | 21 项执行，2 个错误：resolve_paths 不接受 validate 参数 |
| test_demo_role_cross_three_roles.py | 脚本报告 OK |
| test_demo_scheduled_targets.py | 脚本报告 OK |

本机两个期权 local_input.py 的 resolve_paths 已声明关键字参数 validate，并传递到 dataprep.paths.resolve_option_args；公共解析函数也已接收该参数。因此运行环境加载的函数签名与本机源码不一致，需核对实际模块路径并同步两个期权兼容层及公共 paths.py 后复测。calendar_spread 选择测试和真实行情回放尚未收到运行结果，不能将第一阶段整体标记为验收通过。

## 运行环境回归复测结果

2026-10-05 用户随后提供复测输出：test_demo_option_delta.py 的 31 项测试通过，test_demo_option_vega.py 的 21 项测试通过，test_calendar_spread_selection.py 脚本报告 OK。此前三个 validate 接口错误在本次复测中不再出现。

结合首轮输出，公共输入契约和角色场景共 28 项、两个期权回归共 52 项均已在用户运行环境通过；角色交叉、目标计划和跨期选择三个脚本报告 OK。上述是夹具和规则回归证据。用户随后选择先验收 main_ema，实际进展见下一节；其他策略真实行情回放仍待逐个进行。示例 targets.csv 全部时点覆盖 2026-05-06 至 2026-05-19；当前入口会拒绝请求区间外的计划时点，因此不能使用完整示例计划却仅指定一两天日期。

## 单品种主力 EMA 真实行情回测

后续调整：用户明确已有 pcr_factor（单期）和 pcr_cumfactor（累计），并确认资料已按使用当天对齐。主力 EMA 与黑色板块使用外部累计因子，固定匹配当前交易日，移除必填 --factor-date-basis；默认 aligned 接受无发布时间列的已对齐资料，有 available_ns 时仍校验。新增 dataprep/factors.py 和 tests/test_dataprep_external_factors.py。次主力资料覆盖仍待确认。新模式未执行测试；下面成功记录仅属于此前自计算版本。主力入口相对导入已统一为 demos.01_main_ema。

2026-10-05 用户在 Linux 运行环境完成 main_ema 回测。本地未执行代码，也未读取远程结果文件。逐函数说明已补充到 [dataprep 函数使用说明](../../bomber/framework/dataprep/API_USAGE.md)，场景记录见 [主力 EMA README](../../demos/01_main_ema/README.md)。

| 项目 | 用户提供的结果 |
|---|---|
| 品种及交易所 | RB / SHFE |
| 请求与实际范围 | 2026-01-05 至 2026-01-26，16 个交易日 |
| 行情目录 | /workspace/data/temp_bomber1/ctp/kline/fut |
| 时间标签及策略参数 | end、fast=3、slow=5、quantity=1 |
| 回放 Bar / EMA Bar | 5,493 / 5,493 |
| 订单 / 成交报告行数 | 2,275 / 2,275 |
| unavailable | 0 |
| 最后主力 / 最后目标 | rb2605 / 1 |
| 运行选项 | PYTHONPATH=.、WARNING、no-tearsheet、require-fills |
| 报告目录 | /workspace/worker/pj/Chrono/nautilus_trader_demo/pro/demos/main_ema/results/964d248d-741a-48a4-8ada-fad41b041049 |

运行前先遇到行情路径未配置、缺少 trader.instrument_factory、FixedInstrumentBarParser 不接受 available_nanoseconds。配置数据路径并同步公共合约工厂及 fixed.py/mapped.py 后，本次运行成功。部署第一阶段必须同步 dataprep、trader/instrument_factory.py、两个行情解析器和相关 demo 调用层，不能只同步 demo。

单品种 main 场景已实现并在该样本跑通：公共路径与读取、仅 main 角色需求、历史因子研究、真实 OHLCV 回放、EMA 目标和模拟成交。Sector 容器允许单品种，不代表必须加载板块。当前直接调用 prepare_role_research；DataRequirements 与完整 Bundle 没有被此入口自动消费。

本次不能签收的项目包括跨换月路由和因子、逐笔订单与实际持仓、真实 start 标签、完整交易日历及九策略整体行为等价。三份输入 JSON 由入口写出，但尚未收到内容；不能据终端零 unavailable 推断诊断文件完全无问题。下一步先查报表与输入诊断，再选择确实切换主力的区间测试。

静态发现当前本地目录已改为 demos/01_main_ema，入口仍导入 demos.main_ema.strategy。成功运行证据属于远程旧目录版本，不能移用为改名后入口通过的证据。本次仅记录差异，没有修改代码或执行检查。

### 2026-10-06：03_calendar_spread 三文件整理

当前例子位于 demos/03_calendar_spread，Python 核心文件仅保留 run_backtest.py、calendar_signal.py、strategy.py；删除 local_input.py、contract_input.py、selection.py。选约及历史窗口价差信号在 calendar_signal.py，双腿同步、真实目标及换约清仓在 strategy.py。入口直接组合 dataprep 的 load_role_assignments、plan_fixed_contracts、prepare_fixed_contracts、add_prepared_bar_source、futures.instrument 等能力，并修正旧 demos.calendar_spread 导入和启动路径。

本例保持真实双腿未复权价差及此前交易日角色口径，不读取主力复权因子表。新增数据准备、合约和 Feed 注册、回测、报表及清理阶段耗时。README 以使用方法和按模块统一表格说明组件；tests/test_calendar_spread_selection.py 调整新模块导入并加入窗口预热、因果统计、反向、零方差退出、换约重置及保持方向检查。

仅完成代码与文档生成及文本检查，未在本地执行导入、语法检查、测试或回测；此前选约测试通过记录不能代表本次迁移已验收。运行环境须重新执行测试脚本及真实双腿回测。

### 2026-10-06：04_cross_section 三文件整理

当前例子仅保留 run_backtest.py、cross_section_signal.py、strategy.py 三个核心 Python 文件，删除 local_input.py 和 cross_strategy.py。入口修正旧目录导入，直接调用 prepare_role_research，并沿用已确认的当前交易日外部主力累计因子口径，symbol 冲突以因子表为准。真实主力及换约日旧主力通过固定合约 InputPlan / HistoricalInputBundle 严格准备后注册到 Feed；不再使用例子内输入转发层。

收益率窗口、排名和真实价格整数手数抽取到纯信号模块，动态主力策略保留完整帧同步、调仓节奏、最强多 / 最弱空及旧目标归零规则。不再保留该入口未使用的固定标的策略副本；examples/cross_section 中的独立实现未修改。入口新增分阶段耗时、统一 dotenv 路径用法及参数验证；README 更新启动命令和按 dataprep / datahub / market / trader 分组表格。

新增 tests/test_demo_cross_section_signal.py，覆盖窗口预热、收益率排名、并列排序、因子抵消换约跳变、无效帧及真实价格数量换算。仅生成代码并进行文本检查，未执行导入、语法检查、测试、help 或回测；等待部署环境验收。

### 2026-10-06：04_cross_section 多空分组与每侧预算

用户确认横截面应对一组品种做多、另一组做空。当前实现改为 --group-fraction（默认 0.30，上限 0.5），每侧入选数量 max(1, floor(当前品种数 × 比例))；排名前段做多、后段做空，中间品种及历史合约目标为零。分数相同仍按品种代码稳定排序，保持明确、不重叠的分组。

--target-notional 的语义由每个入选品种金额改为每侧总预算，默认多空侧各 100000，组内等额分配。目标按真实价格和乘数向下取整，取消最少一手：不足一手目标为零，余款不重新分配。分配预算相等不保证整数取整后的实际多空金额完全相等。策略事件记录两侧入选品种、品种目标、分组比例和每侧预算；summary 标记 per_side 口径及分组数量。

测试脚本增加多品种分组、两侧预算、余款、不足一手、比例边界和不重叠检查；仅更新代码、中文注释及文档，未执行测试或回测。

### 2026-10-06：05_role_cross 三文件整理

当前目录仅保留 run_backtest.py、role_signal.py、strategy.py 三个核心 Python 文件。SIGNAL_ROLES 移入信号模块，删除 model.py 与 local_input.py；入口直接调用 prepare_role_research 的单品种分钟角色场景，再用固定真实合约输入计划准备三角色及换约旧主力严格行情，注册已校验行到回放源。修正旧 demos.role_cross 导入，补充分阶段耗时、中文注释和模块表格文档。

保持主力、次主力、远期研究价穿越、仅主力逻辑目标执行、完整同分钟实际事件门控和动态路由换约规则。因子仍使用各角色自己的因果换约比值，不使用只有主力口径的外部因子表；此处未变更信号或因子业务规则。目标手数补充整数校验，配置改为显式必传，避免原先无参数配置构造失败。

tests/test_demo_role_cross_three_roles.py 更新新目录信号导入，增加不穿越保持目标及重复时间戳去重检查；保留四角色 DataHub 默认兼容测试。本机只完成文件生成与文本检查，未运行导入、语法检查、测试或回测，需运行环境验收。

### 2026-10-06：06_triangle_spread 三文件整理

目录仅保留 run_backtest.py、spread_signal.py、strategy.py 三个核心 Python 文件，删除 local_input.py 并修正旧 demos.triangle_spread 导入。入口直接调用 prepare_role_research，三个品种主力信号使用当前交易日外部 pcr_cumfactor，同角色合约冲突以因子表为准；真实主力与换约日旧主力经固定合约输入计划严格准备后注册到回放源。

归一化基准和历史窗口移入 TriangleSpreadSignal，保持此前窗口评价当前帧、三腿方向、对冲权重、调仓间隔及换约连续窗口规则。执行手数按真实价格和乘数计算，任一腿不足一手仍报错，未套用横截面场景的跳过不足一手规则。补齐中文注释、阶段耗时、因子参数、摘要和按模块表格说明。

新增 tests/test_demo_triangle_spread_signal.py，覆盖加权归一化、预热与历史窗口因果性、入场退出反向保持、零方差和固定基准、无效帧拒绝。本地仅生成代码与文本检查，未执行导入、语法检查、测试或回测，需在运行环境验收。

### 2026-10-06：07_scheduled_targets 三文件整理

目录仅保留 run_backtest.py、schedule_signal.py、strategy.py 三个核心 Python 文件，保留 targets.csv 示例和 README，删除 local_input.py 与 contract_input.py。入口直接调用 dataprep 的目标 CSV、真实合约条款和固定合约输入计划能力，修正旧 demos.scheduled_targets 导入，已准备行情直接注册到独立时钟回放源。

计划时点判断、错过不追单、重复时钟去重和结束审计移入 ScheduledTargetSignal；策略仅提交完整组合 REPLACE 目标。保持同时间戳先行情后计划、固定真实合约及报表写完后检查遗漏的规则，不引入主力角色或复权因子。新增阶段耗时、中文注释和按模块表格说明。

tests/test_demo_scheduled_targets.py 直接测试公共 CSV 读取及新信号模块，增加精确触发、提交确认、重复时钟、倒退拒绝、错过与未到达、审计幂等检查。本地仅生成代码和文本核对，未运行导入、语法检查、测试或回测，等待部署环境验收。

### 2026-10-07：08_option_vega 三文件整理

当前目录仅保留 run_backtest.py、vega_signal.py、strategy.py 三个核心 Python 文件。原条款、配置、日期规则和数值函数合并到 vega_signal.py，同月期货对冲资格规则一并移入；删除 model.py、option_math.py、local_input.py、instruments.py 和空包标识文件。入口直接使用 dataprep 的条款、目录、日历、路径及期权链 InputPlan / HistoricalInputBundle，合约构造直接调用 trader.instrument_factory，已准备行情经 add_prepared_bar_source 注册。

保持卖出虚值认购、近期限选择、有限期波动率缓存、Vega 整数预算配仓、实际成交仓位 Delta 对冲、在途等待、到期前及末日清仓规则，指数仅作为信号行情。新增各数据、注册、回测、报表和清理阶段耗时，更新中文注释及按模块表格文档。未引入主力角色或外部复权因子。

tests/test_demo_option_vega.py 更新三文件结构和公共输入接口导入，保留原有数值、日历、路径、资料与策略目标夹具。本地仅生成代码和核对文本，未执行导入、语法检查、测试、help 或回测；此前 21 项测试通过记录不代表本次迁移已验收。

### 2026-10-07：08_option_vega 从基础条款读取 tickNum

opt_basic 已提供 tickNum，公共 load_options_basic 优先读取该字段作为每个期权合约的最小变动价位；字段缺失或为空时兼容 minChgPriceNum、price_increment。非空值必须为有限正数，无效值直接报错；tickUnit 仅说明单位，不参与数值读取。

Vega 入口删除 --option-tick 和 OPT_PRICE_INCREMENT 读取，load_contracts 不再接收手动价位参数。公共读取器的 tick_override 接口仍保留给其他调用方兼容，本例不再传入。README 同步说明，测试增加 tickNum 优先级、旧字段兼容和无效值拒绝用例。本机未执行测试或回测，需在运行环境验收。

### 2026-10-07：09_option_delta 三文件整理

目录仅保留 run_backtest.py、delta_signal.py、strategy.py 三个核心 Python 文件。合约与参数、自然年化期限、BS/Black76 定价和 Delta 选约合并到 delta_signal.py，研究快照解析器移入 strategy.py；删除 local_input.py、model.py、selection.py、option_math.py、parser.py、__init__.py。修正旧 demos.option_delta 导入，兼容直接脚本与数字目录包导入。

入口直接使用 dataprep 的路径、条款、目录索引、BarFileKey / BarReadSpec 和期权链 InputPlan / HistoricalInputBundle。期权使用 research_audited 保留无效价格及流动性字段供拒绝审计，指数和同月期货使用 close_strict；准备后的快照通过 PreparedFrameReader 消费，不重新读取原文件。不能把研究快照当作严格交易 Bar。

保持完整静态期权全集期限选择、同分钟价格、分别选 Call/Put、Delta 核心区间与回退、缺目标月行情不跨月补腿以及 SELECT_ONLY 模式；没有新增配仓、对冲、开平仓或执行路由。tick 优先读取 opt_basic.tickNum，不再接收 --option-tick 或读取 OPT_PRICE_INCREMENT；分钟标签默认 end，仍可显式选择 start。新增 dotenv 路径加载与阶段耗时，README 按使用方法和 dataprep / datahub / market / trader 表格整理。

tests/test_demo_option_delta.py 更新新目录、信号/策略/解析器及公共输入接口引用，回放夹具验证 InputPlan 准备结果仍保留缺行情月份拒绝规则，基础条款夹具改用 tickNum。仅修改代码并进行文本检查，未执行导入、语法检查、测试、help 或回放；旧版本 31 项通过记录不代表本次迁移已验收。

### 2026-10-07：09_option_delta 从选约回放升级双腿交易回测

用户确认需要完整交易，并选择买入 Call/Put 双腿，授权继续按建议规则实现：每腿默认 1 手，完整双腿才开仓，每日选约更新组合，先平旧组合并等待实际成交再开新组合，临近最后交易日默认 2 个自然日及回测末日默认 14:50 清仓，暂不进行期货对冲。PARTIAL/NO_SELECTION 保持已有组合；重复同组合不累加数量。

使用严格 OHLCV 加持仓量准备期权交易行情，保留可能持有合约退出开仓期限窗口后的后续行情。SnapshotParser 先发一次原生 Bar 供市场适配器推进模拟，再发 CustomBar 供选约，不用替代价格或重复行情撮合。Black76 同月期货只作为定价信号，不注册执行路由。公共 make_option 增加显式 C/P 类型及报告 profile 标识，原 Vega 默认调用保持认购及原 profile。

接入模拟 Backend/Profile、PositionManager、净目标规划、执行客户端、报价与风控、执行路由及 UnifiedHistoricalRuntime。增加 orders/fills/positions/account/signals、可选 tearsheet 和 BUY_PAIR 摘要，保留 selections/decisions/candidates 及公共输入报告。缺报价时保留真实仓位等待；订单拒绝/取消/过期停止。报告保存后检查残留仓位、在途、执行回报、清仓触发、选约时点和可选成交要求。

更新中文文档、参数和测试夹具，增加双腿买入、部分选约不交易、目标替换、先平后开、在途等待、陈旧报价等待、到期/末日清仓、结束不补单、Call/Put 合约身份及一次原生 Bar 推进检查。本机未执行导入、语法检查、测试、help 或回测；需运行环境验收。本条取代前条当前 SELECT_ONLY 行为描述，历史选约规则继续保留。
