# PF2026100914 固定IM指标文件恢复与首根新事件验证

用户已取得IM2610/20260922真实239根复权预热证据，授权下一步。新增scripts.integration.history.resume，只读使用现有历史及参考工厂；不改公共历史服务、策略或检查点实现，不处理暂缓的主力映射偏差。本机只源码、文档和SHA检查，未执行Python/测试/连接。新增5项离线测试，history-adjustment共28项，远端待验。

## 首轮远端失败及检查点修正

用户真实探针在恢复后状态严格比较失败，尚未处理新事件，无目标/订单，不登记通过。原AnchoredEma构造将保存值喂入ExponentialMovingAverage.update_raw；本地Bomber引擎源码显示首笔先赋值，再执行alpha*x+(1-alpha)*value，可能改变末位double。此前连续恢复用例仅比较后续递推，未比较恢复瞬间完整状态。本次修正正式demos/01_main_ema/checkpoint.py：直接保留保存double，后续使用引擎相同alpha、运算顺序及计数；count0时首次真实行情仍按引擎首笔规则处理。未放宽状态严格比较，未改文件格式或历史缺口规则。探针失败现输出具体字段差异；报告restored_ema_before_event取实际恢复状态。

新增2项continuous用例，共20项：保存double精确往返和count0恢复后的首笔/后续递推与引擎一致。检查点修改涉及正式恢复，因此复测history-adjustment、continuous和entry；远端未验证，运行引擎差异仍以实际测试/字段差异为准。先前“不改检查点实现”的描述仅指初始PF0914版本，本次增补以此段为准。旧失败指标文件保留证据，重试使用新路径。无本机Python/测试/连接。

## 验证路径及边界

读取指定交易日240分钟窗口，按显式fail/allow处理缺口。必须有末根14:59完整真实OHLC及因子，不能以更早一分钟冒充末根；前缀至少满足slow初始化。真实累计成交量未映射，生成策略价格Bar时volume=0，仅验证EMA价格路径。

将末根留作新事件，其余实际分钟warm_closed，只推进指标。保存独立JSON文件（purpose=readonly_indicator_probe、version=1、EMA和SHA256及历史审计），重新读取、校验摘要、调用生产EmaCheckpoint恢复到新策略实例；恢复后的完整指标状态必须等于保存状态。文件仅作为本探针证据，不是可供run_live恢复的账户/订单同代交易检查点。已有文件或报告不覆盖。

旧首分钟及断点分钟各输入一次，要求指标、版本、目标不变。末根真实Bar进入生产MainEmaStrategy.on_bar，显式固定合约与已查询因子作为探针角色，要求bars_used只加1、目标revision1、只捕获1个目标。再输入相同末根，指标和目标均不重复。最终fast/slow与同数据不间断warm_closed递推比较（rel_tol1e-12、abs_tol1e-9）。目标只进入内存探针，没有订单执行器、柜台或交易回调。

此验证范围是JSON文件持久化→新实例恢复→已记录真实末根作为新事件。不是实际进程重启、当前实时行情衔接或柜台仓位归属恢复，未核验完整生产生命周期；也不宣称生产主力角色正确。后续另验这些范围。

## 远端离线检查

同步PF0914清单文件，旧PF0913摘要不覆盖新增改动：

```bash
sha256sum -c doc/evidence/checksums/PF2026100914_SHA256.txt &&
python -u -m tests.run_main_ema_acceptance --only history-adjustment
```

5项新增覆盖：文件恢复与连续指标一致/新旧事件去重；allow真实缺口不填行；缺少末根即使allow也拒绝且不写文件；已有证据拒绝且不查询；指标文件摘要拒绝篡改。当前未收到28项远端结果。

## 真实数据库恢复探针

同一天、同一配置，缺口暂缓，显式allow：

```bash
IM_RESUME_ID="$(date +%s%N)-$$"
python -u -m scripts.integration.history.resume \
  --connect --config demos/01_main_ema/history_im_20260922.json \
  --instrument IM2610.CFFEX --trading-day 2026-09-22 \
  --minutes 240 --fast 3 --slow 5 --timeout 15 \
  --history-missing-policy allow \
  --indicator-file "tests/results/im2610-indicator-$IM_RESUME_ID.json" \
  --report "tests/results/im2610-resume-$IM_RESUME_ID.json"
```

若真实返回仍为239根：history.required240/loaded239/missing1，coverage_complete=false；saved_ema和restored_ema_before_event bars_used238、revision0、last_target null；after_event bars_used239、revision1，new_event_ns1790060399999999999，targets_captured1、historical_targets_replayed0、old_events_ignored2、duplicate_new_event_ignored=true、continuous_ema_matches=true、orders_submitted0、ctp_connected=false、trading_state_written=false。status=passed仅指上述恢复/衔接断言，coverage_complete=false仍明确保留缺口。

如源数据未修订，最终EMA应与用户此前239根预热一致：fast11235.005399922291、slow11235.989822638036，本次捕获SHORT -1目标。指标文件应保留以供复核，不能传给run_live的state-file。运行源码没有改变，不要求重复此前CTP/账户验收。

## 修正后远端真实通过证据

用户反馈status=passed、restart_scope=new_instance_from_json_file。指标文件tests/results/im2610-indicator-1791540272775620835-86497.json，indicator_sha256=1af52e49cb59bda9ce1a3ec7f7fc40195b00154ffd2048b826db0b0963ee921b。未贴报告文件实际路径，不补造。

saved_ema和restored_ema_before_event完全一致：bars_used238、fast11236.78100784458、slow11237.36983795705、last_processed_ns1790060339999999999、last_target null、revision0。末根new_event_ns1790060399999999999后bars_used239、fast11235.005399922291、slow11235.989822638036，与continuous_ema指标相同；after_event last_target -1、revision1，连续对照仅历史预热故仍last_target null/revision0，这一区别符合预期。

continuous_ema_matches=true、historical_targets_replayed0、old_events_ignored2、duplicate_new_event_ignored=true、targets_captured1/captured_target -1、target_sink=in_memory_probe、订单0、回调0、ctp_connected=false、trading_state_written=false。history仍required240/loaded239/missing1，coverage_complete=false，缺口1790053259999999999；因子来源20260921、1.456591及fingerprint54a335597a8cedcb218156af65964a178e936374872940b968329883fe4d4b65与此前预热一致。

文件精确恢复与真实记录末根衔接已通过，支持本轮恢复修正；不扩大为完整240覆盖、实际进程重启、在线行情衔接或CFFEX报单通过。history-adjustment28/continuous20/entry的新版本离线输出未在此反馈中提供，继续待登记。本轮只记录文档及其摘要，源码不变，无本机Python/测试/联网。

后续用户提供修正版离线末段：entry共50项、1.752秒、OK，模块exit_code0、3.018秒；集中入口最终“全部选定离线模块通过”。该段未显示history-adjustment/continuous逐组输出或实际选定模块列表，不补造两组条数、耗时和报告路径。记录entry明确通过与集中入口汇总结论，已通过entry无需重复。下一阶段实际进程重启与分钟接续尚未实施/验证；源码不变，本机无执行。
