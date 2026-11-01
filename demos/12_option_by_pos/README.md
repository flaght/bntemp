# 目标持仓回测：期货 M1 + 期权 M5

入口为 `run_backtest.py`，策略为 `strategy.py`。使用当前 framework 的行情回放、目标规划、风控、撮合及报表，替代 `3rd/strategies/kd_option_by_pos` 中依赖不完整的旧运行入口。默认目标文件仍指向旧目录的 `positions.csv`。

## 输入目录

```text
ctp/
  role/
    fut_basic.feather
    opt_basic.feather
  kline/fut/20221226/IM2301_20221226.feather

外部期权目录/
  20221226/MO2301-C-6000_20221226.feather
  20221226/MO2301-P-6000_20221226.feather
```

`--data-root` 指向 `ctp`，期货默认读取其 `kline/fut`，基础资料读取 `role`。`--opt-dir` 单独指定你转换完成的期权目录；也可通过 `--fut-dir`、`--fut-basic`、`--opt-basic` 覆盖对应位置。本示例只采用命令行路径和上述派生路径，不读环境变量路径。

你的 `/workspace/data/temp_bomber1/ctp/role/opt_basic.feather` 可直接使用：`symbol`、`contractType`、`strikePrice`、`contMultNum`、`varTicker`、`exchangeCD`、`listDate`、`lastTradeDate`、`expDate`、`tickNum` 均由现有基础资料解析器处理，支持认购和认沽。

行情列沿用转换结果：`datetime, open, high, low, close, volume`，可保留 `symbol, trade_date, open_interest`。持仓量不参与本策略下单或结算。文件日期及合约身份必须一致。

## 运行

在项目 `pro` 目录、装有 Bomber 编译扩展及 framework 行情扩展的 Python 环境下运行：

```bash
python demos/12_option_by_pos/run_backtest.py \
  --data-root /workspace/data/temp_bomber1/ctp \
  --opt-dir /workspace/worker/pj/Chrono/nautilus_trader_demo/pro/temp/data/to/opt \
  --start-day 2022-12-26 \
  --end-day 2022-12-30 \
  --require-fills
```

默认从空仓开始，仅加载请求区间出现非零目标的合约。截取日期不会自动恢复区间前的持仓；需要完整复现时应从目标文件的起始日运行。`--positions` 可以指定另一份目标文件。

你之前的源数据存在 OHLC 越界，转换器保留了原值；框架默认仍会拒绝这类 Bar。若接受使用完整 OHLC 极值扩大高低价，并过滤包含零价格的市场 Bar，可显式加：

```bash
  --option-ohlc-policy expand_range --option-zero-price-policy drop
```

扩大区间保留开盘和收盘价；过滤零价格可能导致信号时点缺行情。处理数量和原值样例写入输入诊断，源 Feather 不修改。零成交量 Bar 保留，不保证具有可交易流动性；本例使用基础 Bar 撮合模型。

## 多周期与目标语义

- 期货声明 `1-MINUTE`，期权声明 `5-MINUTE`，共用一个按可用时间排序的回放时钟。M5 不补成 M1。
- 默认两类行情均为北京时间结束标签，例如 M5 的 `09:35` 表示已经完成的 `09:30–09:35`。起点标签需分别设置 `--futures-bar-timestamp start` 或 `--options-bar-timestamp start`，不能重复右移已经转换为结束标签的数据。
- 在目标时点等待该批显式目标合约的同时间 Bar 到齐后，仅提交一次。缺失默认报错；`--missing-signal-policy skip` 会记录并跳过，适合排查数据。
- 每批目标中省略的期货归零；省略的期权保留上次目标；显式 `0` 表示平仓。沿用旧策略的这项差异。
- 默认 `next_market`，等待本合约下一根有效 Bar，在其收盘盘口撮合。期货下一根是 M1，期权下一根是 M5；其他合约的 Bar 不推进这笔订单的等待根数。
- `--execution-delay-bars N` 可增加等待根数。`--execution-timing native` 使用原有原生时序，可能受同一时间戳事件顺序影响。
- 默认按文件实际时点执行。仅在对比旧版时使用 `--legacy-close-early`，把文件 `15:00` 目标提前到 `14:58`，允许用最后已知的期权报价提交意图；订单仍等待本合约新 Bar。

## 到期现金结算

`opt_basic` 提供上市日、最后交易日、行权价等条款，**不包含最终结算价**。跨越期权到期日时，还需提供 `--option-settlements`。加载的到期合约必须有对应的最终结算记录；缺失会在启动前报错。

标准文件支持 CSV、Parquet 或 Feather，字段如下：

| 字段 | 含义 |
| --- | --- |
| `symbol` | 合约代码，可带 `.CFFEX` |
| `last_trade_date` | 最后交易日，必须与 opt_basic 一致 |
| `settlement_price` | **该期权每点最终现金结算价**，允许为 0 |
| `available_at` | 可选，该结算资料实际可用的时间，推荐带 `+08:00` |
| `fee_per_contract` | 可选，每手结算费用，覆盖命令行默认值 |

兼容旧 `opt_eod.parquet` 的 `tradeID,lastTradingDate,settlePrice` 字段；存在 `TradeDate` 时，只使用与最后交易日相同的记录。普通交易日的日结算价不能充当到期终值。

```bash
python demos/12_option_by_pos/run_backtest.py \
  --data-root /workspace/data/temp_bomber1/ctp \
  --opt-dir /实际路径/opt \
  --positions /实际路径/positions.csv \
  --start-day 2022-12-26 --end-day 2023-01-20 \
  --option-settlements /实际路径/opt_eod.parquet
```

没有 `available_at` 时，本示例假设最后交易日 `16:00` 可用，可通过 `--settlement-available-time` 声明其他时间；这不是对实际发布时间的推断。结算可用时间超过回测结束日时需延长回测区间。

若手里只有官方指数交割结算价，认购终值为 `max(指数交割结算价 - 行权价, 0)`，认沽反之。公共函数 `option_settlement_price()` 提供这项计算；加载器不会用期货价格、指数收盘价或期权最后一根 Bar 猜测终值。

生命周期处理如下：

1. 最后交易日 `15:00` 起拒绝新的期权订单。此前已提交的委托允许用 `15:00` 这根已完成 Bar 撮合。
2. 全部同时间行情之后（`15:00 + 1ns`）撤销该期权剩余委托，包括等待下一根 Bar 的意图；其他合约继续运行。
3. 最终资料可用时，以实际剩余仓位产生原生结算订单及成交事件，由原生 ExecEngine 更新现金、已实现盈亏和持仓，同时同步 framework 的账户及策略归属。终值允许为零，也保留比交易 tick 更细的精度。
4. 最后市场 Bar 后没有新行情，也会通过控制时钟推进结算；控制数据不修改市场价格，不撮合普通订单。已平仓的合约不产生额外结算成交。

实现位于 Python framework，未改 Bomber/Cython 引擎源码。期权仅在显式 `CASH_SETTLEMENT` 模式由该模块接管生命周期，原有期权构造默认行为保持兼容。

本例采用基础 NETTING 保证金账户，单策略归属；保证金按固定比例近似，不模拟柜台组合保证金、双向持仓或逐日资金划转。它验证目标交易和到期盈亏，不涉及 Greeks、定价曲面或实盘报单。

## 费用与输出

期货默认按 `fut_basic` 手续费条款；期权普通交易费默认每手 `0.15`，可通过 `--option-commission-per-contract` 修改，并沿用公共交易手续费系数。结算费默认 `0`，通过 `--exercise-fee-per-contract` 或结算文件单独声明，单位均为元/手；不会再次套普通交易费系数。

每次运行写入 `results/<run_id>/`，包含原生 `fills.csv, orders.csv, positions.csv, account.csv`，兼容旧成交列名的 `fills_output.csv`，目标提交 `signals.csv`、真实残仓结算 `settlements.csv`、输入诊断、手续费诊断及 `summary.json`。摘要包含 M1/M5 绑定、实际加载行数、未触发信号和两层最终仓位。

## 验收

独立 Python 回归（需 pandas、pyarrow）：

```bash
python -m unittest tests.test_option_by_pos_framework tests.test_option_expiry_module tests.test_multiperiod_inputs tests.test_simulation_market_timing tests.test_futures_fees
```

在装有编译扩展的 `uv-nautilus` 环境运行：

```bash
python -m unittest tests.test_option_by_pos_native -v
```

原生验收构造真实 Feather，验证 M1/M5 分别成交、最后一根 Bar 撮合、到期后无市场数据仍结算、终值 `0` 和 `120.13`、原生账户现金、已实现盈亏及两层仓位归零。无编译扩展时明确跳过，不能视为原生撮合验收通过。
