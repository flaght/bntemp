# HybridTransformer 主力合约策略层（按品种外部配置）

这是独立于 `01_main_ema` 的策略，不修改 EMA。复用其结构：按事件时间取得当前主力，只处理主力的已完成分钟，将模型信号变成**逻辑目标仓位**，由 Bomber 的动态路由和执行风控决定真实合约、委托及成交。策略本身不调用 CTP，也不在导入时连接账户。`run_backtest.py` 提供历史模拟入口，每次选择一个已配置品种独立运行，多个品种可分别提供发布模型、特征方向文件和信号配置。

## 已实现的边界

- `model_adapter.py` 在初始化时加载当前目录的 `flow.LiveFlow`。该 Flow 完成特征计算、15 期时序标准化、模型输入、多个 seed 等权预测和 Lumina 信号函数；`model.py` 原样复用 Mizar `lib/nn005/model.py` 的 `HybridTransformerRegressor` 定义，保持发布 checkpoint 的网络结构。推理仍需安装 PyTorch、KiChaos、Lumina、Ultron 依赖。
- 行情解析直接使用框架的 `FixedInstrumentBarParser`，由底层 `MappedBarParser` 统一校验并先发原生 `BAR`、再发伴生 `CUSTOM_BAR`，Demo 不再维护单独的解析器。显式配置 `value=value_column`、`open_interest=open_interest_column`、`trade_time="source_timestamp"`、`preserve_raw_ohlcv=True`，伴生字段保留原始 OHLCV 精度、`value`、规范名 `open_interest` 和原始 `trade_time`；`available_nanoseconds="available_ns"` 指定可用时刻。默认不配置这些选项时仍只发标准 `BAR`，兼容 EMA。
- `signal_config.py` 从外部 JSON 按品种读取 `signal_id`、`signal_method`、`signal_params` 和 `hold_bars`；策略和适配器不再固定品种或信号编号。方法名必须由 Mizar 的 `SIGNAL_FUNCTIONS` 注册，函数参数原样传给 Workflow。信号输入仍为原始 `predicted_z`。
- `strategy.py` 每根有效主力分钟 bar 都预测，并推进 `holding.py` 中 `PeriodHoldings(holding_bars, max_contracts)` 批次账本。每批在建立后的第 `hold_bars` 根有效主力分钟 bar 到期；中间信号不会覆盖旧批次。预热返回 `None` 时不新开仓，但仍处理已有批次到期。
- `max_contracts` 是全部未到期批次的**绝对手数总上限**，不是每批上限。固定分给 `hold_bars` 个轮换槽位。以 5 周期为例，10 手为 `(2,2,2,2,2)`，11 手为 `(3,2,2,2,2)`，1 手为 `(1,0,0,0,0)`。零预算槽位不交易；总手数少于持仓周期时，部分槽位预算为零，无法每分钟都建立非零整数手批次。预算不因多空净额抵消而重复使用。
- 信号在当前槽位预算内按 `round_half_up(abs(signal) × slot_capacity)` 量化。每槽 2 手时，连续信号 0.3 对应多头 1 手，离散信号 -1 对应空头 2 手。连续仓位的取整误差需要独立评估。
- 账户目标等于全部未到期批次按真实合约分组的净和。多空批次可以净额抵消，但各自的到期义务仍保留；到期时重新计算剩余敞口。相同合约目标不变无需重复提交，但账本必须记录旧批次到期、新批次建立。
- `rebalance_minutes` 现在仅控制**允许建立新批次的分钟时点**，默认 1；到期处理始终执行，不受此开仓间隔或有无新预测影响。持仓期限由各品种配置中的 `hold_bars` 决定，也可通过 `--hold-bars` 显式覆盖；应与对应发布模型的预测标签周期一致。
- 通用期货数值提取位于 `bomber.framework.market.futures.futures_bar_values(event, contract_multiplier=...)`，返回新的 OHLCV、`value`、`openint`、`vwap` 字典。该函数优先使用伴生原始字段，兼容 `open_interest`，并校验有限值、正价格、正成交量、OHLC 区间与已提供的 VWAP；不依赖 P/RB、模型或主力角色。策略负责主力筛选及添加 `trade_time`、`code`。累计成交数据和成交额单位须由上游统一。
- 该辅助函数只由需要此口径的策略显式调用。OHLC 必须来自已完成、未复权的合约行情；零成交分钟及复权连续价格不适用。它不会生成 `value`，也不会把 `turnover` 自动视为本周期成交额。现有 DolphinDB 转换器保留 `turnover`，且支持累计成交量，接入前须分别核实并统一成交额、成交量的周期与单位；不能只改字段名后直接套用。非法数值及乘数统一抛出 `ValueError`。
- 模型收到的 `value` 必须是**本分钟成交额**，`volume` 必须是本分钟成交量；按 `vwap=value/(volume×contract_multiplier)` 计算。`openint` 来自 `openint` 或 `open_interest`。`contract_multiplier` 从当前品种的合约基础资料取得，不得使用持仓量替代。零成交量或无效字段会拒绝产生新目标，不做零填充。
- 输入 `CustomBar.factors` 必须保留原始 `trade_time` 标签，并提供 `value`、`openint/open_interest`；建议同时放入研究口径完整精度的 `open/high/low/close/volume`。缺这些可退回 Bomber 标准 Bar 的 tick 精度 OHLCV，但正式使用前必须与研究输入逐字段对齐，否则不能宣称模型预测一致。

## 品种与信号配置

新增 `signal_config.example.json`，其中 P、RB 仅为已有研究参数的示例，代码不会自动回退到它们。使用时显式传入 `--signal-config /ABS/PATH/TO/signals.json`。JSON 结构为“品种名 -> 该品种配置”：

```json
{
  "P": {
    "signal_id": "1101",
    "signal_method": "linear_signal",
    "signal_params": {"roll_num": 0, "threshold": 0.05},
    "hold_bars": 5
  },
  "RB": {
    "signal_id": "1301",
    "signal_method": "threshold_signal",
    "signal_params": {"roll_num": 0, "threshold": 0.02},
    "hold_bars": 5
  }
}
```

其他品种在 JSON 中新增对应条目即可；信号编号由研究配置自行命名，不限定为 1101/1301。每个品种的信号函数、参数和持仓周期独立配置，缺失品种或未注册函数会明确报错。接入其他品种时还须提供支持该品种的 Mizar 发布模型、特征方向和合约资料；可配置不代表模型已验证其效果。单次运行仍选择一个品种，不自动创建多品种共享账户组合。

## 接入示意

以下是策略对象的装配边界，不是可直接启用真实报单的命令。先准备已经核验的研究口径 `CustomBar` Feed、角色快照、动态主力路由和合约乘数，再进行纸面/模拟回测。

```python
from importlib import import_module
from decimal import Decimal

demo = import_module("demos.10_hybrid_transformer.strategy")
adapter = import_module("demos.10_hybrid_transformer.model_adapter")
signals = import_module("demos.10_hybrid_transformer.signal_config")
selected = signals.load_product_signal("/ABS/PATH/TO/signals.json", "P")

predictor = adapter.MizarWorkflowAdapter(
    release_path="/ABS/PATH/TO/RELEASE",
    directions_file="/ABS/PATH/TO/FEATURE_DIRECTIONS.csv",
    code="P", signal_id=selected.signal_id,
    signal_method=selected.signal_method, signal_params=selected.signal_params,
    device="cpu",
    history_bars=1500, min_raw_bars=300,
)
config = demo.HybridTransformerConfig(
    product="P", venue="DCE", signal_id=selected.signal_id,
    max_contracts=10, hold_bars=selected.hold_bars, rebalance_minutes=1,
    contract_multiplier=multiplier_from_fut_basic,  # fut_basic.contMultNum
)
strategy = demo.HybridTransformerStrategy(
    "p-transformer", roles, predictor, config,
    target_keys=target_keys_from_execution_routes,  # 实际合约 -> 静态目标键
)
```

切换品种时选择该品种的 JSON 条目，并使用对应交易所、合约乘数、模型发布路径和特征方向文件；信号编号、函数和参数都来自所选条目。

Runner 用 `DataBinding(..., DataType.CUSTOM_BAR, "1-MINUTE")` 接收模型事件；模拟执行桥仅消费同一 Feed 的 `DataType.BAR`，不能把伴生 `CUSTOM_BAR` 再送入撮合。框架 `MappedBarParser` 保留原生行情先、策略事件后的顺序，两个事件共享同一个 Bar 及其时间戳。

历史入口为每个真实合约注册静态 `ExecutionRoute`，目标键为 `<product>_main:<symbol>`，并把 `target_keys` 映射交给策略。主力换月只影响新批次的合约选择，旧批次在原合约保留至到期，不能将整个净仓位提前迁移到新主力。自行装配且未提供映射时仅支持单一合约，换约会明确报错。该 Demo 仍只提供历史入口，不提供实盘报单入口。

## 持仓周期行为示例与时间口径（以下以 hold_bars=5 为例）

`max_contracts=10` 且每分钟信号为 1，前五根可开仓 bar 的总目标依次为 `2、4、6、8、10`；第六根 bar 上第一批 2 手到期、新一批 2 手建立，总目标仍为 10，账户无需先平再开，但新批次的期限已重新开始。若第一根信号为 1、随后全为 0，则前五根目标一直为 2，第六根才变为 0。

若第一根信号为 1、第二根为 -1、此后没有新批次，净目标依次为 `2、0、0、0、0、-2、0`。两个批次分别在第六、第七根 bar 到期。账户暂时为零不表示两个批次已被删除。

周期按实际收到并通过校验的主力分钟 bar 计数；重复事件、非主力行情不推进。休市和缺失分钟不补造 bar。该时钟按 `hold_bars` 条未来有效分钟记录推进，跨休市可能超过相应数量的墙钟分钟。批次以信号 bar 计时，不以 `FillEvent` 重新计时；原生回测的下一行情成交顺序、部分成交、滑点、VWAP 标签偏移和费用仍需通过实际成交回测验证。

账本上限约束的是所有未到期批次的绝对手数及输出目标。真实在途订单、拒单和跨合约换仓的过渡持仓仍由执行层管理，不能把目标上限当作已完成真实账户验收。历史区间结束时不伪造未来 bar 或强制提前到期；未到期批次会保留在 `summary.json` 中。

## 历史模拟运行

在含 Bomber 编译扩展和 PyTorch、KiChaos、Lumina、Ultron 依赖的环境运行；本地 Flow 和模型定义无需再通过 Mizar 根目录导入。以下用占位路径示意；`--bars-dir` 的每个文件须包含本分钟 `value` 与 `openint`，基础资料的 `contMultNum` 用于 VWAP 计算：

```bash
python demos/10_hybrid_transformer/run_backtest.py \
  --product P \
  --signal-config /ABS/PATH/TO/signals.json \
  --start-day 2025-05-06 --end-day 2025-05-09 \
  --bars-dir /ABS/PATH/TO/MINUTE_BARS \
  --contract-struct /ABS/PATH/TO/CONTRACT_STRUCT.feather \
  --fut-basic /ABS/PATH/TO/FUT_BASIC.feather \
  --release-path /ABS/PATH/TO/P_RELEASE \
  --directions-file /ABS/PATH/TO/P_FEATURE_DIRECTIONS.csv \
  --bar-timestamp start \
  --max-contracts 10 --rebalance-minutes 1 \
  --history-bars 1500 --min-raw-bars 300 \
  --report-dir /ABS/PATH/TO/REPORTS
```

切换品种时修改 `--product`，并提供对应 release、方向文件和合约资料；信号规则从 `--signal-config` 的对应条目读取。`--hold-bars N` 可覆盖所选条目的周期，未传时直接使用条目中的 `hold_bars`。`--bar-timestamp` 必须明确符合源数据“分钟起点/终点”的实际定义，不能照搬示例。若源字段为 `open_interest`，加 `--open-interest-column open_interest`。输出目录包含 `signals.csv`（每根有效 bar 的预测、槽位预算、到期/新开手数、未到期绝对手数及逐合约目标，预热预测为空）、`batches.csv`（每批建立/到期事件、合约与起止 bar 编号）、订单、成交、持仓、账户及输入审计文件。短区间回测仍须提供足够的前置 bar 完成模型预热，不能把测试期的第一根直接当充分历史。

## 启用订单前的验收

1. 核对每根原始 `trade_time` 是训练数据使用的时间标签，完整分钟可用时间不早于收盘时间；验证 `value/volume/contract_multiplier` 得到的 VWAP 与研究数据一致，特别检查夜盘、换月和零成交分钟。
2. 对相同原始 bar 逐行比较 Mizar 研究特征、三 seed `predicted_z`、融合 `predicted_z` 和所配信号函数的输出。允许误差阈值要先定义；跨阈值导致的信号状态差异应为零或经明确批准。
3. 以整数手量化后的目标做独立回测，计入手续费、滑点、换手、最大持仓和换月成本。此前连续仓位评估的收益率不可直接充当真实手数回测收益。
4. 先运行记录目标/模拟执行，核对成交回报及仓位；此目录没有 `run_live.py`，不会启动 CTP 实盘报单。实盘入口须沿用 Bomber 的账户锁、行情健康、恢复检查点、订单上限和显式报单门控，不能直接复制 EMA 的启动参数。

当前实现**策略层、Mizar 预测适配器、已准备行情的解析器及历史模拟入口**，但尚未在目标环境的真实行情文件上完成集成验收。Mizar Workflow 每根 bar 重新计算其保留窗口，长区间运行可能较慢；先跑短区间核对信号与耗时。在线 Runner 仍未接入。特别是主力原始合约 bar 若与训练时的连续行情构造不同，不可直接把模拟结果视为投研回测复现。
