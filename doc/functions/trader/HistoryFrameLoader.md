# Live 历史预热与实时衔接

公共实现：[runtime/live/history.py](../../../bomber/framework/trader/runtime/live/history.py)。这些函数编排 Live 启动及恢复，放在 Live 目录；底层历史读取、完整分钟标准化、文件／数据库选择、去重冲突检查与复权仍复用 `framework.dataprep.history.HistoryService`。

## 公共接口

| 接口 | 职责 |
| --- | --- |
| `build_history_service(...)` | `dataprep.history`从普通行情JSON组装服务，调用方显式提供字段需求、日历和缺失政策；不连接存储 |
| `warm_history_window(service, *, calendar, instrument, before_ns, minutes, consume, ...)` | 单路完整窗口加载、归档筛选、覆盖审计和预热回调；返回HistoryResult，失败保留原异常 |
| `merge_history_rows(rows, *, calendar, before_ns, minutes, event_ns)` | 按业务提供的时间映射合并近期完整分钟，相同分钟冲突拒绝，剔除过旧或未来行 |
| `history_phase(stage, calendars, *, now=None)` | 显式 `preopen/recovery` 优先；`auto` 在全部绑定日历处于交易窗口时选择 `recovery` |
| `history_stamps(calendar, before_ns, minutes, *, after_ns=None)` | 首次取最近 N 个完整交易分钟；已有断点只取断点之后、边界之前的交易分钟，跳过休市 |
| `HistoryFrameLoader(missing_policy="fail", coverage_key="instruments")` | 汇总多路历史覆盖，只返回同一分钟各路都有数据的帧 |
| `buffer_history_bar(runner, feed_id, bar)` | 预热期间在提交锁内缓存已绑定来源／合约的非未来 Bar，上限 10000 条；返回 False 表示已切换到实时阶段 |
| `buffered_history(runner)` | 在同一提交锁内获取缓冲快照 |
| `complete_buffer_boundary(events, instruments, *, first_ns, after_ns)` | 找到所有合约到齐且尚未处理的最后一分钟，为回补确定右边界 |
| `first_complete_minute(now_ns)` | 保留原有完整分钟门槛，排除启动时已形成的部分分钟 |
| `start_after_history(runner, *, flush, validate, replay_buffer=False)` | 在同一提交锁内合并缓冲、核验会话、切换到实时阶段；失败或常驻停止时保持决策关闭 |

`HistoryFrameLoader`、`history_stamps`、`start_after_history` 也从 `bomber.framework.trader.runtime.live` 导出；其他辅助函数从 `.live.history` 显式导入。

## 同步窗口加载

```python
loader = HistoryFrameLoader(missing_policy="fail")
frames = loader.load_frames(
    {"near": (near_service, near_instrument), "far": (far_service, far_instrument)},
    stamps,
    phase="recovery",
    live_bars={"near": buffered_near, "far": buffered_far},
)
```

逐路 `HistoryService` 使用 `missing_policy="allow"` 收集完整覆盖，最终缺失政策由加载器统一判断。返回值是按时间排序的 `((ts_event, {key: HistoryBar, ...}), ...)`。业务层先验证全部价格／字段，再调用自己的预热接口；加载器不调用策略实时回调。

`last_audit`／最近 50 次 `audit_history` 记录阶段、要求帧数、完整帧数、缺失位置、来源、耗时和逐路覆盖；现有 03 的 `legs`、04 的 `products` 报告字段继续保留。`allow` 只接受真实覆盖不足，不生成分钟、不复制其他腿，来源不可用仍失败。重复与同键内容冲突继续由原 `HistoryService` 处理。

## 缓冲与放行

Runner 沿用现有的 `_submit_lock`、`history_buffer`、`history_buffering`、`accept_bars`、`market_feed_id`、合约绑定及 `_clock_ns`。没有新增 Runner 基类或策略协议。

`start_after_history` 先关闭决策，再执行 `flush()` 和 `validate()`；`validate()` 失败时须抛异常。成功后清理缓冲并开放实时处理。03／04 使用 `replay_buffer=True`，把剩余事件交给原同步器保留未到齐的帧；处理断点过滤已经预热的分钟。01 消耗已转换的指标分钟，不回放历史交易。常驻窗口的停止检查发生在回补前和放行前，正常停止不会因健康核验而重新开放决策。

缓冲写入与放行共用同一把锁：放行期间到达的行情等待锁释放，随后走实时分支。缓冲异常由应用原有失败逻辑禁单，公共层不决定账户政策。

## 已接入文件与业务边界

| Demo | 公共能力接入 | 保留的业务处理 |
| --- | --- | --- |
| 01 EMA | [history.py](../../../demos/01_main_ema/history.py)、[run_live.py](../../../demos/01_main_ema/run_live.py)：阶段、窗口、缓冲、完整分钟门槛、放行 | 历史复权价、EMA 精确断点、启动最新分钟的 20 秒落库等待政策、指标预热 |
| 03 Calendar | [history.py](../../../demos/03_calendar_spread/history.py)、[live_runtime.py](../../../demos/03_calendar_spread/live_runtime.py)：同步覆盖、缺口、缓冲、放行 | 原始双腿价差、合约配对、方向与目标版本 |
| 04 Cross section | [history.py](../../../demos/04_cross_section/history.py)、[live_runtime.py](../../../demos/04_cross_section/live_runtime.py)：同步覆盖、缺口、缓冲、放行 | 跨品种共同日历、历史复权价、因子来源日一致性、横截面指标 |
| 10 Hybrid | [history.py](../../../demos/10_hybrid_transformer/history.py)：配置组装、单路加载审计、本地归档筛选、交易分钟选择与窗口合并全部复用框架 | 声明原始字段需求、研究时间标签、模型预热回调与模型窗口身份 |
| 08 Vega | 当前没有同类行情历史预热链路，本轮无需接入 | 指数特征查询和期权分钟帧仍按现有流程处理 |

业务 `history.py` 只负责数据需求与策略预热适配；Hybrid的原始期货数值和VWAP校验复用`dataprep.history.futures.futures_history_values`，与实时CustomBar共用`market.futures.futures_values`。现有历史参数、行情来源优先级、账户恢复和执行政策保持原入口约束。

## 验证

```bash
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_history_offline
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_continuous_offline
```

第一条覆盖公共窗口／同步覆盖、缓冲并发、停止与失败放行、真实跨期／横截面策略历史预热、Hybrid 历史字段转换及 EMA 接线。SDK 实体使用项目既有离线夹具；不构造或替代原生 EMA 运算。

完整 EMA 数值及全部原生 Live 装配回归仍须在已安装 Bomber 引擎的环境执行：

```bash
PYTHONPATH=. python -B -m tests.run_main_ema_acceptance \
  --only history-provider --only history-adjustment --only im-runtime \
  --only calendar-history --only cross-history --only calendar-live --only cross-live
```

真实数据库落库、行情并发和柜台联调须在目标运行机验证。同步应用文件时同时同步公共 `live/history.py`，原历史配置文件无需迁移格式。

[返回函数索引](../README.md)

## 单路模型历史的公共加载

```python
service = build_history_service(history_config=path, calendar=calendar,
    required_fields=("open", "high", "low", "close", "volume", "value", "open_interest"),
    missing_policy="allow")
result = warm_history_window(service, calendar=calendar, instrument=instrument,
    before_ns=clock_ns(), minutes=300, consume=warm_standard_history_bars)
```

`consume`负责一次性验证/转换业务输入并调用专用预热接口，不能用于实时开仓回调。
查询成功但覆盖不足时，`allow`保留实际可读分钟；已配置数据库读取失败仍报错。
显式传入`archive`只读取该归档的近期交易分钟，不替代调用方声明的数据库恢复源。
数据库与归档都报告整个请求窗口的`required/loaded/missing`，归档缺失政策也由服务决定。

`merge_history_rows`只处理分钟身份、排序、去重和冲突；模型身份验证及时间标签转换留在业务层。
共享层不读取Runner、predictor或workflow字段，也不创建第二套Provider/状态恢复流程。
