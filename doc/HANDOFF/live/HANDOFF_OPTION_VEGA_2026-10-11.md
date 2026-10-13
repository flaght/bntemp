# 08期权Vega回测与Live改造

2026-10-12最新日历接入与自动Recording核验见
[日历与Recording交接](HANDOFF_OPTION_CALENDAR_RECORDING_2026-10-12.md)：
Live默认公共编译日历，不再要求日历文件。新版本本地验证已过，真实Recording待远程返回。

08已完成五处改造，回测与Live使用相同策略和自身装配模块。行情来源和执行端
分别配置，策略不判断渠道。代码不跨引用01/04。远程Linux两组43项离线测试
已通过；受影响公共组、本次实际分钟流和交易闭环继续分别验收。

## 五处改造

| 项目 | 实现和行为 |
| --- | --- |
| 策略仓位与在途订单 | 策略只读归属成交和冻结分配中的自身未成交数量；实际成交驱动期货对冲，其他策略的仓位、挂单不改变本策略Delta。 |
| 两种时间 | 原始`valuation_ns`用于选约和Greeks，实际接收`request_ns`用于执行时效。Replay仅豁免历史日期年龄，保留来源日期、交易日、接收健康和时间倒退校验。 |
| 分钟帧 | 公共缓冲聚合多个已收盘Bar来源，默认等待2秒后封闭。无成交候选不参与选约，不补造Bar；持仓及必要对冲行情失效闭闸。 |
| 独立指数 | DolphinDB指数流单独注册、校验和绑定，没有执行路由。其估值日须与期权/期货一致，不以期货价格代替指数。 |
| 恢复与停机 | 独立检查点保存选约/目标，公共持久化保存成交和归属。恢复保留成交、清空报价和IV，以新鲜行情重算目标；不重复当日开仓、不续旧未完成开仓。停机保留持仓，主动清仓日需显式配置。 |

`--mode recording`不连接TD、不产生成交；`model`维护独立参考价模型账本；
`simnow`连接原生CTP，需要显式报单授权，可选strict/observe/off物理账户对账。
`build_runtime(args, account_routes=...)`复用公共一对多执行装配；没有实现第三方
厂商网络协议。直接CTP进程仍受账户锁约束，策略状态文件另有独立锁。

08保持固定估值日/资料会话。资料在读取期间变化或刷新出现新版本将停止；
原生执行还检查合约在实际TD日仍上市有效。基础表date/listDate是上市日期，
不是每日资料来源日；有available_ns时检查已发布，没有时明确记录内容指纹和
读取时刻证据。当前同日恢复要求来源、参数、条款、日历和政策一致。

## 文件清单

路径相对项目`pro`。完整同步摘要见
[PF2026101102_SHA256.txt](../../evidence/checksums/PF2026101102_SHA256.txt)。

| 范围 | 文件 |
| --- | --- |
| 08入口和装配 | `demos/08_option_vega/run_backtest.py`、`run_live.py`、`assembly.py` |
| 08策略和恢复 | `demos/08_option_vega/strategy.py`、`checkpoint.py`、`live_runtime.py`、`recovery.py` |
| 公共行情 | 新增`bomber/framework/market/frames.py`；复用`market/stream/aggregation.py`已有MID聚合 |
| 公共资料 | `bomber/framework/dataprep/metadata.py`、`dataprep/live_options.py` |
| 公共策略归属查询 | `bomber/framework/trader/template.py`、`runner.py`、`execution/attribution.py` |
| 公共CTP装配 | `bomber/framework/trader/execution/builders.py`、`execution/ctp/planner.py`、`execution/ctp/limit_planner.py` |
| 公共Live | `bomber/framework/trader/runtime/live/local.py`、`channels/ctp.py`、`channels/dolphin.py` |
| 测试 | `tests/test_demo_option_vega.py`、`test_option_vega_live.py`、`test_option_vega_assembly.py`、`test_position_attribution.py`、`test_live_channels.py`、`run_option_vega_offline.py`、`run_cross_section_acceptance.py` |
| 文档 | 08的`README.md`、本交接、交接索引、规范架构和协作登记 |

CTP关闭规则由08显式指定CFFEX通用CLOSE；原有SHFE/INE今昨仓规则默认保持。
LAST聚合保持成交语义，复用已有MID聚合输出零成交量报价Bar且不伪造TradeTick。

## 离线验证

本机Python3.12运行结果：

- `tests.test_demo_option_vega`、`tests.test_option_vega_live`：33项通过。
- `tests.test_live_common`、`tests.test_live_channels`、`tests.test_live_recovery`、
  `tests.test_execution_composition`：112项通过。
- `tests.run_option_vega_offline`：29项通过，包括10项实际08执行装配和19项归属回归。

第三组仅替换本机缺失的SDK行情实体和未使用的SDK桥；实际Runner、归属、风控、
订单状态机及JSON持久化仍为项目代码。用例覆盖部分成交后对冲、重复成交去重、
他人挂单隔离、Model/Recording、恢复不重复开仓、失败恢复保留原文件、缺少
持仓腿闭闸、指数日期不一致、MID语义和显式CFFEX关闭计划。

远程Linux项目`pro`目录执行以下命令，使用原生SDK重新检查：

```bash
python -u -m tests.run_cross_section_acceptance \
  --only option-vega --only option-vega-live \
  --only live-common --only attribution --only assembly
```

该命令无网络连接或柜台报单。当前不能将本机替身验证记作远程原生通过。

### 远程Linux期权测试结果

用户完成源码同步后反馈：`option-vega`33项OK，测试0.185秒，模块exit_code=0、
耗时0.829秒；`option-vega-live`10项OK，测试0.200秒，模块exit_code=0、耗时
1.298秒。集中入口报告全部选定离线模块通过。共43项通过，包括目标环境SDK
实体下的10项真实框架装配；没有使用本机SDK实体替身入口。

这些会话使用行情与执行夹具。日志中的`valuation_day=20260813`是测试估值日，
Model/Recording就绪不是实际行情或柜台成交证据。输出摘录没有报告目录，
不补造路径或未运行组的结果；结构化记录见
[远程期权离线证据](../../evidence/live/OPTION_VEGA_OFFLINE_20261011.json)。

后续请求的公共回归命令如下，无需重复已通过的两组期权测试：

```bash
python -u -m tests.run_cross_section_acceptance \
  --only live-common --only attribution --only assembly
```

用户后续输出确认`attribution`19项OK，测试0.017秒，模块exit_code=0、耗时
1.081秒；新增自身冻结订单份额用例通过。集中入口再次报告全部选定离线模块
通过。摘录只展示attribution汇总，live-common/assembly本次的单组计数和日志
未展开，不补记其具体结果。此反馈不要求重复已通过组，明细记录于
[归属回归证据](../../evidence/live/OPTION_VEGA_ATTRIBUTION_20261011.json)。

本次仅更新文档和证据，运行代码仍是PF2026101102。历史同步摘要保留，当前
含文档的完整摘要见
[PF2026101102_REV3_SHA256.txt](../../evidence/checksums/PF2026101102_REV3_SHA256.txt)。
远程只部署代码时继续使用`PF2026101102_RUNTIME_SHA256.txt`。

## 在线验收顺序

先按[08 README](../../../demos/08_option_vega/README.md)完整命令运行Recording，
明确真实日历、原始MD日期和同日指数已收盘流表；随后Model验证归属成交与恢复，
最后原生SimNow验证订单、成交、今昨仓/通用关闭和停机撤单。`--require-fills`
要求本次出现归属成交；不加该参数的passed只代表分钟帧验收，运行时段也必须
覆盖原始行情选约时点才能验证开仓。

真实DolphinDB指数流、CTP期权订阅、CFFEX真实报单与完整原生历史回测待验。
回测仍用权利金比例近似保证金，Model同步参考价成交不代表盘口成交能力；
本轮未增加真实期权卖方保证金、行权或到期结算模型。在线状态文件、账户和
真实仓位没有在本机连接或修改。
