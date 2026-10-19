# 08旧指数来源身份新增字段兼容（2026-10-19）

服务器比较显示旧sources.index仅缺alignment和computation_version，当前为EXACT及raw-index-minute-v1:declaration:f0c862a821fd7ef17cae18e73f1d4ca83b4017258243063f5d1cb57e091d9134。默认声明生成的Schema指纹为2a774e029cb0a78d8b3c451a005bbed81d26a570c3e317d78a9e7b31d66692df，与用户原有身份一致。本次是新增元数据被误判为来源切换，发生在draining清理前，不是行情断流或新的仓位冲突。

只为dolphindb-dfs旧默认index.minute/Schema1且指纹完全匹配当前默认声明的身份补齐缺失alignment/computation_version，再按原恢复规则比较。已有字段不能覆盖，真实alignment、计算版本、Schema、表、可见性变化仍拒绝；CTP到DolphinDB的既有迁移规则保留。来源不一致的错误新增差异字段路径，不打印值。输入旧身份不会原地改写，成功恢复后才按原检查点备份提交机制保存新身份。

本包依赖此前OWNED_POSITIONS及DRAINING_RECOVERY补充；不重复修改其公共实现，不包含14:57处理。

## 服务器

停止旧08进程后同步本补充包及SHA清单，执行：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_SOURCE_METADATA_COMPAT_20261019.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_SOURCE_METADATA_COMPAT_20261019_SHA256.txt
```

全部OK后沿用上一条完整启动命令和pro_new下原state-1791869411336135873.json，无新参数，不删除状态或更改来源。等待08共享会话就绪及首个有效分钟决策；若柜台和原账本没有新变化，应恢复7100空1手。服务器尚未验证，不能将离线通过视作实际启动成功。

## 验证

08基础91项及共享恢复29项共120项离线测试通过。新增精确声明身份兼容、原输入不变、对齐/计算版本/指纹/表/可见性变化拒绝、显式旧计算版本不得覆盖；组合回归使用旧元数据加残留draining，完成真实公共跨日恢复、归属保留、备份和最终停止。未连接DolphinDB或CTP，无实际订单。
