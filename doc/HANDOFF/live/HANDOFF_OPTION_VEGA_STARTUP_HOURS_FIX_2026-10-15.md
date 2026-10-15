# 08持续启动：静态时段库参数修正

2026-10-15用户服务器启动停于`load_calendar → load_session_inputs → read_trading_hours → DolphinDbReferenceSource.read`，报`DolphinDB trading_hours 查询失败；禁止解释为空数据`。此阶段只读检查点并查询会话资料，尚未启动策略或连接TD，不产生报单或仓位修改。

此前命令未显式指定时段库和表，底层默认使用`DDB_REFERENCE_DATABASE`（未设置时为`dfs://bomber_daily`）及`DDB_TRADING_HOURS_TABLE`（未设置时为`trading_hours`）。但项目已记录服务器实际时段位于`dfs://bomber_static/fut_trading_session`，见`scripts/integration/reference_data/README.md`。此前启动说明遗漏了这个依赖。

在08快捷入口的`main([...])`参数列表或多策略入口命令中增加：

```python
"--trading-hours-database", "dfs://bomber_static",
"--trading-hours-table", "fut_trading_session",
```

其余恢复、DolphinDB分钟流、指数特征和仓位参数保持。不把`DDB_REFERENCE_DATABASE`全局改为静态库：期权/期货基础资料仍需原日表库。也不修改旧JSON、手工填造交易时段或删除持仓腿。

`--hours-code IM`查询该表的IM规则；原`--no-session-overrides`仍仅适用于已确认无例外的规则范围，有例外时须指定真实例外表。仅修正已记录的表地址；无法据现有通用异常判断服务器是否另有权限或schema问题。若修正后仍失败，在同一DolphinDB连接只读执行下列语句，返回schema及IM规则或原始错误（不要贴密码）：

```dolphindb
schema(loadTable("dfs://bomber_static", "fut_trading_session")).colDefs
select * from loadTable("dfs://bomber_static", "fut_trading_session") where upper(Code) = "IM"
```

公共查询需要`Code, segment_id, date_anchor, start_day_offset, start_time, end_day_offset, end_time`七列。Python底层错误已脱敏，不能只凭通用异常断言表不存在。

本次不改运行源码，不要求重新编译或再次做账户迁移；继续使用当前已同步版本和原状态文件。20261014历史交付包及其校验清单保留原字节，本修正另有20261015文档校验清单。修正后的真实服务器结果仍待用户返回。
