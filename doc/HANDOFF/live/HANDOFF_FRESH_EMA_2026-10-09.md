# PF2026100908 撤回固定目标，恢复全新EMA空仓验收

## 最新远程回归结果

**当前最终结果：EMA真实首次开仓通过，GAP-01既定关闭标准完成。** 用户提供报告simnow-1791524243708841796及状态gap01-fresh-cu-1791524242765065117-86497.json。fresh_flat、无接管、signal_source=ema，5根Bar预热后真实SHORT -1，SELL OPEN一手，ACCEPTED后FILLED，成交110090.0；metadata包括策略/目标/真实合约归属、allocation_token和冻结分配，报单前net_position=0。订单回调2、成交回调1，last_order_position/last_fill_position均-1，策略/账户-1、未归属0。最终passed、权威多0空1、活动订单0、清理无错，达到单笔上限正常结束。

当前空1是这次EMA开仓留下的新仓，旧空1已经独立平仓清除；停机不自动平新仓，也未获得对该新仓的独立平仓授权。此为全新真实EMA首次开仓，不是固定信号、不包含在线完整反手/共享多策略/重启实测。结合原离线验收，GAP-01既定标准完成；不重复已过组、不继续发单。本轮仅更新文档及清单，无源码或本机Python执行。下面段落为历史步骤的当时状态，以当前结论为准。

独立平仓已完成：用户随后提供BUY CLOSE_TODAY一手ACCEPTED→FILLED回报，成交价110150.0，TD20261009；工具结果passed、orders_submitted=1、final_gross={}、final_active_orders=0、cleanup_errors=[]。平仓委托限价110160，实际成交110150；账户旧cu2611空1已清除，不再需要切换账户。此为独立平仓工具成交，不能登记为EMA策略成交。工具结束已做权威空仓/活动订单查询，无需为同一结果重复只读；下一步直接运行下方fresh空仓EMA命令，入口仍重新预检账户。EMA初始0、实际首次成交/回调及最终归属仍待验收，GAP-01未关闭。本轮仅记录用户远程结果，未修改运行源码或本机执行。

随后用户只读反馈：TD30001、broker9999、账户尾61，净仓cu2611.SHFE=-1，活动订单0。助手询问是否授权仅买入平掉这一手、不新开仓，用户明确回复“好 请开始下一步”。记录本次独立SimNow平空1授权，不扩大到开仓或真实资金。已有close_existing工具只发一笔reduce_only平仓，重新查询今昨/总仓/资金/活动订单；实际限价需要本次MD盘口，不能沿用EMA复权价或旧报价。目前尚无实际平仓提交/成交证据；先远程无单平仓预检和cu2611行情采样，取得当次TD、盘口及涨跌停后执行该独立工具。平仓通过且权威空仓确认后才继续EMA新策略验收。

用户在PF0908同步及historical/assembly/entry复测之后确认“全部更新通过”。按此确认记录本轮三组通过；提供的明细为entry50项全部OK（测试1.687秒、模块退出0/2.788秒）。不补造historical/assembly耗时或报告目录，不扩大为此前未确认的controlled/P3g结果。没有新增源码修改，不重复已过组。下一步为本次空仓SimNow账户只读核验及真实EMA首次开仓/回调/最终权威仓位验收；旧账户空1未处理，当前实际成交仍待验证。

2026-10-09，用户明确接受空仓SimNow账户建议，并要求调整acceptance_strategy.py及所有相关代码。修改前已在当前对话通知。仅撤回本任务PF0907相关代码，不修改公共框架；本机仅源码/文档/摘要静态核对，未运行Python、测试、编译或柜台连接。

## 原因和启动语义

PositionManager的策略仓位默认0。之前命令包含adopt-existing-position cu2611.SHFE及expected-position -1；CtpSessionLifecycle.start调用runner.adopt_initial_position，归属账本明确把柜台空1归给新EMA。因此成交回调0但策略仓位-1，是显式接管的结果。该启动模式不代表全新策略从0开始。真实EMA目标SHORT -1与接管持仓一致时无单正确。

当前选择：使用经权威查询确认空仓且无活动订单的SimNow账户、新状态文件，不传接管参数。EMA从0开始，预热后按真实快慢线产生+1或-1开仓需求，成交后才记入策略仓位。当前已知旧账户空1不自动平、不自动归给EMA；用户需使用空仓账户。若需要处理旧仓，属于独立、明确授权的仓位操作，不在本次源码回撤中代为执行。

## 调整范围

- 删除acceptance_strategy.py及入口导入、固定目标CLI参数、组装分支、方向选择钩子与固定反手验收逻辑。
- MainEmaStrategy恢复原快慢线方向选择。保留signal_source=ema、on_order/on_fill及归属观察。
- 保留01入口目标/EMA诊断及无单时“成交验收未触发”的错误，不把零成交标成passed。保留旧显式接管功能，仅供确实接管旧仓的场景；当前空仓验收命令不使用。
- 保留PF0906资金心跳/目标并发修复、受控授权/风控和持久化。公共CTP生命周期、Runner、账本本轮不改。
- 删除固定目标测试，新增上升/下降行情下全新EMA初始0、预热后自然多/空开仓、报单不改实仓、首次成交及重复去重测试；增加旧固定目标参数被拒绝的回归。保留显式接管SHORT与旧空1一致而无单的对照测试。
- 固定目标交接已标明撤回，README/GAP01/协调记录切换当前流程。PF0907旧摘要是历史记录，不用于部署。

## 同步、删除及远程复验

按PF2026100908_SHA256.txt同步文件，并同步删除清单。远程pro根目录删除本次已经撤回的一个文件：

```bash
rm -f demos/01_main_ema/acceptance_strategy.py
sha256sum -c doc/evidence/checksums/PF2026100908_SHA256.txt
test ! -e demos/01_main_ema/acceptance_strategy.py
python -u -m tests.run_main_ema_acceptance --only historical --only assembly --only entry
```

新版P3g若尚未通过/未确认，补执行 `python -u -m tests.run_main_ema_acceptance --only controlled`；PF0906客户端源码本轮未改，已有明确新版通过证据则无需重复。本机没有运行这些组，远程回归待结果。旧PF0907若尚未部署，也需同步本轮当前文件，避免以后混用。

## 空仓账户的当前柜台验收

先配置此次空仓SimNow账户的凭据，用只读探针确认无仓、无活动订单：

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

若返回cu2611空1，说明仍是原非空账户，不能直接当作全新策略账户；不通过删除接管参数绕过预检，不自动平仓。入口也会再次完整核验空仓。

离线组通过且只读确认空仓后，在连续交易窗口执行真实EMA一手首次开仓验收：

```bash
python -u -m demos.01_main_ema.run_live \
  --connect --mode simnow --simnow-environment realtime \
  --product CU --seconds 600 \
  --reference-source dolphindb --reference-database dfs://bomber_daily \
  --factor-date-basis source --factor-availability observed-on-read \
  --expected-trading-day 20261009 --expected-source-day 20261008 \
  --quantity 1 --max-notional 600000 --max-session-orders 1 \
  --enable-orders --confirm-simnow \
  --state-file "demos/01_main_ema/results/gap01-fresh-cu-$(date +%s%N)-$$.json"
```

限定一手、最多一笔SimNow订单，既有订单授权和预检继续有效。可能按真实EMA买开或卖开，停机撤单并报告剩余仓位，不自动平已成交的一手。一次有效成交才能验证本次回调/归属；报单被拒或未成交不算passed。核对订单状态先于成交、首次回调归属从0变为方向一手、账户与策略一致、未归属0、最终权威总仓和活动订单0/清理无错。首次开仓证明全新策略链路，不冒充原完整反手验收。GAP-01仍待本次远程和柜台结果。
