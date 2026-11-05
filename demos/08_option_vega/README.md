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
  --option-product MO --future-product IM --index-code 000852 \
  --start-day 2026-09-01 --end-day 2026-09-11 \
  --frame-wait-seconds 5 --bar-timestamp end \
  --log-level WARNING --no-tearsheet --require-fills
```

`--data-root`按公共路径规则推导交易腿与基础条款，也可逐项传`--fut-dir --opt-dir --fut-basic --opt-basic`。不读取`.env`补文件路径。完整参数见`python demos/08_option_vega/run_backtest.py --help`。

- 分钟行情使用真实期权与同月期货；`--bar-timestamp start/end`只调整交易腿标签。不会以前一分钟价格补造同步开仓输入。
- 指数是标准特征CSV/Parquet，旁边必须有同名`.manifest.json`。Schema/计算版本、原始字节SHA256与时间标签均核验。传输需保留文件字节，包括换行格式。
- 可传`--calendar`提供完整日期/is_trading_day日历；否则以已知行情日期推导。计算IV所需日历必须覆盖合约最后交易日。
- 每日期权交易时点默认13:58，对冲默认10:00/11:00/13:30/14:00/14:55。每日新增目标累加，持仓使用成交归属数量；总量与单腿限额仍生效。
- 最后回测日默认14:50清仓，指数还须至少覆盖之后两分钟，交易腿也需真实后续报价完成撮合。`remaining_positions_or_working`和`report_errors`必须为空。

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

`feature/generate.py`负责生成/导出，提供`generate_features`双文件接口和`generate_index_features`单独指数导出API；`feature/__init__.py`负责`index_bundle`加载声明、`read_index_features`读取文件；`feature/backtest.py`负责`build_backtest_runner`驱动分钟帧决策。生成命令统一使用`feature.generate`。回测入口不隐式连接数据库、重复下载或覆盖数据。Live继续复用包内的声明接口。

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
