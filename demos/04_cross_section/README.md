# 多品种主力横截面动量策略

## 1. 使用方法

### 回测启动命令

从 pro 根目录、在已安装运行依赖的环境中执行，先设置 `export PYTHONPATH=.`。入口自动读取已有 `.env` 配置；没有配置数据路径时，可通过可选的 `--data-root` 或各文件路径参数指定。示例日期需与实际数据匹配。

**完整执行（显式设置常用参数）：**

```bash
python demos/04_cross_section/run_backtest.py \
  --products RB,HC,I,J,JD,JM,NI,NR \
  --start-day 2026-01-05 --end-day 2026-05-29 \
  --lookback 20 --rebalance-interval 5 \
  --group-fraction 0.30 --target-notional 300000 \
  --bar-timestamp end --log-level WARNING --no-tearsheet
```

**默认执行（仅必填参数）：**

```bash
python demos/04_cross_section/run_backtest.py \
  --products RB,HC,I,J,JD,JM,NI,NR \
  --start-day 2026-01-05 --end-day 2026-05-29
```

默认执行采用参数表中的默认值及已有数据路径配置；例如默认启用绩效图，不要求必须产生成交。

### 回测参数说明

“必选”表示启动时必须传入；“可选”表示有默认值或可由已有配置解析。数据路径虽然是可选参数，但运行前必须能解析到有效文件。

| 参数 | 必选 / 可选 | 含义与默认值 |
| --- | --- | --- |
| `--products` | 必选 | 至少两个不重复品种，逗号分隔；全部使用主力合约生成信号和下单 |
| `--start-day / --end-day` | 必选 | 请求日期闭区间 |
| `--lookback` | 可选 | 收益率回看同步帧数，默认 20；需要 21 个价格点。只统计所有品种主力同分钟到齐的帧，不是交易日数 |
| `--rebalance-interval` | 可选 | 默认 5；预热后按累计同步帧数整除间隔调仓；检测到主力变化后首个已预热同步帧也会调仓 |
| `--group-fraction` | 可选 | 排名前后每侧选仓比例，默认 0.30，范围为大于 0 且不超过 0.5；每侧数量为 max(1, floor(当前品种数 × 比例))，多空组不重叠 |
| `--target-notional` | 可选 | 每侧总名义金额预算，默认 100000；多空侧各使用该预算，组内等额分配。整数手数向下取整，不足一手时目标为零，余款保留现金 |
| `--bar-timestamp` | 可选 | 源分钟标签 start / end，默认 end；start 由公共层统一移动到分钟结束 |
| `--factors` | 可选 | 默认角色表同目录 fut_adjustment_factors.feather；需要 trade_date、code、symbol、pcr_cumfactor，pcr_factor 不用于信号 |
| `--factor-availability` | 可选 | 默认 aligned：因子已在上游对齐当前交易日；explicit 要求 available_ns。使用当天已对齐因子 |
| `--contract-struct / --fut-basic / --bars-dir` | 可选 | 覆盖角色表、基础条款、分钟行情路径 |
| `--starting-balance` | 可选 | 每个交易所模拟账户的初始资金，默认 1000000；多交易所账户分别初始化 |
| `--max-notional` | 可选 | 每合约订单与持仓名义金额风控上限，默认 1000000 |
| `--commission-per-contract` | 可选 | 固定每手手续费默认 1 |
| `--margin-init / --margin-maint` | 可选 | 初始 / 维持保证金比例默认 0.10 / 0.08 |
| `--data-root` | 可选 | 可显式指定数据根目录，按 role/ 与 kline/ 布局推导路径；已配置路径时可省略 |
| `--report-dir` | 可选 | 输出根目录，默认本策略目录的 results/ |
| `--log-level` | 可选 | 框架日志等级，默认 WARNING |
| `--no-tearsheet` | 可选 | 指定时仅保存 CSV / JSON，不生成绩效图 |
| `--require-fills` | 可选 | 指定时要求至少一笔模拟成交，否则报错 |

### 数据要求

需要全部品种每日主力及换约日旧主力行情，角色表 main 和外部累计因子表。因子使用 trade_date、code、symbol、pcr_cumfactor，当日对齐；main 合约冲突以因子 symbol 为准。缺失因子明确报错。

| 数据项 | 要求 |
| --- | --- |
| 角色表 | 需要 trade_date、code、main；优先 fut_contract.feather，旧名 fut_contract_data.feather 仅在新名不存在时回退，两个同时存在须显式指定 |
| 基础条款 | fut_basic.feather：symbol、code、exchangeCD、contMultNum、minChgPriceNum、listDate、lastTradeDate |
| 分钟行情 | `<symbol>_YYYYMMDD.feather`；每日主力和换约日旧主力都是必需文件，缺文件在回测前报错 |

### 输出内容

默认保存到本策略目录的 `results/<run-id>/`，可通过 `--report-dir` 修改根目录。控制台输出数据准备、运行回测、报表与清理的阶段耗时。

| 输出文件 | 内容 |
| --- | --- |
| orders.csv | 模拟委托及订单状态 |
| fills.csv | 实际模拟成交；提交目标不等于成交 |
| positions.csv | 真实合约持仓记录 |
| account_<交易所>.csv | 模拟账户及资金记录 |
| summary.json | 回测摘要、策略参数、信号或调仓统计 |
| input_manifest.json / input_coverage.json / input_issues.json | 输入版本、覆盖与质量诊断 |
| tearsheet.html | 可选绩效图；省略 --no-tearsheet 且绘图依赖可用时生成 |

## 2. 策略详细说明

### 策略思路与排名

这是多品种横截面动量策略，比较同一时刻各品种此前一段时间的相对涨幅，做多强组、做空弱组。只使用每个品种当前主力，研究价格为真实 close × 当天累计因子。

| 环节 | 计算规则 |
| --- | --- |
| 同步 | 全部配置品种的当前主力 Bar 必须具有相同时间戳；少一个品种便不推进窗口 |
| 预热 | 每个品种保存 `lookback + 1` 个价格点，默认 21 个完整帧 |
| 分数 | `当前复权价 / lookback 帧前复权价 - 1` |
| 排序 | 分数由高到低，相同分数按品种代码排序 |
| 每侧组数 | `max(1, floor(品种数 × group_fraction))`；比例上限 0.5，避免组重叠 |
| 选仓 | 前一组做多、后一组做空，中间品种目标归零 |
| 调仓 | 预热后累计同步帧数能整除调仓间隔时提交；换约后的已预热完整帧优先更新 |

分组依据品种数量，不依据历史合约数量。8 个品种、比例 0.30 时每侧 2 个；比例 0.45 时每侧 3 个。即使所有动量为负，仍做多相对最高组；即使分数相同，仍按照排序规则分组，不额外设定绝对涨幅门槛。

### 名义金额与实际手数

每侧各获得 `target_notional` 预算，组内等额分配：

```text
每个入选品种预算 = 每侧预算 / 该侧品种数
目标手数绝对值 = floor(品种预算 / (真实价格 × 合约乘数))
多头取正，空头取负
```

例如每侧 100000、两品种时各分 50000；单手金额大于 50000 的入选品种仍是多头或空头候选，但实际目标为零。不会强制买一手，也不把剩余预算转给其他品种。因此选出多空组不保证两侧都有非零成交，整数取整后也不保证实际金额严格中性。

### 换约、退出与核对

换约保留复权动量窗口，在下一有效调仓帧把旧真实合约目标归零，按新主力真实价格重新配仓。目标表里的历史合约和中间品种零值属于完整仓位管理，不是同时建仓。实际旧仓退出受报价、订单及成交约束。

退出发生在品种落入中间组、由多组转空组或由空组转多组，以及主力换约时。本例没有独立止盈止损或末日强制平仓。验收应同时核对排名、多空组、各品种分配预算、单手金额、整数目标和成交；只有分组而手数全零时，首先检查预算换算。

## 3. 使用核心组件描述

### dataprep：输入准备

| 关键接口 | 本例用途 |
| --- | --- |
| input_session / read_feather | main 与 run_case 复用读取缓存、来源版本及输入诊断，加载基础条款 |
| resolve_futures_args | 定位角色表、基础条款与行情目录，返回 DataPaths |
| prepare_role_research | 准备各品种 main 角色、外部当日累计因子和研究行情；组装 DataHub Store，返回日末时间用于选出回测日期 |
| BarFileKey / plan_fixed_contracts | 声明回测每日真实主力及换约日旧主力必需文件，生成 InputPlan |
| BarReadSpec / prepare_fixed_contracts | 统一时间标签、身份及严格 OHLCV 校验，返回 HistoricalInputBundle |
| selected_products / futures.instrument | 查询品种交易所，校验条款并调用 trader 合约工厂，返回原生合约、行情元信息与乘数 |
| add_prepared_bar_source | 注册已准备行情，避免校验后重新读取原文件 |
| write_input_reports | 输出来源版本、覆盖、假设及因子覆盖角色的诊断 |

### datahub：参考资料查询

| 关键接口 | 本例用途 |
| --- | --- |
| SectorRoleStore / snapshot(timestamp) | 按策略事件时刻查询已生效且可见的主力映射和因子 |
| SectorRoleAssignment.instrument(product, main) | 返回该品种当天最终主力真实合约 |
| SectorRoleAssignment.factor(product, main) | 返回上游提供的当前累计因子，供信号价格复权 |

### market：真实行情回放

| 关键接口 | 本例用途 |
| --- | --- |
| FileReplayFeed | 注册各真实主力及换约旧合约元信息与行情源，回放一分钟 Bar |
| DataType.BAR / MarketStreamBinding | 声明各真实合约的行情流 |

### trader：策略与执行

| 关键接口 | 本例用途 |
| --- | --- |
| StrategyTemplate / set_targets | 提交完整真实合约目标快照，附带排名分数、主力映射及同步帧编号 |
| CtpFuturesBasicProfile / NautilusSimExecutionBackend | 按交易所建立模拟账户并运行真实期货合约回测 |
| PositionManager / NetTargetOrderPlanner | 维护实际与在途持仓，把目标换算成订单 |
| MarketReferencePriceStore / PreTradeRiskManager / RiskLimits | 使用真实价格、乘数与报价时效检查订单和持仓名义金额 |
| DataBinding / ExecutionRoute | 绑定行情及各真实合约的执行路由 |
| UnifiedStrategyRunner / NautilusMarketFeedAdapter / UnifiedHistoricalRuntime | 串联行情观察者、策略、模拟执行及历史运行生命周期 |

## 4. 在线交易使用与参数说明

在线入口为 `python -u -m demos.04_cross_section.run_live`，从 `pro` 根目录运行，读取项目 `.env` 中的 CTP 与 DolphinDB 配置。当前报单入口限定 SimNow（BrokerID 9999）、SHFE/INE/DCE/CZCE 人民币期货、物理账户单写和本次固定主力组合；默认模式为 Recording，连接后只记录目标，不报单。PF2026101208新增DCE/CZCE源码适配，远程离线及真实柜台验收待完成，部署步骤见[四交易所交接](../../doc/HANDOFF/live/HANDOFF_CROSS_SECTION_FOUR_VENUES_2026-10-12.md)。

### 四交易所代码与平仓

| 交易所 | CTP代码示例（仅格式示例） | 平仓装配 |
| --- | --- | --- |
| SHFE | `rb2701.SHFE` | 原有平今/平昨分桶 |
| INE | `sc2611.INE` | 原有平今/平昨分桶 |
| DCE | `i2701.DCE` | 通用CLOSE，先平旧方向，成交后新Tick续开 |
| CZCE | `MA701.CZCE` | 通用CLOSE，先平旧方向，成交后新Tick续开 |

郑商所资料可以使用`MA2701`或`MA701`，但角色/因子/基础条款在上游须具有一致身份。在线适配将代码转为大写YMM，行情订阅、目标、柜台回报、持仓核对及检查点都使用`MA701.CZCE`；原始资料与指纹不修改。可选`--expected-instrument MA=MA2701.CZCE`会另外核对原始条款四位月份，防止截掉十年位后误认合约；推荐直接省略手填合约，由资料自动选主力。

DCE/CZCE恢复以`Position`与`TodayPosition`拆分当前今昨仓，缺字段或不合法会拒绝；不把`YdPosition`昨收数量当作当前昨仓。两交易所通用平仓按今仓优先写入成交账本。DCE实时行情的自然日采用北京时间接收日附近最近的日期，事件时分秒仍来自`UpdateTime`，兼容夜盘与午夜；Replay不启用该可选规则，继续按接收时间聚合。

用户的七品种组合可使用`--products AU,AG,SC,RB,I,MA,NI`。七品种与`--group-fraction 0.40`仍每侧选两个；每侧200万预算分成每品种100万，预算不足一手且启用`--require-full-groups`时全组合归零。各品种必须同分钟形成Bar，休市或缺一腿不会调仓；最早收盘品种停止出Bar后，组合不会继续产生完整帧。默认strict新会话要求管理合约空仓、全账户无活动订单；增加交易所不接管旧仓，不支持跨日或换组合恢复。

用户选择“策略维护自己的仓位、订单下到SimNow”时，使用已有独立成交账本参数：

```bash
--mode simnow --enable-orders --confirm-simnow \
--account-reconciliation off --strategy-position-source fills
```

该模式仍原生报单，以本策略归属的真实成交回报更新仓位；不将柜台已有净仓/总仓写入策略账本，也不要求物理账户空仓。资金、全账户活动订单、行情健康、报单授权和物理账户单写锁仍保留。新文件从零策略账本开始，柜台已有同合约仓位不自动归给本策略，新报单会改变同一物理账户总仓；继续既有策略账本须使用同日、同组合、同执行政策的原文件恢复。策略自己的已成交仓位不能靠换文件重新归零。2026-10-12用户反馈的七品种启动失败来自漏传这两个参数而落入strict；无需修改生产代码或默认policy。

### 实时与7×24回放环境

`--simnow-environment realtime` 为默认值：MD/TD交易日须一致，逐合约检查原始事件与接收时效，以事件时间聚合一分钟Bar。

第二套7×24联调增加 `--simnow-environment replay --replay-md-trading-day YYYYMMDD`。MD日期必须与原始行情的TradingDay一致；`--expected-trading-day`仍核对TD实际登录日，`--expected-source-day`仍核对参考资料来源日，三者分别填写。当前公共生命周期限定同组MD `tcp://182.254.243.31:40011`／TD `tcp://182.254.243.31:40001` 和 `CTP_PRODUCTION_MODE=true`。

replay按Tick实际接收时间聚合Bar，并以接收时间推进未完成目标和更新执行估值；原始上游Tick及报告中的深度事件时间保留。每个合约仍须有深度行情，接收年龄须在 `--instrument-max-age-seconds` 内；MD/TD日期变化、时间回退、缺腿和断线仍拒绝。公共会话最近接收不得超过10秒，聚合Tick接收不得超过10秒，Bar/请求120秒墙钟检查和原报单授权/数量/名义金额限制保留。接收时间Bar表示本次联调期间的价格流，不用于解释原交易日收益；没有成交的分钟不补Bar。

例如原始MD确认为20261008时，在原命令中添加：

```bash
--simnow-environment replay --replay-md-trading-day 20261008
```

该示例日期不能固定用于其他会话。Recording和显式授权的SimNow报单均支持这两种环境。恢复要求环境和原始MD日期不变，新目标元数据保存这两项；旧版无环境字段的状态只按realtime解释。切回实时环境使用 `--simnow-environment realtime` 并去掉回放日期参数。远程部署须同步本目录的 `run_live.py`、`live_runtime.py`、`recovery.py`，覆盖此前临时注释的检查代码。

### SimNow恢复执行示例

下面是本次 AU、AG、SC 的 300 秒恢复命令。日期是该次会话的明确核对值，不能作为其他交易日的固定配置。恢复文件必须是原成交会话留下的文件；新会话则去掉 `--resume`，使用不存在的新状态文件，并要求本次绑定合约空仓、全账户无活动订单。PF2026101037起，范围外仓位保留在柜台与账户快照中，不归给本策略。

```bash
python -u -m demos.04_cross_section.run_live \
  --connect --mode simnow --enable-orders --confirm-simnow --resume \
  --products AU,AG,SC \
  --expected-trading-day 20261012 --expected-source-day 20261009 \
  --reference-timeout 30 --instrument-max-age-seconds 30 \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.40 \
  --target-notional 2000000 --target-quantity-cap 1 --require-full-groups \
  --max-quantity 1 --max-notional 2000000 \
  --max-session-orders 20 --seconds 300 \
  --state-file tests/results/cross-section-tick-continue-1791567360820818324.json
```

### 连接、模式、恢复与运行时长

| 参数 | 必选 / 可选 | 含义与默认值 | 本例设置 |
| --- | --- | --- | --- |
| `--connect` | 必选 | 显式允许连接行情与交易柜台；省略即拒绝启动 | 启用 |
| `--mode` | 可选 | `recording` / `simnow`，默认 `recording`；前者只记录目标，后者允许在授权后报单 | `simnow` |
| `--enable-orders` | SimNow必选 | 显式启用报单；Recording不接受此开关 | 启用 |
| `--confirm-simnow` | SimNow必选 | 显式确认本次SimNow报单授权；恢复不继承上次授权 | 启用 |
| `--resume` | 可选 | 默认关闭；从已有原文件恢复交易状态，限定同交易日、同合约组合且本地/柜台无活动订单 | 启用 |
| `--state-file` | SimNow必选 | 交易检查点文件；新会话路径须不存在，恢复路径须为已有原文件；Recording不接受 | 原文件继续保存 |
| `--seconds` | 可选 | 默认1800；最终会话启动完成后的运行时长，须为有限正数。运行窗口包含实时预热，准备/登录和停机清理时间另计 | 300秒 |
| `--query-timeout` | 可选 | 默认15秒；CTP权威查询超时，须为有限正数 | 15秒 |
| `--report-dir` | 可选 | 默认本策略目录 `results/`；每次运行创建独立报告子目录，与交易状态文件分开 | 默认 |

恢复会核对文件完整性、账户身份、策略归属、柜台净仓/总仓、今昨仓分桶和可用资金，并延续目标revision、订单关联及成交去重。当前检查点不包含完整横截面指标窗口，恢复后从实时完整帧重新预热；产生新目标前保留已有仓位，不推进旧目标。跨交易日、主力改变或带活动订单的恢复会拒绝，不能靠修改日期、删除状态或覆盖旧备份绕过。

### 品种、信号与目标预算

| 参数 | 必选 / 可选 | 含义与默认值 | 本例设置 |
| --- | --- | --- | --- |
| `--products` | 必选 | 至少两个不重复字母品种，逗号分隔；自动读取各品种当前主力，全部到齐才形成同步帧 | `AU,AG,SC` |
| `--expected-instrument` | 可选，可重复 | 手工核对 `PRODUCT=symbol.VENUE`；默认自动选约。传入时须完整覆盖全部品种，例如 `--expected-instrument AU=au2612.SHFE` | 省略，自动选约 |
| `--lookback` | 可选 | 默认20，正整数；收益率回看同步帧数，需要 `lookback + 1` 个完整价格点 | 3，需要4帧 |
| `--rebalance-interval` | 可选 | 默认5，正整数；预热后累计同步帧数能整除该间隔时提交新目标，不是秒数 | 1，每个已预热帧调仓 |
| `--group-fraction` | 可选 | 默认0.30，范围 `(0, 0.5]`；每侧选择 `max(1, floor(品种数 × 比例))` 个品种 | 0.40，三品种每侧1个 |
| `--target-notional` | 可选 | 默认100000；**每侧**总名义金额预算，多空各一份，侧内等分；不是账户资金或保证金预算 | 每侧2000000 |
| `--target-quantity-cap` | 可选 | 默认不设置；预算向下取整后裁剪每合约目标绝对手数。须为不超过 `max-quantity` 的正整数；不足一手不会提升为一手 | 每合约最多1手 |
| `--require-full-groups` | 可选 | 默认关闭；任一多空入选品种预算不足以分配非零手数时，**本次全组合目标归零**，允许退出旧仓。只检查目标分配，不保证多腿订单同时成交 | 启用 |

本例三品种、比例0.40选出一个多头和一个空头，中间品种归零；不能形成两多两空。每侧200万元分别分给该侧唯一入选品种，按真实价格×乘数计算整数手数，再由cap限制为1手。预算不必用完，剩余预算不会强制变成仓位；该预算在截断前合计两侧400万元，也不等于实际保证金占用。

### 报单与持仓风控

| 参数 | 必选 / 可选 | 含义与默认值 | 本例设置 |
| --- | --- | --- | --- |
| `--max-quantity` | 可选 | 默认1，正整数；每合约订单数量及绝对持仓风控上限，超限拒绝，**不负责裁剪策略目标** | 1手 |
| `--max-notional` | 可选 | 默认100000；每合约订单及持仓的名义金额上限，使用真实价格和乘数核验；不是全组合或每侧预算 | 每合约2000000 |
| `--limit-offset-ticks` | 可选 | 默认1，非负整数；CTP保护限价的跳价偏移，按各合约自己的tick计算，不保证成交 | 1个tick |
| `--max-session-orders` | 可选 | 默认20，正整数；本次会话报单笔数上限，开仓、平仓及反手续开均计数；恢复后的计数针对新会话 | 最多20笔 |

`target-notional`决定希望分配多少仓位，`target-quantity-cap`裁剪计算出的目标手数，`max-quantity`和`max-notional`检查执行是否超限。例如预算算出多手、未设置cap，而风控最多1手时，会拒绝超限目标，不会自动改成1手。报单达到会话上限后停止新目标执行并进入结束流程，不保证最后目标已经成交完成。

### 交易日、参考资料与行情时效

| 参数 | 必选 / 可选 | 含义与默认值 | 本例设置 |
| --- | --- | --- | --- |
| `--expected-trading-day` | 必选 | `YYYYMMDD`；核对TD登录返回的交易日，实时MD也须匹配，不一定等于电脑当前自然日 | `20261012` |
| `--expected-source-day` | 必选 | `YYYYMMDD`；核对本次参考资料的预期来源日，应由数据和交易日历确定 | `20261009` |
| `--reference-timeout` | 可选 | 默认15秒，正整数；DolphinDB参考资料读取超时。增大超时不代表缺数据时允许交易 | 30秒 |
| `--reference-database` | 可选 | 覆盖DolphinDB参考资料DFS数据库路径；默认读取环境配置 | 环境配置 |
| `--reference-refresh-seconds` | 可选 | 默认5秒，范围 `[0, 60]`；参考服务刷新间隔，由控制线程进行，行情回调只读取缓存 | 5秒 |
| `--reference-max-source-age-days` | 可选 | 默认14，正整数；角色与因子来源距TD日的最大自然日数；不能代替预期来源日核对 | 14天 |
| `--reference-max-observation-seconds` | 可选 | 默认30，正整数；参考资料最近成功观测的最大年龄，超过后禁止用旧缓存交易 | 30秒 |
| `--reference-max-refresh-duration-seconds` | 可选 | 默认30，正整数；一次参考刷新允许的最大耗时，与读取超时分别检查 | 30秒 |
| `--factor-date-basis` | 可选 | 默认 `source`；因子使用选中角色资料的来源日。`trading`表示使用已对齐本次TD日的因子 | `source` |
| `--factor-availability` | 可选 | 默认 `observed-on-read`，配合 `source`使用；以成功稳定读取观测为可用性证据，不代表已知历史发布时间。`source`还可用 `source-day-end`/`explicit`；`trading`须显式选 `aligned`/`explicit` | `observed-on-read` |
| `--instrument-max-age-seconds` | 可选 | 默认10秒，范围 `(0, 120]`；逐合约检查深度行情事件年龄和本机接收年龄，任一腿陈旧、时间回退或交易日不符都闭闸 | 30秒 |

逐合约30秒检查与分钟Bar固定120秒过期检查是两个不同机制。30秒检查报价是否新鲜，120秒检查已收盘Bar是否延迟；这不是允许各品种分钟标签相差120秒，横截面仍只接收同一分钟全品种到齐的完整帧。参考资料缺失/刷新失败也不能解释为“没有行情”，不会自动跳过资料核验或补造数据。

### 预热、退出与结果核对

`lookback=3`至少需要4个完整同步帧，低成交活性或缺腿可能延长预热；300秒运行窗口不保证一定完成预热、调仓及成交。在线当前不提供历史分钟回补参数，不能把01的历史参数直接传给04。

运行到时会先验收，再进入停机流程：关闭新增执行、撤活动订单、查询权威仓位并保存状态；**不自动平掉剩余持仓**。若验收时目标持仓已达到但仍有挂单，也会报“组合最新目标尚未完成”；随后停机撤单可能使最终 `final_active_orders=0`，这两个时间点不同，最终清理完成不能倒推验收已通过。目标变更后的旧挂单处理仍是待完善项。

报告保存在 `results/cross-section-simnow-<标识>/`，包括 `summary.json`和`events.txt`。检查 `resume_ready`、`restored_generation`、`synchronized_frames`、`rebalances`、`last_targets`、各腿策略/账户/未归属仓位、`final_active_orders`、`cleanup_errors`和`status`。恢复验收允许目标已达到时没有本轮新增成交，但须产生有效新目标、无未归属仓位且无在途订单。

本节参数对应当前源码。实际运行前须按[PF1032部署清单](../../doc/evidence/checksums/PF2026101032_SHA256.txt)核对完整版本；此前运行机曾漏同步 `live_runtime.py`。分阶段实盘证据、测试和部署步骤见[SimNow验收说明](SIMNOW_ACCEPTANCE.md)，不要将旧版本运行结果当作当前修复版已验收。

## 5. SimNow实盘化历史（PF2026100920起）

PF2026100921：预期合约参数现为可选，不传`--expected-instrument`时自动读取DolphinDB当前主力、条款和因子并装配；传入时仍完整核对。无需手填月合约，内部账户/行情/参考一致性及运行中主力变更闭闸保留。新增2项用例（共21项）待远端，本次只复测cross-section组，原公共/01代码未改。同步PF2026100921_SHA256.txt后采用[自动选约命令](SIMNOW_ACCEPTANCE.md#自动选主力无需指定合约pf2026100921)；当前已经启动的Recording不受新源码影响。

最新远端末段确认：attribution18项OK、模块exit0，集中入口“全部选定离线模块通过”。本次选定离线阶段已通过，已过组无需重复；当前五品种的实际柜台只读/Recording/成交仍待验证。下方首轮失败记录保留为历史，代码/测试保持REV2。

2026-10-09最新选择改为CU、PB、NI、AU、AG，入口直接支持五品种参数，无公共交易源码修改；当前命令见[验收说明的五品种步骤](SIMNOW_ACCEPTANCE.md#当前五品种测试步骤)。group-fraction0.30时每侧选1个、其余3个归零，按预算取整，不承诺五品种均成交。首轮18项15通过/3测试订单顺序假设失败，已按合约修正并新增五品种Recording用例，共19项待远端，部署清单PF2026100920_REV2_SHA256.txt；原八组验收尚未整体通过。

新增模块入口`python -u -m demos.04_cross_section.run_live`，复用本策略原有同步帧、收益排名、每侧预算与整数取整，默认Recording。报单须显式SimNow开关、完整预期合约/TD/来源日和独立新状态文件。首期使用DolphinDB组合参考、SHFE/INE固定主力、实时完整分钟预热、单策略独占空仓账户；RB、HC为用户确认的首轮品种。每腿使用自己的跳价、乘数和风险限额，成交后统一归属与持久化，反手先平再在后续完整帧续开。

以下为首期历史状态，后续验收与能力扩展见各PF记录。首期源码完成时，离线尚未整体通过、实际柜台尚未运行；不将01的通过记录迁移为04验收。完整同步清单、集中离线命令、当前TD/参考只读、Recording与受控订单步骤见[SimNow验收说明](SIMNOW_ACCEPTANCE.md)。主力/累计因子/条款变化、单腿行情失效或拒单闭闸；多腿非原子，停机撤单并报告剩余仓位，不自动平仓。首期未开放DCE/CZCE；PF2026101208补充四交易所源码，验收见前节。CFFEX、动态实盘换约、共享账户并发、历史预热和自动跨进程恢复仍未开放；状态保存仓位/目标/订单，不包含完整横截面指标窗口。

当前在线进度（PF2026100922）：自动选约Recording实际失败，旧逐腿10秒闸门误用成交Tick活性。已改用CTP逐合约深度快照观察，不改变成交Bar或策略公式，新增3项回归，待Linux cross-section及entry验证；详见SIMNOW_ACCEPTANCE.md末节，部署清单doc/evidence/checksums/PF2026100922_SHA256.txt。此前离线通过不能作为修正后在线通过证据。

PF2026100923：用户随后遇到分钟Bar时效异常；04现跳过超过120秒的延迟成交Bar，未来Bar仍闭闸，输出合约及精确时间诊断。公共聚合器/01/策略不改，新增3项回归，当前04共27项待Linux验证。使用PF2026100923_SHA256.txt同步当前版本及SIMNOW_ACCEPTANCE.md末节命令；在线未通过。

PF2026101030：新增`--resume --state-file <原文件>`，同交易日且相同组合、无活动订单时恢复成交归属/账本/目标版本，权威核对后实时重新预热；预热前不执行旧目标。原文件继续保存，失败校验保留文件。支持受控手工恢复，跨日和挂单断点暂拒绝。执行时效使用当前墙钟，信号Bar标签单独保留；公共框架及算法不改。源码与新增回归完成、Linux尚未运行，命令及边界见[恢复验收说明](SIMNOW_ACCEPTANCE.md#pf2026101030-同交易日组合恢复源码完成linux待验)。

PF2026101032：真实恢复发现首帧旧目标仍被完整帧末推进，以及CTP回报先改Driver后等待客户端锁的保存竞态。已补完整帧预热门控，公共CTP自动保存共享可重入提交锁，严格同代校验保留；新增启动后预热/续开及并发回归。最新原文件仓位AU多1/SC多1、AG0，不能用旧备份覆盖；修正后待Linux cross-section与CTP autosave/recovery/driver四组，通过再恢复。


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

同交易日组合恢复改用公共CTP恢复生命周期与持仓核验；本例只保留组合/环境/版本及同步帧重新预热逻辑，已有状态格式保持。两demo源码不相互引用；行情模式与持仓初始化独立。账户仍单策略独占，失败恢复保留原文件。接管/恢复后新目标等于已有仓位时允许无新增订单，报告区分持仓核验与新成交；空仓新会话仍要求成交验收。

69项公共纯Python测试通过，9项新增原生实体装配测试及实际柜台接管/恢复待Linux验收。本例修改清单、同步摘要、回归和当前命令见[持仓恢复交接](../../doc/HANDOFF/live/HANDOFF_LIVE_POSITION_RECOVERY_2026-10-10.md)。

## 管理合约范围（PF2026101037）

Live装配声明本次主力合约为持仓管理范围；策略公式和行情源接口不变。
例如AU/AG/SC会话忽略范围外`rb2701.SHFE`对启动及组合持仓一致性的影响，
保留它的柜台仓位，不接管、不平仓、不计入04策略持仓。范围来自权威参考资料，
不是硬编码RB豁免名单；范围内任何未知总仓（包括净仓为零的多空双向仓）仍拒绝
新会话，已有本策略组合使用自己的原文件恢复。

定期对账、停机、恢复今昨仓检查使用同一范围。账户PositionManager仍保留
全账户快照；范围外仓位作为本会话未归属账户事实保存，恢复后重新权威查询，
不会恢复为04的所有权。报告`final_gross`是全账户总仓，
`final_managed_gross`是04管理范围总仓，`external_gross`是范围外总仓。

本轮保留全账户活动订单检查、资金检查和物理账户单写锁；没有开放两个进程
同时下单或同一真实合约跨策略共享。01未选择该范围政策，原接管和恢复约束保持。
本机80项公共回归通过；04新增4项原生假API回归、当前修改后的原生集成和实际
Replay下单需Linux验证。完整同步和命令见
[管理范围交接](../../doc/HANDOFF/live/HANDOFF_CTP_POSITION_SCOPE_2026-10-10.md)。
# 独立策略执行与账户仓位政策（2026-10-11）

新增`--mode model --strategy-position-source model --account-reconciliation off`：
保持原横截面信号算法及完整同步帧规则，只连接MD；按参考价模拟成交更新组合
策略仓位，不创建TD或取得账户锁。01和04可以各用独立模型状态文件同时运行。
恢复使用原文件加`--resume`，执行来源政策必须相同，仍重新预热动量窗口。

原生SimNow默认strict。`--account-reconciliation observe/off`分别只观测或不查询
物理仓位，策略仍根据归属成交管理自己的仓位；原生资金、订单恢复及账户锁保留。
报告的`execution_positions`是本执行端账本，`account_positions`是物理观测或None。

通过`build_runtime(args, account_routes=...)`或`assemble(..., account_routes=...)`装配
多个原生/第三方账户；倍率及成交归属由公共执行层处理，strategy.py无需修改，
也不引用01的代码。见[策略执行交接](../../doc/HANDOFF/live/HANDOFF_STRATEGY_EXECUTION_2026-10-11.md)。

2026-10-11远程模型运行及同文件恢复均已通过。首次运行9帧/6次调仓/12笔
模型成交，AU=0、AG=+1、SC=-1；恢复从目标版本6接续至7，重新预热第4帧才
执行新目标，本轮9帧/6次调仓/6笔成交，最终AU=0、AG=-1、SC=+1。
目标、策略仓位、执行账本一致，未归属仓位全为0，status=passed。
原文件model-cross-section-20261011-080812.json已保存后一次的仓位，后续
恢复须继续使用原路径。双策略同时运行及第三方多账户真实成交仍待验收。
完整证据见[模型验收记录](../../doc/HANDOFF/live/HANDOFF_MODEL_REPLAY_2026-10-11.md)。

