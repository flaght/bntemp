# 当前SimNow交易总结与其他策略接手

用户要求总结本次实际交易，在新session继续其他策略。本文件为接手入口。本次是SimNow模拟柜台实际发单，不是生产真实资金账户；用户已授权模拟交易，后续仍按现有账户、健康与风控规则运行。此文档不创建新聊天、不连接柜台、不改交易代码。

## 1. 环境与工作规则

- 本地源码项目：/Users/kerry/work/bomber1/pro。
- 远端运行项目：/workspace/worker/pj/Chrono/nautilus_trader_demo/pro。
- 远端Python：/workspace/worker/env/uv-nautilus/bin/python，uv-nautilus。
- 本机只编辑源码/文档及校验摘要，不运行Python、测试、回测、编译或柜台连接。没有已核验远程访问，不猜SSH；由用户远端运行并反馈。
- 最新相关源码部署清单PF2026100916_SHA256.txt；只读TD输出补充PF2026100918_SHA256.txt；本轮PF2026100919_SHA256.txt仅交接文档，不覆盖源码版本。
- 已通过本轮离线组无需无故重复。公共源码变更先登记HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md，保持其他chat工作，不擅自覆盖或发送跨chat消息。

## 2. 本次启动前实际核验

| 项目 | 实际结果 |
| --- | --- |
| 柜台 | SimNow broker9999，账户脱敏***61 |
| TD前置 / MD前置 | tcp://182.254.243.31:30001 / tcp://182.254.243.31:30011 |
| TD实际交易日 | 20261012 |
| 启动前仓位/活动订单 | instruments0、active_orders0，资金CNY，账户/订单revision均1 |
| CU参考来源日 | 20261009，DolphinDB只读ready=true |
| 本次固定真实合约 | cu2611.SHFE |
| 累计因子 | 1.484671 |
| 跳价 / 乘数 | 10 / 5 |
| 参考快照fingerprint | 5c800a0f87874bb540bde9be7d72f8dc5a4048f3c695a60eb6d6eb14056dd02a |
| 原始行情 | 20261009夜盘21:29:44—21:29:53.500，北京时间；TradingDay20261012，ActionDay20261009，14 ticks，raw market data OK |

MD采样末bid110750/ask110760、限价区间99740—119470仅当时证据，不用于后续报单定价。每次新会话重新核验，不固定沿用旧日期、旧报价或旧合约。

原始CU历史分钟目前不在DolphinDB；本次history-minutes0，实时Tick聚合完整分钟，EMA3/5初始化后自然信号发单。角色/因子/条款参考仍来自DolphinDB，和原始历史分钟分开。未强制接管、不复用缺口旧检查点。

## 3. 本次运行命令与状态文件

以下是本次实际使用的参数记录，不是可直接重启/恢复的命令：

```bash
EMA_SIMNOW_STATE="demos/01_main_ema/results/cu-night-$(date +%s%N)-$$.json"
python -u -m demos.01_main_ema.run_live \
  --connect --mode simnow --simnow-environment realtime \
  --product CU --expected-instrument cu2611.SHFE \
  --reference-source dolphindb \
  --factor-date-basis source --factor-availability observed-on-read \
  --expected-trading-day 20261012 --expected-source-day 20261009 \
  --history-minutes 0 --fast 3 --slow 5 --quantity 1 \
  --seconds 600 --max-notional 600000 --max-session-orders 20 \
  --enable-orders --confirm-simnow --state-file "$EMA_SIMNOW_STATE"
```

本次实际持久化文件：

```text
/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/demos/01_main_ema/results/cu-night-1791552662794605638-86497.json
```

该文件位于远端磁盘，不自动同步Mac。保留文件，不修改内部数量来分配仓位。用户没有提供本次最终summary.json路径、完整停机结果或停机后柜台查询，不补造运行整体passed、总报单数、盈亏或最终账户为空。

## 4. 已确认的实际成交链路

用户提供片段包含三笔ACCEPTED→FILLED，均归属main-ema-simnow/cu_main/真实cu2611.SHFE：

| 订单尾号 | 目标/行为 | 成交 | 按片段推导成交后的策略净仓 |
| --- | --- | --- | --- |
| 20261012-2 | SHORT -1，SELL OPEN1 | 110600 | -1 |
| 20261012-3 | 目标改LONG +1，BUY CLOSE_TODAY1 | 110660 | 0 |
| 20261012-4 | retained_target_retry=true，保留LONG目标在下一Bar继续BUY OPEN1 | 110640 | +1 |

第三笔使用revision2的既有目标，并非策略重复发布新目标；先平仓后下一Bar继续开仓的固定目标执行得到真实证据。委托中allocation_token/legs及position_attribution把真实成交关联到策略归属，订单/成交回报含venue_order_id/trade_id作为关联和去重依据。此片段没有完整后续成交，不能据此认定最后仍多1。

用户随后直接读取状态文件，generation14：

```json
{
  "strategy_positions": [
    {"quantity": "0", "strategy_id": "main-ema-simnow", "target_key": "cu_main"}
  ],
  "attribution_owned": [
    {"client_id": "main-ema-simnow", "instrument_id": "cu2611.SHFE",
     "quantity": "0", "strategy_id": "main-ema-simnow", "target_key": "cu_main"}
  ]
}
```

这里attribution_owned是便于说明的显示名，实际路径是payload.positions.attribution.owned。该快照逻辑和真实合约归属均0；说明保存时策略已经空仓，后续平仓过程未在已贴片段中展示。不能用这份快照替代当前柜台权威查询，也不能凭generation14猜成交次数。

## 5. 策略持仓如何保存与恢复

运行时StrategyTemplate.position(target_key)读取PositionManager策略持仓。状态文件payload.positions.strategy_positions保存strategy_id+target_key+已成交有符号quantity；payload.positions.attribution.owned保存client_id+instrument_id+strategy_id+target_key+已成交quantity。正数多仓、负数空仓、0为空，数量不是目标或挂单量；零记录保留归属键关系。client_id是执行客户端标识，不是柜台账号，和strategy_id本例恰同名。

payload.targets保存期望目标，payload.orders/ctp_drivers/ctp_ledgers保存幂等订单/关联/今昨仓账本。RuntimeStateManager在报单前和回报处理后自动保存，JsonStateStore原子写入、摘要/损坏检测及generation CAS防并发覆盖。generation是状态保存版本号，不是订单或成交数量。

柜台只知道账户持仓，不知道策略归属；恢复时从本地文件还原归属，再独立查询柜台仓位、资金及活动订单。未知或不一致闭闸，不仅凭缓存认为有仓/无仓，不把手工/其他策略仓自动分给当前策略。持久化和权威核验都是必需条件。

重要限制：本次是有限时长、history-minutes0、非run-forever的会话，源码只在continuous或history_enabled时安装EmaCheckpoint组件。因此本次保存目标/仓位/订单，不保证component_states中有完整EMA指标。原命令遇已有state-file会拒绝覆盖；正式resume目前走CU持续模式，EMA旧状态迁移/缺口回补仍需完整证据。不要把“持仓归属可持久化”当作当前命令可自动完整续跑。CU数据库历史缺失时，不能跳过已保存EMA的停机缺口或用当前价补造。

查看本次快照（新终端不要依赖旧shell变量）：

```bash
python - <<'PY'
import json
from pathlib import Path
p = Path("demos/01_main_ema/results/cu-night-1791552662794605638-86497.json")
s = json.loads(p.read_text())
print("generation:", s["generation"])
print(json.dumps(s["payload"]["positions"]["strategy_positions"], ensure_ascii=False, indent=2))
print(json.dumps(s["payload"]["positions"]["attribution"]["owned"], ensure_ascii=False, indent=2))
print("component_states:", list(s["payload"].get("component_states", {})))
PY
```

此命令仅读文件，不连接柜台、不用于替代摘要校验后的正式恢复。空shell变量转Path("")会成为目录`.`，此前IsADirectoryError是变量丢失，不是状态文件丢失。

## 6. 新session继续其他策略的步骤

1. 先读本交接和目标策略的demo/说明；用户尚未指定下一策略，不能默认把EMA换品种当成其他策略，也不要擅自创建另一聊天或并发实例。
2. 最新柜台只读核对（必要，本次最终柜台状态未知）：

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

3. 保存本次状态及报告证据，确认旧运行是否已退出、账户互斥锁释放；状态文件0不能代替停机事实。若有持仓/活动订单，确认其已有归属和来源，不强行接管/平仓，不通过删除状态文件绕过。
4. 为下一策略确认真实合约、当前TD/MD、参考来源/因子与条款、Tick健康、跳价/乘数和适配的风控预算；不要将CU跳价10/乘数5或旧限价复用到其他品种。
5. 下一策略使用独立strategy_id、逻辑target_key和状态/报告路径，显式声明DataBinding、ExecutionRoute及需要的RoleGuard；复用执行客户端、共享PositionManager和RuntimeStateManager关联，避免串投。现01 demo固定main-ema-simnow标识不能作为多个策略实例的共享身份。
6. 本demo单策略/固定角色会话和全账户空仓预检，不等于共享账户并发多策略已上线验收。用户本轮意图是继续其他策略，先按顺序单策略验收；若另要求同时运行，重新设计共享账户组合/互斥和恢复边界，不仅改锁命名绕过。
7. 先完成该策略装配/执行/回报/持久化的必要离线检查，真实Recording核对信号和路由，然后既有SimNow显式授权与受控数量下单；用户此前已授权模拟交易，不重复无理由要求授权确认。核验最终summary的status、orders_submitted、回调、策略/账户/未归属、活动订单、cleanup_errors及状态路径，必要时停机后柜台只读核对。

可复用组件：bomber/framework/trader/template.py（on_order兼容on_order_update、on_fill、position）、assembly.py、execution/builders.py、runtime/managed.py、runtime/ctp.py、persistence.py、execution/attribution.py。示范demos/01_main_ema/run_live.py/strategy.py。优先复用框架，避免每个策略重写订单关联/归属/柜台查询和恢复。

## 7. 独立暂存事项

DolphinDB只有CFFEX历史分钟、无CU历史。IM/IF没有夜盘，用户决定周一20261012新session继续IM在线Recording：见[周一DolphinDB交接](../history/HANDOFF_DOLPHINDB_INTRADAY_MONDAY_2026-10-12.md)。真实239分钟allow预热、指标停机重启和正式IM离线已过；13:00缺口及main/因子偏差暂缓。当前IM/CFFEX正式入口仅Recording，不接受SimNow发单/持续恢复，IF入口未完成；其他策略夜盘交易不能把这项说成已验收。GAP04仍开放；GAP01原先首次成交/归属既定标准已关闭，本轮真实反手片段为增量证据，不扩大为共享账户多策略/生产恢复通过。

## 8. 可直接粘贴到新session的开始说明

> 请先读取 /Users/kerry/work/bomber1/pro/doc/HANDOFF/live/HANDOFF_SIMNOW_CU_NIGHT_AND_NEXT_STRATEGY_2026-10-09.md。继续其他策略的SimNow实际交易验收。此前CU EMA实际开空→平空→保留LONG目标再开多已取得回报证据；最新本地状态generation14、策略/真实归属均0，但未提供停机后柜台核验和完整summary，不能默认当前账户空仓或旧进程已退出。先核对当前柜台和目标策略，复用统一装配、执行、持仓归属及持久化，不复用EMA身份/状态，不强制接管。原始CU历史数据库缺失，新会话可实时预热；旧策略恢复不能绕过缺口。本机只源码/文档，测试与柜台运行由用户远端uv-nautilus执行。DolphinDB IM回补另按周一交接继续，暂缓缺口/角色偏差，不重复已通过组。
