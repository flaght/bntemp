# CTP / Binance 实盘接入交接（2026-10-07）

PF2026100920最新：用户提供attribution18项OK（0.016秒、模块exit0/1.083秒）及集中入口“全部选定离线模块通过”，本次选定离线阶段通过，无需重复。实际选定列表及新报告路径未提供，不补造其他组独立条数；源码/测试保持REV2，本轮仅证据文档更新。下一步当前TD账户只读、CU/PB/NI/AU/AG组合参考与Recording，实际柜台成交待验。

PF2026100920首轮增量：cross-section-acceptance-1791555739441173420的18项15通过/3测试顺序失败，后七组未运行，完整假柜台生命周期通过。已仅按真实合约修正测试并新增五品种Recording用例（共19项）；用户最新选择CU、PB、NI、AU、AG，公共交易源码无需修改，先核对当前资料和Recording目标再确定模拟报单预算。修正版摘要PF2026100920_REV2_SHA256.txt及[最新五品种步骤](../../../demos/04_cross_section/SIMNOW_ACCEPTANCE.md#当前五品种测试步骤)，仍待远端离线/在线，不能登记整体通过。

2026-10-09新增PF2026100920：用户指定04_cross_section下一步SimNow实盘化并确认RB、HC首轮。新增04固定多合约实时装配，复用原策略/公共执行/归属/持久化，默认Recording；公共限价规划器/执行工厂及逐合约保留目标推进兼容扩展。源码完成、离线与柜台未运行；本机不执行Python/编译/联网。步骤见[04验收说明](../../../demos/04_cross_section/SIMNOW_ACCEPTANCE.md)，部署清单PF2026100920_SHA256.txt；01最新实际交易证据见[前策略交接](HANDOFF_SIMNOW_CU_NIGHT_AND_NEXT_STRATEGY_2026-10-09.md)，不迁移为04通过。单策略独占空仓、固定主力，首期SHFE/INE，无自动指标恢复/动态换约；跨聊天待同步。

2026-10-08 新增参考数据库工厂与01可选DolphinDB装配，记录为并行约定PF2026100802。字段及时间口径已由用户确认；第二轮远程数据源23/23通过，公共回归29/30通过、RolePriceStore冲突收盘用例失败，数据库版entry未执行。角色研究价修复登记为PF2026100803，代码完成待远程复验；步骤见[参考数据库核验](../../../scripts/integration/reference_data/README.md)，不得将离线数据源通过解释为本次数据库或柜台验收通过。

第三轮补充：公共回归30项通过，新增2项测试部署待核对；角色研究在M0/M1/M2通过后因旧examples导入失败，角色信号及数据库版entry尚未执行。测试装配已迁移到公共dataprep并增加新增测试方法部署检查，修正版待远程验证；公共运行代码本轮未修改。

第四轮最新结论：2026-10-08远程公共回归32/32、角色研究、角色信号及entry21/21全部通过，报告为 `tests/results/main-ema-acceptance-1791443131209031509`，部署路径和摘要一致。结合此前数据源23/23，01数据库装配相关离线验收已完成；默认角色研究回归已去旧examples依赖。当前仍待真实DolphinDB资料只读和当前主力SimNow实时Recording，测试夹具中的SimNow报单／成交不属于真实柜台证据。命令见上述参考数据库核验，本轮仅维护文档记录。

随后真实DolphinDB只读报缺少TD日2026-10-08因子，用户澄清应读取前一交易日原始日表。PF2026100804将01数据库和只读入口默认改为source，先选此前最近期限结构来源日，再查同日因子，回测／文件默认trading保持兼容。代码与新增用例完成待复验（references29、公共回归32、entry22），第四轮通过证据不覆盖此次日期变更；同步清单及命令见参考数据库核验。

最新结果：PF2026100804修正版references29/29、公共回归32/32、entry22/22远程通过（报告main-ema-acceptance-1791444863148068678）；真实DolphinDB RB只读通过，指定TD日2026-10-08、role_source_day=factor_date=2026-09-30、rb2701.SHFE、累计因子1.181055、tick1、multiplier10，结果为tests/results/reference-data-20261008.json。本次资料日期及真实三表读取验收完成，无源码变更或重复离线测试；下一步01实时SimNow Recording，当前策略的真实MD／TD链和柜台成交仍待验证。

后续实盘接入可与回测优化并行开展，具体采用[并行工作协调约定](../coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md)。接手时先读取该约定及未关闭的公共变更记录；涉及公共框架变化必须在修改前通知用户，完成或回退后再通知。并行开发不改变本文件的柜台验收和真实资金授权边界。

## 1. 范围与结论

项目为 `/Users/kerry/work/bomber1/pro`，远程 Linux 对应目录为 `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro`，运行环境为 `uv-nautilus`。本交接汇总截至 2026-10-07 的已有证据，不把历史日志当作当前柜台状态，也不把代码存在或语法检查视作实盘验收。

当前公共框架已有 CTP、Binance 的在线行情与执行基础组件。旧 `examples/single_ema` 的单 EMA 接入已有部分模拟柜台验证；当前 `demos/01_main_ema` 至 `demos/09_option_delta` 九个策略尚未完成统一 `run_live` 开发和验收。两者不能混为一谈。九策略及公共框架现状以 [2026-10-07 框架交接](../demos/HANDOFF_DEMOS_FRAMEWORK_2026-10-07.md) 为准。

## 2. 分端状态

| 接入端 | 已有证据 | 尚未验收或实现 |
| --- | --- | --- |
| CTP SimNow | 第一套环境的新鲜行情到 EMA Recording 已验证；第二套历史回放环境已完成单 EMA 开仓、平今、反手成交及执行回报链。 | 第一套正常交易时段 EMA 自动下单；带仓、活动订单和跨日重启恢复；多策略账户级风险预算。最后一次日志中的仓位不是当前仓位。 |
| Binance U 本位期货 DEMO | 在线 1 分钟 K 线到同一 EMA 的 Recording 已验证；只读探针和受控 DEMO 下单入口已有代码。 | 本次账户身份、资金、全市场持仓、普通及 Algo 活动订单权威查询；小单受理、成交、部分成交、撤单、拒单；断线与跨进程重启恢复。暂无本次柜台验收证据。 |
| Binance LIVE | 有在线行情与部分通用交易基础组件。 | `examples/single_ema/ema_binance_live.py` 当前拒绝 LIVE 报单；真实账户身份核验、独立 LIVE 授权、账户级风控、状态持久化及重启恢复未完成；没有真实资金报单验收。 |

CTP 细节见 [CTP/Binance 交接](HANDOFF_CTP_BINANCE_NEXT.md) 和 [CTP SimNow 交接](HANDOFF_CTP_SIMNOW_NEXT.md)；DEMO 证据边界见 [SimNow/Binance DEMO 交接](HANDOFF_SIMNOW_BINANCE_DEMO.md)。这些文件记录的是 2026-09-25 前后的运行结果，执行新命令前须以当前代码和本次柜台查询复核。

## 3. 最近一次 Binance 接入任务的结果

“Binance 实盘接入”任务核对了上述交接与主要代码，完成静态审查及相关文件语法检查。本机没有可用的项目 Python 虚拟环境：尝试的解释器分别缺少 `numpy` 或 `dotenv`、`bomber`；当时也未取得 DEMO 凭据。因此该任务没有跑通无网络回归，没有连接 DEMO 或 LIVE 账户，也没有发单。语法检查通过不等于执行链通过。

静态审查发现：受控客户端当前只有 DEMO 授权路径；本地固定的账户 ID 不能证明真实账户身份；在途订单与运行状态缺少跨进程持久化，已有恢复依赖进程内已知状态。这些是 LIVE 前必须补齐的事项，仍需以当前源码复审。

## 4. 接手后的验收顺序

1. **核对环境与版本**：在远程项目目录确认当前源码、`uv-nautilus` 解释器和环境变量名称。凭据只从环境或既有 `.env` 读取，不复制到文档、命令记录或日志。先跑无网络回归；失败项逐项记录，不把未运行写成通过。
2. **DEMO 只读权威查询**：连接前确认是 DEMO 凭据；查询账户身份、资金、全市场持仓及普通/Algo 活动订单。超时、缺字段和未知订单都应闭闸，不视为空仓。
3. **DEMO 受控订单**：在本次账户快照和明确限额基础上，分别验收受理、成交、部分成交、撤单、拒单及执行回报与仓位/资金对账。策略自动入口会实际发模拟订单，应与只读探针分开运行。
4. **DEMO 故障恢复**：验证断线禁单、重新取得权威快照、活动订单恢复、进程重启后的在途订单与策略状态恢复；恢复前不得自动新增风险。
5. **LIVE 开发与只读预检**：实现并审查真实账户身份核验、报单字段及交易所精度、账户级预算、独立授权闸门、持久化和重启恢复。先做 LIVE 只读连接与权威对账。真实资金下单作为独立验收步骤，需用户明确授权。
6. **九策略统一实盘入口**：在公共执行链稳定后，逐策略开发和验收 `run_live`。回测成功、SimNow 成交或 Binance DEMO 成交均不能替代相应策略的实盘验收。

## 5. 远程首轮命令：不发单

在 Linux 项目根目录、已激活 `uv-nautilus` 的终端执行：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
python -u tests/run_p3_controlled_live.py --stage all
python -u tests/run_p4_binance_orders.py
python -u tests/run_live_backend.py --stage all
python -u tests/run_single_ema_online.py --stage offline
PYTHONPATH=. python -u examples/single_ema/binance_demo_readonly.py \
  --connect-demo --symbol BTCUSDT --seconds 30
```

最后一条需要已在运行环境配置 `BINANCE_DEMO_API_KEY`、`BINANCE_DEMO_API_SECRET`，只做 DEMO 账户查询，不授权报单。若前述结果通过，可另运行公开行情到策略目标的 Recording 检查：

```bash
BN_EMA_TIMEOUT=300 python -u tests/run_single_ema_online.py --stage 4
```

记录每项命令的时间、代码版本、退出码、关键结果及脱敏日志。不要在尚未核对本次 DEMO 账户状态前直接运行 `--enable-orders --confirm-demo`；不要将该开关用于 LIVE。CTP 新一轮运行也须先只读查询当前交易日、持仓和全账户活动订单，不能沿用旧日志中的交易日、仓位或限价。

## 6. 交接完成标准

为 CTP SimNow、Binance DEMO、Binance LIVE 分别维护验收状态：`未运行 / 失败 / 通过`，附运行环境、时间与柜台证据。至少分别证明行情健康、策略目标、报单受理、成交与撤单回报、仓位和资金对账、异常闭闸、断线及重启恢复。九个 `demos` 策略需要独立记录各自 `run_live` 能力，不能从单 EMA 的验收推断全部策略可实盘。

## 7. 当前主力 EMA 增量接入进展

2026-10-07 当前实盘工作线针对 `demos/01_main_ema` 交付固定主力 LIVE 装配和无网络集中验证入口。在用户的 Linux `uv-nautilus` / Bomber 1.230.0 环境中，signal 6/6、routing 10/10、CTP 7/7 及 regression 的 7 个旧入口均已通过，证据见[公共变更记录 PF2026100701](../coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100701-保留目标的分阶段执行推进)。

固定主力装配保持策略公式与信号版本，在后续新 Bar 决策完成后推进未完成的 CTP 开平目标；主力变化时关闭授权、请求撤单并要求恢复核对。LIVE 动态换约仍禁用，跨合约测试仅在 HISTORICAL 内存链路通过。上述验证没有连接 SimNow，本次柜台身份、查询、实时行情及策略交易尚未验收，也未完成完整 `run_live` 入口和跨进程状态恢复。

下一模块复用已有交易端只读探针，在远程项目根目录执行：

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

使用已配置的 SimNow `.env`／环境变量，核对本次登录、结算确认、仓位、资金及全账户活动订单查询完整结果；本入口禁单。通过后继续当前主力 EMA 实时行情到 Recording 验证，再接受控模拟订单。用户自行远程执行，不通过 SSH；本机仅静态读写源码和文档。

通道联调入口归属 `scripts/integration`，工程发布工具保留于 scripts 根目录，职责见 [scripts 说明](../../../scripts/README.md)。CTP TD 只读实现已迁至 `scripts/integration/ctp/td_readonly.py`，旧 examples 路径仅兼容转发；首次同步后开发环境在项目根目录运行 `python -u -m tests.run_ctp_td_readonly`，核对新入口 `.env` 定位、三查询与禁单，再进行本次柜台连接。2026-10-08 烟测先因强制导入远程不存在的旧 examples 包而中断，移除后又遇到直接文件命令无法加载本地 framework；当前烟测已支持包内相对导入，采用标准模块方式启动，不注入 `sys.path`。须同步 `bomber/__init__.py` 开发加载文件及 `bomber/framework`，复用已安装的 Bomber 内核；只读入口与烟测不要求部署 examples，修正版待远程复验。迁移状态见[PF2026100702](../coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100702-通道联调入口归入-scripts-integration)。

## 8 2026-10-08 深入验证与完整入口

用户远程以模块命令运行修正版 TD 只读烟测通过，覆盖配置定位、登录／结算确认、三项查询及禁单；该结果使用假 API，没有连接 SimNow。新一轮状态以[01 主力 EMA SimNow 接入验收](../../../demos/01_main_ema/SIMNOW_ACCEPTANCE.md)为准。

新增 `demos/01_main_ema/run_live.py` 与 `live_references.py`：复用角色表、当前 TD 交易日累计因子和真实合约条款，默认 Recording；显式 SimNow 授权后接通原生 TD、限价规划、受控客户端和写前／回报后检查点。采用单交易日、独占空仓账户的固定主力路由，主力／条款变更、历史时间戳、断线、行情异常、查询或落盘失败均停止。停机撤单并做最终总仓及资金查询，不自动平仓，也不自动恢复旧 EMA 或接管带仓。

限价规划器迁入 `bomber/framework/trader/execution/ctp/limit_planner.py`，旧入口兼容导入；当前 CTP 增量不再依赖 examples。新集中命令为 `python -u -m tests.run_main_ema_acceptance`，覆盖9组离线模块；失败即停止并保存源码校验及日志。2026-10-08 用户远程首轮前8组全部通过；entry原17项中14项通过、3项因受控客户端默认时钟在导入时绑定而失败。在线装配已显式传入入口墙钟回调，保留120秒时效闸门，新增过期请求拒绝用例后为18项，待远程仅复测 `--only entry`。本机仅静态检查；当前柜台只读、当前策略实时 Recording 和实际 SimNow 订单尚未验收。公共变化及复测记录见[PF2026100801](../coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100801-主力-ema-simnow-完整入口及深入验证)。

2026-10-08 后续远程结果：entry修正版18/18通过（0.421秒、进程退出码0），连同前8组证据，本轮离线9组均通过。真实TD只读完成登录／结算确认及三类查询，净仓合约0、CNY资金快照、活动订单0。当前主力Recording随后完成空仓预检，但缺少TD交易日2026-10-08／RB／main累计因子，在建立参考资料时失败，bars_used=0、报单0，MD尚未开始验证。需补齐实际因子文件的当日主力累计因子、真实symbol及匹配条款，再执行Recording；当前策略SimNow订单仍未验收。本次仅补充证据和数据要求，不改公共代码。

## 9 2026-10-08 第二套环境完整链路

最新资料日期口径已按PF2026100804修正：原始DolphinDB日表使用此前角色来源日／同日因子，不要求补造TD日因子；references29项、公共回归32项、entry22项和真实RB数据库只读均已远程通过。source模式Recording资料正常，但30秒MD／TD未就绪，未产生Bar或订单。

随后第二套MD40011原始Tick及标准QuoteTick／TradeTick、TD40001只读查询通过；MD原始TradingDay=20260930、ActionDay=20260929，TD探针未打印日期，净仓合约0、CNY资金快照、活动订单0。用户要求继续第二套而非等待第一套时段，PF2026100805新增显式工程联调模式：原始Tick不改，ReceiveTimeTradeTickBarFeed以实际接收时间聚合Bar；MD按参数固定旧行情交易日，TD固定本次登录日，参考资料按当前TD及可用时间加载。这不构成历史回测结果。

默认实时／回测路径保持原事件时间语义；会话开放Bar时重新排除部分启动分钟。订单授权、120秒请求墙钟检查、健康、仓位／资金／活动订单、名义金额、固定路由和检查点继续执行。新增代码完成，entry34项及受控／健康回归待用户远程运行；第二套策略Recording和真实模拟柜台成交仍待验收。同步5文件和三步命令见[第二套完整链路](../../../demos/01_main_ema/SIMNOW_ACCEPTANCE.md#第二套完整链路pf2026100805)，公共协调见[PF2026100805](../coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100805-第二套-simnow-完整链路联调)。本机仅静态读写，没有执行Python、测试、编译或连接远程。

后续远程证据：夹具修正版entry34/34通过（1.385秒），报告main-ema-acceptance-1791449976479272698；controlled／health均已通过。真实第二套Recording通过，报告simnow-1791449059995543664，832条Tick、9根Bar、LONG 1手目标、报单0，停机总仓空且活动订单0、清理无错误。实际MD／TD日20260930，角色／因子日2026-09-29。上述替代本节早先待验收状态，但尚无当前策略真实模拟柜台成交。

## 10 2026-10-08 单实盘组装入口

按用户要求，PF2026100806删除demos/01_main_ema/live_references.py、live_runner.py。公共dataprep/live_role.py提供FileRoleReferences／SourceRoleReferences；trader/live_roles.py提供FixedRoleLiveRunner／SessionRoleLiveRunner。role、逻辑目标键、策略处理时间与会话检查显式注入，不读取EMA配置或依赖demo。01仅run_live.py承担实盘组装和生命周期，strategy.py／run_backtest.py未修改。默认文件与数据库日期规则及SimNow门控、风控、授权、落盘和停机行为保留。

本机仅静态审阅，迁移后references预期30、routing10、ctp7、entry35项待远程复验；旧34项和真实Recording保留为迁移前证据。按[迁移清单](../../../demos/01_main_ema/SIMNOW_ACCEPTANCE.md#单实盘入口迁移pf2026100806)整组同步7文件并删除远程旧2文件，再复测四组，通过后继续第二套受控模拟订单。共享框架记录[PF2026100806](../coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100806-实盘仅保留一个组装入口)已在当前对话通知，跨任务同步仍待执行。

最新首轮报告main-ema-acceptance-1791458546029341474：references30/30及routing10/10通过，CTP6/7通过；主力变更测试仅替换策略内部data_hub，没有改变Runner显式注入的参考服务，因此未触发预期异常。entry35项未运行。当前只修正tests/run_main_ema_simnow.py，让装配时两方共享同一as-of参考源并验证持续闭闸，公共框架与实盘入口不变；仅同步该测试后复验ctp及entry，已通过组无需重复。修正版尚无运行证据，不进入模拟报单验收。

随后修正版远程四组全部通过，报告main-ema-acceptance-1791458768127640128：references30、routing10、CTP7、entry35，共82项退出0，entry耗时1.405秒。新公共适配／Runner和run_live等打印路径／摘要一致，CTP主力变更持续闭闸、文件／数据库次主力适配、无EMA配置的策略绑定及完整假API链已通过。相关公共迁移离线验收完成，无代码新变动，不需重复测试；当前对话已通知，跨任务待同步。接下来直接使用单run_live进行第二套受控模拟订单；入口重新核对实际会话、参考资料、总仓与订单，停机撤单并对账，不自动平仓。旧真实Recording是迁移前证据，此轮假TD的simnow订单不能登记为实际模拟柜台成交。

### PF2026100807 统一回测与实盘组装（离线验收通过，在线待复验）

用户随后授权统一两入口的组装结构。新增公共执行构建、StrategyBindings／assemble_strategy、ManagedLiveRuntime／LiveSessionControllerPort、CtpSessionLifecycle及LiveRunReport。run_live只选择参数与积木，通过Runtime管理会话；run_backtest复用同样的执行／绑定组装，历史Runtime不变。EMA策略文件、实盘资料日期、第二套接收时间规则、固定角色、显式模拟授权、风控、落盘与停机不自动平仓均保留。公共CTP账户锁与旧主力EMA锁同时持有，兼容旧入口并与其他公共CTP策略互斥。

最新82项与真实Recording都是本轮重构前证据。本机没有执行Python、编译、测试或回测；assembly10项、原runtime委托、routing10、ctp7、entry38项待远程验收。先按[PF07同步清单](../../../demos/01_main_ema/SIMNOW_ACCEPTANCE.md#统一组装结构pf2026100807)整组同步10文件并执行集中五组，再对比原回测样本、复验第二套Recording，之后继续受控模拟订单。新版本没有实际柜台成交证据。公共变化已经在当前对话前后通知并登记[协调记录](../coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100807-回测与实盘采用一致的组装结构)，跨任务待同步。

最新报告main-ema-acceptance-1791463560272015358已完成上述五组：assembly10、routing10、CTP7、entry38共65项单元测试以及原runtime委托验证全部退出0；entry1.421秒。新增公共模块、两入口及相关测试的加载路径／摘要一致。异常场景failed摘要均有ok断言，清理、锁互斥、历史下一事件成交和完整假柜台入口通过。当前不修改运行代码，不重复已通过组。实盘继续新入口第二套Recording（含TD只读预检／停机对账），取得本轮真实目标后再受控模拟交易；完整Nautilus样本对比独立待完成，真实策略柜台成交仍未验收。当前对话已告知，跨任务待同步。

新入口真实Recording首次预检被已有仓位拒绝：simnow-1791463768539219912，TD=20260930，rb2701.SHFE多仓0／空仓1手、活动订单0。报单0、Bar0、MD未启动、参考资料未加载；停机总仓仍为1手空仓且cleanup_errors为空。连接及空仓守卫已有本次网络证据，但不计为完整Recording通过。本次未创建该仓，来源未确认；用户先在同一第二套环境核对处理既有模拟持仓或用空仓账户，再重跑原命令。不改源码、无需重复离线五组、不放宽带仓接管规则；在线目标及柜台成交仍待验收。

### PF2026100808 独立一手模拟平仓工具（待远程）

用户再次运行simnow-1791464101801339558，仍因同一空仓1手拒绝，查询和停机无异常。新增scripts/integration/ctp/close_existing.py，默认只读今昨仓预检，显式参数才发送一笔模拟平仓；拒绝其他仓位、双向／多手、活动订单、方向或TD日变化及未知今昨／套保属性。没有EMA自动平仓或带仓接管。公共transport新增不可变持仓明细方法，自有TD绑定补PositionDate／HedgeFlag／TodayPosition／YdPosition；原策略、run_live及既有API行为保持。

源码和12项假API测试完成，本机没有运行Python或编译；按[操作及5文件同步清单](../../../scripts/integration/ctp/README.md#独立处理既有一手模拟仓位pf2026100808)先执行td／close-existing离线组，再重编现有扩展、smoke及真实无单预检。用户核对当次限价和TD日、方向后方可显式发送模拟平仓，工具从柜台属性选择平今／平昨。清仓通过再原Recording；平仓工具成交不计为当前EMA策略成交。公共修改、原生扩展依赖与待验证状态已在当前对话告知，协调记录PF2026100808，跨任务待同步。
