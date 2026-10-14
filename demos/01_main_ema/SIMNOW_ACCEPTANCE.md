# 主力 EMA SimNow 接入验收

## 当前启动选择修正（2026-10-14）

用户明确要求独立策略真实向SimNow报单：当前AU使用--mode simnow --strategy-position-source fills --account-reconciliation off，并显式启用enable-orders/confirm-simnow。策略仓位只按自己的归属成交更新，物理持仓不参与策略账本初始化/校准；CTP TD、资金/订单查询、健康检查和账户锁继续使用。此前独立Model解释不符合该需求，Model命令已从当前启动指南替换。

完整命令、独立SimNow状态文件及恢复条件见[README当前启动方式](README.md#当前启动方式独立策略向-simnow-报单2026-10-14)。最近AU MD返回20261015，当前命令要求MD/TD同日并先只读验证source20261014。此次fills/off真实SimNow报单/成交尚无新增通过结果。

前次AU Model反馈：声明20261014/source20261013，实际MD=20261015，被MD_TRADING_DAY_MISMATCH拦截；bars_used=0、model_reports=0、策略仓位0、cleanup_errors=[]，报告standalone_if_model/model-1791989464943840945。保留原Model文件，不能用它恢复SimNow。本次仅修正文档，未自行启动策略、连接数据库/柜台或报单。

本文下方RB模型通过和原生SimNow/strict验收均保留原模式，不代表当前AU fills/off已通过。

## 2026-10-11独立模型验收

CTP第二套Replay行情配独立reference-price模型、账户仓位政策off的真实在线
运行已通过：报告model-1791673733149458028，9根Bar、1笔模型SELL成交，
rb2701.SHFE目标/策略仓位/执行账本均为-1，物理账户观测为None，
cleanup_errors=[]、status=passed。要求行情/参考交易日20261009，资料日20261008。
这是模型执行证据；此前原生SimNow验收仍按其各自记录判断。

01模型恢复、01/04同时运行、第三方多账户真实成交仍待验收。04模型运行和
同文件恢复已通过，见[最新模型验收记录](../../doc/HANDOFF/live/HANDOFF_MODEL_REPLAY_2026-10-11.md)
和[结构化证据](../../doc/evidence/live/MODEL_REPLAY_20261011.json)。运行代码保持
PF2026101101 REV2，本轮仅更新文档，完整清单为PF2026101101_REV3_SHA256.txt。

## 2026-10-08及后续历史接入记录

2026-10-08。复用 `strategy.py` 的主力 EMA 公式，固定本次主力，使用本次 TD 交易日的参考资料。入口为 `run_live.py`，开发时在 pro 根目录使用模块命令，不注入 `sys.path`，不依赖 examples。

## 当前证据

| 验证层 | 状态 | 证据与范围 |
| --- | --- | --- |
| 原增量及相关回归 | 历史无网络通过 | signal 6、routing 10、CTP 7 及 7 个回归入口；新版本仍须验证变更涉及部分 |
| 新 TD 只读入口烟测 | 2026-10-08 通过 | 用户运行 `python -u -m tests.run_ctp_td_readonly`；假 API，不是本次柜台查询 |
| 本轮深入离线验收 | 2026-10-08 全部9组通过 | 前8组通过，entry时钟修正版18/18通过（0.421秒），进程退出码0；分两轮取得证据 |
| 数据库装配新增离线验收 | 2026-10-08 通过 | 数据源23/23、公共回归32/32、角色研究及角色信号、entry21/21全部有远程通过证据；假DB／MD／TD，不能代替真实数据库或柜台验收 |
| 原始来源日口径修正版 | 2026-10-08 通过 | PF2026100804：references29/29、公共回归32/32、entry22/22通过；报告main-ema-acceptance-1791444863148068678，部署摘要一致 |
| 当前DolphinDB只读 | 2026-10-08 通过 | 指定TD日2026-10-08、角色／因子来源日2026-09-30；rb2701.SHFE、累计因子1.181055、tick1、multiplier10；报告tests/results/reference-data-20261008.json |
| 当前 SimNow 柜台只读 | 2026-10-08 通过 | 第二套TD 182.254.243.31:40001登录、结算确认和三类查询完成；净仓合约0、CNY资金快照、活动订单0；本次探针未打印TD交易日，不能据此认定与MD交易日一致；此前Recording预检曾返回总仓空 |
| 第二套原始 MD | 2026-10-08 通过 | 40011连接、登录、订阅rb2701成功，10秒采样13条Tick；TradingDay=20260930、ActionDay=20260929，行情时间16:17:09.500至16:17:19.500 |
| 第二套标准行情转换 | 2026-10-08 通过 | stage 5输出rb2701.SHFE的QuoteTick与TradeTick；报价／数量精度0，成交价3112、增量7；仅证明转换及事件接收，不是当前实时策略或柜台成交验收 |
| 第二套完整装配修正版 | 2026-10-08 通过 | PF2026100805：controlled及health首轮通过；夹具修正后entry34/34通过（1.385秒），报告main-ema-acceptance-1791449976479272698；加载路径／摘要一致 |
| 第二套策略 Recording（迁移前） | 2026-10-08 通过 | TD／MD=20260930，资料来源／因子日2026-09-29，rb2701.SHFE、因子1.181055；832条Tick、9根Bar、LONG 1手目标；status=passed、报单0、停机总仓空、活动订单0、清理无错误 |
| 单实盘入口公共迁移 | 2026-10-08 离线通过 | PF2026100806：修正版references30/30、routing10/10、ctp7/7、entry35/35，共82项通过；报告main-ema-acceptance-1791458768127640128，打印的公共模块与入口路径／摘要一致；全程假API，不是实际模拟柜台成交 |
| 回测／实盘统一组装 | 2026-10-08 离线五组通过 | PF2026100807：assembly10/10、原runtime委托、routing10/10、ctp7/7、entry38/38均退出0；报告main-ema-acceptance-1791463560272015358，公共新模块／两入口／相关测试打印路径与摘要一致；完整历史样本及重构后真实网络验收仍待完成 |
| 重构后第二套 Recording | 2026-10-08 空仓准入拒绝 | simnow-1791463768539219912：实际TD=20260930，rb2701.SHFE多仓0／空仓1手、活动订单0；本次报单0、Bar0、MD未启动、资料未加载；停机查询完成且cleanup_errors为空。不是完整Recording通过，既有仓位来源未确认 |
| 当前主力实时 Recording | 第一套链路通过，资料新鲜度待补 | MD／TD=20261009、30011／30001，source资料日仍为2026-09-30，rb2701.SHFE；9根Bar产生LONG 1手目标，status=passed、策略报单0、最终总仓空、活动订单0、清理错误空；报告simnow-1791469483764481796。2026-10-08已开市，不能据此放行策略报单 |
| 当前主力 SimNow 策略交易 | 暂缓，待Oct8资料 | 独立工具已实际完成一手平仓；主力EMA的角色／因子需核对2026-10-08来源日，再进行第一套受控订单验证 |

## 同步组合

保留项目 `bomber/__init__.py`、完整 `bomber/framework`、`tests/__init__.py` 和 scripts 包。本轮新增或修改：

- `bomber/framework/trader/execution/ctp/limit_planner.py` 与同目录 `__init__.py`。
- `bomber/framework/dataprep/live_role.py`、`bomber/framework/trader/live_roles.py`、`demos/01_main_ema/run_live.py`；策略保持当前版本。两个原demo辅助文件已删除，远程也须删除，具体见下节PF2026100806。
- `tests/run_main_ema_live.py`、`run_main_ema_acceptance.py`、`run_main_ema_simnow.py`、`run_p4_ctp_autosave.py`、`run_p4_ctp_recovery.py`。集中入口还使用原有 TD 传输、Driver、报单字段、受控客户端和行情闸门测试。
- 如仍运行旧 examples 入口，同步其 `ema_ctp_simnow.py`，该文件转用公共限价规划器；新入口与集中验收不要求部署它。

远程环境继续复用已安装的 Bomber 内核以及已构建的 market/basic 和 Linux CTP MD／TD 扩展。本机仅静态读写，不运行 Python 或编译。

### 单实盘入口迁移（PF2026100806）

2026-10-08按用户要求，demo的实盘组装集中在run_live.py，删除live_references.py、live_runner.py。参考适配迁入dataprep/live_role.py，固定角色路由和会话守卫迁入trader/live_roles.py；参考源／角色／目标键／处理时间／会话检查通过参数注入，不依赖EMA或demo。strategy.py、run_backtest.py未修改，CLI和现有SimNow执行语义保留。公共迁移登记于[并行协调记录](../../doc/HANDOFF/coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100806-实盘仅保留一个组装入口)。

在已部署PF2026100805来源日修正版基础上，整组同步以下7个源码文件：

- bomber/framework/dataprep/live_role.py
- bomber/framework/trader/live_roles.py
- demos/01_main_ema/run_live.py
- tests/test_reference_sources.py
- tests/run_main_ema_simnow.py
- tests/run_main_ema_live.py
- tests/run_main_ema_acceptance.py

再在远程pro根目录删除旧文件并复验。本次新增文件和数据库次主力适配一致性，以及没有EMA配置的另一策略绑定次主力与保留目标推进用例。预期references30、routing10、ctp7、entry35项；测试使用假API，不连接柜台。本机仅静态核对，尚无迁移后运行结果。

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
rm -f demos/01_main_ema/live_references.py demos/01_main_ema/live_runner.py
python -u -m tests.run_main_ema_acceptance \
  --only references --only routing --only ctp --only entry
```

集中入口打印新公共模块加载路径及SHA-256，缺少接口／新增用例时在测试前停止。以下PF2026100805中的5文件摘要、entry34项和Recording证据均保留为迁移前记录，不能用来宣称PF2026100806已通过。四组复验通过后沿用第二套受控模拟订单命令；CLI无变化，受控模拟柜台成交仍待取得。

首轮远程证据：报告 `tests/results/main-ema-acceptance-1791458546029341474`。日志打印的新公共适配／Runner、01入口、参考测试及entry测试路径与静态摘要一致。references30/30通过（1.785秒，模块2.574秒），routing10/10通过（0.007秒，模块1.220秒）；ctp6/7通过（0.014秒，模块1.236秒），test_live_main_change_disarms_ctp未抛预期闭闸异常。entry仅被部署检查导入，没有执行35项测试。

根因：迁移前Runner读取strategy.data_hub，旧测试通过替换该属性模拟换主力；公共Runner现在消费装配时明确注入的参考服务，旧测试仅替换策略属性，导致两个读取方用不同服务。本次仅修改tests/run_main_ema_simnow.py：CTP夹具支持store参数，测试在装配时给策略和Runner注入同一份as-of映射，在时间4触发换主力；保留关闭授权、恢复锁、旧仓1手、报单仍1笔的断言，补充后续旧合约Bar仍闭闸且策略停在3根Bar。公共框架、策略和实盘入口没有修改。

修正版tests/run_main_ema_simnow.py静态SHA-256为 `156437aa8d2015c78e581c3067c673f37a8dd92878da8ad8a3ff4be533a19237`。本机未运行测试，仅同步这一个源码文件后复验失败的ctp及尚未运行的entry，references／routing无需重复：

```bash
python -u -m tests.run_main_ema_acceptance --only ctp --only entry
```

预期ctp7项、entry35项均通过后，再进行第二套受控模拟报单。

最新修正版远程复验：用户再次执行四组，报告 `tests/results/main-ema-acceptance-1791458768127640128`。references30/30通过（1.768秒，模块2.523秒），routing10/10通过（0.006秒，模块1.189秒），ctp7/7通过（0.013秒，模块1.213秒），entry35/35通过（1.405秒，模块2.632秒），共82项、所有模块退出0。CTP主力变更闭闸用例、文件／数据库次主力适配、无EMA配置策略的次主力绑定及完整第二套假API入口均已通过。日志打印的新公共模块／入口／参考测试／entry测试摘要与本机一致。本次未改公共源码或运行入口；相关迁移离线验收完成，源码未改无需再测。

下一步执行下方第二套受控模拟订单命令，沿用本次已验证的前置与认证配置、DolphinDB数据源；真实成交、停机活动订单及柜台总仓对账仍独立验收。此轮entry输出的mode=simnow、orders_submitted=1和final_gross=1手来自假TD，不能登记为真实柜台报单或成交。迁移前真实Recording保留为其原版本证据，本次受控入口会重新核对实际MD／TD、参考资料、总仓与活动订单。

### 统一组装结构（PF2026100807）

本次以当前demos入口为基线，归档`__bak/examples`不修改、不恢复旧接口。两个入口共用`StrategyBindings`、`assemble_strategy`和执行构建组件；回测继续使用原UnifiedHistoricalRuntime，实盘改用ManagedLiveRuntime、CTP控制器与报告。strategy.py未改，CLI参数、因子口径、固定LIVE路由、显式模拟订单授权和停机行为保持。

同步以下10个文件到远程pro根目录的同名路径；其中5个公共文件及1个测试文件为新增，不能只同步run_live.py。此前PF2026100806的参考适配、角色Runner及其依赖应已部署，原tests/run_runtime.py也须存在。01仍只有strategy.py、run_backtest.py、run_live.py三个Python文件，不恢复两个旧辅助文件。

| 同步文件 | 职责 |
| --- | --- |
| bomber/framework/trader/assembly.py | 公共绑定声明及策略注册 |
| bomber/framework/trader/execution/builders.py | 模拟／CTP执行组件和持久化钩子 |
| bomber/framework/trader/runtime/managed.py | 通用在线运行、失败清理及控制器协议 |
| bomber/framework/trader/runtime/ctp.py | SimNow预检、授权、对账、停机及账户锁 |
| bomber/framework/trader/runtime/reports.py | 在线摘要与事件报告 |
| demos/01_main_ema/run_backtest.py | 当前回测组装；支持模块与直接脚本 |
| demos/01_main_ema/run_live.py | 当前实盘组装；使用原模块命令 |
| tests/test_strategy_assembly.py | 公共运行失败清理、历史事件顺序及原策略回测组装 |
| tests/run_main_ema_live.py | 38项完整在线假API及守卫测试 |
| tests/run_main_ema_acceptance.py | 集中入口、新模块部署检查及摘要清单 |

在Linux uv-nautilus环境、远程pro根目录执行：

```bash
python -u -m tests.run_main_ema_acceptance \
  --only assembly --only runtime \
  --only routing --only ctp --only entry
```

预期assembly10项、runtime原委托探针、routing10项、ctp7项、entry38项均退出0。历史顺序测试使用内存Backend，检查首根目标在下一根成交、仓位先更新及在途归零；另用当前run_backtest.assemble运行原MainEmaStrategy、as-of资料和动态主力路由。完整entry仍覆盖原生假TD、持久化、文件／数据库、realtime／replay、反手、断线和陈旧数据门控，新增构造不连接、资料失败关闭DB／TD以及公共／旧账户锁互斥。构造、输入、预检结束、组装、启动、运行、验收及资源释放失败均有对应公共Runtime用例。

账户互斥统一使用bomber-ctp命名空间；本入口同时持有旧bomber-main-ema锁，防止迁移前进程与新进程重叠。其他复用默认公共CTP生命周期的策略共享公共锁；锁冲突须在TD启动前拒绝。该本机进程锁仍不能约束外部交易终端。

这组离线验证通过后，以原来已通过的实际回测样本、相同日期和参数对比订单／成交／仓位报表；输入准备、日期裁剪与回测报表逻辑未改，但内存测试不能代替完整Nautilus撮合样本。在线再运行本页第二套Recording命令，然后继续原受控模拟订单命令；MD／TD交易日使用柜台本次实际返回值。40011／40001及显式replay规则不变，账户仍须空仓且无活动订单。

失败时集中入口停止，只同步修正涉及文件并按输出`--only`命令复测；无变更模块不重复。本机只做源码、静态差异和摘要核对，没有执行Python／测试／回测／编译，没有SSH。公共修改已在当前对话前后通知，登记[PF2026100807](../../doc/HANDOFF/coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100807-回测与实盘采用一致的组装结构)；跨任务待同步。

回退时整组恢复PF2026100806的两个入口及相关测试，新增公共文件可留存但不得混用新入口与旧测试。交易状态文件、实际柜台仓位及历史验收结果不覆盖。

2026-10-08最新远程结果：报告`tests/results/main-ema-acceptance-1791463560272015358`，assembly10/10（0.032秒，模块1.133秒）、runtime委托探针（模块1.159秒）、routing10/10（0.006秒，模块1.156秒）、ctp7/7（0.013秒，模块1.192秒）、entry38/38（1.421秒，模块2.585秒）全部退出0，共65项单元测试及原Runtime委托验证通过。五个新公共模块、两个入口和两个相关测试的打印摘要与静态记录一致。日志中的failed摘要属于预期失败用例，均有ok断言；不是实际运行失败。

本轮没有修改运行代码或测试，已通过五组不重复。公共组装离线验收完成；当前MainEma回测组装采用内存Backend，有下一事件成交与共享仓位证据，完整Nautilus样本对比仍单独待完成。实盘下一步用新run_live复验第二套Recording：该入口会重新执行TD只读预检和停机权威查询，因此沿用已确认的前置及MD交易日即可。20260930是此前真实MD日，不能使用假API测试的20260921／20260922作为实盘参数；若本次提示MD日变化，先原始stage4探针核对并更新显式日期。Recording通过后继续本页受控模拟订单命令，真实柜台成交仍待验收。其他工作对话没有送达记录，跨任务待同步。

随后真实Recording预检因账户已有仓位被拒绝：报告`demos/01_main_ema/results/simnow-1791463768539219912`。TD登录／结算及查询已完成，权威逐合约总仓为rb2701.SHFE=(0,1)，元组顺序为多仓／空仓，实际TD日20260930，活动订单0；Bar0、报单0、原始MD日未知、参考manifest为空，退出原因是既有总仓而非行情或数据库故障。final_gross仍为该1手空仓，cleanup_errors为空；本次入口没有创建此仓，日志不能确定仓位来源。

当前recording与simnow都沿用独占空仓启动政策，不自动接管既有仓位。保持源码和65项已通过离线证据；由用户在同一第二套模拟环境核对并处理既有持仓（该空仓关闭方向为买入平仓，今／昨仓以柜台资料为准），或使用符合前置账户可用条件的空仓账户，再执行原Recording命令。不要仅以净仓为0或活动订单0判断账户为空，原入口仍核验多空总仓。lshw／dmidecode缺失提示不是本次异常来源。完整Recording与策略柜台成交继续待验收。

再次运行simnow-1791464101801339558，TD20260930仍为相同空仓1手、活动订单0、Bar0、报单0、清理无异常。为提供远程可执行的独立处理方式，PF2026100808新增`scripts.integration.ctp.close_existing`，默认只读预检、显式授权才发送恰好一手模拟平仓单；不改变EMA空仓守卫。远程原TD模块通过；新12项首轮因3个测试漏调用`activate()`而未全过，修正后复测报告`main-ema-acceptance-1791467256985484635`为12／12通过、模块退出0。随后用户提供原生方法smoke通过及真实柜台默认只读预检：TD交易日20260930，rb2701.SHFE多0／空1手，PositionDate=1、TodayPosition=1、YdPosition=0、HedgeFlag=1，工具输出BUY CLOSE_TODAY、`prechecked`、报单0、活动订单0、无清理错误。真实回报证实当前扩展传出今昨字段；尚未收到构建日志／C++源码哈希。第一次受控提交限价3071，当次MD卖一3118；柜台ACCEPTED后约8秒CANCELED，成交0。第二次限价3072，反馈未附新MD报价，仍获ACCEPTED后约8秒CANCELED，成交0。两次各报单1，最终仍空仓1手、活动订单0、无清理错误；工具`failed`为未成交的预期退出。下一步先停止提交，重新获取当前MD卖一价并与TD交易日核对；仅权威总仓归零后重跑Recording。完整操作见[独立一手仓位处理](../../scripts/integration/ctp/README.md#独立处理既有一手模拟仓位pf2026100808)。本轮代码未在本机运行，实际清仓及Recording仍待完成；旧65项通过保留。

随后第三次独立平仓使用新MD采样（TradingDay=20260930、rb2701卖一3115）和BUY CLOSE_TODAY一手限价3115，柜台回报ACCEPTED、FILLED，成交价3115、成交1手。工具`status=passed`、本次报单1、最终权威总仓`{}`、活动订单0、清理错误空，既有空仓已关闭。当前下一步是同一第二套环境的EMA Recording：核对会话日、DolphinDB参考资料、完整分钟Bar及停机结果。该独立平仓订单不计入EMA策略订单验收；Recording后仍须进行主力EMA本身的受控报单验证。本次无公共源码修改，跨任务状态仍待同步。

随后一次Recording启动把MD／TD环境变量改为`30011/30001`，仍使用`--simnow-environment replay --replay-md-trading-day 20260930`，在公共生命周期构造阶段收到`replay模式须使用同组SimNow MD40011／TD40001前置`。该次未连接柜台、未读取仓位、未启动行情或策略，也未发单；第二套联调应恢复`40011/40001`再运行原命令。若切换另一组前置，应先独立确认真实MD／TD交易日，使用`realtime`并去掉回放日参数，不能沿用第二套的20260930约束。本次仅配置修正，无公共代码修改。

随后用户以第一套`30011/30001`、`realtime`、固定TD交易日20261009、DolphinDB source口径运行600秒Recording，结果目录`simnow-1791469483764481796`。会话就绪、MD／TD实际日均20261009，参考资料角色／因子日2026-09-30、主力rb2701.SHFE、累计因子1.181055；9根完整分钟Bar生成LONG 1手目标，最终`status=passed`、策略报单0、权威总仓`{}`、活动订单0、清理错误空。这是重构后第一套真实Recording链路通过证据；独立平仓工具成交仍不能代替EMA策略成交。后续发现下面的资料日期缺口，先处理该缺口再进入受控报单。

正式报单前发现资料新鲜度缺口：上期所[2026年国庆期间工作安排](https://www.shfe.com.cn/publicnotice/notice/202609/t20260921_833503.html)确认10月8日白天恢复交易、当晚恢复夜盘。第一套实际TD日20261009的前一交易日是2026-10-08，但本次角色和累计因子来源日仍为2026-09-30。现有资料加载器会选择TD日前最新一行，不能从`ready=True`或Recording的`passed`推断上游10月8日已完整入库。未取得10月8日`fut_contract` RB主力及同日`fut_adjustment_factors`累计因子前，暂停主力EMA报单；不补造因子或改用旧主力。

PF2026100809只修改01实盘组装入口：新增`--expected-source-day YYYYMMDD`，`simnow`报单模式必须显式给出，Recording可选；取得资料快照后若来源日不符，在MD／策略启动及授权前失败。策略公式、公共资料接口和回测入口不变。远程同步`run_live.py`与对应入口测试后，先运行`python -u -m tests.run_main_ema_acceptance --only entry`，再只读核对：

首轮远程entry验收39项通过38项；新增旧来源日测试因假DolphinDB环境缺`DDB_HOST`提前报错。仅补齐该测试的假连接环境变量，生产入口无变化。同步`tests/run_main_ema_live.py`（SHA-256 `55c088d56f6da6c0e68e40257df772549ff1732a203cc2b3c557a5278cc55cd0`）后只重跑了`python -u -m tests.run_main_ema_acceptance --only entry`。

修正版远程复测39／39通过。TD=20261009的DolphinDB只读报告仍选`source_day=2026-09-30`、`factor_date=2026-09-30`、主力rb2701。`ready=true`只表示当前选出的旧日期资料内部完整。系统先选TD日前最新的`fut_contract` RB映射，再读取同日因子；此报告尚不能判断10月8日因子是否存在。先只读检查`fut_contract`中10月8日`Code=RB`及`fut_adjustment_factors`中10月8日`contractObject=RB`的实际行，再处理上游缺口。当前EMA策略仍未获得报单放行。

后续DolphinDB界面只读截图显示10月8日两表的RB行都存在：`fut_contract.main=rb2701`、`fut_adjustment_factors.Code=RB2701`及累计因子`1.181055`。因此先前“可能缺行”已不足以解释旧快照；可能是界面查询之后才入库，也可能两端连接不同实例。先在远程相同Python环境重新运行上面的只读命令；若仍返回9月30日，使用同一环境的资料适配器精确查询10月8日两表并核对连接目标。当前不修改日期选择逻辑，不放行策略报单。

远程相同Python环境已精确查到10月8日两表各1行；再次运行只读入口时，`snapshot()`改报“当前角色或因子版本尚未生效或发布”。`source-day-end`因子政策在没有`available_ns`时保守使用北京时间来源日次日00:00，因此2026-10-08夜盘零点前尚不可使用10月8日因子。这是时间闸门的预期行为。北京时间2026-10-09 00:00后重新执行上面的只读命令，核对`source_day`及`factor_date`均为2026-10-08。上期所[交易时间](https://www.shfe.com.cn/services/calenderandholidays/tradinghours/)显示RB夜盘23:00结束，因此零点后只验资料；以`--expected-source-day 20261008`运行Recording应在10月9日09:00后的有效日盘，并核对实际TD／MD交易日。策略报单仍需之后单独验收。若夜盘零点前必须使用该因子，需上游提供真实`available_ns`并单独验证显式发布时间口径，不以数据库读到行的时间代替发布证明。

```bash
python -u -m scripts.integration.reference_data.readonly \
  --connect --trading-day 20261009 --products RB \
  --database dfs://bomber_daily --factor-date-basis source \
  --report tests/results/reference-data-20261009.json
```

输出须显示`source_day=2026-10-08`、`manifest.role_source_day=2026-10-08`、`factor_date=2026-10-08`及实际主力条款；仅补齐并验证上游数据后，用`--expected-source-day 20261008`重跑第一套Recording。结果通过、总仓仍空，再执行第一套受控策略报单。首次建议`--max-session-orders 1`、新的状态文件；停机不自动平仓，最终仓位需要单独核对。

PF2026100810增加实时数据库专用的`--factor-availability observed-on-read`。对同一资料连续读取两次且指纹一致后，从首次稳定读取完成时刻开始可用于当前实盘；相同内容刷新保留首次时刻，新内容重新计时，源表的`available_ns`仍须满足。该口径显式选择，默认`source-day-end`未改；文件及replay入口拒绝。来源交易日仍用`--expected-source-day`独立核对。先在远程同步公共与入口文件，运行：

```bash
python -u -m tests.run_main_ema_acceptance --only references --only entry
python -u -m scripts.integration.reference_data.readonly \
  --connect --trading-day 20261009 --products CU \
  --database dfs://bomber_daily --factor-date-basis source \
  --factor-availability observed-on-read \
  --report tests/results/reference-data-cu-20261009.json
```

CU只读结果需显示`source_day`和`factor_date`均为2026-10-08、主力合约和真实条款有效、manifest的`factor_publication_policy=observed-on-read`及`ready=true`。这些通过后，在上期所铜夜盘有效时段以第一套30011／30001、`realtime`、`--product CU --expected-trading-day 20261009 --expected-source-day 20261008 --factor-availability observed-on-read`运行600秒Recording，核对分钟Bar、零策略订单及停机权威仓位。公开变更及SHA见[并行协调记录](../../doc/HANDOFF/coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100810-在线参考资料夜盘观测时刻)。本轮修正版尚待远程验收。

第一套CU Recording实际报告`simnow-1791473666741843567`：TD／MD20261009、角色及因子日2026-10-08、主力cu2611.SHFE、factor=1.484671、tick=10、multiplier=5，资料观测口径已成功加载。3根Bar后poll健康检查失败，未达慢EMA5根预热；最终failed、订单0、总仓空、活动订单0、清理错误空。不能据此判定Recording目标或策略订单通过。PF2026100811在公共CTP生命周期异常补充实际／期望交易日、行情健康原因和接收年龄，原10秒阈值保留；同步`bomber/framework/trader/runtime/ctp.py`及`tests/run_main_ema_live.py`后运行`--only references --only entry`，通过后重跑CU Recording，以具体异常定位停机原因。本机未运行Python，远程新版本待验收。

## 1 集中离线验收

在已激活 `uv-nautilus` 的远程终端执行：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
python -u -m tests.run_main_ema_acceptance
```

模块依次为 TD 分片查询及原生回调、报单字段、Driver 生命周期、自动保存、订单关联恢复、受控交易授权、行情健康闸门、当前 EMA 的 CTP 增量、完整在线装配。每组独立进程，默认上限 180 秒，首个失败即停止。该命令不连接柜台，也不读取真实凭据。

结果保存在 `tests/results/main-ema-acceptance-<时间戳>/`，含每模块日志、退出码、耗时和关键源码 SHA-256。新在线装配测试覆盖参考资料发布时间／缺失、因子合约优先与真实条款、授权、双向总仓、未知活动订单、缺查询末片、分钟聚合、部分启动分钟、限价取整、回放拒绝、当前策略反手、重复成交、断线条件、落盘失败、条款变化以及完整入口启动和停机。

失败后只复测对应模块，例如：

```bash
python -u -m tests.run_main_ema_acceptance --only entry
```

`--only` 的通过只代表选中的模块；此前未运行的模块仍须补跑。已通过且源码没有变化的模块无需反复运行。

2026-10-08 首轮集中验收：前8组已通过，CTP增量7/7通过（0.013秒）；entry 的17项中14项通过、3项报相同的请求过期错误，耗时0.407秒。原因是客户端默认 `time.time_ns` 在模块导入时绑定，测试后来替换时钟未影响该默认函数。在线装配现显式传入与Bar校验一致的时钟回调，保留120秒限制；新增“当前请求可规划、过期请求仍闭闸”的用例，entry变为18项。仅同步 `demos/01_main_ema/run_live.py` 与 `tests/run_main_ema_live.py` 后执行上面的 `--only entry` 命令。修正版未在本机运行，前8组通过证据保留，本次柜台验证仍未开始。

随后用户远程复测 `--only entry`，18/18通过（0.421秒），模块退出码0、耗时1.498秒，报告目录为远程 `tests/results/main-ema-acceptance-1791419519660374237`。无需再重复无变化的离线模块，继续本次柜台与行情验收。

## 2 本次柜台只读

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

三类查询都完整返回才通过。使用既有 `.env`／环境变量中的 SimNow MD／TD 与认证配置，不在命令里传递密码。净仓为零不能证明没有双向仓；策略入口会再做总仓查询，并拒绝账户已有仓位或活动订单。

## 3 当前策略实时 Recording

### 本次行情与会话诊断（2026-10-08）

数据库source模式Recording已实际运行，记录目录为远程 `demos/01_main_ema/results/simnow-1791445724870747476`。参考资料ready，主力rb2701.SHFE、累计因子1.181055、tick1、multiplier10；失败发生于30秒MD／TD会话就绪等待，策略未收到可用Bar，未提交订单。停机总仓为空、活动订单0、cleanup_errors为空。不能将该结果登记为Recording通过。

随后用户分层诊断：MD 30011在30秒内未建立前置连接；切换至用户提供的第二套40011、production_mode=true后，原始MD stage 4及标准Feed stage 5均通过。原始行情交易日20260930，ActionDay为20260929，与实际运行日期不同；本次标准事件保留原行情时间戳，不将旧行情改写为当前时间。第二套TD 40001的登录、结算确认、净仓／资金／活动订单查询亦通过；该探针输出没有TD交易日，尚不能认定MD／TD日一致。净仓为零仍不能替代策略入口的总仓检查。

这些模块已有本次网络通过证据，源码未改无需重复运行。随后用户明确要求继续第二套完整链路，按下一节PF2026100805验证；第一套仍使用默认realtime入口。环境变量优先于根目录.env，切换环境时必须一并核对MD、TD与production_mode等配置。默认入口的完整启动分钟和Bar墙钟检查不接受旧日期行情；第二套通过显式选择接收时间Bar进行工程联调。lshw／dmidecode缺失提示未阻止本次TD只读完成，不能仅凭提示判定本轮失败原因。

### 第二套完整链路（PF2026100805）

`--simnow-environment replay`只允许本次指定的第二套MD `tcp://182.254.243.31:40011`／TD `tcp://182.254.243.31:40001`、production_mode=true及BrokerID9999。必须提供`--replay-md-trading-day`，与真实原始Tick的TradingDay核对；TD单独固定为本次登录返回日，任一日期切换、健康异常、行情超过10秒不新鲜或TD断线都会停止。不能把该参数设为自然日期来掩盖实际行情日期变化。

时间声明：原始上游标准Tick不修改，新增公共ReceiveTimeTradeTickBarFeed仅构造聚合输入副本，以实际接收时间ts_init形成Bar。Bar及EMA目标属于当前接收期间；参考角色、因子和条款按实际TD日／当前可用时间加载，不用于解释原历史行情的收益或逐笔回测。接收时间缺失、未来、过期或倒退拒绝；不补无成交分钟、不用尚未结束的分钟。会话真正就绪并开放Bar后重新确定首个完整分钟，排除登录等待跨分钟导致的部分Bar。订单请求120秒墙钟检查、价格／数量／名义金额风控、总仓／活动订单查询、固定路由及写前落盘继续执行。

以下5文件及摘要是PF2026100805迁移前版本的部署记录。当前部署还须采用上节PF2026100806的7文件清单及旧文件删除步骤，不能仅按此历史清单同步：

- `bomber/framework/market/stream/aggregation.py`
- `bomber/framework/market/stream/__init__.py`
- `demos/01_main_ema/run_live.py`
- `tests/run_main_ema_live.py`
- `tests/run_main_ema_acceptance.py`

PF2026100805历史静态SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| market/stream/aggregation.py | 051cb94bd8aebe1924906729dc6250d37eca69af15fbe50b3427e3c7196bb730 |
| market/stream/__init__.py | ed08bb118527214a872ad34a11867a471bf90e3639bcf71a9ab981fcf9013dee |
| 01/run_live.py | c2c4e1d8b4ff0cda22b905a0c3229fab58fccc920bdf1acdd8acc074e644fd60 |
| tests/run_main_ema_live.py（夹具修正版） | bf0c4d192ebb2701b312f3aa76335b71cd251f14319d9ef3f540dc05b1075888 |
| tests/run_main_ema_acceptance.py | 288bdef9edec2fcce9cdee0b2b855c5ac2ef7bc2bb3c17d985c1defa4d2a1d21 |

本机仅静态审阅，没有运行Python、测试或编译。远程先运行相关离线验收，entry预期34项；原实时旧Bar拒绝测试保留，新增覆盖接收时间与原始时间、完整分钟、过期／倒退、日期变更、第二套数据库Recording、假TD反手／重复成交／检查点，以及第二套完整模拟成交入口。以下均使用假API，不连接SimNow：

```bash
python -u -m tests.run_main_ema_acceptance \
  --only entry --only controlled --only health
```

通过后沿用本次已确认的第二套认证配置，远程同一终端执行：

```bash
export CTP_MD_ADDRESS=tcp://182.254.243.31:40011
export CTP_TD_ADDRESS=tcp://182.254.243.31:40001
export CTP_PRODUCTION_MODE=true

python -u -m demos.01_main_ema.run_live \
  --connect --mode recording --product RB --seconds 600 \
  --simnow-environment replay --replay-md-trading-day 20260930 \
  --reference-source dolphindb --reference-database dfs://bomber_daily \
  --factor-date-basis source
```

20260930来自本次原始行情证据；若后续原始日期变化，先核对新的MD／TD会话及参考资料，再显式更新参数。Recording要看到会话就绪、完整分钟EMA进度、至少一个目标及status=passed，不能仅看连接成功。summary记录环境、实际与要求的MD／TD日、Bar及参考资料时间口径、首末原始事件与接收时间和Tick数。

Recording通过后，空总仓、无活动订单，继续第二套柜台订单：

```bash
python -u -m demos.01_main_ema.run_live \
  --connect --mode simnow --product RB --seconds 600 \
  --simnow-environment replay --replay-md-trading-day 20260930 \
  --expected-trading-day 20260930 --expected-source-day 20260929 \
  --reference-source dolphindb --reference-database dfs://bomber_daily \
  --factor-date-basis source \
  --enable-orders --confirm-simnow \
  --quantity 1 --max-notional 50000 --max-session-orders 4 \
  --state-file "demos/01_main_ema/results/simnow-env2-$(date +%Y%m%d-%H%M%S).json"
```

该命令会向第二套模拟柜台真实报单，时间戳生成本次新检查点，已有文件仍拒绝覆盖。MD及TD的20260930来自上次实际会话；日期变化时先核对新会话，再显式更新对应参数，不用自然日猜测。通过条件仍包括至少一笔真实成交、停机查询完整、无活动订单、柜台总仓与本地一致；停机撤单但不自动平仓，不能带仓重新启动本入口。报单受理／开平／反手／撤单等按实际发生结果分别记录。最新状态：PF2026100806迁移离线82项通过，真实Recording为迁移前通过证据，策略柜台订单／成交仍未验收。

2026-10-08首轮远程证据：报告 `tests/results/main-ema-acceptance-1791449039224869214`，本次各模块部署路径及初版摘要一致。controlled退出0、耗时1.161秒，health退出0、耗时1.183秒；entry34项耗时1.044秒，32通过、两项完整第二套入口ERROR，模块退出1、耗时2.274秒。根因是tests/run_p4_ctp_td_transport.py的基础假API在registerFront硬编码只接受fake地址，新增第二套配置命中该断言；随后假API缺少release及未连接清理错误属于连锁错误，不能解释为真实柜台失败。

本次仅在tests/run_main_ema_live.py的FlatTdApi覆盖registerFront，接受既有fake和本次第二套TD地址，仍全程假API、不建立网络；AutoFillTdApi继承该修正，公共FakeTdApi及真实入口／前置校验未修改。测试仍34项，本机仅静态核对，修正版待远程执行：

```bash
python -u -m tests.run_main_ema_acceptance --only entry
```

controlled和health已通过且源码未改，无需重跑。用户随后真实第二套Recording已显示会话就绪，实际TD及要求MD均20260930、source_day=factor_day=2026-09-29、main=rb2701.SHFE、因子1.181055、tick1、multiplier10。已使用8根分钟Bar，第5根后revision1生成LONG、逻辑rb_main目标1手，signal_ts=1791449399999999999。尚未提供600秒结束的summary及停机对账，不把阶段进度登记为整次Recording通过；没有真实策略模拟订单证据。当前Recording无需因测试夹具修正而重启，待最终停机结果和entry复验通过后继续订单阶段。

2026-10-08夹具修正版已远程复验：报告 `tests/results/main-ema-acceptance-1791449976479272698`，entry34/34通过（1.385秒），模块退出0、耗时2.599秒；聚合组件、入口和修正版测试的加载路径／SHA-256一致。结合上轮controlled／health通过，相关离线验收完成，无需重复。

真实第二套Recording最终通过，报告目录为远程 `demos/01_main_ema/results/simnow-1791449059995543664`：status=passed，832条Tick、9根有效分钟Bar、前述LONG 1手目标，orders_submitted=0，final_gross为空、final_active_orders=0、cleanup_errors为空；实际MD／TD日均20260930，factor_date=role_source_day=2026-09-29。资料ready=true、failure=null，指纹 `e5713dcdad70cfa34d06572996360c681ee2febad87448b85768560532d1d3a0`，角色1645行、因子1行、条款12行。汇总同时保留首末原始事件时间1790671953500000000／1790672558500000000及接收时间1791449066832857897／1791449671576266931。第二套行情、实际数据库、分钟聚合、原EMA目标和停机只读对账已取得整次通过证据；尚未发送本次策略柜台订单。下一步直接执行本节受控模拟订单命令，不要求重新跑Recording或已有离线组。

### 数据库装配与运行命令

2026-10-08新增DolphinDB参考资料装配时，曾同步sources、公共live_references、metadata、factors、references、01两个适配／入口文件及测试；这是公共迁移前记录。当前01适配器已迁到公共live_role.py，按PF2026100806部署。原参考数据源23项、公共回归32项、角色研究／信号两组及入口21项全部取得远程无网络通过证据；报告为 `tests/results/main-ema-acceptance-1791443131209031509`，其中entry0.830秒、模块耗时2.112秒。后续变更按相关组复验：

随后用户真实DB只读指出原始因子date是此前交易日来源日期，PF2026100804已将数据库默认改为source；现references29项、entry22项待远程复验。source使用严格早于TD日的最新角色来源日与同日因子，不要求TD日因子，也不退回更老因子；未接入交易所日历时依赖上游角色日表完整性。文件／回测仍默认已对齐的trading口径。上述旧离线通过结果保留为历史证据。

最新证据：日期修正版references29/29、公共回归32/32、entry22/22已远程通过，报告 `tests/results/main-ema-acceptance-1791444863148068678`；真实DolphinDB RB只读亦通过，结果为 `tests/results/reference-data-20261008.json`。本次TD指定日2026-10-08、角色／因子日2026-09-30、main=rb2701.SHFE、累计因子1.181055、tick1、multiplier10。无需再重复未改动的离线测试或补造TD日因子，继续下面数据库Recording命令。实际TD返回日期由入口核验，不固定为只读时指定日。

```bash
python -u -m tests.run_main_ema_acceptance \
  --only references --only reference-regression --only entry
```

前述旧18项通过证据保留，不能代替新增用例。工厂、表级映射及数据库只读核验见 [参考数据库核验](../../scripts/integration/reference_data/README.md)。只读通过后可以用数据库代替参考文件：

```bash
python -u -m demos.01_main_ema.run_live \
  --connect --mode recording --product RB --seconds 600 \
  --reference-source dolphindb --reference-database dfs://bomber_daily
```

复用 DDB_HOST／DDB_PORT／DDB_USERNAME／DDB_PASSWORD。默认每5秒按需刷新，可用 --reference-refresh-seconds 设置0至60秒，--reference-timeout 设置1至120秒。0表示每次检查都查询，仅用于诊断。数据库默认source，因子必须匹配此前角色来源日；缺少对应日不退回更老因子。查询后再次检查会话和Bar时效；停机关闭参考数据库会话，结果保存TD日、因子日期及资料指纹。策略公式、行情通道和报单授权未改变。

以下命令默认仍使用文件：

```bash
python -u -m demos.01_main_ema.run_live \
  --connect --mode recording --product RB --seconds 600
```

角色目录沿用 `FUT_ROLE_DATA_DIR` 或 `CTP_DATA_DIR/role` 等公共解析规则；默认角色表、`fut_basic.feather` 与 `fut_adjustment_factors.feather` 必须存在。也可以明确传入文件：

```bash
python -u -m demos.01_main_ema.run_live \
  --connect --mode recording --product RB --seconds 600 \
  --contract-struct /实际路径/fut_contract.feather \
  --fut-basic /实际路径/fut_basic.feather \
  --factors /实际路径/fut_adjustment_factors.feather
```

本次TD返回交易日，不能用自然日期替代夜盘交易日；可用 `--expected-trading-day YYYYMMDD` 额外核对。角色来源日必须早于该交易日；source因子精确匹配该来源日，trading因子精确匹配TD日。合约冲突保持同角色因子表合约优先，缺因子不补1。数据库默认source/source-day-end，文件默认trading/aligned；已有available_ns优先门控，要求所有记录提供实际发布时间时用 `--factor-availability explicit`。

启动打印最终主力、来源日、累计因子、价格步长和乘数。文件变化会重新读取；主力／条款变化或资料读取失败停止。参考资料不是自动行情排名系统，必须由上游持续提供有效文件。

### 2026-10-08 当前资料阻塞

历史文件模式证据：真实TD只读已通过，Recording随后在默认trading口径下报 `缺少本次交易日累计因子: (datetime.date(2026, 10, 8), 'RB', 'main')`。以下补齐规则仅用于已对齐适用日的文件输入；本次原始DB日表使用source，不要求补造10月8日因子，详见PF2026100804。

补充本次实际读取的 `fut_adjustment_factors.feather`，或通过 `--factors` 明确指定已更新的文件。沿用旧格式时，需要一条 `trade_date=2026-10-08`、`code=RB`、`symbol=当天真实主力合约`、`pcr_cumfactor=真实累计复权因子` 的记录。使用通用格式时字段为 `cumulative_factor` 且必须明确 `role=main`；若已有 `available_ns` 或采用explicit模式，发布时间也须有效且不晚于决策时刻。

因子值沿用回测及上游累计口径，不拿单日调整因子替代、不默认补1。`symbol` 同时决定行情订阅与交易合约，必须核对其为本次主力，并在 `fut_basic.feather` 有匹配且有效的条款。已有角色表能找到此前来源日记录，但合约条款检查发生在因子加载之后，本次尚未走到该步骤。补齐后重新执行相同Recording命令，取得真实行情和当前策略目标后再进入订单验收。

行情按真实成交 Tick 聚合已收盘一分钟 Bar；部分启动分钟不使用，无成交分钟不补。默认快慢周期 3／5、1 手，冷启动预热，不加载历史 EMA 状态。Recording 必须实际生成当前策略目标且停机查询完成才能退出成功。午休、停盘或行情静默超过 10 秒会停止，验收应在持续行情时段运行。

## 4 受控 SimNow 订单

最新CU Recording：第一套实时会话TD／MD均20261009，来源日及因子日2026-10-08，cu2611.SHFE、factor=1.484671、tick=10、multiplier=5。9根完整Bar先产生LONG 1手、再产生SHORT 1手目标；600秒运行passed，订单0、最终权威总仓空、活动订单0、清理无错误。夜盘观测资料及当前EMA Recording链已通过；此前3根Bar失败的具体原因仍不能由本次成功推断。PF10 references及PF11 entry最新离线结果尚未收到，须先运行或提供`--only references --only entry`的通过结果，已通过则不重复。

上述离线验收通过、确认本次SimNow账户为空仓且无活动订单后，第一套CU首次受控订单入口为：

```bash
python -u -m demos.01_main_ema.run_live \
  --connect --mode simnow --product CU --seconds 600 \
  --simnow-environment realtime --expected-trading-day 20261009 \
  --expected-source-day 20261008 \
  --reference-source dolphindb --reference-database dfs://bomber_daily \
  --factor-date-basis source --factor-availability observed-on-read \
  --enable-orders --confirm-simnow \
  --quantity 1 --max-notional 600000 --max-session-orders 1 \
  --state-file "demos/01_main_ema/results/simnow-cu-$(date +%Y%m%d-%H%M%S)-$$.json"
```

该命令会真实发送模拟柜台订单。CU本次Recording的真实价格约108900元、乘数5，一手名义金额约54.45万元，原RB参数50000不足；本次显式选择600000的单笔／绝对持仓名义金额上限，实际报价超出预算仍拒单。这是合约名义金额，不是保证金。使用新的状态文件名；已有文件拒绝覆盖，不能删旧状态文件来掩盖未完成订单。只有报单授权、BrokerID9999匹配会话、MD／TD交易日一致、预期来源日匹配、持续新鲜行情、权威查询和风险校验全部满足时才开闸。运行期间账户由本策略独占，不与其他策略或手工报单同时使用；账户锁仅防止本入口的重复进程，不能锁住外部交易终端。

最多允许本次配置的订单数量；达到上限关闭新增目标，短暂等待回报后停机。反向目标先平旧方向，确认平仓后在后续新 Bar 推进反向开仓，不修改策略信号版本。仓位与权威总仓定期核对，查询失败或差异即停止。写前保存失败不能触达柜台，回报后继续保存同代检查点。

停机关闭授权、撤销本策略活动订单、等待查询确认，并查询最终总仓与资金。**停机不自动平仓**，最终仓位会打印并写入结果；首版拒绝带仓重启，不自动恢复旧 EMA、目标或在途订单。状态文件用于审计及后续恢复开发，不能将基础恢复测试通过解释为此入口已经支持自动重启。

交易模式至少收到一笔真实成交回报，且停机没有活动订单、查询完整、柜台总仓与本地一致，才将本次基础交易运行记为通过。开仓／平仓／反手／部分成交／撤单各项仍按实际发生的事件分别登记；一次开仓成交不代表全部场景都已在线验收。

运行记录位于 `demos/01_main_ema/results/simnow-<时间戳>/`，包括 `summary.json` 和目标或执行回报 `events.txt`。退出码、summary 和脱敏柜台输出共同作为证据。

公共变化见[并行协调记录 PF2026100801](../../doc/HANDOFF/coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100801-主力-ema-simnow-完整入口及深入验证)。

## 2026-10-09 已有CU空仓接管验收（PF2026100901）

用户最近SimNow启动被默认空仓门控拒绝：TD20261009、cu2611.SHFE空仓1手、活动订单0、Bar0、策略报单0。用户明确选择LONG触发后“买入平仓，成交归零后继续开多仓”。已新增显式接管配置；默认空仓规则保留。不能将此前独立RB平仓工具成交视作CU策略反手通过。

源码及离线用例已补充，本机不执行Python、测试或编译；新版本仍待远程验证：

1. 同步公共运行器、生命周期、新增`runtime/ctp_positions.py`、TD传输、TD绑定源码、demo入口、两份验收测试及文档；重新安装原生扩展以获得PositionCost。
2. pro根目录执行`python -u -m tests.run_main_ema_acceptance --only td --only ctp --only entry`。新增用例检查今／昨空仓先平后开、待成交禁止开多、重复成交去重、同向不发单和不合格接管拒绝；此前参考资料最新版本若未复验，增加`--only references`。
3. Recording加`--adopt-existing-position cu2611.SHFE --expected-position -1`及本次TD／来源日，核对adopted_position、Bar、目标、实际订单0、剩余空仓1手。
4. SimNow沿用相同接管参数，开单确认、新状态文件、数量1、名义额600000、订单上限2。验收须看到BUY CLOSE_TODAY或CLOSE_YESTERDAY完整成交，再看到BUY OPEN完整成交，并由最终权威查询确认多仓1手、空仓0、活动订单0。若期间最新目标变化，按最新目标执行，不能强制继续开多。

离线假API和Recording不能替代实际柜台成交证据。未完成上述远程步骤前，本项状态为“代码完成，待验证”。
