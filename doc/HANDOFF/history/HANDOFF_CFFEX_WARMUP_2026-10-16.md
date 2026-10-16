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
缺原生Bar，须远程复验；未连接真实DDB或柜台，不登记M06在线完成。

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

01现有CTP Recording仍会登录TD做会话检查，off不接管总仓；无报单授权，不发送物理订单。

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
