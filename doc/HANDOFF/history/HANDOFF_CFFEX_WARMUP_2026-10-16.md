# 04预热交付及01/03 IM优先验收

2026-10-16，Asia/Shanghai。用户确认SHFE数据缺失，本轮先用CFFEX验证。
本次不修复上游数据，不把SHFE的allow运行登记为完整覆盖通过。

## 交付与同步

04原MainCrossSectionMomentumStrategy新增warm_closed，只更新同分钟复权窗口、时间及帧数；
历史无目标/订单，归属和版本不被覆盖。默认lookback=20请求21个价格点，
之后实时决策遵守原rebalance_interval。历史采用每品种会话与共同交易分钟，
按实际合约自动读取历史累计因子；来源日冲突、价格/字段错误、查询不可用均失败。
缺行情有显式fail/allow及逐品种覆盖审计。缓冲、去重、回补、15秒进度已接入。
独立及共享生命周期均调用预热；恢复重新预热而不重放交易。

03参考适配原先不允许CFFEX，新增Recording模式显式放行IM/IF，准备history_im.json。
04的Recording同样放行IM/IF，并使用仅MD的Recording生命周期，避免受物理账户总仓阻塞。
CTP符号转换对IM/IF保持大写；PortfolioRoleReferences/CalendarReferences默认商品市场限制保留，
只有Recording装配显式扩展allowed_venues。没有宣称CFFEX真实执行通过。

同步当前修改及新增文件，关键路径：

- bomber/framework/dataprep/live_portfolio.py、bomber/framework/trader/runtime/live/shared.py。
- demos/04_cross_section/{strategy,live_runtime,run_live,history,progress}.py、history_cffex.json、README.md。
- demos/03_calendar_spread/live_references.py、run_live.py、history_im.json。
- scripts/integration/shared_account/factories.py。
- tests/test_cross_section_history.py、test_cross_section_live.py、test_calendar_spread_live.py、
  test_shared_account.py、run_main_ema_acceptance.py。

已跑的本机无网络组：cross-history、shared-account、calendar-history、history-session-factors、
cross-section-signal，合计119项通过。另以实际生命周期基类和源码中的Recording类验证MD构造不创建TD。完整cross-live本机导入失败，
缺原生Bar；该本机验证不包含真实DDB或柜台连接。后续远程结果见下节。

## 远程验收进展（2026-10-16）

用户回传cross-live离线80项全部通过。之后在线Recording曾在轮询时因访问
RecordingExecutionClient不存在的report_errors失败；公共生命周期已改为Recording
检查is_started、Model保留report_errors检查，并新增轮询回归。
该修复本机execution-composition/live-common/live-channels/live-recovery共120项通过。

用户回传04 IM/IF在线Recording最终报告status=passed，记录目录：
`demos/04_cross_section/results/IM_IF_ddb_recording/cross-section-recording-1792116339492511168`。
报告history_frames=22、live_frames=9，最新目标IM2610.CFFEX=-1、IF2610.CFFEX=1；
行情/Recording通道ready=true，observed_md_trading_day=20261016，cleanup_errors为空。
历史帧数包含运行期回补；该摘要未列逐品种历史覆盖审计，不据此另行推断missing数量。
Recording的成交数和持仓为零、account_positions=None符合模式约定；本次验证了
历史预热、实时接续和目标记录，未验证物理订单执行。下一步依次验01 IM、03 IM双腿。

01 IM第一次远程运行已处理250根Bar并生成目标1，但TD资金查询超时，
停机查询委托又返回-2，最终failed；该次不登记完整在线通过。
后续01 Recording切为独立Recording客户端和仅MD生命周期，保留完整历史/因子审计和目标验收，
不创建TD或占用物理账户锁，报告真实账户及TD观测为空。SimNow订单模式保留原TD流程。
本机公共生命周期及历史因子相关129项通过；01实际Recording生命周期源码配合公共基类完成
仅MD凭据构造、poll、verify、shutdown及停机后实际行情日保留验证。
原生entry/im-runtime/continuous仍须在服务器复验，再执行同一条01 IM在线命令。

随后用户确认这三组选定离线模块全部通过（entry55项）。01仅MD在线复验在
10:43启动时缺10:42:59.999999999这一根分钟；用户DDB截图证明完成标签10:43:00
对应行updateTime=10:43:05.079，属于最新分钟落库延迟。01新增启动尾分钟有限重试：
只在recovery/fail、指标尚未预热且最新分钟距启动不超过30秒、成功查询仅缺尾部一根时生效；
固定窗口，每2秒重查，等待预算20秒，单次读取仍受原查询超时限制。
过程中刷新参考并核对健康，不提前推进EMA或开闸；旧缺口/源不可用/数据冲突立即失败。
补齐后完整推进一次；仍缺失或健康失效则失败。回归覆盖延迟补齐、固定窗口、到期失败、
旧缺口、较旧尾分钟、等待中行情失效及数据库不可用。

用户随后回传修复后01 IM在线Recording最终status=passed，目录为
`demos/01_main_ema/results/IM_ddb_recording/main-ema-recording-1792118949894298711`。
初始历史required=240、loaded=240、missing=0，运行期间另有1根DDB回补且missing=0；
bars_used=250、last_target=1、fast_ema=9828.769185659849、slow_ema=9828.52357415313。
历史跨交易日分别采用2026-10-14与2026-10-15来源日因子，均为1.456591；
此因子证据仅用于当前在线指标预热，不代表历史版本as-of重放。
行情/Recording通道ready=true，observed_md_trading_day=20261016，observed_td_trading_day=None，
account_position=None、orders_submitted=0、cleanup_errors为空，符合仅MD Recording约定。
因此01本次历史预热、实时接续和目标记录通过；物理报单未验。下一步验03 IM双腿。

03随后按main+secondary组合完成在线Recording，用户回传最终status=passed，目录：
`demos/03_calendar_spread/results/IM_ddb_recording/calendar-spread-recording-1792119928435472683`。
实际固定双腿IM2612.CFFEX/IM2703.CFFEX；来源日2026-10-15的second与far均为IM2703，
因此本次明确改用main+secondary，不是原默认secondary+far组合的验收。
双腿初始历史均required=120、loaded=120、missing=0，另回补完整双腿1帧；
最终history_frames=121、live_frames=9、complete_frames=130，两腿各9根实时Bar，
pending_frames为空、expired_frame_count=0、session_readiness.ready=true、cleanup_errors为空。
原始合约价差未使用复权因子。last_z=0.2369050077061904、direction=0、submissions=0，
本次日志未触发开仓，因此通过范围为完整历史覆盖、双腿实时同步及信号计算，
未验证03新目标下发或物理订单。此时01和04已另有在线目标记录证据；08无需滚动历史预热。

## 远程离线复验

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
export PYTHONPATH=.
python -u -m tests.run_main_ema_acceptance \
  --only cross-history --only cross-live --only calendar-live --only shared-account
```

## Recording命令

以下沿用此前已核对TD=20261016、资料日=20261015及明确无例外的局部范围。
换会话须核对日期；IM及IF的时段与历史覆盖由实际数据库验证。
先完成离线复验，然后逐个运行。共用原环境配置；同账户的旧进程先正常停止。

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
export PYTHONPATH=.
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true
```

### 01 IM：DDB自动复权预热，CTP实时接续

```bash
python -u -m demos.01_main_ema.run_live \
  --connect --mode recording --product IM \
  --expected-instrument IM2610.CFFEX \
  --market-source ctp --simnow-environment realtime \
  --account-reconciliation off --strategy-position-source fills \
  --reference-source dolphindb --reference-database dfs://bomber_daily \
  --factor-date-basis source --factor-availability observed-on-read \
  --expected-trading-day 20261016 --expected-source-day 20261015 \
  --calendar-id china.cffex \
  --trading-hours-database dfs://bomber_static --trading-hours-table fut_trading_session \
  --no-session-overrides \
  --history-db dolphindb --history-config demos/01_main_ema/history_im_raw.json \
  --history-minutes 240 --history-stage recovery --history-missing-policy fail \
  --fast 3 --slow 5 --quantity 1 --max-notional 3000000 \
  --ctp-future-buffer-ms 100 --seconds 600 \
  --report-dir demos/01_main_ema/results/IM_ddb_recording
```

01 CTP Recording仅登录MD，不创建TD、不查询资金或委托；使用显式参考交易日并核对实际行情日。

### 03 IM：双腿原始价差预热

```bash
python -u -m demos.03_calendar_spread.run_live \
  --connect --mode recording --product IM \
  --market-source ctp --simnow-environment realtime --account-reconciliation off \
  --expected-trading-day 20261016 --expected-source-day 20261015 \
  --reference-database dfs://bomber_daily \
  --leg1-role secondary --leg2-role far --quantity 1 \
  --lookback 120 --entry-z 2 --exit-z 0.5 --rebalance-interval 5 --max-notional 3000000 \
  --calendar-id china.cffex \
  --trading-hours-database dfs://bomber_static --trading-hours-table fut_trading_session \
  --no-session-overrides \
  --history-db dolphindb --history-config demos/03_calendar_spread/history_im.json \
  --history-minutes 120 --history-stage recovery --history-missing-policy fail \
  --ctp-future-buffer-ms 100 --seconds 600 \
  --report-dir demos/03_calendar_spread/results/IM_ddb_recording
```

03实际两个IM合约由角色服务选择，不能以主力有数据代替两腿都有数据；Recording不接TD。

### 04 IM,IF：完整跨品种同步复权窗口

```bash
python -u -m demos.04_cross_section.run_live \
  --connect --mode recording --products IM,IF \
  --market-source ctp --simnow-environment realtime --account-reconciliation off \
  --expected-trading-day 20261016 --expected-source-day 20261015 \
  --reference-database dfs://bomber_daily --factor-date-basis source --factor-availability observed-on-read \
  --lookback 20 --rebalance-interval 5 --group-fraction 0.5 \
  --target-notional 3000000 --target-quantity-cap 1 --max-quantity 1 --max-notional 3000000 \
  --calendar-id china.cffex \
  --trading-hours-database dfs://bomber_static --trading-hours-table fut_trading_session \
  --no-session-overrides \
  --history-db dolphindb --history-config demos/04_cross_section/history_cffex.json \
  --history-minutes 21 --history-stage recovery --history-missing-policy fail \
  --ctp-future-buffer-ms 100 --seconds 600 \
  --report-dir demos/04_cross_section/results/IM_IF_ddb_recording
```

04至少需要两个品种，IM单品种不能验横截面。IM与IF实际主力、因子及分钟覆盖须由报告核实。
上述额度仅是Recording配置，不代表实际价格或账户资金已验证。所有命令不发物理订单。
报告应区分history_frames与live_frames，保留真实sources及缺口，最终status及目标链另行登记。
