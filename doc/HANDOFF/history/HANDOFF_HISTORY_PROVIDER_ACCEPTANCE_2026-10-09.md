# PF2026100911远程离线验收进度

2026-10-13增量：此前用户提供history-provider41、im-runtime9、gap03 22、entry55项通过输出；它们不自动覆盖后续旧日历清理。清理后会话组43项本地通过，当前IM Runtime远程复验待提供。CU真实时段／窗口／分钟时间点对照已通过，但CU历史分钟源用户确认没有，第二套Replay记录`history_enabled=False`，只见实时Bar预热进度。历史源3份JSON仍待统一，数据库历史预热／实际接续和最终Recording未验收；完整当前范围见[会话台账](../../data/TRADING_SESSION_GENERALIZATION_2026-10-09.md)。下文保留各历史版本的结果。

2026-10-09，用户提供远程uv-nautilus输出。本轮仅登记证据，不改源码、不运行本机Python、不联网。原实施说明中“首轮失败、后续未运行”是当时记录，由下列新结果补充。

| 模块 | 结果 | 用例时间 | 模块时间 | 报告目录（远程pro相对路径） |
| --- | --- | --- | --- | --- |
| history-provider | 33/33通过 | 0.110秒 | 1.297秒 | tests/results/main-ema-acceptance-1791535396844485170 |
| continuous | 18/18通过 | 0.067秒 | 1.271秒 | tests/results/main-ema-acceptance-1791535401649845489 |
| gap03 | 22/22通过 | 1.315秒 | 2.133秒 | 同上 |
| entry | 50/50通过 | 1.772秒 | 3.027秒 | 同上 |

此前tuple/list断言修正后的实时缓冲预热及首个新Bar正常目标用例现通过。四组共123项；测试为假SDK/假柜台，不能作为真实DolphinDB分钟读取或实际CTP衔接证据。所提供输出没有单独的新SHA清单核验结果，不补造该证据。

本版本剩余已计划公共回归：state、recovery、autosave、assembly、historical、joint、controlled。源码未变化，无需重复已通过四组：

```bash
python -u -m tests.run_main_ema_acceptance \
  --only state --only recovery --only autosave --only assembly \
  --only historical --only joint --only controlled
```

实际DolphinDB分钟表路径/字段/时间标签/历史复权口径仍待用户提供。取得配置后先只读历史检查，再Recording，最后SimNow受控衔接。MongoDB实际驱动仍未实现，GAP04保持开放。此前已外部平仓的旧EMA检查点只作历史诊断，不能在线恢复。实现参数及边界见[实施交接](HANDOFF_HISTORY_PROVIDER_2026-10-09.md)。

## 后续集中验收反馈

用户随后提供集中验收末段：autosave exit_code=0（1.192秒）、recovery exit_code=0（1.203秒）、controlled exit_code=0（1.281秒），最终输出“全部选定离线模块通过”。明确覆盖柜台受理但调用异常时保留在途量、回报后保存失败关闭Driver、同代持久化及权威活动订单恢复、跨重启成交去重、未知/冲突订单闭闸、DEMO授权及并发资金核验。

本段未包含state/assembly/historical/joint的逐模块结果、所选命令或报告目录，不编造其条数、耗时或报告路径；保留用户集中成功结论。源码未变化，不要求重复已通过模块。下一阶段是实际DolphinDB历史分钟只读读取及覆盖验证，再进行Recording衔接，最后SimNow受控订单。离线成功不替代实际数据库/柜台验收，GAP04保持开放。本次仅更新证据文档，PF0911源码摘要保持不变。
