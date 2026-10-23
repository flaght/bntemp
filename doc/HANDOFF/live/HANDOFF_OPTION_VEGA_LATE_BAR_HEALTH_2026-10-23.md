# 08迟到Bar健康记录修复（2026-10-23）

## 现场与代码问题

用户10月23日报告：10:31持仓MO2611-C-7100行情正常；10:32帧在10:32:22.546617封闭，持仓腿缺失，DolphinDB该腿updateTime为10:32:22.645（相差约0.098秒），10:33:00.054983 Runner仍将10:31作为最新源事件，年龄120.055秒后停止。后续表中10:33、10:34数据均存在。柜台收尾正常，08归属仍空1手、活动订单0、TD30秒配置已生效。

updateTime不是程序实际接收时刻，单凭表截图不能证明当时回调是否已到达。但代码存在确定的放大路径：publish在frames.add返回False时提前return，导致有效迟到Bar也不能更新observations，后续健康检查仍读旧分钟。离线使用真实Runner复现并验证修复该路径。

## 修改内容与边界

只改demos/08_option_vega/live_runtime.py的publish和run_live.py的报告：

1. 继续先校验绑定来源、估值日期、源事件年龄/未来时间和单合约时间单调性；新加独立的有限正价格校验，因为公共FrameBuffer对迟到Bar在价格检查前就返回。
2. 只有frames.late_count因本次输入增加，且该业务分钟严格晚于此前健康记录（或尚无健康记录）时，更新observations=(valuation, received)。帧已封闭与期限已到但控制线程尚未flush两条路径一致处理。
3. 不向帧补行情，不改变已封闭frame_audit，不写strategy.latest或执行价格缓存，不直接调用策略或提交目标；候选选择仍要求原有同分钟输入。不能把HEALTH_ONLY_LATE当成COMPLETE或补单成功。
4. 同一分钟重复回调不刷新健康接收时间；价格无效、业务日错误、绑定来源错误、未来时间、真实过期或倒退仍保持原拒绝。过期未持仓候选继续按旧逻辑忽略，不借新逻辑刷新健康。
5. 增加late_bar_health_updates计数，所有源的有效迟到健康更新均计数，本次进程内统计、不持久化。bar_time_audit记录HEALTH_ONLY_LATE，仍为最近32条；必需腿输出INFO“迟到Bar仅更新行情健康”，非必需候选不逐条刷INFO，避免全链迟到日志膨胀。

不修改公共MinuteFrameBuffer、接收期限、期权源120秒时效、CTP超时、DolphinDB查询/订阅方式、策略数值公式、风险限制、真实成交归属或检查点结构。仍可能因为真实断流、行情严重迟到、字段无效而停止；这不是自动恢复故障策略或延长行情期限。

## 验证

302项unittest通过：
- 原08装配/来源/归属基础91项。
- 新增test_option_late_bar_health七项，加共享跨日恢复、分钟诊断、共享持续及公共窗口调度，共63项。
- 原数值/策略/归属恢复/strategy1源逻辑46项。
- 公共CTP查询生命周期与共享账户102项。

新增测试以真实08 Runner、模拟执行真实归属仓位复现10:31→10:32缺失→22.645迟到→10:33健康不误报，核对历史帧、策略价格缓存、执行参考价格、提交计数和成交数不变；下一10:33及时Bar能正常生成COMPLETE帧。另验证截止已过但尚未flush的迟到不能进入选约或产生目标；没有后续Bar时超过源事件120秒仍关闭闸门并保留空1手；重复旧分钟不能延长接收健康；0/负数/NaN/Infinity迟到价格不更新健康；时间倒退和真实过期仍失败。原测试继续覆盖未来日期/时点、错误来源、指数迟到拒绝等。

SDK实体、行情和柜台使用离线夹具，没有连接DolphinDB或CTP、没有真实报单。新健康计数不需要改已有检查点。服务器线上待验证。

## 更新及运行

先正常结束旧进程，保留原账户与信号文件。同步本包后：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_LATE_BAR_HEALTH_20261023.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_LATE_BAR_HEALTH_20261023_SHA256.txt
```

全部OK后沿用原完整启动命令，继续原state-file、--hours-code MO及export CTP_TD_TIMEOUT_SECONDS=30。不调整frame-wait-seconds或market-bar-max-age-seconds，不强制产生新成交。

若再次遇到有效Bar稍晚于帧期限，预期见“迟到Bar仅更新行情健康”，随后运行心跳继续；该旧帧仍INCOMPLETE，策略旧缓存不补价。未来真实及时分钟正常进入决策；若持续收不到更新的有效分钟，仍必须按原120秒约束失败。终止后检查late_bar_health_updates与bar_time_audit中的HEALTH_ONLY_LATE；0表示本次没有走到新的有效迟到健康更新路径，不能声称已验证线上效果。若仍停止，保留failure、逐策略failure、bar_time_audit、末两帧及停机核验结果，不直接放大时效阈值。

## 本机复测命令

```bash
PYTHONPATH=.validation/packages:. python - <<'PYTEST'
import runpy, unittest
try:
    runpy.run_module('tests.run_option_vega_offline', run_name='__main__')
except SystemExit as e:
    if e.code:
        raise
suite = unittest.defaultTestLoader.loadTestsFromNames([
    'tests.test_option_late_bar_health', 'tests.test_option_shared_rollover',
    'tests.test_option_minute_diagnostics', 'tests.test_shared_continuous',
    'tests.test_continuous_position_runtime'])
result = unittest.TextTestRunner().run(suite)
raise SystemExit(not result.wasSuccessful())
PYTEST
```

上述显式使用SDK实体替身，不代替服务器原生运行。其余46/102项各自在独立Python进程执行，避免不同测试的SDK替身互相污染。
