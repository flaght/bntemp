# PF2026100913固定IM历史服务与240分钟EMA预热

用户授权继续下一步；主力/因子映射偏差按用户要求暂缓，不作为固定测试前置。源码已完成、待远程测试和真实240分钟验证。本机未执行Python/测试、未连接库或柜台，GAP04继续开放。

## 实施范围

- HistoryService增加可选price_adjuster，在文件/数据库/实时缓冲合并去重后处理价格；不能增删/改写分钟身份、顺序。未配置时行为保持原样；缺口严格拒绝仍生效。
- ReferenceFactorAdjuster通过ReferenceSourceFactory读取指定日期、合约的唯一pcr_cumfactor，原始close乘累计因子得到adjusted_close；不读取fut_contract、不覆盖main、不重复累乘pcr_factor。因子处理与行情后端分离，策略不查数据库。
- 配置factor_source_days显式逐日期声明来源：2026-09-22→2026-09-21。未声明日期、合约/品种/日期不匹配、缺少/重复/无效因子都失败，不能跨日沿用或填1；allow缺行情也不豁免因子错误。每次批次按日期/合约读一次因子，本批次内复用，不跨请求缓存未确认版本。
- 原始输入保持不可变；已有adjusted_close的文件或真实缓冲不重复乘因子。全部因子、价格与可用时间验证完成后才推进EMA，错误不会部分预热指标。
- ImDayWindow仅枚举用户指定交易日的IM完整分钟，按[中金所IM细则](https://www.cffex.com.cn/cn/ssxz/20220718/43093.html)09:30—11:30、13:00—15:00共240分钟，排除午休。它不是全年日历，不自动判断节假日，也不支持跨日窗口；真实休市日无数据时严格失败。
- 新只读warmup入口复用MainEmaStrategy/EmaCheckpoint/warm_history，历史只推进EMA值、计数和最后处理时间。输出覆盖、因子审计、指标状态、revision=0、目标未发布、回调计数0；不连接CTP、不保存交易检查点、不把历史价格交给交易撮合。

## 时间/版本边界

因子来源必须严格早于对应分钟的北京时间日期。无available_ns时显式采用source-day-end假设；若源有实际available_ns，则不能早于该发布时间消费。报告保留source、fingerprint、来源日和availability_policy。读取的是当前表中所指定历史日期的记录，不宣称已有历史修订as-of版本；fingerprint不是历史发布版本。后续自动历史日期选择、跨日/跨月连续序列及主力映射统一修改另行处理，本轮不让这些偏差阻塞显式固定测试。

实际CFFEX在线Runner的Venue/全年日历仍待接入。此预热脚本不等于股指run_live已可发单；真实分钟加载期间Tick缓冲与柜台权威对账仍需后续Recording/在线验证。

## 远端离线验收

同步新清单所列文件；旧PF0911/PF0912摘要不要用于覆盖本次部署：

```bash
sha256sum -c doc/evidence/checksums/PF2026100913_SHA256.txt &&
python -u -m tests.run_main_ema_acceptance \
  --only history-adjustment --only history-provider --only continuous --only entry
```

history-adjustment现有23项：原19项加显式allow预热239根并报告缺口、同一缺口默认严格拒绝且不查因子、少于slow所需数量不宣称初始化、CLI默认fail及显式allow。原用例覆盖构造不连接、240根单查询处理、跨日期显式因子、禁止累乘pcr_factor、未知日/缺失/重复/错合约/未来发布时间拒绝、失败关闭、输入不可变、file/db合并复权、午休窗口、240根无历史目标、错误不部分预热、239根指标恢复后首个新Bar一次目标及重复抑制、配置装配。首根新Bar用例是离线合成固定角色/Bar，不冒充真实角色一致性或实际行情衔接。

## 真实240分钟只读预热

沿用用户已通过的固定IM2610和显式2026-09-21累计因子，扩大至2026-09-22完整交易日。配置为history_im_20260922.json，factor_source_days仅声明这一日，不能直接复用为当前实盘自动来源配置。

```bash
IM_WARMUP_REPORT="tests/results/im2610-warm240-$(date +%s%N)-$$.json"
python -u -m scripts.integration.history.warmup \
  --connect --config demos/01_main_ema/history_im_20260922.json \
  --instrument IM2610.CFFEX --trading-day 2026-09-22 \
  --minutes 240 --fast 3 --slow 5 --timeout 15 \
  --report "$IM_WARMUP_REPORT"
```

预期status=passed、history.required/loaded均240、missing=0、price_adjustment显示来源日2026-09-21及因子1.456591、ema.bars_used=240、ema.revision=0、ema.last_target=null、orders_submitted=0、targets_published=0、state_written=false、ctp_connected=false。首分钟为09:30，末分钟14:59；午休不计缺失。缺分钟仍报失败并输出缺口，不补造价格或默许少于240根通过。

用户贴回离线和真实结果后分别登记。本机只静态检查，当前不填写通过证据。

## 真实缺口与显式允许缺失

用户执行严格240分钟命令，返回HistoryUnavailable：缺1根，首根ts_event=1790053259999999999，对应2026-09-22北京时间13:00分钟结束。这记录的是预期窗口与返回数据的差集；源表是否缺行或存在时间标签偏差仍需查询确认，不补造分钟、不改时间标签，不记为240分钟通过。

只读入口原先固定fail，现暴露服务已有的参数，默认仍fail。同步本次改动后先运行`--only history-adjustment`，再按用户已授权的允许缺失方式验证实际239根：

```bash
python -u -m scripts.integration.history.warmup \
  --connect --config demos/01_main_ema/history_im_20260922.json \
  --instrument IM2610.CFFEX --trading-day 2026-09-22 \
  --minutes 240 --fast 3 --slow 5 --timeout 15 \
  --history-missing-policy allow \
  --report "tests/results/im2610-warm239-$(date +%s%N).json"
```

若返回仍为239根，预期status=allowed_missing、coverage_complete=false，history.required=240、loaded=239、missing=1；missing_minutes列出13:00:59+08:00，精确纳秒由history.first_missing_ns保存。EMA只使用真实返回行，bars_used=239，revision=0、targets_published=0、orders_submitted=0。有效行少于slow初始化需求时本预热核验仍失败；allow不豁免因子错误。此次仅修改只读入口、测试和文档，公共HistoryService不变。新增4项和真实allow结果均待远端执行。

可在DolphinDB核对缺口附近，暂不阻塞上述允许缺失测试：

```sql
select Code,date,minTime,open,high,low,close
from loadTable("dfs://min_bar","cffex_1min")
where Code="IM2610",date=2026.09.22,
      minTime>=12:59:00.000,minTime<=13:03:00.000
order by minTime
```

## 远端真实allow预热结果

用户反馈：status=allowed_missing、coverage_complete=false，required240/loaded239/missing1，缺口1790053259999999999（13:00分钟），sources=[dolphindb]、unavailable_sources=[]。固定IM2610.CFFEX，事件日20260922、因子来源20260921、累计1.456591；availability_policy=declared_source_day_end、available_ns1790006400000000000，fingerprint=54a335597a8cedcb218156af65964a178e936374872940b968329883fe4d4b65，version_evidence=current_read_not_historical_revision_asof。

首根ts1790040659999999999、raw7760.0→adjusted11303.1461600；末根ts1790060399999999999、raw7712.0→adjusted11233.2297920。EMA fast11235.005399922291、slow11235.989822638036、bars_used239、last_processed_ns1790060399999999999、last_main im2610；revision0、last_target null、order_updates_received0、fills_received0。ema_warmed=true，orders_submitted0、targets_published0、state_written=false、ctp_connected=false。

实际数据库→显式因子→EMA允许缺失只读预热已取得证据；完整240分钟仍未通过，源缺行/标签原因待核对，不推称真实重启/实时接续或股指报单已通过。用户未提供本次报告文件名及23项离线结果，不补造。本次仅登记文档证据及对应文档摘要，源码不变，无本机Python/连接。
