# PF2026100915 同一策略停机重启只读探针

同一策略先保存退出，再次启动，不是同时运行多个策略进程。prepare/resume为两次独立Python命令，进程启动生成process_run_id，第二次报告separate_process_observed。新增只读入口和4项离线测试（history-adjustment共32项），公共策略/检查点实现未再改。本机仅源码/文档/静态SHA，无Python/测试/联网；待用户远端验证。

## 范围和步骤

使用现有Provider/因子工厂及已核验固定IM2610/20260922数据。prepare只读取末两根之前的238个预期分钟，当前13:00缺1故预热237根，断点14:57分钟末；指标、日期、窗口、来源审计及启动标识写入独立证据文件并退出。

resume新Python读取、校验文件摘要/参数，生产EmaCheckpoint恢复状态。数据库只补断点后、末根之前的14:58分钟，warm_closed只推进指标，不发布目标。该回补分钟必须真实存在，即使全窗口允许缺失也不能跳过本次衔接必需分钟。重新读取整日作连续对照，前缀必须与恢复回补状态精确一致，源修订或新补入数据改变序列时拒绝；末根14:59真实OHLC+因子经生产on_bar，仅内存捕获目标，重复末根不再推进。固定角色显式指定、不查询或修改生产main；源累计volume不映射，价格Bar volume=0。

这是同一策略的指标停机重启与历史记录接续验证。不会连接CTP、提交订单或恢复账户归属；不代表run_live完整生命周期、当前实时行情、全年度CFFEX日历或完整240分钟已通过。指标证据不能传run_live的state-file。现有交易权威对账与共同generation检查点仍另行必需。

## 远端执行

同步PF0915清单后，仅复测新增用例所在组，已通过其他组不重复：

```bash
sha256sum -c doc/evidence/checksums/PF2026100915_SHA256.txt &&
python -u -m tests.run_main_ema_acceptance --only history-adjustment
```

新增4项：237→238回补无目标→239新事件1次目标；allow不掩盖衔接分钟缺失；不同EMA配置在数据库查询前拒绝；原历史前缀修订拒绝。进程标识用例为mock，仅两次真实Python命令证明实际重启。

第一条执行结束后再执行第二条；同一终端保留IM_RESTART_ID：

```bash
IM_RESTART_ID="$(date +%s%N)-$$"
python -u -m scripts.integration.history.restart \
  --stage prepare --connect \
  --config demos/01_main_ema/history_im_20260922.json \
  --instrument IM2610.CFFEX --trading-day 2026-09-22 \
  --minutes 240 --fast 3 --slow 5 --timeout 15 \
  --history-missing-policy allow \
  --indicator-file "tests/results/im2610-restart-state-$IM_RESTART_ID.json" \
  --report "tests/results/im2610-restart-prepare-$IM_RESTART_ID.json"
```

第一条预期status=prepared、saved_ema bars_used237、revision0、目标0、报单0。保持原指标文件，下一条读取它：

```bash
python -u -m scripts.integration.history.restart \
  --stage resume --connect \
  --config demos/01_main_ema/history_im_20260922.json \
  --instrument IM2610.CFFEX --trading-day 2026-09-22 \
  --minutes 240 --fast 3 --slow 5 --timeout 15 \
  --history-missing-policy allow \
  --indicator-file "tests/results/im2610-restart-state-$IM_RESTART_ID.json" \
  --report "tests/results/im2610-restart-resume-$IM_RESTART_ID.json"
```

第二条预期status=passed、separate_process_observed=true，restored_ema237→after_backfill238/revision0→after_event239/revision1；continuous_ema_matches=true、historical_targets_replayed0、targets_captured1、captured_target -1、duplicate_new_event_ignored=true、订单0。完整窗口仍coverage_complete=false、缺1分钟；若源不修订，最终fast11235.005399922291/slow11235.989822638036。证据文件和报告独占创建，错误不覆盖已有证据；重试prepare使用新ID，不能借新文件名跳过失败原因。

## 远端验收证据（用户已反馈）

history-adjustment32项全部OK，测试0.937秒、模块exit_code0/1.206秒；集中目录tests/results/main-ema-acceptance-1791549411555222779，Python=/workspace/worker/env/uv-nautilus/bin/python，项目=/workspace/worker/pj/Chrono/nautilus_trader_demo/pro。本轮只登记实际结果，不要求重复本组。

prepare真实命令返回prepared，process_run_id=97bb3fba2659403194a04b63f590944d，指标文件tests/results/im2610-restart-state-1791549415478291510-86497.json；保存bars_used237、fast11239.458269089158、slow11239.002883635574、last_processed_ns1790060279999999999、revision0/last_target null、目标0/订单0、不接CTP。前缀required238/loaded237/missing1，缺口仍13:00。

该命令结束回到shell后另启resume，返回passed，新process_run_id=f4d12a5681fd4c5da763449ee6d77978，saved_process_run_id与prepare一致，separate_process_observed=true。restored_ema与保存完全一致。backfill_history required1/loaded1/missing0，断点后14:58数据库分钟仅预热：bars_used238、fast11236.78100784458、slow11237.36983795705、revision0/last_target null。最后14:59真实事件后bars_used239、fast11235.005399922291、slow11235.989822638036、revision1、last_target -1。

continuous_ema_matches=true、historical_targets_replayed0、duplicate_new_event_ignored=true、targets_captured1/captured_target -1，target_sink=in_memory_probe，orders_submitted0、ctp_connected=false、trading_state_written=false。整日仍required240/loaded239/missing1、coverage_complete=false。两阶段及回补因子来源20260921、1.456591、fingerprint54a335597a8cedcb218156af65964a178e936374872940b968329883fe4d4b65一致。

同一策略真实Python停机再启动的指标恢复、数据库断点回补、历史不重放目标和新分钟去重已通过。仅staged_indicator_only；生产run_live账户/订单同代恢复、当前实时分钟衔接、CFFEX日历/入口仍未由此验收，GAP04不整体关闭。主力映射偏差和13:00缺口按用户约定继续暂缓。本次只文档证据及其摘要，源码不变，无本机Python/测试/连接。
