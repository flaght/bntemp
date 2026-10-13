# PF2026100911 历史行情工厂与EMA启动/恢复

2026-10-09。用户已授权实现。第一阶段源码已接入，未在本机执行Python、测试或连接；远程离线与真实DolphinDB验收待完成，GAP04保持开放。之前entry50和continuous18通过属于此前版本，不能作为本次新增代码的通过证据。

## 实施范围

- 公共dataprep/history提供统一请求、分钟DTO、Provider生命周期、可注册工厂、文件和DolphinDB适配、HistoryService。MongoDB仅预留后端，调用未实现驱动显式失败；可注册实际实现而不改EMA策略或协调器。
- 盘前优先指定JSONL/CSV，缺文件或不足窗口则回退配置数据库；盘中直接数据库。按交易日历选完整交易分钟，排除休市。默认fail，显式allow按真实已有行推进，保留缺口审计。
- 历史只更新EMA指标、计数和最后处理时间，不发布历史目标/订单/成交。恢复从断点后补齐，保留已发布目标和revision；完全重复去重，冲突拒绝。加载期间缓存完整实时分钟，接续后新Bar按正常策略决策。
- CTP健康就绪后、报单授权前加载历史，加载后再次核验MD/TD健康。实时缺口仍调用同一服务。报告history/history_audit，EMA组件与策略目标同代持久化。allow只放宽历史覆盖，策略slow最小预热及账户/订单闸门仍生效。
- 参考资料仍独立从原工厂读取；offline仍只允许文件。GAP03参考时效/发布口径与既有关闭证据保持独立，新增历史用途矩阵由本版本复验。

## 参数与限制

在原run_live命令基础上增加：

```bash
--history-minutes 240 \
--history-db dolphindb \
--history-config /实际路径/history-config.json \
--history-file /实际路径/cu-minute-history.jsonl \
--history-stage preopen \
--history-missing-policy fail
```

盘中用`--history-stage recovery`，忽略本地文件；auto根据启动时是否在交易窗口选择阶段。持续监督器首次会话选择preopen，已有状态的后续会话选择recovery。恢复状态仍限定既有`--run-forever --resume`流程，普通一次性入口不会自动接管旧检查点。窗口默认为0，未显式启用history-minutes/config/file时保留原路径；不能和旧recovery-bars同时使用。配置可由EMA_HISTORY_CONFIG提供，但须显式启用历史功能。

首期示例入口限定CU、2026会话日历、固定真实合约和1分钟。加载钩子在MD/TD就绪后执行；不支持在长时间无行情的休市期间预先连接并完成盘前预热。持续监督器仍按既有窗口等待启动。未知日历范围拒绝运行。原始Tick聚合、跨主力历史、历史数据修订可见性、其他品种日历和MongoDB实际驱动不在本次完成范围。

### DolphinDB实际配置待确认

需要真实数据库路径、分钟表名、合约/时间/收盘价字段，时间单位和分钟标签，以及历史复权收盘价或每分钟累计因子。连接复用DDB_HOST/PORT/USERNAME/PASSWORD，JSON不放密码。没有实际Schema前不创建猜测配置、不连接查询。配置结构如下，名称均为占位，须替换后才能使用：

```json
{
  "database": "dfs://ACTUAL_DATABASE",
  "table": "ACTUAL_MINUTE_TABLE",
  "columns": {
    "instrument_id": "ACTUAL_SYMBOL_FIELD",
    "ts_event": "ACTUAL_TIME_FIELD",
    "close": "ACTUAL_CLOSE_FIELD",
    "cumulative_factor": "ACTUAL_EVENT_FACTOR_FIELD"
  },
  "time_unit": "ms",
  "time_label": "start",
  "instrument_format": "symbol",
  "venue": "SHFE"
}
```

若源表已有历史adjusted_close则映射该字段，可省略close/factor。毫秒起点在查询中转为纳秒分钟末最后1ns；end_ns要求源值确实是该分钟末最后1ns。不存在历史因子字段时须另行实现实际历史因子适配，不能映射当前日因子冒充历史。

## 远程验证

同步完整文件集后，在远程pro根目录先核验新清单；不要套用此前PF0910或GAP03旧摘要：

```bash
sha256sum -c doc/evidence/checksums/PF2026100911_SHA256.txt &&
python -u -m tests.run_main_ema_acceptance \
  --only history-provider --only gap03 --only continuous --only entry \
  --only state --only recovery --only autosave --only assembly \
  --only historical --only joint --only controlled
```

新增history-provider33项，覆盖工厂切换、用途矩阵、文件/库回退、严格/允许缺失、冲突、时间/价格、DolphinDB假SDK、240分钟窗口、指标恢复、实时缓冲接续和历史失败不得授权。其他所选模块回归受影响的公共运行时。所有集中模块为离线假库/假柜台，成功不等于真实数据库已接通。

实际表映射确认且离线通过后，先用check_recovery只读验证数据库回补（不连CTP、不改状态、不发布目标），再验证Recording历史/实时衔接，最后复验SimNow受控运行。旧gap01-fresh-cu检查点归属的空仓已被外部平掉，保留它作只读历史诊断；不能用它live resume，否则策略归属与柜台实际仓位冲突。不能仅因允许缺历史而绕过该冲突。

## 首轮远程反馈与测试修正

报告`tests/results/main-ema-acceptance-1791535228971290902`：history-provider运行33项，32通过、1失败（0.115秒，模块1.291秒）。失败发生在实时缓冲衔接用例首次空请求断言：RecordingExecutionClient.requests按公共接口返回tuple，测试错误地与list比较，得到`() != []`。已将该用例两处空请求断言改为`()`，不修改运行源码或放宽行为要求。后续指标/首次新Bar目标断言尚未走到，不能宣称衔接验证通过；gap03/continuous/entry因集中入口停止而尚未运行。

同步更新的test_history_provider.py、本文和PF2026100911_SHA256.txt后，先仅复测history-provider；通过后继续此前未执行的模块。本机仍未执行Python/测试。
