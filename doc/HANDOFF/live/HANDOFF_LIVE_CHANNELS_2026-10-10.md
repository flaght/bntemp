# Live渠道分离交接（PF2026101035，2026-10-10）

本轮按用户指定的live目录结构重构。铁律：渠道来源显式、行情与交易分别装配；
策略只消费标准行情和输出逻辑目标。沿用RuntimePort / MarketDataFeed /
ExecutionBackendPort三维架构，不新增另一套策略框架或交易状态机。

## 目录与职责

```text
bomber/framework/trader/runtime/live/
├── __init__.py
├── runtime.py          # Managed/Direct在线生命周期
├── contracts.py        # 控制器/渠道端口、来源身份、结构化就绪结果
├── profiles.py         # 通用时间语义、时段策略、分钟Feed工厂
├── health.py           # 通用时效与流健康
└── channels/
    ├── __init__.py
    ├── ctp.py          # CTP行情/执行分别检查，SimNow配置及原有生命周期
    └── binance.py      # BN行情/执行分别检查
```

Runtime不识别CTP/BN/DDB。ChannelIdentity分别记录domain、channel、source_id；
SessionReadiness完整收集每个渠道的失败原因，结果可序列化为报告的
session_readiness。适配器异常、来源冒认、空通道组合均闭闸。

CtpMarketChannel读取MD日、健康状态、接收心跳及注入的逐合约深度门禁；
CtpExecutionChannel读取TD日、连接状态及当前SimNow交易授权，不读取MD。
BinanceMarketChannel只接受在线realtime；BinanceExecutionChannel读取执行节点
连接与注入的已对账账户状态，不套用CTP交易日或日历。CTP/BN执行检查均可
显式注入connection_ready；第三方实现无需冒充原生Driver或TradingNode。

DolphinDB标准流可用StreamHealthCheck明确绑定market/dolphindb身份；文件回测
继续使用既有FileReplayFeed+历史Runtime+模拟Backend。其他执行端实现已有
ExecutionBackendPort，并单独提供ChannelReadinessPort，不通过行情名称选择执行端。
本次新增的是Live分层和适配接口，没有新增第三方柜台协议、文件在线回放入口
或provider CLI，也没有扩大真实资金下单授权。

## 时间与门禁

CtpLiveProfile继承通用MarketTimeProfile，额外保留MD回放日约束。realtime按事件
时间聚合；replay按接收时间构造Tick副本，Bar和执行推进一致。上游原始Tick不改，
审计保留源时间；Tick副本保留主动买卖方向。04不再在Runner内重新转换Tick。

接收时间聚合器现在使用声明的instrument-max-age-seconds，修正以前固定10秒
而逐合约门禁可设30秒的不一致；CTP全流接收心跳仍独立保持10秒，与逐合约
门禁用途不同。replay仅豁免历史事件年龄上限，仍检查接收新鲜度、未来时间、
源时间缺失/倒退、MD日和固定合约覆盖。TD断线后保留的交易日不能证明连接就绪。
参考数据继续使用当前TD及墙钟/as-of规则，不因为行情回放而跳过dataprep校验。

LiveProfile支持注入时段判定；默认不附加CTP日历。已有01 CU持续模式日历和恢复
流程保持，后续日历统一可以在该接口上收拢。

## 接入与兼容

01_main_ema和04_cross_section采用新目录；CTP/BN联合入口接入两种渠道的独立
检查，参考缓存由控制线程刷新，行情回调不查询数据库。关闭单个源时，其来源
和失败用途仍可区分；跨市场对冲关系必须显式声明DataBinding/ExecutionRoute。

旧runtime/{managed,direct,ctp,live_policy,ctp_setup}.py仅重导出新实现，避免测试、
旧脚本和部署突然断导入。ctp_account/ctp_positions是原有CTP账户核验工具，本次
未重写或改变状态格式。策略文件、回测入口及交易账本/规划器未修改。
当前CtpSessionLifecycle仍为原有受控SimNow生命周期；它不会自动支持所有第三方
通道，新组合通过控制器端口及独立渠道积木装配。

## 验证

本机Python3.12执行48项纯Python回归通过：原公共21项加新渠道/Runtime/聚合27项。
其中RuntimeMode枚举、原生行情实体和Feed基类使用替身，实际Live控制代码、渠道
规则与聚合代码直接运行；这不是原生引擎集成结果。

原生test_cross_section_live/run_main_ema_live/test_strategy_assembly三组在导入
bomber.framework.fixes.Bar时失败，未进入用例；本机缺原生实体不能替代Linux
uv-nautilus复测。未连接CTP、BN或数据库，未操作账户或保存交易检查点。

完整同步后，在项目pro目录执行：

```bash
sha256sum -c doc/evidence/checksums/PF2026101035_SHA256.txt
python -u -m tests.run_cross_section_acceptance \
  --only live-common --only cross-section --only entry --only continuous --only assembly
python -u -m unittest tests.test_joint_live
```

live-common组现在同时运行test_live_common和test_live_channels。若只验证不依赖
原生引擎的公共层，可以直接执行：

```bash
python -u -m unittest tests.test_live_common tests.test_live_channels
```

就绪诊断：session.readiness.check().as_dict()可直接查看哪个来源失败；超时/poll
错误附带readiness，报告保存停机前最近一次检查。sources与issues分别列明market
或execution，不再只能看到False。当前命令参数无需改变。

## 本轮新增文件

- `bomber/framework/trader/runtime/live/__init__.py`
- `bomber/framework/trader/runtime/live/channels/__init__.py`
- `bomber/framework/trader/runtime/live/channels/binance.py`
- `bomber/framework/trader/runtime/live/channels/ctp.py`
- `bomber/framework/trader/runtime/live/contracts.py`
- `bomber/framework/trader/runtime/live/health.py`
- `bomber/framework/trader/runtime/live/profiles.py`
- `bomber/framework/trader/runtime/live/runtime.py`
- `tests/test_live_channels.py`

## 本轮修改源码/测试

- `bomber/framework/trader/runtime/__init__.py`
- `bomber/framework/trader/runtime/direct.py`
- `bomber/framework/trader/runtime/managed.py`
- `bomber/framework/trader/runtime/ctp.py`
- `bomber/framework/trader/runtime/live_policy.py`
- `bomber/framework/trader/runtime/ctp_setup.py`
- `bomber/framework/market/stream/aggregation.py`
- `demos/01_main_ema/run_live.py`
- `demos/01_main_ema/continuous.py`
- `demos/04_cross_section/run_live.py`
- `demos/04_cross_section/live_runtime.py`
- `demos/04_cross_section/recovery.py`
- `scripts/integration/ctp_binance/run.py`
- `scripts/integration/ctp/close_existing.py`
- `tests/test_live_common.py`
- `tests/run_main_ema_acceptance.py`
- `tests/run_cross_section_acceptance.py`

## 本轮文档与部署摘要

- doc/architecture/STRATEGY_ARCHITECTURE.md
- demos/01_main_ema/README.md
- demos/04_cross_section/README.md
- doc/HANDOFF/live/HANDOFF_LIVE_COMMON_2026-10-10.md
- doc/HANDOFF/live/HANDOFF_LIVE_CHANNELS_2026-10-10.md（新增，本文）
- doc/HANDOFF/coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md
- doc/evidence/checksums/PF2026101035_SHA256.txt（新增）

新SHA摘要覆盖PF1034完整基线加本次新增/修改文件，历史摘要保留。这里只提供
文件和核验列表，不打包。原生集成与两套SimNow在线验收状态仍开放。


## 2026-10-10运行机回归结果（附件d0b18ba4）

用户提供Linux uv-nautilus运行日志。附件SHA256：`9eda4c00c94566687053171d68a6344d614a92e223086977c2647a093eb957e2`。
本截取明确记录：

| 组 | 用例数 | 结果 | 时长 |
|---|---:|---|---:|
| entry | 53 | OK，模块exit_code=0 | 用例7.297秒，模块8.568秒 |
| assembly | 10 | OK，模块exit_code=0 | 用例0.032秒，模块1.263秒 |
| joint | 24 | unittest OK | 用例0.092秒 |

集中入口末尾报告“全部选定离线模块通过”。截取没有live-common、cross-section、
continuous各组的完整汇总或本轮集中报告路径，故不补造它们的数量、耗时或路径。

参考缓存过期、缺因子、资源关闭失败、启动各阶段失败、错误运行模式等输出
status=failed，随后对应测试ok，属于预期负例。joint的文件资料缺显式授权错误
由test_joint_cli_requires_both_authorizations_and_new_checkpoint（assertRaises(SystemExit)）验证，最终24项OK。
session_readiness.ready=True只表示渠道检查通过，参考缓存门禁仍独立拒绝过期资料；
两者同时出现在负例中符合分层，不代表参考校验被跳过。

entry的53项通过关闭PF1034数据库入口夹具回归问题；assembly验证共享装配、
回测入口及ManagedRuntime资源释放；joint验证既有双端分腿/门禁/恢复契约。
该结果为离线测试（假柜台/行情夹具），不是CTP或BN网络连接、柜台成交的新证据。
本轮只更新验证文档及独立EVIDENCE_SHA256，不修改源码或历史PF1035部署摘要，
无需因证据更新重复已通过测试。
