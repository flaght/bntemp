# 单品种主力 EMA 回测

## Recording仅接行情（2026-10-16）

`--mode recording`不创建TD、不占用账户锁、不查询资金/委托/柜台持仓，
只连接所选行情源并记录策略目标。CTP行情仍使用`.env`中的账户ID和密码登录MD。
须显式填写`--expected-trading-day`作为参考交易日，实际MD交易日由健康闸门核对；
账户对账仅允许省略或`--account-reconciliation off`。报告中真实账户仓位与TD交易日为null。
历史预热、因子审计、行情时效及目标验收继续执行。`--mode simnow`保留原TD查询、账户锁及报单门控。

## 当前启动方式：独立策略向 SimNow 报单（2026-10-14）

当前需求是：**独立进程只启动01_main_ema，直接向SimNow报单，只按自己的归属成交管理策略仓位**。此前Model解释已纠正，Model只模拟成交，不符合当前真实SimNow报单需求。直接运行python -m demos.01_main_ema.run_live，只装配一个MainEmaStrategy，不启动其他demo、共享账户母进程或共享worker。

“无物理账户关联”在这里表示**不将柜台持仓关联/同步到策略账本**，不表示无需交易账户。真实报单仍连接CTP TD，查询资金、关联本策略订单与成交，并保留账户锁和执行门控。

| 配置 | 当前选择与作用 |
| --- | --- |
| --mode simnow | 使用原生CTP TD向SimNow提交订单，接收真实柜台回报 |
| --strategy-position-source fills | 本策略仓位只由本策略已归属的真实成交更新；提交目标/报单不等于成交 |
| --account-reconciliation off | 不查询柜台持仓来初始化或校准策略账本，不自动接管其他仓位 |
| --enable-orders --confirm-simnow | 显式启用SimNow报单；仅connect不足以启用报单 |
| 独立state-file | 保存本策略归属、订单关联和执行账本；不复用Model/其他策略文件 |
| --product "$EMA_PRODUCT" | 从参考资料解析所选品种当前主力；本次AU，不固定某个月份 |

### 完整启动命令

在已安装Bomber及CTP MD/TD依赖的环境中、pro根目录执行，连接配置沿用项目.env。用户最近一次AU MD实际返回20261015，故本次声明交易日20261015和资料来源日20261014；MD/TD必须均匹配，来源资料须先只读核验。换会话时按实际结果更新日期，不直接使用自然日期。以下命令通过只读后会实际启用SimNow订单。

```bash
export PYTHONPATH=.
EMA_PRODUCT=AU
EMA_TRADING_DAY=20261015
EMA_SOURCE_DAY=20261014
EMA_STATE_FILE="demos/01_main_ema/state/simnow/${EMA_PRODUCT}_main_ema_${EMA_TRADING_DAY}_fills_off.json"
EMA_REPORT_DIR="demos/01_main_ema/results/standalone_${EMA_PRODUCT}_simnow/${EMA_TRADING_DAY}"
mkdir -p demos/01_main_ema/state/simnow

python -u -m scripts.integration.reference_data.readonly \
  --connect --products "$EMA_PRODUCT" \
  --database dfs://bomber_daily \
  --trading-day "$EMA_TRADING_DAY" \
  --expected-source-day "$EMA_SOURCE_DAY" \
  --factor-date-basis source --factor-availability observed-on-read &&
python -u -m demos.01_main_ema.run_live \
  --connect \
  --mode simnow \
  --product "$EMA_PRODUCT" \
  --market-source ctp \
  --simnow-environment realtime \
  --strategy-position-source fills \
  --account-reconciliation off \
  --reference-source dolphindb \
  --reference-database dfs://bomber_daily \
  --factor-date-basis source \
  --factor-availability observed-on-read \
  --expected-trading-day "$EMA_TRADING_DAY" \
  --expected-source-day "$EMA_SOURCE_DAY" \
  --state-file "$EMA_STATE_FILE" \
  --report-dir "$EMA_REPORT_DIR" \
  --fast 3 --slow 5 --quantity 1 \
  --max-notional 3000000 \
  --max-session-orders 4 \
  --history-minutes 0 \
  --seconds 600 \
  --enable-orders --confirm-simnow
```

首次启动要求state-file不存在，策略归属初始为0；已有柜台持仓不会自动归给本策略。若该策略已有真实成交和状态，使用原SimNow文件加--resume恢复同一交易日、同一合约和相同执行政策，不能另建文件把已有策略仓位重新当作0。新文件已存在时先核对其身份，不删除或覆盖来绕过校验。Model文件不能用于SimNow恢复，账户既有仓位也不能在off下通过adopt-existing-position接管。

本命令使用当前实时完整Bar完成3/5根EMA预热，不读取DolphinDB历史Bar；数据库仍提供角色、因子及条款。单次运行600秒、最多4笔报单、一手目标；停止时撤销本策略活动订单并保存自己的状态，**不自动平掉已成交仓位**。不能靠off绕过资金、活动订单、会话健康、报单授权或账户锁；同账户多个独立TD写进程仍不能并发启动。独立成交账本不是柜台对同一真实合约的逐策略物理隔离。

### 如何核对真实报单与自己的仓位

报告应为mode=simnow、strategy_position_source=fills、account_reconciliation=off。查看orders_submitted、订单受理/成交回调、strategy_position和last_target；策略仓位必须由实际已归属成交推进。account_position=None/final_gross=None表示本政策未查询物理持仓，不代表账户空仓。出现“独立策略执行，无物理账户关联”或execution_origin=model、model_reports时，是此前的Model运行，不能当作真实柜台成交证据。

本启动路径不运行scripts.integration.shared_account.run/server/continuous，也不传入account_routes/shared_service；只管理自己的目标、订单关联、成交和持久化账本。当前AU属于已支持的SHFE入口；IF原生SimNow报单限制仍保留，不能把本命令的product改成IF来绕过该限制。

### AU 前次Model失败反馈（保留历史证据）

前次命令为AU Model，声明交易日20261014/source20261013，实际MD为20261015，启动被MD_TRADING_DAY_MISMATCH拦截；bars_used=0、model_reports=0、策略仓位0、cleanup_errors=[]，报告standalone_if_model/model-1791989464943840945。该结果只证明日期门控生效。保留if_main_ema.json及之前Model状态，不跨交易日resume，也不改名用作SimNow状态。此前source20261013加载成功不能证明本次source20261014已经发布，需按上面的只读结果判断。

本节按当前run_live、执行政策、CTP构建及生命周期静态核对，命令已做Shell语法检查；仅更新文档，未自行启动策略、连接数据库/柜台或报单。本次fills/off真实SimNow报单与成交尚待运行环境验证。后文Model/Recording及strict空仓/接管验收均保留各自模式，不作为当前启动命令。

### 会话 Provider 与旧日历清理（2026-10-13）

Live 的会话资料现在由 DolphinDB 时段表、`calendars` 日期 Provider 和独立日期例外组成。实际时段源为 `dfs://bomber_static/fut_trading_session`，文件源为资料目录中的 `fut_trading_session.feather`；DolphinDB 边界处理 `Code → code` 并用 `string(start_time/end_time)` 返回时钟值。正式历史预热、持续运行及恢复预检使用同一服务，默认不读取日历 JSON。品种与时段键、日历标识的绑定仍须明确配置。

三个旧 demo 日历 JSON 已移除。IM/IF 单元测试改用自建内存快照，JSON 兼容测试自建临时输入。集中验收入口已清理旧文件摘要引用，公共 `--trading-calendar` 保留为外部旧配置的显式兼容入口。

原 CU 日历的日期集合已与 `china.sse` Provider 对照一致。原有 **7 条节前停夜盘例外**完整迁移至 [标准例外 CSV](../../scripts/integration/reference_data/configs/session_overrides_cu_2026.csv)，适用的原配置范围为2025-12-31至2026-12-31。它是原资料迁移，不代表本次重新核验交易所公告，也不证明其它品种或年份的例外完整性。`NIGHT_START/CANCEL/NIGHT` 会同时取消午夜前后两段；普通日盘保持。

本地使用时将标准CSV导出为新的Feather文件，不需要 manifest：

```bash
python -u -m scripts.integration.reference_data.session_overrides_export \
  --input scripts/integration/reference_data/configs/session_overrides_cu_2026.csv \
  --output tests/results/cu-session-overrides-2026.feather
```

回测使用时段Feather和例外Feather配套读取。Live 部署将这些标准记录导入实际例外表，源字段 `code` 对应 DolphinDB 的 `Code`，`date` 应为 DATE；然后配置 `--session-overrides-table` 或 `DDB_SESSION_OVERRIDES_TABLE`。本次文件清理没有连接数据库或创建远程例外表。Live 会话参数为 `--calendar-id china.sse --trading-hours-database dfs://bomber_static --trading-hours-table fut_trading_session`，再指定实际例外表。已知有停夜盘规则的范围不能声明 `--no-session-overrides`；明确核对的无例外局部验收范围仍可使用该声明。

2026-10-13 用户远程 CU 时段对照已通过：5个分段、10月12/13日的10个实际窗口、最近600个分钟时间点及930个区间分钟时间点在DolphinDB与Feather两侧一致；周五夜盘和周六凌晨归属10月12日。该验收使用明确的无例外声明；分钟枚举不等于读取了600根历史行情。第二套replay Recording使用接收时间Bar实时预热，目前没有CU历史分钟源配置。

### IF 主力历史预热样例

#### 2026-10-15：01 共享入口历史预热

01 的历史服务现已接入共享同进程宿主、独立 worker 和宿主单策略重启。
在共享配置中，仅为 `kind=main_ema` 的策略 `argv` 增加以下参数（路径换成部署机实际配置）：

```text
--history-db dolphindb
--history-config /absolute/path/to/ema-history.json
--history-minutes 240
--history-stage recovery
--history-missing-policy fail
```

同时提供该品种已有的会话日历、时段及日期例外参数，格式沿用下方独立入口。
240 是可配置样例，并非固定要求；默认3/5周期最低初始化门槛为5根。
仅指定 history-config 或 history-file、未指定非零 history-minutes 时，使用慢EMA周期作为启动请求根数。
不启用历史参数时仍由实时Bar预热。`recovery`阶段直接读数据库；`preopen`可用文件并由数据库补足。

新启动和共享同日 `--resume` 都先恢复账户归属及目标版本，再重新预热指标；历史只更新EMA，
不重放旧目标。行情先订阅并缓冲，历史准备和健康复核完成后才开放实时决策。
单策略独立入口的单次 `--resume` 仍保留原限制；共享恢复由账户服务负责仓位与订单。
共享宿主summary每个策略及worker summary增加 `history`、`history_audit`、`bars_used`，
应核对实际 `sources` 含 `dolphindb`、条数及缺口，不能只看配置backend名称。

2026-10-15用户确认：`shfe_1min`、`cffex_1min`、`index_1min` 的 minTime 为已完成分钟标签。
历史配置使用 `time_label=end`：例如21:01转换为21:00:59.999999999，不再按起点多移一分钟。
对应CU原始分钟配置为 `history_cu.json`，行情库 `dfs://min_bar/shfe_1min`，只读原始OHLC。
用户随后确认该表目前只有AU，实际历史预热验收改用 `--product AU` 和 `history_au.json`；
CU配置仅保留字段模板，不表示库中已有CU数据。AU主力合约及因子由参考服务自动解析，
不固定为截图中的au2612；历史窗口仍须覆盖运行时实际主力。
不映射不存在的adjusted_close；01默认按会话交易日自动选择角色来源日及固定合约累计因子，
计算close×pcr_cumfactor。夜盘、跨午夜、周末归属由同一会话日历确定，不手写逐日映射。
因子日期口径与入口factor-date-basis一致；source使用交易日前最新角色资料日期，trading使用交易日。
新自动模式是Live指标初始化，审计标记current_read_indicator_warmup_only，
不将当前读取的历史因子版本宣称为历史时刻可见版本；实际available_ns仍门控，缺失/冲突不兜底为1。
显式factor_source_days旧配置仍保留旧处理。下方IF限定日期样例不能当作CU配置。
本次共享回归使用受控传输及历史Provider，不代表真实DDB/CTP接续已验收。

IF已接入CFFEX单次realtime Recording观察，省略`--expected-instrument`时采用当前参考适配器解析的main真实合约；可显式传入该参数核对合约。主力/因子覆盖规则沿用现有实现，未在本次修正。IF不接受SimNow报单、持续模式或单次交易恢复；Model仍不装配历史参数。

原入口已有运行期`EMA分钟进度/策略目标`，但历史加载只有允许不足时打印。现增加`EMA预热配置`、`历史预热/回补开始`、`完成`和`失败`日志，包含请求根数、实际来源、缺失及不可用来源、EMA初始化状态/数值、有效根数和耗时。未启用历史时，记录每根有效实时Bar的最低预热进度及完成；回测默认不打印。没有缺口的逐分钟历史检查不打印。

`history_*.json` 配置行情库、字段及历史复权口径，交易时段表不能替代它们。`history_im_raw.json` 是原始分钟源配置；两份含日期的IF/IM配置是限定日期的验收样例，不能原样用于新的会话日期，也不能作为CU行情配置。三份现有配置的time_label已按用户确认更正为end；旧start口径的证据不自动等同更正后的接续通过。

以下IF历史配置供**2026-10-13日盘样例**使用，历史分钟复用已声明的`dfs://min_bar/cffex_1min`，因子读取`dfs://bomber_daily`。配置声明10月12日分钟使用10月9日因子、10月13日分钟使用10月12日因子；这些是请求口径，不代表本次数据库已经核验到这些行。当前主力必须在历史窗口的各因子来源日具有唯一匹配因子，否则拒绝；不拼接跨主力连续行情。不同交易日运行应更新历史因子来源映射和命令日期，交易日期覆盖由Provider提供。

同步本次文件后，先在已安装Bomber依赖的远程pro根目录执行离线验证（`im-runtime`现含3项IF增量，合计9项）：

```bash
export PYTHONPATH=.
sha256sum -c doc/evidence/checksums/EMA_SESSION_PROVIDER_CLEANUP_20261013_SHA256.txt &&
python -u -m tests.run_main_ema_acceptance \
  --only trading-hours --only im-runtime
```

通过后，在同一环境、项目根目录和有效日盘窗口执行样例命令；`.env`中的CTP/DDB连接配置须已准备，并先确认时段表有IF完整分段、本次查询范围没有日期例外。下面的日期与行情配置是保留的历史样例：

```bash
export PYTHONPATH=.
python -u -m demos.01_main_ema.run_live \
  --connect --mode recording --simnow-environment realtime \
  --product IF \
  --reference-source dolphindb --reference-database dfs://bomber_daily \
  --factor-date-basis source --factor-availability observed-on-read \
  --expected-trading-day 20261013 --expected-source-day 20261012 \
  --calendar-id china.cffex \
  --trading-hours-database dfs://bomber_static --trading-hours-table fut_trading_session \
  --no-session-overrides \
  --history-db dolphindb \
  --history-config demos/01_main_ema/history_if_20261013.json \
  --history-minutes 240 --history-stage recovery --history-missing-policy fail \
  --fast 3 --slow 5 --quantity 1 --max-notional 3000000 --seconds 600
```

该命令先读取IF当前主力参考资料，MD/TD健康就绪后从DolphinDB加载最近240根完整交易分钟，历史仅推进EMA；缓冲实时Bar并接续新分钟后生成Recording目标。缺分钟默认拒绝；只在明确接受实际缺口时改成`--history-missing-policy allow`。`--history-stage recovery`明确采用数据库阶段，不传history-file。正常应看到`history_enabled=True`以及完成日志的实际`sources`含`dolphindb`；不能只凭backend配置名称判断已读到数据库。随后summary的history/history_audit保存实际来源和覆盖。Recording不发送柜台订单，但仍连接MD/TD并执行账户预检。

IF实际时段从部署的时段表读取，日期从 `china.cffex` Provider 获取。启动必须先取得健康MD/TD。历史预热启用须有实际分钟源配置，源缺失时拒绝；CU的时段对照结果不代表IF行情已读取成功。源码历史预热和接续的假柜台验收与真实在线验收分别记录。

以下是原生SimNow/strict成交验收口径：使用空仓SimNow账户和新状态文件，不传接管参数。策略初始持仓0，预热后按真实EMA方向生成目标，成交回报更新策略归属。该模式下账户非空会拒绝空仓启动，不自动归给EMA；不适用于本文顶部的独立Model/off命令。PF0907固定目标方案已撤回，相关文件和参数已删除。同步、回归及空仓命令见 [PF2026100908交接](../../doc/HANDOFF/live/HANDOFF_FRESH_EMA_2026-10-09.md)。

## 1. 使用方法

### 启动命令

从 pro 根目录、在已安装运行依赖的环境中执行，先设置 `export PYTHONPATH=.`。入口自动读取已有 `.env` 配置；没有配置数据路径时，可通过可选的 `--data-root` 或各文件路径参数指定。示例日期需与实际数据匹配。

**完整执行（显式设置常用参数）：**

```bash
python demos/01_main_ema/run_backtest.py \
  --product RB \
  --start-day 2026-01-05 --end-day 2026-05-26 \
  --fast 3 --slow 5 --quantity 1 \
  --bar-timestamp end --log-level WARNING --no-tearsheet
```

**默认执行（仅必填参数）：**

```bash
python demos/01_main_ema/run_backtest.py \
  --product RB \
  --start-day 2026-01-05 --end-day 2026-05-26
```

默认执行采用参数表中的默认值及已有数据路径配置；例如默认启用绩效图，不要求必须产生成交。

### 参数说明

“必选”表示启动时必须传入；“可选”表示有默认值或可由已有配置解析。数据路径虽然是可选参数，但运行前必须能解析到有效文件。

| 参数 | 必选 / 可选 | 含义与默认值 |
| --- | --- | --- |
| `--product` | 必选 | 品种代码，例如 RB、I、HC；一次运行一个品种 |
| `--start-day / --end-day` | 必选 | 回测日期范围 |
| `--fast / --slow` | 可选 | 快 / 慢 EMA 周期默认 3 / 5，按有效主力 Bar 计数，要求 0 < fast < slow |
| `--quantity` | 可选 | 多空目标手数绝对值，默认 1 |
| `--bar-timestamp` | 可选 | 默认 end，可选 start；start 由公共层转换为完成分钟 |
| `--bars-dir / --contract-struct / --fut-basic / --factors` | 可选 | 显式覆盖行情、角色表、条款及累计因子路径 |
| `--data-root` | 可选 | 按 role/ 和 kline/fut/ 布局推导路径 |
| `--factor-availability` | 可选 | 默认 aligned，使用已对齐资料；explicit 强制要求 available_ns。已有发布时间仍会校验 |
| `--max-notional` | 可选 | 单笔与持仓名义金额上限，默认 1,000,000 |
| `--require-fills` | 可选 | 要求至少一笔模拟成交，否则报错 |
| `--no-tearsheet / --report-dir / --log-level` | 可选 | 跳过绩效图、指定输出根目录（默认本目录 results/）、调整框架日志（默认 WARNING） |

### 数据要求

需要角色表、fut_basic.feather、fut_adjustment_factors.feather 和每日主力行情；换约日还需旧主力行情。因子字段为 trade_date、code、symbol、pcr_cumfactor，按上游已对齐的当天口径使用；合约冲突时以因子 symbol 为准，不再次累计 pcr_factor。

期货角色表默认查找 fut_contract.feather，缺失时兼容 fut_contract_data.feather；两者同时存在需通过 --contract-struct 选择。基础条款默认 fut_basic.feather，行情为 `<symbol>_YYYYMMDD.feather`。

### 输出内容

默认保存到本策略目录的 `results/<run-id>/`，可通过 `--report-dir` 修改根目录。控制台输出数据准备、运行回测、报表与清理的阶段耗时。

| 输出文件 | 内容 |
| --- | --- |
| orders.csv | 模拟委托及订单状态 |
| fills.csv | 实际模拟成交；提交目标不等于成交 |
| positions.csv | 真实合约持仓记录 |
| account.csv | 模拟账户及资金记录 |
| summary.json | 回测摘要、策略参数、信号或调仓统计 |
| input_manifest.json / input_coverage.json / input_issues.json | 输入版本、覆盖与质量诊断 |
| tearsheet.html | 可选绩效图；省略 --no-tearsheet 且绘图依赖可用时生成 |

### SimNow 接入增量验证

在运行环境中验证当前 `MainEmaStrategy` 与已有 CTP 执行组件的衔接：

```bash
python -u tests/run_main_ema_simnow.py --stage all
```

集中入口按阶段启动独立进程，失败即停止，打印实际 Python、Bomber、策略及公共 Runner 加载路径。单独复测某一模块时，将 `all` 替换为下表中的阶段名；`--timeout` 可选，默认每个阶段或回归脚本 180 秒。

| 阶段 | 验证内容 |
| --- | --- |
| signal | 当前策略的预热、方向、目标去重、合约过滤、角色发布时间及累计因子，共 6 项 |
| routing | LIVE 固定主力路由：默认行为、目标推进和版本、在途及恢复约束、过期目标、行情时间、资料缺失、主力变更闭闸、行情恢复确认及 LIVE 动态路由禁用，共 10 项 |
| ctp | 内存传输下复用原生 CTP Driver、公共限价规划器与受控客户端：LIVE 固定路由验证开仓／平今／反手、最新信号、部分成交／撤单、断线及主力变更关闭授权；HISTORICAL 单独验证跨合约换约，共 7 项 |
| regression | 复用 7 个相关旧回归入口，检查公共修改对既有链路的影响 |

上述验证不连接 SimNow 或发送柜台订单，结果只证明无网络场景。公共 [FixedRoleLiveRunner](../../bomber/framework/trader/live_roles.py) 通过 `add_role_strategy` 装配单策略、固定角色合约的 LIVE 路由。角色、参考源、逻辑目标键及策略处理时间回调显式传入，可供其他单角色策略复用。在新 Bar 决策后推进尚未完成的目标，不增加策略或组合贡献版本；角色合约变化时关闭授权、请求撤单并标记需恢复，资料不可用时暂停目标推进。LIVE 动态换约限制继续保留。

当前目录中，`strategy.py` 定义策略，`run_backtest.py` 组装回测，`run_live.py` 是唯一实盘组装文件。原 `live_references.py`、`live_runner.py` 已删除：文件／数据库参考适配由公共 [FileRoleReferences／SourceRoleReferences](../../bomber/framework/dataprep/live_role.py) 提供；运行路由及会话守卫由公共 `FixedRoleLiveRunner／SessionRoleLiveRunner` 提供。PF2026100807进一步将在线生命周期移入公共Runtime，两个入口统一使用执行组件与绑定声明，策略文件保持不变。

PF2026100806修正版远程验收已通过：references30/30、routing10/10、CTP7/7、entry35/35，共82项，报告main-ema-acceptance-1791458768127640128。这是PF2026100807重构前的证据；新版本按[本次组装迁移清单](SIMNOW_ACCEPTANCE.md#统一组装结构pf2026100807)同步并远程复验后，再使用第二套Recording和受控模拟订单命令。实际柜台成交与最终对账仍待验收。

2026-10-08第二套MD原始／标准行情及TD只读已远程通过。显式`--simnow-environment replay --replay-md-trading-day YYYYMMDD`以Tick实际接收时间聚合Bar，保留原始Tick时间；MD／TD分别固定各自会话日，资料按实际TD日／当前可用时间加载。默认realtime仍用事件时间和MD／TD同日检查，请求120秒时效及执行门控保留。迁移前entry34/34及真实第二套Recording已通过：832条Tick、9根Bar、LONG 1手目标、报单0，停机总仓空且无活动订单。历史失败修正见[单实盘入口迁移](SIMNOW_ACCEPTANCE.md#单实盘入口迁移pf2026100806)。

### 回测与实盘的共同组装方式（PF2026100807）

两个入口都遵循：准备输入 → 执行组件 → 同一个MainEmaStrategy → StrategyBindings → Runner → Runtime.run/stop。参考服务负责查询角色和因子，执行组件负责共享仓位及真实参考价，绑定声明负责决定数据进入哪个策略、目标进入哪个执行通道。

| 积木 | 回测选择 | SimNow选择 |
| --- | --- | --- |
| 参考服务 | prepare_role_research生成as-of快照，文件trading/aligned | FileRoleReferences或SourceRoleReferences；数据库默认source/source-day-end |
| 行情 | FileReplayFeed，标准真实合约Bar | CtpLiveDataFeed + TradeTickBarFeed；第二套显式选择接收时间Bar |
| 执行构建 | build_simulation_execution + 模拟Backend | build_ctp_execution + 原生TD Driver；默认Recording，显式授权后受控执行 |
| 公共绑定 | StrategyBindings + assemble_strategy | 同左；固定角色额外声明RoleGuard |
| Runner | UnifiedStrategyRunner(HISTORICAL)，动态路由 | SessionRoleLiveRunner，固定本次角色及条款 |
| Runtime | UnifiedHistoricalRuntime，保持行情撮合先于策略 | ManagedLiveRuntime + CtpSessionLifecycle |
| 结果 | 回测CSV、摘要与可选绩效图 | LiveRunReport、目标／执行回报、停机权威查询 |

公共入口：[绑定声明](../../bomber/framework/trader/assembly.py)、[执行构建](../../bomber/framework/trader/execution/builders.py)、[在线Runtime及控制器接口](../../bomber/framework/trader/runtime/managed.py)、[CTP生命周期](../../bomber/framework/trader/runtime/ctp.py)、[在线报告](../../bomber/framework/trader/runtime/reports.py)。新组件通过各自模块直接导入；原RuntimePort、DirectLiveRuntime及公共包导出保持不变。

`run_backtest.assemble(...)`完成策略链组装，返回runtime、strategy、client；`run_live.assemble(...)`完成同样的策略链组装，`build_runtime(args)`再注入输入工厂、CTP控制器和报告。构造阶段不连接、不授权、不发单，启动统一交给Runtime。回测入口保留模块及直接脚本运行；实盘继续使用`python -m demos.01_main_ema.run_live`。

CTP控制器负责账户锁、只读预检、最终会话、就绪、授权、持续对账和停机。主力EMA同时持有公共bomber-ctp账户锁及旧bomber-main-ema锁，使其他使用公共CTP控制器的策略和旧入口进程都受互斥约束。输入工厂将数据库close登记到Runtime的ExitStack，读取或组装失败也释放资源；持久化钩子在最终TD启动前安装。停机仍撤单并报告剩余仓位，不自动平仓，也不接管带仓账户或旧状态。

其他单角色策略可替换策略实例、目标键、角色和处理时间回调，使用相同绑定及执行组件。其他在线通道可实现LiveSessionControllerPort并复用ManagedLiveRuntime。Binance专用控制器、LIVE动态换约、多账户及多腿会话不在此次实现范围；不能只更换名称就绕过对应的生命周期要求。

公共变化已在[并行协调记录PF2026100807](../../doc/HANDOFF/coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100807-回测与实盘采用一致的组装结构)登记并在当前对话通知；跨任务待同步。本轮只完成源码与静态核对，没有本机Python／编译／测试结果。

随后远程五组已全部通过，报告main-ema-acceptance-1791463560272015358：assembly10、routing10、CTP7、entry38共65项单元测试及原runtime委托验证退出0，公共新模块／两入口／相关测试摘要一致。当前没有新源码修改，已通过组无需重复。下一步新入口第二套Recording复验，再受控模拟订单；完整Nautilus样本报表对比及真实柜台成交仍单独待验收。日志中的failed摘要属于预期异常测试，不能解释为实际模拟会话失败。

2026-10-07 远程验证状态：Linux `uv-nautilus` / Bomber 1.230.0 环境下，signal 6/6、修正版 routing 10/10（0.007 秒）、修正版 CTP 7/7（0.021 秒）及 regression 的 7 个相关旧入口全部通过。CTP 首轮 HISTORICAL 换约用例因漏掉旧合约行情推进的撤单阶段而失败，修正事件顺序并补充旧仓未平前禁止开新仓的断言后复验通过。合计 23 项增量用例及 7 个相关回归入口已有远程无网络通过证据，可进入下一模块，已通过的阶段无需重复运行。SimNow 行情与实际柜台交易尚未验收。

下一模块复用既有交易端只读探针。在项目根目录、已激活的 `uv-nautilus` 环境中，使用项目 `.env` 或当前环境变量配置的 SimNow TD：

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

该命令执行登录、结算确认和本次仓位／资金／全账户活动订单查询，不发单；读取 `CTP_TD_ADDRESS`、`CTP_BROKER_ID`、`CTP_ACCOUNT_ID`、`CTP_PASSWORD` 及既有认证配置，不在命令中填入凭据。以三项查询完整完成为此模块的验收依据；失败时先反馈完整脱敏输出，不沿用历史柜台快照。通过后继续当前主力 EMA 的实时行情到 Recording 装配，再接受控模拟订单。

通道只读入口已归入 [scripts/integration/ctp](../../scripts/integration/ctp/README.md)，开发时先以 `python -u -m tests.run_ctp_td_readonly` 验证新入口配置定位、三查询与禁单，再连接柜台。该模块命令须在项目根目录运行，使用已同步的 `bomber/__init__.py` 开发加载文件与 `bomber/framework`；只读入口及烟测不要求部署旧 examples 目录，也不在源码注入 `sys.path`。目录调整见[协调记录 PF2026100702](../../doc/HANDOFF/coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100702-通道联调入口归入-scripts-integration)。

当前增量同步以[单实盘入口迁移清单](SIMNOW_ACCEPTANCE.md#单实盘入口迁移pf2026100806)为准，远程须删除两个旧辅助文件。CTP 增量使用公共 `CtpLimitPlanner`，不依赖 examples。原目标推进的变更与历史验证状态见[并行协调记录 PF2026100701](../../doc/HANDOFF/coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100701-保留目标的分阶段执行推进)。

2026-10-08 用户远程运行 `python -u -m tests.run_ctp_td_readonly`，假 API 的配置定位、登录／结算确认、三项查询和禁单全部通过。后续集中验收前8组全部退出码0；entry原17项中14项通过、3项因测试时钟未绑定到受控客户端而失败。在线入口已显式传入时钟回调，保留120秒限制并新增过期请求闭闸验证，修正版18项待以 `python -u -m tests.run_main_ema_acceptance --only entry` 复验。通过后再按接入验收文档运行当前柜台只读及主力 Recording。

随后远程entry复验18/18通过（0.421秒），本轮9组离线模块均有通过证据。真实TD只读查询完成，净仓合约0、CNY资金快照和活动订单0。文件模式Recording在默认trading口径下缺少2026-10-08／RB／main累计因子，尚未连接MD并产生Bar。该失败是历史文件输入证据；原始数据库日期最新澄清为source，不要求补造TD日因子。数据字段、日期口径修正及证据见[接入验收](SIMNOW_ACCEPTANCE.md)。

2026-10-08 实盘入口新增可选 `--reference-source dolphindb`，读取期货条款及此前来源日期限结构／同日累计因子，数据库默认 `--factor-date-basis source`。文件模式默认trading用于已对齐适用日的输入；可显式切换日期口径，回测默认不改。EMA策略代码未修改，条款生命周期仍按TD当前日验证。默认source-day-end可用性，已有available_ns优先门控。字段映射、环境变量、日期修正版验证与远程步骤见 [参考数据库核验](../../scripts/integration/reference_data/README.md)。

PF2026100810：第一套实时SimNow夜盘可显式使用`--factor-availability observed-on-read`配合`--reference-source dolphindb --factor-date-basis source --expected-source-day YYYYMMDD`。该口径从两轮一致的当前数据库读取完成时起使用快照，数据库实际`available_ns`仍会延后放行；默认source-day-end继续保留。回放和文件入口不接受该选项。先按[SimNow验收步骤](SIMNOW_ACCEPTANCE.md)完成离线、只读和Recording，再进行独立受控报单验收。

GAP-03更新：在线默认dolphindb，一次性Recording／simnow须声明按日历核对的`--expected-source-day`；持续模式由各会话日历确定。文件资料测试须显式`--reference-source file --allow-file-reference-test`，不作自动回退。来源年龄默认14自然日、成功观测30秒、单轮刷新30秒，可分别用`--reference-max-source-age-days`、`--reference-max-observation-seconds`和`--reference-max-refresh-duration-seconds`调整；真实available_ns继续门控。代码及远程验收见 [GAP-03交接](../../doc/HANDOFF/gaps/HANDOFF_GAP03_2026-10-09.md)，本机未运行。

## 2. 策略详细说明

### 策略思路与交易对象

这是单品种趋势方向策略：用快、慢 EMA 的相对位置决定持仓方向，信号与交易均对应当前主力。它判断的是当前均线关系，预热结束后即使没有观察到一次交叉，也可以建立首笔仓位。

| 项目 | 具体规则 |
| --- | --- |
| 研究价格 | 当根主力真实收盘价 × 当天 `pcr_cumfactor` |
| 有效事件 | 事件时刻快照可用，合约与交易所匹配当前主力，时间晚于上一有效主力 Bar |
| 信号周期 | `fast`、`slow` 均按有效主力分钟 Bar 计数；不是自然分钟或交易日 |
| 预热 | 慢 EMA 的 `initialized` 为真后才允许提交目标 |
| 多头 | 快 EMA ≥ 慢 EMA，目标 `+quantity` |
| 空头 | 快 EMA < 慢 EMA，目标 `-quantity` |
| 目标更新 | 仅当目标方向变化时重新提交；同方向不会逐根加仓 |

每根有效价格同时更新两条 EMA，指标计算由 Bomber 的 `ExponentialMovingAverage` 实现。缺少可用角色快照时跳过并计数；旧主力 Bar 和重复时间事件不推进指标。

### 仓位、换约与退出

策略提交的是 `rb_main` 等逻辑目标，动态执行路由将它解析到真实月份合约。若实际持仓为 +1 手、目标变为 -1 手，执行层根据持仓差额规划反向交易；策略并非只发一手卖单。

主力切换时保留两条 EMA 和此前方向，通过累计因子保持研究序列的连续口径；执行路由协调旧主力退出和新主力仓位。换约日的旧合约行情用于执行，不参与新主力的 EMA。因子表与角色表冲突时，数据准备层采用因子表合约，避免研究与执行身份不一致。

`strategy.position(config.target_key)` 查询本策略已归属的实际仓位。`on_order(OrderUpdateEvent)` 接收受理、部分成交、全部成交、撤单及拒单等订单状态；订单、账户、在途和归属先更新，再通知策略，订单回调先于对应 `on_fill(FillEvent)`。本例保存 `order_updates_received`、`last_order_update` 和回调内 `last_order_position`，成交回调保存 `last_fill_position` 与 `fills_received`。订单累计成交量属于状态事实，不能在回调中再次累加仓位。

新策略推荐重写 `on_order`。模板默认将它转发给旧 `on_order_update`，既有策略保持兼容；同时重写两者时只自动调用 `on_order`，需要父类行为时显式使用 `super().on_order(event)`。历史换月按旧、新真实合约分别入账，再汇总到同一逻辑目标。在线固定路由下，`account_position` 查询账户净仓；正常单策略会话应与归属仓位一致。在线 `summary.json` 提供 `strategy_position`、`account_position`、`unassigned_position`、订单回调次数及最后状态／订单 ID／原因／仓位、成交回调观测字段。非零未归属差额需核验接管或订单关联，不能当作策略已成交仓位。

本例没有中性区、独立止盈止损或末日强制平仓。方向改变是主要退出及反向条件，回测末尾可能仍持仓。检查结果时应同时查看有效 Bar 数、最终主力、目标方向及 `fills.csv`、`positions.csv`，不能仅凭最终 target 判断成交。

## 3. 使用核心组件描述

### dataprep：输入准备

负责加载和校验文件，把本例需要的资料及真实行情准备好，不计算 EMA、不启动回测。

| 关键接口 | 本例用途 |
|---|---|
| input_session / read_feather | 共享本次运行的读取缓存、文件版本和输入诊断 |
| resolve_futures_args | 返回 DataPaths，定位行情目录、角色表和基础条款 |
| prepare_role_research | 只准备当前品种的 main 信号与执行角色；加载当天累计因子，合约冲突时以同角色因子 symbol 为准，组装 DataHub Store |
| futures.instrument | 校验真实条款，调用 trader 合约工厂，返回原生合约、行情元信息和乘数 |
| BarReadSpec / bar_path / add_bar_source | 定位必需文件，统一一分钟时间标签、严格校验 OHLCV，将准备好的行注册到 market Feed |
| write_input_reports | 写出输入版本、日期覆盖和问题，便于对照交易报表 |

### datahub：参考资料查询

负责保存 dataprep 准备的参考资料，按策略事件时间查询；不回放行情、不生成订单。

| 关键接口 | 本例用途 |
|---|---|
| SectorRoleAssignment | 保存交易日、角色来源日、生效及可用时间、最终真实合约映射和累计因子 |
| SectorRoleStore | 管理日级快照，供策略按时间查询；文件加载及冲突覆盖由 dataprep 完成 |
| snapshot(timestamp) | 查询事件时刻已生效且可用的角色快照 |
| instrument(product, "main") | 从快照取得当前品种的最终真实主力合约 |
| factor(product, "main") | 从快照取得当前主力的累计复权因子，供 EMA 研究价格使用 |
| SectorDataUnavailable | 参考资料尚不可用时通知策略跳过事件并计数，不补因子或生成目标 |

### market：行情定义和回放

负责真实行情的数据结构、解析和文件回放；保留真实价格，复权只在策略计算信号时使用。

| 关键接口 | 本例用途 |
|---|---|
| InstrumentMeta | 描述真实合约的价格精度、最小变动、乘数、币种和交易所 |
| DataType.BAR / Bar | 声明分钟行情类型，向策略提供真实 OHLCV、合约身份和事件时间 |
| FileReplayFeed | 管理并回放 dataprep 注册的数据源；换月当日包含旧、新主力行情 |
| FixedInstrumentBarParser / MappedBarParser | 将准备好的行解析为固定真实合约 Bar，映射事件时间和可用时间 |

当前主力 Bar 用于 EMA，旧主力 Bar 用于换月执行；连接行情与模拟后端的 NautilusMarketFeedAdapter 属于 trader。

### trader：策略运行和模拟执行

负责把行情、策略目标、真实合约路由、仓位和模拟后端连接起来。

| 关键接口 | 本例用途 |
|---|---|
| StrategyTemplate | 策略基类；set_target 提交逻辑目标仓位，例如 rb_main |
| ScheduledContractResolver / DynamicExecutionRoute | 按生效时间映射真实主力，协调换月旧仓处理与新合约目标 |
| PositionManager / NetTargetOrderPlanner | 区分策略归属、账户净仓及在途量，根据目标与有效仓位差额生成订单 |
| PositionAttributionLedger | 冻结订单成交分配、按真实合约更新策略归属，记录内部净额和未归属差额 |
| MarketReferencePriceStore / PreTradeRiskManager | 维护真实参考价，检查手数、名义金额和行情年龄 |
| NautilusMarketFeedAdapter | 将 market 行情连接策略回放与模拟后端；该适配器属于 trader |
| UnifiedStrategyRunner / UnifiedHistoricalRuntime | 注册数据和执行绑定，推进历史运行，管理停止清理 |
| instrument_factory / NautilusSimExecutionBackend / CtpFuturesBasicProfile | 构造原生合约并模拟执行；本例起始资金 1,000,000、每手手续费 1，采用基础保证金模型 |

## 显式接管已有一手仓位（PF2026100901）

本节仅适用于原生SimNow/strict，默认须空仓启动；顶部Model/off不接管柜台仓位。只有指定`--adopt-existing-position cu2611.SHFE --expected-position -1`时，才尝试将该合约的一手空仓分配给当前唯一策略。首版支持SHFE／INE、单合约单向一手投机仓；指定合约须等于本次主力，账户不能有其他仓位或活动订单。`--expected-position 1`表示多仓。入口要求realtime、明确TD日及参考来源日，并要求一手目标。

预检和正式登录后分别做两轮权威查询，检查净仓、总仓、今昨仓、资金及活动订单一致；从原生`PositionCost`和乘数初始化成本账本。缺字段或仓位变化即拒绝。需要重新编译安装TD扩展，不能只更新Python文件。接管不恢复旧EMA和旧订单，重新收集完整分钟Bar预热。

已有空仓时，SHORT目标保持仓位；LONG目标先BUY平今／平昨。平仓全部成交且本地执行账本归零后，后续有效Bar再推进最新目标；目标仍为LONG才开多，不提前发送开仓单。原有重复成交去重、行情健康、参考资料检查和风控继续生效。

建议先用Recording加接管参数验证：不发单，实际空仓保留。离线与Recording通过后，SimNow命令沿用已确认的CU参数，增加：

```bash
--adopt-existing-position cu2611.SHFE --expected-position -1 \
--quantity 1 --max-notional 600000 --max-session-orders 2
```

SimNow还须`--enable-orders --confirm-simnow`及一个不存在的新`--state-file`。两笔预算分别用于平空和开多；平仓未成交或信号一直同向时，不能认定反手验收完成。停机只撤活动订单和报告权威剩余仓位，不自动平掉新多仓。重启需重新核验并声明当时实际方向。


## 公共Live机制（PF2026101033）

01和04共同使用公共环境时间策略、逐合约深度门控、CTP配置、控制线程参考缓存及恢复今昨仓解析，继续沿用ManagedLiveRuntime/CtpSessionLifecycle和公共执行装配。默认realtime检查事件及接收时效；显式replay按接收时间聚合，仍核对原始MD交易日及每合约深度。两入口均支持`--instrument-max-age-seconds`，默认10秒，范围(0,120]；全局MD接收门控仍为10秒。

数据库参考只在启动／控制线程刷新，行情回调读取有时效约束的原子缓存；刷新失败闭闸。01额外核对累计因子版本，改变后须重新装配。策略公式、EMA历史预热和04同步帧各保留原实现。源码完成，完整原生集成回归及在线验收待运行机验证；详见[公共化交接与修改文件清单](../../doc/HANDOFF/live/HANDOFF_LIVE_COMMON_2026-10-10.md)。


## Live渠道分离（PF2026101035）

在线运行时已迁移至`bomber/framework/trader/runtime/live/`，行情与执行来源分别
装配；CTP实时/7×24回放规则在`channels/ctp.py`，不会传入策略算法。原启动参数
兼容。旧runtime导入文件只重导出新实现；部署须包含新增live目录，不能只同步demo。

本机48项纯Python回归通过，原生集成受Bar实体缺失限制，需在uv-nautilus复测。
完整文件列表、时间策略和复测命令见
[Live渠道交接](../../doc/HANDOFF/live/HANDOFF_LIVE_CHANNELS_2026-10-10.md)。


## 策略持仓与恢复公共化（PF2026101036）

原生SimNow/strict的realtime/Replay均支持显式一手接管与单次同交易日`--resume`；恢复归属/账本/目标版本，EMA重新预热，旧目标在新信号前暂停。新状态含会话身份，旧单次文件缺身份组件不自动恢复。两demo源码不相互引用；行情模式与持仓初始化独立。strict接管/恢复路径仍要求单策略独占账户，失败恢复保留原文件；本文顶部Model/off不使用柜台仓位。接管/恢复后新目标等于已有仓位时允许无新增订单，报告区分持仓核验与新成交；空仓新会话仍要求成交验收。

69项公共纯Python测试通过，9项新增原生实体装配测试及实际柜台接管/恢复待Linux验收。本例修改清单、同步摘要、回归和当前命令见[持仓恢复交接](../../doc/HANDOFF/live/HANDOFF_LIVE_POSITION_RECOVERY_2026-10-10.md)。
# 独立策略执行与账户仓位政策（2026-10-11）

`--mode model --strategy-position-source model --account-reconciliation off`只连接行情，
按当前参考价模拟成交并维护自己的策略仓位，不创建TD或读取柜台持仓。
必须声明`--expected-trading-day`（此模式为参考资料交易日）、来源日及独立状态文件；
重新启动使用同一个文件加`--resume`，不能混用原生SimNow状态文件。
模型事件标记`execution_origin=model`，不会被当作真实成交。

原生`--mode simnow`默认`--strategy-position-source fills --account-reconciliation strict`。
可显式选择`observe`（单独记录真实仓位，不覆盖归属成交账本）或`off`（不查询真实仓位）。
原生资金/订单恢复/账户锁继续由交易端管理，不能靠off让两个独立TD进程写同一账户。
不关联模式不能使用`--adopt-existing-position`；自己的归属成交仍保存和恢复。

多账户通过公共`AccountExecutionRoute`及`TargetFanoutClient`装配。`build_runtime(args,
account_routes=...)`在模型模式接受原生/第三方执行端或共享账户服务；跟随账户仓位
和回报不进入EMA的主策略账本。具体示例及完整文件清单见
[策略执行交接](../../doc/HANDOFF/live/HANDOFF_STRATEGY_EXECUTION_2026-10-11.md)。

2026-10-11远程独立模型运行已通过：CTP Replay行情产生9根Bar、1笔模型成交，
最终RB目标和策略仓位均为-1，无物理账户关联或清理错误。01模型恢复及与04
同时运行仍待验收，具体报告见[模型验收记录](../../doc/HANDOFF/live/HANDOFF_MODEL_REPLAY_2026-10-11.md)。



## M01 公共行情来源配置（2026-10-14）

本入口已接入统一 `--market-config` / `--market-source`。回测使用文件，Live默认CTP，
可配置DolphinDB共享分钟流；行情选择独立于执行与历史来源。
参数、BN业务输入限制、共享worker用法及验证见[公共配置说明](../market_sources/README.md)。
