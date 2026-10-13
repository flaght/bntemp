# 中金所股指期权 Delta 选约

保留《中金所股指期权Delta选约策略说明.docx》的期限、虚值和 Delta 规则，支持 MO、IO、HO。按用户 2026-10-04 的要求，直接使用分钟 `close` 做研究选约，不保留盘口模式，也不需要 `--quote-mode` 参数。策略继承当前项目 `trader.StrategyTemplate`，通过 `UnifiedStrategyRunner` 和 `FileReplayFeed` 接收 `CustomBar` 分钟收盘价；实现独立放在本目录，不引用 examples 的策略代码。

**只选约和输出研究报告。** 原文将买卖方向、单腿/双腿组合、数量、对冲、退出和风险预算列为待确认。本实现不自行决定这些规则，不发委托，因此没有收益曲线。收盘价选约不验证盘口质量或可成交价格。

## 规则

1. 根据上市、最后交易日期和剩余自然日筛选期限。默认 20–45 天，选择最接近 32.5 天的月份；同距离选择较早到期月份。可显式指定月份。任何模式均剔除剩余不足 7 个自然日的合约。
2. 在选定月份分别筛选虚值 Call 和 Put：Call 行权价大于指数，Put 行权价小于指数。不因为某月份缺分钟行情而使用其他月份补腿。
3. 排除缺本分钟 Bar、无效/非正收盘价、无效或低持仓量/分钟成交量，以及不能反解 IV 的价格。
4. 直接用 `close` 反解 IV，统一计算 Delta、Gamma、Vega。默认 BS 使用指数；可选择 Black76 使用同月期货。Put 的 Delta 为负。
5. Call 选 Delta 0.25–0.30；Put 选 Delta −0.30 至 −0.25，目标绝对 Delta 0.275。如果核心区间无人可选，允许在绝对 Delta 0.225–0.325 中回退，使用 `--no-fallback` 可关闭。
6. 按以下顺序比较：绝对 Delta 到目标的距离、更高持仓量、更高分钟成交量；最后按合约代码保证结果确定。报告标明是否使用回退。不再按买卖价差或盘口深度筛选、排名。

Call 和 Put 选约相互独立；只有一侧合格时记为 `PARTIAL`，不会把单腿结果当成可直接交易的双腿组合。每个选约时点等全部同时间戳快照进入后计算，避免使用下一分钟价格。

期限选择依据 `opt_basic` 的完整静态合约资料，不按行情文件存在与否缩小候选月份。例如目标月份为 10 月，但只有 11 月行情，仍记录 10 月缺 Bar，不会静默改选 11 月。因此基本资料本身必须完整；程序无法发现资料中完全缺失的月份。

13:58、32.5 天期限目标、2% 利率、0 股息率、持仓量下限 1，都是本实现的可配置默认值；原文没有为这些参数全部指定固定数值。请按实际数据及研究设置调整。

## 数据与环境变量

```text
CTP_DATA_DIR/
  role/opt_basic.feather
  role/fut_basic.feather                 # 仅 Black76 必须
  kline/opt/YYYYMMDD/MO2610-C-7000_YYYYMMDD.feather
  kline/index/YYYYMMDD/000852_YYYYMMDD.feather
  kline/fut/YYYYMMDD/IM2610_YYYYMMDD.feather # 仅 Black76 必须
```

支持现有 `FUT_ROLE_DATA_DIR`/`ROLE_DIR`、`KLINE_DATA_DIR`/`KLINE_DIR`，以及各类行情的 `OPT_KLINE_DATA_DIR`、`INDEX_KLINE_DATA_DIR`、`FUT_KLINE_DATA_DIR`。命令行对应目录参数优先；行情根目录必须指向行情，不是 role 资料目录。

期权基本资料必须有 `symbol`（可用 `Code`）、`exchangeCD`、`contractType`、`strikePrice`、`contMultNum`、`varTicker`、`listDate`、`lastTradeDate`、`expDate`。不要求期货式的品种 `code`。tick 可来自 `minChgPriceNum` 或 `price_increment`；否则显式传 `--option-tick` 或设置 `OPT_PRICE_INCREMENT`。最后交易日期用于计算可交易剩余自然日。

上市、最后交易和到期日期不得为空或无效；最后交易/到期日期必须与合约月份一致，同月期权最后交易日期必须一致。Black76 的期货资料也检查空日期及上市/最后交易日期顺序。

期权分钟快照必须有：

```text
datetime close open_interest volume
```

`datetime` 应是北京时间；带时区和不带时区均支持。`volume` 应是当分钟成交量，不是交易日累计成交量。指数和期货必须有 `datetime close`；可保留原有其他字段。可选 `symbol` 用于核对文件身份。

分钟快照 `datetime` 必须对齐整分钟，不能带非零秒或亚秒；开始标签加一分钟后不能跨出文件所属日期。负成交量、负持仓量一律记为 `INVALID_BAR_FIELDS`，即使相应流动性阈值设为 0。所有数值参数均须有限，不能用 NaN 或无穷时效绕过校验。

已有 OHLC、分钟 volume 和 open_interest 文件可以直接使用，无需补盘口。即使文件带 bid/ask、盘口数量、quote_datetime 或 updateTime，选约也不使用这些字段，不生成任何替代盘口。

默认 `--min-volume 0`，允许零成交分钟参与选约，并输出 `zero_volume_bar=true`。这些分钟的 close 可能是延续的旧成交价；无法从分钟时间确认真实最后成交时间。已有 `--min-volume 1` 可以排除零成交分钟。期权、指数及 Black76 期货都只使用同一已完成分钟的 Bar，不用旧帧补缺。

## 启动

在项目 pro 根目录执行：

```bash
export CTP_DATA_DIR=/workspace/data/temp_bomber1/ctp

python demos/option_delta/run_backtest.py \
  --product MO \
  --start-day 2026-09-11 --end-day 2026-09-20 \
  --bar-timestamp end --option-tick 0.2 \
  --require-selection
```

MO 自动对应指数 000852、期货 IM；IO 对应 000300/IF；HO 对应 000016/IH。均可用 `--index-code`、`--future-product` 显式覆盖。默认 BS 不需要期货行情，不使用不存在的同月期货替代指数。

Black76 示例（必须具备对应月份的期货基本资料和行情）：

```bash
python demos/option_delta/run_backtest.py \
  --product MO --model black76 --expiry-month 202610 \
  --start-day 2026-09-11 --end-day 2026-09-20 \
  --bar-timestamp end --option-tick 0.2 \
  --select-slots 10:00,13:58
```

`--expiry-month` 使用 YYYYMM，显式指定后不受 20–45 天窗口限制，但仍受最低剩余天数限制。默认期限窗口下，9 月 11 日不会选择 9 月 18 日到期合约，因为只剩 7 天。

| 参数 | 作用 | 默认 |
|---|---|---|
| `--bar-timestamp` | 分钟时间表示开始或结束；start 在回放前加一分钟，防止提前使用完整分钟数据 | 必填 |
| `--model` | BS 指数模型 / Black76 同月期货模型 | bs |
| `--sides` | C、P 或 C,P | C,P |
| `--select-slots` | 每天选约时点，以逗号分隔 | 13:58 |
| `--dte-min` / `--dte-max` / `--target-dte` | 剩余自然日范围及期限优先目标 | 20 / 45 / 32.5 |
| `--min-remaining-days` | 最低可用剩余自然日 | 7 |
| `--delta-min` / `--delta-max` / `--target-delta` | 核心绝对 Delta 范围及目标 | 0.25 / 0.30 / 0.275 |
| `--fallback-min` / `--fallback-max` | 无核心候选时的绝对 Delta 回退范围 | 0.225 / 0.325 |
| `--no-fallback` | 关闭回退 | 不关闭 |
| `--rate` / `--dividend-yield` | 年化利率和股息率，使用小数 | 0.02 / 0 |
| `--max-quote-age-seconds` | 兼容原参数名，仅控制完整分钟帧最大迟到秒数，不判断最后成交时间 | 60 |
| `--min-open-interest` / `--min-volume` | 持仓量、当分钟成交量下限 | 1 / 0 |
| `--require-selection` | 全区间未选出任何合约时返回错误；PARTIAL 也算选出 | 关闭 |
| `--report-dir` | 报告目录，每次创建独立子目录 | 本目录 results |

不需要独立交易日历；使用区间内期权/指数文件日期，假设数据已检查交易日完整性。某天缺指数或整个期权链则报错；完全缺失的交易日无法凭这些文件检测。区间两端可包含周末。选约时点缺失会在报告保存后报错。

旧参数 `--max-relative-spread`、`--min-depth` 已移除，旧启动命令中如有这两项请删除；不需要增加任何价格模式参数。请核实源分钟标签后继续显式传 `--bar-timestamp start|end`。

## 输出和排查

- `selections.csv`：选中合约、方向类型、到期月份、Delta/IV/Gamma/Vega、close、price_source=close、bar_ns、open_interest、volume、zero_volume_bar、时点和回退标记。移除 mid/bid/ask/relative_spread/depth/quote_ns。
- `decisions.csv`：每个时点 SELECTED/PARTIAL/NO_SELECTION/LATE_FRAME。
- `candidates.csv`：候选和拒绝原因，包括 MISSING_BAR、MISSING_BAR_TIME、MISMATCHED_BAR_TIME、MISSING_BAR_FIELDS、INVALID_BAR_FIELDS、INVALID_CLOSE、LOW_LIQUIDITY、INVALID_IV_OR_ARBITRAGE_BOUND、MISSING_SAME_MONTH_FORWARD。
- `summary.json`：参数、请求/实际日期、缺失时点、回放数量及分钟价格时间政策；price_source=close、research_only=true、order_book_checks=false，说明零成交分钟政策；orders/fills 恒为 0。

`LATE_FRAME` 表示下一帧到达时间超过允许间隔；没有使用迟到后的价格追补选约。Black76 缺同月合约则不选约，不自动拿其他月份代替。

验证：`PYTHONPATH=. python -m unittest discover -s tests -p test_demo_option_delta.py`。数值、筛选、输入和真实 StrategyTemplate 生命周期使用夹具验证；完整 FileReplayFeed 原生回放仍需安装项目的 bomber 运行时并具备真实分钟行情文件。

2026-10-04 已将盘口模式替换为收盘价研究选约，31 项测试通过；验证覆盖 OHLC 输入、close 定价及盘口字段忽略、零成交标记/过滤、缺整月行情、数值计算、日期一致性、分钟对齐、微秒时间戳、解析器及报告输出。脚本 help 和源码语法检查通过。Feather读取/原生类型/Feed/Runner使用夹具，不代表真实行情或原生回放验收。
