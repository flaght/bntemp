# 08：期权Vega策略

以**回测、Live、自定义特征**为三条主线。两个入口使用相同的策略、风险校验机制和特征契约，执行与仓位由`bomber/framework`负责。默认MO认购、同月IM和000852指数；IV与Delta使用同月期货价格，选约/增仓/退出/对冲保持原strategy1逻辑。

本文对应当前代码与随附配置，历史交付文档中的命令不作为当前默认值。阅读入口：

- [策略逻辑与计算公式](#1-策略逻辑)
- [回测启动与数据要求](#2-回测)
- [Live启动、恢复与持续管理](#3-live)
- [自定义特征生成与使用](#4-自定义特征)
- [启动参数、策略参数与默认值](#5-参数参考)
- [验证、日志及常见错误](#6-验证与诊断)

## 目录

| 文件 | 职责 |
| --- | --- |
| `run_backtest.py` | 文件准备、公共模拟Backend/结算接线及回测报告 |
| `run_live.py` | 在线参数、行情/参考/执行渠道选择、期权时段接线及公共常驻调度启动 |
| `run_worker.py` | IPC worker入口，复用公共worker，不创建CTP交易连接 |
| `feature/` | 特征生成/导出、声明加载、标准文件读取及回测分钟帧适配 |
| `strategy.py` | 策略决策、实际自有仓位管理与执行回报处理 |
| `vega_signal.py` | 策略配置、共享JSON参数声明、风控限额转换、数值算法与信号状态 |
| `live.py` | Live分钟帧适配、健康检查、公共执行装配与只读诊断 |
| `recovery.py` | 应用恢复/跨日约束、Recording窗口状态续接，调用公共账户与持久化能力 |
| `config/strategy.yaml` | 启动模式、数据路径、交易日、输出目录与依赖JSON路径 |
| `config/vega_backtest.json`、`config/vega_live.json` | Vega预算、选约/对冲时点和风险参数 |
| `config/market_dolphindb.json` | 公共行情源配置：流表、时间标签、量口径和时效 |
| `reports.py` | 复用公共交易日报告，追加08信号及保存只读策略快照 |

解析、结算、模拟撮合、目标差额、OMS、归属、风险、日历及共享账户复用[框架](../../bomber/framework/README.md)。探针/验收在[scripts/integration/options](../../scripts/integration/options)，测试在[tests](../../tests)。目录不保留旧模块的转发壳。

回测在`run_backtest.py`、Live在`live.py`创建`OptionVegaStrategy`，声明各自的行情周期、来源及执行路由，并直接调用公共`assemble_strategy()`；策略文件不负责运行环境装配。

同步到服务器时须同步本次删除项，不能只覆盖新增文件。根目录`features.py`已迁入`feature/`包；策略目录下的`continuous.py`已合并到`run_live.py`与`recovery.py`，`configuration.py`的两个共享函数已合并到`vega_signal.py`，须删除服务器上的这些旧文件，不保留转发壳。持续运行由公共`bomber/framework/trader/runtime/live/continuous.py`维护，YAML/JSON解析由公共`bomber/framework/cli.py`维护。旧辅助模块导入路径已由工厂、脚本和测试统一替换；两个运行入口及JSON检查点格式保持不变。

## 1. 策略逻辑

实现见[strategy.py](strategy.py)，数值公式与配置见[vega_signal.py](vega_signal.py)。本节描述当前执行行为，公式例子不是实际回测或成交结果。

### 1.1 组合目标、输入和数量含义

策略每天在指定时点新增认购期权空头，用Vega预算决定新增手数，在若干对冲时点用同月期货调整已成交期权的Delta敞口。当前没有均线交叉、指数突破或“IV高于历史波动率”才开仓的条件。

| 输入/数量 | 用途 |
| --- | --- |
| 指数特征值`I` | 作为执行价过滤基准，要求`K > I`；可以是原始指数、MA20或价格量纲的组合特征 |
| 同月期货收盘价`F` | 反解IV、计算Greeks、评估退出，不能直接用主力合约或指数特征替代 |
| 期权收盘价`C`、执行价`K` | 定价、时间价值过滤及退出指标 |
| 期权/期货条款及交易日历 | 上市期、到期月、剩余交易日、乘数和合约映射 |
| `actual` / 日志`positions` | 框架已经记账的**本策略**净仓，负数为空头；不是全账户总仓 |
| `working` | 本策略仍在途的带方向委托数量 |
| `option_goals` / `future_goals` | 希望达到的目标净仓，不是本次买卖增量，也不证明已成交 |

Recording只记录目标，没有成交持仓；Model的持仓来自模型成交；SimNow来自归属到本策略的柜台成交。多个策略可以持有同一合约，持仓所有权由公共归属账本与策略ID确定，不采用“每个品种只能由一个策略管理”的约束；同一物理账户的执行写入仍需通过共享账户服务协调。

### 1.2 每个Bar是否都会交易

当前帧路径以`valuation_ns`作为估值时点，以`request_ns`记录实际消费/请求时点，按上海时间判断日内时点。上游封闭帧后不再补入迟到数据；封闭不代表所有候选都齐全。

每帧按以下顺序处理：

1. 读取实际仓位和在途数量，保护已停止交易、待现金结算的期权。
2. 恢复后等待持仓依赖获得新场次行情，未齐返回`RESUME_WAIT`，超时按实际缺失原因报错。
3. 到达显式最终清仓日/时点时优先清仓，并返回。
4. 到达当日`option_slot`时选约、增仓，再评估期权退出。
5. 到达`hedge_slots`时评估期货Delta对冲。
6. 业务目标有变化才提交完整组合，执行端根据实际仓位及在途计算订单差额。

不是每分钟都会下单；错过`option_slot`不会在之后任意分钟补开。M5期权之间可由M1期货/指数继续推进决策，旧期权报价保留其真实时间并接受时效检查，不读取未来M5价格。

### 1.3 选月与选执行价

只有当前时点等于`option_slot`、当天尚未处理选约，并且没有期权在途委托时，才尝试选约。当前JSON模板设为14:00；不加载模板的原始CLI默认是13:58。

从外部提供的当日认购链筛选：

1. 合约仍在上市区间，尚未达到停止交易时点。
2. `K > I`，剩余交易日不少于`min_remaining_days`。
3. 期权、对应同月期货、指数特征的缓存时点都等于本帧时点。
4. 在以上合格合约中取最近的最后交易日，再计算该月份的Greeks。
5. 每个`delta_targets`档取Delta距离最小的候选；同一合约可命中多个档。

这里使用的是多头认购正Delta；卖出后风险方向由负手数体现。选择最近合格月不等于读取主力标记换月；所选月IV全部不可用时，不自动再选下一月。无候选、整手配仓为零或累计限额拒绝后，当日机会也视为已处理。存在期权在途而跳过时，不会自动延后到另一个配置外时点。

### 1.4 IV、Greeks与缓存

`source_iv`使用同月期货价反解Black76认购IV，搜索区间为`[0.0001, 5.0]`，二分64次，保留源代码的价格边界和迭代行为。设波动率为`sigma`、交易年期限为`T`：

```text
d1 = (ln(F/K) + sigma² × T / 2) / (sigma × sqrt(T))
d2 = d1 − sigma × sqrt(T)
C  = exp(−r × T) × (F × N(d1) − K × N(d2))
```

`N`为标准正态累计分布，`phi`为密度。当前Greeks沿用源口径：

```text
Delta = N(d1)
Gamma = phi(d1) / (F × sigma × sqrt(T))
Vega  = phi(d1) × F × sqrt(T) × exp(−r × Tc)
```

剩余交易日`Ndays`不含当日、包含最后交易日。开仓`T=Ndays/252`；对冲另外加当日剩余分钟`R/(240×252)`。`Tc`为到最后交易日的自然日差/365，用于Vega折现。定价函数保留源代码全天240分钟、15:00结束的期限口径；Live是否在14:57暂停由外部期权日历决定，两者职责不同。

只有`C − max(F−K, 0) >= min_time_value`才尝试新IV。新计算不可用时，可使用`max_iv_age_seconds`内的旧IV，并用当前价格和期限重算Greeks；不会刷新旧IV时间。没有可用IV返回空值，但缺少新鲜期权/期货价格仍报错，不能靠IV缓存绕过行情检查。当前IV算法不额外做搜索区间可达性及最终重定价误差检查。

### 1.5 Vega预算与新增手数

`nav`是策略的配置本金，不自动跟随账户净值复利变化。每日新增配仓共享预算：

```text
B = nav × vega_budget / 100
每个选中Delta档j：
    w_j = 1 / (Gamma_j × 期权乘数m_j)
    D   = sum(w_j × Vega_j × m_j)
    a_j = B × w_j / D
```

取整前理想条件下`sum(a_j × Vega_j × m_j)=B`，且各档的现金Gamma贡献相等，并非各档手数或Vega相等。重复命中同一合约先合并`a_j`，不会让每档分别再用一份预算。

`Vega`按`sigma`变化1.0计量。例如`nav=40,000,000`、`vega_budget=1.25`，`B=500,000`，对应波动率上升1个百分点的线性现金敏感度尺度为5,000。预算参数不是保证金比例或1.25倍本金。

合并后依次Python `round`取整、应用单合约增量上限、总量超限时同比缩放并`int`向下取整、去掉0手，最后转成负数空头增量。`round`为半偶舍入；缩放后的余数不再分配。因此实际配仓可能低于预算或为零，不保证达到理论Vega额度。

### 1.6 累加旧仓、常规退出与限额

当日负增量累加到已有**目标**，保留未被选中的旧合约。例如原目标A=-1，当日新增B=-1，最终A=-1、B=-1；再次选中A则可能变成A=-2。若超过累计单合约或期权总手数上限，拒绝整批新增，保留原目标，记录`RISK_LIMIT`。

每日分配使用新预算B，不先扣除旧仓Vega；累计风险不是通过“总组合Vega恰好等于B”控制，仍受手数和公共执行端限额约束。

同一`option_slot`内先处理新增目标，再对所有非零、未停止交易的期权目标评估：

```text
退出指标E = C − (F − K)
E <= 0 → 该期权目标设为0
```

这里没有`max(F−K,0)`，与IV筛选的时间价值口径不同。例：`F=7100,K=7000,C=100`时E=0，目标置零；`F=6800,K=7000,C=30`时E=230，不退出。`close_remaining_days`只是旧参数兼容，不是“剩2天必平”的现行规则。没有新候选时仍可评估旧目标退出；若有期权在途，本帧选约和常规退出均不执行。

新增与退出在本帧合并后提交，不强制先开仓再平仓。目标置零之后仍需真实成交才能认为实际持仓已归零。

### 1.7 Delta对冲与零期货仓

只有到达`hedge_slots`、该日/时点尚未处理、所有期货腿无在途委托时才对冲。按期权的**已成交actual**分同月期货汇总：

```text
exposure_f    = sum(actual_option_lots_i × option_multiplier_i × Delta_i)
future_goal_f = round(−exposure_f / future_multiplier_f)
```

例如空1手认购、乘数100、Delta=0.30、期货乘数200，计算`round(0.15)=0`；所以有期权持仓而没有期货仓位不必然异常。空10手时为`round(1.5)=2`。所需手数超过`max_future_lots`时报错，不静默截限。

这是期货最终目标，不是新增买卖量。刚提交但未成交的期权新增不提前纳入；待平但未成交的期权仍属于敞口。普通`on_fill`不会即时触发对冲，通常等下一配置时点。某月任一期权缺少可用IV时，保留该月原期货目标；本次时点仍记为已评估，不在后续任意分钟重放。

### 1.8 最终清仓、现金结算和恢复

回测把最后一个交易日作为`final_day`；Live仅显式提供`--flatten-day YYYYMMDD`才指定主动清仓日。到当天`flatten_slot`及以后优先清仓，不再选约或对冲；有在途先等待，后续帧重新检查。`flatten_requested=true`只表示进入过清仓分支，不证明全部成交。

正常午休、收盘或操作员停止是保存持仓的生命周期操作，与策略主动清仓不同。已停止交易的期权不再发送普通平仓，而由公共现金结算模块处理实际残仓；回测缺最终结算资料不能用最后close替代。Live成交事实来自柜台，模拟结算不会写成真实账户事实。

Model/SimNow恢复后以已归属成交数量重建目标，不继续旧会话未完成开仓意图，等待持仓依赖更新。Recording只续接内存目标与每日选约标记，不把零成交当成旧目标被平掉；重启Recording进程是新的观察会话。

## 2. 回测

在安装Bomber原生扩展及依赖的环境中，从项目根目录运行：

```bash
python -u demos/08_option_vega/run_backtest.py \
  --data-root /实际数据根目录 \
  --index-feature-file /实际路径/index-minute.csv \
  --calendar-id china.cffex \
  --option-product MO --future-product IM --index-code 000852 \
  --start-day 2026-09-01 --end-day 2026-09-11 \
  --frame-wait-seconds 5 --bar-timestamp end \
  --log-level WARNING --no-tearsheet --require-fills
```

`--data-root`按公共路径规则推导交易腿与基础条款；期货与资料可逐项传`--fut-dir --fut-basic --opt-basic`。期权M1固定使用`data_root/kline/opt`，自定义`--opt-dir`仅用于M5。不读取`.env`补文件路径。完整参数见`python demos/08_option_vega/run_backtest.py --help`。

### YAML启动参数与JSON策略参数

回测和Live入口均复用公共`bomber.framework.cli.ConfigArgumentParser`，共用[config/strategy.yaml](config/strategy.yaml)，通过不同配置ID选择各自参数。安装YAML读取依赖后，可直接启动回测：

```bash
python -m pip install -r bomber/framework/requirements-config.txt
python -u demos/08_option_vega/run_backtest.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id m5_index_blend
```

配置采用`experiment_setups -> 配置ID -> 参数名/值`，`description`仅作为说明。`m5_index_blend`与M5启动命令一致：期权M5、期货/指数M1、14:00开仓、原生日历、`index_blend`列以及`next_market`撮合；`opt_dir`须指向真实M5文件。`live_recording`、`live_model`、`live_simnow`分别用于三个Live执行模式。各入口只加载选中的配置，不混入其他ID的参数；把回测ID传给Live会报未知参数。复制一组参数即可添加实验，不需要修改Python。

模板中的`data_root`、M5 `opt_dir`、特征文件路径和起止日期是部署示例，首次使用必须改成当前机器实际覆盖的数据。可直接覆盖路径和日期，无需修改代码：

```bash
python -u demos/08_option_vega/run_backtest.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id m5_index_blend \
  --data-root /实际路径/数据根目录 \
  --opt-dir /实际路径/M5期权目录 \
  --index-feature-file /实际路径/index-blend.csv \
  --index-feature-config /实际路径/index-blend.csv.dataset.json \
  --start-day 2026-07-01 --end-day 2026-08-24
```

上述特征文件需真正覆盖所选日期、代码和历史可见时间。修改回测日期不会生成缺失特征或行情。

启动YAML引用`strategy_config: demos/08_option_vega/config/vega_backtest.json`或`vega_live.json`；策略JSON保存`nav/vega_budget/delta_targets/option_slot/hedge_slots`和持仓、名义本金等限额。回测与Live保留各自原有手数上限，不把回测100手配置直接用于Live。`delta_targets/hedge_slots`在JSON中使用列表，仍复用原有CLI类型和`OptionVegaConfig`业务校验。

Live启动YAML通过`market_config`引用`config/market_dolphindb.json`，原始指数Schema仍在`feature/index.json`。账户凭据继续从用途专属环境变量读取，不放入JSON。日期、模式、状态文件、连接/报单授权和运行时长放YAML，策略JSON不允许这些字段。

优先级为**显式CLI > 启动YAML > 策略JSON > 原有默认值**；YAML中的`null`沿用下一层。现有CLI或旧YAML内的策略参数仍可作为显式覆盖，不自动改写用户文件。仅使用CLI时也可传`--strategy-config /路径/参数.json`，无需YAML依赖。例如同一数据集改用MA20：

```bash
python -u demos/08_option_vega/run_backtest.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id m5_index_blend \
  --index-value-field ma20 \
  --report-dir tests/results/backtest/option-vega-m5-ma20
```

配置键使用`start_day`、`index_value_field`等参数名，也接受`start-day`形式；`--config_file/--config_id`保留下划线别名。日期、时点和指数代码应加引号，如`"2026-08-13"`、`"14:00"`、`"000852"`；布尔开关写`true/false`，可重复参数（如`commission_product_multiplier`）使用YAML列表。所有相对路径（包括YAML引用的JSON）与普通CLI一致，按运行目录解析，示例应在`pro`根目录启动。

文件不存在、ID不存在、重复键、未知参数、类型/枚举或互斥选项错误均停止启动；配置不会新增任意Namespace属性，也不会绕过M5对齐、数据覆盖或特征可见时间检查。回测与Live报告的`parameter_config`均记录文件路径与配置ID。普通CLI和`--help`无需PyYAML，也不强制使用配置文件。原`config/backtest.yaml`已统一更名为`config/strategy.yaml`，同步时删除旧文件并更新启动路径。

- 分钟行情使用真实期权与同月期货；`--bar-timestamp start/end`声明交易腿源标签，期权可用`--options-bar-timestamp`单独覆盖。不会以前一分钟价格补造同步开仓输入。
- 指数是标准特征CSV/Parquet，旁边必须有同名`.manifest.json`。Schema/计算版本、原始字节SHA256与时间标签均核验。传输需保留文件字节，包括换行格式。
- 可传`--calendar-id china.cffex`直接调用框架`NativeCalendarProvider`，无需导出或加载日历文件。日历保留回测开始日至规则维护末端的日期，回测行情仍只加载`--start-day`至`--end-day`，避免在回测末端把日历不足误判成临近到期。报告记录日历标识、规则版本、指纹和实际交易日范围。当前原生日历维护至2026-12-31；超出维护范围或原生扩展不可用时明确报错，不回退为工作日猜测。
- `--calendar`仍可提供完整`date/is_trading_day`文件，与`--calendar-id`互斥；两项均省略时沿用已知行情日期推导。期权到期判断需要足够的后续交易日；超过已知日历末端时，只有剩余日期下界已达到对应阈值才允许继续。
- 每日期权交易时点默认13:58，对冲默认10:00/11:00/13:30/14:00/14:55。每日新增目标累加，持仓使用成交归属数量；总量与单腿限额仍生效。
- 最后回测日默认14:50清仓，指数还须至少覆盖之后两分钟，交易腿也需真实后续报价完成撮合。`remaining_positions_or_working`和`report_errors`必须为空。

日历日期不代表文件已覆盖该日期。按日预检会一次检查全部请求交易日，分别记录`MISSING_OPTION_CHAIN`与`MISSING_INDEX_FEATURE`：前者同时给出与条款匹配的认购文件数和上市期内候选数，后者给出当日指数特征分钟数。失败日志展示前10个缺失日期及实际期权目录、条款与特征路径；全部缺失项写入本次`<report-dir>/input_failed_*/input_issues.json`。预检不会跳过缺失交易日或自动缩短回测区间。若M5目录只覆盖部分日期，修改开始日期不能补齐区间内的缺口。

### 使用期权M5、期货M1行情

默认仍为期权M1。若已有真实五分钟期权Feather，在原回测命令中增加：

```bash
--opt-dir /实际路径/五分钟期权目录 \
--option-bar-minutes 5 \
--option-slot 14:00 \
--options-bar-timestamp end \
--execution-timing next_market
```

路径随期权周期选择：`--option-bar-minutes 1`必须提供`--data-root`，读取其`kline/opt`，即使YAML仍保留M5的`opt_dir`也不会使用；`--option-bar-minutes 5`必须显式配置`opt_dir`或传`--opt-dir`，不回退读取默认M1目录。启动日志打印最终周期与实际目录。

因此同一配置切回M1只需：

```bash
python -u demos/08_option_vega/run_backtest.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id m5_index_blend \
  --option-bar-minutes 1
```

这只切换期权周期和数据目录；配置中的14:00开仓时点仍保留，需要改回13:58时再显式传`--option-slot 13:58`。

读取规格、策略订阅和引擎回放分别声明期权`5-MINUTE`、期货`1-MINUTE`；指数自定义特征仍为一分钟。复用公共`BarReadSpec`和撮合网关，与12示例使用相同多周期能力，不转换或填充期权M5为M1。这个参数声明源文件已经是M5，不能用来修复M1文件缺口，也不自动聚合M1。

开仓使用同一时点的完整三腿行情。M5模式须显式选择五分钟完成时刻，如14:00；不会把默认13:58自动改成14:00，也不把14:00完整Bar提前用于13:58。起点标签M5以5分钟右移；期货起点标签仍以1分钟右移。旧M1命令与Live默认订阅保持原样。

期货M1继续推进分钟决策，M5之间只保留此前真实期权报价的原时间，并按原有新鲜度限制使用，不读取下一根M5。`next_market`由公共网关等待下单后本合约下一根真实Bar：期货不等待期权M5，期权也不会因期货M1到达而提前放行。它使用下一根完整Bar处理后的盘口，不承诺下一根开盘价。默认`native`保留原有撮合行为，建议M5研究时显式选择`next_market`。

`summary.json`记录`bar_specs`、各类时间标签和`execution_timing`。缺少开仓时点的真实三腿行情仍报错；到期实际残仓仍由公共现金结算模块处理。

期权OHLC默认严格拒绝（`option_ohlc_policy: reject`）。若研究数据存在高低价未包围开收价的异常，可显式在启动YAML中设置`option_ohlc_policy: expand_range`，或传`--option-ohlc-policy expand_range`，复用公共读取器在内存中将high扩为原OHLC最大值、low扩为最小值。原始Feather不改写，open、close、volume及时间保持原值；零成交量Bar不因此删除。读取器保留`source_high/source_low`并在`input_issues.json`写入`OHLC_RANGE_EXPANDED`警告，`input_coverage.json`记录读取策略，`summary.json`记录实际选项。此项仅影响期权文件回测，期货仍严格拒绝。扩展区间是明确的研究修整假设，可能影响原生Bar撮合及收益；volume=0不能证明哪一个价格正确，正式数据仍需核对来源。

### 到期现金结算

期权使用公共`OptionCashSettlementModule`，IF/IH/IM期货对冲仓使用同一公共流程的`FutureCashSettlementModule`。
YAML启动参数可分别配置两个来源，以下为部署路径示例，须按实际目录填写：

```yaml
    option_settlements: /root/bbnt/data/daily/opt
    future_settlements: /root/bbnt/data/daily/fut
    settlement_available_time: "16:00"
```

也可在回测命令中追加：

```bash
--option-settlements /实际路径/官方期权日行情目录 \
--future-settlements /实际路径/官方股指期货日行情目录 \
--settlement-available-time 16:00 --exercise-fee-per-contract 0
```

期权、期货目录内文件均为`YYYY-MM-DD.feather`，统一固定读取`code,trade_date,settlePrice`，只需指定目录，不配置列名。
日行情使用已确认的固定格式：`trade_date,code,secShortName,exchangeCD,preSettlePrice,preClosePrice,openPrice,highestPrice,lowestPrice,closePrice,settlePrice,turnoverVol,turnoverValue,openInt`。
额外行情列不参与结算校验；只按opt_basic/fut_basic指定的到期日读取本次合约，code须为真实合约，例如IM2605。
此前结算价列名拼写有误，读取器现统一使用`settlePrice`；旧的三个`future_settlement_*_column`配置项已移除，应从启动YAML中删除。

也可传标准CSV/Parquet/Feather结算文件，列为`symbol,last_trade_date,settlement_price`，可选`available_at,fee_per_contract`。
期权允许零终值；期货最终交割结算价必须为有限正数。没有实际残仓可不提供终值，有残仓缺终值则失败。
来源应保证所选到期日价格字段确为最终结算价，不使用分钟close或其他月份价格代替。
`settlement_available_time`共用于两个来源，逐行`available_at`优先；16:00是配置假设，不是对来源实际发布时间的证明。
期货结算费按每手配置或文件`fee_per_contract`收取，不重复套用普通成交费及`delivery_fee_multiplier`。

期货最后交易日来自fut_basic，期权来自opt_basic；截止后撤销未终结委托，并停止普通交易。
公共结算模块按权威终值生成了结成交，更新原生账户、持仓和各策略归属；08收到回报后清零对应目标。
因此旧月IM在结算后不会再成为下一交易日恢复所需的行情腿。结算回报前保留实际残仓，不能仅将字典置零冒充了结。
缺少持有合约的终值时，分别明确提示`--option-settlements`或`--future-settlements`，不等到跨日行情过期才失败。
该接线仅用于文件回测的IF/IH/IM现金交割，不扩展商品期货实物交割，也不替代Live柜台的权威结算和仓位核对。

普通成交费用由公共FeeModel处理，结算费用单独记录。模拟仍使用基础NETTING/Bar撮合与比例保证金，不代表完整卖方保证金、期货交割或实物行权。

回测默认保存在`results/backtest/<期权品种>/<开始日>-<结束日>/<run-id>/`：`summary.json`、`orders/fills/positions/account.csv`、`signals.csv`、期权`settlements.csv`、期货`future_settlements.csv`、`feature_frames.json`、`strategy_parameters.json`、输入诊断和费用报告。summary分别记录两个结算来源、已加载终值数量和实际结算次数；启动时也打印两个来源与已加载数量。`--report-dir`替换根目录；运行编号保留，重复回测不覆盖此前结果。准备失败仍使用公共`<report-dir>/input_failed_*`诊断目录。

## 3. Live

Live分别配置交易腿行情、指数特征、参考条款和执行渠道。`recording`只记录目标；`model`使用参考价模型；`simnow`需要显式发单授权。连接凭证使用部署环境或项目`.env`，不写入命令与报告。

| 配置ID | 执行行为 | 检查点 | 同账户已有Hybrid写入进程时 |
| --- | --- | --- | --- |
| `live_recording` | 实时判断并记录目标，不连接TD报单，不产生成交仓位 | 不接受`state_file/resume` | 可作为独立行情观察进程运行 |
| `live_model` | 按参考价格模型执行，模型持仓不是柜台仓位 | 独立模型检查点 | 不占用TD；本文命令未配置账户跟随端 |
| `live_simnow` | 使用CTP SimNow成交回报管理自有仓位 | 与独立Live身份匹配的交易检查点 | 不另启同账户写入进程，应接入共享账户服务 |

### 环境准备

所有相对路径按项目根目录解析。用**实际启动的同一个Python**安装配置依赖，避免安装到另一个虚拟环境：

```bash
python -m pip install -r bomber/framework/requirements-config.txt
```

Live/特征生成需要用途专属DolphinDB环境变量：

| 变量组 | 用途 |
| --- | --- |
| `DDB_MARKET_HOST/PORT/USERNAME/PASSWORD/ENABLE_SSL` | 期权/期货分钟流与指数DFS查询；数据表映射在JSON声明 |
| `DDB_MARKET_STREAMING_PORT` | 分钟推送的本地订阅端口，例如`0`自动分配；不是数据库服务端口 |
| `DDB_REFERENCE_HOST/PORT/USERNAME/PASSWORD/ENABLE_SSL` | 条款和交易时段参考来源，需与实际参考服务器一致 |
| `DDB_REFERENCE_DATABASE` | 未显式传`reference_database`时的参考库；时段库可由YAML单独指定 |
| `CTP_*` | 仅选择CTP行情或SimNow交易时需要；Recording+DolphinDB命令不需要CTP登录 |

终端已`export`的值优先于当前`load_dotenv()`默认加载的`.env`。用途专属变量优先于兼容的旧`DDB_*`，只修改旧变量不会覆盖已有`DDB_MARKET_*`。例如切换行情服务器时，替换下方占位值后执行：

```bash
export DDB_MARKET_HOST='新的行情服务器地址'
export DDB_MARKET_PORT='8902'
export DDB_MARKET_USERNAME='实际用户名'
export DDB_MARKET_PASSWORD='实际密码'
export DDB_MARKET_ENABLE_SSL='false'
export DDB_MARKET_STREAMING_PORT='0'
```

这不会自动切换参考服务器；按部署另行配置`DDB_REFERENCE_*`。期权/期货条款表名可由`DDB_OPT_BASIC_TABLE/DDB_FUT_BASIC_TABLE`指定，时段库表在YAML中显式声明。凭据不写入版本化YAML/JSON。

SimNow交易需要`CTP_TD_ADDRESS/CTP_BROKER_ID/CTP_ACCOUNT_ID/CTP_PASSWORD`，本入口要求BrokerID为`9999`；`CTP_APP_ID/CTP_AUTH_CODE`按环境提供，`CTP_PRODUCTION_MODE`默认true。仅当交易腿行情选择CTP时还需要`CTP_MD_ADDRESS`；使用DolphinDB行情不会因此订阅CTP行情。`CTP_TD_FLOW_PATH/CTP_MD_FLOW_PATH`可覆盖连接流文件目录，仍不能绕过账户写入锁。

### Recording常驻与定时验收

同一配置文件选择Live Recording：

```bash
python -u -m demos.08_option_vega.run_live \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id live_recording \
  --run-forever
```

三个Live示例默认`run_forever: true`，交易日由公共会话日历的窗口提供，`expected_trading_day: null`不固定日期。可显式传`--expected-trading-day YYYYMMDD`核对首个窗口；后续窗口随日历推进。三个Live示例使用DolphinDB完成分钟流与原始指数Bar投影，期权仍为M1；流表名、时间标签及累计量口径在`config/market_dolphindb.json`中声明，须与实际来源一致。

使用`live_model`或`live_simnow`前，在同一YAML中填写对应的`state_file`：新会话使用独立新文件，恢复已有会话同时设置`resume: true`。模板保留`null`，未填写时原有检查点校验会阻止启动，不自动创建空状态接管已有仓位。`live_simnow`已明确设置`enable_orders/confirm_simnow: true`；任一授权缺失、Recording带报单参数或Model带物理发单授权仍报错。恢复时仍需通过原有身份与仓位核验，不能把共享账户检查点直接交给独立`run_live`入口。

关闭常驻、按指定交易日运行900秒：

```bash
python -u -m demos.08_option_vega.run_live \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id live_recording \
  --no-run-forever --expected-trading-day 20261106 --seconds 900
```

日期须替换为实际会话交易日。定时模式不由常驻调度器等待开盘，盘前启动仍可能因没有行情而超过就绪期限；盘前/跨休市使用`--run-forever`。

### Model新建与恢复

选择一个不存在的模型检查点路径，启动新会话：

```bash
python -u -m demos.08_option_vega.run_live \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id live_model \
  --state-file demos/08_option_vega/results/state/model.json \
  --run-forever
```

重启恢复同一检查点，增加`--resume`：

```bash
python -u -m demos.08_option_vega.run_live \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id live_model \
  --state-file demos/08_option_vega/results/state/model.json \
  --resume --run-forever
```

这里的原仓位是模型成交账本，不会接管同账户的柜台持仓。检查点已存在却不传`resume`，或传了`resume`但文件不存在，均会报错。

### 独立SimNow与共享账户入口

当该账户没有其他写入进程、且需要恢复与独立入口身份匹配的原检查点时：

```bash
python -u -m demos.08_option_vega.run_live \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id live_simnow \
  --state-file /实际路径/独立Live原检查点.json \
  --resume --run-forever --enable-orders --confirm-simnow
```

新会话使用独立的新路径并省略`--resume`，须通过启动时账户/持仓核验，不能用空文件规避原仓归属。SimNow跨日自动恢复沿用当前`strict`和`fills`约束，不更改账户、策略、来源或风险参数来绕过身份校验。已有共享账户检查点使用后文的`scripts.integration.options.shared`，不与上述独立检查点互换。该命令也不支持把08热加入正在运行的Hybrid进程。

常驻复用公共`run_continuous`，不用另写循环。交易时段从`dfs://bomber_static/fut_trading_session`读取**期权品种MO**的规则（IO/HO对应各自规则），不能套用IM/IF/IH期货时段。本文部署示例采用MO的09:30–11:30、13:00–14:57规则，实际以时段表为准；`no_session_overrides: true`表示部署已明确没有额外日期例外，有例外时应配置公共例外表并关闭此声明。

盘前启动先等待开盘，不在9:10启动行情就绪倒计时；午休、收盘按窗口关闭决策，完成各模式的停机核验/检查点保存，SimNow处理撤单及最终对账，保留成交仓位，进程继续等待。下一窗口重新加载当日资料；Model/SimNow恢复原归属账本并校验持仓条款，Recording只续接内存中的每日选约标记和目标，不伪造成交。Recording重启进程后是新观察会话；持仓合约从当日资料消失、来源身份变化或停机对账失败仍会报错，不丢弃原仓或盲目重启。SimNow当日报单额度累计包含午休前和重启前的订单，额度用完等待下一交易日。

常驻期间`seconds`不作为退出条件。按Ctrl+C或SIGTERM正常清理后退出；若需原900秒验收，使用`--no-run-forever --expected-trading-day YYYYMMDD --seconds 900`。该入口不绕过公共账户锁：已有Hybrid占用同一CTP账户时可并行启动08 **Recording**；08独立SimNow不能作为第二个账户写入进程启动。多策略实盘仍使用共享账户服务。下面的`shared --run-forever`入口和检查点保持原调用方式。

### 按交易日归档

默认目录示例：

```text
results/
  live/MO/option-vega-recording-MO-20261106/
    summary.json              # 最新会话摘要
    sessions.jsonl            # 当天各次会话摘要，追加
    signals.jsonl             # 实时策略信号，追加
    events.txt                # 执行事件，追加；停机再次收集迟到回报
    strategy_parameters.json  # 最新会话实际生效参数
    strategy_state.json       # 最新自有仓位/目标/决策，只读报告快照
    feature_frames.json       # 最近最多256帧诊断，非完整日历史
  backtest/MO/20260813-20260930/<run-id>/
```

`report_dir`为可指定的输出根目录，品种、执行模式及会话交易日决定子目录；同交易日重启继续追加明细，下一交易日另建目录。定时模式日期来自显式参数，常驻来自日历窗口，并由对应生命周期核验；Recording不登录TD，不能把其日期当成已核验的柜台日期。报告不迁移旧纳秒目录，也不改变原`state_file`、归属账本或恢复格式；`strategy_state.json`不能替代正式检查点。特征生成仍按显式`--output-dir`与原始事件日期区间导出，不把研究快照改写为当天行情。

例如，以DolphinDB分钟推送和DFS指数记录实时目标（日期按柜台实际交易日替换）：

```bash
python -u -m demos.08_option_vega.run_live \
  --connect --mode recording --expected-trading-day 20261105 \
  --market-source dolphindb --market-bar-table stream_cffex_1min \
  --market-bar-timestamp end --market-volume-mode cumulative \
  --market-bar-max-age-seconds 120 \
  --index-source dolphindb-dfs --index-poll-seconds 1 \
  --frame-wait-seconds 20 --seconds 900
```

表名、时间标签和量口径须按真实来源声明。CTP行情可显式选择`--market-source ctp`；指数仍独立通过特征渠道接入。完整参数见`python -m demos.08_option_vega.run_live --help`。

已有归属检查点的持续SimNow管理，继续使用：

```bash
python -u -m scripts.integration.options.shared \
  --connect --enable-orders --confirm-simnow \
  --calendar-id china.cffex --hours-code MO \
  --trading-hours-database dfs://bomber_static \
  --trading-hours-table fut_trading_session --no-session-overrides \
  --state-file /实际路径/已有账户检查点.json \
  --index-poll-seconds 1 --max-session-orders 20 \
  --report-dir tests/results/gap08-shared-continuous --run-forever
```

保留原账户、信号检查点和策略参数。不能通过空状态或手改JSON接管已有仓位。公共日历控制交易窗口，共享执行支持同进程及不同进程布局；具体配置见[共享账户入口](../../scripts/integration/shared_account/README.md)。正常停止保留成交仓位，按一次Ctrl+C后等待撤单核验与保存完成。

08旧账本加入已有Hybrid共享账户前，可先执行只读合并预检：

```bash
python -u -m scripts.integration.shared_account.join_option \
  --hybrid-config demos/10_hybrid_transformer/config/shared_rb_p.json \
  --option-state /实际路径/原08归属检查点.json \
  --strategy-limits-only
```

该入口仅提供只读预检，`can_start_orders`始终为false。`--strategy-limits-only`只报告取消联合手数/名义限制的计划，不修改原配置或状态。实际注册与不同交易时段的worker启动见下一节。

关注`分钟行情汇总`、`分钟决策汇总`与心跳。迟到的新Bar可只更新健康，不回填旧决策；真实过期、断流或账户冲突仍停止。模拟现金结算不用于生成Live柜台事实。

### 独立账户服务与原生策略入口

账户JSON只描述账户，策略JSON只描述策略。账户日常启动不读取08/10目录的JSON，不导入策略工厂、
模型或信号配置。三个进程分别运行：公共账户、08的一个Vega实例、10的RB/P两个实例。

```text
scripts/integration/shared_account/
├── account.json                     纯账户配置，无strategy_configs或registration
├── account.env                      账户自己的CTP环境，已忽略提交
├── launch.yaml                      账户启动参数
└── results/account/state.json       账户自己的订单、成交、归属及执行登记

demos/08_option_vega/
├── config/strategy.yaml             08启动配置及live_simnow业务参数
├── config/vega_live.json             08期货/期权手数和策略参数
└── results/live/state/worker_signal.json

demos/10_hybrid_transformer/
├── config/strategy.yaml             10启动配置
├── config/instances_rb_p.json        仅RB/P实例，无账户字段
└── results/live/state/              原rb_signal.json、p_signal.json
```

`account.json`中的所有相对路径以该JSON所在目录为基准。`account_env_file: account.env`、
`account_state_file: results/account/state.json`及`report_dir: results/account`都位于公共账户目录。
08/10启动YAML中的`env_file`和`account_config`相对于项目工作目录；策略分别读取`work1/env1`和`work2/env2`，
再通过IPC连接账户服务。账户不读取这些策略env，也不读取其模型/行情/信号JSON。

账户账本保存纯执行登记：已登记策略ID、真实合约、最小变动价位、乘数、执行限额和公共市场时段快照。
这些是恢复订单和归属所需的执行事实，不包含策略argv或业务文件路径；恢复市场时段无需查询策略的DDB。
策略自行准备行情、参考与模型，连接时核验自身信号身份；账户只验证执行声明。
已登记策略可在原产品、原有限额内刷新空仓路由，不新增策略身份，不移除非零持仓/目标，不跳过未确认回报。
执行登记、路由、归属、报单计数及事件日志在同一账户检查点代次保存。

账户仍保留`max_gross_quantity/max_gross_notional: null`和共享TD每交易日200笔；各策略自身限额生效。
执行client_id沿用历史值以保留订单身份，不代表公共账户属于10。两个策略的ID、模型、手数、批次和信号路径保留。

**切换前：**正常停止原账户、Hybrid及08交易进程，等待撤单、回报确认和保存完成。同步本次完整代码与框架包，
两个Python环境都要能加载新的框架模块。先为账户创建独立env（不打印内容）：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
install -m 600 work2/env2 scripts/integration/shared_account/account.env
```

也可参考公共目录的`account.env.example`只填写账户CTP字段。首次切换不要创建空的`results/account/state.json`。

**旧文件只在首次迁移命令中显式提供。**原`workers.json`现改名为`legacy_workers.json`，只作为迁移来源和旧部署兼容声明。
它引用原`shared_rb_p.json`、08旧注册声明及原检查点；不会由新账户入口读取。10日常启动只读取新的`instances_rb_p.json`。

保留此前已核对的`VEGA_OLD_STATE`，并把`VEGA_TD_DAY`设置为CTP实际返回的交易日，例如20261110。
先执行纯文件预检：

```bash
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
  /workspace/worker/env/chs-live/bin/python -u -m scripts.integration.shared_account.migrate_account \
  --account-config scripts/integration/shared_account/account.json \
  --source-config scripts/integration/shared_account/legacy_workers.json \
  --trading-day "$VEGA_TD_DAY" --option-checkpoint "$VEGA_OLD_STATE"
```

看到`status: files_checked`后，在同一命令末尾追加`--connect --apply`，禁单核验后迁移：

```bash
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
  /workspace/worker/env/chs-live/bin/python -u -m scripts.integration.shared_account.migrate_account \
  --account-config scripts/integration/shared_account/account.json \
  --source-config scripts/integration/shared_account/legacy_workers.json \
  --trading-day "$VEGA_TD_DAY" --option-checkpoint "$VEGA_OLD_STATE" --connect --apply
```

未合入08时，默认原08信号文件为旧检查点文件名追加`.vega-signal.json`；也可用`--option-signal`指定。
若原账户已注册08，则直接迁移该完整账本，不重复导入旧08。源配置、信号/批次、归属、未决订单和每日额度均需匹配。
此前出现的“检查点200、原shared_rb_p.json配置100”仍须先核对并恢复原源配置，不能修改检查点绕过。
迁移过程使用临时账户副本完成柜台核验和最终停机检查，成功后才创建新的公共账本，并保存原代次来源备份；
原账户、原08检查点及两个策略信号文件均不改写。看到`status: migrated`并正常退出后，开始日常三终端启动。

**终端1先启动账户服务：**只需CTP与框架环境，不需要Hybrid模型依赖。

```bash
PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python -u \
  -m scripts.integration.shared_account.account \
  --config-file scripts/integration/shared_account/launch.yaml --config-id workers_account
```

**终端2从10原生入口启动RB/P：**

```bash
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
  /workspace/worker/env/chs-live/bin/python -u -m demos.10_hybrid_transformer.run_shared \
  --config-file demos/10_hybrid_transformer/config/strategy.yaml --config-id workers_hybrid
```

**终端3从08原生入口启动Vega：**

```bash
PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python -u \
  demos/08_option_vega/run_live.py \
  --config-file demos/08_option_vega/config/strategy.yaml --config-id workers_option
```

三个终端先进入同一项目目录，均默认常驻。10在本进程用两个控制线程运行RB/P，不创建策略子进程；
追加`--strategy-id hybrid-transformer-live`只运行RB，追加`--strategy-id hybrid-p-live`只运行P。
账户覆盖其已保存市场时段的并集，各策略按自己的日历预热和启停；Hybrid保留休盘前30秒停止，08使用自己的期权终点。
各终端默认通过公共账本旁的`service/current.json`发现服务；覆盖`--runtime-dir`时三个终端须一致。
正常退出先停止策略，等待保存和回报确认，再停止账户；保留已成交仓位和已使用额度。

旧`run_workers/server/worker`及原同进程布局仍用于旧部署兼容，新三终端流程使用本节入口。
原策略文件和公共账本不能由新旧入口同时占用；迁移成功后日常启动不再运行迁移命令。
验证覆盖`tests/test_account_isolation.py`、`tests/test_native_shared_entries.py`及原共享执行、跨日与布局回归；
柜台、行情和模型采用离线替身，实际服务器仍需执行上述首次禁单核验。

## 4. 自定义特征

所有特征职责统一放在[feature/](feature/README.md)：查询一次DolphinDB，同时导出原始指数与MA20；加载声明、读取和回测分钟帧也在该包内，不依赖`scripts`。底层使用现有特征注册/Provider。

`feature/generate.py`负责生成/导出，提供`generate_features`双文件接口和`generate_index_features`单独指数导出API，两者共用查询、导出校验与报告结构，双文件模式增加MA计算；`feature/__init__.py`负责`index_bundle`加载声明、`read_index_features`读取文件；`feature/backtest.py`负责`build_backtest_runner`驱动分钟帧决策。生成命令统一使用`feature.generate`。回测入口不隐式连接数据库、重复下载或覆盖数据。Live继续复用包内的声明接口。

### 从DolphinDB生成文件

在项目根目录运行，连接使用`.env`/环境中的`DDB_MARKET_*`（兼容旧`DDB_*`）：

```bash
python -u -m demos.08_option_vega.feature.generate \
  --connect --key 000852 --window 20 \
  --start '2026-09-01T00:00:00+08:00' \
  --end '2026-09-11T23:59:59+08:00' \
  --output-dir tests/results/features/index-000852-20260901-20260911
```

默认声明是本目录`feature/index.json`，读取`dfs://min_bar/index_1min`。输出`index.csv`与`index-ma20.csv`，各自带`.manifest.json`和`.dataset.json`，以及`summary.json`；成功需看到`status=passed`及两套`file_roundtrip=passed`。已有文件、数据为空或无法完成MA预热时失败。底层注册、查询、编码和摘要均复用公共API，接口和完整输出说明见[例子](feature/README.md)。

M5回测模板使用的`index_blend`由另一个DataFrame业务例子计算：

```bash
python -u -m demos.08_option_vega.feature.example_dolphindb \
  --connect \
  --config demos/08_option_vega/feature/index_close.json \
  --dataset index.close.minute --schema-version 1 --key 000852 \
  --start '2026-07-01T00:00:00+08:00' \
  --end '2026-08-24T23:59:59+08:00' \
  --output-dir demos/feature/index
```

此例生成`index-blend.csv`、同名manifest与dataset.json及summary.json，数据集为`research.index.ma20_ma60.minute`，字段为`close/ma20/ma60/index_blend`。公式为`(MA20+MA60)/2`，按实际输入行滚动，跨午休/跨日延续，前59行预热不输出，不等同于上一个连续分钟重置的单MA例子。输出目录已有文件时必须另选目录；需要起始日完整特征时，应查询更早数据作为预热。

切换Tinysoft等库表时，在来源声明中显式映射库、表、键、时间列和close列，并用`--config`选择；源代码若为SH000852，再传`--source-key SH000852`，导出键仍为000852。已提供`feature/index_tinysoft.json`，对应`dfs://JC_MKT_MIN/idx_1min_tiny`。close精简声明适用于这个DataFrame例子；`feature.generate`仍要求真实OHLCV和bar_projection，不能只改dataset名称混用入口。默认生成的组合均线具有下面的快照可见时间限制，不能因为生成命令成功就直接作为过去逐分钟可用的证据。

需要用当前历史快照进行研究回测时，显式传`--output-time-mode historical-research --assumed-delay-seconds 1`，假设完整分钟结束1秒后特征可用；来源声明仍保持observed。输出使用独立的计算版本，保留实际观测/导出时间并注明未验证历史发布/修订。完整Tinysoft生成及2026-01-05至2026-08-24回测命令见[特征包说明](feature/README.md#tinysoft历史研究回测)。该选项属于DataFrame生成示例，不改变策略公式、Live时间或公共可见性规则。

默认`observed`保留本次观测时间：能得到数据快照，不代表9月份当时已可见，不能直接用于9月份的时点回测。若数据源确认配置中的`updateTime`是该版本真实可用时刻，本例可显式加`--availability source-time`；如果只是后来补录的时间，仍不能提前使用。MA的可用时间仍是实际计算完成时间。先核对输出可见时间与实际`--frame-wait-seconds`，不要为了成交把时间改成Bar结束。

生成后将输出文件及相应声明传给`run_backtest.py --index-feature-file/--index-feature-config`，由`feature`包完成声明加载、文件校验和帧决策。引用的期权5分钟/期货行情导出不能替代一分钟指数输入，也不把M5补成M1。新计算公式属于本例业务逻辑，新增声明/映射仍使用已有底层框架。

### 修改自定义声明

1. 通过`--index-feature-config /实际路径/features.json`指定声明，`--index-dataset`与`--index-schema-version`选择数据集。
2. 08要求一分钟、EXACT对齐。默认使用真实OHLCV的Bar投影；回测可显式用`--index-value-field`选择声明中的非空数值字段，无需为MA等标量伪造OHLCV。自定义计算版本和映射写在声明中，不能只换名称而沿用错误指纹。
3. 回测使用标准导出的`--index-feature-file`；Live默认查询声明对应的DolphinDB来源。切换存储保留同一Schema、计算版本和时间语义。
4. 保留`ts_event,available_ns,received_ns,revision`。历史决策按真实可见时间封帧，不把今天观测到历史数据的时间改写成过去发布时间。

声明示例与导出方式见[特征框架](../../bomber/framework/dataprep/features/README.md)和[集成工具](../../scripts/integration/features/README.md)。自定义特征仍需满足当前策略的指数价格语义；若改变业务含义，应显式修改策略。

### 用MA20替换原始指数信号回测

这里的MA20是20根已完成的一分钟收盘价的均值，包含当前已收盘Bar。当前策略将指数信号用于`行权价 > 指数信号`的候选筛选；选择MA20会改变这个筛选基准。IV、Delta、Vega及退出计算继续使用真实期权价格和同月期货价格。MA20仍是价格量纲；收益率等无量纲特征不能直接套用这个比较。

[本目录MA例子](feature/generate.py)支持`--window 20`，生成`research.index.ma20.minute`，字段为`close/ma20/window`，同时保存CSV、`.manifest.json`与`.dataset.json`。按连续自然分钟计算，缺分钟、午休或跨日清空窗口；每段前19根预热，不输出不完整均值。不会自动向查询起点之前补取预热数据。

业务公式独立为`calculate_moving_average(frame, window)`，直接接收/返回DataFrame，默认以NumPy保留Decimal精度。`feature/dataframe.py`提供通用`dataframe_to_records`，其他业务特征算完DataFrame后也可直接转换；MA已复用它。`moving_average_records`负责MA来源、分段及依赖时间检查；可通过`calculator`传入其他Pandas/NumPy计算函数，配套修改输出Schema与计算版本。转换接口和组合均线示例见[说明](feature/README.md#业务dataframe转换接口)。

对于**已经具备历史可见时间的MA20标准文件**，在上面的完整回测命令中替换/添加以下参数：

```bash
--index-feature-file /实际路径/index-ma20.csv \
--index-feature-config /实际路径/index-ma20.csv.dataset.json \
--index-dataset research.index.ma20.minute \
--index-value-field ma20
```

同一文件改用`--index-value-field close`，可对比相同分钟、相同可见时间下的原始值与MA20；保持交易腿、日期、资金、风险及等待时间参数一致。报告`index_feature.value_field`和可用帧的`value_field`记录实际选用字段。新参数仅用于回测，Live仍沿用原投影路径。

注意`feature.generate`及DataFrame示例的默认模式是**当前计算的研究快照**，可用时间是本次计算完成时间。今天导出9月份的MA20，可以核验计算与读写，却不能直接参加9月份的时点回测；真实历史回放需要保留历史已发布MA记录，或按输入当时可见版本逐分钟计算。DataFrame示例可显式选择上面的历史研究假设，但不能将其当作真实历史发布时间。超过帧期限的MA不会进入已封闭决策；全部历史帧均无可用指数特征时，在原生回放前报错，避免长时间运行后才发现没有决策。

离线测试覆盖20条输入计算MA20、声明与标准文件读回、原策略候选/目标变化，以及预热、缺口和迟到拒绝；这些是夹具验证，不是收益表现或原生成交回测结果。

## 5. 参数参考

本节区分**未加载配置的CLI默认值**和**仓库模板值**。使用`--config-file`后，模板YAML/JSON会覆盖CLI默认；最终以报告`strategy_parameters.json`和来源身份为准。

### 5.1 配置选择和公共启动参数

| CLI参数 | YAML键/示例值 | 说明 |
| --- | --- | --- |
| `--config-file` | `config_file`，CLI通常传`config/strategy.yaml`完整相对路径 | YAML启动文件；相对路径按当前工作目录解析 |
| `--config-id` | `config_id`，如`m5_index_blend/live_recording` | 只加载一个`experiment_setups`条目；与文件参数成对提供 |
| `--strategy-config` | `strategy_config`，`vega_backtest.json`或`vega_live.json` | 策略/风险参数JSON，不接受账户授权、日期、路径等启动字段 |
| `--option-product` | `option_product: MO` | 期权品种，默认MO |
| `--future-product` | `future_product: IM` | 对应期货品种，默认IM；更换期权品种时同时匹配期货、指数和资料 |
| `--index-code` | `index_code: "000852"` | 指数特征业务键，Live须六位数字；YAML保留引号 |
| `--market-config` | `market_config` | 公共行情源JSON，Live模板为`config/market_dolphindb.json` |
| `--market-source` | 回测`file`；Live模板`dolphindb` | 回测只支持file；本策略Live支持ctp/dolphindb，虽然公共CLI列出bn-ws，国内合约会拒绝它 |
| `--index-feature-config` | `index_feature_config` | 指数数据集声明，定义Schema、字段和来源；不是策略JSON |
| `--index-dataset` | `index_dataset`，默认`index.minute` | 必须与所选声明/文件中的数据集ID一致 |
| `--index-schema-version` | `index_schema_version`，默认1 | 必须与声明版本一致；旧Tinysoft完整声明若仍为3，须显式传3 |
| `--frame-wait-seconds` | 回测模板5，Live模板20 | 未配置默认2秒，范围`[0,min(30,max_market_age_seconds)]`；按封帧期限接纳行情，超过期限不回填 |
| `--log-level` | 回测模板/Live模板`INFO` | 未配置回测WARNING、Live INFO；可选ERROR/WARNING/INFO/DEBUG |
| `--report-dir` | `report_dir` | 输出根目录；回测默认`demos/08_option_vega/results/backtest`，Live默认`.../results/live` |

`--config_file/--config_id`兼容下划线写法；业务选项CLI通常用连字符，YAML通常用下划线。`description`只作说明。优先级为`显式CLI > YAML > 策略JSON > CLI默认值`，`null`表示继承下一层；字段、类型、互斥或重复配置错误会停止启动。布尔值写YAML的`true/false`；只有声明了负向选项的开关才可用CLI关闭，例如`--no-run-forever`。不要自行拼接`--no-resume`或`--no-require-fills`，关闭它们需修改YAML对应值。

### 5.2 回测数据、周期和日历参数

| CLI参数 | 默认/模板 | 作用及约束 |
| --- | --- | --- |
| `--start-day`、`--end-day` | 必填，可由YAML提供 | `YYYY-MM-DD`；按完整交易日区间检查数据，不按现有文件缩短回测 |
| `--data-root` | 模板为部署示例路径 | 推导`kline/fut`、`kline/opt`、`role/fut_basic.feather`、`role/opt_basic.feather`；不从环境补路径 |
| `--fut-dir` | 未提供则由data_root推导 | 真实同月期货M1文件目录 |
| `--opt-dir` | M5必须显式提供 | 真正M5期权文件目录；M1忽略此值并固定读取data_root/kline/opt |
| `--fut-basic`、`--opt-basic` | 未提供则由data_root推导 | 历史合约基础资料；须覆盖行情合约及上市/到期日 |
| `--option-bar-minutes` | CLI 1，M5模板5 | 仅支持1或5；声明源文件周期，不聚合、不补齐 |
| `--bar-timestamp` | `end` | 交易腿源标签start/end；start按各自真实周期右移到完成时点 |
| `--options-bar-timestamp` | 继承bar_timestamp；M5模板end | 仅覆盖期权标签，M5起点标签右移5分钟 |
| `--option-ohlc-policy` | `reject` | `expand_range`显式扩展期权高低价并记录修整警告；原文件不变，可能影响撮合 |
| `--execution-timing` | CLI native，M5模板next_market | native保留原撮合；next_market等待本合约下一真实Bar，不承诺下一开盘价 |
| `--index-feature-file` | 必填，可由YAML提供 | 标准CSV/Parquet及同名manifest；业务日期、代码、Schema、可见时间均须匹配 |
| `--index-value-field` | CLI沿用Bar投影close；模板index_blend | 选择Schema内非空数值字段，可选close/ma20/ma60/index_blend等实际字段；仅回测支持 |
| `--index-precision` | 4 | 指数注册价格精度，范围0–12 |
| `--calendar-id` | 模板china.cffex | 使用公共NativeCalendarProvider，保留到维护末端的后续日期用于期限计算 |
| `--calendar` | 未指定 | `date/is_trading_day`日历文件，与calendar_id互斥；两者均省略才从行情日期推导 |

日期目录与文件名示例：`kline/opt/20260701/MO2609-C-6600_20260701.feather`，期货同理。扫描递归读取Feather，目录和文件日期须一致；合约代码需匹配基础资料。文件列为`datetime`或`timestamp`、`open/high/low/close/volume`，以及可核验的`symbol/trade_date`。价格需为有限正数，量非负且为整数，OHLC须满足高低价包围开收价；时间不得重复或偏离声明周期。M5文件用M1参数或反向声明均不是合法的周期转换。

### 5.3 回测资金、费用、结算和验收

| CLI参数 | CLI默认值 | 说明 |
| --- | --- | --- |
| `--starting-balance` | 40000000 | 模拟账户初始资金，独立于配置的策略nav |
| `--commission` | 1 | 固定每手费用配置；实际期货费用还取决于commission_source和基础资料 |
| `--commission-source` | auto | auto优先期货基础费用、缺失回退每手配置；metadata要求资料完整；fixed使用每手配置 |
| `--commission-multiplier` | 1 | 普通交易费用系数 |
| `--commission-product-multiplier PRODUCT=VALUE` | 空列表 | 按品种覆盖费用系数，可重复；YAML用列表 |
| `--commission-contract-multiplier CONTRACT=VALUE` | 空列表 | 按具体合约覆盖费用系数，可重复；YAML用列表 |
| `--delivery-fee-multiplier` | 1 | 公共交割费系数预留；普通成交不会因此产生交割费 |
| `--margin-init`、`--margin-maint` | 0.15、0.12 | 期货初始/维持保证金比例 |
| `--option-margin-init`、`--option-margin-maint` | 1、1 | 期权模拟保证金比例；不代表完整交易所卖方保证金模型 |
| `--option-settlements` | 未指定 | 官方最终结算文件或按到期日保存的目录；实际残仓需要终值时必须提供 |
| `--future-settlements` | 未指定 | IF/IH/IM期货官方最终交割结算文件或日行情目录；可与期权目录分别配置 |
| `--future-settlement-fee-per-contract` | 0 | 期货每手结算费用；逐行fee_per_contract优先，不重复收取普通交易费 |
| `--settlement-available-time` | 16:00 | 源结算记录未提供available_at时采用的本地可用时间 |
| `--exercise-fee-per-contract` | 0 | 默认每手现金结算费用，文件的逐合约fee_per_contract优先 |
| `--no-tearsheet` | false，模板true | 省略tearsheet输出，不关闭订单、成交或账户报告 |
| `--require-fills` | false，模板true | 本次回测至少有成交才通过验收；不会为满足条件强制开仓 |

若显式配置了`nav`，只修改starting_balance不会同步修改策略预算；比较不同资金规模时应分别核对两项。期权/期货最后交易时点、最终结算资料和回测结束日需配合，结算可用时间在区间之外时应延长回测范围，不能提前结算。

### 5.4 Live生命周期、账户和验收参数

| CLI参数 | CLI默认/模板 | 说明 |
| --- | --- | --- |
| `--connect` | false，Live模板true | 显式连接授权，缺失即停止；不等于发单授权 |
| `--mode` | recording | recording/model/simnow，需选择与配置ID、检查点一致的模式 |
| `--run-forever`、`--no-run-forever` | CLI false，Live模板true | 公共日历常驻；关闭后恢复定时模式 |
| `--seconds` | 900 | 仅定时会话的运行期限；常驻不以此到期退出 |
| `--expected-trading-day` | CLI无，模板null | 定时必填`YYYYMMDD`；常驻可选，仅核对首个窗口 |
| `--valuation-day` | 实时沿用交易日 | Replay沿用声明MD日；不能把历史行情改写到另一天 |
| `--ready-timeout` | 90秒 | 会话启动后等待健康行情/参考/执行；分钟源装配可提升到至少market_bar_max_age_seconds，常驻休市等待不是此倒计时 |
| `--state-file` | 未指定 | Model/SimNow必填；新建须不存在，恢复须原文件，Recording拒绝此参数 |
| `--resume` | false | 使用已有匹配检查点；后续常驻窗口由公共运行时按保存状态续接 |
| `--enable-orders`、`--confirm-simnow` | false，SimNow模板true | SimNow两项均必需；Recording和Model拒绝物理报单授权 |
| `--max-session-orders` | 20 | SimNow订单预算；常驻按交易日累计，包含午休前/重启前订单，耗尽后等待下一日 |
| `--query-timeout` | 30秒 | TD连接/查询超时，不是行情帧等待预算 |
| `--account-reconciliation` | SimNow strict；Model/Recording off | strict/observe/off；SimNow跨日自动恢复要求strict，不能靠改成off解决归属差异 |
| `--strategy-position-source` | Model model；其他fills | model仅随mode=model使用；真实账户持仓不能作为策略所有权推断来源 |
| `--flatten-day` | 未指定 | 指定策略主动清仓日YYYYMMDD；省略则停机保留仓位，不代表每天flatten_slot平仓 |
| `--require-fills` | false | 本次会话须收到归属成交；Recording拒绝。常驻会逐窗口验收，普通持续管理不要强制每窗口成交 |

### 5.5 Live行情、指数和条款参数

| CLI参数 | 默认/模板 | 说明 |
| --- | --- | --- |
| `--market-bar-table` | 模板stream_cffex_1min | DolphinDB共享分钟流表；不是DFS持久化表；可用DDB_OPTION_BAR_TABLE |
| `--market-bar-timestamp` | 模板end | 推送Bar的源时间标签，start/end |
| `--market-volume-mode` | 模板cumulative | per-bar为当分钟量，cumulative为累计量；按实际源口径声明 |
| `--market-bar-max-age-seconds` | 模板120秒 | 推送Bar源/接收健康阈值，范围`(60,min(120,max_market_age_seconds)]` |
| `--ctp-price-type` | last | CTP成交价聚合；mid为报价中间价Bar，零成交量，不证明可成交性 |
| `--instrument-max-age-seconds` | 30秒 | CTP逐合约行情健康/执行报价时效，须为正且不超过120秒 |
| `--index-source` | dolphindb-dfs | 默认特征Provider区间查询缓存；dolphindb为旧指数完成分钟流兼容入口 |
| `--index-database`、`--index-table` | 沿用特征声明 | 显式覆盖DFS来源，例如dfs://min_bar、index_1min |
| `--index-availability` | 沿用声明 | observed/source-time/update-time；update-time映射到source-time，只在源发布时间语义经确认后使用 |
| `--index-poll-seconds` | CLI 0.2，模板1秒 | DFS轮询间隔，范围`(0,30]`；不是每分钟仅查一次 |
| `--index-query-timeout` | 2秒 | 指数只读查询超时，范围1–30秒 |
| `--index-price-precision`、`--index-size-precision` | 4、4 | 指数精度配置/来源身份字段，范围0–9 |
| `--index-bar-table` | DDB_INDEX_BAR_TABLE或显式参数 | 旧指数推送流入口使用的共享表；默认DFS路径不使用此项 |
| `--index-bar-timestamp` | DFS沿用声明，旧流默认end | 指数Bar源标签；不等于发布/可见时间 |
| `--reference-source` | dolphindb | 合约条款来源；file仅用于显式测试 |
| `--reference-database` | 环境参考库配置 | 期权/期货基础资料库；与行情库、时段库分别配置 |
| `--reference-timeout` | 30秒 | 参考来源读取超时 |
| `--reference-refresh-seconds` | 5秒 | 条款观测刷新间隔，可为0 |
| `--reference-max-observation-seconds` | 30秒 | 条款最近观测有效时长，不等同于条款发布日期 |
| `--allow-file-reference-test` | false | 选择file来源还需显式开启，并同时提供opt-basic与fut-basic |
| `--opt-basic`、`--fut-basic` | 未指定 | Live文件测试条款路径；正式模板使用DolphinDB |

Live仍使用M1交易腿和可投影的原始指数声明；`--option-bar-minutes`与`--index-value-field`是回测参数，不能直接传给Live。

### 5.6 Live常驻会话日历参数

| CLI参数 | 默认/模板 | 说明 |
| --- | --- | --- |
| `--calendar-id` | china.cffex | 公共原生日期日历，与期权时段规则共同生成交易窗口 |
| `--hours-code` | 未指定时为option_product，模板MO | 使用期权自身MO/IO/HO规则，不能用IM/IF/IH时段代替 |
| `--session-timezone` | Asia/Shanghai | 会话时区 |
| `--trading-hours-database` | 模板dfs://bomber_static | 时段库；入口可回退DDB_TRADING_HOURS_DATABASE或默认静态库 |
| `--trading-hours-table` | 模板fut_trading_session | 时段表；表名虽含fut，规则键仍为期权品种 |
| `--trading-hours-file` | 未指定 | 文件时段来源测试入口；与数据库来源互斥 |
| `--session-overrides-table` | 未指定/环境例外表 | 按日期覆盖、替换或取消时段的规则表 |
| `--session-overrides-file` | 未指定 | 配合文件时段来源使用的例外文件 |
| `--no-session-overrides` | false，模板true | 显式声明没有日期例外；与例外来源互斥，有例外应修改YAML而不是忽略 |
| `--trading-calendar` | 不支持 | 虽由公共CLI注册，08 Live拒绝旧日历JSON入口 |

窗口以实际参考资料为准，不在策略中硬编码收盘时间；目前只支持期权当日日盘窗口。日历超出维护范围、规则缺失或不匹配时明确失败，不能用工作日猜测休市。

### 5.7 策略JSON参数对照

配置来源：[vega_backtest.json](config/vega_backtest.json)、[vega_live.json](config/vega_live.json)。JSON键与CLI对应关系为下划线换成连字符，例如`vega_budget`对应`--vega-budget`。`delta_targets/hedge_slots`在JSON中使用数组，在CLI中使用逗号字符串。

| JSON键 / CLI参数 | 无配置CLI默认（回测 / Live） | 回测JSON | Live JSON | 策略含义 |
| --- | --- | --- | --- | --- |
| `nav` / `--nav` | 未指定时用starting_balance / 40000000 | 40000000 | 40000000 | 固定配置本金，决定每日新增Vega预算，不自动按账户净值更新 |
| `vega_budget` / `--vega-budget` | 1.25 / 1.25 | 1.25 | 1.25 | `B=nav×vega_budget/100`，单位波动率现金敏感度尺度 |
| `delta_targets` / `--delta-targets` | `[0.2,0.25,0.3,0.35]` | 同左 | 同左 | 各档选择最近的多头认购Delta，允许重复选中同一合约 |
| `option_slot` / `--option-slot` | 13:58 / 13:58 | 14:00 | 14:00 | 每日选约、增仓及常规退出检查时点；M5须对齐完成时刻 |
| `hedge_slots` / `--hedge-slots` | `[10:00,11:00,13:30,14:00,14:55]` | 同左 | 同左 | 已成交期权的期货Delta对冲时点 |
| `flatten_slot` / `--flatten-slot` | 14:50 / 14:50 | 14:50 | 14:50 | 仅在final_day/flatten_day达到时生效；不是每日固定平仓 |
| `min_remaining_days` / `--min-remaining-days` | 5 / 5 | 5 | 5 | 新选合约的最小剩余交易日阈值 |
| `close_remaining_days` / `--close-remaining-days` | 2 / 2 | 2 | 2 | 旧检查点兼容字段，当前不用于到期前强制退出 |
| `rate` / `--rate` | 0.02 / 0.02 | 0.02 | 0.02 | 定价年化利率，小数口径 |
| `min_time_value` / `--min-time-value` | 5 / 5 | 5 | 5 | 新IV尝试要求`C-max(F-K,0)`不小于此值；不是退出阈值 |
| `max_market_age_seconds` / `--max-market-age-seconds` | 300 / 300 | 300 | 300 | 策略报价缓存相对估值时点的最大年龄，另受通道健康检查约束 |
| `max_iv_age_seconds` / `--max-iv-age-seconds` | 300 / 300 | 300 | 300 | 可回退使用的IV缓存期限，不放宽行情价格时效 |
| `max_option_lots` / `--max-option-lots` | 100 / 1 | 100 | 1 | 单张期权空头目标绝对手数上限，也用于新增配仓截限 |
| `max_total_option_lots` / `--max-total-option-lots` | 200 / 2 | 200 | 2 | 累计期权目标绝对手数总和上限 |
| `max_future_lots` / `--max-future-lots` | 100 / 1 | 100 | 1 | 每张期货目标绝对手数上限；所需对冲超过时停止，不静默截限 |
| `max_notional` / `--max-notional` | 100000000 / 2000000 | 100000000 | 2000000 | 转成公共逐合约委托/仓位名义金额限额，不是现金Vega或保证金预算 |
| `limit_offset_ticks` / `--limit-offset-ticks` | 回测无此CLI / 1 | 未配置 | 1 | Live CTP限价偏移跳数，非负整数；不是模拟滑点参数 |

`nav`、手数和金额须为正；Delta档须在`(0,1)`；利率有限、最小时间价值非负；时点为HH:MM且option_slot早于flatten_slot；兼容字段仍要求`0 <= close_remaining_days < min_remaining_days`。改单个配置前同时核对累计手数和所需对冲手数。执行限额还会约束每笔最大数量，当前转换为对应单腿手数上限的2倍，以容纳方向切换的差额。

### 5.8 Replay工程测试参数

`--simnow-environment`默认realtime；replay须声明`--replay-md-trading-day YYYYMMDD`，并使用CTP行情。可选`--replay-test-start-time HH:MM`和`--replay-test-source-time HH:MM`用于显式工程时间映射，要求CTP Replay、DFS指数及新测试会话；source-time须同时声明start-time。常驻模式只支持realtime，不重映射历史日期；Replay不是通过改日期发送已下架合约的授权。

## 6. 验证与诊断

### 回测性能统计与默认日志

`m5_index_blend`示例使用`log_level: WARNING`，阶段耗时、交易日进度和性能统计仍直接输出。
启动方式不变；临时排查引擎细节时可以追加`--log-level INFO`。

每个回测结果目录新增`performance.json`，在运行器清理后写入最终统计；
`summary.json`的`performance_report`指向此文件。输入失败报告也包含已收集的性能统计。
所有时间都是本机墙钟耗时，不是模拟交易时间。嵌套计时包含子项，不能直接相加：

| 统计项 | 范围 |
| --- | --- |
| `phase:*`、`run_case` | 各入口阶段和本次运行总耗时 |
| `index_feature_file_read` | 特征文件读取、manifest及可见时间校验 |
| `feather_disk_read` | 实际Feather读取；另计文件数、行数和缓存命中 |
| `bar_read_normalize_validate` | 交易腿读取、标准化和校验，包含Feather读取及副本创建 |
| `replay_event_conversion` | 各回放来源的逐行转换、解析和订阅过滤，不含排序 |
| `replay_event_sort` | 按可用时间稳定排序并创建事件元组 |
| `feature_alignment` | 将指数特征按可见时间对齐分钟帧 |
| `market_binding_lookup` | 行情到模拟引擎的绑定匹配 |
| `simulation_engine_including_decisions` | 每根行情推进引擎；包含原生定时器内触发的策略决策 |
| `simulation_backend_prepare` | 生命周期/时间顺序检查、首次启动、订单行情门控 |
| `simulation_add_data` | 单根真实行情注册到内核 |
| `simulation_run_including_decisions` | 内核run推进，包含定时器内的策略决策 |
| `simulation_clear_data` | 内核已消费数据清理 |
| `strategy_decision` | 策略分钟决策回调；它是引擎推进耗时的子项 |
| `replay_dispatch_including_engine` | 回放分发总耗时，包含引擎及策略决策 |

`counters`记录期权/期货文件数、合约数、交易日、事件数、READY/SKIPPED帧数及订单/成交数。
Feather读取数包含基础资料等输入文件；`option_bar_files`和`future_bar_files`单独统计交易腿。
这些计数用于比较相同输入、相同参数的运行，不能用某个局部优化的耗时代表整段回测。

回放适配器按合约、数据类型索引绑定，不再为每根Bar扫描整个合约集合。
准备后的DataFrame按行迭代输出，避免一次创建全部行字典；08内部只读链路借用已校验的
DataFrame，省去准备结果和回放注册时的两次深拷贝。公共读取器默认仍返回独立副本，
其他调用方不受内部借用方式影响。逐根推进引擎、同刻事件顺序、接收期限、策略公式、
`next_market`成交时序和结算规则均沿用原逻辑。

### XY引擎推进优化验证

2026-11-10的第一轮优化继续逐根推进，不合并批次：公共Backend只在首次行情时启动，
每根行情仍执行生命周期/单调时钟检查和`next_market`门控。四项`simulation_*`细分计时
用于定位add/run/clear的开销，`simulation_run_including_decisions`包含策略决策，不能重复相加。

原生内核另有独立源码补丁：`add_data`用单个合约缓存索引校验身份，L1模式在深度数据
校验前跳过全合约遍历；L2/L3仍每批核验，不缓存校验结果。该部分必须重建并安装
XY对应Python/平台的Bomber内核wheel；只同步本目录和framework只能获得Python侧调整及细分计时。
新内核公开`STREAMING_VALIDATION_REVISION="l1-indexed-validation-v1"`作为部署证据。
启动日志、`summary.json`及最终`performance.json`的`engine_identity`记录实际扩展路径和此标记；
标记为null时，不把本轮运行当作原生优化版。

完整补丁、XY/DX验证步骤及本地测试限制见
[引擎推进优化交接](../../doc/HANDOFF/demos/HANDOFF_SIM_STREAMING_ENGINE_2026-11-10.md)。

### 行情加载校验优化

公共`dataprep/bars.py`在保留输入契约的前提下，批量转换时间和检查数值：

- 日期时间数组显式统一到纳秒，再读取整数值；不直接把微秒/秒存储的整数当成纳秒。
- 常见数值类型批量检查有限值，价格与数量仍保留原列类型；正数和整数成交量政策不变。
- 同类型NumPy OHLC列使用数组比较，混合/可空类型沿用原Pandas规则。
  `expand_range`仍先修整high，再基于已修整的high计算low，并保留原价和审计警告。
- 仅当可用时间和事件时间都已经递增时跳过排序；其他输入仍稳定排序，
  排序后事件时间倒退仍拒绝。非默认索引仍统一重置。

这轮只更新独立维护的Python framework，不需要重编译Bomber内核。
XY上一轮已安装新内核的基线为`run_case=789.960931s`、
`bar_read_normalize_validate=323.712959s`，订单1048、成交1060。
测试时保持该内核和相同输入配置，单独评估本轮加载变化。

本地真实合成Feather、独立输入会话、交替执行新旧代码并取三轮中位数：
300个48行M5文件由1.383s降到1.066s，100个240行M1文件由0.515s降到0.400s。
这是本地小文件准备基准，不代表XY完整回测的提速比例。
同步及验证见[行情准备优化交接](../../doc/HANDOFF/demos/HANDOFF_BAR_PREPARATION_VECTORIZE_2026-11-10.md)。

第二轮进一步减少每个小文件的Pandas列/Series创建：批量组装时间列，直接复用已标准化
的可用时间；普通有限数值列使用原类型数组检查，不重复转换或创建Series掩码。
混合、可空、非有限及特殊数值保持原兼容路径，列顺序、元数据和错误报告不变。
本地与上一版交替执行的小文件基准下降约33%，XY完整收益待验证。
本轮XY基线为加载`252.148737s`、总计`719.843958s`。
见[第二轮更新和Bar转换说明](../../doc/HANDOFF/demos/HANDOFF_BAR_PREPARATION_BULK_2026-11-10.md)。

Bar对象转换是加载校验之后的独立阶段：`feature/backtest.py:run_replay`调用
`FileReplayFeed.load_events`，由`PreparedFrameReader`从已准备的DataFrame逐行输出，
`FixedInstrumentBarParser`补合约身份、`MappedBarParser.parse`解析时间和数值，
经`make_bar`生成原生`Bar/Price/Quantity/BarType`，再按订阅收集事件。
`replay_event_conversion`包含此过程（最近XY约111.875s），事件排序另计约1.268s；
该阶段还未推进模拟时间，也不重新读取Feather或执行策略决策。

### 长区间回测进度与特征缓存

`Registered Strategy`后会先转换、排序全部文件Bar，再预处理特征帧，之后才推进模拟撮合。
回测入口输出`[回测进度]`区分这三个阶段：行情事件数、特征帧READY/SKIPPED数量及各阶段耗时；
模拟回放期间在交易日变化或每30秒输出模拟分钟、已封闭帧/总帧、策略帧、选约、提交及成交计数。
这些诊断不受引擎INFO日志过滤影响，也不会改变模拟时钟或额外触发决策。

公共FeatureStore采用增量原子发布及业务槽索引；EXACT只截取目标分钟的全部修订，WINDOW截取窗口，
ASOF查找最近可见槽。发布时间、接收时间、修订顺序、健康、质量及超时规则不变，缺失不前填。
这避免逐条发布复制全部缓存、每次EXACT查询扫描全部历史所造成的平方级开销。
升级须同时更新框架`datahub/features/store.py`、`alignment.py`及本策略`feature/backtest.py`。
框架从site-packages导入的环境需要更新实际安装包，只同步demos不会获得缓存优化。

同机离线特征帧样本：4000帧从约4.53秒降为0.066秒；优化后50000帧约1.26秒。
样本只衡量特征发布/对齐，不代表期权全链加载、原生撮合或完整回测耗时。
若仍长时间无日期进度，可在另一终端用`ps -eo pid,etime,pcpu,pmem,rss,args | grep '[r]un_backtest.py'`
及`free -h`查看资源；CPU占用不能单独证明模拟日期在前进。

```bash
python tests/run_option_vega_offline.py
python -m unittest tests.test_option_vega_native -v
python -m scripts.integration.options.check_backtest_data --help
python -m scripts.integration.options.recording --help
```

离线入口采用SDK/柜台夹具，原生测试须在安装真实Bomber扩展的独立进程运行。日志字段见[历史字段说明](../../doc/strategies/OPTION_VEGA_MINUTE_LOG_FIELDS.md)，历次报告与交付记录移至[历史文档](../../doc/strategies/OPTION_VEGA_HISTORY.md)，不作为当前启动指南。

### 当前分钟日志怎样判断

| 字段/状态 | 当前含义 |
| --- | --- |
| `分钟行情汇总.status=COMPLETE`、`missing_required=[]` | 本帧必需腿齐全；不表示全部候选都有行情或已经成交 |
| `same_minute=true` | 对应必需腿属于本次估值时点；receive_delay_seconds另外描述接收延迟 |
| `positions` | 本策略已成交净仓，负数为空头；以此判断当前自有持仓 |
| `option_goals/future_goals` | 当前业务目标，需与实际仓位及working一起看 |
| `WAIT_SLOT/ALREADY_SELECTED` | 未到当天选约时点/该日已处理，不表示停止持仓管理 |
| `NO_VALID_ENTRY/ZERO_ALLOCATION/RISK_LIMIT` | 没有有效候选/配仓为零/新增累加超限；仍可能退出旧仓或调整对冲 |
| `RESUME_WAIT` | 新场次依赖行情未齐，本帧提前返回 |
| `hedge=NOT_DUE/EVALUATED/WAIT_WORKING_ORDERS` | 未到时点/已评估/有期货在途，不与entry混为一个维度 |
| `NO_TARGET_CHANGE` | 本帧没有提交新目标，不代表没有持仓 |
| `TARGET_SUBMITTED`、`target_submissions_total` | 已交完整目标及其次数，不是订单或成交数 |
| `fills_received_total`、`order_updates_received_total` | 本会话收到的归属成交/订单状态回报数量，零值不否定恢复的旧仓 |
| `此前缺失`后`指数分钟查询投影完成` | 同一个待查分钟先未查到、后已查到，不表示前一分钟必定缺失；是否入帧仍看接收期限 |
| `HEALTH_ONLY_LATE` | 迟到新Bar只更新健康记录，不改已封闭帧、策略价格或历史决策 |

### 输入和启动错误的定位顺序

| 错误 | 检查对象 | 处理方式 |
| --- | --- | --- |
| `DolphinDB只读连接失败` | 实际Python、SDK、host/port/用户名/SSL、用途环境变量 | 尚未查询表；先核对连接，不能靠改字段映射解决 |
| `清单未声明该数据集/版本` | 配置schema与dataset/schema-version | 保持请求和声明一致，例如close精简声明不是旧index.minute版本3 |
| `manifest...校验失败` | 特征文件SHA256、Schema和时间规则 | 同步原始文件与manifest，保留LF/CRLF字节；不直接改摘要掩盖内容差异 |
| `MISSING_OPTION_CHAIN` | 文件目录、命名、历史基础资料与日期覆盖 | matched_option_files是与条款匹配后的数量，不等于磁盘文件总数；看input_issues.json |
| `MISSING_INDEX_FEATURE`或没有请求日期/代码记录 | 特征日期、键、版本、可见时间 | 文件中存在历史日期也不证明当时可用；按完整请求核对 |
| `INVALID_OHLC` | 错误文件的open/high/low/close原值 | 高价须包围开/低/收、低价须包围开/高/收；核对导出与精度，不用补值掩盖 |
| `option-slot...没有三腿同步行情` | 当日指定时点的期权、同月期货、指数 | 核对周期/标签/缺口；M5显式选择14:00等完成时点，不使用未来Bar |
| `必需行情失效` | 持仓及其同月期货/指数的源时点、接收时点、健康阈值 | 区分缺帧、迟到和真实断流；不通过清空原仓或关闭校验绕过 |
| 恢复身份、持仓或账户核验失败 | 原检查点、策略ID、条款、来源、真实归属与活动单 | 保留原文件，核对差异和权威回报，不手改JSON伪造恢复 |

准备失败明细在`<report-dir>/input_failed_*/input_issues.json`；运行后看本次summary中的failure及cleanup字段。`status=passed`表示该执行模式的验收通过，Recording通过不等于取得真实SimNow成交。


## 策略空仓后的维护

期权与Hybrid共用公共CTP空仓维护流程。期权入口为`python -m demos.08_option_vega.reconcile_flat`，
支持独立账户`--state-file`或共享账户`--config`及`--strategy-id`，并要求`--confirm-manual-flat`。
普通对齐要求同日且本地订单已终结；后续交易日旧未决平仓单必须显式追加`--archive-unknown-close`。
维护同时核验期权和期货对冲腿，保留选约日期、已处理对冲时点及其他策略/外部仓位，
不调整配置、不补造成交。成功后单独按原Live命令恢复。
完整流程、参数与备份说明见[公共CTP空仓维护](../../doc/functions/trader/CtpFlatMaintenance.md)。
