# Hybrid：RB/P共用账户，同进程与多进程切换

入口：`python -m demos.10_hybrid_transformer.run_shared`。两种布局复用既有共享执行服务；模型、信号和五分钟批次使用原实现，不增加第二套策略算法。

## 配置

配置位于 `demos/10_hybrid_transformer/config/shared_rb_p.json`。相对路径以配置文件所在目录为基准；读取后固定为绝对路径，子进程沿用同一配置。

配置文件位于`config/`，所以`signal_rb.json`等同目录文件直接写文件名；报告目录用`../results/live`；全新的账户与策略状态文件用`../results/live/state/...`。相对路径不是相对于启动时的工作目录。

- 账户：`client_id=hybrid-transformer-live`，预算4手、200万元名义金额，每个交易日最多100笔。预算需要按实际需求核对。
- RB：策略ID保留 `hybrid-transformer-live`，离散信号，每批上限2手、总上限2手，DolphinDB行情及260根历史预热。行情、历史与信号集中在`config/`；本次使用新的账户和策略状态，原`prod`下的旧文件保留但不读取。
- P：策略ID `hybrid-p-live`，`pc/10001`模型、159001方向文件、1101连续信号，每批上限2手、总上限2手。示例使用DolphinDB DCE行情及260根历史预热。P的信号、行情和历史分别由`signal_p.json`、`market_dolphindb_p.json`、`history_p.json`声明。
- 两种布局均固定 `execution_mode=independent`，不会因为变成同进程而改用跨策略净额抵消。
- 每个批次独立持有5个交易分钟；连续信号按每批预算量化为整数手，到期与同一时点的新信号合并为净目标。

## 每个品种的独立配置

各策略的`argv`分别声明`--release-path`、`--directions-file`、`--signal-config`、`--market-config`、`--history-config`与`--report-dir`，不会自动沿用另一个品种。`state_file`写在策略条目上，不放进argv；账户委托、成交和仓位归属共用顶层`account_state_file`。

| 字段 | RB | P（当前示例） |
| --- | --- | --- |
| 模型发布 | `1release/rbb/10002` | `1release/pc/10001` |
| 特征方向 | `rbb/rulex/113001/nxt1_ret_5h/cohort_pro.csv` | `pc/rulex/159001/nxt1_ret_5h/cohort_pro.csv` |
| 信号配置 | `config/signal_rb.json` | `config/signal_p.json` |
| 行情配置 | `config/market_dolphindb_rb.json` | `config/market_dolphindb_p.json`，DCE来源 |
| 历史配置 | `config/history_rb.json`，读取260根 | `config/history_p.json`，读取260根 |
| 策略状态 | `results/live/state/rb_signal.json` | `results/live/state/p_signal.json` |
| 报告父目录 | `demos/10_hybrid_transformer/results/live/RB` | `demos/10_hybrid_transformer/results/live/P` |

P按已确认的RB同源规则配置：实时流表`market_bar_state_v2_dce`，历史库`dfs://ctp`、表`bar_1min_dce`，交易所`DCE`。实时流保持`volume_mode=per-bar`，历史表保持`volume_mode=cumulative`，成交额/成交量由历史读取层差分；`open_interest`为持仓量快照，不作差分。字段映射、分钟开始标签、交易日date与timestamp类型的minTime均沿用RB。

两个品种均先尝试加载260根历史，不足的部分继续等待完整实时分钟。市场连接和历史读取使用`DDB_MARKET_*`，主力/条款等参考资料继续使用`DDB_REFERENCE_*`。本次明确作为全新会话启动：不读取旧`signal_config.json`相关检查点，不迁移或覆盖旧文件。当前`signal_rb.json`、`signal_p.json`建立新的模型身份，指纹校验仍保留。

## 同进程启动

在服务器 `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro` 目录、已配置模型依赖及.env的终端执行：

```bash
/workspace/worker/env/chs-live/bin/python -u \
  -m demos.10_hybrid_transformer.run_shared \
  --config demos/10_hybrid_transformer/config/shared_rb_p.json \
  --process-layout same-process \
  --run-forever \
  --connect --enable-orders --confirm-simnow
```

一个进程运行两个模型/Runner和唯一CTP执行服务。

## 多进程启动

同一配置，只更换布局：

```bash
/workspace/worker/env/chs-live/bin/python -u \
  -m demos.10_hybrid_transformer.run_shared \
  --config demos/10_hybrid_transformer/config/shared_rb_p.json \
  --process-layout separate-processes \
  --run-forever \
  --connect --enable-orders --confirm-simnow
```

调度入口启动一个账户server和两个独立worker，只有server登录TD；server不加载Transformer模型，worker各自计算并通过Unix IPC提交目标。故障不自动重复报单或重启；停机先排空worker回报，再停唯一账户服务。

## 盘前准备与开盘放行

启动命令不需要增加参数。`run_shared.py`用`PREPARE_SECONDS=300`提前5分钟启动每个
真实交易时段的准备：登录/结算确认、账户与委托核查、状态恢复、参考资料检查、行情订阅和历史预热。
例如09:00开盘，08:55开始准备；10:30恢复交易则10:25准备；夜盘21:00则20:55准备。
交易日和夜盘归属仍由各品种日历决定，例外停市不会按固定钟点启动。

准备完成会输出`盘前准备完成，等待开盘`，此时维护行情连接与账户观测，保持策略决策和报单关闭。
盘前没有新报价不算开盘后就绪超时；开盘后重新核验行情、参考与账户，正常通过才授权并开始决策。
如果预热不足，继续沿用原有等待完整实时分钟的规则。真实连接/查询/数据异常仍会报告并停止。
启动晚于准备起点时立即准备，不要求在08:55或20:55重新执行命令。

同进程在唯一宿主内准备两个策略；多进程由账户server提前接受worker连接，worker各自预热，
server在开盘前不授权。调度器内部传递开盘边界，不改变模型、持仓期限、检查点身份或配置文件。
开盘后才启动的进程仍会完成同样核验；准备耗时超出开盘边界时，以全部准备与健康检查完成为准。

## 首次启动与后续恢复

1. 原来直接登录TD的RB进程如仍在运行，先正常停止并等待保存完成，释放账户写入锁。新会话自己的策略仓位从0开始，不要求柜台RB/P合约空仓：`existing_position_policy=preserve_external`把启动时已有的多空仓及今昨明细保存为外部基线，包括同合约仓位和范围外仓位；不转为新策略归属、不用其抵消新开仓，也不主动平掉。未知活动委托仍按原共享账户恢复规则处理。

   例如柜台已有`rb2701`多2手，新RB策略仓位为0；新策略空信号2手会另开空2手，柜台变为多2/空2。新空头批次到期后只平自己的空2手，柜台原多2手保留。外部基线后续异常变化仍会触发核验；其他策略若持续交易同一合约，应接入同一共享执行服务，以明确订单与成交归属。
2. 配置使用全新账户文件`demos/10_hybrid_transformer/results/live/state/account_rb_p.json`，以及同目录的`rb_signal.json`、`p_signal.json`。首次文件不存在时新建；旧`prod/01_hybrid_transformer/results/live/state_RB_20261029.json`等文件保留但不读取。
3. 后续重启沿用这三份新文件，自动恢复新会话自己的订单、仓位归属和批次。不要每次删除状态文件或换路径来绕过恢复。
4. 布局切换时正常停止当前共享会话，再修改`--process-layout`启动；保持同一配置及状态路径。布局不进入策略身份，不重新计算原批次五分钟期限。两种布局不能同时占用同一账户。
5. `--run-forever`由公共交易日历安排休市等待、午休和下一交易日。初始trading_day用于声明和离线校验，各运行窗口自动更新实际TD和前一来源日；主力合约或模型/风险参数变化仍按原严格检查处理。
6. 首版联合调度要求各策略的交易日历、时段和例外规则一致；RB/P分别读取自己的规则，不借用其他品种时段。

## 报告

按各自`--report-dir`归档：示例为`demos/10_hybrid_transformer/results/live/RB/hybrid-simnow-RB-20261103/`及`demos/10_hybrid_transformer/results/live/P/hybrid-simnow-P-20261103/`。未声明策略报告目录时沿用顶层`report_dir`；顶层仍保存账户/调度报告。各策略的夜盘、日盘、同日重启继续追加信号、批次与执行事件。进程布局切换不另分策略目录；summary是最新会话，sessions.jsonl保留各次摘要。

旧纳秒目录不会自动迁移。交易恢复仍使用账户文件及各策略信号文件，不使用报告快照。

## 离线检查

去掉连接和报单授权参数即可只校验声明式配置：

```bash
/workspace/worker/env/chs-live/bin/python -u \
  -m demos.10_hybrid_transformer.run_shared \
  --config demos/10_hybrid_transformer/config/shared_rb_p.json \
  --process-layout separate-processes
```

结构检查不加载模型，也不证明服务器上的模型文件、数据库或真实柜台可用。专项测试使用真实策略、批次、共享服务、持久化和Unix socket，模型与柜台为离线替身；真实服务器组合仍需运行验证。

### 休盘前停止报单与撤单

同进程及多进程的 Hybrid 共享入口在每个交易时段结束前30秒开始正常停机，
例如10:15休盘时，在10:14:30关闭新目标并撤销未成交委托。已成交持仓、
五分钟批次及原到期时间保留，下个窗口继续恢复；不会为了休盘提前平仓。
账户查询返回后会再次检查窗口，避免慢查询跨过停止边界后继续产生新目标。
同进程入口的CTP查询等待每0.1秒检查调度停止信号，到达停止边界时退出
本地等待并进入撤单，不再等完整的60/120秒查询期限。旧查询身份继续保留，
停机核验时须先等旧最终回报，不能拿半份数据恢复交易。
原生调用本身或模型计算若长时间不返回，仍可能影响边界时效；状态不明时
保留检查点并报错，不把未确认委托假定为已撤销。

宿主先向所有策略发起停止，再等待各自回报。某个策略先达到等待期限，
但账户随后确认撤单和持仓均一致时，会在保存、断开TD之前处理迟到回报，
保存策略消费游标并完成解绑。真实回报冲突、撤单仍未确认或保存失败继续报错。
这项调整不迁移或清空已存在的异常检查点，启动命令和仓位参数无需改变。

### 同交易日漏回报恢复

启动时若本地委托仍未结束，但柜台完整订单查询显示已成交、已撤单或不再排队，
恢复链先核对原订单身份、方向、手数和累计成交量。累计成交量增加时，必须
通过 `ReqQryTrade` 获取真实成交编号、价格、数量及开平标志，以交易所OrderSysID
和本地OrderRef双重核对；不根据净持仓差额或委托限价补造成交。

已核验的缺失成交复用原成交回报链入账、更新归属及回报journal，再进行账户
资金、多空仓和活动委托核验。全部成功后才备份、提交检查点；缺少成交明细、
未知活动单、身份/数量不符或柜台持仓不一致时，原检查点保留，不发送新订单。
当前只支持柜台仍能查询到完整成交明细的同交易日恢复，不能推断上个交易日
已经无法查询的漏成交。

成交查询要求项目自有 `bomber-ctp>=0.1.2`，新增 `reqQryTrade` 原生接口。
同步代码后需重新编译安装CTP binding，并执行 `smoke_test_td.py`；Bomber引擎无需重编译。


## 缺价拒单后的检查点恢复

策略递增目标版本后，账户预算仍可能因报价失效而拒绝受理。此时策略文件的内部版本可能
比账户已受理版本高1，且`targets_pending=true`。恢复会先核验模型、信号配置、日历和手数身份，
再使用同文件保存的最近已受理副本，保留同向续持后的五分钟期限；旧文件无此副本时使用账户metadata
保存的同代批次。账户从未受理该策略目标时，还须确认该策略无归属仓位、
无在途数量，才恢复初始空批次。其他策略和外部持仓不受影响。

被拒绝的开仓信号不会重新下单，已受理批次的原到期时间保留，之后继续走正常到期处理。
已经生成这类文件的旧版本也可恢复，不需要删除检查点或手工调整版本号。恢复不会忽略模型/参数
变化，也不会猜测成交；缺少已受理批次、存在非待提交版本跳跃或账户对账失败仍会停止。
