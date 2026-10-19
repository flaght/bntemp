# 08跨日终态停止标志恢复（2026-10-19）

用户检查点generation=88、TD=20261016：failed=false、pending=false、draining/suspended含option-vega-live，Driver无活动关联；20261019柜台无活动订单，7100空1手仍在。旧跨日入口把残留draining与未决发送合并拒绝。本次只修复终态停止恢复，不修改仓位归属、预算、策略参数或14:57时段方案。

## 逻辑

跨日有draining时先校验原订单全为终态、执行在途量为零、Driver无活动关联、归属冻结计划已释放；failed/pending和未完成IPC请求仍拒绝。draining先保留到内存恢复，后续原有柜台资金/总仓/今昨明细/活动订单核对通过后，在未授权状态poll完成贡献冻结并清理标志；冻结阶段暂时停用自动检查点回调，防止提前覆盖原文件。冻结只使用原归属实际仓位，不重发旧目标。之后按原恢复备份提交，报告rollover.completed_draining记录已完成策略。suspended保留，策略预检/授权/新目标仍按原流程，不直接打开报单。

同时修复停止幂等：策略已suspended且已排空、没有自有冻结在途时，重复Runner清理/端口断开不重新加入draining。存在自有在途仍执行撤单和等待，不提前标记完成。

## 部署

本包叠加在OPTION_VEGA_OWNED_POSITIONS_20261016版本上。先停止旧08进程，不手改JSON，不删除draining/suspended，不更换检查点。

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_DRAINING_RECOVERY_20261019.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_DRAINING_RECOVERY_20261019_SHA256.txt
```

全部OK后使用原完整启动命令，保留pro_new下state-1791869411336135873.json、原信号文件、原订单额度和指数1秒频率。运行器自行观察20261019柜台日并恢复，不能手动把检查点日期改成今日。

启动成功应出现08共享会话就绪；若没有额外成交/外部干预，首个有效分钟决策应仍为MO2611-C-7100空1手。恢复过程不会为验收强制报单。若恢复仍失败，保留报告failure和记录目录，不能把未装载的内存零仓当成柜台平仓。

## 证据

离线250项通过：08基础91、共享恢复28、共享账户84、IPC/真实多进程47。新增终态draining跨日权威对账后冻结、原归属和备份保留、failed/pending/查询失败不覆盖原检查点、重复停止不再次落盘draining。SDK实体/柜台为离线夹具，持久化/归属/账户生命周期/IPC为真实组件。无网络CTP/DolphinDB连接，无实际订单。服务器恢复待验证；只读净仓不能替代恢复时多空总仓和今昨明细核验。
