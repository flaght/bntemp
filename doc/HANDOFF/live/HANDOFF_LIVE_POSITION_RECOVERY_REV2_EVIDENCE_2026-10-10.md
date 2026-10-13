# PF2026101036 REV2：Linux恢复测试结果

2026-10-10，用户提供远程Linux执行结果：

```text
Ran 9 tests in 0.413s
OK
模块结果: {'module': 'ema-recovery', 'exit_code': 0, 'seconds': 1.65}
全部选定离线模块通过；实际柜台Recording/组合成交仍须独立验收。
```

执行组为`python -u -m tests.run_cross_section_acceptance --only ema-recovery`。
此前访问第二笔未产生订单的`IndexError`已消失，新增9项恢复回归整组通过。
该组使用原生实体和假MD/TD API，包含Replay接管、同TD恢复、旧目标暂停、
身份/柜台仓位不一致拒绝及CLI约束；不代表实际柜台接管或恢复成交已验收。

本次结果仅确认`ema-recovery`组；不能据此宣称其他回归组全部通过。
公共恢复模块同时被01和04使用，后续应继续核验`live-common`、`entry`、
`cross-section`、`continuous`和`assembly`组，再单独进行实际柜台验收。

本记录独立保存，不改写原部署摘要`../../evidence/checksums/PF2026101036_SHA256.txt`及修正摘要
`../../evidence/checksums/PF2026101036_REV2_SHA256.txt`。源码和修正说明见
[Live策略持仓与恢复交接](HANDOFF_LIVE_POSITION_RECOVERY_2026-10-10.md)。
