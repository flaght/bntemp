# 08：期权Vega策略

以**回测、Live、自定义特征**为三条主线。两个入口使用相同的策略、风险限额和特征契约，执行与仓位由`bomber/framework`负责。默认MO认购、同月IM和000852指数；IV与Delta使用同月期货价格，选约/增仓/退出/对冲保持原strategy1逻辑。

## 目录

| 文件 | 职责 |
| --- | --- |
| `run_backtest.py` | 文件准备、公共模拟Backend/结算接线及回测报告 |
| `run_live.py` | 在线参数、行情/参考/执行渠道选择及公共Live生命周期启动 |
| `feature/` | 特征生成/导出、声明加载、标准文件读取及回测分钟帧适配 |
| `strategy.py` | 策略决策、实际自有仓位管理、公共装配绑定与限额 |
| `vega_signal.py` | 配置、原数值算法、合约关系及可独立恢复的信号状态 |
| `live.py` | Live分钟帧适配、健康检查、公共执行装配与只读诊断 |
| `recovery.py` | 应用恢复/跨日约束，调用公共账户与持久化能力 |

解析、结算、模拟撮合、目标差额、OMS、归属、风险、日历及共享账户复用[框架](../../bomber/framework/README.md)。探针/验收在[scripts/integration/options](../../scripts/integration/options)，测试在[tests](../../tests)。目录不保留旧模块的转发壳。

同步到服务器时须同步本次删除项，不能只覆盖新增文件。根目录`features.py`已迁入`feature/`包，须删除服务器上的旧文件；不保留转发壳。旧辅助模块导入路径已由工厂、脚本和测试统一替换；两个运行入口及JSON检查点格式保持不变。

## 1. 回测

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

### YAML启动参数

回测和Live入口均复用公共`bomber.framework.cli.ConfigArgumentParser`，共用[config/strategy.yaml](config/strategy.yaml)，通过不同配置ID选择各自参数。安装YAML读取依赖后，可直接启动回测：

```bash
python -m pip install -r bomber/framework/requirements-config.txt
python -u demos/08_option_vega/run_backtest.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id m5_index_blend
```

配置采用`experiment_setups -> 配置ID -> 参数名/值`，`description`仅作为说明。`m5_index_blend`与M5启动命令一致：期权M5、期货/指数M1、14:00开仓、原生日历、`index_blend`列以及`next_market`撮合；`opt_dir`须指向真实M5文件。`live_recording`、`live_model`、`live_simnow`分别用于三个Live执行模式。各入口只加载选中的配置，不混入其他ID的参数；把回测ID传给Live会报未知参数。复制一组参数即可添加实验，不需要修改Python。

优先级为**命令行显式参数 > 选中的YAML配置 > 默认值**。例如同一数据集改用MA20：

```bash
python -u demos/08_option_vega/run_backtest.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id m5_index_blend \
  --index-value-field ma20 \
  --report-dir tests/results/backtest/option-vega-m5-ma20
```

配置键使用`start_day`、`index_value_field`等参数名，也接受`start-day`形式；`--config_file/--config_id`保留下划线别名。日期、时点和指数代码应加引号，如`"2026-08-13"`、`"14:00"`、`"000852"`；布尔开关写`true/false`，逗号列表参数仍写字符串，可重复参数（如`commission_product_multiplier`）使用YAML列表。相对数据路径与普通CLI一致，按运行目录解析，示例应在`pro`根目录启动。

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

### 到期现金结算

使用公共`OptionCashSettlementModule`，到期有实际残仓时追加：

```bash
--option-settlements /实际路径/官方期权日行情目录 \
--settlement-available-time 16:00 --exercise-fee-per-contract 0
```

目录为`YYYY-MM-DD.feather`，含`code,trade_date,settlPrice`；也可传标准CSV/Parquet/Feather，列为`symbol,last_trade_date,settlement_price`，可选`available_at,fee_per_contract`。只按真实条款指定的到期日读取；允许零终值。没有实际残仓可不提供终值，有残仓缺终值则失败，不使用最后close猜结算价。

普通成交费用由公共FeeModel处理，结算费用单独记录。模拟仍使用基础NETTING/Bar撮合与比例保证金，不代表完整卖方保证金、期货交割或实物行权。

报告保存在`results/<run-id>/`：`summary.json`、`orders/fills/positions/account.csv`、`signals.csv`、`settlements.csv`、`feature_frames.json`、输入诊断和费用报告。

## 2. Live

Live分别配置交易腿行情、指数特征、参考条款和执行渠道。`recording`只记录目标；`model`使用参考价模型；`simnow`需要显式发单授权。连接凭证使用部署环境或项目`.env`，不写入命令与报告。

同一配置文件选择Live Recording：

```bash
python -u -m demos.08_option_vega.run_live \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id live_recording \
  --expected-trading-day 20261106
```

日期须按实际会话交易日修改YAML或用CLI覆盖，不能按本机自然日期自动猜测。三个Live示例使用DolphinDB完成分钟流与原始指数Bar投影，期权仍为M1；本次配置接入没有增加Live M5或MA列选择能力。流表名、时间标签及累计量口径须与实际来源一致。

使用`live_model`或`live_simnow`前，在同一YAML中填写对应的`state_file`：新会话使用独立新文件，恢复已有会话同时设置`resume: true`。模板保留`null`，未填写时原有检查点校验会阻止启动，不自动创建空状态接管已有仓位。`live_simnow`已明确设置`enable_orders/confirm_simnow: true`；任一授权缺失、Recording带报单参数或Model带物理发单授权仍报错。恢复时仍需通过原有身份与仓位核验，不能把共享账户检查点直接交给独立`run_live`入口。

本配置接入面向`run_live.py`；下面的`shared --run-forever`持续共享管理入口仍沿用原调用方式和检查点。Live示例默认运行900秒，可修改`seconds`；一次运行结束或按Ctrl+C停止仍沿用原有资源清理与保存逻辑。

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

关注`分钟行情汇总`、`分钟决策汇总`与心跳。迟到的新Bar可只更新健康，不回填旧决策；真实过期、断流或账户冲突仍停止。模拟现金结算不用于生成Live柜台事实。

## 3. 自定义特征

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

注意本例是**当前计算的研究快照**，可用时间是本次计算完成时间。今天导出9月份的MA20，可以核验计算与读写，却不能直接参加9月份的时点回测；需要保留历史已发布MA记录，或另行实现按输入当时可见版本逐分钟计算的历史回放。不能把当前计算时间改成历史Bar时间。超过帧期限的MA不会进入已封闭决策。

离线测试覆盖20条输入计算MA20、声明与标准文件读回、原策略候选/目标变化，以及预热、缺口和迟到拒绝；这些是夹具验证，不是收益表现或原生成交回测结果。

## 验证与诊断

```bash
python tests/run_option_vega_offline.py
python -m unittest tests.test_option_vega_native -v
python -m scripts.integration.options.check_backtest_data --help
python -m scripts.integration.options.recording --help
```

离线入口采用SDK/柜台夹具，原生测试须在安装真实Bomber扩展的独立进程运行。日志字段见[历史字段说明](../../doc/strategies/OPTION_VEGA_MINUTE_LOG_FIELDS.md)，历次报告与交付记录移至[历史文档](../../doc/strategies/OPTION_VEGA_HISTORY.md)，不作为当前启动指南。
