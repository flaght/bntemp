# 08日历接口与Recording验收

08 Live默认调用公共Cython日历，回测读取同版本导出文件。真实Recording自动运行与
报告核验已交付；日历相关远程离线组通过。实时Recording曾因指数缺失失败；
Replay工程映射已完成分钟帧、非零Recording目标、Model成交及策略归属仓位验证；
Model状态文件读回已通过，重启恢复及原生柜台尚待核验。
此次不连接SSH、DolphinDB或柜台，不登记线上通过。交付版本PF2026101204。

## 日历

`bomber/framework/calendars/trading.py` 提供CalendarProvider、CalendarSnapshot、
NativeCalendarProvider和ExportedCalendarProvider。Live以`--calendar-id china.cffex`
装配，策略仍接收日期快照，不判断来源、不查数据库。原`--calendar`从08 Live删除；
回测入口保留文件参数。库的底层规则没有改变，无须重新编译已通过的11个扩展。

当前CFFEX/SSE维护范围声明为1991—2026。超范围查询或缺失扩展直接失败；不填充工作日，
不推测2027节假日。08既有远期阈值算法在已知未来天数足够时使用下界，不足时拒绝判断。
规则版本、覆盖范围、日期内容指纹进入报告和恢复身份；旧状态不自动迁移。
导出CSV及manifest包含全部自然日、交易日标志、规则身份与SHA256，回测加载时校验。

## Recording

08保持来源分离：CTP期权/同月期货，DolphinDB DFS指数，Recording执行。
新增选约audit记录业务时间、候选数、TARGET/NO_VALID_ENTRY/ZERO_ALLOCATION/WORKING_ORDERS；
分钟audit记录来源时间、实际接收、期限及CONSUMED/SKIPPED_INDEX。报告另保存Recording
客户端实际请求，不能仅以策略产生目标或frame_count证明执行路由已收到目标。

`scripts.integration.options.recording` 固定调用Recording、继承.env的实时MD配置，默认4200秒。
检查实际日期与启动时点，需在13:58前至少ready-timeout+65秒启动；默认建议13:53前。
运行后只读取本次唯一summary，自动检查当天MD、编译日历、指数查询、13:58帧的来源/接收期限、
同分钟交易腿及客户端非零目标。进程非零退出或清理失败均失败。
停止后查询缓存connected=False为正常状态；核验保存的查询完成和送达证据。

没有目标时默认不通过目标链。`--summary ... --allow-no-target`只复核决策评价，scope为decision_only，
不称非零目标链通过。线上未出现超时样本时，不伪造超时验收，依赖既有离线迟到拒绝测试。
未来核验真正返回的runtime summary和recording-acceptance.json后才登记实际通过。

## 测试与部署

本地CPython3.12：日历/报告/08纯策略49项通过；真实框架Runner、归属、风险、恢复及查询
装配40项通过，其中SDK实体为替身、数据库为夹具。后一组不是原生SDK/真实来源证据。
纯日历使用实际已编译扩展；部署入口另外9项通过，本地合计98项。
证据为doc/evidence/live/OPTION_CALENDAR_RECORDING_OFFLINE_20261012.json。
此次远程验收尚未执行；原PF1203远程252项和指数只读/CSV往返
证据继续按其旧范围保留。

同步PF2026101204_FILES.txt列出的代码文件，使用PF2026101204_SHA256.txt核验。
集中入口的内置摘要已更新，新增option-calendar组，避免新代码被旧摘要误判混版。
无须同步旧二进制、覆盖状态文件或重复编译未修改的Cython源码。

```bash
python -u -m tests.run_cross_section_acceptance \
  --only option-calendar --only feature-query-live --only option-vega --only option-vega-live

read -r -p "实际MD交易日 YYYYMMDD: " GAP08_DAY
python -u -m scripts.integration.options.recording \
  --trading-day "$GAP08_DAY" --seconds 4200 --report-dir tests/results/gap08-recording
```

自动入口不负责选择或改写CTP前置；运行前.env中的CTP_MD_ADDRESS须为当天实时MD环境。
选约时点已过则拒绝本次启动，下一交易日使用当天已经核验的MD交易日重新执行。

## 启动等待修复

PF2026101204_REV2修复首次真实Recording暴露的问题：MD未送达时，旧启动循环只刷新指数，
参考资料30秒后过期；随后停机调用严格snapshot又抛异常，丢失交易日/报单计数字段。
报告缺计数被误写成“发生报单”。这次实际运行0帧、0目标，指数已缓存168条，但CTP没有首份行情。
证据为doc/evidence/live/OPTION_RECORDING_STARTUP_FAILURE_20261012.json，不登记Recording通过。

公共live/readiness.py在控制线程先刷新参考和查询缓存，再检查只读就绪条件；08 Recording/Model、
原生新会话以及公共CTP恢复复用。等待仍有期限，断线、刷新失败或资料版本变化都不打开策略闸门。
manifest只读最后观测事实并输出ready/failure/年龄，不把过期资料交给策略，不再破坏停机报告。
08入口默认INFO显示连接/登录/订阅；停机结果保存实际market_sources配置。真实CTP未送达的原因
仍须依据同环境IM2610/MO2610-C-8000原始MD探针确认，不能从WAITING_FOR_FIRST_EVENT推定登录成功。

REV2本地公共Live116项、受影响参考/报告/等待/恢复57项、SDK实体替身装配42项、部署入口9项通过；
组间有重叠，不相加作唯一测试总数。远程REV2和新的真实Recording尚未执行。
同步PF2026101204_REV2_FILES.txt的13个文件及对应摘要，无需重复同步未变化的日历代码。

```bash
sha256sum -c PF2026101204_REV2_SHA256.txt
python -u -m tests.run_cross_section_acceptance \
  --only option-vega --only option-vega-live --only option-calendar --only live-common
```

## 原始深度健康与指数送达诊断

PF2026101204_REV3处理第二次真实Recording（14:18启动，选约14:30）：CTP实际连接、登录、
105合约订阅且送出分钟Bar，参考资料ready且清理无错误。14:19—14:22四帧全部SKIPPED_INDEX，
DFS缓存203条但无投影版本；14:23因STREAM_UNHEALTHY/STALE停止，未到选约，0报单/0成交。
证据为OPTION_RECORDING_INDEX_MISSING_20261012.json；不登记真实Recording通过。
用户随后确认原表date=2026.10.12，minTime=14:18—14:24共7条，未提供当时可见性证据。
72个迟到Bar未区分来源，不能解释为72个迟到指数Bar。

CTP旧健康仅随派生Tick更新；原始深度持续收到但累计成交量/盘口不变时可能误报STALE。
公共StreamHealthMonitor新增经过校验的来源观测，CTP转换成功后登记，派生事件去重保持原行为。
按合约检查原始时间回退，未知合约/转换失败不刷新健康；真实静默、队列溢出和分发失败仍拦截。
这是来源层修复，策略没有CTP健康特殊分支，不会制造零成交Bar。

公共FeatureQueryCache新增cached_slots、latest_event_ns/time及可见/接收时间。
FeatureQueryBarFeed保存最近32个请求分钟的查询状态和源时间；MISSING也保存缓存最新业务时间。
PROJECTED表示Feed实际发出，是否赶上帧截止仍依据frame_audit。标签/时区/等待预算没有放宽。
验收超时说明按实际SKIPPED_INDEX记录更新，避免timeout_seen_online=True却写没有样本。

本地CPython3.12：受影响健康/报告/DataHub/指数/部署入口72项，公共Live120项，
SDK实体替身装配42项通过；有重叠，不作唯一测试总数。未连接数据库或柜台。
同步PF2026101204_REV3_FILES.txt列出的10个源码文件，使用REV3摘要核验，再执行：

```bash
sha256sum -c PF2026101204_REV3_SHA256.txt
python -u -m tests.run_cross_section_acceptance \
  --only live-common --only feature-query-live \
  --only option-vega --only option-vega-live --only option-calendar
```

复测实际Recording须重新选择尚未到达的选约时点并预留就绪时间，不能把已过时点的无决策运行登记通过。
新runtime summary中的feature_queries.minute_lookups和latest_event_time用于继续定位指数来源问题。

## REV3远程回归与指数时间映射核验

用户返回的REV3十个源码摘要全部匹配。远程uv-nautilus五组选定离线测试均通过：
feature-query-live 9、option-vega 34、option-vega-live 14、option-calendar 18、live-common 120，
共执行195项。证据为OPTION_RECORDING_REV3_REMOTE_20261012.json。

相同来源身份/Schema的指数只读探针成功读取000852七条记录：
first_event_ns=1791785880000000000（上海14:18），last_event_ns=1791786240000000000（14:24），
与原表分钟结束标签及CTP请求边界一致。查询于15:00:04完成；observed模式available_ns
表示本次首次观测时间，不证明14:19—14:22运行时可见，也不能解释为数据库15:00才写入。
直接Provider探针received_ns=null正常；运行时FeatureRefreshService另登记查询完成接收时间。

新版本的真实Recording仍待交易时段复测。沿用end、Asia/Shanghai及5秒预算，
读取feature_queries.minute_lookups、latest_event_time和frame_audit定位送达，不凭历史查询通过登记目标链。

## 08专用Replay工程时钟

PF2026101205新增08自身`replay_test.py`，在公共CTP聚合器发出已完成Bar时复制并映射业务时间。
本次公共框架与`strategy.py`均未修改。默认路径保留原始时间，只有显式参数启用工程映射。
`--replay-test-start-time 13:55`把首份完成Bar映射到估值日13:55；所有合约共享固定偏移，
指数查询映射后的同一分钟，原策略按13:58正常选约。也可声明源16:00、目标09:00固定对应。
源Bar/深度不变、OHLCV与接收时间不变，不造未成交Bar，不填缺指数。源日期或映射估值日跨日即失败。
时钟映射只支持CTP Replay+DFS历史指数及新会话，Recording/Model/已授权SimNow执行来源独立。

报告标记`engineering_chain`与`replay_test_mapped`，保留源日、偏移和原始/映射时间样例。
属于工程链路验证，不能作为同步历史行情或实时策略验证证据。
`scripts.integration.options.recording`的真实实时验收继续排除Replay，不改其通过标准。
完整示例见08 README的Replay工程时间映射一节。

本地CPython3.12：SDK实体替身装配49项、08纯策略/参考/报告41项、公共Live120项、
部署入口9项通过；有重叠，不合计为唯一测试数。装配实际运行公共Runner、帧、DataHub、
执行和归属模块，覆盖映射跨日原始时间、Recording非零目标及Model策略归属成交。
数据库为夹具、无网络，不登记远程新版本或实际柜台通过。

同步PF2026101205_FILES.txt的8个源码文件及SHA256清单，然后执行：

```bash
sha256sum -c PF2026101205_SHA256.txt
python -u -m tests.run_cross_section_acceptance \
  --only option-vega --only option-vega-live --only live-common --only option-calendar
```

## 实际Replay工程行情和选约评价

2026-10-12用户返回的实际CTP 40011加DFS历史指数运行正常停机，market_frames通过。
原始2026-10-08 16:52完成分钟映射到2026-10-09 13:55，固定偏移1263分钟。
412个完成Bar被映射，13:55—14:04共10个帧CONSUMED，指数送出对应10个分钟且跳过数为0。
13:58帧的指数与IM/MO同分钟，全部实际接收均在5秒期限内；参考ready、通道ready、清理无错。
证据为OPTION_REPLAY_TEST_CLOCK_MARKET_DECISION_20261012.json。

13:58评价有6个候选，但结果ZERO_ALLOCATION；entry_decisions只计非零开仓，所以为0。
Recording请求、目标提交、报单及成交均为0。本次只证明工程行情和选约评价，不证明非零目标或执行链。
271个迟到Bar没有来源拆分，不能据此推定指数迟到；同分钟指数实际已送达且全部帧消费。

现有allocate_short在组合数量超限时按比例缩小并floor。多档Delta分别选到3/4个合约、
各1手但组合上限2手时，可以全部取整0；示例验证4档为空、单档0.25为1手。
原始报告没有实际分配明细，具体ZERO路径未证实。下一次只增加`--delta-targets 0.25`，
保留max-option-lots=1、max-total-option-lots=2，复测非零Recording请求。
未修改算法或公共框架，无需重新复制源码；08 README示例已改用这个小手数验收参数。

## 实际Replay工程Recording目标链

2026-10-12用户将Delta档改为0.25后运行600秒，10个分钟帧全部消费，指数缺失0。
源2026-10-08 17:11完成分钟映射到2026-10-09 13:55，偏移1244分钟，351个完成Bar被映射。
13:58评价有7个有效候选，目标MO2610-C-7300=-1，并明确绑定IM2610。
Recording实际收到revision=1请求，其唯一非零目标与选约目标一致，signal_ns对应13:58，
ts_event使用2026-10-12 17:06:31.331498的实际请求墙钟。
该帧指数、目标期权及对应期货同分钟，所有观测接收均在5秒期限内，帧封闭后提交请求。

runtime passed、通道ready、参考ready、清理无错，物理账户未关联。
Recording不产生成交，0报单/0成交符合本次执行来源。220个迟到Bar未拆来源，
已消费帧和实际目标仍具备同分钟期限证据。
证据为OPTION_REPLAY_TEST_CLOCK_RECORDING_TARGET_20261012.json。
登记范围为Replay工程Recording非零目标链；不能记为同步历史策略、实时Recording或原生下单通过。

下一步保留同一行情/映射/单档参数，将mode改为model，使用新独立state-file，增加require-fills。
策略仓位来源model、账户核对off，检查本次模拟归属成交及停机状态保存。
工程映射不接受resume；实际恢复继续使用原始时间路径的既有恢复流程。

## 实际Replay工程Model成交和归属仓位

2026-10-12实际Model运行600秒，runtime passed，acceptance=attributed_fills。
9个帧全部消费，历史指数缺失0，13:58有10个候选并形成MO2610-C-7300空1手目标。
模拟归属成交1次、订单更新2次，策略仓位和Model执行仓位均为-1，与保留目标一致。
物理账户核对off、仓位来源model，未连接TD，柜台报单0，停机保留仓位、清理无错误。
该运行没有非零期货目标，不登记期货对冲成交。证据为OPTION_REPLAY_TEST_CLOCK_MODEL_FILL_20261012.json。

状态文件报告路径为demos/08_option_vega/results/replay-model-20261012-093719.json。
运行已完成持久化调用且无错误，文件内容及校验和仍需用户在远程调用JsonStateStore.load读回。
fresh会话的resume_ready=True不证明重启恢复；工程时间映射仍不接受resume。
登记范围为工程Model成交与策略归属仓位，不登记原生下单或历史策略有效性。

## Model状态文件读回

用户在远程通过公共JsonStateStore.load读回
demos/08_option_vega/results/replay-model-20261012-093719.json，文件校验和及版本检查通过。
generation=14，非零策略仓位为strategy_id=option-vega-live、
target_key=MO2610-C-7300.CFFEX、quantity=-1，与停机报告一致。
Model状态落盘与读回核验通过；这不是重启恢复验收，不将模拟仓位作为柜台仓位接管。
下一步使用7×24 TD只读探针确认实际登录日、净仓和活动订单。
原生执行交易日依据TD结果，Replay MD/估值日仍按MD证据；原生会话使用独立新状态文件。

## 7×24交易端只读核验

用户在2026-10-12通过40001前置完成TD登录、结算确认及净仓、资金、活动订单查询。
TD trading_day=20261009，instruments=0，account_revision=1，CNY，orders_revision=1，active_orders=0。
只读查询未报单；原生成交验收仍待执行。
下一次原生会话使用TD日20261009、Replay MD日20261009、工程映射估值日20261009，
单档Delta0.25、每合约1手、组合2手、最多4笔报单及require-fills。
采用strict柜台对账与fills仓位来源，使用新独立CTP状态文件，不读取Model仓位作柜台接管。

## 首次原生报单未成交

用户返回的原生CTP工程运行已消费10帧，13:58有15个候选、目标MO2610-C-7300=-1，
发出1笔订单、订单更新2次，但归属成交0。require-fills使本次验收正确失败。
停机后final_gross={}、final_active_orders=0，策略与柜台仓位均为0，清理无错。
源通道ready、指数同分钟与接收期限已核验，当前待核验范围是订单接受/拒绝/未成交原因。
摘要没有订单状态和reason，2次更新不能单独区分拒单或未成交后撤单。
需读取tests/results/gap08-replay-ctp/option-vega-simnow-1791799716299368929/events.txt，
依据回报继续定位；此阶段未修改跳价、手数或公共框架，不登记原生成交通过。
证据为OPTION_REPLAY_CTP_ORDER_NO_FILL_20261012.json。

用户随后返回订单事件：MO2610-C-7300 SELL OPEN 1，柜台OrderSysID=12073，
ACCEPTED于1791799951592554816，CANCELED于1791800319590772410，间隔约368秒。
两条回报成交量均0，没有REJECTED。结合停机HALTED撤单流程，确认本次接受后未成交并撤单，
原生接入/接受/撤单链已通，原生成交仍未通过。事件未保留限价或盘口，价格与撮合原因仍需查询。
现有CtpLimitPlanner使用执行参考价（本例为已完成分钟Bar价）减1个tick生成卖单限价，
不读取实时买一来定价；因此接受不保证成交。需先查询委托LimitPrice并读取C7300盘口，
不能由此直接断言原始委托必然未跨过买一。

## 08原生执行价格诊断

后续查询确认12073已撤单、成交量0，但LimitPrice/InsertTime/CancelTime均缺失。
本地绑定bomber_ctp_td.cpp的OrderField映射没有导出这三个字段；null是读取缺字段，
不能据此判断柜台没有限价。当前C7300盘口为last=29.4、bid=29.2@25、ask=29.6@16，
但当前盘口不还原旧委托时刻，不把它作为旧订单未成交的确定原因。

PF2026101206仅在08新增execution_audit.py包装原生Backend，提交原OrderIntent前记录真实
limit_price、完成分钟reference_price及已观测Quote；公共框架、绑定和策略算法均不改。
额外订阅同一批CTP合约Quote用于诊断，不改策略Bar输入或执行参考价。
保留原生driver/回报/对账/持久化代理，提交一次、异常原样传出，不重试、不调整价格。
报告order_submission_audit和INFO“CTP报单诊断”保留至多32条；盘口缺失/过期时跨价为null。
crosses_observed_quote仅比较观测报价，不能当成柜台成交证明。

本地SDK实体替身装配53项、08/审计/部署回归54项、公共Live120项通过，有重叠。
远程新版本及带诊断的原生复测尚待执行，不登记成交通过。
同步PF2026101206_FILES.txt的7个源码文件及摘要，无需重编或覆盖已有状态文件。

```bash
sha256sum -c PF2026101206_SHA256.txt
python -u -m tests.run_cross_section_acceptance \
  --only option-vega --only option-vega-live --only live-common
```

再使用新的独立CTP状态文件重跑原生命令，保持原跳价及手数上限。
依据实际提交价和同时观测盘口决定下一步定价或撮合诊断，不从旧null字段猜价格。

## 原生Replay成交与状态读回核验

2026-10-12带价格诊断的原生SimNow运行通过归属成交验收。MO2610-C-7300卖出开仓1手，
限价37.2、成交价37.4，柜台订单号12681。策略、执行账本、账户净仓均为-1；
停机后柜台总仓为多0/空1，活动订单0，清理错误为空。
公共JsonStateStore.load读回原状态文件，generation=15，校验和通过，
保存的option-vega-live归属仓位MO2610-C-7300.CFFEX=-1与运行结果一致。
证据：doc/evidence/live/OPTION_REPLAY_CTP_NATIVE_FILL_20261012.json。

本次只验证工程时间映射链路，未验证实时非映射、非零期货对冲成交或重启恢复。
原状态文件demos/08_option_vega/results/replay-ctp-20261012-114149.json必须保留。
PF2026101207以dataclasses.replace修复冻结执行组件包装，并补装配回归，本地54项通过。

## 2026年10月13日实时非映射验收

09:30开盘后、09:32前启动以下Recording命令，运行约15分钟，覆盖09:35选约。
09:35是本次显式测试参数，正式默认选约时点仍为13:58。
实时MD前置使用此前核验的30011；登录日必须为20261013。
指数DFS表须持续更新当天已收盘分钟，minTime保持结束标签。
日历通过编译公共接口加载，不提供日历文件，也不启用时间映射。

Recording不连接TD或报单，账户现有空仓不影响本次行情与目标测试。
不要将Replay映射状态用于当天实时恢复：会话身份包含日期、行情前置和映射配置，
当前恢复只允许同一固定资料会话。需要原生实时执行时，先单独设计现有归属仓位的迁移。

```bash
(
  cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
  export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
  export CTP_PRODUCTION_MODE=true

  python -u -m demos.08_option_vega.run_live \
    --connect --mode recording \
    --market-source ctp --ctp-price-type last \
    --simnow-environment realtime \
    --expected-trading-day 20261013 --valuation-day 20261013 \
    --calendar-id china.cffex --reference-source dolphindb \
    --option-product MO --future-product IM --index-code 000852 \
    --index-source dolphindb-dfs \
    --index-database dfs://min_bar --index-table index_1min \
    --index-bar-timestamp end --index-availability observed \
    --index-price-precision 4 --index-size-precision 4 \
    --index-poll-seconds 0.2 --index-query-timeout 2 \
    --reference-timeout 30 --reference-refresh-seconds 5 \
    --frame-wait-seconds 5 --instrument-max-age-seconds 30 \
    --ready-timeout 180 --seconds 900 \
    --option-slot 09:35 --delta-targets 0.25 \
    --max-option-lots 1 --max-total-option-lots 2 --max-future-lots 1 \
    --max-notional 2000000 \
    --report-dir tests/results/gap08-realtime-recording
)
```

返回停机结果、summary.json和events.txt，检查frames、entry_audit、signals、
skipped_index_frames、frame_audit及session_readiness。status=passed只能证明运行门槛通过；
必须进一步看到09:35的TARGET及实际Recording请求才登记目标链通过。
无候选或ZERO_ALLOCATION保留为诊断结果，不登记原生成交或对冲成交通过。
