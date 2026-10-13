# 策略交接：demos 当前实现（2026-10-04）

本文根据当前项目的 demos 代码及 README 核对，供新对话接手。它描述实现和已知边界，不代表所有策略已在真实数据上完成回测验收。旧 HANDOFF 文档可作历史参考；当前接口和目录以本文件及实际代码为准。

## 1. 项目位置与协作要求

- 本机项目根目录：`/Users/kerry/work/bomber1/pro`。
- 策略实现目录：`/Users/kerry/work/bomber1/pro/demos`，新策略独立新建文件夹。
- 用户运行环境曾为 Linux：`/workspace/worker/pj/Chrono/nautilus_trader_demo/pro`，也出现过 `/home/dev/workspace`；实际路径由运行环境确定，不把这些路径写死进策略。
- 不要误改同名 examples 目录；examples 是研究/参考实现，demos 是当前框架下的演示入口。忽略 `__bak`、`___bak` 等备份。
- 不把实现写到 `/private/tmp`；交接文档、代码、结果应保存在项目中。文件修改后说明具体改了哪些文件。
- 不在 demos 中只通过 `from examples... import ...` 复用整套策略；现有策略逻辑独立保存在对应目录。
- 品种、日期、数量/金额、窗口、数据路径等应可配置，不固化 RB。
- DataHub 主要负责角色、研究数据与角色换月信息；不能因期权回测需要而将它重构成通用行情文件读取器。行情读取优先放策略的 local_input/contract_input 等适配层。

## 2. 框架约定

交易策略走：

```text
文件行情 → market.FileReplayFeed → DataBinding
→ trader.StrategyTemplate.on_bar / on_custom_bar
→ set_target / set_targets
→ UnifiedStrategyRunner → 模拟执行客户端/后端
```

具体导入位置见各 run_backtest.py。当前框架目录是 `trader`；历史对话中出现过 `strategy`，不要直接照旧 traceback 写导入。

- `set_target(target_key, target, timestamp)` 提交的是目标持仓，正数多、负数空、0 平仓；不是直接指定一张订单的方向与数量。
- 执行器依据目标与当前有效持仓的差额生成委托，反手可能包含平仓、开仓。
- 同步多腿行情后才计算信号；研究复权价与真实成交价分开。
- 多腿同批目标不保证同时成交，更不代表交易所层面的原子套利。
- 多数交易 demo 用基础净持仓、Bar 撮合、固定每手手续费、简化保证金；未等价实现 CTP 平今/平昨和逐日结算。
- option_delta 只有选约信号层，使用相同 Runner/Feed/Template，但没有执行路由、委托与绩效回测。

## 3. 数据目录与配置

用户展示过的数据布局：

```text
/workspace/data/temp_bomber1/ctp/
  role/
    fut_basic.feather
    opt_basic.feather
    fut_contract.feather
    fut_adjustment_factors.feather
  kline/
    fut/YYYYMMDD/真实期货合约_YYYYMMDD.feather
    opt/YYYYMMDD/MO2610-C-7000_YYYYMMDD.feather
    index/YYYYMMDD/000852_YYYYMMDD.feather
  tick/
```

文件名示例不是对所有日期真实存在的保证。此前本机没有实际行情，完整性信息来自用户；不得声称已扫描并确认真实数据齐全。

### 环境变量

在采用上述 fut/opt/index 子目录的环境，建议明确设置：

```bash
export CTP_DATA_DIR=/workspace/data/temp_bomber1/ctp
export FUT_ROLE_DATA_DIR="$CTP_DATA_DIR/role"
export FUT_KLINE_DATA_DIR="$CTP_DATA_DIR/kline/fut"
export OPT_KLINE_DATA_DIR="$CTP_DATA_DIR/kline/opt"
export INDEX_KLINE_DATA_DIR="$CTP_DATA_DIR/kline/index"
export ROLE_DIR="$FUT_ROLE_DATA_DIR"
export KLINE_DIR="$FUT_KLINE_DATA_DIR"
export PYTHONPATH=.
```

**不能把 FUT_ROLE_DATA_DIR 当成行情目录。** 期货 demo 的 FUT_KLINE_DATA_DIR 最稳妥地直接指向 `kline/fut`；两个期权 demo 支持其指向含 fut 子目录的父目录。不要假定所有入口具有完全相同的目录解析逻辑。

- main_ema 当前仍强制要求 ROLE_DIR、KLINE_DIR，即使给了文件覆盖参数；它尚未与其他期货 demo 完全统一环境变量接口。
- cross_section、black_sector、role_cross、calendar_spread、triangle_spread、scheduled_targets 优先用 FUT_ROLE_DATA_DIR/FUT_KLINE_DATA_DIR，兼容 ROLE_DIR/KLINE_DIR。
- option_vega、option_delta 支持 CTP_DATA_DIR、各类独立行情变量、FUT_ROLE_DATA_DIR/ROLE_DIR；还支持行情父目录 KLINE_DATA_DIR/KLINE_DIR。
- `--bars-dir`、`--contract-struct`、`--fut-basic` 等覆盖参数以各入口为准。

**角色表文件名差异：** 多个角色类期货 demo 默认读取 `fut_contract_data.feather`；用户展示的是 `fut_contract.feather`。启动角色类策略时应显式添加：

```bash
--contract-struct /workspace/data/temp_bomber1/ctp/role/fut_contract.feather
```

这只解决路径差异，仍需确认该文件的列结构满足角色适配器。scheduled_targets 不需要角色表；两个期权 demo 不依赖主力角色表。

### 字段与时间

- 期货 tick、乘数、交易所取自 fut_basic，尤其 `minChgPriceNum`、`contMultNum`，不是统一写死价格步长。
- opt_basic 已有字段：symbol、exchangeCD、currencyCD、contractType、strikePrice、contMultNum、varTicker、listDate、lastTradeDate、expDate、exerDate。两个期权 demo 不要求增加期货式品种 code，支持 symbol/Code 识别。
- opt_basic 没有 tick 时用 `--option-tick` 或 OPT_PRICE_INCREMENT；0.2 为之前的配置示例，仍应核实合约规格。
- 期权/指数导出文件保留北京时间；trade_date 只存日期，可不存 timestamp。
- volume/turnover 如来源是交易日累计值，导出阶段按合约、交易日排序差分；持仓量是时点值，不同于成交量，不做这种差分。
- 分钟标签代表开始还是结束必须核实。两个期权 demo 要显式传 `--bar-timestamp start|end`，start 会在内存中加一分钟，不能提前使用完整分钟行情。
- 现有期货旧格式包括 vt_symbol、symbol、exchange、OHLC、date、time、datetime、volume、value、open_interest、vwap。
- `examples/opts2/kline_task.py` 是现有导出工具，含期货/期权/指数导出逻辑和可选盘口字段。不要假定已有导出文件必然带盘口；如继续修改，先读当前代码，它的参数/期货基本资料要求与早期对话曾有变化。

## 4. 策略总览

| 目录 | 核心逻辑 | 交易对象/输出 |
|---|---|---|
| main_ema | 主力因果复权价的快慢 EMA | 单品种真实主力目标 |
| cross_section | 多品种主力收益排名，强多弱空 | 两个品种主力，其余归零 |
| black_sector | 两个领头品种收益平滑后与比较品种对照 | 指定执行品种真实主力 |
| role_cross | 主力相对主力/次主力/远期均价的零轴穿越 | 单品种真实主力 |
| calendar_spread | 同品种两个角色合约价差 Z-score 回归 | 两个不同真实月份 |
| triangle_spread | 三品种归一化对数相对价差回归 | 主腿与两个对冲腿 |
| scheduled_targets | 按 CSV 独立时钟执行完整目标快照 | 任意已配置期货合约组合 |
| option_vega | 卖虚值认购、现金 Vega 配仓、期货对冲 | 期权及同月股指期货 |
| option_delta | 同月 Call/Put 收盘价反解绝对 Delta，持仓量/分钟成交量筛选 | 仅研究选约报告，不下单 |

以下命令均在 **pro 根目录**执行，先设置上述环境变量。日期只是使用示例，实际可用区间必须以数据为准。角色类策略命令均显式给角色表，以适应用户目前的文件名。

### 4.1 main_ema：主力 EMA

文件：`demos/main_ema/strategy.py`、`run_backtest.py`、`README.md`。

每天使用上一交易日角色资料确定主力；真实主力收盘价乘因果复权因子计算 EMA。快线高于慢线目标做多，低于目标做空，目标手数由 quantity 决定。预热、信号细节见 strategy.py。动态路由管理换月，旧合约持仓先处理，新合约有行情后执行；成交使用未复权真实 Bar。

```bash
python demos/main_ema/run_backtest.py \
  --product RB --start-day 2026-01-05 --end-day 2026-05-29 \
  --fast 3 --slow 5 --quantity 1 --log-level WARNING \
  --contract-struct "$FUT_ROLE_DATA_DIR/fut_contract.feather"
```

数据：角色表、fut_basic、真实合约分钟 Bar。单品种信号因子从历史真实收盘价推导，不直接读取外部累计复权表。换月日旧合约仍需 Bar。当前入口环境变量是 ROLE_DIR/KLINE_DIR。

### 4.2 cross_section：主力截面动量

文件：`cross_strategy.py`、`local_input.py`、`run_backtest.py`。

品种传 `--products`，不是合约代码。所有品种同一分钟主力 Bar 到齐后，用 lookback 根收益率排序，多最强、空最弱，其余零目标。每 rebalance-interval 根同步 Bar 更新；换月旧合约目标清零。相同分数按代码确定顺序。

```bash
python demos/cross_section/run_backtest.py \
  --products RB,HC,I,JM,NI \
  --start-day 2026-01-05 --end-day 2026-05-29 \
  --lookback 20 --rebalance-interval 5 --target-notional 100000 \
  --contract-struct "$FUT_ROLE_DATA_DIR/fut_contract.feather"
```

lookback 是 Bar 数，不是天数；target-notional 是各被选腿的目标名义金额。当前代码不足一手时仍至少给一手，因此金额是近似值，小金额可能被超过。依赖各品种主力与换月旧合约数据。

### 4.3 black_sector：行业领先收益对照

文件：`strategy.py`、`sector_signal.py`、`local_input.py`、`run_backtest.py`。

默认 JM/I 为领头品种、RB 为比较与执行品种。信号默认使用三个品种的次主力同步复权价，订单默认执行 RB 主力。

每根同步 Bar 计算收益率：两个领头收益分别取最近 return-period 根平均，再取两者均值，再对行业序列取 sector-period 根平均。比较品种当前收益高于行业值则多，低于则空，相等保持方向。它不是三腿套利，也不同时交易两个领头品种。

```bash
python demos/black_sector/run_backtest.py \
  --start-day 2026-01-05 --end-day 2026-05-29 \
  --leader-products JM,I --comparison-product RB --execution-product RB \
  --quantity 1 --return-period 30 --sector-period 15 \
  --log-level WARNING \
  --contract-struct "$FUT_ROLE_DATA_DIR/fut_contract.feather"
```

可配置 signal-role、execution-role、submission-delay-bars。默认 secondary/main。窗口单位是完整同步 Bar 数。需检查信号三腿、执行腿与旧执行合约行情。

### 4.4 role_cross：三角色价格穿越

文件：`strategy.py`、`role_signal.py`、`local_input.py`、`run_backtest.py`。

只使用 main/second/far（主力、次主力、远期），不使用 recent。每角色各自做因果复权，完整同步后计算：

```text
spread = main - (main + secondary + far) / 3
```

从负到正穿越则主力多 quantity 手，从正到负则空；未穿越保持目标，首帧预热。它只交易主力，不是持有三个角色的套利组合。

```bash
python demos/role_cross/run_backtest.py \
  --product RB --start-day 2026-01-05 --end-day 2026-05-29 \
  --quantity 1 --log-level WARNING \
  --contract-struct "$FUT_ROLE_DATA_DIR/fut_contract.feather"
```

原来近期合约到期后缺 Bar 的问题通过移除 recent 规避；旧主力有持仓时仍不能忽略换月所需行情。

### 4.5 calendar_spread：单品种跨期均值回归

文件：`strategy.py`、`selection.py`、`contract_input.py`、`run_backtest.py`。当前正式目录是 calendar_spread，不是旧名 rb_calendar_spread。

默认次主力与远期；第一腿 minus 第二腿未复权价差与之前 lookback 根完整价差比较。低于 −entry-z：多第一腿空第二腿；高于 +entry-z：反向；|z|≤exit-z：两腿平仓。阈值之间保持目标，标准差为零时 z=0。

```bash
python demos/calendar_spread/run_backtest.py \
  --product RB --start-day 2026-01-05 --end-day 2026-05-29 \
  --near-role secondary --far-role far \
  --quantity 1 --lookback 120 --entry-z 2 --exit-z 0.5 \
  --log-level WARNING \
  --contract-struct "$FUT_ROLE_DATA_DIR/fut_contract.feather"
```

不接受 recent/near 作为近期角色；near-role 参数名保留，但当前取值是 main 或 secondary。两角色合约必须不同。任一腿换约先处理旧组合持仓/在途订单，再切换并清空窗口。

missing-role-policy 默认为 raise。next-available 可按当日文件可用性替换角色并记录，存在当日可用性前视风险；严格研究用 raise。旧合约缺行情且仍有仓位/在途订单时必须报错。

### 4.6 triangle_spread：三腿相对价值

文件：`strategy.py`、`spread_signal.py`、`run_backtest.py`。

默认方向结构多 RB、空 HC/I，或反向；品种及权重可配置。三品种主力同步复权价以首帧归一化：

```text
spread = log(anchor/base_anchor)
         - w1*log(hedge1/base_hedge1)
         - w2*log(hedge2/base_hedge2)
```

当前价差与前 lookback 根均值/标准差比较，低 Z 多主腿空对冲，高 Z 反向，回归 exit-z 内平仓。主腿金额 target-notional，对冲侧总金额相同并按权重分配。

```bash
python demos/triangle_spread/run_backtest.py \
  --anchor RB --hedges HC,I --hedge-weights 0.5,0.5 \
  --start-day 2026-01-05 --end-day 2026-05-29 \
  --lookback 120 --entry-z 2 --exit-z 0.5 \
  --target-notional 300000 --rebalance-interval 5 --log-level WARNING \
  --contract-struct "$FUT_ROLE_DATA_DIR/fut_contract.feather"
```

示例总名义金额约 60 万，不是 30 万。手数向下取整；任一腿不足一手报错。两权重须正且合计 1。不保证因子中性或价格关系稳定，也不是无风险三角套利。

### 4.7 scheduled_targets：外部持仓计划

文件：`strategy.py`、`local_input.py`、`contract_input.py`、`run_backtest.py`、`targets.csv`。

CSV 列：timestamp,target_key,target_qty。同一 timestamp 多行是完整目标组合；正数多、负数空、0 平。使用 REPLACE，下一时点未列出的旧目标也归零。独立时钟触发，不靠 on_bar 产生信号；同时间戳先处理 Bar 再提交目标，新委托等待后续行情成交。

```bash
python demos/scheduled_targets/run_backtest.py \
  --targets demos/scheduled_targets/targets.csv \
  --start-day 2026-05-06 --end-day 2026-05-29 --log-level WARNING
```

已有示例 100 条；README 描述其覆盖 2026-05-06..19、10 个工作日，每天 5 开5平。它是演示计划，非策略优化成果，运行前核对合约覆盖。只支持当前已适配的期货目标，不自动支持指数/期权执行。需要 fut_basic、计划合约行情，不需要角色表。

相对路径支持项目根目录解析；当前目录为 demos 时应避免再拼成 demos/demos。审计包括 MISSED、NOT_REACHED；默认要求时点全部触发，allow-missed-slots 可放宽审计验收，但不会补造成交。

### 4.8 option_vega：认购卖方 Vega 配仓与 Delta 对冲

来源：examples/opts2/strategy1.py，独立框架实现。文件：`strategy.py`、`model.py`、`option_math.py`、`local_input.py`、`instruments.py`、`run_backtest.py`。

- 默认 MO/IM/000852，13:58 在最近有效到期月份中选虚值认购。
- 至少剩余 5 个交易日，同月期货需基本资料、上市状态和行情有效。缺资料/未上市的同月期货排除对应候选，记录原因；已上市的必需行情缺失仍报错。
- 同月期货作为 Black76 forward，反解 Greeks；Delta 目标默认 0.2/0.25/0.3/0.35，同合约多档合并。
- 按现金 Gamma 倒数分权重，再以现金 Vega 预算归一化并限制手数。NAV 4000万、vega-budget 1.25 时，每 1% IV 变化的现金 Vega 预算约 5000元，不是卖 1.25 手。
- 卖出认购目标为负，每日完整替换，不累加。基于实际成交期权仓位计算各同月期货 Delta 对冲，定时及仓位变化复核。
- 剩余交易日≤2或时间价值≤0时平期权；最后回测日默认14:50全平，保留后续行情用于成交，结束检查残仓和在途订单。

```bash
python demos/option_vega/run_backtest.py \
  --option-product MO --future-product IM --index-code 000852 \
  --start-day 2026-09-11 --end-day 2026-09-20 \
  --bar-timestamp end --option-tick 0.2 \
  --starting-balance 40000000 --vega-budget 1.25 \
  --log-level WARNING --require-fills
```

数据：opt_basic、fut_basic、期权/同月期货/指数 OHLCV 分钟文件。不要求主力角色表、外部历史 IV/Greeks。默认从已完成交易日完整性检查的行情日期推导日历，可显式覆盖。

到期阈值用交易日；定价自然年化到最后交易日15:00。推导日历不足以判断远期剩余天数时不能当成0天，应补后续日期覆盖或显式日历。

行情时效默认120秒，IV缓存默认300秒。午休/隔夜恢复会等待持仓相关腿更新；正常时段行情陈旧仍报错。不要以复制旧价或无限放大阈值掩盖缺失。原先出现15:00持仓最新报价14:57（180秒）的错误，需核对尾部行情、清仓完成情况、分钟口径与最后有效报价，不能仅凭“程序报错”认定数据不需处理。

卖方保证金仍是权利金比例近似；没有真实组合保证金、行权结算、盘口滑点和成交量约束。零成交延续价参与基础Bar撮合可能偏乐观。同步运行环境时删除旧 signal.py，避免遮蔽标准库 signal 导致 uvloop 缺 NSIG。

### 4.9 option_delta：文档 Delta 选约（最新）

2026-10-04 用户明确要求直接使用分钟 close，不增加价格模式参数，也不保留盘口模式。以下描述已同步这一变更；历史盘口规则不再是当前输入要求。

来源：《中金所股指期权Delta选约策略说明.docx》。本机源文件：
`/Users/kerry/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/flaght_23ec/msg/file/2026-09/中金所股指期权Delta选约策略说明.docx`。

文件：`strategy.py`、`selection.py`、`model.py`、`option_math.py`、`local_input.py`、`parser.py`、`run_backtest.py`、`README.md`、`__init__.py`。

支持 MO/IO/HO，自动对应 IM/IF/IH、000852/000300/000016，可覆盖。先确定期限，默认自然日20–45，目标32.5，至少7天；随后只在该月份分别筛选虚值 Call 与 Put，不跨月补腿。

- 核心绝对 Delta：0.25–0.30，目标0.275；无核心候选时可回退0.225–0.325。
- 直接使用close反解IV；默认BS指数模型，可选Black76同月期货。
- 排名依次为Delta距离、较高持仓量、较高分钟成交量，最后按合约代码确定顺序。
- 拒绝缺本分钟Bar、无效/非正close、负或低持仓量/成交量、套利边界不合理价格。
- CustomBar 携带close、open_interest、volume、bar_ns，完整分钟帧后选约，不使用下一帧的价格。期限由完整静态合约资料确定，不因目标月份缺行情而改选其他月份。

```bash
python demos/option_delta/run_backtest.py \
  --product MO --start-day 2026-09-11 --end-day 2026-09-20 \
  --bar-timestamp end --option-tick 0.2 --require-selection
```

**该策略只选约，不交易。** 文档将买卖方向、单双腿组合、数量、对冲、退出、风险预算列为待确认。本轮采取只选约的保守默认，用户尚未确认上述交易规则。下一对话不要从 option_vega 的“卖认购”推断 option_delta 也卖双腿。

必须有 datetime、close、open_interest、volume（分钟增量）。现有OHLC文件包含这些列即可使用。忽略盘口、quote_datetime和updateTime，不生成替代bid/ask。默认min-volume=0允许零成交分钟，报告zero_volume_bar；close可能是延续价，不代表最后成交价已更新。已有--min-volume 1可排除零成交分钟。静态日期/月度一致性、分钟对齐和同帧校验仍保留。

BS不需要期货数据；Black76需要同月期货，缺失记录MISSING_SAME_MONTH_FORWARD，不自动代用其他月份。期限默认自然日，与option_vega的交易日开平仓阈值不同。13:58、32.5天、2%利率、0股息率及OI下限1均为可配置实现默认，并非文档全部明确要求。--max-relative-spread和--min-depth已移除；--max-quote-age-seconds兼容原名，仅控制完整帧迟到限制，不检测真实成交时间。

输出：selections.csv、decisions.csv、candidates.csv、summary.json。价格列改为close，附price_source=close和zero_volume_bar；summary标记research_only=true、order_book_checks=false，不含盘口报告字段。只出一侧时PARTIAL；无候选NO_SELECTION；帧迟到LATE_FRAME。没有orders/fills/P&L，require-selection只检查是否选出至少一个合约，不要求所有时点或双腿都成功。

## 5. 报告、日志与典型故障

交易demo成功后一般保存 `demos/<策略>/results/<run_id>/`，包含summary、orders、fills、positions、account及可选tearsheet.html；option_vega另有signals.csv，scheduled_targets另有schedule_audit.csv。具体账户分交易所文件名见入口。option_delta输出是选约报告，不能用它做成交绩效统计。

多数交易入口支持 `--log-level ERROR|WARNING|INFO|DEBUG`，默认WARNING；ERROR更少。阶段耗时和行情覆盖的print不一定受原生日志级别控制。option_delta没有该参数，只打印概要和报告路径。require-fills要求至少模拟成交一笔，不代表策略盈利或数据完整。

| 现象 | 排查方向 |
|---|---|
| 请求到7月，实际只到1月/5月 | 检查文件日期覆盖；多个期货入口检查大于14自然日缺口。周末端点早于请求日正常 |
| 角色资料目录里找不到Bar | 行情变量错指role；两类目录必须分开 |
| recent合约到期缺Bar | role_cross/calendar_spread已移除recent；有旧仓仍需旧合约平仓行情 |
| 行情时间差超风控限制 | 订单所需合约参考价过期，尤其换月、跨场次；核对真实行情更新 |
| 同月期货不在上市期间/缺基本资料 | 比较listDate/lastTradeDate与文件日期；不要假造上市日期或无限外推可对冲月份 |
| 最终清仓后不足行情/残仓 | 提早flatten-slot，核对持仓腿尾部Bar和后续撮合事件；不能删除持仓冒充平仓 |
| signal无NSIG | 检查策略目录是否遗留signal.py遮蔽标准库 |
| targets.csv找不到 | 核对工作目录与相对路径，或传绝对路径 |
| option_delta输入缺列 | 当前要求datetime/close/open_interest/分钟volume，不再要求盘口 |

## 6. 验证现状与下一步

本机缺真实行情文件与完整bomber原生运行时；此前用户在Linux运行发现的错误不等于本机已完成真实回测验收。

- 最新 option_delta：31项收盘价研究模式的数值、选约、输入、解析器、真实StrategyTemplate生命周期及报告夹具测试通过；脚本help和源码语法检查通过。Feather读取/行情原生类型/Feed/Runner用夹具替换，不是完整FileReplayFeed原生回放验收。
- 项目已有 `tests/test_demo_option_vega.py`、`test_demo_role_cross_three_roles.py`、`test_demo_scheduled_targets.py` 等测试文件。本次交接未重新执行这些测试，不宣称当前全部通过。
- 其他demo已有实现、README与历史运行讨论；缺少当前真实区间“全部成功且清仓完成”的统一验收记录。代码存在不等于绩效有效。

新对话优先：

1. 先确认操作的是本机pro项目及对应demos，不改备份/错目录。
2. 若用户要启动回测，核对环境变量、实际数据文件名、上市状态、分钟口径，先看覆盖与异常，再评价收益。
3. 对option_delta若要接执行，先明确买卖方向、单/双腿、数量或预算、退出/换月、风险边界，再接现有执行路由；不能仅凭选中Call/Put推导持仓。
4. 若修改任一策略，同步该目录README，并给修改文件清单与实际验证范围。没有原生环境时明确区分夹具验证和真实撮合。

## 7. 新对话可直接粘贴

```text
请先读取 /Users/kerry/work/bomber1/pro/doc/HANDOFF_DEMOS_2026-10-04.md，
再读取我要继续处理的 demos/<策略>/README.md 及代码。
项目根目录是 /Users/kerry/work/bomber1/pro，当前策略框架是 trader。
代码与交接写在项目内，不要写 /private/tmp，不要误改 examples 或备份目录。
不要把 DataHub 重构成行情加载模块。品种/路径通过配置提供。
目前 demos 有9个策略，其中option_delta只选约，交易方向和退出规则尚未确定；
不能把它当成已完成交易绩效回测。改完请列出具体文件与验证结果。
接下来我需要你处理：<填写策略目录和问题>。
```

## 8 公共输入层第一阶段补充

本交接的策略业务说明继续适用。2026-10-04 已按后续方案生成公共 dataprep 层并接入九个 demo，原先关于各目录独立读取器和环境变量入口的描述，以 [公共输入开发说明](../../../bomber/framework/dataprep/README.md) 与 [第一阶段实现交接](../../strategies/DEMOS_INPUT_STAGE1_IMPLEMENTATION_2026-10-04.md) 为准。

当前角色表默认 fut_contract.feather，旧名仅在新名缺失时回退；各入口统一路径配置。七个期货入口新增 --bar-timestamp，默认 end；两个期权入口仍要求明确 start/end。输入标准化后通过现有 Feed.add_source 回放，新增三份输入诊断报告。

本次生成代码未执行任何导入、语法检查、测试或回测。上文历史测试记录不能作为公共输入迁移后的通过证据。下一步先在完整运行环境验收第一阶段，再开发公共运行工厂及 run_live。
