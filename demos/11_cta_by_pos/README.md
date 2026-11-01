# 日目标持仓 CTA

使用与 `01_main_ema`、`10_hybrid_transformer` 相同的框架。目标文件读取、信号判断和目标提交集中在 `strategy.py` 的 `CtaByPosStrategy` 中，公共数据准备、合约、费用、风控和执行组件完成回测。

## 策略规则

`positions.csv` 提供每日完整目标仓位，格式保持原文件：

```csv
timestamp,instrument_id,instrument_type,target_qty
2026-05-06 09:40:00,IF2606,FUTURE,2
2026-05-06 09:40:00,IH2606,FUTURE,-1
2026-05-07 09:40:00,IF2606,FUTURE,0
```

- 按日期读取目标；同日同合约最后一行覆盖。文件中时间部分不改变统一触发时间。
- 北京时间 `09:40` 收到第一根 Bar 时提交当天完整组合，同日不重复提交。可配置 `--signal-time`。
- 数量表示最终仓位：正数多头、负数空头、零平仓；原样保留零目标和与昨日相同的目标。
- 使用 `TargetUpdateMode.REPLACE` 表达完整组合，上一组合未出现的目标由框架归零。
- 没有目标记录的日期不调仓；缺少指定分钟 Bar 时不追发。
- 保留原策略的股指期货过期目标过滤：合约月份第三周五 15:00 后跳过非零目标，零目标保留。全部目标被过滤时不提交空组合。

策略只处理本次原文件使用的 IC/IF/IH/IM 期货。每个交易日独立提交，仓位轧差、在途委托、反向调仓、成交、费用和合约生命周期全部由现有框架处理。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `strategy.py` | 策略配置、读取目标文件、提供合约清单、判断信号并提交目标 |
| `run_backtest.py` | 数据准备、标准框架装配、报表输出 |
| `compare_results.py` | 可独立使用的结果比较工具，不参与策略运行 |

`run_backtest.py` 创建策略后，通过 `get_insts()` 获取需要注册的合约和行情清单。持仓文件由策略自身读取，信号判断和提交始终在策略的 `on_bar` 中完成。

回测使用以下公共组件，与其他 demo 保持一致：

- `resolve_futures_args`、`input_session`、`prepare_fixed_contracts`、`add_prepared_bar_source`。
- `fut_basic` 与 `make_instrument` 提供真实合约的交易所、tick、乘数和生命周期。
- `CtpFuturesBasicProfile` 与 `CommissionConfig` 提供手续费，默认从 `fut_basic` 读取。
- `build_simulation_execution` 使用标准 `NetTargetOrderPlanner`、仓位管理和风控。
- `StrategyBindings`、`assemble_strategy`、`UnifiedStrategyRunner`、`UnifiedHistoricalRuntime` 完成装配与回放。

目标文件指定的是固定真实合约，因此不需要主力角色表或复权因子。

## 运行

在已安装 Bomber 内核及 market/basic 扩展的环境中，从项目根目录运行：

```bash
python demos/11_cta_by_pos/run_backtest.py \
  --positions 3rd/strategies/kd_cta_by_pos/positions.csv \
  --start-day 2023-01-03 \
  --end-day 2026-07-05 \
  --data-root /path/to/ctp_data \
  --require-fills
```

也可分别指定 `--bars-dir` 和 `--fut-basic`。数据布局与现有 demo 相同：

```text
ctp_data/
  role/fut_basic.feather
  kline/fut/.../IF2606_20260506.feather
```

分钟文件由公共层严格校验，须含 `datetime` 或 `timestamp`，以及 `open,high,low,close,volume`。`fut_basic` 条款与手续费字段要求遵循框架公共契约。

常用参数：

| 参数 | 默认值 / 用途 |
| --- | --- |
| `--positions` | 默认引用原 `positions.csv`，只作为业务输入 |
| `--start-day / --end-day` | 必填；只加载请求区间的目标和合约 |
| `--signal-time` | 09:40，北京时间 |
| `--bar-timestamp` | end；start 标签由公共层转换为分钟结束时间 |
| `--execution-timing` | next_market；native 保留原生提交行为 |
| `--execution-delay-bars` | next_market 默认 1；等待本合约后续有效 Bar 根数，native 为 0 |
| `--starting-balance` | 15000000 |
| `--commission-source` | metadata；auto/fixed 的含义与公共费用配置一致 |
| `--commission-multiplier` 等 | 沿用公共手续费参数 |
| `--margin-init / --margin-maint` | 0.10 / 0.08 |
| `--max-quantity / --max-notional` | 100 / 50000000；供公共风控使用 |
| `--max-market-age-seconds` | 120 |
| `--report-dir` | 本目录 results，按 run_id 建子目录 |
| `--no-tearsheet` | 只生成 CSV/JSON |
| `--require-fills` | 无成交则报告失败 |

输出 `orders.csv`、`fills.csv`、`positions.csv`、`account.csv`、`signals.csv`、`summary.json`，以及框架公共输入诊断、手续费报告和可选绩效图。

`signals.csv` 用于核对策略实际提交的日期、信号时间、合约与目标数量。`summary.json` 记录撮合时序 `execution_timing`、等待根数 `execution_delay_bars`、未触发日期与过滤的过期目标。

策略在 09:40 提交目标。本示例默认 `--execution-timing next_market`，模拟执行层等待订单对应合约时间戳严格晚于信号及提交时钟的行情，完整分钟 Bar 模式按下一根有效 Bar 的收盘盘口撮合。例如 IF 在 09:40 产生目标，最早使用 IF 的 09:41 Bar；其他合约的 09:40 或 09:41 Bar 均不能放行 IF 订单。若 IF 缺少 09:41 Bar，则继续等待 IF 后续 Bar。回放结束仍未等到新行情的意图会取消并释放在途数量。

可在回测命令后增加 `--execution-delay-bars N`，选择等待本合约后续第 N 根有效 Bar 收盘盘口。分钟行情连续时，09:40 信号、N=1 对应 09:41，N=2 对应 09:42；缺 Bar 时按实际到达根数顺延，同时间戳重复事件不累计。每笔订单独立计数。N 大于 1 时仅支持 Bar 回放，且每个合约使用一种 Bar 周期。

```bash
# 在原回测命令后增加以下参数，等待下一根 Bar（默认）
--execution-timing next_market --execution-delay-bars 1

# 等待第二根 Bar
--execution-timing next_market --execution-delay-bars 2

# 保留原有提交行为，不等待
--execution-timing native --execution-delay-bars 0
```

通过代码组装时，在 `NautilusSimExecutionBackend` 构造参数中设置 `execution_timing="next_market", execution_delay_bars=2`。默认值与命令行一致：next_market 等待一根，native 不等待。

`--execution-timing native` 保留此前直接提交订单的行为，多合约同时间戳后续事件可能使订单成交。共享 Backend 默认仍为 `native`，已有 01/10 等示例不会被统一改成等待下一根 Bar。撮合时序是执行层设置，策略信号逻辑不参与等待。`next_market` 使用正式历史 Runtime 的逐事件入口，不支持一次性 `Backend.run()`。

## 与原结果对比

迁移保留每日目标生成规则，交易执行统一使用当前框架的标准行为。原 `bomber_adapter` 的批次协议、跨 Bar 延迟队列、自定义轧差下单、报表费用重算不进入本策略；运行时也不导入或执行原策略源码。

建议先核对信号，再统一执行规则后重跑两套框架。当前原适配器的 `execution.delay_bars=1` 声明等待本合约下一根 Bar close，可与本示例的 `next_market`、`--execution-delay-bars 1` 作为对比起点；仍需核查实际触发时间、成交价格及缺 Bar 行为。原适配器的 `delay_bars=0` 也可能因批次刷新产生延后时间戳，不能仅凭 09:41 成交判断它已经使用下一根 Bar 的价格。原配置和旧成交 CSV 保留原样；本示例新增的执行设置不保证与旧报表逐笔相同。

两边还应统一原始行情、Bar 时间标签、起止时刻、初始资金、手续费和滑点。先关闭随机滑点，按合约、成交时刻、方向汇总比较数量与均价，排除反手拆单和订单ID差异的干扰，再比较费用与权益。

`fills_output.csv` 额外提供与原导出相同的列名，所有值来自框架实际成交和实际手续费，便于你自行比较。也可以使用独立工具：

```bash
python demos/11_cta_by_pos/compare_results.py \
  --reference /path/to/original/fills_output.csv \
  --candidate /path/to/new/run_id/fills_output.csv \
  --output /path/to/comparison
```

工具按成交时间、合约、方向比较数量、均价、手续费，并从初始空仓重建日终净仓。它不改变策略、不调整结果，也不比较 PnL。

## 验证

```bash
PYTHONPATH=. python -m pytest tests/test_demo_cta_by_pos.py -q
```

单元测试隔离框架的目标提交接口，覆盖策略本身的目标读取、当根 Bar 触发、每日去重、缺失信号、日期选择、过期过滤和实际费用导出；完整框架回放测试需 Bomber 编译内核，无内核时明确跳过。
