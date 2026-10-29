# 单品种跨期价差策略

Live 重复的Local／CTP启动及停机已归入[公共生命周期](../../doc/functions/trader/LiveSessionLifecycle.md)（2026-10-29）；方向、双腿手数和成交验收保留在策略恢复层。

## 公共历史协调（2026-10-29）

Live 的历史阶段、窗口、双腿同步覆盖与实时缓冲放行已接入公共
`bomber.framework.trader.runtime.live.history`。本策略 `history.py` 仅保留历史配置与原始价差预热，
原方向、版本和完整双腿要求继续保留。接口及验证见 [Live 历史协调](../../doc/functions/trader/HistoryFrameLoader.md)。

Live入口已实现，见[第4节](#4-live版本)。当前固定交易日和固定双腿；原生离线及在线验收待Linux运行。

## 2026-10-15：DDB双腿历史预热

独立Recording/Model/SimNow、共享同进程宿主、独立worker及宿主单策略重启已接入历史准备。
默认不启用历史时保留实时预热。启用history-config/file或非零history-minutes后，
默认请求lookback个历史同步帧，随后新实时帧按原算法决策；不缩小原120根窗口。
历史只推进原始close差值窗口，不计算复权因子，不改变交易方向、目标版本、订单或归属仓位。
恢复时保留原方向及版本后重新预热，不重放历史目标。

复用公共HistoryService和DolphinDB Provider；两腿使用同一截止点，按合约/时间去重后取
完整帧交集。仅一腿或错位分钟不拼接、不填价。默认fail，allow仅使用真实完整帧并保留缺口；
连接/查询失败、字段错误及价格冲突不被allow掩盖。加载期间缓冲实时Bar，完整缓冲帧仅预热，
剩余单腿保留等待对腿；开放实时决策后重复时间不再次推进窗口。

`history_au.json` 读取 `dfs://min_bar/shfe_1min`，`time_label=end`，21:01表示
已完成的21:00—21:01。AU实际主力/次主力/far由角色服务确定，不写死月份。
用户只确认该表包含AU，尚未证明默认secondary/far两腿都有历史；缺任一实际合约即明确报告。

同步后先运行受影响的离线验收（不连接真实服务）：

```bash
python -u -m tests.run_main_ema_acceptance \
  --only calendar-history --only calendar-live --only shared-account
```

以下为沿用此前TD=20261016、资料日=20261015的Recording样例；换会话须重新核对日期。
沿用本次无日期例外的局部范围；有例外时改用实际session-overrides-table。
在pro根目录及原运行环境中执行，CTP/DDB连接参数读取既有环境配置。

```bash
python -u -m demos.03_calendar_spread.run_live \
  --connect --mode recording --product AU \
  --market-source ctp --simnow-environment realtime \
  --expected-trading-day 20261016 --expected-source-day 20261015 \
  --reference-database dfs://bomber_daily \
  --leg1-role secondary --leg2-role far \
  --lookback 120 --rebalance-interval 5 --entry-z 2 --exit-z 0.5 \
  --quantity 1 --max-notional 1000000 \
  --calendar-id china.sse \
  --trading-hours-database dfs://bomber_static \
  --trading-hours-table fut_trading_session --no-session-overrides \
  --history-db dolphindb --history-config demos/03_calendar_spread/history_au.json \
  --history-minutes 120 --history-stage recovery --history-missing-policy fail \
  --ctp-future-buffer-ms 100 --seconds 600 \
  --report-dir demos/03_calendar_spread/results/AU_ddb_recording
```

03 Recording不连接TD、不发物理订单、不使用交易状态文件。报告history/history_audit记录
逐腿覆盖及完整帧数量；complete_frames包含预热帧和实时帧，不能当作实时观察分钟数。
预热120帧不发布目标；之后真实完整帧才可能触发原价差信号，未触发信号不伪造交易。
2026-10-16补充：独立入口启动打印实际lookback、历史请求量及seconds；运行中每15秒及新帧时
打印history_frames/live_frames、窗口长度、双腿Bar计数、最近完成分钟及等待腿。
历史覆盖报告增加load_seconds，区分数据库读取耗时与等待实时输入。无新目标不再静默。
共享配置在本策略argv增加同样的历史/会话参数即可，账户server、worker命令保持原形式。
新增参数和日历身份参与恢复核对，不给旧检查点静默更换配置。

本机受控回归通过不等于真实DDB接续及双腿成交通过，详见
[交接说明](../../doc/HANDOFF/history/HANDOFF_CALENDAR_HISTORY_WARMUP_2026-10-15.md)。

## 1. 使用方法

### 启动命令

从 pro 根目录、在已安装运行依赖的环境中执行。回测只读取文件，必须显式提供 `--data-root /实际数据根目录`，或逐项提供所需文件／目录路径；不再从 `.env` 或环境变量补数据路径。下面的 `/path/to/market-data` 是占位符，请替换为真实路径，日期也须匹配实际文件。

**完整执行（显式设置常用参数）：**

```bash
python demos/03_calendar_spread/run_backtest.py \
  --data-root /path/to/market-data \
  --product RB \
  --start-day 2026-01-05 --end-day 2026-05-29 \
  --leg1-role secondary --leg2-role far \
  --quantity 1 --lookback 120 --entry-z 2 --exit-z 0.5 \
  --bar-timestamp end --log-level WARNING --no-tearsheet
```

**默认执行（仅必填参数）：**

```bash
python demos/03_calendar_spread/run_backtest.py \
  --data-root /path/to/market-data \
  --product RB \
  --start-day 2026-01-05 --end-day 2026-05-29
```

默认执行采用参数表中的策略默认值；文件路径仍须显式指定。例如默认启用绩效图，不要求必须产生成交。

### 参数说明

“可选”路径可由显式 `--data-root` 推导；如果不传根目录，就须逐项提供所需路径，不能全部省略。

| 参数 | 必选 / 可选 | 含义与默认值 |
| --- | --- | --- |
| `--product` | 必选 | 选择一个品种，两腿均属于该品种；例如 RB、I、JM、TA |
| `--start-day / --end-day` | 必选 | 请求日期闭区间 |
| `--leg1-role / --leg2-role` | 可选 | 默认 secondary / far，对应角色表 second / far；也支持 main / secondary、main / far，不使用 recent |
| `--quantity` | 可选 | 每腿目标手数默认 1，正整数；两腿方向相反 |
| `--lookback` | 可选 | 此前同步价差窗口根数，默认 120 根一分钟双腿 Bar；第 121 根开始可生成信号 |
| `--entry-z / --exit-z` | 可选 | 入场 / 出场阈值，默认 2 / 0.5 |
| `--rebalance-interval` | 可选 | 持仓时每隔若干根完整同步 Bar 重提目标；方向变化即时提交，默认间隔 5 |
| `--bar-timestamp` | 可选 | 源分钟标签 start / end，默认 end；start 在公共层统一移动到分钟结束 |
| `--missing-role-policy` | 可选 | 默认 raise，任一指定腿缺文件时报错；next-available 在请求角色范围内按当日文件可用性替换并记录。严格无前视研究保持 raise |
| `--contract-struct / --fut-basic / --bars-dir` | 可选 | 覆盖角色表、基础条款、分钟行情路径 |
| `--starting-balance` | 可选 | 初始资金默认 1000000 |
| `--commission-per-contract` | 可选 | 固定每手手续费默认 1 |
| `--margin-init / --margin-maint` | 可选 | 初始 / 维持保证金比例默认 0.10 / 0.08 |
| `--max-notional / --max-market-age-seconds` | 可选 | 名义金额上限默认 1000000，真实报价最大年龄默认 120 秒 |
| `--data-root` | 可选 | 可显式指定数据根目录，按 role/ 与 kline/ 布局推导路径；仅在逐项显式指定路径时可省略 |
| `--report-dir` | 可选 | 输出根目录，默认本策略目录的 results/ |
| `--log-level` | 可选 | 框架日志等级，默认 WARNING |
| `--no-tearsheet` | 可选 | 指定时仅保存 CSV / JSON，不生成绩效图 |
| `--require-fills` | 可选 | 指定时要求至少一笔模拟成交，否则报错 |

### 数据要求

需要角色表 second / far 列及两腿真实行情；默认只选择次主力与 far。每天使用严格早于当日的最新角色记录，不读取复权因子。换约日需提供有敞口的旧双腿行情；带非空 available_ns 的延迟发布角色记录当前不支持。

| 数据项 | 要求 |
| --- | --- |
| 角色表 | 优先 fut_contract.feather，旧名 fut_contract_data.feather 仅在新名不存在时回退；两个同时存在须显式指定。需要 trade_date、code 及所选角色列 |
| 基础条款 | fut_basic.feather：symbol、code、exchangeCD、contMultNum、minChgPriceNum、listDate、lastTradeDate |
| 分钟行情 | 真实合约的 `<symbol>_YYYYMMDD.feather`，由公共层校验身份、时间、OHLC 和成交量 |

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

## 2. 策略详细说明

### 策略思路与统计口径

这是同品种、两个期限角色的价差均值回归策略。默认只使用**次主力（角色表 second）和 far（角色表 far）**，没有第三个 near 合约，也不使用 recent。内部统一命名为 `leg1 / leg2`：第一腿次主力，第二腿 far，实际合约由日级日程确定。策略没有根据合约到期天数重新排序，也没有拟合价差对冲系数，两腿按相等手数交易。

`CalendarSelection.leg1_symbol / leg2_symbol` 保存每日选出的两个真实合约；`CalendarSpreadConfig.leg1_role / leg2_role` 保存角色配置。默认价差明确为“次主力 close − far close”，不是额外选择一个近月合约。两腿参数都可省略，默认就是 secondary / far。

```text
S(t) = 第一腿真实 close - 第二腿真实 close
μ(t) = 此前 lookback 个同步价差的均值
σ(t) = 此前 lookback 个同步价差的总体标准差
z(t) = [S(t) - μ(t)] / σ(t)
```

当前价差计算完信号后才加入窗口；默认第 121 个完整双腿帧开始评价。方差为零时 z 为零。只有两腿同分钟到齐才推进窗口，缺失腿不补价；不使用复权因子。

| 标准分数条件 | 第一腿目标 | 第二腿目标 |
| --- | --- | --- |
| `z ≤ -entry_z` | `+quantity` | `-quantity` |
| `z ≥ entry_z` | `-quantity` | `+quantity` |
| `abs(z) ≤ exit_z` | 0 | 0 |
| 其余区间 | 保持方向 | 保持方向 |

方向变化立即更新目标。方向未变且持仓方向非零时，按累计完整帧数满足 `rebalance_interval` 重提目标，供执行层调整实际持仓；这不会逐次累加手数。

### 换约与完整目标表

任一角色合约变化即启动换约：清空统计窗口、方向归零；若旧双腿有实际仓位或在途订单，等待旧双腿同分钟行情提交零目标。只有确认旧敞口消失后，才等待新双腿同步行情切换，并重新预热。旧合约缺文件且仍有敞口时明确报错。

目标表包含回测区间注册的全部真实合约，但有效组合只有当前两腿。其他合约的零值用于清理历史仓位，不能把“字典里出现多个合约”理解为同时套利多个组合。批量目标不保证两腿同时成交，相等手数也不保证逐时金额完全相等。

本例没有固定止损、持有期限或末日强制平仓。退出依赖价差进入出场区间或换约清仓。验收应检查入场 z-score、两腿成交方向、旧组合实际归零和换约后重新预热，而不只检查信号目标。

## 3. 使用核心组件描述

### dataprep：输入准备

| 关键接口 | 本例用途 |
| --- | --- |
| input_session / read_feather | main 与 run_case 共享读取缓存、来源版本和诊断；读取基础条款 |
| resolve_futures_args | 返回 DataPaths，解析行情目录、角色表与基础条款路径 |
| product_inventory / load_role_assignments | 列出所选品种每天真实行情合约，读取并校验所需角色列与重复记录 |
| BarFileKey / plan_fixed_contracts | 声明每日必需的新双腿文件和换约日可选旧合约文件，生成 InputPlan |
| BarReadSpec / prepare_fixed_contracts | 使用固定合约场景统一读取、时间归一及严格行情校验，返回已准备的 HistoricalInputBundle |
| venue / futures.instrument | 校验真实合约条款；调用 trader 合约工厂，返回合约、行情元信息和乘数 |
| add_prepared_bar_source | 将 Bundle 中已校验的行注册到 Feed，不重新读取原文件 |
| write_input_reports | 输出输入来源、文件覆盖、假设与问题，便于对照交易报表 |

### datahub：参考资料服务

| 关键接口 | 本例用途 |
| --- | --- |
| 本例不调用 DataHub Store | 使用 CalendarSelection 的固定双腿日程；无需复权因子或连续角色价格查询。角色文件读取仍由 dataprep 完成 |

### market：行情回放

| 关键接口 | 本例用途 |
| --- | --- |
| FileReplayFeed | 保存真实合约元信息与已准备行情源，按时间回放 Bar |
| DataType.BAR / MarketStreamBinding | 声明每个真实合约的一分钟 Bar 流 |

### trader：策略与执行

| 关键接口 | 本例用途 |
| --- | --- |
| StrategyTemplate / set_targets | 策略提交两腿等手数反向目标，保留信号与换约原因 |
| CtpFuturesBasicProfile / NautilusSimExecutionBackend | 模拟账户、手续费、保证金及真实合约回测 |
| PositionManager / NetTargetOrderPlanner | 维护实际与在途敞口，将目标转换为净持仓订单 |
| MarketReferencePriceStore / PreTradeRiskManager / RiskLimits | 用真实合约参考价检查数量、名义金额和行情时效 |
| DataBinding / ExecutionRoute | 绑定真实行情合约和对应执行路由 |
| UnifiedStrategyRunner / NautilusMarketFeedAdapter / UnifiedHistoricalRuntime | 串联行情、策略、执行客户端及历史回放生命周期 |

## 4. Live版本

从 `pro` 根目录用模块入口启动：`python -u -m demos.03_calendar_spread.run_live`。
默认 Recording 只连接CTP行情并记录目标；Model维护独立归属成交账本；SimNow
连接交易前置并执行受控模拟盘委托。三种模式复用上面的价差公式和同一个策略。
生产代码不从其他demo导入组件。

### 运行前资料与环境

`.env`继续使用项目已有配置。Recording / Model只需要`CTP_MD_ADDRESS`、
`CTP_BROKER_ID`（默认9999）、`CTP_ACCOUNT_ID`、`CTP_PASSWORD`和
`CTP_PRODUCTION_MODE`；不要求TD地址、AppID和AuthCode。SimNow另需
`CTP_TD_ADDRESS`、`CTP_APP_ID`、`CTP_AUTH_CODE`，沿用公共账户锁及模拟盘授权。
DolphinDB基础资料地址、账户及表配置使用`DolphinDbReferenceConfig.from_env()`，读取`DDB_REFERENCE_*`；行情订阅和历史分钟读取`DDB_MARKET_*`。各项未设置时兼容旧`DDB_*`，两台服务器示例见[公共行情配置](../market_sources/README.md#dolphindb-行情与基础资料连接)。

参考资料使用DolphinDB的角色和期货基础表，不使用分钟文件或复权因子表。
必须提供所选品种的两个角色及其真实条款；角色来源日严格早于本次参考交易日，
`available_ns`仍核验发布时间。默认secondary / far，不因缺腿改用main或recent。
支持SHFE、INE、DCE、CZCE人民币期货，郑商所柜台代码由公共适配器转换。
可选手工核验必须同时提供两腿，例如
`--expected-instrument secondary=rb2701.SHFE --expected-instrument far=rb2705.SHFE`，
其中示例合约须根据当前资料替换。

下面日期变量须先填入实际核验值；Replay的原始MD日也须从当前前置核对。
不要将示例合约或文档日期当作实际登录结果。

```bash
export PYTHONPATH=.
export REFERENCE_DAY=YYYYMMDD
export ROLE_SOURCE_DAY=YYYYMMDD
```

### Recording

```bash
python -u -m demos.03_calendar_spread.run_live \
  --connect --mode recording --product RB \
  --expected-trading-day "$REFERENCE_DAY" --expected-source-day "$ROLE_SOURCE_DAY" \
  --reference-database dfs://bomber_daily \
  --leg1-role secondary --leg2-role far \
  --quantity 1 --lookback 120 --entry-z 2 --exit-z 0.5 \
  --rebalance-interval 5 --max-notional 1000000 --seconds 9000
```

默认实时环境使用事件时间聚合；只有收到下一分钟成交才发送上一分钟Bar，
无成交分钟不补价，两腿同分钟完整才推进窗口。跳过启动时的不完整分钟。
默认需要至少121个完整帧，运行时间应覆盖有效交易时段。

第二套SimNow Replay可在命令中增加：

```bash
--simnow-environment replay --replay-md-trading-day "$MD_DAY" --instrument-max-age-seconds 30
```

须先设置实际`MD_DAY`和对应MD前置。Replay按接收时间聚合，策略不判断模式；
原始MD日、深度、接收心跳与源时间倒退仍检查。参考交易日、角色来源日及可见性
不因回放而豁免；SimNow的TD日必须等于`REFERENCE_DAY`。

### Model与恢复

在Recording命令上替换模式并增加状态文件：

```bash
--mode model --strategy-position-source model --account-reconciliation off \
--state-file demos/03_calendar_spread/results/calendar-model.json
```

新会话须使用不存在的文件。恢复使用完全相同的模式、参数、参考资料与原文件，
追加`--resume`，不换文件名。模型参考价成交有model来源标记，不代表盘口或柜台
成交，不模拟费用、滑点和限价队列。

恢复保留归属仓位和目标revision，清空行情窗口；重新预热后的首个有效判断才
发布新目标，即使目标归零也会提交，以处理保留仓位。恢复预热期间旧目标不续发。
本地或柜台仍有活动订单、参数/合约/资料指纹/行情环境/交易日变化时拒绝恢复。

### 受控SimNow

在Recording命令上替换模式，并增加：

```bash
--mode simnow --enable-orders --confirm-simnow \
--state-file demos/03_calendar_spread/results/calendar-simnow.json
```

默认strict核验管理双腿空仓；范围外仓位不归属本策略。恢复原文件追加`--resume`，
并核验同日成交账本、归属、今昨仓及柜台活动订单。也可显式配置
`--account-reconciliation observe|off --strategy-position-source fills`，物理账户锁、
资金与活动订单核验仍生效。本入口只支持Broker9999模拟盘，不授权真实资金交易。

每腿持仓上限为quantity，单笔数量允许2×quantity以支持Model反手；
每腿订单和持仓名义金额均受max-notional限制，超限拒绝。
SimNow还支持`--limit-offset-ticks`（默认1）、`--max-session-orders`（默认20）
和`--query-timeout`（默认15秒）。原生反手先平后开，由新鲜Tick推进保留目标；
SHFE/INE平今平昨和DCE/CZCE通用平仓复用公共Planner。
多腿提交不保证原子成交；首腿拒单或回报冲突后停止后续腿并要求核对。

### 会话范围和输出

这是固定交易日、固定双腿会话。资料换版、角色/条款变化、MD/TD日期变化时闭闸
停止，停机保留已成交仓位；SimNow撤销活动订单并核验保存账本。
目前没有跨日自动換约或跨日带仓恢复；有旧仓时不能换用新合约和新文件绕过核验。
历史回测原来的清旧仓换约逻辑继续保留，本次没有声明在线动态换约已完成。

默认输出`results/calendar-spread-<mode>-<ns>/summary.json`和`events.txt`，
可用`--report-dir`改输出根目录。报告包含双腿、帧数、z-score、目标、归属仓位、
模型/执行仓位、可选物理账户观测、回调数、参考指纹及就绪渠道。
Recording的execution_origin为recording、strategy_position_source为none。

默认`passed`要求至少lookback+1个同步帧；没有信号或成交也可能通过分钟帧验收。
Model/SimNow需要本次归属成交证据时增加`--require-fills`；Recording不接受此参数。
即使有成交也须分别检查双腿实际仓位、拒单和停机结果，不以受理回报当成交。

### 离线验收（Linux uv-nautilus）

```bash
python -u -m tests.run_calendar_spread_acceptance
```

REV2入口内嵌源码SHA摘要，不需要额外同步`.sha256`文件。预检后运行entry组的
3个标准库测试、22个Live测试方法及原有选约/信号断言，保存分组日志与JSON。
它使用假MD/TD，既不连接外部系统也不报实际订单。
可用`--only entry`、`--only live`或`--only selection`重跑失败组。
公共代码未改，可选`--only public`复核既有公共组。

本机仅做静态审阅和文件SHA核验，未运行Python、测试或编译。用户Linux复测
live组22项全部OK（0.330秒），验收报告passed；此前selection也已通过。
报告目录末段为calendar-spread-acceptance-1791817491340659580。
这些是离线假行情/柜台夹具；实际DolphinDB/CTP行情、在线Model恢复及SimNow
成交验收仍待验证。无需重复已通过的live和selection组。
详见[交接记录](../../doc/HANDOFF/live/HANDOFF_CALENDAR_SPREAD_2026-10-12.md)。

2026-10-12远端第一次运行因缺少旧入口配套的`calendar_spread_live.sha256`而在
预检失败，尚未执行测试。修订为REV2后，已同步初版其余源码的机器只需更新
`tests/run_calendar_spread_acceptance.py`并补充`tests/test_calendar_spread_acceptance.py`，
重跑原命令；真实源码缺失或版本不符仍会明确列出。旧外置摘要保留为历史材料。

REV3修复假CTP夹具缺少断线闭闸回调，以及未发布角色负例的异常类型断言。
生产代码与门控保持原样。同步`tests/test_calendar_spread_live.py`和
`tests/run_calendar_spread_acceptance.py`后，执行
`python -u -m tests.run_calendar_spread_acceptance --only live`复测失败组即可。

### CU realtime分阶段在线验收（PF2026101210）

用户指定CU、realtime，全部在Linux uv-nautilus执行，本机不运行。
同步`scripts/integration/calendar_spread/acceptance.py`和
`tests/test_calendar_spread_online_acceptance.py`，确认终端环境/`.env`中MD与TD
为当前realtime前置，然后运行：

```bash
python -u -m unittest -v tests.test_calendar_spread_online_acceptance && \
python -u -m scripts.integration.calendar_spread.acceptance \
  --connect --product CU --environment realtime \
  --through simnow --enable-orders --confirm-simnow \
  --lookback 3 --recording-seconds 600 --execution-seconds 900 \
  --max-notional 1000000 --max-session-orders 20
```

先只读登录TD取得实际交易日并关闭连接，再从DolphinDB读取严格早于该日的
secondary/far角色及条款，不查询因子。夜盘日期从柜台取得，不按本地日历猜。
随后依次运行Recording、Model、SimNow；Recording本身只连接MD。
每阶段单独输出summary和acceptance.json，前一阶段通过才启动后一阶段。
目录为`tests/results/calendar-spread-online/cu-<ns>/`，含preflight.json及每阶段
实际command.json、runtime/报告，Model和SimNow状态文件各自独立。

此为显式验收参数：lookback=3、每腿1手、阈值2/0.5、调仓间隔1；常规策略默认
窗口120没有修改。Recording除了同步预热还要求至少提交一次完整目标；
Model/SimNow要求本次归属成交、双腿最新目标实际完成；SimNow还须停机确认
无活动订单及无清理错误。名义超限、没信号、单腿成交、休市、失效或错误均停止
后续阶段，不能以降低阈值或伪造成交绕过。三阶段最大正常时长约40分钟，
启动/停止和只读准备另计。

默认控制器只运行到Recording；只要Recording可改`--through recording`并去掉
两个报单授权开关；到Model用`--through model`并去掉授权开关。命令中通过
SimNow的两个开关只交给最后一阶段，Recording/Model子进程不接收它们。

Ctrl+C由控制器转发给当前运行阶段，给其撤单/保存/释放时间；清理超时保留
进程PID并明确失败，不能当作已撤单。所有已成交仓位和交易状态文件保留。
若SimNow已运行并失败，不另开新文件冒充恢复，应核对其原状态与柜台，按03
原入口的同日--resume规则处理。在线Model同文件恢复仍是独立待验项。

该控制器与6个标准库回归目前仅静态审阅及SHA核验，本机没有Python、测试、
编译或连接；上述命令在运行机执行后才能登记实际结果。已通过的22项Live离线
基线不因新增此控制器而重跑。

#### PF1210 REV2：第一次CU Recording反馈

运行机实际核验参考日20261013、来源日20261012，双腿cu2612.SHFE/cu2701.SHFE，
资料ready=True且不使用因子；Recording启动后约12秒，两个深度事件年龄
10.033秒超过默认10秒，接收年龄约9.005秒，逐合约门控拒绝。此时完整帧与目标
均为0，Model/SimNow没有启动。不能从该日志判定缺成交、休市、前置停流或算法
错误，只能确定当时事件时间超限。全流10秒接收检查尚未报告失败。

REV2阶段控制器新增`--instrument-max-age-seconds`并透传原03参数，默认仍为
realtime10/replay30；有限范围(0,120]，全流接收心跳仍独立限制10秒。
阶段acceptance.json额外保存运行失败、完整渠道就绪及两腿Bar计数，便于定位。
未修改03策略、原生Feed、公共健康门控或已通过22项Live基线。
新增3项标准库回归，阶段控制器组现9项，当前源码仅静态核验，未在本机运行。

同步控制器及测试这两个文件后，运行机先只复验Recording，显式逐合约30秒：

```bash
python -u -m unittest -v tests.test_calendar_spread_online_acceptance && \
python -u -m scripts.integration.calendar_spread.acceptance \
  --connect --product CU --environment realtime --through recording \
  --lookback 3 --recording-seconds 600 --instrument-max-age-seconds 30
```

此命令不含报单授权。30秒不是本次数据问题已经解决的证据；若全流接收仍断档
超过10秒，会继续停止，下一步核对原始MD更新时间/接收间隔和成交量变化。
Recording通过后再接续Model及SimNow；没有双腿完整分钟或目标不进入后续阶段。
原失败报告保留，新运行目录独立生成。实际证据见
`doc/evidence/live/CALENDAR_SPREAD_RECORDING_FAILURE_20261012.json`。

#### CU Recording已通过，直接进入Model

用户复验目录`calendar-spread-online/cu-1791818623717906555/`：运行/阶段/总控制器
均passed，5个完整帧、1次目标，cu2612=-1、cu2701=+1，z=3.5355；readiness正常，
停机无错误，成交与仓位仍0。无需重跑Recording。下一步在同一个Linux环境执行：

```bash
CU_MODEL_STATE="demos/03_calendar_spread/results/cu-model-$(date +%Y%m%d-%H%M%S).json"
python -u -m demos.03_calendar_spread.run_live \
  --connect --mode model --product CU --simnow-environment realtime \
  --strategy-position-source model --account-reconciliation off \
  --expected-trading-day 20261013 --expected-source-day 20261012 \
  --expected-instrument secondary=cu2612.SHFE --expected-instrument far=cu2701.SHFE \
  --reference-database dfs://bomber_daily \
  --quantity 1 --lookback 3 --entry-z 2 --exit-z 0.5 --rebalance-interval 1 \
  --instrument-max-age-seconds 30 --max-notional 1000000 \
  --seconds 900 --require-fills --state-file "$CU_MODEL_STATE"
```

这里日期和双腿来自本次实际预检，仅用于已核对的当前会话，未来运行重新核对。
Model从新行情重新预热，不复制Recording目标或伪造已成交。原入口的
--require-fills要求出现本次成交；进入SimNow前还需从报告确认两个归属仓位均
达到last_targets、无report/portfolio/cleanup错误。保留模型原状态文件，以便
随后同日--resume独立验收。模型成交不作为柜台成交证据。

#### Model300秒只收到3个完整帧

用户首轮把运行时长设为300秒，readiness正常，但cu2612 Bar4/cu2701 Bar3，
仅3个完整帧；lookback3要求第4帧才能评价信号，因此无目标/成交，预热验收失败。
停机无清理错误，状态文件保留。下一轮沿用原命令，仅改为：

```bash
CU_MODEL_STATE="demos/03_calendar_spread/results/cu-model-20261012-154511.json"
# 原Model命令增加--resume，并使用--seconds 900。
```

其余日期、合约、参数、政策保持一致；不要重新生成文件名。恢复仍使用新行情
重新预热，不累加前一轮3个帧。当前尚无Model成交通过证据，SimNow未启动。

### 2026-10-13遗漏检查与成交验收补齐（PF2026101301_CALENDAR）

静态检查发现：原直接入口--require-fills只要求至少一次归属成交，未确认两腿
达到完整最新目标或剩余在途/未归属仓位。现补齐这些检查，并在summary增加
working_quantities与unassigned_positions；阶段控制器也要求两字段完整且全零。
这只收紧显式成交验收，不改变信号、规划、风险限额或检查点格式，原模型文件
继续可恢复。Recording默认验收不变，已通过证据保留，不重复运行。

新增空目标/零版本模型检查点恢复、仅单腿成交拒绝通过、已达目标但有在途量
拒绝通过三项原生回归；Live组现25项。标准库阶段组新增在途/未归属验收负例，
现10项。计数是源码统计，本机不运行；新组未记作通过，旧22项通过属于旧基线。
同步本次6个源码文件（见PF2026101301_CALENDAR_FILES.txt），在Linux执行：

```bash
python -u -m tests.run_calendar_spread_acceptance --only live && \
python -u -m unittest -v tests.test_calendar_spread_online_acceptance
```

本次没有修改strategy、公共框架或状态序列化；没有联网或报单。
两个新报告字段不在状态文件中，原文件无需迁移。严格阶段review会拒绝缺字段
的旧Model/SimNow报告，不把旧报告按新规则补记通过；旧Recording报告仍有效。
独立验收入口更新内嵌摘要为PF1209_REV4，实际部署清单PF2026101301_CALENDAR_SHA256.txt。

检查遗漏还包括补全原文件续跑命令。上述离线回归通过后，在同日、原资料身份
保持一致的Linux环境直接运行：

```bash
CU_MODEL_STATE="demos/03_calendar_spread/results/cu-model-20261012-154511.json"
python -u -m demos.03_calendar_spread.run_live \
  --connect --mode model --product CU --simnow-environment realtime \
  --strategy-position-source model --account-reconciliation off \
  --expected-trading-day 20261013 --expected-source-day 20261012 \
  --expected-instrument secondary=cu2612.SHFE --expected-instrument far=cu2701.SHFE \
  --reference-database dfs://bomber_daily \
  --quantity 1 --lookback 3 --entry-z 2 --exit-z 0.5 --rebalance-interval 1 \
  --instrument-max-age-seconds 30 --max-notional 1000000 \
  --seconds 900 --require-fills --resume --state-file "$CU_MODEL_STATE"
```

使用原文件，不能再次按date生成文件名；文件名20261012只是创建时的系统日期，
检查点交易日是20261013。午夜后的日历变化不意味着角色来源日自动改成20261013。
若实际MD/资料会话已变化，则由既有门控拒绝恢复，不能修改旧检查点绕过。
恢复重新预热，900秒不保证有信号/成交；预热、归属成交及最新双腿完成都须满足。
SimNow仍等Model通过后接续，不把空仓恢复或Recording当成模型/柜台成交证据。
跨日带仓自动换约仍未实现，已明确作为功能范围限制。

2026-10-13后续反馈：阶段控制器10项标准库回归OK，0.012秒；25项Live组汇总
未出现在摘录中，不补记通过。无需重复10项及已过Recording；确认25项Live结果
后，按上面的原文件--resume/900秒命令继续Model。实际模型与柜台成交仍待验。

### Model恢复通过，下一步受控SimNow

2026-10-13用户实际Model报告passed：原文件恢复、14帧、8目标、8模型归属成交，
包含反手与清仓，最终双腿目标/仓位/在途/未归属全零，停机无错误。报告目录
calendar-spread-model-1791855106176839622。此次原检查点为零仓/空目标，非零仓位
在线恢复仍需单独验收。模型源明确为model，不作为柜台成交。

下一步在Linux启动模拟柜台，新建独立状态文件，不复用模型文件、不加resume：

```bash
CU_SIMNOW_STATE="demos/03_calendar_spread/results/cu-simnow-$(date +%Y%m%d-%H%M%S).json"
python -u -m demos.03_calendar_spread.run_live \
  --connect --mode simnow --product CU --simnow-environment realtime \
  --enable-orders --confirm-simnow \
  --strategy-position-source fills --account-reconciliation strict \
  --expected-trading-day 20261013 --expected-source-day 20261012 \
  --expected-instrument secondary=cu2612.SHFE --expected-instrument far=cu2701.SHFE \
  --reference-database dfs://bomber_daily \
  --quantity 1 --lookback 3 --entry-z 2 --exit-z 0.5 --rebalance-interval 1 \
  --instrument-max-age-seconds 30 --max-notional 1000000 \
  --limit-offset-ticks 1 --max-session-orders 20 \
  --seconds 900 --require-fills --state-file "$CU_SIMNOW_STATE"
```

此命令会向Broker9999 SimNow模拟柜台报单。日期与合约仍由本次已核验会话提供，
实际TD/MD及资料不一致会拒绝启动；要求管理双腿空仓、账户无活动订单及可用
CNY资金。范围外柜台仓位保留，不自动接管或清空。每腿1手、同一状态文件只由
一个进程使用，订单上限20；停机撤销活动订单并核验保存账本，已成交仓位保留。
交易报告通过仍需本次归属成交、双腿最新目标完成、在途/未归属归零及停机无错。

### SimNow已双腿成交，修复报告后原文件带仓恢复

2026-10-13实际柜台开仓：cu2612多1手109970、cu2701空1手109460；14帧、6目标、
2归属成交，三种仓位与目标一致，在途/未归属0、活动单0。报告failed是停机
摘要读取CalendarReferences.factor_date_basis缺属性，不是单腿成交或柜台仓位
不一致。旧失败报告保留，当前柜台仍有本策略多1/空1；模型状态文件与此无关。

PF1302_CALENDAR只修03无因子报告投影（不增加因子查询），并补1项真实原生
生命周期snapshot回归；Live组26项仅源码计数，需Linux复测。同步清单中4个
Python文件后执行；若共享trading_sessions.py不符，也须按清单同步该已审阅的
并行基线（03不调用其日历，无共享源码修改）：

```bash
python -u -m tests.run_calendar_spread_acceptance --only live
```

通过后用原SimNow文件继续，不新建、不删除、不修改原状态；原授权和风控保持：

```bash
python -u -m demos.03_calendar_spread.run_live \
  --connect --mode simnow --product CU --simnow-environment realtime \
  --enable-orders --confirm-simnow \
  --strategy-position-source fills --account-reconciliation strict \
  --expected-trading-day 20261013 --expected-source-day 20261012 \
  --expected-instrument secondary=cu2612.SHFE --expected-instrument far=cu2701.SHFE \
  --reference-database dfs://bomber_daily \
  --quantity 1 --lookback 3 --entry-z 2 --exit-z 0.5 --rebalance-interval 1 \
  --instrument-max-age-seconds 30 --max-notional 1000000 \
  --limit-offset-ticks 1 --max-session-orders 20 \
  --seconds 900 --require-fills --resume \
  --state-file demos/03_calendar_spread/results/cu-simnow-20261013-015114.json
```

会核验原归属与柜台今昨仓，保留已有双腿并重新预热；只由新行情产生后续目标。
恢复、干净停机及平今/平昨结果需本轮报告证明。当前只修源码和SHA，本机未运行。

2026-10-13后续验收：补同步REV5验收入口后，远端26项Live测试全部OK，
0.641秒，报告calendar-spread-acceptance-1791858789863169356，包含上述
无因子原生snapshot回归。离线门槛已通过，下一步执行上方原SimNow状态文件
的--resume命令；实际带仓恢复、后续成交与干净停机仍需本轮在线报告。

后续原文件恢复报告calendar-spread-simnow-1791858862895542144：带仓恢复成功，
13完整帧、6目标、3订单，cu2701平今1手109590成交；近腿平仓未成交，最终目标
双腿0而cu2612仍多1，验收正确failed。停机撤销两张剩余单，活动单/在途/未归属0，
对账无差异、cleanup_errors为空；快照兼容修复及干净停机已验证。当前持仓已变为
近腿多1/远腿0，不再是双腿多1/空1。同日继续沿用上述原文件--resume及原参数，
重新预热后由新行情产生目标，不删除账本或将剩余仓位当空仓；平昨与跨日滚动未验。

再下一轮报告calendar-spread-simnow-1791860057808226113，generation=19恢复，
12帧、6目标、2订单、1成交，最终双腿目标/三种仓位/在途/未归属全部0，
活动单0且停机无错。报告仍failed，因为验收时cu2701旧卖单working=-1尚未撤掉。
REV6（PF1303_CALENDAR）补03目标变化时撤销旧策略订单，不提前清零在途；
等待真实回报后由新Tick推进最新目标。新增回归覆盖相同目标不撤单、变化目标
撤旧单、撤单途中成交归属及按剩余仓位平今。同步live_runtime.py、
test_calendar_spread_live.py及run_calendar_spread_acceptance.py三个文件后运行
`python -u -m tests.run_calendar_spread_acceptance --only live`，本轮27项待Linux验。
原状态文件保留，当前本策略双腿均已平；在线复测仍沿用原文件--resume和原风控。

REV6后续Linux验收：27项Live测试全部OK（0.803秒），报告目录
calendar-spread-acceptance-1791869615627255753。离线撤旧单回归已通过，
真实柜台目标变化撤单及停机前完成最新双腿目标仍待原文件--resume复验。


## M01 公共行情来源配置（2026-10-14）

本入口已接入统一 `--market-config` / `--market-source`。回测使用文件，Live默认CTP，
可配置DolphinDB共享分钟流；行情选择独立于执行与历史来源。
参数、BN业务输入限制、共享worker用法及验证见[公共配置说明](../market_sources/README.md)。
