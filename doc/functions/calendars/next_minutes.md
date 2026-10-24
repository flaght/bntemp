# SessionCalendar.next_minutes

导入：`from bomber.framework.calendars import SessionCalendar`。源码：[sessions.py](../../../bomber/framework/calendars/sessions.py)。核对日期：2026-10-24。

```python
calendar.next_minutes(after_ns, count)
```

在日历定义的分钟桶中，返回结束时间严格大于 `after_ns` 的前 `count` 个交易分钟。返回值为有序 `tuple[int, ...]`，每个时间戳使用“分钟结束边界减 1ns”。不读取行情，不依赖本进程收到多少根 bar。

| 参数 | 含义 |
| --- | --- |
| `after_ns` | 非负整数纳秒时间戳，表示查询起点。查询完整已结束 bar 之后的分钟时，使用该 bar 的结束边界减 1ns。不是分钟开始标签。 |
| `count` | `0` 至 `calendar.max_minutes` 的整数，布尔值不接受；默认资源上限为 10000。 |

交易分钟由当前 SessionCalendar 的品种时段、交易日快照和日期例外共同决定。休市、周末和节假日跳过；夜盘所属交易日、跨午夜与取消夜盘使用同一套规则。覆盖不足、夜盘所属下一交易日未知时明确报错，不推测未来假期。

例如，正常螺纹钢交易日，已完成事件结束时间为 14:58：

```python
future_ends = calendar.next_minutes(completed_event_ns - 1, 5)
expires_at_ns = future_ends[-1] + 1
# 依次为 14:59、15:00、21:01、21:02、21:03 的结束边界（减 1ns）。
```

这里假设 `completed_event_ns` 已由行情层统一为 14:58 的结束边界；如果原始标签 14:57 表示分钟开始，完成时间才是 14:58，不能再对完成时间重复加一分钟。若 14:57 本来就是完成时间，五周期到期为 21:02。

本函数只确定日历中的到期位置，不保证某时点可成交，也不恢复批次、目标、账户或订单。HybridTransformer 的 [holding.py](../../../demos/10_hybrid_transformer/holding.py) 保存绝对期限；[checkpoint.py](../../../demos/10_hybrid_transformer/checkpoint.py) 接入统一状态管理器。

[返回函数索引](../README.md)
