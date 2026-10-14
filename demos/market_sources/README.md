# M01 公共行情来源配置

01_main_ema、03_calendar_spread、04_cross_section、08_option_vega 共用
`bomber/framework/market/sources.py` 的配置、参数解析及 Feed 工厂。算法仍在各自原 `strategy.py`，
通过原 Runner 的标准分钟 Bar 绑定运行；行情选择不改变执行模式、参考资料、指数特征或历史预热来源。

## 选择方式

已有 Live 命令追加 `--market-config demos/market_sources/ctp.json`，或直接使用 `--market-source ctp`。
不指定时保持原 CTP 默认。已有回测命令可追加 `--market-config demos/market_sources/file.json`
或 `--market-source file`，不指定仍使用文件；文件路径、业务 parser 和模拟执行保持原入口参数。

Live 使用 DolphinDB 时，已有命令追加：

```bash
--market-config demos/market_sources/dolphindb.json \
--market-bar-table '<已核实的共享分钟流表>' \
--market-bar-timestamp end \
--market-volume-mode per-bar
```

`end` / `per-bar` 只是参数写法示例。必须核实表的实际含义再选 `start|end`、`per-bar|cumulative`，
也可以将这三个真实值直接写入 JSON。示例 JSON 故意留空，避免猜测数据源；未填齐会在连接之前报错。
表必须推送已完成的标准 OHLCV 分钟输入，包含策略声明的真实合约，DFS 历史表不能直接当推送流。
08 已有 `stream_cffex_1min` 证据不意味着该表覆盖 RB、HC、CU；01/03/04 必须核实对应业务流。

命令行显式参数覆盖 JSON；未声明的字段沿用默认。01/03/04 的流表也可来自 `DDB_MARKET_BAR_TABLE`，
08 保留 `DDB_OPTION_BAR_TABLE`。连接凭据仍用 `DDB_HOST/PORT/USERNAME/PASSWORD/STREAMING_PORT`，不写入 JSON。
Live JSON 中的 `source` 可为 `ctp`、`dolphindb`、`bn-ws`，回测为 `file`。

BN-WS 公共工厂支持 BINANCE 现货和永续：`market_type=spot|futures`，可选 `ws_base_url`；
未指定 URL 时按市场类型选择已有 WS 端点。只消费完成的 1 分钟 Kline，永续标识为 `*-PERP.BINANCE`。
当前四个 demo 的业务输入是国内期货/期权，选择 BN-WS 会在连接前明确拒绝；不会将 Binance 价格
伪装成 RB、CU、MO、IM。使用 BN 行情需要匹配 BINANCE 标的的业务参考输入，不能只改合约后缀。

## 时效、执行和恢复

01/03/04：CTP 继续从真实 Tick 聚合分钟并检查原始深度；DDB 已完成 Bar 按配置转换为
与 CTP 相同的分钟末纳秒时间。重复分钟忽略，未来/未完成、时间回退、缺腿或超龄均闭闸；
多腿只在同一分钟输入齐全后交给原策略。分钟源逐合约时效默认 120 秒，首帧等待预算至少覆盖一分钟来源。
03 的分钟来源用完整双腿帧推进保留执行目标，不生成模拟 Tick。08 保留原 MinuteFrameBuffer 和指数特征装配。

`--mode recording|model|simnow` 仍单独选择执行。01/03/04 的 DDB Recording/Model 不需要 CTP MD/TD 凭据；
SimNow 仍需要原 TD 授权和账户配置。CTP 默认路径保持原验收语义。参考资料可能仍需 DDB 凭据，
不能将行情不接 CTP 理解成所有资料不接数据库。

同进程共享账户、独立 worker 的各策略 `argv` 也可使用上述参数。
共享宿主按各策略解析后的配置判断是否需要 CTP MD，只给 CTP 设置 SDK flow_path。
新增来源身份进入报告和非 CTP 检查点；同一路径的配置变化仍接受原身份核验，不能静默改源丢弃原仓位。

01 复用原历史准备钩子；本次没有完成 M06 的 DDB 历史窗口/实时接续验收，也没有给03/04新增历史窗口实现。
08 特征回测的 M05 缺口仍单列。

## 验证

在安装了项目原生引擎的环境运行，无网络连接：

```bash
python -m unittest tests.test_market_sources
python -m tests.run_option_vega_offline
python -m unittest tests.test_live_common tests.test_live_channels tests.test_live_recovery tests.test_execution_composition
python -m unittest tests.test_shared_account tests.test_shared_process_cli tests.test_shared_ipc
```

本机缺少原生引擎时，可复用已有SDK实体夹具运行专项：

```bash
python -m tests.run_option_vega_offline --market-sources
```

此入口明确使用SDK实体与EMA替身，只验证配置、输入契约和目标链，不能登记原生指标/撮合通过。

专项覆盖配置覆盖顺序、构造不连接、CTP Tick→Bar、DDB时间标签/逐腿时效/缺腿、BN完成Kline和业务边界，
以及01/03/04原策略的DDB分钟目标链。实际数据库/柜台组合的输入覆盖和运行报告仍须分别验收。
