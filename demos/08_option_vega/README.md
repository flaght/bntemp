# 认购期权卖方与同月期货对冲策略（08_option_vega）

按固定 Vega 预算卖出虚值认购期权，以实际归属成交仓位计算 Delta，再用同月期货对冲。默认组合为 MO / IM / 000852。回测与 Live 共用 `OptionVegaStrategy` 和 `assembly.py`，只替换输入来源、执行组件和生命周期。

本文按当前入口源码及 `doc/` 专项交接整理，更新日期为 **2026-10-13（Asia/Shanghai）**。操作入口如下：

| 运行方式 | 入口 | 行情与执行 | 结束行为 |
| --- | --- | --- | --- |
| 文件回测 | `run_backtest.py` | 文件分钟行情 + 原生模拟撮合 | 最后实际回测日主动清仓，核对实际仓位和在途订单 |
| Live Recording | `run_live.py --mode recording` | 在线行情 + 目标记录 | 不连接 TD，不产生成交，不保存交易状态文件 |
| Live Model | `run_live.py --mode model` | 在线行情 + 参考价模型成交 | 保存独立模型仓位，停机保留仓位 |
| Live SimNow | `run_live.py --mode simnow` | 在线行情 + 原生 CTP 模拟柜台执行 | 撤销自身活动订单、保存归属仓位，停机保留已成交仓位 |

建议先阅读[回测](#2-文件回测)，再按 [Live 运行方式](#3-live-运行方式)选择执行模式。完整参数分别见回测参数表、[Live 参数表](#4-live-参数说明)；每个代码文件的作用和必要性见[代码职责](#6-代码文件的作用与必要性)。所有命令从 `pro` 根目录执行，使用已安装项目依赖和原生 Bomber 扩展的 Python 环境：

```bash
cd /Users/kerry/work/bomber1/pro
export PYTHONPATH=.
python demos/08_option_vega/run_backtest.py --help
python -m demos.08_option_vega.run_live --help
```

远程运行时将 `cd` 改为部署机器的 `pro` 路径。入口读取 `.env`，环境变量已有值优先；日期、行情表和前置地址必须与实际来源核对。本文提供运行方法，本次文档更新没有连接数据库或柜台。

## 1. 策略逻辑

### 策略思路与候选选择

这是按固定 Vega 预算卖出虚值认购、再用同月期货管理 Delta 的组合策略。它不比较隐含波动率与预测波动率，也不以 IV 高低设置择时门槛；IV 和风险指标主要用于选约、配仓与对冲。

每日默认 13:58，要求认购、同月期货和指数同一分钟价格都已到达。候选须在生命周期内、行权价高于指数、至少剩余 5 个交易日，并能得到有效 Greeks。先保留合格候选中最近的最后交易日，再为各 Delta 档选择最接近的认购；距离相同时按合约代码排序。

同月期货作为 Black76 定价输入，认购真实权利金用于反解 IV。期限按距离最后交易日 15:00 的自然年化时间计算；开平仓天数阈值则使用交易日计数，两者用途不同。

### Vega 预算与卖方手数

设规模基准为 NAV，预算参数为 b，合约乘数为 m。函数中的 Vega 按波动率变化 1 计算：

```text
现金 Vega 预算 B = NAV × b / 100
每一个百分点波动率变化的现金预算 = B / 100
每个 Delta 档的权重 a_i = 1 / (Gamma_i × m_i)
该档未取整手数 = B × a_i / Σ(a_j × Vega_j × m_j)
```

同一认购可匹配多个 Delta 档，先合并数量，再向下取整并应用单合约、总手数上限，最终目标取负数。默认 NAV 40000000、b=1.25，对应每一个百分点的现金预算 5000。NAV 是固定配仓基准，不随回测账户权益更新；每日重新计算替换总目标，不向原仓位累加。取整或手数上限可能使实际 Vega 低于预算。

### 实际持仓对冲与退出

| 环节 | 当前规则 |
| --- | --- |
| 对冲基础 | 使用实际已成交认购仓位，不用尚未成交的目标仓位代替敞口 |
| 分月汇总 | 每个同月期货分别汇总其对应认购的 `实际手数 × 乘数 × Delta` |
| 期货目标 | `round(-汇总敞口 / 期货乘数)`；卖方认购通常对应多头期货保护 |
| 复核触发 | 目标变化、实际认购持仓变化或指定对冲时点 |
| 在途门控 | 相应订单在途时等待；对冲需求超过上限则停止 |
| 认购退出 | 剩余交易日不超过 2，或以指数计算的时间价值不大于零 |
| 风险指标失效 | 仍需持有的认购无法可靠计算 Delta 时停止；待平认购暂保留已有对冲直到实际退出 |
| 最后回测日 | 默认 14:50 起全部期权和期货目标归零，结束核对实际仓位与在途订单 |

成功 IV 可在有效期内缓存，但重算 Greeks 仍使用当前价格与期限；缓存不代替新鲜行情。场次恢复后等待持仓相关腿的新报价，超时会停止，不能将昨日价格当作今天的对冲依据。默认真实价格与 IV 缓存年龄均为 300 秒。

新分钟到达时先处理上一分钟，避免用尚未送达的同帧行情选约。批量组合目标不保证认购与期货同步成交。本例没有额外止损或价格预测，验收重点是卖方方向、Vega 分配、实际成交后对冲、过期报价诊断和末日实际全平。

## 2. 文件回测

### 启动命令

从 pro 根目录、在已安装运行依赖的环境中执行，先设置 `export PYTHONPATH=.`。入口自动读取已有 `.env` 配置；没有配置数据路径时，可通过可选的 `--data-root` 或各文件路径参数指定。示例日期需与实际数据匹配。

**完整执行（显式设置常用参数）：**

```bash
python demos/08_option_vega/run_backtest.py \
  --option-product MO --future-product IM --index-code 000852 \
  --start-day 2026-09-01 --end-day 2026-09-11 \
  --bar-timestamp end --log-level WARNING --no-tearsheet
```

**默认执行（仅必填参数）：**

```bash
python demos/08_option_vega/run_backtest.py \
  --start-day 2026-09-01 --end-day 2026-09-11
```

默认执行采用参数表中的默认值及已有数据路径配置；例如默认启用绩效图，不要求必须产生成交。

### 参数说明

“必选”表示启动时必须传入；“可选”表示有默认值或可由已有配置解析。数据路径虽然是可选参数，但运行前必须能解析到有效文件。

| 参数 | 必选 / 可选 | 含义与默认值 |
| --- | --- | --- |
| `--start-day / --end-day` | 必选 | 必填，请求日期闭区间 |
| `--data-root` | 可选 | 数据根目录，通常包含 role、kline/fut、kline/opt、kline/index |
| `--fut-dir / --opt-dir / --index-dir` | 可选 | 显式覆盖期货、期权、指数真实分钟行情目录 |
| `--fut-basic / --opt-basic` | 可选 | 显式覆盖基础条款文件，默认 role 目录下 fut_basic.feather / opt_basic.feather |
| `--option-product / --future-product / --index-code` | 可选 | 默认 MO / IM / 000852；期权必须有可用同月期货对冲，指数只用于信号 |
| `--bar-timestamp` | 可选 | 可省略，默认 end；源分钟标签为 start 时须显式指定，公共层只移动一次到分钟结束 |
| `--calendar` | 可选 | 可选显式交易日历；未提供时由三类行情全部已知文件日期的并集推导，信任上游交易日完整性，不自动补工作日 |
| `--starting-balance / --nav` | 可选 | 初始资金默认 40000000；固定策略规模基准默认同初始资金，不随账户权益自动更新 |
| `--vega-budget` | 可选 | 默认 1.25；每一个百分点波动率变化的现金风险预算为 nav × 参数 / 10000 |
| `--delta-targets` | 可选 | 默认 0.2,0.25,0.3,0.35，按最接近各档位的 Delta 选择认购；重复合约合并 |
| `--option-slot / --hedge-slots` | 可选 | 每日选约默认 13:58；对冲复核默认 10:00,11:00,13:30,14:00,14:55；实际期权仓位变化也触发对冲 |
| `--min-remaining-days / --close-remaining-days` | 可选 | 开仓至少剩余 5 个交易日，到期前剩余不超过 2 日清仓；计数不含当天、含最后交易日 |
| `--flatten-slot` | 可选 | 最后回测日开始全组合清仓，默认 14:50；指数必须在该时点之后至少有两分钟行情 |
| `--rate / --min-time-value` | 可选 | 默认 0.02 / 5；固定年化利率及允许反解隐含波动率的最少期权时间价值 |
| `--max-market-age-seconds / --max-iv-age-seconds` | 可选 | 默认 300 / 300 秒，真实价格及成功波动率缓存的最长允许年龄 |
| `--max-option-lots / --max-total-option-lots / --max-future-lots` | 可选 | 默认 100 / 200 / 100；单认购、认购总手数、单对冲期货手数上限 |
| `--commission / --max-notional` | 可选 | 每手每次固定手续费默认 1；单订单及单合约持仓名义金额上限默认 100000000 |
| `--margin-init / --margin-maint` | 可选 | 基础期货保证金比例，默认 0.15 / 0.12 |
| `--option-margin-init / --option-margin-maint` | 可选 | 基础期权保证金比例，默认 1 / 1，以权利金名义金额近似 |
| `--require-fills / --no-tearsheet` | 可选 | 可选要求至少一笔成交 / 跳过绩效图 |
| `--index-precision` | 可选 | 指数价格精度默认 4，允许范围 [0,12] |
| `--report-dir` | 可选 | 输出根目录，默认本策略目录的 results/ |
| `--log-level` | 可选 | 框架日志等级，默认 WARNING |

### 数据要求

需要 opt_basic.feather、fut_basic.feather、认购链、同月期货与指数行情。期权最小变动价位直接读取 tickNum，缺失或为空时兼容 minChgPriceNum / price_increment，必须为有限正数；tickUnit 仅为单位说明。未指定 calendar 时用全部已知行情文件日期并集推导交易日；需覆盖期限判断所需的未来日期。末日指数须在清仓时点后至少还有两分钟行情，实际交易腿也须有退出报价和后续撮合机会。

| 数据项 | 要求 |
| --- | --- |
| opt_basic.tickNum | 从每条期权基础条款读取最小变动价位，必须为有限正数；无需命令行或环境变量指定 |

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
| signals.csv | 选约、调仓、对冲或退出行为记录 |

### 文件布局、路径优先级与日历

典型文件布局如下，目录日期与文件名日期必须一致，合约代码允许大小写差异：

```text
<data-root>/
  role/fut_basic.feather
  role/opt_basic.feather
  kline/fut/20260901/IM2609_20260901.feather
  kline/opt/20260901/MO2609-C-7300_20260901.feather
  kline/index/20260901/000852_20260901.feather
```

路径优先使用显式 `--fut-dir / --opt-dir / --index-dir / --fut-basic / --opt-basic`。环境变量可以配置 `CTP_DATA_DIR`、`FUT_ROLE_DATA_DIR`、`FUT_KLINE_DATA_DIR`、`OPT_KLINE_DATA_DIR`、`INDEX_KLINE_DATA_DIR`、`KLINE_DATA_DIR` 和 `CTP_CALENDAR_PATH`；专用目录优先于通用根目录推导。`--data-root` 设置根目录，但不会覆盖已配置的资产专用环境变量。完整兼容规则见[公共输入层](../../bomber/framework/dataprep/README.md#路径配置)。

分钟文件需要时间列（`datetime`，兼容 `timestamp`）和 `open/high/low/close/volume`，`volume` 为分钟增量；按日盘规则校验时间、身份和 OHLCV，不前填价格或补造分钟。`--bar-timestamp start` 会在准备层加一分钟，默认 `end` 不移动。该参数同时用于三类文件，因此上游标签需一致。期权基础条款需要合约代码、类型、行权价、乘数、标的指数、交易所和上市/最后交易/到期日期；字段契约见[基础条款说明](../../doc/data/BASIC_METADATA_ALIGNMENT.md)。

可从 Live 使用的公共日历导出同版本文件，避免由缺文件误判休市：

```bash
python -m scripts.integration.reference_data.calendar_export \
  --calendar-id china.cffex --start 2026-01-01 --end 2026-12-31 \
  --output demos/08_option_vega/results/cffex-2026.csv

python demos/08_option_vega/run_backtest.py \
  --data-root /path/to/market-data \
  --calendar demos/08_option_vega/results/cffex-2026.csv \
  --start-day 2026-09-01 --end-day 2026-09-11 \
  --bar-timestamp end --require-fills --no-tearsheet
```

`/path/to/market-data` 替换为实际根目录。导出 CSV 旁的 manifest 保存规则版本与摘要，回测加载时核验；普通 CSV/Feather 日历仍兼容。不传日历时由三类行情**全部已知文件日期的并集**推导，默认信任上游完整性；应让日期覆盖结束日之后足够多的交易日，以判断剩余期限。

### 回测指数如何接入、未传路径为何仍能运行

**当前 08 回测读取本地指数 Feather 分钟文件。** Live 的 `dolphindb-dfs` 指数使用声明式特征 Provider、Schema 与查询缓存；回测入口目前没有调用这条特征查询链，也不使用 Live 的 `features.json` 或 `--index-source` 参数。两个入口最终都向同一个策略提供指数价格，但输入装配不同：

| 场景 | 指数输入链路 | 策略如何接收 |
| --- | --- | --- |
| 当前文件回测 | 本地 `000852_YYYYMMDD.feather` → 公共 dataprep 校验/标准化 → `FileReplayFeed` | `DataBinding("000852", …, DataType.BAR, "1-MINUTE")` → `on_bar` → `latest["000852"]` |
| Live DFS 指数 | DolphinDB 原表 → 声明式 `index.minute` 特征 Provider/缓存 → `FeatureQueryBarFeed` → 封闭分钟帧 | `on_market_frame` → `latest["000852"]` |
| Live 标准指数流 | DolphinDB 已收盘 Bar 流 → 独立 Feed → 封闭分钟帧 | `on_market_frame` → `latest["000852"]` |

指数只用于选约、时间价值及清仓时钟，没有执行路由，不作为可成交期货加入账户。当前回测中，指数、期权和期货共用名为 `option-bars` 的回放 Feed 绑定；这个名字不表示其中只有期权。

回测代码位置按调用顺序如下：

| 位置 | 做什么 |
| --- | --- |
| [run_backtest.py](run_backtest.py)：模块初始化的 `load_dotenv()`、`run_case` 中 `resolve_option_args` | 加载环境配置，解析并验证 `args.index_dir` |
| [dataprep/paths.py](../../bomber/framework/dataprep/paths.py)：`resolve_paths / resolve_option_args` | 指数优先级：显式 `--index-dir` → `INDEX_KLINE_DATA_DIR` → `KLINE_DATA_DIR/index` → 根目录的 `kline/index`；根目录可来自 `--data-root` 或 `CTP_DATA_DIR`；无这些来源时兼容 `KLINE_DIR/index` 或旧直接目录 |
| [run_backtest.py](run_backtest.py)：`idx_paths = inventory(...)`、每日 `needed[(day, args.index_code)]` | 索引目标指数文件，缺少任一回测日指数文件会抛异常 |
| [run_backtest.py](run_backtest.py)：`prepare_option_chain(...)`；[scenarios/option_chain.py](../../bomber/framework/dataprep/scenarios/option_chain.py)：`prepare_option_chain` | 把指数作为 `asset_kind="index"` 加入输入计划，读取文件并校验时间和 OHLCV |
| [run_backtest.py](run_backtest.py)：`index_id` 注册及 `add_prepared_bar_source`；[dataprep/bars.py](../../bomber/framework/dataprep/bars.py)：`add_prepared_bar_source` | 注册指数行情元信息；将已准备数据用 `PreparedFrameReader + FixedInstrumentBarParser` 加入回放，不再读取原文件或重复移动标签 |
| [assembly.py](assembly.py)：`all_ids`、`DataBinding`；[strategy.py](strategy.py)：`on_bar` | 绑定包括指数在内的所有行情，回放送入策略缓存并参与分钟决策 |

因此，不显式传 `--index-dir` 也能运行，表示已有配置解析出了有效目录、目录中已有指数文件；不能据此推定没有指数输入。该入口不会自动下载指数、查询 DolphinDB 补数据、生成假指数或用 IM 价格替代指数。

2026-10-12 用户返回的 9 月 1—9 日回测成功日志已通过文件校验、每日选约三腿同步、最终结果检查，说明该运行环境具备所需指数输入。**控制台未展示实际目录，当前尚不能仅凭日志确定是哪个环境变量提供了它。** 已保存的输入报告可直接回答这个问题，比读取当前可能已经变化的 `.env` 更准确。

在运行机 `pro` 目录执行以下只读命令，读取本次成功报告中的路径和来源，不输出账户凭据：

```bash
python - <<'PYCODE'
import json
from pathlib import Path
report = Path("demos/08_option_vega/results/70d4ca7d-e457-4f78-a7f2-0b2a4f7d7bed")
manifest = json.loads((report / "input_manifest.json").read_text())
coverage = json.loads((report / "input_coverage.json").read_text())
for entry in manifest.get("paths", []):
    print("指数目录:", entry.get("resolved", {}).get("index"))
    print("路径来源:", entry.get("sources", {}).get("index"))
for filename, info in coverage.get("files", {}).items():
    if "asset_kind='index'" in info.get("identity", ""):
        print("指数文件:", filename, "行数:", info.get("rows"))
PYCODE
```

来源为 `env:INDEX_KLINE_DATA_DIR` 表示专用环境变量；`derived` 表示由行情父目录或根目录推导；`legacy_kline` 表示旧目录兼容解析。这些环境值可能由已有进程环境或 `.env` 提供，manifest 的 `env:` 本身不区分两者。`input_coverage.json` 另记录每个文件的行数、首末纳秒时间与标签，`input_manifest.json` 记录实际读取文件的大小与修改时间。

### 回测完成条件与模拟边界

每天选约分钟至少要有一组认购、对应期货、指数同步行情；缺日数据会报错，不自动截短请求区间。最后实际回测日的指数最大时间须不早于 `flatten-slot + 2分钟`，持仓腿还需有退出报价及后续撮合事件。报告中 `remaining_positions_or_working` 应为空，`report_errors` 应为空；未触发最终清仓或有残留则失败。`--require-fills` 额外要求至少一笔模拟成交。

模拟使用 NETTING、Bar 撮合、每手固定手续费和权利金比例近似期权保证金；订单最早在后续行情事件撮合，组合多腿不保证原子成交。没有完整期权卖方保证金、行权和到期结算模型；实际成交、资金和风险应结合这些范围解释。

### 排查“持仓计算缺少新鲜行情”

2026-10-12 返回的 9 月 1—11 日回测日志在 `2026-09-10 09:37` 停止：IM2609 实际多头 2 手、目标 2 手、在途 0，策略最新报价为 `2026-09-09 15:01`，年龄 66960 秒。隔夜恢复已等待超过默认 300 秒，不能继续按昨日价格计算持仓。日志里的 IM2611 未上市、IM2706 缺基础条款是候选排除记录，当前异常指出的合约是 IM2609。

文件加载和每日 13:58 同步检查通过，只证明当天文件及选约时点有覆盖，不证明开盘已有持仓腿的新报价。先在同一个运行环境、相同 `.env` 和路径参数下执行只读诊断，无需再次启动引擎：

```bash
python demos/08_option_vega/check_backtest_data.py \
  --day 2026-09-10 --symbol IM2609 --at 09:37 --bar-timestamp end
```

若原回测提供了 `--data-root / --fut-dir / --index-dir`，检查命令也传相同值。它复用公共路径解析、文件索引、分钟标签转换及 OHLCV 校验，输出实际路径、原始/完成分钟范围、决策前最新价格时间、后续首条和附近样例；零成交量行保留。检查范围为原文件覆盖，不能独立证明回放送达。

| 诊断结果 | 处理方向 |
| --- | --- |
| `FILE_MISSING / DIRECTORY_MISSING` | 核对实际解析目录、文件名与日期；补齐持仓合约的真实输入 |
| `INPUT_ERROR` | 按错误核对时间列、日期身份、重复分钟及 OHLCV |
| `NO_BAR_BY_DECISION` | 当天文件存在但决策前没有完成分钟，检查采集/导出覆盖和原始标签 |
| `STALE_AT_DECISION` | 当天最近分钟已超过允许年龄，核对缺口；恢复还要求首条行情属于当前场次 |
| `FRESH_AT_DECISION` | 文件中有新鲜行；进一步对照原失败报告的路径/文件版本和回放绑定、送达记录 |

诊断返回非零表示所查分钟存在缺失、过期或输入错误，JSON 仍会输出。源分钟标签必须按数据实际含义设置；`start/end` 的一分钟移动不能填补缺失的开盘行情。提高年龄上限不能恢复缺失数据，且可能使昨日价格继续参与风险计算。保留失败目录里的 `input_manifest.json / input_coverage.json / input_issues.json`，用于核对实际文件来源与版本；该 9 月 1—11 日区间仍未通过；后续 9 月 1—9 日成功结果见第 7 节，缩短区间没有修复 9 月 10 日缺报价问题。

## 3. Live 运行方式

### 3.1 准备来源与环境

行情、指数、参考资料和执行模式分别配置，来源不会隐式回退：

| 输入 | 可选来源 | 运行要求 |
| --- | --- | --- |
| 期权及同月期货 | `--market-source ctp`（默认） | CTP 原始深度聚合一分钟 Bar；`last` 使用成交，`mid` 使用盘口中间价 |
| 期权及同月期货 | `--market-source dolphindb` | 显式提供已收盘期权/期货标准分钟流表 |
| 指数 | `--index-source dolphindb`（默认） | 独立标准分钟流表，只有行情绑定，无交易路由 |
| 指数 | `--index-source dolphindb-dfs` | 只读查询配置声明的指数分钟原表，默认 `dfs://min_bar/index_1min` |
| 参考资料 | `--reference-source dolphindb`（默认） | 读取期权、期货条款快照；按估值日检查有效性 |
| 参考资料 | `--reference-source file` | 仅显式测试：同时提供 `--allow-file-reference-test --opt-basic ... --fut-basic ...` |
| 日历 | `--calendar-id china.cffex`（默认） | 公共编译日历；Live 不接受 `--calendar` 文件参数 |

项目 `.env` 或运行环境需要以下配置；不要把密码放进命令或报告：

| 环境变量 | 用途与默认值 |
| --- | --- |
| `DDB_HOST / DDB_USERNAME / DDB_PASSWORD` | DolphinDB 连接必需；指数及默认参考资料共用这些凭据 |
| `DDB_PORT / DDB_STREAMING_PORT` | 连接端口默认 8848；流订阅监听端口默认 0 |
| `DDB_REFERENCE_DATABASE` | 参考资料库默认 `dfs://bomber_daily`；可用 `--reference-database` 覆盖 |
| `DDB_FUT_BASIC_TABLE / DDB_OPT_BASIC_TABLE` | 参考表默认 `fut_basic / opt_basic` |
| `DDB_INDEX_BAR_TABLE` | 标准指数流表，或用 `--index-bar-table`；DFS 模式不使用此变量 |
| `DDB_OPTION_BAR_TABLE` | DolphinDB 期权/期货流表，或用 `--market-bar-table` |
| `CTP_MD_ADDRESS` | `market-source=ctp` 必需，按 realtime / replay 选择实际核验的行情前置 |
| `CTP_ACCOUNT_ID / CTP_PASSWORD` | CTP MD 或原生 TD 登录必需 |
| `CTP_BROKER_ID` | MD 默认 9999；SimNow TD 必须显式配置为 9999 |
| `CTP_TD_ADDRESS` | 仅原生 SimNow 执行必需 |
| `CTP_APP_ID / CTP_AUTH_CODE` | TD 认证配置，按实际账户填写 |
| `CTP_PRODUCTION_MODE` | 默认 true，第二套看穿式前置要求 true；这是连接配置，不代表真实资金生产授权 |
| `CTP_MD_FLOW_PATH / CTP_TD_FLOW_PATH` | 可选覆盖公共客户端 flow 目录；默认使用带进程 ID 的 `/tmp` 目录 |

DolphinDB 标准流遵循 `DolphinDbStreamSpec.bar()` 的字段和顺序：

```text
minTime,date,Code,open,high,low,close,prev_close,prev_settlement,
volume,turnover,pct_change,open_interest,prev_open_interest,updateTime
```

DFS 指数原表由[特征配置](../../bomber/framework/dataprep/features/configs/features.json)声明，字段为 `minTime/date/Code/open/high/low/close/prev_close/volume/turnover/pct_change/updateTime`，无需冒充标准流表。查询验证真实类型，按上海时区转换 UTC 纳秒；价格/数量无法按配置精度无损表达时失败。公共查询、只读探针及导出方法见[特征说明](../../bomber/framework/dataprep/features/README.md)。

### 3.2 实时 Recording：直接运行

以下采用 CTP 成交分钟 + DFS 指数；先将 `VEGA_DAY` 设置为已核验的当天 MD 交易日，并确保 `.env` 中 MD 地址为实时前置。示例日期为 20261012，运行时替换。所有时点均为上海时间，需在选约分钟前预留连接、首根完成 Bar 和就绪时间。

```bash
export VEGA_DAY=20261012

# 同一终端保存公共参数；后续 Model / SimNow 命令复用。
VEGA_LIVE_ARGS=(
  --connect --market-source ctp --ctp-price-type last
  --simnow-environment realtime --expected-trading-day "$VEGA_DAY"
  --valuation-day "$VEGA_DAY" --calendar-id china.cffex
  --reference-source dolphindb
  --option-product MO --future-product IM --index-code 000852
  --index-source dolphindb-dfs
  --index-database dfs://min_bar --index-table index_1min
  --index-bar-timestamp end --index-availability observed
  --index-price-precision 4 --index-size-precision 4
  --index-poll-seconds 0.2 --index-query-timeout 2
  --option-slot 13:58 --delta-targets 0.25
  --frame-wait-seconds 20 --instrument-max-age-seconds 30
  --ready-timeout 180 --seconds 900
  --max-option-lots 1 --max-total-option-lots 2 --max-future-lots 1
  --max-notional 2000000
)

python -u -m demos.08_option_vega.run_live \
  "${VEGA_LIVE_ARGS[@]}" --mode recording \
  --report-dir demos/08_option_vega/results/live-recording
```

数组语法适用于 Bash/Zsh。在 13:53 左右启动该 900 秒示例可覆盖默认 13:58 选约；若提前很久启动，应增大 `--seconds`。此直接运行示例采用已验证样本的 20 秒帧等待，不修改入口默认值；默认 5 秒等待的自动验收启动入口见 3.3。小手数示例显式用单档 `--delta-targets 0.25`：默认四档可能选到多个不同合约，总手数上限 2 导致比例缩小后全部向下取整为零。单档有利于验收目标链，但仍需满足行情和选约条件；不改变策略默认四档。

Recording 不接受 `--state-file / --resume / --require-fills / --enable-orders / --confirm-simnow`。它没有成交，无法证明实际成交后 Delta 对冲。

### 3.3 实时 Recording：自动运行与核验

自动入口固定 MO / IM / 000852、CTP LAST、DFS 指数、实时环境、默认四档 Delta，继承 `.env` 的实际前置配置；它不复用上面的数组，也不连接 TD。默认应在 13:53 前启动：

```bash
python -u -m scripts.integration.options.recording \
  --trading-day "$VEGA_DAY" --seconds 4200 \
  --report-dir demos/08_option_vega/results/recording-acceptance
```

必须在目标交易日、选约时点前至少 `ready-timeout + 65秒` 启动，时长要覆盖选约分钟及等待时间。自动入口另支持 `--option-slot`（默认 13:58）、`--ready-timeout`（默认 180 秒）、`--summary` 和 `--allow-no-target`。自定义选约时点须早于策略默认清仓时点 14:50。

它从唯一子进程 `summary.json` 检查实际 MD 日、编译日历、指数查询、同分钟接收期限和实际非零 Recording 请求，写出 `recording-acceptance.json`。运行报告 `status=passed` 只说明运行器验收通过，不自动代表非零目标链通过；默认四档取整为零时目标链核验会失败。

仅复核已有报告，无网络连接：

```bash
python -m scripts.integration.options.recording \
  --summary /path/to/summary.json --trading-day "$VEGA_DAY"

# 缺少非零目标时，只检查决策评价；验收范围为 decision_only。
python -m scripts.integration.options.recording \
  --summary /path/to/summary.json --trading-day "$VEGA_DAY" --allow-no-target
```

自定义选约时点的报告需传相同 `--option-slot`。自动核验排除 Replay 工程映射，不用工程结果登记实时通过。

### 3.4 Model：在线行情驱动模型成交

在同一终端复用 3.2 的参数，每次新会话使用尚不存在的独立状态文件：

```bash
VEGA_MODEL_STATE="demos/08_option_vega/results/model-vega-$(date +%Y%m%d-%H%M%S).json"
python -u -m demos.08_option_vega.run_live \
  "${VEGA_LIVE_ARGS[@]}" --mode model \
  --strategy-position-source model --account-reconciliation off \
  --state-file "$VEGA_MODEL_STATE" --require-fills \
  --report-dir demos/08_option_vega/results/live-model
```

Model 按公共执行参考价模拟成交，维护独立策略账本，不查询物理账户，也不接受报单授权开关。`--require-fills` 要求本次出现策略归属成交；未覆盖选约时点、配仓为零或缺少行情会导致成交验收失败。模型成交不证明盘口可成交性。

### 3.5 SimNow：原生模拟柜台执行

配置实际 TD 前置及账户，并核验 TD 交易日。实时示例中 `VEGA_DAY` 须与 TD / MD 日一致；默认 strict 新会话要求本次管理合约范围内没有待接管仓位。已经存在的本策略仓位使用原文件恢复，不能用新文件绕过归属检查。

2026-10-13 的 15 分钟实时 Recording 非零目标链通过后，可进入 SimNow 原生执行阶段。先在服务器 `.env` 配置真实的 SimNow `CTP_TD_ADDRESS / CTP_BROKER_ID=9999 / CTP_ACCOUNT_ID / CTP_PASSWORD` 及需要的认证字段，再执行现成 TD 只读探针；MD 连接成功不代表 TD 已就绪：

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

它登录、确认结算并查询净仓、资金与活动订单，不报单。核对 trading_day 与本次 MD 日一致、active_orders=0，并查看 MO / IM 持仓；净仓为零不排除同合约同时存在多空总仓，08 的 strict 启动还会检查权威总仓与可用资金。此前 Replay SimNow 留下的 MO2610-C-7300 空 1 手若仍在账户中，不能当成空仓新会话；应先核对原状态的归属与兼容性。检查点绑定估值日、TD 日、参考资料、配置与 realtime/replay 来源身份，旧工程映射状态不能直接转换为新的实时会话，也不能通过改 JSON 或关闭 strict 绕过。

2026-10-13 用户返回 TD 只读结果：前置 `tcp://182.254.243.31:30001`、BrokerID=9999，登录及结算确认成功，trading_day=20261013、active_orders=0，9 条净仓记录中没有 MO / IM；范围外非零净仓为 AG -1、SC +1、NI +1、I -1。这支持进入 08 新会话的总仓预检，不等同于已核验所有今昨多空分桶或原生报单成交。08 设置 `scope_to_reference_instruments=True`，因此 strict 对本次参考合约范围检查总仓并对账，范围外仓位记录到 external_gross，不自动接管。输出中的 lshw/dmidecode 缺失没有阻止本次登录和查询，不登记为 08 策略错误。

以下新会话命令仅适用于管理合约总仓为空且没有活动订单的账户。选约时点必须重新设置为尚未到达的交易时点；保留 20 秒帧等待，单档 Delta、每合约 1 手、总期权 2 手、期货 1 手以及最多 4 次柜台报单。若使用已有策略仓位，先确认原状态兼容及柜台一致，再按第 5 节恢复流程执行。

```bash
VEGA_CTP_STATE="demos/08_option_vega/results/ctp-vega-$(date +%Y%m%d-%H%M%S).json"
python -u -m demos.08_option_vega.run_live \
  "${VEGA_LIVE_ARGS[@]}" --mode simnow \
  --enable-orders --confirm-simnow \
  --strategy-position-source fills --account-reconciliation strict \
  --state-file "$VEGA_CTP_STATE" --require-fills \
  --limit-offset-ticks 1 --max-session-orders 4 \
  --report-dir demos/08_option_vega/results/live-simnow
```

该命令会向 SimNow 模拟柜台报单。`--enable-orders` 和 `--confirm-simnow` 缺一不可；仅授权此受控 SimNow 入口。示例把单会话报单上限设为 4，入口默认 20。到达上限会进入停机撤单和保存流程，不自动平仓。

限价以完成分钟执行参考价及 `--limit-offset-ticks` 计算，不以实时买一/卖一重新定价。`order_submission_audit` 和 INFO“CTP报单诊断”记录限价、分钟参考价、已观测盘口、年龄和是否跨价；盘口过期或缺失时跨价为空。提交调用返回不代表柜台接受，柜台接受或观测跨价也不保证成交；成交以实际归属回报为准。CFFEX 使用通用 CLOSE。

### 3.6 替换为 DolphinDB 标准分钟流

2026-10-14 按同连接原始行情对比结果，新增并补齐推荐的直接分钟推送路径：期权与同月期货从 DolphinDB 接收已完成的一分钟 Bar，指数可继续 DFS 查询，原生 SimNow 执行仍使用 CTP TD。原 CTP Tick 聚合路径保留为对照，入口默认仍为 ctp；启用下面的显式来源参数即可切换，不打开 CTP MD，也不再订阅全链 QuoteTick/TradeTick 或本地合成市场分钟。截图 `dfs://min_bar/cffex_1min` 是持久化查询表，不是共享推送表名称。

先在行情所在 DolphinDB 数据节点的查询窗口逐条执行这些只读语句：

```dolphindb
getStreamTables()
getStreamingStat().pubTables
```

前者查看共享流表清单，后者查看发布/订阅状态；读取 DFS 的节点不一定是流表实际发布节点。找到候选名称后，用实际名称替换下面字符串，再检查结构及最新数据：

```dolphindb
schema(objByName("实际共享流表名", true))
t = objByName("实际共享流表名", true)
select top 10 * from t
where Code in ["MO2611-C-7400", "IM2611"]
order by date desc, minTime desc
```

用户随后返回 `getStreamingStat().pubTables` 截图，确认该节点存在 `stream_cffex_1min`（动作 save_cffex_1min_to_dfs）和 `stream_index_1min`（动作 save_index_1min_to_dfs）。市场使用前者，指数推送使用后者；`stream_ctp_cffex`、`stream_ctp_cffex_std` 是原始/标准化逐笔链，不替代一分钟流。actions 是服务器已有的处理任务，客户端使用独立订阅任务名，不复用或取消这些任务。发布状态截图没有提供字段结构、最新业务日及 minTime/volume 语义，因此仍须验证实时新行。

只核对结构时可直接执行 `schema(objByName("stream_cffex_1min", true))`、`schema(objByName("stream_index_1min", true))`。也可把 [OPTION_VEGA_DOLPHIN_STREAM_PROBE.py](../../doc/evidence/commands/OPTION_VEGA_DOLPHIN_STREAM_PROBE.py) 同步到运行机同名目录，在项目根运行：

```bash
python -u doc/evidence/commands/OPTION_VEGA_DOLPHIN_STREAM_PROBE.py --seconds 180
```

探针复用实际 DolphinDB 原生适配，订阅两张已确认流表，offset=-1，结构在订阅前按服务器列名验证；不依赖 minTime 标签猜测，不转换标准 Bar、不启动策略、不连接 CTP，也不读取或改写策略状态。输出 `DDB_STREAM_SCHEMA`、`DDB_FIRST_ROW`、`DDB_BAR_ROW` 和 `DDB_STREAM_PROBE_FINAL`，保存到 `tests/results/option-vega-dolphin-stream-probe/sample-<ns>/summary.json`。监控 MO2611-C-7400、IM2611、000852，每行保留原始字段、Python 类型及实际上海接收时间；每键最多保留最近 20 行，同时统计该表全部接收行。180 秒采样不含连接准备时间，准备期间已收到的行也保留；Ctrl+C 保存已有记录并释放订阅。缺列会在订阅前失败，监控键未收到时退出码为 1；completed 只表示采样结束，不证明发布的是完整分钟或已经通过策略验收。本地真实适配 + 假 SDK 的两流三键/缺列/清理检查通过，首次线上失败与后续修正见下段。

用户第一次运行在约 11:07 返回 failed：市场流实际列为既有 15 列，但 SDK 异步报 `actionName` 不允许连字符；指数结构检查又发现缺少 date/minTime，三键没有收到。列名查询完成不等于订阅成功。这次暴露了客户端任务名生成的遗漏和原始探针对指数时间列的过度假设，精简记录见 [任务名/结构失败及修正](../../doc/evidence/live/OPTION_VEGA_DOLPHIN_ACTION_FIX_20261014.json)。

REV3 将正式入口和原始探针任务名统一生成为字母/数字/下划线，并在流配置及原生订阅对象构造时拒绝非法字符，避免 SDK 自动重订阅反复报错。原始探针迁入 `scripts.integration.options.dolphin_stream_probe`，旧 doc 命令仍作为兼容入口；当前版本启动打印 `DDB_STREAM_PROBE_VERSION: 3`。市场检查仍要求八列；原始指数探针只要求 Code/open/high/low/close/volume，并完整输出其已有时间列，不猜列名、不补造 date/minTime。这项放宽仅用于读原始行，正式标准 Bar 输入及策略校验仍要求明确时间结构；指数生产路径继续使用已验证的 DFS。

更新 REV3 源码后可重新测试两流，或先只测已确认结构的市场流：

```bash
python -u -m scripts.integration.options.dolphin_stream_probe --streams both --seconds 180
python -u -m scripts.integration.options.dolphin_stream_probe --streams market --seconds 180
```

上述两条是不同采样范围，任选一条，不要同时运行。market 范围不查询/订阅指数流，报告不把未选择的 000852 当成缺失。both 范围用于补齐指数实际 schema 和原始行。REV3 本地 76 项主回归、111 项相关回归共 187 项通过，包括服务器任务名约束及非 date/minTime 的指数夹具；原生 Driver 离线探针、77 文件部署摘要预检及独立 entry 组通过。该版交付时尚无修正版真实推送结果，随后接收结果见下文。

随后用户返回修正版 market/120 秒结果：11:27:11.704 开始，最终 completed、failure=null、cleanup_errors=[]、missing_monitored_keys=[]，报告为运行机 `tests/results/option-vega-dolphin-stream-probe/sample-1791948431148948591/summary.json`。11:28 和 11:29 分别实际收到 IM2611 与 MO2611-C-7400 两条监控行情，日期均为 2026-10-14，没有此前任务名错误。本次只测市场，不登记指数流或正式 Recording 通过，见 [原始市场推送接收证据](../../doc/evidence/live/OPTION_VEGA_DOLPHIN_MARKET_RECEIPT_20261014.json)。

| 业务分钟 | IM 接收时间 / 累计 volume | MO 接收时间 / 累计 volume |
| --- | --- | --- |
| 11:28 | 11:28:01.272 / 19420 | 11:28:01.284 / 1607 |
| 11:29 | 11:29:00.862 / 19467 | 11:29:02.991 / 1608 |

该样本支持 `cumulative`：IM 增量 47，MO 增量 1；MO turnover 增量 7200，恰好为 72 元 × 100 乘数 × 1 手。minTime 在 SDK 中为 `datetime64[ms]`、显示 `1970-01-01T11:28:00.000`，日期列为 `datetime64[D]` 的 2026-10-14；转换器取前者时分秒与后者日期拼接，不使用 1970 作为估值日。新增真实 NumPy 类型样本回归已验证日期、OHLC、实际接收时间及 47/1 的量差分，77 项主回归通过；未改变运行模块。源码/入口摘要需按后续补充交付同步测试版本。

分钟标记后约 1—3 秒的接收时序与结束标签一致，本轮 Recording 工程验收采用 `end`；该标签是基于样本的推断，发布程序完整分钟定义尚未独立核对。当前宜用已确认市场推送 + 原 DFS 指数验证目标链，午间不启动持续运行测试。10 月 14 日下午同步 REV5 后，可自动选择北京时间约六分钟后的业务分钟，运行 900 秒；开始时应距收盘至少 15 分钟：

```bash
SLOT=$(python -c 'from datetime import datetime,timedelta; from zoneinfo import ZoneInfo; print((datetime.now(ZoneInfo("Asia/Shanghai"))+timedelta(minutes=6)).strftime("%H:%M"))')
bash doc/evidence/commands/OPTION_VEGA_DOLPHIN_RECORDING.sh \
  20261014 "$SLOT" stream_cffex_1min end cumulative
```

若启动已晚于该时点，应另选能留出 ready-timeout+65 秒准备余量且被 900 秒覆盖的未来业务分钟。该 Recording 不连接 CTP MD/TD、不报单、不恢复旧仓位；原市场与指数都必须送达选约同一分钟，源时效及 20 秒接收期限仍校验。返回市场 stream 转换诊断、OptionMarketInputs、entry_audit、frame_audit 与停机结果，再由报告复核确认标准 Bar/目标链；原始推送成功不能替代这些检查。

这些语句不创建或改写共享表。应确认表持续接收当日这两类合约的已完成分钟，并确认 minTime 是起点还是终点、volume 是分钟增量还是日累计量；字段名相同不能证明业务含义相同。若没有分钟共享流表，需要先由数据发布程序提供对应流；客户端不能把 `loadTable(...)` 的查询结果当成实时推送，也不自动创建表、改聚合引擎或定期查询市场 DFS 来冒充推送。推送模型及流表发现依据见 [DolphinDB 流订阅](https://docs.dolphindb.com/zh/api/python/BasicOperations/Subscription/Subscription.html) 与 [流计算状态监控](https://docs.dolphindb.com/zh/stream/str_monitor.html)。

在 3.2 数组中替换对应来源参数，并用实际已核验表名与业务语义：

```text
--market-source dolphindb --market-bar-table stream_cffex_1min
--market-bar-timestamp end --market-volume-mode cumulative
--market-bar-max-age-seconds 120
--index-source dolphindb-dfs --index-bar-timestamp end --index-availability observed
```

市场与指数来源独立；本轮第一阶段推荐指数继续使用已验证的 `--index-source dolphindb-dfs --index-bar-timestamp end --index-availability observed`，无须同时更换指数。若也改指数流，再单独确认其共享表与标签。市场流没有默认表名，使用 `--market-bar-table` 或 `DDB_OPTION_BAR_TABLE`；minTime/volume 两项参数在分钟推送模式必需，禁止猜测。客户端在订阅前通过 `schema(objByName(...,true)).colDefs.name` 获取实际列顺序，要求 minTime/date/Code/open/high/low/close/volume 八列；其他列可有可无，包括 updateTime。原固定 15 列的公共模式仍兼容。当前按表订阅并在客户端保留当日参考绑定的认购和同月期货，没有声称已在发布端按 Code 过滤。

转换保留源 minTime，帧层仅在标签为 start 时加一分钟得到已收盘业务分钟；不会改写成当前时间或前填价格。推送提供的零成交 OHLC 分钟会正常接入，客户端不自行补造。`per-bar` 保留分钟 volume；`cumulative` 按合约、Bar 类型、业务日差分，日内首份只建立基线、输出量 0，之后累计量不变的分钟为 0；同日累计量下降或新分钟时间倒退报错。首份缺少此前基线时不能精确还原该分钟量，模型撮合不把历史累计量当成当分钟可成交量；08 选约估值主要使用价格。重复业务分钟不再次发送，已消费分钟不因后续修订重新决策。

Bar 的 ts_init 使用客户端实际接收时间，updateTime 不冒充本机到达；分钟流健康检查读取每次有效转换更新的源事件/接收时间，避免 READY 快照时间冻结造成持续推送仍超时。`--market-bar-max-age-seconds` 默认 120，须大于 60 且不超过策略 `--max-market-age-seconds`；它控制市场分钟流和必需市场腿的完成分钟及接收年龄，并非原 CTP 原始深度 30 秒阈值。起点标签的源事件年龄也按完成分钟计算。缺持仓/在途/目标腿或流停止仍会关闭会话，错误 OHLC/累计量不静默丢弃，而记录 conversion_errors 并关闭流健康。帧等待仍推荐 20 秒，超过期限不追溯决策。指数保留其独立的查询与时效政策。

确认流表及语义后，先使用 [OPTION_VEGA_DOLPHIN_RECORDING.sh](../../doc/evidence/commands/OPTION_VEGA_DOLPHIN_RECORDING.sh)，参数顺序为交易日、未来选约时点、实际共享表名、start/end、cumulative/per-bar；运行 900 秒，只接行情和记录目标，不接 TD、不报单。凭据从项目 `.env` 读取，不写入命令。报告检查 `market_sources.market`、`market_streams`（实际列顺序/接收 Bar 数/最后时间/转换错误）、frame_audit 和 entry_audit。不要直接复制占位表名，也不要使用已过去的选约时点。

取得实际报告后，可用 `python -u -m scripts.integration.options.recording --trading-day YYYYMMDD --option-slot HH:MM --summary <实际summary.json路径>` 只读复核。该入口接纳 CTP 市场或 DolphinDB 分钟市场 + DFS 指数；推送路径要求真实结构/接收数、无转换错误和明确标签/量语义，不要求或伪造 CTP MD 日。start 标签的市场源时间须为选约完成分钟前一分钟，指数仍为同一完成分钟；缺目标、迟到、错误来源及混入物理账户仍拒绝。

切换行情来源会改变持久化 sources/labels/volume_mode 身份，旧 CTP 会话文件不能直接用此路径 `--resume`。昨日真实 MO 空 1 手的原文件及柜台仓位须保留，Recording 不接管或改变它；后续真实交易恢复需另行处理跨日及来源迁移，不能新建空状态绕过已有持仓。分钟输入没有实时买一/卖一，原生限价仍按分钟参考价及跳价计算，order_submission_audit 的 quote/crosses_observed_quote 可以为空，不能把 Bar 接入或目标记录当成实际成交。

当前本地 81 项 SDK 实体替身下的实际公共组件回归及 111 项相关回归通过，包含 16 项分钟推送专项和 11 项报告复核。实际市场接收证据已返回，累计量以连续分钟样本核对，end 标签仍由到达时序推断；标准 Bar/完整选约目标和交易验收须分别核对。本地没有连接运行机或读取远端源码。

**2026-10-14 13:22 Recording 失败及 REV5 修正**：用户报告 [失败证据](../../doc/evidence/live/OPTION_VEGA_DOLPHIN_OLD_MINUTE_20261014.json) 显示市场分钟流已转换 90 根 Bar，conversion_errors=0；13:22 指数于 13:22:14.306 成功投影。13:22:18.819 待处理集合又包含 13:21，查询 Feed 尝试发布旧指数，Runner 的单合约倒退检查关闭会话。frames=0、entry_audit=[]、orders_submitted=0，不能登记为完整 Recording 通过。异常发生在投影后的发布回调，旧日志“指数DFS查询失败”不能据此证明数据库网络或查询失败。

不同市场合约可交错送达业务分钟，使待处理集合跨轮询新增旧分钟；原 Feed 已在每次 poll 内排序，但无法保证多次 poll 之间仍向前。REV5 的公共 `FeatureQueryBarFeed` 记录 `latest_projected_ns`：晚于或等于既有流进度的未投影分钟按原精确查询处理；早于已投影分钟的新请求记为 `OLDER_THAN_PROJECTED`，不再发布，不刷新指数行情观测。查询缓存仍保留历史资料，旧帧缺指数则按原接收期限记 `SKIPPED_INDEX`；不会将较新指数放入旧帧，也不会补做旧选约。没有关闭 Runner 的倒退检查、未来时间/年龄检查或帧期限检查。

summary 的 `feature_queries[].minute_lookups` 会记录该状态及 `latest_projected_ns`，`OptionMarketInputs` 同时输出“指数分钟不可用”。真正查询或投影异常改为“指数DFS查询或投影失败”，仍传播并停机。新增回归覆盖跨轮询旧请求、旧指数后补、同轮询排序，以及实际 Runner/帧/Recording：旧帧跳过后新帧仍生成 TARGET，直接倒退发布仍拒绝。同步 [REV5 包](../../doc/evidence/archives/OPTION_VEGA_DOLPHIN_PUSH_20261014_REV5.tar.gz) 和配套摘要/验收入口后，选择留有启动余量的未来分钟运行原 Recording 命令；无须调整帧等待来掩盖本次异常。后续实际复测及完整目标链复核已通过，结果如下。

**2026-10-14 13:40 Recording 复测返回**：报告目录为 `tests/results/gap08-dolphin-recording/option-vega-recording-1791956075214033439`。用户提供 events.txt 请求和 summary 尾部，status=passed、session_readiness.ready=true、issues=[]、cleanup_errors=[]。市场流实际转换 1533 根 Bar、conversion_errors=0；指数 13:34—13:49 共 16 个业务分钟均为 PROJECTED，skipped_index_frames=0。13:35—13:49 连续分钟的指数接收延迟为 14.091—14.397 秒；启动时的 13:34 缓存样本在分钟后 35.971 秒首次观测，不属于稳态延迟统计。

实际 Recording 请求 revision=1，signal_ts 为 13:40:00、请求时刻为 13:40:20.612690，唯一非零目标为 `MO2611-C-7300.CFFEX = -1`。请求里的 107 个合约构成完整目标快照，其余 106 个目标为 0，不能解释成 107 笔柜台订单。尾部 orders_submitted=0、execution_origin=recording、observed_td_trading_day=null，记录目标没有建立实际空仓或更改昨日柜台持仓。本次正常完成，没有因指数时间倒退停机；提供的 minute_lookups 全是 PROJECTED，没有线上 OLDER_THAN_PROJECTED 或超时样本，因此对应拒绝分支仍由离线用例验证。

最初附件只包含请求和 summary 尾部，接收及非零目标记录于 [初次复测证据](../../doc/evidence/live/OPTION_VEGA_DOLPHIN_RECORDING_RECEIPT_20261014.json)。用户随后在运行机执行以下既有只读入口，返回 [完整目标链验收通过证据](../../doc/evidence/live/OPTION_VEGA_DOLPHIN_RECORDING_ACCEPTANCE_20261014.json)。该命令只读原报告并写旁边的 recording-acceptance.json，不启动行情或交易连接：

```bash
python -u -m scripts.integration.options.recording \
  --trading-day 20261014 --option-slot 13:40 \
  --summary tests/results/gap08-dolphin-recording/option-vega-recording-1791956075214033439/summary.json
```

实际复核返回 status=passed、scope=target_chain、errors=[]；target_chain_verified、market_stream_verified、index_query_verified、index_bar_delivery_verified 均为 true，runtime_failure=null。frames=16、skipped_index_frames=0；13:40 决策 outcome=TARGET、candidate_count=49，目标 MO2611-C-7300 空 1 手、future_bindings 明确为 IM2611。公共复核函数已检查选约帧实际消费、指数/目标腿/绑定期货同分钟来源、接收不超过截止时刻、请求不早于期限，以及 Recording 请求目标与决策一致。因此登记市场分钟推送 + DFS 指数 + Recording 完整目标链通过。

验收报告位于原目录的 `recording-acceptance.json`。worker_exit_code=null 是 --summary 模式未启动子进程的正常值，不表示运行失败；这次由运行机读取原报告后返回结果，本机没有独立连接或重新读取远端文件。timeout_seen_online=false，线上超时及旧请求跳过分支仍只有离线样本覆盖。此结论限于 Recording，不登记 DolphinDB 市场下的 SimNow 报单、成交、持仓恢复/迁移或持续运行到收盘通过。当前只更新文档与证据，运行代码保持 REV5，无须为本次证据更新重新部署运行模块。

**继续 SimNow 验证：先做原仓只读预检**。用户已要求继续验证，下一步沿用昨日 MO2611-C-7400 空 1 手的原状态；不能将今日 Recording 的 MO2611-C-7300 目标当成已有真实持仓。原状态固定身份为 20261013 / CTP 市场，当前为 20261014 / DolphinDB 市场，现有恢复同时要求会话身份完全一致和账本同交易日；直接改日期/来源 --resume 会拒绝，新建空状态在 strict 政策下也会因已有 MO 总仓拒绝。先核对原文件与当前柜台，再准备保留归属的独立迁移方案。

新增 [simnow_preflight.py](../../scripts/integration/options/simnow_preflight.py) 是临时验收入口，不是策略运行必需模块。它使用公共 JsonStateStore 校验原文件、提取持仓/目标/来源参数，使用既有禁单 TD 探针查询当前总仓、权威今昨仓/持仓成本、资金和活动订单。持有与正式会话相同的账户锁和原状态锁，检查原账户身份、声明 TD 日、旧状态无在途/活动订单、当前无活动订单、有可用 CNY、今昨仓与总仓一致，且当前 MO/IM 总仓与原归属账本一致；外部 AG/CU 等总仓单独报告，不能归给 08。净仓相同而双向总仓不同仍拒绝。

```bash
python -u -m scripts.integration.options.simnow_preflight \
  --connect --expected-trading-day 20261014 --timeout 30 \
  --state-file tests/results/gap08-realtime-simnow/state-1791869411336135873.json
```

结果打印 OPTION_VEGA_SIMNOW_PREFLIGHT，并保存至 tests/results/option-vega-simnow-preflight/sample-<ns>/summary.json。completed 只表示这轮只读核对完成，can_start_orders 固定 false；migration_required 表示旧日期或来源仍需迁移，不是开闸信号。脚本没有报单/撤单调用，也不写新的策略状态；前后比较原文件 SHA256，检测状态变动即失败。若仓位不同、原文件损坏、同账户有运行进程或订单未结束，保留现场并返回原因，不自动平仓、接管或绕过 strict 对账。

本地 9 项无网络专项验证通过，覆盖昨日空 1 手在当前权威昨仓中读回、外部持仓隔离、净仓相同但总仓不符、旧/当前活动订单、错误账户/交易日、分桶冲突、原文件损坏和原字节保留；SDK 实体替身下原 81 项主回归仍通过。真实预检尚待运行机返回；源码和测试见 [只读预检补充包](../../doc/evidence/archives/OPTION_VEGA_SIMNOW_PREFLIGHT_20261014.tar.gz)，不修改既有 REV5 行情或执行模块，不登记跨日迁移/SimNow 新来源成交通过。

### 3.7 Replay 与工程时间映射

**原始时间 Replay**：使用 `--simnow-environment replay --replay-md-trading-day YYYYMMDD`。估值日默认为原始 MD 日，选约和 Black76 按原始事件时间；执行请求使用接收墙钟。只豁免历史事件相对墙钟的年龄，仍检查 MD 日、源日期、接收健康、倒退和参考条款。期权、期货、指数必须属于同一估值日；墙钟到 13:58 不会触发源流其他时刻的选约。

**工程映射**：`--replay-test-start-time 13:55` 将首份完成 CTP 分钟 Bar 映射到估值日 13:55，所有合约共享固定偏移，历史指数按映射后的同分钟查询。可另设 `--replay-test-source-time 16:00`，把源 16:00 对应到目标开始时刻。保留分钟间隔、OHLCV 和接收时间，不补造成交/指数，源日期或映射估值日跨日会失败。

以下采用历史证据中的 MD / TD 日 20261009，仅作配置示例；运行前应重新核验当前环境返回的日期、前置及历史指数覆盖：

```bash
export VEGA_MD_DAY=20261009
export VEGA_TD_DAY=20261009
# CTP_MD_ADDRESS 设为已核验 Replay 前置，CTP_PRODUCTION_MODE=true。
python -u -m demos.08_option_vega.run_live \
  --connect --mode recording --market-source ctp --ctp-price-type last \
  --simnow-environment replay --replay-md-trading-day "$VEGA_MD_DAY" \
  --expected-trading-day "$VEGA_TD_DAY" --valuation-day "$VEGA_MD_DAY" \
  --replay-test-start-time 13:55 --option-slot 13:58 --delta-targets 0.25 \
  --calendar-id china.cffex --reference-source dolphindb \
  --option-product MO --future-product IM --index-code 000852 \
  --index-source dolphindb-dfs --index-database dfs://min_bar --index-table index_1min \
  --index-bar-timestamp end --index-availability observed \
  --index-poll-seconds 0.2 --index-query-timeout 2 \
  --index-price-precision 4 --index-size-precision 4 \
  --frame-wait-seconds 5 --instrument-max-age-seconds 30 \
  --ready-timeout 180 --seconds 600 \
  --max-option-lots 1 --max-total-option-lots 2 --max-future-lots 1 \
  --max-notional 2000000 \
  --report-dir demos/08_option_vega/results/replay-recording
```

删除 `--replay-test-start-time` 即恢复原始时间路径，运行仍须覆盖源流选约时点。工程映射仅支持 **CTP Replay + DFS 指数 + 新会话**，不能 `--resume`。Model 可替换 mode 并按 3.4 提供新状态文件、`--require-fills`；SimNow 可按 3.5 配置报单授权、独立新状态和实际 TD 日。原生还要求所选历史合约在实际 TD 日仍可交易。

报告标记 `validation_scope=engineering_chain`、`bar_time_basis=replay_test_mapped`，保存映射偏移和原始/映射时间样例。映射价格流与历史指数不是同步原市场行情，只验证接收、聚合、分钟帧、信号、执行与归属链路，不能用来评估历史策略表现或登记实时验收。

## 4. Live 参数说明

`run_live.py` 的默认值与回测独立。除 `--connect`（运行必须显式提供）和 `--expected-trading-day` 外，其他参数按所选模式有条件必需；完整关系见下表。日期使用 `YYYYMMDD`，日内时点使用 `HH:MM`。

### 4.1 策略参数与回测差异

| 参数 | Live 默认值 | 用途 / 与回测差异 |
| --- | --- | --- |
| `--option-product / --future-product / --index-code` | MO / IM / 000852 | 期权/同月期货/独立指数；Live 指数必须为六位数字 |
| `--nav` | 40000000 | 固定配仓规模；Live 无 `--starting-balance`，不代表柜台余额 |
| `--vega-budget` | 1.25 | 每一百分点波动率变化的现金预算为 `nav × b / 10000` |
| `--delta-targets` | 0.2,0.25,0.3,0.35 | 逗号分隔，每档在 (0,1)；重复合约合并，小手数验收可显式用 0.25 |
| `--option-slot` | 13:58 | 每日选约时点，必须早于 `flatten-slot` |
| `--hedge-slots` | 10:00,11:00,13:30,14:00,14:55 | 逗号分隔的对冲复核时点；实际归属持仓变化也触发 |
| `--flatten-slot` | 14:50 | Live 仅在配置清仓日时执行最终组合清仓；回测默认末日清仓 |
| `--flatten-day` | 不设置 | 主动清仓日，须为估值日当天或之后的已知交易日；不设置则停机保留仓位 |
| `--min-remaining-days / --close-remaining-days` | 5 / 2 | 开仓最少剩余交易日 / 清仓阈值；须满足 `0 ≤ close < min` |
| `--rate / --min-time-value` | 0.02 / 5 | Black76 年化利率 / 反解 IV 的最少时间价值，后者非负 |
| `--max-market-age-seconds / --max-iv-age-seconds` | 300 / 300 | 策略真实价格 / 成功 IV 缓存最长年龄，正整数秒 |
| `--max-option-lots` | **1** | 单认购上限，回测默认 **100** |
| `--max-total-option-lots` | **2** | 认购总手数上限，回测默认 **200** |
| `--max-future-lots` | **1** | 单对冲期货上限，回测默认 **100**；超限停止 |
| `--max-notional` | **2000000** | 单订单及单合约持仓名义金额上限，回测默认 **100000000** |
| `--require-fills` | false | Model/SimNow 要求本次归属成交；Recording 禁止；回测要求模拟成交 |
| `--report-dir` | 本策略 `results/` | 报告根目录；状态文件另由 `state-file` 指定 |
| `--log-level` | INFO | DEBUG/INFO/WARNING/ERROR；回测默认 WARNING |

Live 不接受回测的 `--start-day / --end-day / --data-root / --fut-dir / --opt-dir / --index-dir / --calendar / --bar-timestamp / --index-precision / --commission / --margin-init / --margin-maint / --option-margin-init / --option-margin-maint / --no-tearsheet`。Live 的 `--opt-basic / --fut-basic` 仅用于显式文件参考测试；指数使用下面的独立精度及时间标签参数。

### 4.2 模式、交易日、执行和恢复

| 参数 | 默认值 | 含义与约束 |
| --- | --- | --- |
| `--connect` | false | 所有 Live 运行必须显式提供，包括 Recording/Model |
| `--mode` | recording | recording / model / simnow，分别对应记录、模型、原生柜台 |
| `--expected-trading-day` | 必填 | 执行/参考会话交易日；SimNow 与实际 TD 日核对 |
| `--valuation-day` | 自动 | realtime 默认会话日，replay 默认 MD 日；必须与所选行情环境日期一致 |
| `--simnow-environment` | realtime | realtime / replay；用于 CTP 行情时间政策，与执行 mode 独立 |
| `--replay-md-trading-day` | 无 | replay 必需的原始 MD 日，realtime 禁止 |
| `--replay-test-start-time` | 无 | 工程映射目标开始分钟；仅 CTP Replay + DFS 指数新会话 |
| `--replay-test-source-time` | 无 | 显式源分钟，必须同时设置 start-time；省略则以首份完成 Bar 为锚 |
| `--calendar-id` | china.cffex | 编译日历身份、覆盖和指纹写入报告/恢复身份；现维护到 2026 年，超范围拒绝 |
| `--seconds` | 900 | 就绪后运行时长，有限正数秒；应覆盖源流选约时点 |
| `--ready-timeout` | 90 | 启动等待就绪期限，有限正数秒 |
| `--query-timeout` | 30 | 原生 TD 查询/连接超时，有限正数秒 |
| `--limit-offset-ticks` | 1 | 原生限价相对执行参考价的偏移跳数，非负整数 |
| `--max-session-orders` | 20 | 原生单会话报单上限，正整数；达到后停机撤单保存 |
| `--account-reconciliation` | 按模式 | Recording 强制 off；Model 只允许 off；SimNow 默认 strict，可选 observe/off |
| `--strategy-position-source` | 按模式 | Model 默认且必须 model；Recording/SimNow 默认且必须 fills；不把物理总仓当策略仓位 |
| `--enable-orders / --confirm-simnow` | false / false | SimNow 两个都必须显式提供；Recording/Model 禁止 |
| `--state-file` | 无 | Model/SimNow 必需：新会话文件须尚不存在；Recording 禁止 |
| `--resume` | false | 沿用已存在的同一原状态文件；工程映射/Recording 禁止 |

SimNow 的 strict 将柜台仓位与执行账本核对，不一致闭闸；observe 只观测差异；off 不查询柜台仓位。各模式的策略仍只读取自身归属成交和自身未成交委托分配；改变政策不自动接管柜台已有仓位。

### 4.3 行情、参考资料与分钟同步

| 参数 | 默认值 | 含义与约束 |
| --- | --- | --- |
| `--market-source` | ctp | ctp / dolphindb，期权及同月期货来源 |
| `--index-source` | dolphindb | dolphindb 标准流 / dolphindb-dfs 原表查询 |
| `--ctp-price-type` | last | last 成交 Bar；mid 报价 Bar，volume=0；无成交 LAST 不造 Bar |
| `--market-bar-table` | 无 | DolphinDB 市场标准流表，或 `DDB_OPTION_BAR_TABLE`；该来源必需 |
| `--index-bar-table` | 无 | DolphinDB 指数标准流表，或 `DDB_INDEX_BAR_TABLE`；标准流来源必需 |
| `--market-bar-timestamp` | 无，分钟推送必需 | DolphinDB 市场流 start/end 标签；CTP 聚合时间由适配器规范化 |
| `--market-volume-mode` | 无，分钟推送必需 | per-bar 保留分钟量；cumulative 按合约及业务日差分，首份基线量为 0 |
| `--market-bar-max-age-seconds` | 120 | 市场分钟推送及必需腿时效；大于60秒且不超过max-market-age-seconds，CTP路径不使用 |
| `--index-bar-timestamp` | 不覆盖 | start/end；DFS 沿配置，内置 end；标准流默认 end |
| `--frame-wait-seconds` | 2 | 同一分钟收集期限，有限秒数，`0 ≤ 值 ≤ min(30,max-market-age-seconds)` |
| `--instrument-max-age-seconds` | 30 | CTP 必需合约深度接收时效，`0 < 值 ≤ 120` 秒，独立于策略 Bar 年龄 |
| `--reference-source` | dolphindb | dolphindb / file；来源显式绑定 |
| `--reference-database` | 环境/默认 | 覆盖 `DDB_REFERENCE_DATABASE`，默认 `dfs://bomber_daily` |
| `--reference-timeout` | 30 | 参考资料读超时，正整数秒 |
| `--reference-refresh-seconds` | 5 | 控制线程刷新间隔，有限非负数秒 |
| `--reference-max-observation-seconds` | 30 | 资料最大观测年龄，有限正数秒 |
| `--allow-file-reference-test` | false | 文件参考测试必须显式提供 |
| `--opt-basic / --fut-basic` | 无 | file 参考模式两个文件都必需；仅条款，行情仍在线 |

行情帧的 `valuation_ns` 决定选约日期/时点和 Greeks；`request_ns` 使用接收墙钟用于执行时效。无行情候选不参与选约，持仓腿/对应期货/指数失效则等待恢复或失败闭闸。封闭后迟到 Bar 不追溯修改决策。DFS 指数要求同分钟，期限内缺失会跳过**整次决策**并增加 `skipped_index_frames`；晚到不补做。

### 4.4 DFS 指数查询

以下参数用于 `index-source=dolphindb-dfs`：

| 参数 | 默认值 | 含义与约束 |
| --- | --- | --- |
| `--index-feature-config` | `bomber/framework/dataprep/features/configs/features.json` | 数据集、原表字段、时间与 Bar 投影配置 |
| `--index-dataset / --index-schema-version` | index.minute / 1 | 数据集身份及正整数 Schema 版本；必须有一分钟真实 OHLCV 投影 |
| `--index-database / --index-table` | 沿配置 | 内置 `dfs://min_bar / index_1min`，可显式覆盖 |
| `--index-availability` | 沿配置 | 内置 observed；source-time 使用源发布时间，update-time 是 source-time 的兼容值 |
| `--index-poll-seconds` | 0.2 | 查询刷新间隔，`0 < 值 ≤ 30` 秒；不保证数据库实际延迟 |
| `--index-query-timeout` | 2 | 查询超时，整数 [1,30] 秒 |
| `--index-price-precision` | 4 | 指数价格精度 [0,9]，也用于标准指数流元信息 |
| `--index-size-precision` | 4 | DFS 数量精度 [0,9]；标准指数流数量精度固定 0 |

查询在控制线程、分钟帧锁外执行，只将市场待决策分钟的同分钟指数 Bar 投入缓冲；启动不灌入历史整日 Bar。没有估值日数据时等待至 ready-timeout，连接、类型、内容冲突失败闭闸。

#### 自动补查多少分钟，由哪里设置（2026-10-13 核对）

当前没有“补最近 N 分钟 / N 根 Bar”的命令行参数。需要区分数据库缓存读取范围和真正投递到策略的 Bar：

| 层次 | 当前范围 / 数量 | 代码位置 |
| --- | --- | --- |
| DFS 查询缓存 | 从估值日上海时间 00:00 开始，到当前墙钟已经结束的分钟，上限为该估值日末尾；启动时和后续轮询均在这个业务区间读取。只缓存源表已有数据，不按缺口造 Bar | `run_live.py:prepare_inputs` 设置 `FeatureQueryCache.start_ns/end_ns`；公共 `dataprep/features/query.py:FeatureQueryCache.poll` 计算本次结束分钟 |
| 向策略补入指数 | Runner 把当前尚未封闭的市场分钟集合 `frames.pending` 传给查询 Feed；按业务时间精确查找同一分钟指数并向前投影。跨轮询后出现的更旧请求记 OLDER_THAN_PROJECTED，旧帧按原期限跳过；不回放缓存历史，不借用新指数，不刷新旧请求的行情观测 | `live_runtime.py:OptionSessionRunner.poll_features`；公共 `market/stream/feature_query.py:FeatureQueryBarFeed.poll` |
| 缺指数等待 | 从该分钟首条行情实际接收起等待 `frame-wait-seconds`；到期仍缺指数则跳过该分钟整次决策，封闭后不补做历史决策 | `market/frames.py:MinuteFrameBuffer`；`live_runtime.py:flush_frames` |

例如 09:35 市场 Bar 创建了待决策分钟，查询缓存可能已包含当天 09:31—09:35 多条指数；策略这次只接收请求的 09:35 指数 Bar。只有 09:34 指数时不会替代 09:35，09:35 超期后即使查到也不追溯选约。

| 示例参数 | 控制什么 | 是否设置补查 Bar 数量 |
| --- | --- | --- |
| `--index-poll-seconds 0.2` | 数据库缓存刷新间隔；实际循环和查询耗时也影响刷新速度 | 否 |
| `--index-query-timeout 2` | 单次指数查询超时，单位秒 | 否 |
| `--frame-wait-seconds 5` | 同一分钟帧的接收等待期限，单位秒 | 否，不是补 5 根或 5 分钟 |
| `--max-market-age-seconds 300` | 价格/Bar 时效预算，默认 300 秒；DFS 源 Bar 仍须匹配请求分钟 | 否，不是补最近 5 分钟 |
| `--ready-timeout 180 / --seconds 900` | 启动就绪等待期限 / 就绪后运行时长 | 否 |

`FeatureQueryCache` 的默认 `max_rows=10000` 是单次查询返回记录的容量检查，`MinuteFrameBuffer` 的默认 `max_pending_frames=32` 是待封闭帧容量；两者均没有在 08 CLI 暴露为历史预热分钟数。当前 CTP 期权/期货缺失 Bar 不会自动用 DolphinDB 补齐。若要支持指标预热 N 根、跨日历史窗口或停机缺口回放，需要另行增加输入和可见时间政策；不能通过上述秒数参数取得这些行为。

`observed` 以本次查询完成作为首次可见时间，重复读取保持首次观测，内容改变形成新版本；不能恢复下载前的历史可用性。只有来源确认 `updateTime` 是真实发布/修订可用时间时才选 `source-time`。原表仅保留最后覆盖值时，无法重建消失的历史修订；历史研究应使用带版本与可用时间的标准导出。

## 5. 恢复、停机与输出

### 恢复与主动清仓

Model/SimNow 使用原始时间路径、原执行模式和同一原文件恢复，在原完整启动命令中增加：

```text
--resume --state-file <上一次的原状态文件>
```

例如恢复 3.4 的实时 Model 会话，保持原参数数组并明确指定原文件：

```bash
VEGA_RESUME_STATE=/path/to/original-model-state.json
python -u -m demos.08_option_vega.run_live \
  "${VEGA_LIVE_ARGS[@]}" --mode model \
  --strategy-position-source model --account-reconciliation off \
  --resume --state-file "$VEGA_RESUME_STATE" \
  --report-dir demos/08_option_vega/results/live-model-resume
```

恢复阶段可继续提供 `--require-fills`，但该参数要求**恢复后本次新增成交**，原文件中的已有仓位不计作本次成交。SimNow 恢复还需保留原生报单授权、原账户政策和其他原始参数。

2026-10-13 已成交 MO2611-C-7400 空 1 手会话的完整恢复命令见 [OPTION_VEGA_RESUME_20261013.sh](../../doc/evidence/commands/OPTION_VEGA_RESUME_20261013.sh)。该文件是针对已核验服务器路径和原状态的启动命令，不是新的策略模块；复制到服务器后可用 Bash 执行，也可直接复制文件全文到终端。它固定恢复原 `state-1791869411336135873.json`、原选约时点 13:36 和原策略/来源参数，不要求本次新增成交；仅将运行时长改为 86400 秒作为备用上限，并在控制器轮询边界按上海时间 15:00 或 SIGINT/SIGTERM 请求正常结束。无需修改、同步运行源码或重新编译。

主动结束时按一次 Ctrl+C，等待当前数据库/柜台操作或启动准备结束，再由原生命周期撤单、核验、保存和写报告；不要连续发送中断或强制终止。直接调用原 `run_live.py` 的 Ctrl+C 会在清理后记录 KeyboardInterrupt 为运行失败，这个启动命令改用停止标志走正常验证和停机路径。若在取得首帧前结束，仍可能因没有封闭帧而验收失败；行情失效、柜台/账本错误、订单拒绝或本次 4 次报单上限也仍会提前停止。收盘停机本身不提交平仓目标；已有策略退出规则仍可在运行中触发平仓。

该恢复命令只用于当天收盘前；原状态不能直接用于第二天。启动应看到“策略会话已恢复: generation=... revision=... TD=20261013”，并取得持仓腿、同月期货及指数的新鲜行情。后续用户返回 resume_ready=true，但持仓合约逐合约行情时效触发失败；该结果确认恢复启动完成，尚未证明能健康运行到收盘或手动/定时正常结束，详见第 7 节。

保留已成交归属仓位；首次新鲜行情帧按归属成交重建目标、重新计算对冲。不续发旧未完成开仓目标，当天已选约不会重复开仓；旧价格和 IV 缓存不恢复为新鲜数据。合约集合、条款指纹、日历、来源、时间模式、参数和执行政策必须一致，目前只支持同一执行日的固定资料会话恢复。资料版本改变或跨日要停止重新装配，不能删除旧持仓腿继续运行。

状态文件独立加锁，原生 CTP 另有账户锁。旧格式缺少新日历身份的状态不自动迁移，失败恢复不覆盖原文件。Replay 工程映射不允许 resume，其保存/读回通过不能等同于恢复通过，也不能转作次日实时会话接管文件。

Live 停机撤销自身活动订单并保存已成交仓位，默认不主动平仓。只有显式 `--flatten-day YYYYMMDD --flatten-slot HH:MM` 才在该业务日时点起提交全组合零目标，且仍需新鲜报价和成交反馈。配置清仓日不保证停机前全部成交；应核对最终仓位与活动订单。

### Live 报告

默认写入 `results/option-vega-<mode>-<时间纳秒>/`：

| 文件 / 字段 | 用途 |
| --- | --- |
| `summary.json` | 最终 status、failure、cleanup_errors、通道/参考就绪、模式/时间/日历/来源身份 |
| `events.txt` | Recording 目标请求，或 Model/原生执行回报；启动在装配前失败时可能无此文件 |
| `recorded_requests / entry_audit` | 客户端实际收到的 Recording 请求、选约 TARGET/NO_VALID_ENTRY/ZERO_ALLOCATION/WORKING_ORDERS |
| `frame_audit / late_bar_count / skipped_index_frames` | 帧来源时间、接收期限、CONSUMED/SKIPPED_INDEX、迟到与缺指数情况 |
| `bar_time_audit / ignored_stale_candidate_bars` | 最近 32 条 Bar 时间异常及累计忽略数；包含合约、来源、源时间、业务分钟、接收时间、年龄、必需状态与处置，详见第 7 节 |
| `feature_queries` | DFS 缓存、最新业务时间、查询状态；PROJECTED 只说明 Feed 发出，以 frame_audit 确认消费 |
| `strategy_positions / execution_positions / account_positions` | 策略归属仓位、执行账本、物理账户观测；未关联物理账户时为 null |
| `fills_received / order_updates_received / orders_submitted` | 本次归属成交、订单更新及柜台报单计数；目标、接受和成交分别核对 |
| `order_submission_audit` | 原生报单限价与观测盘口诊断，最多 32 条 |
| `replay_test_time_mapping / validation_scope` | 工程映射身份、样例及验收范围 |
| 独立 `state-file` | Model/SimNow 检查点、目标版本、归属、订单、成交去重和政策；不自动放进每次报告目录 |

无 `--require-fills` 时 `acceptance=market_frames`，并不要求非零目标或成交；有该开关时为 `attributed_fills`。还应检查 cleanup_errors、最终柜台活动订单和仓位，不以状态字段代替完整链路证据。

## 6. 代码文件的作用与必要性

### 6.1 是否每个文件都必须

**不是每次运行都需要全部 12 个 Python 文件。** 三个文件实现共用策略核心；其余承担回测入口、在线装配与生命周期、状态保存、工程测试、诊断或报告验收。按当前源码统计，目录内约 2200 行 Python，其中 `vega_signal.py + strategy.py` 为 465 行，约占五分之一；其他代码用于输入、运行、执行和验证。行数是本次阅读时的快照，不是后续版本的固定指标。

要区分两个问题：**功能是否需要**，以及**当前入口是否依赖该文件**。例如报单价格诊断不决定策略算法，但当前 SimNow 自动装配它，删掉文件就会启动失败；Recording 不保存检查点，但当前在线装配模块仍导入检查点文件。下面按当前实现说明，不把“本次没有调用”当作“可以直接删除”。

### 6.2 逐文件说明

| 文件 | 主要对象 / 入口 | 具体作用 | 必要性 |
| --- | --- | --- | --- |
| [vega_signal.py](vega_signal.py) | `Contract`、`OptionVegaConfig`、`tau`、`implied_greeks`、`allocate_short`、`filter_hedgeable_options` | 保存条款和配置；计算剩余期限、Black76 认购价格、IV、Delta/Gamma/Vega；按 Delta 档及 Vega 预算分配空头手数，过滤无同月有效期货的候选。纯数值和业务规则，不打开行情/执行连接 | **策略核心**；回测和所有 Live 模式需要 |
| [strategy.py](strategy.py) | `OptionVegaStrategy` | 接收分钟行情；管理每日选约、目标替换、实际成交后 Delta 对冲、到期/时间价值退出、末日清仓及订单异常；检查新鲜报价和场次恢复等待。数学计算调用 `vega_signal.py` | **策略核心**；回测和所有 Live 模式需要 |
| [assembly.py](assembly.py) | `risk_limits`、`assemble_strategy_components` | 共用策略装配：创建手数/名义金额风控、数据绑定和执行路由，注册同一个策略；指数只绑定行情。调用方提供 Feed 与执行积木，它不打开连接 | **共用装配**；当前回测和所有 Live 模式需要，保证两种入口使用相同策略与路由规则 |
| [run_backtest.py](run_backtest.py) | `load_contracts`、`run_case`、`main` | 解析回测参数，读取文件/日历/基础条款，准备期权链和旧持仓腿；检查选约同步和最终清仓时钟；装配原生模拟账户与历史 Runtime，输出报表并核对实际全平 | **文件回测入口**；直接 Live 不依赖它 |
| [run_live.py](run_live.py) | `parse_args`、`prepare_inputs`、`build_runtime`、`describe`、`main` | 解析在线模式、来源、交易日和执行政策；准备 CTP/DolphinDB 行情、独立指数、参考资料与编译日历；选择生命周期/执行模式并生成在线报告 | **在线入口**；所有 Live 模式需要，文件回测不依赖它 |
| [live_runtime.py](live_runtime.py) | `OptionReferences`、`OptionSessionRunner`、`assemble` | 把公共条款快照投影为本策略的认购与同月期货关系；收集/封闭多源分钟帧、查询指数、检查日期与时效、跳过缺指数帧；选择 Recording/Model/CTP 执行并装配状态管理 | **在线运行装配**；所有 Live 模式需要。分钟帧缓冲、执行、归属和持久化基础能力复用公共框架 |
| [recovery.py](recovery.py) | `OptionControl`、`OptionLocalLifecycle`、`OptionCtpLifecycle` | 负责启动就绪、控制线程轮询、参考刷新、行情检查、成交验收、停机报告；Local 处理 Recording/Model，CTP 处理 TD 对账、授权、报单上限、撤单与保存；恢复时核对归属及目标版本 | **在线生命周期**；即使不传 `--resume`，首次 Live 启动与停机也需要。文件名包含 recovery，但职责覆盖整个会话 |
| [checkpoint.py](checkpoint.py) | `OptionVegaCheckpoint` | 保存期权/期货目标、已选约日期、提交/选约计数和清仓状态；恢复时校验数量、合约、日历，清空旧价格与 IV 缓存，并标记等待新鲜行情。公共持久化另存成交、归属及订单 | **Model/SimNow 必需**；新会话也要保存，不只用于重启。Recording 不使用状态保存，但 `live_runtime.py` 顶层导入它，当前仍须随 Live 源码提供 |
| [replay_test.py](replay_test.py) | `ReplayTestTimeMap`、`mapped_bar_feed` | 在完成 CTP Bar 的副本上施加所有合约共用的时间偏移，让 Replay 流推进到指定估值时点；校验源日期/跨日，保存映射样例。文件名含 test，但它是可被入口调用的工程适配器，不是 unittest 文件 | **条件使用**；仅显式 `--replay-test-start-time` 时导入/调用。普通实时和原始时间 Replay 不需要该功能 |
| [execution_audit.py](execution_audit.py) | `AuditedExecutionBackend` | 包装原生执行 Backend，记录实际限价、分钟参考价、已观测盘口及年龄、是否跨价、提交调用结果。原订单发送一次，异常继续抛出；不调整定价或模拟成交 | **诊断功能**；当前 SimNow 无条件装配、没有关闭 CLI 开关，因此现有 SimNow 启动必须保留。回测/Recording/Model 不使用 |
| [acceptance.py](acceptance.py) | `review_recording` | 离线检查真实 Recording 的 summary：日期、模式、编译日历、指数查询、选约同分钟/接收期限，以及客户端实际非零目标。返回 target_chain 或 decision_only 验收结果，不连接网络 | **验收工具**；直接 `run_live.py` 不调用。`scripts.integration.options.recording` 在自动运行或复核已有报告时需要它 |
| [check_backtest_data.py](check_backtest_data.py) | `inspect_file`、`inspect_market`、`main` | 最近新增的只读排错工具：按相同路径规则与分钟标签检查失败时点的期货/指数文件，输出实际路径、时间范围、价格年龄和附近行。用于定位 IM2609 隔夜持仓缺报价问题 | **排错工具**；两个正常运行入口都不导入它。需要查输入覆盖时才单独执行，诊断不等同于回放送达验证 |

### 6.3 不同运行方式实际依赖哪些文件

“必需”表示当前模式会使用；“导入”表示当前装配仍要求文件存在，虽然该模式不启用其功能；“条件”表示显式工程映射才使用；“—”表示该模式的直接入口不依赖。表中只统计 `08_option_vega` 自身文件，公共框架、第三方依赖、原生扩展和数据配置仍需具备。

| 文件 | 文件回测 | Recording | Model | SimNow |
| --- | --- | --- | --- | --- |
| `vega_signal.py` | 必需 | 必需 | 必需 | 必需 |
| `strategy.py` | 必需 | 必需 | 必需 | 必需 |
| `assembly.py` | 必需 | 必需 | 必需 | 必需 |
| `run_backtest.py` | 必需 | — | — | — |
| `run_live.py` | — | 必需 | 必需 | 必需 |
| `live_runtime.py` | — | 必需 | 必需 | 必需 |
| `recovery.py` | — | 必需 | 必需 | 必需 |
| `checkpoint.py` | — | 导入 | 必需 | 必需 |
| `replay_test.py` | — | 条件 | 条件 | 条件 |
| `execution_audit.py` | — | — | — | 必需（自动诊断） |
| `acceptance.py` | — | — | — | — |
| `check_backtest_data.py` | — | — | — | — |

因此，**只运行文件回测时，本目录内的代码依赖为 4 个文件**：`run_backtest.py`、`assembly.py`、`strategy.py`、`vega_signal.py`。这是最小代码依赖说明，不能把这四个文件单独复制到空目录就认为所有依赖已满足。

直接 Live 的基础源码依赖为 Recording/Model 7 个文件、SimNow 8 个文件；启用 Replay 工程映射再增加 `replay_test.py`。使用自动 Recording 运行/复核工具时，再需要本目录的 `acceptance.py` 和目录外 `scripts/integration/options/recording.py`。只读回测数据诊断则单独运行 `check_backtest_data.py`，不依赖策略或执行模块。

### 6.4 调用关系与阅读顺序

下面只画目录内主要调用关系，文件、网络、撮合、订单、归属和公共状态管理实现位于 `bomber/framework/`：

```text
文件回测：
  run_backtest.py
    → assembly.py → strategy.py → vega_signal.py

在线运行：
  run_live.py
    → live_runtime.py → assembly.py → strategy.py → vega_signal.py
        → checkpoint.py          Model/SimNow 状态保存与恢复
        → execution_audit.py     SimNow 自动报单诊断
    → recovery.py                启动、轮询、验收、恢复、停机
    → replay_test.py             仅启用 Replay 工程时间映射

独立工具：
  scripts/integration/options/recording.py → acceptance.py
    自动工具运行时另启动 run_live.py；--summary 时只复核已有报告
  check_backtest_data.py → 公共 dataprep 文件读取与质量校验
```

了解策略先读 `vega_signal.py → strategy.py → assembly.py`；跑回测再读 `run_backtest.py`。跑 Live 再读 `run_live.py → live_runtime.py → recovery.py`；关注状态保存/恢复时读 `checkpoint.py`。其余三个工程/诊断/验收模块按任务阅读，回测排错时单独看 `check_backtest_data.py`。

### 6.5 为什么拆成多个文件，哪些可以精简

算法、策略状态、输入来源、执行装配和会话生命周期有不同的职责。现在的划分让回测与 Live 共用选约/对冲逻辑，并让工程时钟或报单诊断的修改集中在对应模块。Python 并不要求拆成 12 个文件；合并文件可以减少文件数，但不会消除输入校验、分钟同步、归属、保存或停机所需的逻辑。

保留回测与 Live 全部现有能力时，核心、入口、装配和生命周期都有实际调用。工程映射、独立验收和输入检查可按使用范围选择；报单审计若要从 SimNow 中移除，需要同时修改在线装配及对应测试。当前没有发现完全无调用入口或无引用用途的 `.py` 文件。当前用户要求是说明和更新文档，因此本次不删文件，也不改变运行行为。

可以优化的一个明确依赖是：将 `live_runtime.py` 中的 `OptionVegaCheckpoint` 导入移入状态管理分支，Recording 就不再需要仅为导入而带上检查点模块。这属于后续代码精简，不改变 Model/SimNow 新会话仍须保存状态的要求。

`__pycache__/` 是 Python 自动生成的字节码缓存，可重新生成，不属于需要人工维护的策略源码。`results/` 属于运行输出；其中 Model/SimNow 的原状态文件、锁和报表要结合会话判断用途，不能因为它们是输出就当成无用代码统一清理。测试代码主要放在项目 `tests/`，诊断工具的测试为 `tests/test_option_vega_backtest_data.py`。

### 6.6 使用的公共组件

回测和 Live 都由 `assembly.py` 创建策略、数据绑定与执行路由。策略使用 `position()` 和 `strategy_working_quantity()` 查询自身归属成交及在途分配，不以物理账户总仓或其他策略委托计算 Delta。

第三方一对多执行通过 `build_runtime(args, account_routes=...)` 和公共 `AccountExecutionRoute` 装配，策略主账本选择 Model；目前没有对应命令行账户路由参数，也没有新增厂商网络连接器。08 不引用其他 demo 文件。

#### dataprep：输入准备


| 关键接口 | 本例用途 |
| --- | --- |
| input_session / resolve_option_args | 共享来源与读取缓存，解析期权、期货、指数行情及基础条款路径 |
| load_calendar / infer_market_calendar | 读取显式日历或推导全部已知行情日期，为期限判断提供日期集合 |
| load_options_basic / load_cffex_futures | 校验股指认购与真实期货条款，返回规范化静态资料 |
| inventory / BarFileKey / plan_option_chain | 索引三类行情，声明选约及持仓管理所需文件，生成 InputPlan |
| BarReadSpec / prepare_option_chain | 按日盘一分钟统一时间、身份和严格 OHLCV 校验，返回 HistoricalInputBundle |
| add_prepared_bar_source | 注册公共层已准备行情，不重新读取原始文件或重复移动时间 |
| write_input_reports | 输出输入版本、覆盖与问题诊断 |

#### datahub：参考资料查询

| 关键接口 | 本例用途 |
| --- | --- |
| 参考资料来源 | 回测使用文件条款；Live使用DolphinDB条款快照。策略只使用准备后的认购条款、同月期货映射和日历，不查询数据库、连续主力角色或复权价格 |

#### market：真实行情回放

| 关键接口 | 本例用途 |
| --- | --- |
| FileReplayFeed / InstrumentMeta | 注册认购、同月期货与指数真实行情；指数仅提供信号行情元信息 |
| DataType.BAR / MarketStreamBinding | 声明交易腿一分钟回放流；指数不创建执行合约或交易路由 |

#### trader：卖方及对冲执行

| 关键接口 | 本例用途 |
| --- | --- |
| instrument_factory.make_option / make_future / instrument_meta | 构造原生期权、期货合约及对应行情元信息，无例子内转发文件 |
| StrategyTemplate / set_targets | 提交认购和对冲期货的完整目标，包括旧腿零目标 |
| CtpFuturesBasicProfile / NautilusSimExecutionBackend | 模拟账户、手续费、基础保证金及原生合约回测 |
| PositionManager / NetTargetOrderPlanner | 管理实际与在途持仓，把目标转换为净持仓订单 |
| MarketReferencePriceStore / PreTradeRiskManager / RiskLimits | 校验真实报价时效、手数和名义金额，指数没有交易风控路由 |
| DataBinding / ExecutionRoute | 按符号绑定三类信号行情，只给期权与期货创建执行路由 |
| UnifiedStrategyRunner / NautilusMarketFeedAdapter / UnifiedHistoricalRuntime | 串联真实行情、策略、模拟执行与历史回放生命周期 |

## 7. 验证方法与现有证据

具备原生 SDK 的目标环境可运行离线验收；这些命令使用行情/执行夹具，不连接数据库或柜台：

```bash
python -u -m tests.run_cross_section_acceptance \
  --only option-vega --only option-vega-live \
  --only live-common --only attribution --only assembly \
  --only option-calendar --only feature-query-live
```

无原生 SDK 的开发环境可运行：

```bash
python -m unittest tests.test_demo_option_vega tests.test_option_vega_live
python -m tests.run_option_vega_offline
```

第二条替换 SDK 行情实体和未使用的 SDK 桥，实际 Runner、归属、风控、执行状态机与 JSON 持久化仍是项目代码，不能替代原生环境验收。

截至 2026-10-13 的已有线上证据来自专项交接及用户返回的运行报告；本次本地离线验证另列如下：

| 范围 | 已有结论 | 仍需单独确认 |
| --- | --- | --- |
| REV3 远程离线回归 | 五组选定组共执行 195 项通过；真实指数只读探针正确映射 14:18—14:24 七条记录 | 只读查询不证明原运行帧截止前可见 |
| 实时非映射 Recording | 修复后 11:13—11:28 正常运行约 15 分钟，15 帧全部消费，11:19 非零期权目标到达 Recording 客户端，18 根过期候选被忽略；现有报告复核函数的 target_chain 检查通过 | 本次没有线上指数超时样本；Recording 不证明真实成交、持仓恢复或非零期货对冲成交 |
| 候选过期 Bar 本地修复回归 | 无 SDK 离线装配入口 61 项通过，策略/Live 单测 34 项通过；新增 5 项测试覆盖忽略过期候选、随后新鲜帧产生 Recording 目标，以及目标腿、持仓腿、对应期货、指数和未来时间仍拒绝 | SDK 实体替身不能代替原生环境与真实行情复测 |
| Replay 工程 Recording | 单档 0.25 运行 10 帧，收到 MO2610-C-7300 空 1 手非零请求 | 不代表实时路径通过 |
| Replay 工程 Model | 9 帧、1 次归属模型成交、2 次订单更新，策略及执行仓位 -1；状态读回 generation=14 | 没有非零期货目标；重启恢复未验 |
| Replay 工程原生 SimNow | 首次订单接受后未成交并撤单；随后带价格诊断运行成交：MO2610-C-7300 卖开 1 手，限价 37.2、成交 37.4、柜台单号 12681；策略/执行/账户净仓均 -1，停机活动订单 0，状态读回 generation=15 | 仅覆盖该 Replay 会话；实时成交见下一行，非零期货对冲成交、重启恢复未验 |
| 2026-10-13 实时原生 SimNow | 原开仓会话 status=passed：13:36 选约 MO2611-C-7400 为 -1 手，1 次报单、1 次归属成交，三方净仓均 -1，停机活动订单 0、清理无错误；后续同日恢复 resume_ready=true，但持仓原始行情事件年龄 30.172 秒超过 30 秒阈值，运行失败并正常清理 | 开仓与恢复启动分别有证据；持续健康运行、主动/定时停止、平仓及非零期货对冲成交待验，成交价格/柜台单号和恢复 generation 未提供 |
| 9 月 1—9 日原生文件回测 | 用户返回的远程运行成功：7 个交易日、1687 帧、7 次选约、32 笔订单、33 笔模拟成交，最终检查及清理完成 | 原始报表尚未单独读取；9 月 10 日缺报价问题及延长区间仍需核对 |

### 2026-10-13 实时指数可见延迟与分钟截止不匹配

用户返回运行机报告 `tests/results/gap08-realtime-recording/option-vega-recording-1791855044004253758/summary.json` 的字段。来源为实时 CTP LAST + DolphinDB DFS 指数、observed 可见性，轮询间隔 0.2 秒、查询超时 2 秒、帧等待 5 秒；记录状态 failed、frames=1、entry_audit=[]、skipped_index_frames=5、late_bar_count=149。以下以上海时间换算，完整 summary 中最后成功分钟为 09:31：

| 业务分钟 | 原帧接收截止 | 结果 |
| --- | --- | --- |
| 09:31 | 09:31:19.766 | CONSUMED；Runner 指数接收时刻为 09:31:15.089 |
| 09:32 | 09:32:09.073 | SKIPPED_INDEX；最后请求检查仍为 MISSING |
| 09:33 | 09:33:06.971 | SKIPPED_INDEX；最后请求检查仍为 MISSING |
| 09:34 | 09:34:06.522 | SKIPPED_INDEX；最后请求检查仍为 MISSING |
| 09:35 | 09:35:07.236 | SKIPPED_INDEX；未形成选约审计 |
| 09:36 | 09:36:09.647 | SKIPPED_INDEX；此分钟稍后才进入查询缓存 |

停机缓存已有 6 个独立分钟槽、最新业务时间为 09:36；对应 `available_ns` 换算为 09:36:14.083963545，缓存接收为 09:36:14.085008726，晚于原帧截止约 4.44 秒。查询通道 failure=null，只有 09:31 出现在 emitted_versions；证明来源连接、业务时间转换和至少一帧投递可用，后续数据在帧关闭之后仍能读取。末次查询 09:36:15.135 与 Runner 最后接收相隔约 300.046 秒，与默认 300 秒接收时效超限一致。connected=false 是停机关闭后的状态。

源表截图中的 `updateTime` 为分钟后约 3 秒，但本模式用真实查询首次观测作为可见时间，例如 09:31 首次观测为 09:31:14.152。两种时间不能混用；报告不能单独证明延迟来自写入提交、查询节点、采集服务器时钟或网络/查询处理。149 个迟到 Bar 是总数，未拆来源，不作为“149 个指数迟到”的结论。

本次直接问题是原 5 秒窗口不匹配实际查询可见延迟。下一次 Recording 可显式用 `--frame-wait-seconds 20`，其余时间标签、observed 可见性、查询超时和新鲜度保持原值；选约时点改为尚未到达且处于交易时段的时点，并预留启动就绪时间。20 秒会延后分钟帧封闭和决策，是新的显式运行参数，不修改策略默认值；该值只用于复核当前延迟样本，不保证其他运行的完整送达。

使用公共 `MinuteFrameBuffer` 和本次 09:36 的实际首条市场接收、指数缓存接收时刻作离线复现：5 秒拒绝、20 秒接纳。该检查验证帧期限计算，不代表新的线上 Recording 已通过。提高 `max-market-age-seconds` 只会推迟最终停止，不能让已经跳过的 09:35 恢复选约；切换 source-time 也不能把实际查询接收时间提前。

### 2026-10-13 的 20 秒窗口实际复测

用户返回完整控制台输出，报告目录为 `tests/results/gap08-realtime-recording/option-vega-recording-1791855964401760144`。本次使用 20 秒等待、真实事件时间、CTP LAST、DFS 指数及 observed 可见性，实际北京时间约 09:46:05 启动，09:52:30 因异常提前结束；命令虽配置 `--seconds 900`，并未正常完成 15 分钟。

| 业务分钟 | 指数实际接收时间 | 帧截止 | 结果 |
| --- | --- | --- | --- |
| 09:47 | 09:47:14.099 | 09:47:22.308 | CONSUMED |
| 09:48 | 09:48:14.130 | 09:48:23.429 | CONSUMED |
| 09:49 | 09:49:14.163 | 09:49:22.687 | CONSUMED |
| 09:50 | 09:50:14.184 | 09:50:21.434 | CONSUMED |
| 09:51 | 09:51:14.134 | 09:51:22.297 | CONSUMED |

报告 frames=5、skipped_index_frames=0，五个指数均在期限内，余量约 7.25—9.30 秒；查询 failure=null、缓存最新分钟 09:52。该证据验证这个样本中 20 秒窗口的指数补查与实际帧消费，不能只凭 PROJECTED 日志判断，亦不能据此承诺所有运行都满足 20 秒。

本次仍设置 `--option-slot 09:35`，已经早于启动时间，因此 entry_audit=[]、entry_decisions=0、submissions=0、recorded_requests=[] 符合当前规则。历史指数缓存不补发过去选约；这不是 NO_VALID_ENTRY 或 ZERO_ALLOCATION，没有实际评价选约候选。记录 orders_submitted=0、fills_received=0、cleanup_errors=[]，运行 status=failed，非零 Recording 目标链仍未验收。

停止时 session_readiness 的问题只指向 CTP：STREAM_UNHEALTHY/STALE 与 RECEIVE_STALE，接收年龄 30.08655193 秒、阈值 30 秒。DolphinDB 指数与参考资料没有本次就绪错误。源码对成功转换的原始 CTP 深度更新接收心跳，即使没有派生成交 Tick 也应刷新；仅凭当前报告不能确定原因是柜台/网络停止推送，还是本地回调/转换未更新，不能把这个停止解释为指数查询再次失败。late_bar_count=84 为所有来源总数，未拆来源。

下一步在同一实时 MD 前置对活跃期货做原始行情采样，独立核对行情是否持续推进。以下探针仅连接 MD，不连接 TD、不报单；它只验证采样范围，不代替完整 15 分钟或多合约策略验收：

```bash
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_PRODUCTION_MODE=true
CTP_SYMBOL=IM2610 CTP_CONNECT_TIMEOUT=30 CTP_MARKET_TIMEOUT=60 \
  CTP_MARKET_SAMPLE_SECONDS=60 python -u -m tests.run_ctp_live --stage 4
```

探针检查登录、订阅、首份原始行情，并输出 60 秒采样末的累计 tick 数与最新行情时间。随后 Recording 保持 `--frame-wait-seconds 20`，将 `--option-slot` 改为尚未到达且处于交易时段的时点，预留 `ready-timeout + 首根完成Bar` 时间。调整指数等待不能修复 CTP 接收停止；单独提高逐合约参数也不关闭公共 Feed 的来源健康检查。

#### 10:02—10:03 原始 MD 探针结果与下一层检查

用户在同一 30011 实时前置对 IM2610 执行 stage 4，2026-10-13 北京时间 10:02:17 登录/订阅成功；首份原始行情时间为 10:02:16，交易日和 ActionDay 均为 20261013。60 秒采样末 ticks=121，最新原始行情为 10:03:16，输出 `CTP raw market data OK`。这验证该采样时段单合约原始订阅持续推进，不回溯证明此前 09:52 会话没有断流，也不单独验证标准转换、策略分发或 105 合约装配。

下一层使用正式 `CtpLiveDataFeed`，按 IM 条款显式设置精度、最小价位和乘数，检查标准 QuoteTick/TradeTick 的转换与投递，仍不连接 TD、不报单：

```bash
CTP_SYMBOL=IM2610 CTP_EXCHANGE=CFFEX \
  CTP_PRICE_PRECISION=1 CTP_PRICE_INCREMENT=0.2 \
  CTP_SIZE_PRECISION=0 CTP_MULTIPLIER=200 \
  CTP_CONNECT_TIMEOUT=30 CTP_MARKET_TIMEOUT=60 \
  python -u -m tests.run_ctp_live --stage 5
```

现有 stage 5 取得首份标准 Quote 和成交增量 Tick 后结束，不进行 stage 4 的 60 秒采样；即使通过也不能证明约 6 分钟后的健康稳定。若转换检查通过，继续在未来选约时点运行原 Recording 组合；若再次停止，保留停止前连接/订阅/转换错误日志及 summary，核对运行机实际加载的 Feed 版本和原始接收记录。

用户后续返回 stage 5 成功输出：北京时间 10:10:39 登录/订阅 IM2610，收到标准 QuoteTick（bid=6908.0@4、ask=6909.6@1，价格精度 1/1、数量精度 0/0）及 TradeTick（price=6908.8、size=2），输出 `CTP standard feed OK` 并安全断开。该结果验证本次单合约正式转换和投递，之前 09:52 的 CTP 接收停止原因仍未确定；不把短时探针记为组合持续运行或非零目标链通过。

回到 Recording 组合时可在交易时段、启动前计算未来约 6 分钟的选约时点，替换原命令的 `--option-slot 09:35`；保留 `--frame-wait-seconds 20 --ready-timeout 180 --seconds 900`。例如 Bash/Zsh 中执行：

```bash
VEGA_OPTION_SLOT=$(python -c 'from datetime import datetime, timedelta; from zoneinfo import ZoneInfo; print((datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(minutes=6)).strftime("%H:%M"))')
printf '本次选约时点（北京时间）: %s\n' "$VEGA_OPTION_SLOT"
```

在原完整 Recording 命令中使用 `--option-slot "$VEGA_OPTION_SLOT"` 并立即启动，不要长时间放置后再执行；所算时点须处于交易时段且早于 14:50。运行日、MD 前置及指数日期仍按实际核验值填写。完整组合成功需要同时满足持续行情/清理通过、对应时点的选约评价及客户端非零 Recording 请求。

### 2026-10-13 10:38—10:44 的 Bar 分派异常与修复

用户纠正后提供的 08 输出对应 `tests/results/gap08-realtime-recording/option-vega-recording-1791859084554862099`。北京时间约 10:38:05 启动，10:44:40 异常停止；10:39—10:44 六帧均为 CONSUMED，frames=6、skipped_index_frames=0、late_bar_count=121。指数每分钟约在分钟后 14 秒送达并被接纳，本次停止原因不是指数缺失。

最早错误是 `TradeTickBarFeed._aggregate_tick → OptionSessionRunner.publish` 抛出“实时Bar过期或尚未收盘”，随后公共 Feed 标记 DISPATCH_ERROR、会话就绪检查失败。旧报错没有合约、时间戳或年龄，无法判断触发的是超龄还是未来时间。LAST 聚合器在下一次成交进入新分钟时才输出上一成交桶；稀疏成交候选可能在几分钟后输出旧桶，这是代码上可发生的路径，尚不能凭本次旧日志确认具体合约。

原实现将任何合约的 Bar 时间异常都视为会话硬错误，导致未持有候选的过期桶也可能停止整个组合。现已在 [live_runtime.py](live_runtime.py) 的 `OptionSessionRunner.publish` 中修正：

| 输入情况 | 处理 |
| --- | --- |
| 同日、非必需候选的 Bar 年龄超过 `--max-market-age-seconds` | 忽略该 Bar，记录 IGNORED_CANDIDATE/STALE_BAR；不更新分钟帧、最新报价或执行参考价 |
| 有实际仓位、在途量或非零目标的腿，以及其对应对冲期货、指数的过期 Bar | 仍记录 REJECTED 并关闭接收；必需集合由 `strategy.required_market_keys()` 决定 |
| 未来业务分钟、错误估值日、错误绑定来源或合约 | 仍拒绝；不通过放宽年龄或时间映射绕过 |

`OptionMarketInputs` 新增“候选过期Bar已忽略”和“必需或未来Bar时间无效”日志；[run_live.py](run_live.py) 将 `bar_time_audit`、`ignored_stale_candidate_bars` 写入 summary。审计保留最近 32 条，累计忽略数不受此上限影响。`late_bar_count` 统计的是帧缓冲拒绝的迟到 Bar；上述提前忽略数独立统计，不能将两者当成同一计数。默认 300 秒年龄上限及 20 秒显式帧等待不变，迟到帧仍不补做决策。

本次 summary 的 option_slot=10:19，早于 10:38 启动，故 entry_audit=[]、entry_decisions=0、recorded_requests=[]、submissions=0；这次仍未验证选约。每次启动前都须重新执行上面的 `VEGA_OPTION_SLOT=...`，不能沿用上一轮 Shell 变量。

本次部署的运行代码为 `demos/08_option_vega/live_runtime.py`、`demos/08_option_vega/run_live.py`；同步验证时再带上 `tests/test_option_vega_assembly.py`、`tests/run_cross_section_acceptance.py`、`tests/run_feature_datahub.py`，后两者更新的是被测源码摘要及入口摘要。README 为说明文件。本次修改均为 Python，不需要重新编译 Cython。具备 SDK 的运行机先执行本节顶部原生离线验收，再用原 Recording 命令复测，保留 `--frame-wait-seconds 20` 并重新计算未来选约时点。

结束后核对 `status / failure / frames / entry_audit / recorded_requests / skipped_index_frames / ignored_stale_candidate_bars / bar_time_audit / session_readiness / cleanup_errors`。若再次失败，新的 Bar 审计能区分必需腿过期、未来时间和候选忽略；若显示 CTP 接收超时，仍须沿原始接收链路检查。上述修复的后续线上结果见下一节。

### 2026-10-13 11:13—11:28 实时 Recording 非零目标链通过

用户返回修复后完整控制台输出，对应运行机目录 `tests/results/gap08-realtime-recording/option-vega-recording-1791861210699461223`。北京时间 11:13:31.505 启动、11:28:32.100 安全断开，覆盖约 900.6 秒；summary 的 status=passed、failure=null、cleanup_errors=[]、session_readiness.ready=true。使用真实 CTP LAST + DolphinDB DFS、observed 可见性、20 秒帧等待，没有 Replay 时间映射。

| 核验项 | 本次结果 |
| --- | --- |
| 分钟帧与指数补查 | 11:14—11:28 共 15 帧全部 CONSUMED；指数每分钟约在分钟后 14 秒接收，skipped_index_frames=0，查询 failure=null、emitted_versions 共 15 个分钟 |
| 11:19 选约 | 唯一 entry_audit：outcome=TARGET、candidate_count=6，目标 MO2611-C-7300 为 -1 手，对应期货 IM2611；candidate_count 是到期月筛选前的有效候选数 |
| 客户端目标 | recorded_requests 共 1 条，与选约业务分钟一致，唯一非零目标 MO2611-C-7300.CFFEX=-1；请求发生于 11:19:21.767 |
| 同分钟输入 | 选约帧包含指数、选中期权及 IM2611；指数接收 11:19:14.121，早于帧截止 11:19:21.764；期权和期货源时间归一后同为 11:19 |
| 过期候选处理 | ignored_stale_candidate_bars=18，18 条审计均 required=false、IGNORED_CANDIDATE/STALE_BAR，年龄 308.462—523.828 秒；目标期权、对应期货、指数均未出现在忽略审计中 |
| 报单与成交 | submissions=1 表示 Recording 接收到 1 次目标提交；orders_submitted=0、fills_received=0，没有柜台报单或成交 |

例如 MO2612-C-8800 的 11:16 完成分钟在 11:21:29.682 才收到，年龄 329.682 秒，被忽略后 11:22 起后续分钟继续消费并正常停机。这次已直接观测到稀疏成交候选的过期桶，并验证新处理没有因此停止会话；仍不能回溯确认 10:44 旧异常的具体合约。late_bar_count=253 是帧缓冲迟到总数，与提前忽略的 18 根分别统计。

本地从用户控制台的“停机结果”安全提取 summary（Decimal 数量保存为字符串），调用现有 `acceptance.review_recording(..., trading_day="20261013", option_slot="11:19", require_target=True)`，结果 status=passed、scope=target_chain、target_chain_verified=true、errors=[]。本次没有直接读取运行机原始 summary.json 或取得进程退出码；可在运行机对原文件补做同一只读复核：

```bash
python -u -m scripts.integration.options.recording \
  --trading-day 20261013 --option-slot 11:19 \
  --summary tests/results/gap08-realtime-recording/option-vega-recording-1791861210699461223/summary.json
```

带 `--summary` 只读已有报告，不连接数据库或柜台。输出 `recording-acceptance.json` 保存到该报告目录。精简证据与复核结果见 [OPTION_VEGA_REALTIME_RECORDING_20261013.json](../../doc/evidence/live/OPTION_VEGA_REALTIME_RECORDING_20261013.json)。

此次登记实时非映射的指数补查、分钟消费、非零期权 Recording 目标与候选过期处理通过；线上没有指数超时跳过样本，超时拒绝仍由既有离线测试覆盖。Recording 没有实际期权仓位，而策略按实际成交仓位计算期货对冲，故本次期货目标为 0 符合规则；原生实时报单成交的后续结果见下一节，非零期货对冲成交和持仓恢复仍需各自验收。

### 2026-10-13 13:36 实时 SimNow 期权开仓成交通过

用户先返回停机片段，后从运行机原报告 `tests/results/gap08-realtime-simnow/option-vega-simnow-1791869412309900311/summary.json` 补充选择字段。execution_origin=ctp_native、bar_time_basis=source_event，MD / TD 日均为 20261013，session_readiness.ready=true、skipped_index_frames=0；最终 status=passed、failure=null。

| 核验项 | 本次结果 |
| --- | --- |
| 13:36 选约 | outcome=TARGET、candidate_count=14，MO2611-C-7400 目标 -1 手，future_bindings 指向 IM2611；实际目标请求时间为 13:36:21.208 |
| 原生执行 | orders_submitted=1、fills_received=1，报告登记 1 次原生报单和 1 次策略归属成交 |
| 三方净仓 | strategy_positions、execution_positions 与 account_positions 均为 MO2611-C-7400 空 1 手 |
| 停机总仓与清理 | final_managed_gross 为 MO2611-C-7400 的多 0 / 空 1；final_active_orders=0、cleanup_errors=[]；范围外 AG / SC / NI / I 总仓与此前只读结果一致 |
| 差异字段 | position_differences=null，不将 null 当成单独的“无差异列表”证明；仓位结论依据上述三方数量及停机管理总仓 |

本次状态文件为运行机绝对路径 `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/tests/results/gap08-realtime-simnow/state-1791869411336135873.json`。停机后保留实际空头 1 手，后续会话应保留该原文件，并先核对恢复身份及柜台仓位；不能换新 state-file 将已有持仓当成空仓启动。恢复检查点绑定原策略配置和来源身份，修改选约时点、估值日或来源需要先确认兼容性，不能直接改 JSON。

#### 同日恢复后的持仓合约原始深度行情超时

用户随后返回恢复报告片段，目录为 `tests/results/gap08-realtime-simnow-resume/option-vega-simnow-1791871421727321406`：resume_ready=true、orders_submitted=0、skipped_index_frames=0，最终 status=failed，原因是 MO2611-C-7400.CFFEX 的 event_age_seconds=30.172288059、receive_age_seconds=28.905667247，超过 `--instrument-max-age-seconds 30` 的事件年龄上限；交易日正确、timestamp_regressed=false。停机保留该合约空 1 手、final_active_orders=0、cleanup_errors=[]，范围外仓位与原会话一致。此次不是恢复身份不一致、指数缺失或收盘定时结束，而是恢复成功后行情时效检查关闭接收。

`OptionSessionRunner.stale_instruments → InstrumentHealthGate` 对有持仓/在途/目标的必需腿逐合约检查原始 CTP 深度的事件与接收年龄，任一超限均拒绝。30.172 秒源事件年龄已经超限，即使接收年龄仍为 28.906 秒也会失败。行情 Feed 整体 ready 只说明通道整体状态；其他合约持续更新不能代替持仓合约更新。公共 `CtpLiveDataFeed.on_depth_market_data` 在成功转换原始深度后刷新逐合约观测，即使没有派生成交 TradeTick 也刷新，因此不能把本次错误直接等同于“30 秒没有成交”。柜台该合约推送间隔、回调未到达或转换异常的具体原因还需原始行情及错误日志定位。

本地用相同两项年龄复现逐合约门控：阈值 30 秒拒绝、60 秒接纳；60 秒下时间倒退仍拒绝。这只是边界检查，不是新的线上运行。继续 SimNow 工程观察时，可明确将原恢复命令中的 `--instrument-max-age-seconds 30` 改为 60，再恢复同一个原状态文件；含义是允许原始事件和接收观测最多 60 秒旧，不能视为已经修复行情推送。该参数允许上限为 120 秒，当前未纳入 SessionIdentityCheckpoint，因此单独调整它不改变恢复身份；原 13:36 选约、20 秒帧等待、其他策略/来源与风险参数仍须保持一致。分钟帧等待、原始深度时效与 Bar/IV 的默认 300 秒限制是不同检查；改帧等待或 max-market-age-seconds 不会消除本次逐合约 30 秒检查。

下一次恢复片段对应 `tests/results/gap08-realtime-simnow-resume/option-vega-simnow-1791872194341594164`：resume_ready=true、orders_submitted=0、skipped_index_frames=0，但 session_readiness.ready=false，CTP issue 为 STREAM_UNHEALTHY/MarketHealthReason.STALE，status=failed。这次是公共 CTP 行情源健康层失败，不是上一轮逐合约 event_age 超限报告。当前 `CtpLiveDataFeed` 没有显式覆盖 StreamHealthConfig，公共 `stale_after_seconds` 默认仍为 30；`--instrument-max-age-seconds` 控制 CtpMarketChannel 的来源接收检查及必需腿检查，却不改变这个 Feed 内部静默阈值。片段没有提供本次实际逐合约阈值、健康详细快照或所有合约最后接收时间，不能直接确认整体静默的原因，也不能据此证明订阅数量过大。

该次停机依旧保留 MO2611-C-7400 空 1 手、活动订单 0、清理错误为空；DFS 缓存最新业务分钟为 14:21，查询 failure=null。优先使用 [OPTION_VEGA_RAW_GAP_20261013.sh](../../doc/evidence/commands/OPTION_VEGA_RAW_GAP_20261013.sh) 仅 MD 采样持仓合约 120 秒，复用原 stage 4 探针并增加 `RAW_GAP_RESULT`：目标合约原始回调数、最大接收间隔（含采样末静默）和最后源时间。该命令不连接 TD、不报单、不改 08 运行源码；它统计原始回调，不能替代正式转换与全链负载对比，也不伪造未收到的 Tick。应在交易时段、策略已停止后采样；单合约结果仅覆盖该采样窗口。先查清有效来源观测为何停止，再决定是否调整整体静默策略，不仅靠放宽年龄继续运行。

用户随后返回单合约原始探针结果：北京时间 14:46:28—14:48:28，MO2611-C-7400 采样 120 秒、ticks=75、max_receive_gap_seconds=9.097、trailing_silence_seconds=2.843，最后源时间为 14:48:24.500，交易日和 ActionDay 均为 20261013。这验证该窗口内单合约原始推送连续性，没有 30 秒接收静默；它不能回溯证明 14:10/14:21 的来源状态，也不能单独确定全链订阅的负载原因。

全链对比使用 [OPTION_VEGA_FULL_RAW_GAP_20261013.sh](../../doc/evidence/commands/OPTION_VEGA_FULL_RAW_GAP_20261013.sh) 只读原状态中的合约集合，并只连接 MD 订阅同一全链；记录全部订阅响应及 ALL、持仓期权、同月期货 IM2611 的最大接收间隔。它不恢复或修改状态文件、不连接 TD、不报单，也不执行正式 Tick 转换、分钟聚合、DolphinDB 查询和策略控制。全链原始推送也稳定时，只能说该窗口未复现“订阅数量本身导致原始接收中断”，还需排查正式转换/分派/聚合、控制线程数据库调用和 CPU/GIL；不能直接宣布数据库或某个线程为根因。原命令仅排除 15:00 休市，遗漏了期权 14:57 进入收盘集合竞价，已按下一节修正：两个间隔探针现在要求采样处于期权连续竞价期间，并为连接、登录、全体订阅响应和首份行情预留各项超时预算。默认预算为 120 秒采样 + 150 秒准备，下午最迟启动须早于 14:52:30，上午同样不能跨 11:30；超出实际预算而跨交易阶段的结果不能作为同阶段负载对比。

#### 全链原始采样进入收盘集合竞价：不登记订阅负载根因

用户在北京时间 14:57:09—14:59:10 返回全链原始采样：105 个合约全部订阅成功，无拒绝或缺失；120 秒窗口的 ALL 共 884 次回调、最大接收间隔 1.959 秒，IM2611 共 192 次回调、最大间隔 3.053 秒，末源时间为 14:59:08。MO2611-C-7400 只有初始 1 条，源时间停留在 14:56:58.500，采样窗口内静默 120 秒。这说明该窗口全局原始推送与期货持续更新，并未观察到整个原始流因全链订阅而停住；持仓期权没有后续深度回调。

但该窗口已经进入不同的交易阶段：[中金所股指期权交易细则第十七条](https://www.cffex.com.cn/u/cms/www/202207/1820311129r5.pdf)将下午连续竞价限定到 14:57，随后至 15:00 为收盘集合竞价；[IM 交易细则第十一条](https://www.cffex.com.cn/u/cms/www/202207/182020307zry.pdf)的下午连续竞价延续到 15:00。因此此前 14:46—14:48 的单合约与本次 14:57—14:59 的全链不是同交易阶段的 A/B 对比，不能把期权回调从 75 次变为 1 次直接归因于订阅数量。规则确认的是交易阶段，不单独证明 SimNow 在该阶段的具体逐合约行情发布方式；本样本里的期权静默与阶段切换相符，作为推断记录。

原 stage 4 的 `CTP raw market data OK` 表示登录、订阅及首份原始行情通过，不表示目标合约在整个采样窗口持续更新。最大间隔的判断应读 FULL_RAW_GAP_RESULT，而不能只看该 OK 文本。后续负载对比应在同一连续竞价阶段、尽量同一连接内进行全链与缩减订阅的前后采样；不能把集合竞价或休市的静默直接登记成订阅拥堵。

要使 08 持续监控到 15:00，还需在行情健康与策略决策中明确区分期权连续竞价、收盘集合竞价及期货交易阶段。14:57 后不能把旧连续报价伪装成新鲜输入；应有明确的竞价阶段决策与报单限制，并继续处理执行回报和保存实际仓位。当前运行器尚无这一阶段策略，收盘启动脚本的 15:00 结束条件本身不能解决逐合约 30/60 秒门控。之前 14:10/14:21 的超时发生在连续竞价阶段，不能用收盘集合竞价解释；其全源健康原因及连续阶段持仓腿间隔仍待定位。

#### 收盘后利用 SHFE 夜盘排查公共行情链路

CU / AG / RB 夜盘可用于公共 CTP 行情层诊断。按[上期所交易时间](https://www.shfe.com.cn/services/calenderandholidays/tradinghours/)，铜连续夜盘为 21:00—次日 01:00，白银至次日 02:30，螺纹钢至 23:00；[SimNow 产品与服务](https://www.simnow.com.cn/product.action)也列有相应夜盘安排，实际登录和推送仍以探针结果为准。夜盘归属后续交易日，不能将 TradingDay 强行改成当晚自然日；[上期所交易时间](https://www.shfe.com.cn/services/calenderandholidays/tradinghours/)说明某交易日夜盘从上个工作日 21:00 开始。

夜盘只读诊断命令见 [CTP_CU_NIGHT_PIPELINE_PROBE.sh](../../doc/evidence/commands/CTP_CU_NIGHT_PIPELINE_PROBE.sh)。该命令使用原生 MD、正式 CtpLiveDataFeed、真实 QuoteTick / TradeTick 转换与 TradeTickBarFeed 分钟聚合；默认采样 cu2612 600 秒，不连接 TD、不读取或恢复 MO 状态、不报单，不查询指数或参考数据库，也不运行 08 Greeks/风控/目标逻辑。CU 元信息按[上期所铜期货细则](https://www.shfe.cn/regulation/exchangerules/productrules/202512/t20251231_829954.html)固定最小价位 10 元/吨、乘数 5 吨/手，价格/数量精度均为 0。选择的是此前账户查询已出现的合约，不登记为当前主力；若 SimNow 没有该合约有效行情，应先核对在交易的 CU 合约。脚本只接受 CU 期货代码，不能将 AG/RB 代入而继续使用铜的元信息。

将这个命令文件复制到运行机同名路径后，在 21:05 左右启动：

```bash
bash doc/evidence/commands/CTP_CU_NIGHT_PIPELINE_PROBE.sh cu2612 600
```

命令每 30 秒输出 CU_PIPELINE_PROGRESS，结束输出 CU_PIPELINE_RESULT 并写入 `tests/results/ctp-cu-night-probe/sample-<ns>/summary.json`。记录原始回调次数、最大间隔（含末次静默）、成功转换的深度事件/接收年龄、Quote/Trade/Bar 数量、单次原始回调最大耗时、采样队列峰值、进程 CPU、控制循环最大间隔和源健康变化。健康快照中的 last_event_received_ns 只在健康状态迁移时更新，本探针因此从原始回调计时和 depth_observations 取实际时效，不把冻结的状态迁移快照当成每份行情的最新接收时间。Ctrl+C 或 SIGTERM 可结束采样并保存诊断报告；断开前先取健康和原始观测，避免停机清空后丢失现场。

该探针的结果只验证 CU 单合约公共行情链路和采样窗口，不等价于 105 个 MO/IM 的负载、DolphinDB 控制线程调用、08 原生交易/恢复或集合竞价阶段通过。08 固定为股指认购、同月股指期货及指数估值，不能直接通过 option-product/future-product 将它换为商品策略；完整 08 日盘问题仍须在对应交易阶段继续定位。原 MO 空 1 手状态维持独立，当前固定身份只支持同一执行日恢复，次日直接改日期并使用旧文件会被身份校验拒绝。

#### 2026-10-14 开盘后同连接全链／缩减／全链诊断

[OPTION_VEGA_RAW_ABA_PROBE.py](../../doc/evidence/commands/OPTION_VEGA_RAW_ABA_PROBE.py) 在启动时只读 DolphinDB 当日 MO/IM 基础条款，复用 `LiveOptionReferences` 的有效认购及同月期货筛选，随后关闭参考源。它不读取昨天状态、不恢复策略、不连接 TD、不报单；采样阶段不查询指数 DFS，也不执行标准 Tick 转换或分钟聚合。用户已补齐三段结果及切换确认，完整对比出现超过 30 秒的原始回调空档，不登记为正式行情验收通过。

将该 Python 文件同步到运行机同名目录，先停止其他使用同一账号的行情诊断或 08 进程，避免额外会话影响对比。在项目根目录执行：

```bash
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_BROKER_ID=9999
export CTP_PRODUCTION_MODE=true
python -u doc/evidence/commands/OPTION_VEGA_RAW_ABA_PROBE.py \
  --day 20261014 --held MO2611-C-7400 --phase-seconds 300
```

账号和数据库配置使用项目 `.env`，不把密码写入命令。三段依次为 FULL_1（今日全链）、REDUCED（该期权及 IM2611）、FULL_2（重新订阅今日全链），每段 300 秒，另有读取、登录和切换耗时；全程同一个 MD 连接，期权及期货两腿不退订。每次切换等待全部订阅/退订响应；拒绝、响应超时或前置断开会中止。合约数按今日参考源实际结果记录，不固定为昨日 105 个。工作日检查不代替交易所节假日日历，仍须确认当天已开盘。

启动时为三段及准备预留 1200 秒，上午须早于 11:10、下午须早于 14:37；读取条款后重新核对余下预算，各采样段也检查实际交易阶段边界。要求 `--day` 等于本机上海日期，下午在期权 14:57 集合竞价前完成；若耗时超出预算，边界检查会停止，不能把部分采样当成完整对比。Ctrl+C/SIGTERM 中止采样并释放 MD，保存已有分段结果；登录/切换等待最多每 0.2 秒检查停止标志，数据库准备阶段则须等待当前查询返回。

每 30 秒输出 `ABA_PROGRESS`，每段结束输出 `ABA_PHASE_RESULT`，最终输出 `ABA_FINAL` 和 `tests/results/option-vega-raw-aba/sample-<ns>/summary.json`。每段统计 ALL、MO2611-C-7400、IM2611 的原始回调数、包含首尾静默的最大接收间隔、最后源时间和最新源事件年龄；零回调也记录整段静默。`wrong_trading_day_ticks` 标记源交易日不符，`unexpected_ticks` 标记缩减后仍到达的范围外回调。`status=completed` 仅表示三段采样完成，不表示时效检查通过；应核对各段间隔与源时间。

如果两个全链段都有长间隔、缩减段明显连续，可进一步怀疑订阅负载，但盘中推送本身会变化，仍不能凭单次对比确定因果。若三段原始接收都稳定，再做正式 Feed/Bar 与指数轮询的对照，排查转换、分派、线程竞争和数据库调用；此探针本身没有覆盖这些层。昨日 MO 空 1 手的检查点仍须保留，当前固定恢复身份不能通过修改估值日直接跨日恢复交易。

本地已用无网络回调夹具检查交易阶段边界、同连接订阅/退订确认、两腿不退订、首尾/零回调间隔及断线中止；没有连接运行机或柜台。

用户先返回 FULL_2，随后从原报告补齐 FULL_1、REDUCED 及全部切换确认；最终 `ABA_FINAL` 为 completed、failure=null、cleanup_errors=[]。初次订阅 107/107、退订 105/105、重新订阅 105/105，missing 均为空。原报告路径为运行机 `tests/results/option-vega-raw-aba/sample-1791942104929305866/summary.json`；本机未独立读取该报告。

| 分段（北京时间，各 300 秒） | 订阅数 | ALL 回调数 / 最大间隔秒 | MO2611-C-7400 回调数 / 最大间隔秒 | IM2611 回调数 / 最大间隔秒 |
| --- | --- | --- | --- | --- |
| FULL_1 09:41:47—09:46:47 | 107 | 27509 / 31.550 | 414 / 31.552 | 526 / 31.575 |
| REDUCED 09:46:47—09:51:47 | 2 | 1035 / 1.940 | 443 / 5.016 | 592 / 1.940 |
| FULL_2 09:51:50—09:56:51 | 107 | 29467 / 13.910 | 439 / 17.231 | 548 / 17.667 |

全部源交易日均为 20261014，wrong_trading_day_ticks=0，unexpected_ticks=0。FULL_1 的 ALL 已出现 31.55 秒原始回调接收空档，超过昨日 30 秒静默阈值；两腿最大间隔数值接近，与整体停顿相符，但旧探针没有记录空档发生时刻，不能确认三个最大值必定对应同一段。FULL_2 单段小于 30 秒的初步结论保留，完整三段结论已更新为“原始接收停顿已复现，缩减订阅后接收连续性明显改善”。completed 只代表采样完成，不是行情健康通过。

两次全链段均比缩减段更不连续，支持订阅负载关联；盘中推送变化、线路、原生回调、服务器资源及其他同时运行的会话尚未隔离，具体根因未确认。停顿可在不执行正式转换、Bar 聚合和指数轮询的探针中出现，故这些层并非这次原始空档发生的必要条件；仍不能证明它们在昨日正式会话中没有影响。各段末尾源年龄与 30 秒进度快照都不代表整个窗口的最大源年龄。精简证据见 [OPTION_VEGA_RAW_ABA_20261014.json](../../doc/evidence/live/OPTION_VEGA_RAW_ABA_20261014.json)。

增强版探针 diagnostic_version=2 保持同一连接及三段订阅方式，每次原始接收间隔达到 5 秒时记录 `ABA_RECEIVE_GAP`：流名、空档起止接收时间、前后源时间和下一份行情的源事件年龄。日志由主线程在回调锁外输出，原始回调中不打印；每段最多保留最近 64 条事件，receive_gap_count 按 ALL/两腿分别累计，因此同一次整体停顿可能对应三条。末尾静默仍由原有字段记录，尚未结束的空档不产生 BETWEEN_CALLBACKS 事件。

分段 diagnostics 还记录主线程循环最大间隔及超过 1 秒的起止时间、回调获取统计锁的最大等待、进程 CPU 秒和实际经过秒。若回调空档期间主线程持续约每 0.2 秒运行，更应查上游推送、网络或原生回调线程；若二者同时长时间停止，则更应查进程调度、GIL/原生阻塞或输出阻塞。两类现象都是定位线索，不能单独证明根因。CPU 用时属于整个进程（包括原生线程），不是宿主机总体 CPU；`actual_elapsed_seconds` 可显示采样控制超时，而业务采样仍截至预定 300 秒。增强版尚未取得线上结果。

将同一探针文件更新到运行机后，仍执行上述命令，下一轮重点返回 `ABA_RECEIVE_GAP`、三段 `ABA_PHASE_RESULT`（含 diagnostics）和 `ABA_FINAL`。保持其他同账号策略/探针停止，并记录服务器是否同时有其他高负载任务，减少对比干扰；不通过放宽超时阈值将故障登记为修复。

#### 订阅范围与订阅量诊断

当前 Live 从估值日有效 MO 条款中筛选认购 C，并要求存在同月 IM，形成固定参考合约集合；行情装配对整个集合绑定一分钟 Bar。原生 SimNow 另为这些交易合约订阅 QuoteTick 用于限价诊断，CTP 同一合约的 Quote/Trade 请求共用符号订阅，不代表订阅了两份合约。本次参考报告为 101 个认购、4 个同月期货，因此是 105 个市场合约；指数 000852 来自 DolphinDB，不占 CTP 订阅。它不是所有品种或认购/认沽全部期权，但也尚未按选约到期月、执行价或持仓状态缩减候选行情订阅。

订阅量可能增加推送、转换、回调和 CPU 压力。10 月 13 日持仓合约超时不足以单独证明数量影响；10 月 14 日同连接三段原始对比新增订阅负载关联证据，但具体停顿原因尚未确定。`CtpLiveDataFeed.on_depth_market_data` 成功转换后先更新逐合约事件/接收时间，再将派生 Tick 入队；仅后端分派或 Bar 队列积压不会直接推迟这项原始观测更新时间，不过回调本身、转换开销及线程竞争仍可能受负载影响。公共队列默认容量 100000，溢出会记录“队列满，丢弃实时事件”及 sticky QUEUE_OVERFLOW；本次提供片段没有这类证据。

定位时应在可比交易时段对持仓 MO2611-C-7400 进行仅 MD、不报单的单合约与全链采样，记录原始事件推进、接收最大间隔、事件到接收延迟、转换错误、处理耗时、队列积压和 CPU；当前 stage 4 探针只记录首/末行情及总数，不能据此证明采样期间没有超过 30 秒的间隔。单合约也出现长间隔更支持合约推送/线路问题；单合约连续而全链出现广泛延迟及 CPU/队列压力时，才有订阅负载问题的对比证据。

后续可采用选约前订阅必要候选月与候选执行价区间、选约后保留有仓位/在途/目标的腿及同月期货的分阶段订阅。未观测价格的候选不能直接按目标 Delta 精确筛选；预筛规则需要先有指数/期货及可用价格，并保留策略选约所需覆盖。当前 08 CLI 没有订阅数量或执行价窗口参数，该优化需改行情装配和订阅生命周期；不能通过删参考条款、截断合约字典或移除持仓腿来临时缩减，否则可能破坏恢复身份或风险管理。以上为待实现的优化方向，不登记为已经支持或已修复本次超时。

精简证据见 [OPTION_VEGA_REALTIME_SIMNOW_FILL_20261013.json](../../doc/evidence/live/OPTION_VEGA_REALTIME_SIMNOW_FILL_20261013.json)，后续恢复失败单独记在 resume_followup。证据来自用户返回的报告字段，本机没有连接柜台、直接读取运行机原文件或运行新的交易。已确认原会话实时期权开仓成交、策略归属、三方仓位一致及停机清理；恢复启动标记已有证据，但恢复后持续健康运行失败。成交价格/柜台单号、恢复 generation 尚未提供，实际平仓、非零期货对冲成交及主动/定时正常停止不登记为通过范围。仅 1 手期权时 Delta 按期货乘数折算并取整后可能为 0，没有期货订单不单独说明对冲链已通过或失败。

### 2026-10-12 返回的文件回测成功记录

用户在远程 `uv-nautilus` 环境运行 `run_backtest.py --option-product MO --future-product IM --index-code 000852 --start-day 2026-09-01 --end-day 2026-09-09 --bar-timestamp end --log-level WARNING`。日志报告实际 7 个交易日、105 个认购合约、4 个同月期货；`frames=1687`、`entries=7`、`orders=32`、`fills=33`。回测阶段 75.212 秒，main 总耗时 88.428 秒，报表路径为运行机 `pro/demos/08_option_vega/results/70d4ca7d-e457-4f78-a7f2-0b2a4f7d7bed`。

日志到达最终结果校验完成和 main 完成，按当前入口的检查可推知没有残留策略仓位/在途数量、回报错误且已触发末日清仓；本次尚未单独读取其 `summary.json`、逐笔报表或输入文件，因此不补记收益、指数真实目录或逐笔正确性结论。它验证的是这个区间的文件回放与模拟成交链路；此前 9 月 1—11 日运行在 9 月 10 日缺少 IM2609 新鲜报价，仍需另行定位。

原生成功会话的状态文件 `demos/08_option_vega/results/replay-ctp-20261012-114149.json` 是交接记录中的远程原文件，应保留；本机不假设它存在，也不覆盖或作为新会话文件使用。最新代码交接还记录 PF2026101207 的冻结执行组件包装修复及本地装配回归，与此前版本的测试证据分别解读。

阅读依据与详细记录：

- [文档导航](../../doc/README.md)、[统一策略架构](../../doc/architecture/STRATEGY_ARCHITECTURE.md)、[公共输入/运行设计](../../doc/strategies/DEMOS_INPUT_RUNTIME_DESIGN_2026-10-04.md)。
- [08 回测与 Live 改造](../../doc/HANDOFF/live/HANDOFF_OPTION_VEGA_2026-10-11.md)、[日历、Recording、Replay 及原生成交交接](../../doc/HANDOFF/live/HANDOFF_OPTION_CALENDAR_RECORDING_2026-10-12.md)。
- [声明式指数特征接入](../../doc/HANDOFF/gaps/HANDOFF_GAP08_DECLARATIVE_2026-10-12.md)、[Replay 原生成交证据](../../doc/evidence/live/OPTION_REPLAY_CTP_NATIVE_FILL_20261012.json)。

### 缺失分钟诊断日志

OptionMarketInputs日志记录指数DFS轮询启用、分钟MISSING、查询投影完成、查询异常、
等待超时跳过及帧必需输入缺失。相同来源/分钟/状态不重复打印，状态记录上限64。
查询投影完成只表示查到数据并尝试发布，不代表已经通过帧接收期限。
CTP期权/期货缺失Bar不自动补查DolphinDB；日志不改变查询、等待、决策或定价规则。
