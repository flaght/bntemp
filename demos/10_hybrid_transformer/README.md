# HybridTransformer 主力合约策略层（按品种外部配置）

Live 启动、恢复提交、状态锁和Recording停机现共用[公共生命周期](../../doc/functions/trader/LiveSessionLifecycle.md)（2026-10-29）；模型身份、旧批次期限及换约约束保留。

Live 历史、CTP 本地分钟和休市窗口续接现已共用 `runtime.live.history.history_stamps` 选择完整交易分钟。
原始模型字段转换、模型预热与真实缺口政策仍在本策略 `history.py`；见
[Live 历史协调](../../doc/functions/trader/HistoryFrameLoader.md)（2026-10-29）。

这是独立于 `01_main_ema` 的策略，不修改 EMA。复用其结构：按事件时间取得当前主力，只处理主力的已完成分钟，将模型信号变成**逻辑目标仓位**，由 Bomber 的动态路由和执行风控决定真实合约、委托及成交。策略本身不调用 CTP，也不在导入时连接账户。`run_backtest.py` 提供历史模拟入口，每次选择一个已配置品种独立运行，多个品种可分别提供发布模型、特征方向文件和信号配置。

## 已实现的边界

- `model_adapter.py` 在初始化时加载当前目录的 `flow.LiveFlow`。该 Flow 完成特征计算、15 期时序标准化、模型输入、多个 seed 等权预测和 Lumina 信号函数；`model.py` 原样复用 Mizar `lib/nn005/model.py` 的 `HybridTransformerRegressor` 定义，保持发布 checkpoint 的网络结构。推理仍需安装 PyTorch、KiChaos、Lumina、Ultron 依赖。
- 行情解析直接使用框架的 `FixedInstrumentBarParser`，由底层 `MappedBarParser` 统一校验并先发原生 `BAR`、再发伴生 `CUSTOM_BAR`，Demo 不再维护单独的解析器。显式配置 `value=value_column`、`open_interest=open_interest_column`、`trade_time="source_timestamp"`、`preserve_raw_ohlcv=True`，伴生字段保留原始 OHLCV 精度、`value`、规范名 `open_interest` 和原始 `trade_time`；`available_nanoseconds="available_ns"` 指定可用时刻。默认不配置这些选项时仍只发标准 `BAR`，兼容 EMA。
- `signal_config.py` 从外部 JSON 按品种读取 `signal_id`、`signal_method`、`signal_params` 和 `hold_bars`；策略和适配器不再固定品种或信号编号。方法名必须由 Mizar 的 `SIGNAL_FUNCTIONS` 注册，函数参数原样传给 Workflow。信号输入仍为原始 `predicted_z`。
- `strategy.py` 每根有效主力分钟 bar 都预测，`holding.py` 中 `PeriodHoldings(..., calendar=calendar)` 按品种交易日历计算绝对到期时间。每批保存 `opened_at_ns`、`expires_at_ns`；序号只用于核对和槽位轮换，中间信号不会覆盖旧批次。预热返回 `None` 时不新开仓；模型错误或坏行情仍尝试处理已到期批次。
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
    calendar=product_session_calendar,  # 框架 SessionCalendar，品种须与配置一致
)
```

切换品种时选择该品种的 JSON 条目，并使用对应交易所、合约乘数、模型发布路径和特征方向文件；信号编号、函数和参数都来自所选条目。

Runner 用 `DataBinding(..., DataType.CUSTOM_BAR, "1-MINUTE")` 接收模型事件；模拟执行桥仅消费同一 Feed 的 `DataType.BAR`，不能把伴生 `CUSTOM_BAR` 再送入撮合。框架 `MappedBarParser` 保留原生行情先、策略事件后的顺序，两个事件共享同一个 Bar 及其时间戳。

历史入口为每个真实合约注册静态 `ExecutionRoute`，目标键为 `<product>_main:<symbol>`，并把 `target_keys` 映射交给策略。主力换月只影响新批次的合约选择，旧批次在原合约保留至到期，不能将整个净仓位提前迁移到新主力。自行装配且未提供映射时仅支持单一合约，换约会明确报错。另有独立 Live 入口 `run_live.py`，支持 CTP/DolphinDB 行情与 Recording/SimNow 执行，参数和验收边界见下文。

## 持仓周期行为示例与时间口径（以下以 hold_bars=5 为例）

`max_contracts=10` 且每分钟信号为 1，前五根可开仓 bar 的总目标依次为 `2、4、6、8、10`；第六根 bar 上第一批 2 手到期、新一批 2 手建立，总目标仍为 10，账户无需先平再开，但新批次的期限已重新开始。若第一根信号为 1、随后全为 0，则前五根目标一直为 2，第六根才变为 0。

若第一根信号为 1、第二根为 -1、此后没有新批次，净目标依次为 `2、0、0、0、0、-2、0`。两个批次分别在第六、第七根 bar 到期。账户暂时为零不表示两个批次已被删除。

期限按交易日历中的分钟推进：小节休市、午休、周末和节假日跳过，夜盘取消及替换时段由现有会话例外决定。断线漏收行情时，经过的交易分钟仍计入期限和槽位轮换，不补造历史开仓。主力换月前的批次始终绑定原合约。

`opened_at_ns` 取框架提供的已完成分钟结束事件时间；历史整分钟结束边界和实时“结束边界减 1ns”先统一。原始 `trade_time` 只作为模型标签，不再次加一分钟。例：14:57 若是开始标签，该 bar 在 14:58 才完成，正常螺纹钢日盘剩两分钟，夜盘再走三分钟，到期是 21:03；股指是下一交易日 09:33。若 14:57 已是完成时点，则分别为 21:02、下一交易日 09:32。实际夜盘、交易日须来自所传日历；不能写死这些时间。

`batch_id` 使用建立时的结束时间戳，重启不重新编号。`opened_bar/expires_bar` 保留作诊断；缺失行情时序号会跳过对应交易分钟。绝对期限用于比较，程序中断不会延长旧批次。到期表示应调整目标，实际成交仍取决于下一行情、可交易时段、流动性和执行状态。

账本上限约束的是所有未到期批次的绝对手数及输出目标。真实在途订单、拒单和跨合约换仓的过渡持仓仍由执行层管理，不能把目标上限当作已完成真实账户验收。历史区间结束时不伪造未来 bar 或强制提前到期；未到期批次会保留在 `summary.json` 中。

## 历史模拟运行

当前入口是 `run_backtest.run_case(...)` 函数。除原有行情、模型和信号参数外，必须显式传入 `calendar=product_session_calendar`，没有序号模式或默认品种日历回退。日历须覆盖回测行情及最后一批的完整到期范围；不足时明确报错。

行情文件读取后、送入回放 Feed 前，入口调用 `dataprep.bars.filter_session_bars(prepared, calendar)`，只保留整个 Bar 区间落在文件所属交易日会话内的数据。起点标签先由 `read_bar_frame` 转为结束边界再判断，因此终点标签的收盘 Bar 会保留；午休、盘后及被例外取消的夜盘同时从撮合 `BAR` 和模型 `CUSTOM_BAR` 中剔除。原始 `source_timestamp/trade_time` 不改，`input_issues.json` 记录剔除数量及时间样例，`input_coverage.json` 的 `session_filters` 记录输入/保留/剔除行数与日历摘要。全部被剔除或日历覆盖不足时明确报错。交易时段内的零成交量、无效成交额仍由原有数值校验拒绝，不作为盘外数据静默忽略。

日历复用框架现有装配，示意如下（文件须为已核验的品种时段、例外及日期日历导出）：

```python
from bomber.framework.calendars import ExportedCalendarProvider
from bomber.framework.dataprep.session_inputs import SessionInputs, load_session_inputs

product_session_calendar = load_session_inputs(
    SessionInputs(
        product="RB", calendar_id="china.sse", exchange="SHFE",
        backend="file", hours_file=hours_path, overrides_file=overrides_path,
    ),
    calendar_provider=ExportedCalendarProvider(calendar_csv_path),
)
# import_module("demos.10_hybrid_transformer.run_backtest").run_case(
#     calendar=product_session_calendar, ...原有参数...)
```

输出包括 `orders.csv`、`fills.csv`、`positions.csv`、`signals.csv`、`batches.csv`、`summary.json` 以及 `strategy_state.json`。批次报告新增开仓/到期纳秒时间；总结记录日历来源。`strategy_state.json` 是策略组件快照，不能替代账户、目标和订单的完整运行时检查点。

## 手续费来源与系数

Hybrid 回测统一使用 `fut_basic` 的费用规则，已移除 `commission_per_contract` 参数。公共合约工厂保留原始 `tradeCommiNum/tradeCommiUnit` 并传递标准化费用，框架按实际成交量价和合约乘数计算；支持元/手及成交金额百分比。

不传 `commission_config` 时使用 `CommissionConfig(commission_source="metadata")`，系数为 1。缺少合约交易费用时，框架在注册合约、模型加载前报错。显式零费率仍是有效资料；不允许 `auto` 或 `fixed` 回退模式。

需要调整手续费系数时：

```python
from decimal import Decimal
from bomber.framework.fees import CommissionConfig

fees = CommissionConfig(
    commission_source="metadata",
    commission_multiplier=Decimal("1.2"),
    commission_product_multipliers={"RB": Decimal("1.5")},
    commission_contract_multipliers={"rb2704.SHFE": Decimal("1.8")},
)
# run_case(..., commission_config=fees)
```

合约系数优先于品种，品种优先于全局，覆盖关系由框架统一处理，入口不会再乘一次。结果目录保留 `commissions.csv` 和 `commission_config.json`，记录标准费用、最终系数、实际费用和来源。共享 Profile 的旧固定费用字段仅供其他入口兼容，Hybrid 的严格资料模式不会使用该回退金额。

当前基础净持仓模型使用源表通用交易费用；源表只有一组交易费时，开仓、平今、平昨沿用同一标准，不凭空推断差异。完整费用边界见[框架手续费说明](../../doc/data/FUTURES_FEES.md)。

## 中断恢复与独立时钟

`checkpoint.HybridCheckpoint(strategy)` 实现框架 `snapshot_state/restore_state/validate_targets` 接口。恢复会验证预算、周期、信号配置、执行路由及日历指纹，保留原批次 ID、期限、槽位、最后处理位置和目标版本。变更日历覆盖或规则须显式迁移，不能直接用旧快照恢复。

未来实盘装配须完成以下接线；当前 Demo 仍没有实盘入口：

1. 在同一个 `RuntimeStateManager` 的 `state_components` 中注册 `{"hybrid": HybridCheckpoint(strategy)}`，与 TargetStore、账户和订单一起保存、恢复。
2. 装配后调用 `strategy.bind_checkpoint(manager.save)`；每次行情处理、批次续期或时钟推进都保存，不能只在产生委托时保存。运行时须串行处理行情、时钟、保存与恢复。
3. 通过 `StrategyBindings(time_feed_ids=(clock_feed_id,), ...)` 绑定框架独立时钟，让它持续调用 `on_time`。同一分钟边界先交付已完成 bar，再交付该边界时钟，以保留到期与新信号的净额合并；缺少 bar 时仍由时钟处理到期。恢复先完成账户/未完成订单核对，再以当前时间处理过期批次，随后才开放新信号。
4. 模型历史窗口由历史服务单独预热，不能通过交易回调重放历史开仓。账本和时钟已越过的旧 bar 即使迟到，也不会新开批次。模型尚未就绪时到期处理仍可运行。

此组件没有自行建立一个独立 JSON 自动恢复路径，以免只恢复策略账本却遗漏真实账户或在途订单。模拟报告中的组件快照只供核对；不要把它当作完整实盘恢复文件。程序停机期间已经错过的成交无法补造，重连后按现有执行层能力调整当前目标。

## 启用订单前的验收

1. 核对每根原始 `trade_time` 是训练数据使用的时间标签，完整分钟可用时间不早于收盘时间；验证 `value/volume/contract_multiplier` 得到的 VWAP 与研究数据一致，特别检查夜盘、换月和零成交分钟。
2. 对相同原始 bar 逐行比较 Mizar 研究特征、三 seed `predicted_z`、融合 `predicted_z` 和所配信号函数的输出。允许误差阈值要先定义；跨阈值导致的信号状态差异应为零或经明确批准。
3. 以整数手量化后的目标做独立回测，计入手续费、滑点、换手、最大持仓和换月成本。此前连续仓位评估的收益率不可直接充当真实手数回测收益。
4. 先运行记录目标/模拟执行，核对成交回报及仓位；本目录的 `run_live.py` 通过显式授权接入 SimNow，沿用 Bomber 的账户锁、行情健康、恢复检查点、订单上限和显式报单门控，不能直接复制 EMA 的启动参数。

当前实现**策略层、Mizar 预测适配器、已准备行情的解析器及历史模拟入口**，但尚未在目标环境的真实行情文件上完成集成验收。Mizar Workflow 每根 bar 重新计算其保留窗口，长区间运行可能较慢；先跑短区间核对信号与耗时。在线 Runner 仍未接入。特别是主力原始合约 bar 若与训练时的连续行情构造不同，不可直接把模拟结果视为投研回测复现。

## Live：CTP / DolphinDB 可配置行情

第一次接手代码，建议先阅读[Hybrid Live新手组装导读](../../doc/functions/demos/run_live.md)。它按实际调用顺序解释Runtime、Runner、策略和执行端，源码 `run_live.py` 与 `history.py` 也已补充对应注释。

入口为 `python -m demos.10_hybrid_transformer.run_live`，每个进程只运行一个品种、一条 Hybrid 策略。

策略和装配集中在 `run_live.py`，常驻窗口调度复用 `bomber.framework.trader.runtime.live.continuous`，命令行只保留业务输入和运行开关。各函数仅接收其使用的显式参数，命令行对象只在解析和main读取字段时存在，不沿调用链传递；Runner不保存整份命令行配置。`build_strategy()` 独立声明模型和策略，参数解析阶段不构造它们。

- 模型默认设置：CPU、历史窗口1500根、最低260根、开始时间标签；由本文件的 `MODEL_*` 常量明确声明。
- 策略开仓间隔默认1分钟，持仓周期和信号函数仍按外部信号文件的品种配置读取。
- `main()` 将解析结果逐项转换为显式关键字参数传给 `build_runtime()`；不传命令行Namespace。命令行未声明的查询超时、报单上限、名义金额、限价偏移及执行政策沿用函数默认值：15秒、100笔、1000000、1跳、fills/off。需调整时在代码调用中显式传入对应关键字参数。
- `prepare_inputs()` 默认参考超时15秒、刷新5秒。主力/条款参考库沿用 `DDB_REFERENCE_DATABASE`，通常为 `dfs://bomber_daily`。时段库独立配置，依次采用显式 `trading_hours_database`、`DDB_TRADING_HOURS_DATABASE`、默认 `dfs://bomber_static`；表沿用 `DDB_TRADING_HOURS_TABLE`，默认 `fut_trading_session`。不再把参考库自动当作时段库。
- 日历默认 `china.sse`，可用 `BOMBER_CALENDAR_ID` 指定；时段表和例外表沿用 `DDB_TRADING_HOURS_TABLE`、`DDB_SESSION_OVERRIDES_TABLE`。没有例外时仍需显式 `--no-session-overrides`。

DolphinDB 连接按用途分离：实时订阅和历史分钟查询使用 `DDB_MARKET_HOST/PORT/USERNAME/PASSWORD`；
`bomber_daily` 基础资料与 `bomber_static` 交易时段共同使用 `DDB_REFERENCE_HOST/PORT/USERNAME/PASSWORD`。
各项未设置时兼容回退旧 `DDB_HOST/PORT/USERNAME/PASSWORD`，两组新变量不会互相回退。
流订阅端口使用 `DDB_MARKET_STREAMING_PORT`，兼容旧 `DDB_STREAMING_PORT`，默认0。
使用 CTP 且仅本地文件/实时积累窗口时不需要行情服务器连接，基础资料连接仍需配置。
完整示例见 [DolphinDB 双服务器配置](../market_sources/dolphindb.env.example)。

行情源通过 `--market-source` / `--market-config` 选择；执行模式为 `recording` 或 `simnow`，默认 Recording。
Live使用 realtime、每个活动会话固定主力和 SHFE/INE 执行范围。`--run-forever` 复用08的公共日历调度，支持休市等待和跨交易日自动重登录；有旧批次时不自动移仓，共享账户宿主未接入。

- CTP：原始深度中的累计 `Volume/Turnover` 按交易日差分，持仓量取分钟最后值；首个部分分钟丢弃。
- DolphinDB：使用已完成分钟流；须声明 `start/end` 时间标签和 `per-bar/cumulative` 量额口径。表应沿用公共适配器的 `Code/date/minTime/open/high/low/close/volume` 字段，成交额提供 `value` 或 `turnover`，持仓量提供 `open_interest` 或 `openint`。
- 行情 JSON 的 `volume_mode` 同时声明成交量和成交额的口径，二者须一致。混合口径须先在来源表规范化，不能以末价乘成交量代替实际成交额。
- 行情 JSON 的 `timestamp_label` 描述 DDB 来源标签；代码中的 `MODEL_BAR_TIMESTAMP` 描述研究模型使用的标签，两者独立。模型默认 `start`，必须与训练数据一致。
- CTP 时段末精确结束边界快照并入最后一分钟；没有后续成交时最多等待 1 秒封闭完整桶。OHLC 来自深度快照末价，须在部署时与研究分钟逐字段核对。
- 交易时段、夜盘归属和分钟期限使用同一 `SessionCalendar`；交易所由合约资料决定。会话中资料/交易日变更按框架闭闸并保存状态，不自动迁移持仓。

模型计算链、五个交易分钟批次及总手数预算继续复用当前回测策略。
Live 控制线程先处理已到达完整分钟，再调用已有 `on_time()` 处理到期；新生命周期没有增加行情源异常后的自动补偿交易。
健康检查失败时沿用框架停止路径，不能把到期目标提交等同于实际成交。

### 启动参数

连接凭据沿用项目 `.env` 的 CTP/DDB 配置。交易日和资料来源日须按实际会话核对，夜盘不能直接使用自然日期。
以下路径、交易日和例外表需要替换为当前部署值。示例先运行 Recording，不产生柜台订单。

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
export PYTHONPATH="$(pwd):/workspace/worker/pj/KiChaos${PYTHONPATH:+:$PYTHONPATH}"
export IMPLUSE_VERSION=all

# 用同一组业务输入切换行情源。仅在确认没有时段例外时使用 --no-session-overrides。
hybrid_args=(
  --connect --mode recording --product RB
  --release-path /ABS/PATH/TO/RB_MODEL_RELEASE
  --directions-file /ABS/PATH/TO/feature_directions.csv
  --signal-config /ABS/PATH/TO/signals.json
  --expected-trading-day YYYYMMDD
  --expected-source-day YYYYMMDD
  --no-session-overrides
  --history-minutes 1500 --history-config /ABS/PATH/TO/hybrid_history.json
  --max-contracts 10 --seconds 600
)

# 方案一：CTP 深度聚合。
python -u -m demos.10_hybrid_transformer.run_live \
  "${hybrid_args[@]}" --market-source ctp

# 方案二：DolphinDB 完整分钟流。下面声明结束标签、量额均为本周期值。
python -u -m demos.10_hybrid_transformer.run_live \
  "${hybrid_args[@]}" --market-config /ABS/PATH/TO/hybrid_market.json
```

`hybrid_market.json` 声明 DolphinDB 表名、时间标签与量额口径：

```json
{
  "source": "dolphindb",
  "table": "YOUR_FUTURES_MINUTE_STREAM",
  "timestamp_label": "end",
  "volume_mode": "per-bar",
  "max_age_seconds": 120
}
```

SimNow 模式将示例中的 `--mode recording` 改为 `--mode simnow`，另外传入：

```text
--enable-orders --confirm-simnow
--state-file /ABS/PATH/TO/hybrid_RB_TD.json
```

这会向 SimNow 报单。`fills/off` 是策略归属成交账本模式，资金和订单仍使用交易账户。
新会话使用不存在的状态文件；同交易日、同合约、同模型及配置恢复使用原文件加 `--resume`。
停机撤销活动订单并保存状态，不自动平掉剩余仓位。

### 仅用CTP实时行情积累模型窗口

DolphinDB分钟表停止更新时，使用 `--market-source ctp --history-minutes 0`，
不传 `--history-config` 或 `--history-file`。此模式不读取DolphinDB历史行情；
主力角色、合约条款、手续费、因子和交易时段仍按现有参考源读取。

首次启动和同交易日 `--resume` 均可使用此模式。恢复仍使用原检查点，校验持仓、订单、
路由及会话身份，保留批次绝对到期时间。模型先恢复本机近期录制窗口，再接实时分钟；首次无文件时从零积累。未就绪时不建立新批次；
通道正常且满足执行条件时，已有批次仍按原期限推进到期目标。旧恢复目标不会作为新信号重复执行。

日志 `bars` 是策略累计处理根数，恢复后可能非零；`model_window` 是本次模型实际窗口根数，
`min_raw_bars=260`是Live最低原始分钟门槛；第260根起允许进入预测计算，因子和序列条件仍须满足，
并不保证立即产生有效预测或非零信号。
模型DataFrame不写入交易检查点，重启从独立原始分钟文件恢复；自动跨交易日重登录由 `--run-forever` 单独启用。

### CTP分钟自动落盘与重启复用

CTP行情默认启用完整分钟录制，不新增命令行选项。默认路径按实际入口和真实合约确定：

```text
<入口目录>/results/market/ctp/<真实合约>.jsonl
```

例如远程为 `prod/01_hybrid_transformer/results/market/ctp/rb2701.SHFE.jsonl`。
路径独立于本次报告目录和state-file，所以多次运行可以复用同一合约的原始行情。
在代码中可向 `build_runtime(bar_directory=Path(...))` 显式传入其他目录。
主力换约使用不同文件，不把旧主力拼成当前真实合约行情。

框架 `FuturesMinuteArchive` 录制原始OHLC、分钟成交量、成交额和末值持仓量，保存统一的
UTC分钟结束减1ns时间戳、CTP来源和合约乘数；数值以十进制字符串保存。每根分钟先写入、
flush和fsync，再向策略发布。首个不完整分钟不录制；同一分钟重复去重、内容冲突和文件损坏明确失败。
文件单写锁防止两个进程同时追加；磁盘写入失败时不发布该分钟，沿用正常故障停机路径。

`--market-source ctp --history-minutes 0` 在打开本机存档后自动恢复近期窗口，最多1500根；
不足260根也可以恢复已有部分，再继续积累，不要求文件一开始就有260根。范围限制为最近1500个
交易分钟，不把很久之前的行情当成近期窗口。启动日志 `Hybrid本地分钟恢复` 包含file、loaded、missing和recorded；
运行报告增加bar_file和recorded_bars。

历史恢复只调用预测器warm_history，不回放交易或生成历史批次。停机期间没有订阅到的分钟不会
被补造或移到其他时间，缺口会明确记录；模型沿用现有按真实时间分段的逻辑，并不保证与持续在线
时的研究特征和预测完全相同。新批次只由后续完整实时分钟驱动；持仓仍按原检查点期限管理。
显式 `history-minutes>0` 时继续采用原历史服务规则，本机缓存不会隐式顶替声明的历史数据源。

之前只存在内存里的Bar不能追溯导出；本功能只能保存启用之后收到的完整分钟。
程序停机后保留行情文件即可，勿用同一个文件记录其他品种、来源或乘数口径。
同步到远程时，除framework改动外，还需将demo的run_live.py和history.py同步到实际执行的prod目录。

### 新DolphinDB分钟状态流

公共连接分离已经接入：实时订阅和历史分钟读取 `DDB_MARKET_*`；基础资料及静态时段读取
`DDB_REFERENCE_*`。两组新变量逐项兼容旧 `DDB_*`，旧变量仍指向基础服务器时，须完整配置
行情组，避免兼容回退读到旧服务器。配置文件不放地址或密码。

新增 `market_dolphindb_rb.json`：`market_bar_state_v2_shfe`，`bar_mode=state`，
时间标签start，量额per-bar。该流的volume/turnover是本分钟量额，cum_volume/cum_turnover才是
交易日累计值；不能对volume/turnover再次做分钟差分。

状态流按用户选择采用“同合约下一分钟到达，封闭上一分钟”。同一分钟的多次状态覆盖待完成版本；
只发布最后一份一次，并使用完整barTime自然时间。updateTime保留纳秒精度，但不是唯一序号：
同一时间戳下，量额同时增长、开盘价不变、高点不下降、低点不上升且基准标记不变时，接受后续快照；
完全相同的重复快照去重。相同时间戳但无法确认先后的修改、量额倒退、已封闭分钟的不同内容仍报告异常。
较旧updateTime的待完成分钟快照忽略。volume_baseline_ok=false的分钟不发布为完整行情。
休盘前最后一根不会由定时器推测完成，将等待下一条分钟状态；该机制尚未实际服务器联调。
如果未来服务器增加完成标记或独立完整分钟流，可以再接入明确完成语义。

选择DolphinDB状态行情时使用 `--market-source dolphindb --market-config <路径>/market_dolphindb_rb.json`。
先使用recording模式验证，只积累实时窗口时保持history-minutes=0。启用数据库预热时再加
history-config=history_rb.json；2026-10-29探查只有约103行，包含形成中分钟，还不足当前最低260根，
不足260根时会先装入实际可用的分钟并接续推送。

CTP路径、本地分钟录制和已存CTP会话身份保持兼容；持仓期限保持原配置；旧检查点的模型门槛若为300，须先显式迁移到260再恢复。

### 历史模型预热

历史预热复用框架 Provider，但必须提供完整原始行情，不能使用 EMA 的 `adjusted_close` 历史配置。
`history_minutes` 是交易分钟根数，应满足 `MODEL_MIN_RAW_BARS <= history_minutes <= MODEL_HISTORY_BARS`；默认为 0 时仅靠实时行情预热。
可使用历史服务或实时完整分钟重新预热。历史覆盖不足时先装入实际可用分钟，再保持订阅积累到模型门槛；字段缺失、查询失败或来源冲突仍报错，不填造成交额、不发布历史信号。

已按 `01_main_ema` 的 `HistoryService + DolphinDbHistoryProvider` 路径补充
`history_rb.json`，对应已确认的新行情表 `dfs://ctp/bar_1min_shfe`。启动时在原命令上增加：

```bash
--history-minutes 1500 --history-config demos/10_hybrid_transformer/history_rb.json
```

远程目录为 `prod/01_hybrid_transformer` 时，将上面的配置路径同步替换为该目录。
已有状态文件按原恢复要求使用 `--resume`，不覆盖旧状态。

该配置使用用户确认的**交易日日期、分钟开始标签**；`turnover → value`，
`open_interest`直接读取。新表已通过RB样本确认是交易日累计量额，继续采用 `volume_mode=cumulative`。
`minTime`现在是完整TIMESTAMP，配置 `time_column_kind=timestamp`，以其自然时间为准，并用 `date`和
同一个 `SessionCalendar`核对交易日归属，不再拼接交易日与时钟，也不固定减一天。
累计模式读取涉及交易日的完整行，在请求窗口之前取得基准，量额同时差分，持仓量不差分。
首个交易分钟采用日内累计值；盘中首行缺少前值时只作基准，不能把此前成交归入这一分钟。
周末夜盘、跨午夜、午休及交易日重置均按会话规则处理；同日分钟缺口不会合并量额；缺口后的首行只作新基准，该分钟记为缺失，后续连续分钟仍可差分。
数据库按完整交易日读取，超过声明行数上限时明确失败。

该配置不改变模型输入要求：零成交分钟、量额倒退、同一分钟冲突或VWAP/OHLC不一致仍拒绝。
截图中的AG差分异常不能通过配置修复；实际RB数据也不一致时，日志会显示合约、时间和量额，
应检查分钟表生成逻辑。当前不把零成交分钟的VWAP填为收盘价，也不跳过模型分钟。

启动日志包含“未启用”或“开始／完成／失败”，完成日志包含实际数据来源和覆盖；
历史只进入预测器专用预热接口，不重放开仓。历史口径及样例通过离线替身验证，不等于真实数据库验收。

`hybrid_history.json` 示例（库、表及实际列名须替换；下例明确来源是北京时间、开始标签）：

```json
{
  "database": "dfs://YOUR_MINUTE_DATABASE",
  "table": "YOUR_FUTURES_MINUTE_TABLE",
  "columns": {
    "instrument_id": "symbol",
    "ts_event": "datetime",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "value": "value",
    "open_interest": "open_interest"
  },
  "time_unit": "ns",
  "time_label": "start",
  "instrument_format": "symbol",
  "venue": "SHFE",
  "source_timezone": "Asia/Shanghai"
}
```

若来源已经保存 UTC 纳秒，应设置 `source_timezone=UTC`，不能重复减八小时。
可用 `--history-file` 提供框架 JSONL/CSV 完整分钟；时间 `ts_event` 为 UTC 分钟结束减1ns、合约带交易所后缀。
启用历史服务时，盘前阶段优先文件并由数据库补足，`--resume`恢复阶段使用数据库；`history-minutes=0`不读外部历史服务，CTP仍恢复本机录制窗口。


历史请求300根而只有103根可用时，会打印 `requested=300 loaded=103 missing=197`，模型窗口日志为 `model_window=103 min_raw_bars=260 warmup_remaining=157`。
空结果会打印loaded=0并继续等待260根实时分钟。这里的loaded指通过字段和数值校验、实际装入模型的分钟，
不等于数据库原始行数；累计量额的首行和缺口后首行可能只作差分基准。
每根实时分钟日志打印model_window和warmup_remaining，运行报告也保存history_loaded_bars和warmup_remaining_bars。
只允许“数据不足”继续；查询失败、无效字段、量额倒退或同一分钟冲突仍失败。恢复持仓的绝对到期时间不受模型预热等待影响。

### 检查点与报告

SimNow 装配时将 `HybridCheckpoint`、模型/信号配置摘要、日历指纹注册进统一 `RuntimeStateManager`，并调用 `strategy.bind_checkpoint(manager.save)`。
目标相同的批次续期也保存；恢复后保持原批次绝对期限，模型窗口通过本机录制、历史服务或实时完整分钟重新积累。
不兼容EMA的旧状态文件。定时模式仍只接受同交易日恢复；常驻模式允许核验后向前跨日，模型/信号/行情口径变更仍拒绝恢复。

每次运行输出 `summary.json`、`events.txt`、`signals.csv`、`batches.csv` 和最终只读 `strategy_state.json`。
运行状态通过与是否发生真实成交分开：合法零信号可以没有成交，报告的 `fills_received` 只由执行回报计数。

离线验证：

```bash
python -B -m unittest tests.test_demo_hybrid_live tests.test_demo_hybrid_holdings \
  tests.test_demo_hybrid_timing tests.test_demo_hybrid_flow tests.test_futures_bar_values -q
python -B tests/run_hybrid_live_offline.py
python -B tests/run_option_vega_offline.py --market-sources
```

离线装配使用原生SDK实体与模型的替身，其余 Runner、路由和 Recording 执行链使用真实代码。
真实 DDB/CTP 数据接续、完整模型性能及 SimNow 成交需在目标 Linux 环境验收。

## 按交易日历一直运行（2026-10-29）

增加 `--run-forever` 即启用常驻。`--seconds`不再控制退出；不带该开关时，原定时模式保持不变。
进程在休市期间等待，不执行策略，也不把正常休市静默解释为行情断流。进入下一窗口时重新连接、
刷新主力/条款/复权资料并预热，交易日来自日历窗口，SimNow还核对实际TD登录日。
`--expected-trading-day`/`--expected-source-day`在此模式可省略；若提供，仅核对第一个活动窗口，
以后自动采用窗口交易日及其前一交易日，不用每天修改启动命令。

- 午休、夜盘结束、日盘收盘和周末由同一SessionCalendar决定。日历维护范围或参考资料失效仍报错，不推测缺失规则。
- SimNow复用同一个`--state-file`：文件存在则恢复，不存在则新建；不需要手工加`--resume`。
  休盘只撤销活动委托，保留成交仓位和未到期批次；跨日先核验柜台并结转今昨仓，原文件在提交时自动备份。
- `HoldingBatch.opened_at_ns/expires_at_ns`不改变，五分钟仍按交易分钟计算，休盘和重连不会重置期限。
  模型尚未重新就绪时，健康通道上的已有批次仍由独立到期时钟管理。
- 同一进程在窗口间保留最近1500根原始模型分钟，与新读历史按时间合并；不重放旧信号或旧委托。
  进程重启后仍使用显式历史配置或CTP原始分钟文件恢复窗口，Recording没有可恢复的交易账户状态。
- 主力在两个窗口之间变化时，只有旧批次、旧仓位和目标均清零才可续接新合约；有余仓则保留检查点并报错，不迁移旧批次。
  单个活动窗口内仍由固定主力守卫检查角色和条款变化。
- 报单上限按当个交易日已保存的订单累计扣减，午休重连不刷新额度。真实断流、拒单、恢复冲突、停机核验失败仍停止，不盲目重启。
- 按一次Ctrl+C或发送SIGTERM会走正常停止、撤单与保存；不强制平掉已有仓位。常驻依赖终端/宿主进程持续存活。

服务器按当前DolphinDB分钟源做Recording常驻验证：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
export PYTHONPATH="$PWD:/workspace/worker/pj/KiChaos${PYTHONPATH:+:$PYTHONPATH}"
export IMPLUSE_VERSION=all

/workspace/worker/env/chs-live/bin/python -u \
  -m prod.01_hybrid_transformer.run_live \
  --connect --run-forever --mode recording --product RB \
  --market-source dolphindb \
  --market-config "$PWD/prod/01_hybrid_transformer/market_dolphindb_rb.json" \
  --release-path /workspace/worker/pj/Chrono/genuis/mizar/records/ricso2/1release/rbb/10002 \
  --directions-file /workspace/worker/pj/Chrono/genuis/mizar/records/ricso2/rbb/rulex/113001/nxt1_ret_5h/cohort_pro.csv \
  --signal-config "$PWD/prod/01_hybrid_transformer/signal_config.json" \
  --max-contracts 10 \
  --history-minutes 300 \
  --history-config "$PWD/prod/01_hybrid_transformer/history_rb.json" \
  --report-dir "$PWD/prod/01_hybrid_transformer/results/live" \
  --no-session-overrides
```

SimNow执行时仍须显式`--mode simnow --enable-orders --confirm-simnow --state-file <同一个原状态路径>`，
不得用新文件绕过旧持仓。常驻已统一到公共Live模块，策略目录原有 `continuous.py` 已删除。
同步到远程实际执行的 `prod/01_hybrid_transformer` 时，须同步 `run_live.py`、`history.py`、`checkpoint.py`
及公共 `runtime/live` 变更，并移除远程旧 `continuous.py`；不需要因此重新编译原生扩展。
模型窗口与Recording批次通过已有状态组件契约在进程内续接，接口及验证范围见
[run_continuous](../../doc/functions/trader/run_continuous.md)。

离线回归（复用SDK实体夹具，不联网）：

```bash
PYTHONPATH=.:.validation/packages python -B -m unittest tests.test_hybrid_continuous
```

公共常驻、共享调度兼容与EMA接线的聚合检查：

```bash
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_continuous_offline
```

真实DolphinDB/SimNow跨休盘、跨日接续仍需在Linux运行机验证。

## 历史预热适配层收敛（2026-10-29）

`history.py`现只保留研究字段需求、模型时间标签、预测器预热和模型窗口身份适配。
配置JSON解析、DolphinDB服务构建、单路加载/审计、本地归档过滤和窗口合并复用公共框架；
历史/实时VWAP使用相同的期货数值校验。历史不足继续等待实时分钟，来源故障和数据冲突仍阻止启动。
归档审计现在按整个请求窗口计算缺失数量，包括尚未录制的前段，而非仅统计已有数据之间的内部缺口。

启动命令和现有JSON配置不变。部署时同步本目录`history.py`以及框架：
`dataprep/history/factory.py`、`dataprep/history/__init__.py`、新增`dataprep/history/futures.py`、
`market/futures.py`、`trader/runtime/live/history.py`。完整公共历史回归包含新测试`tests/test_history_warmup.py`。
详细接口见[公共历史预热](../../doc/functions/trader/HistoryFrameLoader.md)。

历史读取数量与模型门槛分别设置：`--history-minutes`允许0至1500，0表示关闭外部历史读取。
当前 `MODEL_MIN_RAW_BARS=260`：读取到260根有效原始分钟就达到输入门槛，无需再等40根。
历史预热不会重放预测或开仓；后续完整实时分钟才触发新的预测和批次，因子/序列条件仍须满足。
读取数量不写入模型恢复身份，但最低原始分钟数会写入。旧检查点若保存300，须先备份并显式
仅迁移 `component_states.live_session.identity.min_raw_bars` 为260，再由正常恢复流程核验账户和批次；
不能直接删除检查点或绕过其他身份校验。
