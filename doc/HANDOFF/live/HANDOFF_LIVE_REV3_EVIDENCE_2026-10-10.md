# PF2026101036 REV3：Linux离线验证结果

2026-10-10，用户提供远程Linux执行结果：

```text
Ran 20 tests in 0.072s
OK
模块结果: {'module': 'continuous', 'exit_code': 0, 'seconds': 1.204}

Ran 69 tests in 0.025s
OK
模块结果: {'module': 'live-common', 'exit_code': 0, 'seconds': 0.125}
全部选定离线模块通过；实际柜台Recording/组合成交仍须独立验收。
```

此前持续模式无法导入checkpoint、Replay Tick副本缺少aggressor_side的失败
均未再出现。本消息未提供远程摘要核验输出，不补写已核验SHA的结论。

目前用户提供的本轮离线通过证据：ema-recovery 9项（REV2）、continuous
20项、live-common 69项。它们分别验证假API原生恢复装配、持续会话和公共
Live机制，不代表实际柜台接管、恢复或成交已验收。

本轮公共恢复修改后的entry、cross-section、assembly三组尚无新的通过结果；
后续命令只包含这三组，已通过组没有新修改时不重复执行：

```bash
python -u -m tests.run_cross_section_acceptance \
  --only entry --only cross-section --only assembly
```

本记录独立保存，不改写历史部署或REV2/REV3摘要。
同步说明见[REV3依赖记录](HANDOFF_LIVE_REV3_DEPENDENCIES_2026-10-10.md)，
恢复证据见[REV2结果](HANDOFF_LIVE_POSITION_RECOVERY_REV2_EVIDENCE_2026-10-10.md)。

## 后续选定组结果

随后用户提供集中验收的结束日志：

```text
Ran 10 tests in 0.033s
OK
模块结果: {'module': 'assembly', 'exit_code': 0, 'seconds': 1.292}
全部选定离线模块通过；实际柜台Recording/组合成交仍须独立验收。
```

该片段明确确认assembly 10项通过，并报告全部选定模块通过；没有单独展示
entry/cross-section模块的计数与退出码，不推测这些计数。上一条建议命令
选择entry、cross-section和assembly，若按该命令执行，则结束状态覆盖三组。
无新修改时结束本轮离线回归，不重复执行已通过组。

下一阶段为实际Replay柜台验证：重新只读核对当前TD/MD交易日、资料日、
真实合约及已有仓位；符合显式接管条件后用新的独立状态文件运行01，停机保存，
随后用同一文件和相同身份参数执行--resume。柜台/时段/状态必须用运行时事实，
不将此前20261009等探针值当作永久配置；接管和恢复命令见策略持仓恢复交接。
本轮尚未收到实际接管或恢复成交证据。
