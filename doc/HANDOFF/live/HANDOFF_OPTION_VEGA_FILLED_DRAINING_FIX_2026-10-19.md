# 08全成历史计划与跨日停止标志（2026-10-19）

上一补充的检查错误地要求所有归属订单released=true。实际归属组件在全部成交后可保留released=false，legs每项filled=quantity，实际冻结剩余量已经为零。因而没有活动订单/Driver关联也可能被误判为未完成停止。本补充将检查改为未释放且至少一条leg仍有剩余量才拒绝，与公共_owner_working、_active_instruments、finish_stop_events语义一致，不改写历史订单released值，不丢弃成交记录。

仍要求原订单状态机终态、工作量为零、Driver活动关联为空，并通过归属/订单同代一致性校验和后续权威柜台核验。若仍拒绝，异常列出orders_nonterminal、working_quantity_nonzero、driver_associations_nonempty、attribution_remaining_nonzero中的具体项。尚未直接读取服务器完整检查点，不能以本地复现代替服务器成功结果。

本包依赖OWNED_POSITIONS、DRAINING_RECOVERY及SOURCE_METADATA_COMPAT已部署。只同步本包及SHA清单，不手动清理JSON，不再次覆盖旧完整包：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_FILLED_DRAINING_FIX_20261019.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_FILLED_DRAINING_FIX_20261019_SHA256.txt
```

全部OK后沿用原完整启动命令及pro_new下原state-1791869411336135873.json。应观察08共享会话就绪和首个有效分钟归属；原账本/柜台未变时仍为7100空1手。若仍失败贴出完整异常末行（带检查项），不能跳过未解释订单或直接清空标志。

离线120项通过：08基础91、共享恢复29。组合测试包含缺少来源元数据、残留draining、FILLED订单released=false但剩余量零的跨日真实公共恢复，并验证归属/备份保留及最终停止；未释放有剩余量的计划仍失败且原检查点字节不变。无实际网络连接或订单，线上恢复待验；14:57处理不包含本次。
