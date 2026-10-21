# 公共CTP查询超时及停机拒绝修复（2026-10-21）

## 已知现场

08报告 option-vega-shared-1792546319880010647 的原始failure为“等待CTP account最终回报超时”；停止重新查资金时返回-2，第一次核验退出，cleanup_error为“共享宿主停机未完成，资源锁保留”、final_active_orders=null。后续新连接只读探针交易日20261021、活动订单0；柜台MO2611-C-7100空1手，与08报告归属一致。柜台IM2611空2手没有08归属，不能作为08仓位接管。

这说明新连接目前可查询，不证明旧查询最初为什么超时。修复的是本地查询生命周期和停止的错误分类，不保证柜台或网络不再超时。

## 实现

1. td_transport.py保留超时或发送结果不明请求的_Pending（kind、reqid、timed_out）；超时立即丢弃部分数据，仍向调用方抛TimeoutError，正常运行退出/禁止报单。后续_request在原timeout_seconds内等旧请求结束，不覆盖请求身份、不重复发送。
2. _response只接受匹配kind+reqid；只有last=True确认最终回报，错误分片未标last也不提前释放额度。超时后的数据不进入rows；匹配最终回报只结束旧请求，下一查询丢弃旧数据并使用新ID。断线/close通知等待线程并失败，不自动重连。通用onRspError仍按原流程关闭会话，不放宽协议错误。
3. query_errors.py的CtpQueryRejected兼容RuntimeError，保留kind/method/request_id/code；只有reqQry方法立即返回-2/-3标为可重试。请求已受理后超时仍保留，立即拒绝则清除当前未受理请求。send_order/cancel_order逻辑未变，不自动重发。
4. CtpSharedAccountLifecycle.poll记录第一个查询超时/可重试查询拒绝，闭闸并标记待核验。stop复用原撤单/本地终态与回报排空，最多两轮资金、物理多空及今昨明细、活动订单核验；查询超时由Transport先等待旧最终回报，查询-2/-3在下一轮前退避1秒。其他拒绝、断线、交易日不符、仓位/回报冲突不重试。
5. 两轮仍失败就保持未完成，不以未核验柜台快照覆盖账本、不伪装活动订单为0；此前已确认的成交及停止进度仍按公共持久化流程记录。核验全部通过且无未决执行/活动单才解除此次查询遗留的恢复标志、保存实际归属、停止连接；始终不重新授权报单。原运行failure仍保留，本次持续运行不会自动续跑。
6. 公共SharedAccountLiveHost.snapshot新增stop_reconciliation.errors（最近16轮记录）及queries；Transport保留最近32条send/final/timeout/wait_previous/late_final/drained/rejected等事件。事件仅含类型、ID、耗时、方法和返回码，不含查询字段、账户凭据或资金数据。异常等待／迟到结束会输出INFO，超时／拒绝输出WARNING，正常查询细节为DEBUG。

## 有界范围

没有新增参数；沿用Transport.timeout_seconds，分别约束旧请求结束等待和新请求回报等待。停机最多两轮，每轮有多项查询，原快照受成交扰动时的有限核验次数保留；因此总耗时可能超过单项timeout。旧请求永远没有最终回报时，不再尝试靠不断发新查询“冲掉”旧请求；有限等待失败后保留错误，后续由新进程按原权威恢复流程重新连接。

未增加查询线程池、持久化队列或重连服务；未改变交易策略、风险预算、订单次数、成交归属、时段表或检查点结构。402项离线测试通过，不需要重编译原生CTP扩展。

## 验证

- tests.test_ctp_query_lifecycle + tests.test_shared_account：100项（Transport新测试9项，共享账户新增2项），覆盖无最终回报阻止所有后续查询、迟到最终回报唤醒等待且不复用旧值、错误ID/类型不能释放额度、断线唤醒、发送异常保留不明状态、查询立即拒绝类型、认证/报单/撤单不重试。
- tests.test_live_common、test_live_channels、test_live_recovery、test_execution_composition：126项。
- tests.run_option_vega_offline：91项，随后tests.test_option_shared_rollover、test_option_minute_diagnostics：36项。
- tests.test_shared_ipc、test_shared_process_cli、test_shared_scope_migration：49项，包含真实本机Unix socket及三个进程；首次受沙箱socket限制，允许本机socket后全部通过，无真实网络/柜台。
- 同一SDK实体替身下执行tests.run_p4_ctp_td_transport，原脚本6组检查通过。

合计402项unittest及一个Transport脚本通过。新增集成用真实Transport及假SDK复现：本策略空1手→资金超时→旧请求最终回报迟到→停机第一次查询-2→第二次正常核验，持仓归属不变、无新增委托、保持禁单。持续-2/-3及-1分别验证两轮／一轮失败、不覆盖原状态、恢复标志和未确认结果保留。

## 服务器更新及复测

旧进程结束后，保留原账户及信号文件；不要删除恢复标志或手动编辑JSON。同步本包后：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/CTP_QUERY_DRAIN_RECOVERY_20261021.tar.gz
sha256sum -c doc/evidence/checksums/CTP_QUERY_DRAIN_RECOVERY_20261021_SHA256.txt
```

全部OK后，继续使用原08持续启动命令，保留原state-file及--hours-code MO。MO表下午14:57结束；本包不更改该配置，也不把通用多策略入口视为已接入逐策略时段调度。无需更换state文件、增加仓位或强制制造成交。

如再出现查询故障，贴回summary.json的failure、cleanup_error、stop_reconciliation、final_active_orders和非零strategy_positions。预期：运行仍可因查询故障failed；若最终核验成功，cleanup_error=null、final_active_orders=0，日志出现“停机权威对账完成”；若旧最终回报始终不来，queries.pending应明确保留原ID，errors列出等待失败，cleanup仍失败、final_active_orders=null，这属于有界失败结果，不应自动继续交易。服务器真实运行待验。
