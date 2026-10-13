# 04横截面SimNow与独立模型验收

## 最新：2026-10-11独立模型运行与恢复通过

实际输入为CTP第二套Replay的AU/AG/SC行情，参考资料来自DolphinDB；
执行为reference-price模型，account_reconciliation=off。要求行情/参考日
20261009，资料日20261008，没有TD登录或物理账户成交证据。

| 会话 | 报告 | 帧/调仓/模型成交 | 最终仓位 |
| --- | --- | --- | --- |
| 首次运行 | cross-section-model-1791706100508175757 | 9/6/12 | AU=0、AG=+1、SC=-1 |
| 同文件恢复 | cross-section-model-1791707067358178583 | 9/6/6 | AU=0、AG=-1、SC=+1 |

两次均status=passed、cleanup_errors=[]，目标/策略仓位/执行账本一致，
未归属仓位全为0。恢复使用原文件model-cross-section-20261011-080812.json，
首批新目标版本6→7，第4帧重新预热后才执行；SC从空仓先平空再开多。
原文件已更新为第二行仓位，后续--resume应保留最新状态。

01模型单独运行也已通过；01模型恢复、01/04同时运行、第三方多账户真实
委托与成交仍待验收。详见[完整验收记录](../../doc/HANDOFF/live/HANDOFF_MODEL_REPLAY_2026-10-11.md)
及[结构化证据](../../doc/evidence/live/MODEL_REPLAY_20261011.json)。此前五品种
Recording和原生柜台成交的历史状态不因三品种模型通过而自动签收。

本轮只更新文档/证据，代码保持PF2026101101 REV2，当前完整清单为
PF2026101101_REV3_SHA256.txt；本机公共110项通过，用户选定离线组已报告通过。

## 在线参数速查

当前 `run_live` 的完整在线参数说明、300秒SimNow恢复命令、预算与风控区别，以及预热和停机行为见[README第4节：在线交易使用与参数说明](README.md#4-在线交易使用与参数说明)。本文保留分阶段验收和历次部署证据；日期及原状态路径须按实际会话核对。

早期处理为PF2026100923，见末节：过期成交Bar跳过，未来Bar闭闸，均记录时间诊断。PF0922后用户提供在线Bar时效异常，但没有当时最终summary或离线结果；该阶段27项04用例待Linux验证，实际Recording尚未通过。后续状态以文首最新验收与对应专项为准。

## 自动选主力，无需指定合约（PF2026100921）

最新远端结果：cross-section21项OK（0.200秒、模块exit0/1.261秒），自动选约版本离线通过，已通过组无需重跑。源码/测试仍为PF0921清单版本。所贴/tmp/cross-section-offline-j4_brbgf/...为假柜台测试临时报告，实际五品种Recording仍待最终summary。下方测试命令为留档，当前已有运行继续即可。

用户要求省去合约指定及手工身份核对，现已将expected-instrument改为可选。不传时从本次DolphinDB快照自动选择五品种主力、条款与累计因子，打印所选合约并固定本次路由；报告instrument_selection=automatic_main/fixed_instruments记录实际装配。传入时仍须覆盖全部品种并与参考身份一致。账户/资料/行情内部检查保留，运行中主力/条款/因子变化仍闭闸；这不开放动态换约，不改变排名/手数公式。只改04入口/装配及测试，公共源码不变。已运行进程继续原版本，不需为本项中断它。

本机未执行，新版仅复测cross-section组（现21项），其他已通过公共/01组无需重跑：

```bash
sha256sum -c doc/evidence/checksums/PF2026100921_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only cross-section
```

验证通过后，下一次启动无需五条expected-instrument：

```bash
python -u -m demos.04_cross_section.run_live \
  --connect --products CU,PB,NI,AU,AG \
  --expected-trading-day 20261012 --expected-source-day 20261009 \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.30 \
  --target-notional 100000 --max-quantity 1 --max-notional 100000 \
  --seconds 600
```

这是Recording，不发单。日期对应本次已核验会话，换会话须更新；原显式合约命令保持兼容，下文为既有验收记录。PF0921新功能离线待验，当前Recording仅取得启动信息，尚无最终summary，不宣称实时验证通过。

最新结果：用户提供集中入口末段，attribution18项OK（0.016秒、模块exit0/1.083秒），入口报告“全部选定离线模块通过”。本次选定离线阶段通过，不要求重复；本段未提供实际选定列表、其他组条数/耗时及新报告路径，不补造逐组结果。当前五品种实际柜台只读/参考/Recording/组合成交待验。运行源码与测试不再修改，修正版源码部署仍对应REV2清单；本轮仅更新证据文档。

用户最新选择为**CU、PB、NI、AU、AG**，原RB/HC命令保留为初版示例，当前优先使用下节五品种命令。继续既有SimNow模拟交易授权。Mac只处理源码/文档/摘要，不运行Python、测试、回测、编译或柜台；Linux项目为`/workspace/worker/pj/Chrono/nautilus_trader_demo/pro`，使用`/workspace/worker/env/uv-nautilus/bin/python`。由用户执行并反馈，不猜SSH、不创建并发聊天。

首轮离线报告`cross-section-acceptance-1791555739441173420`：18项中15通过、3失败，组exit1/1.281秒；失败都是测试误假定RB先于HC报单，实际按真实合约排序先HC。完整假柜台生命周期通过，不是实际柜台证据；其余七组尚未运行。已按合约核对价格/数量/部分成交，并新增五品种Recording同步/分组/乘数取整测试，共19项；公共交易源码不变，修正版未运行。当前离线整体未通过、在线未运行。初版摘要保留，修正版部署用`../../doc/evidence/checksums/PF2026100920_REV2_SHA256.txt`。

## 当前五品种测试步骤

同步修正版清单后，在Linux项目根目录执行：

以下为离线命令留档，本次已通过的选定组不需要重跑。下一步直接执行后面的TD只读，先核对当前交易日/总仓/活动订单。

```bash
sha256sum -c doc/evidence/checksums/PF2026100920_REV2_SHA256.txt
python -u -m tests.run_cross_section_acceptance
```

先复测修正后的cross-section组，通过后自动继续此前未运行的七组；不把原3项失败误判为成交归属错误，不改公共排序迁就测试。离线通过后先最新TD只读：

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

按本次输出与日历填写`CS_TD`和`CS_SOURCE`（YYYYMMDD），然后核对五个品种共同来源日/最终主力/累计因子/条款：

```bash
python -u -m scripts.integration.reference_data.readonly \
  --connect --products CU PB NI AU AG --trading-day "$CS_TD" \
  --expected-source-day "$CS_SOURCE" \
  --factor-date-basis source --factor-availability observed-on-read \
  --report "tests/results/cross-section-five-reference-$(date +%s%N).json"
```

从当前输出填写`CS_CU`、`CS_PB`、`CS_NI`、`CS_AU`、`CS_AG`，值均是本次真实合约ID（含`.SHFE`），不默认旧CU或固定月合约。先Recording：

```bash
python -u -m demos.04_cross_section.run_live \
  --connect --products CU,PB,NI,AU,AG \
  --expected-trading-day "$CS_TD" --expected-source-day "$CS_SOURCE" \
  --expected-instrument "CU=$CS_CU" --expected-instrument "PB=$CS_PB" \
  --expected-instrument "NI=$CS_NI" --expected-instrument "AU=$CS_AU" \
  --expected-instrument "AG=$CS_AG" \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.30 \
  --target-notional 100000 --max-quantity 1 --max-notional 100000 \
  --seconds 600
```

这条命令**不发单**。五品种×0.30向下取整，每侧选1个，其余3个目标归零；并非5个品种同时持仓。100000是每侧信号预算，仍按真实价×当前乘数取整；CU/AU等候选可能不足一手而为0，PB/NI等也可能算出多手。Recording展示目标，不执行订单风控；这不表示上述预算与一手上限已经适合报单。待本次参考、真实价和目标输出后再明确受控预算/限额，不能把所有候选强制一手，也不能沿用RB/HC的35000示例。五品种需要全部当前Tick健康且同分钟到齐，不因缺一腿而缩减排名集合。

五品种SimNow报单复用上面的品种/五条预期合约参数，但须先核对实际每侧预算、每合约手数/名义限额与账户资金，使用`five-metals-<新标识>.json`独立状态，再显式加入`--mode simnow --enable-orders --confirm-simnow --state-file ...`。本轮没有修改策略预算公式或扩大默认一手授权边界，未生成未经价格核对的交易预算。其他会话/成交/归属/停机边界沿用下文。

## 同步与离线验证

同步`doc/evidence/checksums/PF2026100920_REV2_SHA256.txt`内全部文件及既有公共依赖。该清单包含修改/新增文件与本次复用的关键依赖；不替代已部署扩展，不需要为本项修改原生C++或编译依赖。项目没有Git信息，以实际文件摘要标识版本。

在Linux项目根目录：

```bash
sha256sum -c doc/evidence/checksums/PF2026100920_REV2_SHA256.txt
python -u -m tests.run_cross_section_acceptance
```

集中运行cross-section、signal、01 entry、routing、ctp、assembly、dispatch、attribution八组；首个失败即停止，记录实际Python、项目/模块路径、摘要、日志、退出码和耗时于`tests/results/cross-section-acceptance-<id>/`。失败修正后只复测对应`--only <组名>`，已经通过且未受新修改影响的组不用重复。真实引擎的01/assembly相关回归保留，不把假TD成交当柜台证据。

新增04用例覆盖CLI授权/新文件限制、多跳价、完整帧/重复Bar、预算不足一手、双腿归属与持久化、双腿先平后开/保持revision、部分成交去重/在途不重报、单腿行情过期、主力/因子/条款变化、拒单阻止后续腿、写前保存失败、全合约账本、单腿成交不能通过组合验收、实际Tick聚合、独占客户端，以及完整预检/重新登录/成交/停机生命周期。全部使用假MD/TD，无网络、不读真实凭据。

## 本次柜台与参考资料只读

先确认01已退出；04使用同一公共账户锁和旧EMA锁，不能改锁名绕过。01状态归属0不替代当前账户查询；有任何总仓或活动订单会拒绝启动，不自动接管/平仓。保留01状态及报告。

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

核对本次TD交易日、全账户空仓、活动订单0、可用CNY。根据最新输出和交易日历填写以下变量，不能复用旧CU合约或硬编码历史TD：

```bash
CS_TD="<本次TD的YYYYMMDD>"
CS_SOURCE="<按日历核对的最近参考来源日YYYYMMDD>"
python -u -m scripts.integration.reference_data.readonly \
  --connect --products RB HC --trading-day "$CS_TD" \
  --expected-source-day "$CS_SOURCE" \
  --factor-date-basis source --factor-availability observed-on-read \
  --report "tests/results/cross-section-reference-$(date +%s%N).json"
```

核对两品种共同来源日、当次最终main、累计因子、SHFE条款、上市/到期、跳价/乘数和manifest ready。实际合约从该次输出填入：

```bash
CS_RB="<本次RB真实合约，含.SHFE>"
CS_HC="<本次HC真实合约，含.SHFE>"
```

本入口复用已有多品种LiveFuturesReferences，单次稳定读取同一组合快照；默认DolphinDB/source/observed-on-read，不补因子、不查询不存在的CU历史。启动及会话中重新核对，不仅相信独立只读探针。`observed-on-read`是本次稳定读取后的可见性下界，不声称历史发布时间。

## Recording核对信号

```bash
python -u -m demos.04_cross_section.run_live \
  --connect --products RB,HC \
  --expected-trading-day "$CS_TD" --expected-source-day "$CS_SOURCE" \
  --expected-instrument "RB=$CS_RB" --expected-instrument "HC=$CS_HC" \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.30 \
  --target-notional 35000 --max-quantity 1 --max-notional 50000 \
  --seconds 600
```

默认Recording不发单、不写交易状态。两腿各自Tick及接收年龄须≤10秒、TD/MD交易日一致、MD健康；只消费完整已收盘分钟，启动部分分钟丢弃。所有主力同一分钟到齐才推进，缺分钟不补造，不同步不会推进窗口；Bar事件年龄≤120秒。两品种/比例0.30各选一个，lookback3需要4帧；这是显式缩短验收窗口，默认策略仍lookback20/rebalance5，每侧预算100000，默认会话1800秒。有效调仓统计不等于成交。

核对scores为复权收益、排名/多空组、真实价×乘数的整数手数；每侧35000仅为示例预算，须据本次真实价格核算。预算不足一手允许0；大于一手上限时交易风控拒绝，不截断/强制一手。RB/HC两侧各一个时，35000/(真实价×乘数)向下取整；乘数以当前条款为准。限价偏移也计入订单名义金额校验。

## 受控SimNow组合成交

在Recording及本次账户核验通过后，仍使用已核对变量，独立新状态：

```bash
CS_STATE="demos/04_cross_section/results/rb-hc-$(date +%s%N)-$$.json"
python -u -m demos.04_cross_section.run_live \
  --connect --mode simnow --products RB,HC \
  --expected-trading-day "$CS_TD" --expected-source-day "$CS_SOURCE" \
  --expected-instrument "RB=$CS_RB" --expected-instrument "HC=$CS_HC" \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.30 \
  --target-notional 35000 --max-quantity 1 --max-notional 50000 \
  --seconds 600 --max-session-orders 20 \
  --enable-orders --confirm-simnow --state-file "$CS_STATE"
```

账户/策略身份为`cross-section-simnow`，逻辑目标键沿用04原规则，等于真实合约ID；不复用EMA身份/目标/状态。共享PositionManager及统一成交关联/去重、今昨账本、写前/回报后原子持久化。每个合约独立跳价/乘数/名义/手数限额；目标预算每侧一次组内等分，多腿不保证原子成交，不自动补偿。同步拒单在后续腿发送前拦截；异步拒单不能撤回此前已发送或成交的腿，会关闭后续执行并进入停机撤单/对账。

反手先平今/昨，后续完整同步帧决策结束后继续开仓；当前帧如发布新目标不再重试旧目标。重试保持目标revision，每条请求只包含对应合约，由该合约真实新鲜Bar触发；其他策略共享客户端拒绝保留目标推进。主力/条款/累计因子变更、行情失效/断线、拒单、回报冲突或持久化失败闭闸；首期固定路由不自动换约。Recording或报单会话一旦故障不可重开同一实例。

核对最终`summary.json`的status、orders_submitted、回调、scores/groups/last_targets、各腿strategy/account/unassigned_positions、final_gross、活动订单、cleanup_errors与state_file。报单通过需有成交/策略回调、未归属0、最新完整目标每腿均达到且无在途，单腿成交不能独自通过。`passed`只表示本次受控会话核验，不是盈利或所有故障/恢复验收。停机撤活动单、查询全账户并保存，不自动平剩余仓；停机后再执行td_readonly核对。

## 边界与后续

本节记录首期范围，当时未开放DCE/CZCE/CFFEX、第二套历史回放、全账户多策略并发、动态主力换约、历史数据库预热、跨交易日持续运行或自动停机重启。DCE/CZCE在PF2026101208新增源码适配，具体范围与待验状态见[四交易所交接](../../doc/HANDOFF/live/HANDOFF_CROSS_SECTION_FOUR_VENUES_2026-10-12.md)。状态包含仓位/订单/目标归属，**不包含完整横截面指标窗口**。PF2026101030新增显式同日恢复，见末节；不能删除状态绕过带仓核验。

源码与公共影响登记于[并行约定PF2026100920](../../doc/HANDOFF/coordination/HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md#pf2026100920-04横截面rbhc-simnow装配源码完成待远端)。回退须停止04并整套恢复本项公共文件；已产生交易状态/报告保留用于归属对账。跨聊天待同步，当前没有线上/离线通过证据。

## 最新实际Recording失败与修正（PF2026100922）

实际报告：`/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/demos/04_cross_section/results/cross-section-simnow-1791556990687918256`。自动主力与参考已装配，MD/TD=20261012，整体行情HEALTHY、receive_age_seconds=0.032396367，但PB最后成交Tick落后超过10秒，旧逐合约成交Tick闸门停止会话；同步帧、调仓、报单及成交均0，status=failed，不能算600秒Recording通过。Recording中simnow_session=False是未启用报单的正常状态；lshw/dmidecode缺失不是本次退出原因。

根因：标准TradeTick只在累计Volume增加时生成，QuoteTick同样按盘口去重；无新成交不能直接视为深度行情停滞。修正：公共CTP feed额外提供只读depth_observations，逐合约记录成功转换快照的TradingDay、事件时间与单调接收时间，时间回退锁存，断线/停机清空；不改变标准事件去重。04使用这套观察记录做10秒时效检查，报告per_instrument_depth并保留停机前观察，失效时打印具体合约及两种年龄。仍只用真实成交构建分钟Bar，不生成空分钟，不改变排名/预算/风控阈值；五品种真实同步帧可能需要等待成交。

新增3项回归（04现24项）覆盖无成交的深度更新、逐腿日期/双时钟/回退闸门、真实转换去重及生命周期清理。未在Mac执行Python或柜台；当前仅源码完成，远端验证待取得。同步PF0922清单文件后：

```bash
sha256sum -c doc/evidence/checksums/PF2026100922_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only cross-section --only entry
```

全部通过后重新运行（Recording、不发单；本次已核验日期，换会话需更新）：

```bash
python -u -m demos.04_cross_section.run_live \
  --connect --products CU,PB,NI,AU,AG \
  --expected-trading-day 20261012 --expected-source-day 20261009 \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.30 \
  --target-notional 100000 --max-quantity 1 --max-notional 100000 \
  --seconds 600
```

查看最终summary的status、synchronized_frames、rebalances及per_instrument_depth；本轮在线验收仍开放，不进入SimNow报单验收。

## 最新Bar时效异常处理（PF2026100923）

用户提供PF0922后五品种实际会话就绪，随后“分钟Bar过期或来自未来”异常；未提供Bar时间/合约、最终summary、报告目录或PF0922离线输出，不能断言具体过期来源，也不能登记通过。标准聚合器等待下一次成交才发出旧桶；即使深度行情正常，长时间无成交也可能迟发旧Bar。

04运行器现将age>120秒的成交Bar记入expired_bar_count及最近20条expired_bar_samples，打印合约、bar_ns、now_ns、age_seconds并跳过，不进入策略、不发单、不停止健康会话；age<0仍闭闸并输出同样诊断。参考刷新前后均检查，已接受Bar重复仍忽略，运行中资料/会话失效仍闭闸。公共聚合器和01入口未修改，120秒阈值不放宽，空分钟不填充，不增加MID或接收时间信号。缺少真实五品种同步帧时仍不能通过Recording。

新增3项回归：过期Bar跳过后新鲜帧继续、未来Bar闭闸、参考刷新期间过期不进策略。04共27项，Mac未执行Python；同步PF0923清单后Linux执行：

```bash
sha256sum -c doc/evidence/checksums/PF2026100923_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only cross-section
```

这次只改04，不重复已通过的无关组；PF0922若还未运行entry兼容回归，应另执行`python -u -m tests.run_cross_section_acceptance --only entry`。通过后使用前节相同五品种自动选约Recording命令（本次TD20261012/source20261009；换会话更新日期）。反馈最终summary，包括status、synchronized_frames、rebalances、expired_bar_count和expired_bar_samples。若仍来自未来，提供报错中的具体两时间与年龄；不能把未来Bar改成当前时间以绕过检查。

## 最新部署不一致（PF2026100924）

用户远端27项0.184秒，failures1/errors17、模块exit1/1.271秒：测试已是PF0923，但runtime仍在旧165行报“分钟Bar过期或来自未来”，run_live无parse_args。本地源有这两个新接口。上一PF0923清单漏run_live.py，当前修正并明确承认；不能把这次结果当新版实现已验或据此修改策略。未提供完整远端源码/报告目录，不猜同步原因。集中入口现先验证parse_args、bar_is_current和depth_observations，缺接口则部署失败停止。

当前统一清单PF2026100924_SHA256.txt包括run_live、live_runtime、公共CTP feed、测试、集中入口及说明。必须同步全部文件并让SHA全部OK；失败时先修正同步，不启动Recording。源码逻辑仍PF0922/23，Mac未运行Python。

```bash
sha256sum -c doc/evidence/checksums/PF2026100924_SHA256.txt
```

仅全部OK后：

```bash
python -u -m tests.run_cross_section_acceptance --only cross-section
```

请提供SHA核对和该模块最终结果。新版离线/在线仍待验，不重跑其他已通过组；通过后再运行既有五品种Recording命令。

PF0924后续验证：用户提供cross-section27项OK（0.204秒、模块exit0/1.295秒）、集中入口“全部选定离线模块通过”。本次04修正组离线通过，无需重跑。过期Bar用例正常跳过；/tmp/cross-section-offline-sflvc_dt/.../cross-section-simnow-1790038800000000000是离线夹具报告，不是实盘Recording。新通过轮集中报告目录未提供，不将此前部署失败轮目录登记为通过；全量SHA/entry新结果也未提供。源码与测试保持PF0924部署版本，更新仅为证据说明；下一步使用既有自动主力五品种600秒Recording，实际在线仍待最终summary。

最新启动失败报告cross-section-simnow-1791558733307236777：contract_structure查询异常，尚未生成参考manifest/固定合约或启动MD，报单0。04已为history_minutes=0，从本次实时成交重新预热，不访问历史分钟库；不需要历史缺失容忍开关才能冷启动。lookback3需要至少4个真实完整同步帧。此次fut_contract查询异常不能解释为历史行情缺失或空参考表，此前五品种参考只读曾成功；先复核参考只读，查询失败不绕过主力、累计因子和合约条款。源码/测试不变。

参考故障后续：用户使用readonly --timeout30复核五品种通过，ready=true/failure=null，来源20261009及fingerprint与先前相同，五品种主力/条款/因子齐全；上一轮查询异常原因仍未确定，不视为无历史行情或无参考。下一次Recording加入--reference-timeout30，继续history_minutes0实时预热；不放宽参考总刷新时效，不改源码或重跑离线。在线最终summary仍待验。

## 最新PB深度短暂停更（PF2026100925）

实际Recording报告cross-section-simnow-1791559024813958999，参考ready但PB深度事件年龄16.059940173秒/接收14.985419036秒超过默认10秒，frames/rebalances/orders均0，statusfailed。停机时PB快照已恢复到与其他腿同事件时间；不能据此确认网络、数据库或派发阻塞根因，未通过在线验收。

04新增--instrument-max-age-seconds，默认10不变，范围(0,120]秒、必须有限；同时限制逐合约深度事件年龄和单调接收年龄，阈值记录入报告及失败诊断。此次Recording显式30秒；交易日不匹配、未来或回退继续闭闸，整体MD/TD检查、Bar120秒、资料时效及策略公式不变，不删PB、不补空分钟。新加2项离线回归，04共29项，Mac未执行，先同步PF0925清单：

```bash
sha256sum -c doc/evidence/checksums/PF2026100925_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only cross-section
```

全部通过后五品种Recording：

```bash
python -u -m demos.04_cross_section.run_live \
  --connect --products CU,PB,NI,AU,AG \
  --expected-trading-day 20261012 --expected-source-day 20261009 \
  --reference-timeout 30 --instrument-max-age-seconds 30 \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.30 \
  --target-notional 100000 --max-quantity 1 --max-notional 100000 \
  --seconds 600
```

参数仅30秒观察窗口，并不保证PB足够成交产生分钟Bar或形成同步帧；仍以真实summary判断是否通过。此次尚不进入报单验收，日期仅当前会话适用。

## PF2026100926 消除行情回调同步参考查询

最新实际报告cross-section-simnow-1791559432681860719失败：39根过期Bar、frames/rebalances/orders0，最后Recording无目标；停机后回调仍在查询已关闭的数据源。源码中publish.refresh、spec/snapshot自动刷新使同一派发线程多次数据库读取并持锁，五腿成批过期符合队列积压；未量测数据库/GIL各自占比，不断言全部延迟根因。

04 PortfolioReferences改为原子发布已验证assignment/specs/manifest缓存。启动和生命周期poll控制线程调用service.snapshot一次（其内部已有刷新）；Bar回调、策略snapshot、固定资料检查和停机报告只读缓存，不读原service、不等待数据库锁。查询期间只可用仍在原观察限额内的成功缓存；过期、时间回退、不可见或刷新失败拒绝访问。缓存发布与策略submit锁互斥，整轮数据库查询不持submit锁；默认观察期限30秒不放宽，固定主力/条款/因子变化仍闭闸。shutdown先禁用Bar发布并等待正在发布完成，再沿原撤单/对账/停止Feed/关闭资源路径退出。公共LiveFuturesReferences/CTP feed/01/策略公式未修改。

旧夹具按跳分钟模拟控制线程刷新；过期Bar测试改为验证回调不读库，新增阻塞数据库读取时仍完成同步Bar回调的并发测试及缓存过期/查询失败拒绝访问测试。04共31项，Mac只源码/静态/SHA，无测试执行。新部署PF2026100926_SHA256.txt含8文件；只重跑受影响cross-section，其他已通过组不重复：

```bash
sha256sum -c doc/evidence/checksums/PF2026100926_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only cross-section
```

全部通过后沿用前节五品种Recording命令（reference-timeout30、instrument-max-age-seconds30、seconds600），需实际summary验证同步帧和目标。新缓存不会填空分钟，也不保证稀疏成交的五品种每分钟都有Bar；观察期限超过仍停止。此修正尚未取得远端结果，不能标记通过。

PF0926远端验证：cross-section31项OK（0.193秒，模块exit0/1.265秒），集中入口全部选定离线通过；无须重复。所贴/tmp/cross-section-offline-at3js4a5/...为假柜台临时报告，不是实际Recording。源码/测试不再变更，PF0926部署清单保留；本次证据文档另存PF2026100926_EVIDENCE_SHA256.txt。下一步使用前节五品种自动主力600秒Recording（数据库及逐腿时效参数均30），实际summary待验。

PF0926实际Recording通过：报告cross-section-simnow-1791560861869092362，statuspassed、frames7、rebalances4、expired0、参考ready、最终gross{}、activeorders0、cleanup_errors[]。revision1 PB目标-1手、AG多组预算不足一手为0；后续3次预算取整均0（负零也是0），无真实报单是Recording正常行为。当前参数验证了真实同步与目标生成，积压本轮未复现；不等同真实成交链通过。源码/测试未变，仅更新证据。下一步受控SimNow保持每侧100000/一手上限/每合约名义100000/最多20单，用新state-file，加--mode simnow --enable-orders --confirm-simnow；日期仍当前会话，停机撤单并报告剩余仓、不自动清仓。真实柜台成交和归属仍待验，已通过离线/Recording不重复。

实际SimNow结果cross-section-simnow-1791561641011670778：statuspassed、frames7/rebalances4、4订单/4成交/8订单回调、expired0、最终gross{}和activeorders0、全部策略/账户/未归属净仓0。全部成交仅PB一手：卖开16075→买平今16080→买开16080→卖平今16080。第4帧NI多0/PB空-1，第6帧PB多1/NI空负零，其余帧选组预算取整全0。原公式允许少于一手为0，非行情漏单或风控拒单。本次仅PB开平链与归属对账覆盖通过；两合约同时多空实际成交尚未覆盖，不能依据statuspassed认定整个组合验收完成。用户提出至少两侧非零的预期，与当前允许单侧取整0的语义不同；暂不更改公式或扩预算，需确定双侧可执行开仓门控/预算方案后再验证。

## PF2026101027 可选完整双组与一手目标

用户要求不必用满预算、每侧两个合约一手。新增--target-quantity-cap（正整数且<=max-quantity，预算取整后裁剪绝对手数，不强制最小一手）及--require-full-groups（任一多/空入选品种预算算不出一手，则全组合目标归零，旧仓允许退出）。默认关闭，不改变原回测排序/预算公式。metadata记录cap、require_full_groups和incomplete_group_products，报告记录配置。5品种group_fraction0.40每侧2个，示例每侧2000000、每入选1000000、cap1；实际资金占用不是名义预算，预算不会强制用满。max-notional2000000为逐合约保护限价风控上限，仍maxquantity1。

四个目标都是非零并不保证原子成交；执行仍按现有逐腿拒单锁存机制，若成交失败可存在临时不平衡，不新增自动平仓或隐瞒此限制。一手名义超过1000000时仍全组归零而非越过预算。新4项测试覆盖五品种四合约各一手假成交归属、预算不足不强制、旧仓退出及CLI验证；04共35项，原默认行为需signal组回归。Mac未运行Python，源码完成、远端待验。

同步PF2026101027_SHA256.txt全部9文件，先SHA全部OK，再Linux执行：

```bash
python -u -m tests.run_cross_section_acceptance --only cross-section --only signal
```

先Recording验算新目标：

```bash
python -u -m demos.04_cross_section.run_live \
  --connect --products CU,PB,NI,AU,AG \
  --expected-trading-day 20261012 --expected-source-day 20261009 \
  --reference-timeout 30 --instrument-max-age-seconds 30 \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.40 \
  --target-notional 2000000 --target-quantity-cap 1 --require-full-groups \
  --max-quantity 1 --max-notional 2000000 \
  --max-session-orders 20 --seconds 600
```

确认非零组合包含2个+1、2个-1，其余0，且无预算不足/参考/行情异常后，受控SimNow沿用该命令并加入--mode simnow --enable-orders --confirm-simnow --state-file "tests/results/cross-section-four-legs-$(date +%s%N).json"。新独立状态不覆盖旧PB通过文件；真实四腿覆盖仍待验，旧PBstatuspassed不可替代。停机撤单报告剩余仓位不自动平仓，日期仅本次会话适用。

## PF2026101028 按分钟汇合与缺帧诊断

实际四腿运行cross-section-simnow-1791563697799375072失败：frames1、rebalances/orders/fills0、PB过期1根146.552秒，账户最终空仓无活动单。lookback3需4个同步帧，所以未生成目标，尚未进入预算检查。旧策略latest只保存各品种一根，快腿进入下一分钟时可能覆盖慢腿尚未到达的当前分钟；日志没有逐Bar轨迹，不能证明全部缺帧原因。

仅04 live runner增加按分钟时间戳缓冲，同一帧五腿有效Bar到齐后统一交原策略；较早未齐帧最多保留到120秒，过期丢弃，已发布帧不倒退、不补空分钟。排名/手数/预热/参考可见性不变。报告新增per_instrument_bar_count/per_instrument_latest_bar_ns/pending_frames（包含缺腿）/expired_frame_count，验收先报告actual/required预热不足。无真实成交的分钟仍无法组成帧；本轮是否稀疏成交或迟发需新诊断。

新增2项回归：快腿先进入下一分钟，慢腿迟到仍恢复两帧；未齐帧过期不制造缺腿。04现37项，Mac未运行，先同步PF2026101028_SHA256.txt全部9文件并SHA核对；Linux只运行cross-section+signal（PF1027默认兼容结果尚未提供）：

```bash
sha256sum -c doc/evidence/checksums/PF2026101028_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only cross-section --only signal
```

通过后先使用上一节每侧两品种一手Recording600秒，确认帧/目标和pending缺腿；不要直接再报单试错。真实四合约成交覆盖仍待验；新源码/离线/在线未取得结果，不标记通过。

PF1028后续离线通过：cross-section37项OK（0.262秒、模块exit0/1.326秒），signal8项OK（0.001秒、模块exit0/0.058秒），入口全部选定离线通过；已通过组不重复。/tmp/cross-section-offline-3kinx6oh/...是无网络假柜台报告，过期日志属于测试，不能算实际新行情证据。源码/测试保持PF1028部署版本，仅证据更新另存PF2026101028_EVIDENCE_SHA256.txt。下一步前节两侧各两品种一手Recording600秒，需检查最终status/frames/rebalances、两正两负目标、pending缺腿/expired_frame_count；实际新版本四腿成交仍待验。

PF1028实际三品种Recording通过：AU/AG/SC报告cross-section-simnow-1791565156396931019，frames9/rebalances6、各腿Bar9且末分钟一致，expired_bar0/expired_frame0/pending[]、参考ready、账户空仓且无活动单/清理错误、statuspassed。3*group_fraction0.40向下取整为每侧1，非每侧2；末目标SC+1/AG-1/AU0，每次完整两侧各1手。无报单为Recording正常。可确认本次三腿汇合和目标生成正常，不等于五品种四合约成交覆盖，不能拆分换品种和缓冲变化效果。每侧2若保持0.40需5品种；4品种则0.50，3品种不可能互不重叠两多两空。源码/测试不变，真实SimNow多腿覆盖仍待验。

## PF2026101029 反手平仓后用Tick续开

实际三品种SimNow报告cross-section-simnow-1791565959209689316失败：frames9/rebalances6/orders9/fills9、expired0/pending[]，AU多+SC空已真实成交，但末目标AU-1/AG+1/SC0，实际AU0/AG+1/SC0。最新AU平今成交后没有后续完整帧推进开空，statusfailed latest target incomplete。柜台仍AG多1、无活动单，归属与本地一致；不要直接空仓重跑，也不自动平仓或接管。

新增04私有Bar feed包装，先聚合并完成本Tick触发的完整帧决策，再在新鲜真实Tick上复用continue_execution_target推进该合约旧目标。Tick只更新价格观察器和执行触发时刻，不进入信号窗口，不增加revision，不直接在成交回调发单；working/健康/账户对账/拒单锁存/资料一致性保持。旧Tick和重复Tick不能重发，停机accept_bars关闭后不推进；公共框架/01不改。新增反手平仓后无新帧仍完成两腿续开且归属正确、旧Tick不推进两项回归，04现39项，Mac未运行。

同步PF2026101029_SHA256.txt后Linux先cross-section；signal默认源码未变化无需重复已通过组。账户剩余AG多1是需要另外明确处理的持仓，不在本修正自动改变。真实新续开行为仍待验。

PF1029 Linux回归通过：39项OK（0.329秒、模块exit0/1.41秒），集中入口选定离线全部通过。本次仅执行续开变更，信号与三品种Recording已有通过证据，可以直接SimNow验证，不重复Recording。前轮AG多1未取得用户已平证据，须先处理并由启动权威gross/orders核验空仓、无活动单；不自动清仓/接管。新state独立命名，原文件保留，真实续开结果仍待验。

## PF2026101030 同交易日组合恢复（源码完成，Linux待验）

同步PF2026101030_SHA256.txt整组文件，先执行：

```bash
python -u -m tests.run_cross_section_acceptance --only cross-section
```

恢复必须给原成交会话保存的文件，不能用带date的新文件名。最新用户附件状态为`tests/results/cross-section-tick-continue-1791567360820818324.json`，保存AG空1/SC多1、无活动单；该账户之后若有人工交易，按实际权威查询核验，不能假定仍一致。原文件保留，建议先复制备份；恢复仍写回原文件并递增generation。

```bash
python -u -m demos.04_cross_section.run_live \
  --connect --mode simnow --enable-orders --confirm-simnow --resume \
  --products AU,AG,SC \
  --expected-trading-day 20261012 --expected-source-day 20261009 \
  --reference-timeout 30 --instrument-max-age-seconds 30 \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.40 \
  --target-notional 2000000 --target-quantity-cap 1 --require-full-groups \
  --max-quantity 1 --max-notional 2000000 \
  --max-session-orders 20 --seconds 600 \
  --state-file tests/results/cross-section-tick-continue-1791567360820818324.json
```

日期仅适用于本次TD20261012/source20261009。同日、相同合约路由、无本地及柜台活动单，才可恢复；校验文件checksum、账户身份、策略归属、净仓/全账户总仓、今昨分桶和资金后开闸。恢复订单状态/成交去重/目标版本，策略revision延续；实时动量窗口重新预热，期间保留已有仓位、不推进旧目标。新目标产生后才按排名调仓，不自动接管或清仓。若目标已达到，恢复验收允许本轮无新增成交，但须新帧/新目标、逐腿目标匹配、未归属及在途为零。校验失败保留原文件、闭闸；报告resume_ready/restored_generation。跨日、变主力、挂单断点暂拒绝。

本轮同时修正执行时间基准：行情价格可能晚于信号分钟收盘标签1–1.5秒，不能按信号标签判为未来行情。仅04订单目标提交使用当前执行墙钟，元数据signal_bar_ns保留原标签；分钟汇合和动量算法不变，真实未来行情及陈旧深度仍拒绝。Mac未运行Python/测试/柜台，新增恢复与时间基准回归待Linux验证，线上恢复未验收。

PF2026101031：首轮Linux47项执行报5个错误，均为新增恢复夹具缺少必需disconnect_handler、在构造Driver阶段停止，未进入恢复校验。已仅修正测试夹具，补与既有夹具相同的runner.close_role_gate断线回调，生产源码保持PF1030。在已完整部署PF1030基础上同步PF2026101031_SHA256.txt，仅复测cross-section；修正后未运行，不登记通过，不进入真实恢复验收。

PF1031后续Linux已通过：集中报告tests/results/cross-section-acceptance-1791568649484295767，47项OK（0.362秒、exit0、模块1.453秒）。接受恢复与执行时间基准离线回归，无需重复Recording或既有signal组；下一步按PF1030同日原文件命令实际恢复。当前只更新证据，生产源码/测试无变化；真实恢复尚待验。

## PF2026101032 实际恢复后的预热推进与并发保存修正（源码完成，待Linux）

真实报告`demos/04_cross_section/results/cross-section-simnow-1791568704576447555`：generation8/revision3已恢复，但第1帧（未预热）仍走完整帧末的旧目标推进，2单成交，保存遇到Driver/订单机不同代。当前原状态文件同前，最新总仓变为AU多1/SC多1、AG0；活动单0、cleanup_errors[]，整体failed，不能算恢复验收通过。不要删除、换新状态或用旧备份覆盖。

修正两处：完整帧末与Tick路径一样，必须已产生新last_targets才能继续目标；公共CTP自动持久化绑定客户端同一RLock，回报先拿锁再更新Driver，跨Driver/订单机/归属/持久化的过渡完整可见，不取消严格同代校验。未绑定的只读/旧Driver接口兼容，同步柜台回调使用可重入锁。公共修改影响01及其他CTP自动保存调用方，其源码不改，受影响底层组必须复验。

按PF2026101032_SHA256.txt同步整组，再执行：

```bash
python -u -m tests.run_cross_section_acceptance \
  --only cross-section --only ctp-autosave --only ctp-recovery --only ctp-driver
```

新增真实加载状态→Driver启动对账→首帧零发单→新信号反手/续开回归，以及公共受理/成交线程与保存并发回归。Mac未运行Python，修正后全部待验；既有signal与Recording不重复。通过后沿用同一--resume命令和原文件，程序核对最新AU/SC仓位，重新预热后才调仓。原文件里旧revision3的目标可与最终实际仓位不同（本轮只执行部分目标），恢复以已成交归属及柜台一致为前提，旧目标预热前不得继续。

PF1032后续Linux四组通过：集中报告tests/results/cross-section-acceptance-1791569259068621879，cross-section48项OK（0.483秒、exit0），ctp-autosave（包括并发）/ctp-recovery/ctp-driver均exit0。无需重复离线或Recording；下一步同一原文件真实--resume，最近柜台AU多1/SC多1、AG0，由恢复查询核对。首次新目标前应保持零新单，lookback3需要4个同步帧；generation/revision使用本轮实际读到值，不固定要求8/3。当前仅更新证据，不改源码，真实恢复仍待验。

最新实际报告cross-section-simnow-1791569325789905813：恢复generation16/revision3，9帧6调仓13单12成交；最终AG空1/SC多1、AU0，策略/账户一致，未归属0、活动单0、cleanup_errors[]。statusfailed原因是在停机撤单之前verify检查AU有非零working，即使target0/position0也拒绝；旧AU卖开OrderRef13未成交，随后停机撤单。保存同代异常未复现。首两笔SC仍推进旧revision3、发生在第4帧新revision4之前，与当前修复版预热门控不符；需要核对运行机live_runtime实际SHA，不能认定预热修正线上通过。最新原文件保存仓位AG空1/SC多1、AU0，禁止旧备份覆盖。本次仅解释和证据更新，旧挂单随目标变更取消及退出诊断尚待处理，实际恢复未全部通过。
