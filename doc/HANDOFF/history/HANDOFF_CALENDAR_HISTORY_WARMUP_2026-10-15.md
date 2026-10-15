# 03 跨期策略 DDB 历史预热

2026-10-15，Asia/Shanghai。范围为原CalendarSpreadStrategy的Live历史窗口，
覆盖Recording/Model/SimNow、共享宿主、worker及宿主单策略重启；不扩展跨日换约。

## 行为

默认lookback=120，以120个历史同步原始价差点初始化，第121帧起按原信号规则决策。
跨期不用复权因子，历史及实时均为第一腿原始close减第二腿原始close。
新增warm_closed只推进窗口与时间/帧计数；保留direction、revision及归属仓位，历史无目标或报单。

新增history.py复用公共HistoryService及DDB Provider，逐腿查询后按同一时间交集对齐。
保留每腿required/loaded/missing/sources及完整帧覆盖；缺腿默认失败，allow仅用真实完整帧。
查询不可用、字段/价格/重复冲突不受allow豁免。先验证全批原始价格再推进策略。
完成分钟标签end复用01已实现的转换；AU配置不包含adjusted_close或因子参数。

订阅期间先缓冲，历史加载与健康复核完成后开放实时决策；完整缓冲帧只预热，
部分缓冲保留到对腿到达，按时间去重。实时发现缺口先补历史窗口，不回放历史交易。
恢复沿用原账户/策略归属及方向核验，再预热；策略会话身份增加历史配置和日历摘要。
不启用历史参数的原实时路径保持原行为。

## 代码及同步

- demos/03_calendar_spread/{strategy,live_runtime,recovery,run_live,history}.py、history_au.json、README.md。
- bomber/framework/trader/runtime/live/shared.py：历史准备允许无drain_events的本地Recording客户端；报告加complete_frames。
- scripts/integration/shared_account/factories.py：允许03历史参数，04仍不支持。
- tests/test_calendar_spread_history.py、test_calendar_spread_live.py、test_shared_account.py及run_main_ema_acceptance.py。

旧tests/run_calendar_spread_acceptance.py保留其历史固定摘要；本次使用中央验收入口，
不以旧摘要文件代表当前预热版本。中央入口新增calendar-history组。

## 验证

本机Python3.12 + .validation/packages：12项历史/实际策略/纯信号回归和76项共享账户回归通过，
合计88项。覆盖历史无目标、首个实时帧决策、缺腿严格失败/允许、错分钟不拼接、
实时缓冲补足与去重、部分缓冲等待对腿、恢复方向/版本不重放、无原始价格批次不部分推进、
数据库故障不可豁免及缓冲/数据库价格冲突。

tests.test_calendar_spread_live本机导入失败：缺少原生Bar扩展，未执行该组。
该组新增原生Runner缓冲接续、失败不开闸及CLI历史参数用例，须远程运行。
没有连接真实DDB/CTP，不登记M06真实场景通过。

远程先执行：

```bash
python -u -m tests.run_main_ema_acceptance --only calendar-history --only calendar-live --only shared-account
```

随后使用03 README的AU Recording命令验证。表目前有AU不等于secondary/far两腿齐全；
必须核对实际两合约的历史覆盖，缺数据不改角色或缩小lookback制造通过。
Recording通过后，真实订单、双腿成交归属和同日恢复另行验证。

## 2026-10-16：远程进度与运行日志

用户远程calendar-live修正假环境变量及旧InstrumentHealthGate引用后31/31通过。
真实AU Recording已确认secondary=au2702、far=au2704，两腿DDB各20帧、缺口0；
进入实时阶段后又实际回补两腿各6帧、缺口0。仅登记这两个窗口读取成功，
不登记120根预热、完整600秒Recording或真实成交通过，仍缺终态报告及实际参数核对。

独立入口新增progress.py，每15秒或新帧打印窗口/历史帧/实时帧/双腿Bar/缺腿等待及目标数；
历史与实时分别计数并进入最终报告，启动打印实际lookback/history_minutes/seconds，
历史报告增加load_seconds。不改变信号、窗口或运行期限。
同步03的progress.py、run_live.py、live_runtime.py、history.py及中央验收清单。
本次历史回归12项通过，进度输出的节流/新帧/窗口及缺腿显示手工检查通过；语法与diff检查通过。
