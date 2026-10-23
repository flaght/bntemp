# HybridTransformer 主力合约策略层（P / RB）

这是独立于 `01_main_ema` 的策略，不修改 EMA。复用其结构：按事件时间取得当前主力，只处理主力的已完成分钟，将模型信号变成**逻辑目标仓位**，由 Bomber 的动态路由和执行风控决定真实合约、委托及成交。策略本身不调用 CTP，也不在导入时连接账户。`run_backtest.py` 提供历史模拟入口，P 和 RB 各自独立运行。

## 已实现的边界

- `model_adapter.py` 调用 Mizar 现有的 `lib.nnl005.workflow.Workflow.on_bar`。该 Workflow 完成特征计算、15 期时序标准化、20 期模型输入、三个 seed 等权预测和 Lumina 信号函数；此目录不重写这些算法。
- `market_adapter.py` 复用 Bomber 的 `FixedInstrumentBarParser`，同一行先发原生 `BAR` 给撮合，再发 `CUSTOM_BAR` 给策略。伴生字段保留原始 OHLCV 精度、`value`、`openint` 和原始 `trade_time`；数据准备阶段需提供 `datetime`（已完成分钟的事件时间）、`available_ns` 与 `source_timestamp`（训练口径的标签时间）。字段名不同可在构造解析器时显式指定。
- P 使用已固化的 `1101 / linear_signal(roll_num=0, threshold=0.05)`；RB 使用 `1301 / threshold_signal(roll_num=0, threshold=0.02)`。信号函数输入均为原始 `predicted_z`，不预先除以 0.1。
- `strategy.py` 每根主力 `CustomBar` 调用一次模型和信号；默认**每分钟**依据最新预测更新目标仓位。训练标签 `nxt1_ret_5h` 在每分钟都有一个未来五分钟收益，研究评估中的 `minute % 5 == 0` 仅用于降低相邻标签重叠，不是下单约束。`rebalance_minutes=5` 是可选的低频交易方案，不能据此声称更接近原评估收益。
- 每分钟更新的是净目标仓位，不是逐笔“持有五分钟后必须先平再开”。同合约目标不变无需交易；目标变化只交易差额。逐分钟交易得到的真实收益要从成交记录重新回测，不能把高度重叠的五分钟标签收益逐行相加。
- 期货执行使用整数手。连续信号按 `round_half_up(abs(signal) × max_contracts)` 量化。例如 `max_contracts=1` 时，`|signal|<0.5` 对应空仓；这会改变研究环境的连续仓位收益，必须在回测中重新验证。离散信号 `±1` 对应 `±max_contracts` 手。
- 通用期货数值提取位于 `bomber.framework.market.futures.futures_bar_values(event, contract_multiplier=...)`，返回新的 OHLCV、`value`、`openint`、`vwap` 字典。该函数优先使用伴生原始字段，兼容 `open_interest`，并校验有限值、正价格、正成交量、OHLC 区间与已提供的 VWAP；不依赖 P/RB、模型或主力角色。策略负责主力筛选及添加 `trade_time`、`code`。累计成交数据和成交额单位须由上游统一。
- 该辅助函数只由需要此口径的策略显式调用。OHLC 必须来自已完成、未复权的合约行情；零成交分钟及复权连续价格不适用。它不会生成 `value`，也不会把 `turnover` 自动视为本周期成交额。现有 DolphinDB 转换器保留 `turnover`，且支持累计成交量，接入前须分别核实并统一成交额、成交量的周期与单位；不能只改字段名后直接套用。非法数值及乘数统一抛出 `ValueError`。
- 模型收到的 `value` 必须是**本分钟成交额**，`volume` 必须是本分钟成交量；按 `vwap=value/(volume×contract_multiplier)` 计算。`openint` 来自 `openint` 或 `open_interest`。`contract_multiplier` 从当前品种的合约基础资料取得，不得使用持仓量替代。零成交量或无效字段会拒绝产生新目标，不做零填充。
- 输入 `CustomBar.factors` 必须保留原始 `trade_time` 标签，并提供 `value`、`openint/open_interest`；建议同时放入研究口径完整精度的 `open/high/low/close/volume`。缺这些可退回 Bomber 标准 Bar 的 tick 精度 OHLCV，但正式使用前必须与研究输入逐字段对齐，否则不能宣称模型预测一致。

## 接入示意

以下是策略对象的装配边界，不是可直接启用真实报单的命令。先准备已经核验的研究口径 `CustomBar` Feed、角色快照、动态主力路由和合约乘数，再进行纸面/模拟回测。

```python
from importlib import import_module
from decimal import Decimal

demo = import_module("demos.10_hybrid_transformer.strategy")
adapter = import_module("demos.10_hybrid_transformer.model_adapter")

predictor = adapter.MizarWorkflowAdapter(
    release_path="/ABS/PATH/TO/RELEASE",
    directions_file="/ABS/PATH/TO/FEATURE_DIRECTIONS.csv",
    code="P", signal_id="1101", device="cpu",
    history_bars=1500, min_raw_bars=300,
)
config = demo.HybridTransformerConfig(
    product="P", venue="DCE", signal_id="1101",
    max_contracts=1, rebalance_minutes=1,
    contract_multiplier=multiplier_from_fut_basic,  # fut_basic.contMultNum
)
strategy = demo.HybridTransformerStrategy("p-transformer", roles, predictor, config)
```

RB 同一结构使用 `code="RB"`、`signal_id="1301"`，交易所、乘数、实际合约及文件路径均取对应的权威资料；不要照搬上面的 P 样例。

Runner 用 `DataBinding(..., DataType.CUSTOM_BAR, "1-MINUTE")` 把逐分钟模型事件送入策略；模拟执行桥仅订阅同一 Feed 的 `DataType.BAR`，不能把 `CUSTOM_BAR` 再送入撮合、形成双份行情。`market_adapter.ResearchCustomBarParser` 会按这个顺序生成两类事件。目标键采用 `<product>_main`，对应 `DynamicExecutionRoute`。参考 `01_main_ema/run_backtest.py` 的角色、合约注册、模拟执行、风控和报表装配，不要将目标键直接当作真实合约。
当前 Bomber 的 `DynamicExecutionRoute` 只允许历史模式；不能把这个示例直接接到 LIVE Runner。主力切换后，策略会在下一次调仓时刻向新主力重提目标，即使目标手数未变化。

## 历史模拟运行

先将 Mizar 根目录加入 Python 模块路径，并在含 Bomber 编译扩展、Mizar/Lumina 依赖的环境运行。以下用占位路径示意；`--bars-dir` 的每个文件须包含本分钟 `value` 与 `openint`，基础资料的 `contMultNum` 用于 VWAP 计算：

```bash
python demos/10_hybrid_transformer/run_backtest.py \
  --product P \
  --start-day 2025-05-06 --end-day 2025-05-09 \
  --bars-dir /ABS/PATH/TO/MINUTE_BARS \
  --contract-struct /ABS/PATH/TO/CONTRACT_STRUCT.feather \
  --fut-basic /ABS/PATH/TO/FUT_BASIC.feather \
  --release-path /ABS/PATH/TO/P_RELEASE \
  --directions-file /ABS/PATH/TO/P_FEATURE_DIRECTIONS.csv \
  --bar-timestamp start \
  --max-contracts 1 --rebalance-minutes 1 \
  --history-bars 1500 --min-raw-bars 300 \
  --report-dir /ABS/PATH/TO/REPORTS
```

RB 换成 `--product RB`、RB 的 release 和方向文件，自动使用 1301；其他期货资料也须对应 RB。`--bar-timestamp` 必须明确符合源数据“分钟起点/终点”的实际定义，不能照搬示例。若源字段为 `open_interest`，加 `--open-interest-column open_interest`。输出目录包含 `signals.csv`（每根已预测 bar 的预测值、信号、整数目标和调仓标记）、订单、成交、持仓、账户及输入审计文件。短区间回测仍须提供足够的前置 bar 完成模型预热，不能把测试期的第一根直接当充分历史。

## 启用订单前的验收

1. 核对每根原始 `trade_time` 是训练数据使用的时间标签，完整分钟可用时间不早于收盘时间；验证 `value/volume/contract_multiplier` 得到的 VWAP 与研究数据一致，特别检查夜盘、换月和零成交分钟。
2. 对相同原始 bar 逐行比较 Mizar 研究特征、三 seed `predicted_z`、融合 `predicted_z` 和 1101/1301 信号。允许误差阈值要先定义；跨阈值导致的信号状态差异应为零或经明确批准。
3. 以整数手量化后的目标做独立回测，计入手续费、滑点、换手、最大持仓和换月成本。此前连续仓位评估的收益率不可直接充当真实手数回测收益。
4. 先运行记录目标/模拟执行，核对成交回报及仓位；此目录没有 `run_live.py`，不会启动 CTP 实盘报单。实盘入口须沿用 Bomber 的账户锁、行情健康、恢复检查点、订单上限和显式报单门控，不能直接复制 EMA 的启动参数。

当前实现**策略层、Mizar 预测适配器、已准备行情的解析器及历史模拟入口**，但尚未在目标环境的真实行情文件上完成集成验收。Mizar Workflow 每根 bar 重新计算其保留窗口，长区间运行可能较慢；先跑短区间核对信号与耗时。在线 Runner 仍未接入。特别是主力原始合约 bar 若与训练时的连续行情构造不同，不可直接把模拟结果视为投研回测复现。
