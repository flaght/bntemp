# 08分钟行情结构与决策诊断

用户要求每分钟可确认行情是否齐全、是否执行判断、为何没有新委托。2026-10-15实现，仅增加诊断；原行情等待期限、选约时点、持仓管理/退出/对冲规则及归属持久化保持。

## 输出

INFO新增每自然分钟一次`分钟运行心跳`，每封闭帧一次`分钟行情汇总`及`分钟决策汇总`。全程仍沿用OptionMarketInputs logger，使用之前的INFO启动命令即可，不增加日志开关。

下面是字段示例，非服务器运行结果，省略了实际输出的其他字段：

```text
分钟行情汇总: {"minute":"2026-10-15T09:40:00+08:00","status":"COMPLETE","missing_required":[],"required":{"000852":{"same_minute":true,"price":"6890.9252"},"IM2611":{"same_minute":true,"price":"6900.0"},"MO2611-C-7400":{"same_minute":true,"price":"82.8"}}}
分钟决策汇总: {"minute":"2026-10-15T09:40:00+08:00","status":"NO_TARGET_CHANGE","positions":{"MO2611-C-7400":"-1"},"entry":"WAIT_SLOT","entry_slot":"13:36","hedge":"NOT_DUE"}
```

真实行情摘要还显示每个必需标的原Bar时间、接收时刻与延迟、帧deadline/sealed_at、最后观测分钟，及期权/期指数量、同分钟候选三腿组数。记录`late_bars_total`和`ignored_stale_candidates_total`，不会把所有未到齐候选逐一刷屏或误报为持仓腿失效。

决策摘要包含真实归属仓位/在途量、实际目标、退出条件、对冲触发原因、选约audit及原目标提交/成交/订单更新计数。目标提交不意味着柜台受理或成交。恢复等待显示RESUME_WAIT及missing；缺指数显示SKIPPED_INDEX；异常显示ERROR并仍按原规则停止。NO_TARGET_CHANGE表示本帧已判断且目标未变化。price_cache显示当前可用价格及业务年龄，避免将缓存价格误认为同分钟输入。实际退出检查remaining_days_exact=false时计数仅为已知范围下界，不伪称精确到期天数。

原始结构写入`frame_audit[].market_summary/decision`，最多256帧。心跳中的last_closed_minute含已跳过的帧，不等于最后成功决策；结合last_decision_status和帧decision判断。

## 同步与使用

先在服务器现有进程Ctrl+C正常保存退出，再更新模块、用同一原状态路径及原启动命令重启；不删除或改写JSON，不为验证制造成交。不能只替换运行中模块文件就认为已加载新日志。

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_MINUTE_DIAGNOSTICS_20261015.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_MINUTE_DIAGNOSTICS_20261015_SHA256.txt
python -u -m unittest tests.test_option_minute_diagnostics tests.test_option_vega_assembly
```

随后执行原INFO启动命令，保留：

```text
--calendar-id china.cffex --hours-code IM
--trading-hours-database dfs://bomber_static
--trading-hours-table fut_trading_session
--no-session-overrides
--run-forever
```

其中无日期例外声明仍须匹配真实规则；已有例外表时使用原正确例外表。可选集中入口为`python -u -m tests.run_main_ema_acceptance --only option-minute-diagnostics`。本包为当前08的日志补充，不覆盖其他策略或共享账户升级包；旧版本内置源码摘要不能用于判定新代码是否部署，本包单独SHA清单用于核对新增/修改的模块。

## 验证

本地SDK实体替身下，实际08/公共行情帧/策略/执行/持久化81项回归通过；新增7项分钟诊断及19项共享跨日回归通过。覆盖正常无目标变化、选约目标但零成交、指数缺失跳过、恢复缺腿等待后新帧恢复判断、异常传播、心跳频率及日志开关不改变目标。未连接真实DolphinDB或CTP，服务器当前09:31缺腿及09:38过期非持仓候选样本不登记为在线诊断通过，需以新运行日志确认。
