# CTP查询超时后的受控停止（2026-10-19）

用户08已成功跨日恢复并持有7100空1手，在运行定期对账时orders最终回报超时。旧停止调用要求账户健康的service.poll，导致二次报错、策略停止未完成。后续只读柜台查询成功、无活动订单，不代表原超时不存在。

## 修改

1. 独立归属且无本系统活动单的定期检查复用完整对账中一次活动订单查询，不再随后重复查询。存在活动单时仍按原快照/成交时序规则检查外部仓位及订单。
2. 公共service增加poll_stopping：只冻结已经排空的停止贡献，不检查交易健康、不做预算推进/目标续单；failed、pending、回报/组合冲突仍拒绝。SharedAccountLiveHost.stop_strategy使用该路径，持续接收/分派成交和订单回报，随后解绑Runner。
3. 账户停止先禁止新委托并撤本系统已知订单，本地终态等待不再每0.2秒查询全账户。之后查询资金、总仓、今昨明细及活动单完成权威对账。仅TimeoutError最多尝试两轮，每项沿用原查询超时；原有成交导致快照变化的内部有界重查保持。断线、柜台日不符、未知活动订单、仓位/归属冲突不当作超时重试。
4. 仅当原错误是查询TimeoutError且最终对账确认无活动单、本地无在途/回报冲突，解除这次查询遗留的恢复标志，再保存实际归属。绝不重新授权或自动继续交易。资金心跳超时可能取消client.reconciled，停止核验直接使用仍连接的Driver只读查询，不把旧资金缓存当新结果。
5. 最终总仓和活动单结果复用已核验查询，不在保存后追加查询。持续超时仍报告停止未完成，final_active_orders保持null、恢复标志保留，不伪装成0；未知活动单不自动撤销。
6. 新报告stop_reconciliation包含query_failure/attempts；SharedAccountStop日志显示对账轮次及完成。原始failure保留，正常完成停止时cleanup_error=null、final_active_orders=0；status仍failed，持续运行器不会因查询恢复而自动继续本次交易。
7. Transport已有kind+reqid过滤迟到回报，本次新增测试验证，未改传输代码。未证明本次超时具体由CTP限流/网络造成，不增加无界重试或自动重连。

## 部署

本包叠加此前OWNED_POSITIONS、DRAINING_RECOVERY、SOURCE_METADATA_COMPAT、FILLED_DRAINING_FIX修复。先停止旧进程，原状态文件及信号文件保留，不手动删除恢复标志：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_QUERY_TIMEOUT_STOP_20261019.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_QUERY_TIMEOUT_STOP_20261019_SHA256.txt
```

全部OK后沿用原完整启动命令，仍使用pro_new/tests/results/gap08-realtime-simnow/state-1791869411336135873.json。先看08共享会话就绪和首个有效分钟决策，不强制测试报单。如果再发生查询超时，记录原failure、cleanup_error、stop_reconciliation及final_active_orders；出现停机权威对账完成后应是failed运行但已完成停止，可再核验后重启。仍失败时保持检查点等待对账，不能因最新探针无订单就直接清状态。

## 验证

376项离线回归通过：08基础91、共享恢复29、共享账户89、公共生命周期120、IPC/多进程47。新增运行检查仅查一次订单、订单查询超时后部分成交/撤单/策略回报排空并有界重查成功、持续超时保留恢复标志、资金查询超时可停止但不重新授权、旧查询迟到最终回报不冒充下一次查询。实际柜台/SDK实体为夹具，其余使用真实公共执行、归属、持久化、IPC组件。Unix socket测试在本机权限下执行，无真实柜台或数据库连接，无实际订单。服务器运行及故障停止仍待验证。14:57交易阶段方案不属于本补充。
