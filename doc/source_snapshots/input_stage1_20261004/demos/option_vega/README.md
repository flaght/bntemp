# Option Vega：strategy1 的统一框架实现

本目录基于 `examples/opts2/strategy1.py` 的无数据库版本实现：卖出虚值认购期权，以同月股指期货对冲 Delta。默认组合为 MO、IM、中证1000指数 `000852`，可通过参数更换匹配的股指组合。策略实现独立保存在本目录，不导入 examples 中的策略。

## 数据是否足够

你已经说明具备期权基本资料、期货基本资料，以及期权、期货、指数分钟文件；`000852_20260901.feather` 的命名可以直接识别。对于本版本，数据类别已基本齐全，IV、Delta、Gamma、Vega 从价格反解，无须额外导出历史 Greeks、IV 或 Shibor；利率由 `--rate` 配置。

行情已完成交易日完整性检查，因此默认直接从行情日期推导交易日，不再要求单独的日历文件。运行前请确认：

- **行情日期范围**：自动扫描 fut/opt/index 全部 `YYYYMMDD` 目录，取其中存在匹配日期 Feather 文件的日期并集，排序去重；不会只截取回测区间。该模式信任你已检查的数据完整性，不会自动补齐工作日或读取区间外价格。
- **结束后的日期覆盖**：到期在已知日期范围内时精确统计；到期更远时，只需已有日期足以判断 5 日开仓、2 日清仓阈值，不要求远期期权的数据一直到到期。例如回测到 9 月 10 日，可以使用已有 9 月后续行情日期判断远期合约是否至少剩余 5 个交易日。若日期尾部不足以判断，报错而不把未知日期当成零天；可缩短回测结束日期、补充后续行情，或选用显式日历。
- **可选交易日历**：需要覆盖配置时仍可提供 `--calendar` 或 `CTP_CALENDAR_PATH`。CSV/Feather 包含 `date,is_trading_day`，标记取 True/False 或 1/0。显式日历优先于行情日期推导。
- **期权最小变动价位**：优先读取 opt_basic 的 `minChgPriceNum` 或 `price_increment`；缺失时用 `--option-tick` 或 `OPT_PRICE_INCREMENT` 提供经核实的值。

当前机器没有真实数据和原生 bomber 运行依赖，因此已验证的是数值、文件读取和目标接口测试，尚未验证真实区间的覆盖和原生撮合结果。

目录结构：

```text
CTP_DATA_DIR/
  role/
    opt_basic.feather
    fut_basic.feather
  kline/
    opt/20260901/MO2609-C-6600_20260901.feather
    fut/20260901/IM2609_20260901.feather
    index/20260901/000852_20260901.feather
```

本策略直接读取真实月份合约，不使用主力角色表或复权因子。行情的最小必需字段为 `datetime,open,high,low,close,volume`；可保留 `symbol,trade_date` 用于一致性检查。没有 `timestamp` 也可以回测。`datetime` 支持上海时区或不带时区的上海本地时间；不会修改原文件。`volume` 必须已经转换为当分钟成交量，不能直接传入交易日累计成交量。

基本资料要求：

| 文件 | 必需字段 |
| --- | --- |
| opt_basic | symbol（或 Code）、contractType、strikePrice、contMultNum、varTicker、exchangeCD、listDate、lastTradeDate、expDate |
| fut_basic | symbol、code、exchangeCD、contMultNum、minChgPriceNum、listDate、lastTradeDate |

期权资料不要求期货式的 `code` 品种列，品种直接从 `symbol`/`Code` 的合约代码识别；兼容两种字段名。若两列同时存在，规范化后的合约代码必须一致。字段值两侧空格会去除。

可选 `currencyCD` 必须为 CNY。交易所接受 CCFX/XCFX/CFFEX。期权代码保留 `MO2609-C-6600` 中的连字符，指数代码按六位字符串识别。同一合约的相同静态条款重复行可合并；条款冲突会报错，需要按历史实际可用资料整理。

## 启动

在项目 `pro` 根目录执行。以下 tick=0.2 仅作为配置示例，使用前按你的合约资料核实；时间区间需同时具备 MO、同月 IM 和指数数据。

```bash
export CTP_DATA_DIR=/workspace/data/temp_bomber1/ctp
export OPT_PRICE_INCREMENT=0.2

python demos/option_vega/run_backtest.py \
  --option-product MO --future-product IM --index-code 000852 \
  --start-day 2026-09-01 --end-day 2026-09-10 \
  --bar-timestamp end \
  --starting-balance 40000000 --vega-budget 1.25 \
  --log-level WARNING --require-fills
```

`--bar-timestamp` 必须显式指定。`end` 表示文件时间是分钟结束时间，直接使用；`start` 表示分钟开始时间，在内存中加一分钟，使完整 OHLC 到结束后才可用于决策。不要只凭截图的分钟标签判断这个口径。

已有环境变量也可使用：

| 配置 | 环境变量/参数 |
| --- | --- |
| 资料目录 | FUT_ROLE_DATA_DIR 或 ROLE_DIR；默认 CTP_DATA_DIR/role |
| 行情父目录 | KLINE_DATA_DIR 或 KLINE_DIR；默认 CTP_DATA_DIR/kline |
| 期货行情 | FUT_KLINE_DATA_DIR / --fut-dir |
| 期权行情 | OPT_KLINE_DATA_DIR / --opt-dir |
| 指数行情 | INDEX_KLINE_DATA_DIR / --index-dir |
| 基本资料文件 | --fut-basic、--opt-basic |
| 交易日历（可选） | CTP_CALENDAR_PATH / --calendar；不配置则自动取行情日期 |
| 期权 tick | OPT_PRICE_INCREMENT / --option-tick |

显式文件/目录参数优先。资料目录与行情目录分别解析，FUT_ROLE_DATA_DIR 不会用于查找 Bar。各行情变量既可指向对应 opt/fut/index 目录，也可指向存在该子目录的 kline 父目录。

## 策略逻辑

1. 三类行情经 `FileReplayFeed` 和 `DataBinding` 推送到 `on_bar`。指数只用于研究信号，不创建执行路由。使用下一时间戳到达作为上一分钟的帧结束标志，同一时间戳的全部行情到齐后计算；跨午休或隔夜长间隔不会补执行旧时点。
2. 默认每日 13:58 选约。筛选行权价高于指数、至少剩余 5 个交易日的认购；只使用期权、同月期货、指数同一分钟的价格。候选的同月期货须有基本资料、已上市且未到期；远期期权存在但同月期货缺基本资料时排除该候选（MISSING_BASIC），不阻断近月选约；期货未上市（NOT_LISTED）或已到期（EXPIRED）也排除，并在日志和 summary.json 记录原因。期权上市早于同月期货时，待对冲期货可用后才纳入。仍检查已上市对冲腿的实际行情，缺失不会静默跳过。在可有效反解 IV 的候选中选择最近到期月份。
3. 用同月期货价格作为 Black-76 的 forward，反解 IV 和 Greeks；到期时间统一采用截至最后交易日 15:00 的自然年化时间。按 Delta 最接近 0.2、0.25、0.3、0.35 选约，同一合约可以匹配多个档位，先合并再计算手数。
4. 按每手现金 Gamma 的倒数分配权重，以现金 Vega 预算归一化，向下取整并施加单合约/总手数上限。默认预算 NAV=4000万元、vega-budget=1.25，对应 IV 变化 **1 个百分点**时约 5000 元的组合 Vega，取整后可能更低。
5. 每次选约提交整个组合的目标仓位，卖方期权目标为负数；每日重新计算后替换目标，避免无限累加昨日仓位。
6. 同月期货对冲目标为 `round(-Σ(实际期权手数 × 期权乘数 × Delta) / 期货乘数)`。默认 10:00、11:00、13:30、14:00、14:55 复核；期权成交仓位变化也会触发复核。根据实际成交仓位对冲，在途期货订单期间等待回报，不把未成交目标当作持仓。
7. 时间价值 `期权价格-max(指数价格-行权价,0)` 不大于零，或剩余交易日不超过 2 天时，将期权目标置零。旧月份明确输出零目标，直到实际平仓后撤掉该月份的期货对冲。
8. 最后一个回测交易日默认 14:50 开始全部清仓；指数信号时钟要求清仓时点后至少两分钟行情；未持有的候选不要求尾部行情。实际持仓必须有新鲜报价，并由后续交易腿 Bar 完成撮合；缺新鲜报价会报错。结束时检查实际仓位、在途订单和执行回报，残留即报错。

IV 反解失败时只允许使用有限时间内的成功缓存，默认 300 秒。隔夜或午休长间隔后，最多按 max-market-age-seconds 等待持仓相关腿的首Bar更新；等待期间保留目标和对冲，不使用上一场次价格计算。正常时段行情过期或恢复后超过等待期限仍未更新，会报错并显示决策时间、最新报价时间、报价年龄、实际/目标仓位与在途数量。已有持仓缺少新鲜价格或有效 Delta 会报错；处于平仓过程且 IV 无效时保留原对冲，等待实际期权平仓。推导日期并集中的回测交易日仍会检查指数、期权和同月期货文件；缺少所需文件会报错。所有来源同时缺少某日期时，自动模式遵照完整性已检查的前提，将其视为非交易日。

## 主要参数

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| --option-product / --future-product / --index-code | MO / IM / 000852 | 匹配的股指期权、对冲期货、标的指数 |
| --start-day / --end-day | 必填 | 回测日期，包含两端 |
| --bar-timestamp | 必填 | 分钟开始或结束标签 |
| --calendar | 不提供 | 可选日历覆盖；默认根据完整行情日期自动推导 |
| --starting-balance | 40000000 | 模拟账户初始资金 |
| --nav | 同初始资金 | 固定策略规模基准，不随账户权益自动增长 |
| --vega-budget | 1.25 | 每 1% IV 的现金预算=nav×该值/10000 |
| --delta-targets | 0.2,0.25,0.3,0.35 | 选约 Delta 档位 |
| --option-slot | 13:58 | 每日选约时点 |
| --hedge-slots | 10:00,11:00,13:30,14:00,14:55 | 定时对冲复核 |
| --min-remaining-days / --close-remaining-days | 5 / 2 | 最少开仓剩余交易日、到期前清仓阈值 |
| --flatten-slot | 14:50 | 最后回测日清仓时点 |
| --rate / --min-time-value | 0.02 / 5 | 年化利率、反解 IV 的最少时间价值点数 |
| --max-option-lots / --max-total-option-lots | 100 / 200 | 单期权和总期权手数上限 |
| --max-future-lots | 100 | 单期货对冲手数上限，所需对冲超过上限会报错 |
| --max-market-age-seconds / --max-iv-age-seconds | 120 / 300 | 行情与 IV 缓存最长允许年龄 |
| --commission | 1 | 每手每次成交固定手续费，期权和期货共用 |
| --margin-init / --margin-maint | 0.15 / 0.12 | 基础期货保证金比例 |
| --option-margin-init / --option-margin-maint | 1 / 1 | 基础期权保证金比例，按期权权利金名义金额计算 |
| --max-notional | 100000000 | 单订单和单合约仓位名义金额上限 |
| --log-level | WARNING | 原生框架日志级别；ERROR 更少日志 |
| --report-dir | 本目录/results | 报表输出目录 |
| --no-tearsheet / --require-fills | 关闭 | 跳过绩效图 / 没有成交时报错 |

运行 `python demos/option_vega/run_backtest.py --help` 查看所有参数。

## 框架与回测边界

使用当前 `trader.StrategyTemplate → set_targets → UnifiedStrategyRunner → SimulationExecutionClient → NautilusSimExecutionBackend`，由 `UnifiedHistoricalRuntime` 管理回放和撮合。策略不直接调用交易引擎下单，也没有另建 DataHub 数据接口。

该版本用于策略逻辑和基础盈亏回测：NETTING 净持仓、Bar 撮合、固定手续费、StandardMarginModel。期权保证金按权利金比例近似，**尚未实现真实卖方保证金、组合保证金减免、现金行权结算、盘口价差、滑点和成交量约束**。零成交量分钟的延续价格仍可参与基础 Bar 模型，因此低流动性合约的收益可能偏乐观。多腿目标并非原子成交。不能把该版本的资金占用和收益直接当作实盘结果。

与原示例有意不同：采用完整目标替换和真实成交仓位对冲，统一 Greeks 时间口径，IV 缓存设有效期，增加到期前和回测末日清仓。原示例的策略思想保留，但结果不会逐笔完全一致。

## 输出与验证

每次运行生成独立 results/<run_id>，包含 `orders.csv,fills.csv,positions.csv,account.csv,signals.csv,summary.json`，以及可选 `tearsheet.html`。原生可视化依赖缺失时仍保存原始报表。

测试：

```bash
python -m unittest tests.test_demo_option_vega -v
```

模拟数据测试覆盖自动交易日推导、可选日历与日期边界、远期剩余交易日下界、IV 反解/导数、Vega 预算和手数上限、Feather 无 timestamp 读取、基础条款重复与冲突、分钟帧同步、实际成交后对冲、每日目标不累加、末日零目标、持仓行情缺失和在途订单等待。行情类型使用夹具，目标输出使用项目真实 StrategyTemplate；测试不代表原生撮合集成验证。

## 直接启动时的标准库导入

数值模块使用 `option_math.py`，避免策略目录下的 `signal.py` 遮蔽 Python 标准库 `signal`，导致 uvloop 报 `module 'signal' has no attribute 'NSIG'`。同步此版本到运行环境时，请删除旧的 `demos/option_vega/signal.py`，不能只复制新文件而保留旧文件；启动命令不变。
