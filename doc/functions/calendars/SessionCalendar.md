# SessionCalendar

用途：将品种交易时段、交易日快照和日期例外组合成统一的会话日历，查询交易窗口、夜盘归属和完整交易分钟。

- 导入路径：`from bomber.framework.calendars import SessionCalendar`
- 实际定义：`bomber.framework.calendars.sessions.SessionCalendar`，是类，不是普通函数。
- [类及查询方法源码](../../../bomber/framework/calendars/sessions.py)
- [时段结构与解析](../../../bomber/framework/calendars/hours.py) · [交易日快照与 Provider](../../../bomber/framework/calendars/trading.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个类负责什么

它主要回答以下问题：

1. 某个日期是否属于已知交易日？
2. 某交易日有哪些实际交易窗口，包括属于它的前一交易日夜盘？
3. 当前时刻是否位于交易时段内，属于哪个交易日？
4. 某时刻之前或之后的若干完整交易分钟是什么？
5. 断点到当前时刻之间应有哪些日历分钟？

历史和实时消费者使用相同的分钟桶规则，午休、夜盘、跨午夜和日期例外由同一个服务处理。构造时接收已经准备好的对象，不直接读取 Feather、不连接数据库，也不加载本地编译日历扩展；只有主动调用相应 Provider 时才涉及来源加载。

它依据规则生成应有的交易分钟，不检查实际行情是否到达，不保证可成交，也不替策略发单或恢复账户状态。

## 2. 构造签名与参数

```python
from bomber.framework.calendars import SessionCalendar

calendar = SessionCalendar(
    hours,
    dates,
    binding,
    overrides=(),
    max_minutes=10000,
)
```

上面 `dates` 是交易日快照，传给构造参数 `calendar`。后两个参数只能按名称传入，构造函数签名为：

```python
def __init__(self, hours: TradingHoursSnapshot, calendar: CalendarSnapshot,
             binding: SessionBinding, *, overrides=(), max_minutes=10000):
    ...
```

| 参数 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `hours` | `TradingHoursSnapshot` | 必填 | 可复用的静态时段快照，包含一个或多个时段代码的完整分段 |
| `calendar` | `CalendarSnapshot` | 必填 | 交易日身份、版本、覆盖起止日期及有序交易日列表 |
| `binding` | `SessionBinding` | 必填 | 当前品种使用哪个时段代码、交易日日历和时区 |
| `overrides` | `SessionOverride` 可迭代对象 | `()` | 已知的日期例外集合；默认表示没有例外，不会自动查询或推导节假日前停夜盘 |
| `max_minutes` | `int` | `10000` | `last_minutes`、`next_minutes` 单次查询的最大分钟数；必须为正整数，不接受布尔值 |

`hours`、`calendar` 必须是标准快照对象。`binding.calendar_id` 必须与 `calendar.calendar_id` 完全一致；`hours` 中必须存在 `binding.hours_code`，缺失时不回退到其他品种。

`max_minutes` 不是每个品种一天的分钟数，也不限制 `missing_minutes` 的区间长度。查询窗口按需展开并缓存，不会在构造时预生成整个日期范围的分钟。

## 3. 三类输入对象

### hours：静态交易时段

`TradingHoursSnapshot` 保存 `(code, SessionSegment)` 行。经 `parse_trading_hours(frame, source=...)` 解析时，DataFrame 必须恰好包含以下七列，列名唯一：

| 字段 | 含义 |
| --- | --- |
| `code` | 时段规则代码，例如 `ZN`；匹配 `binding.hours_code` |
| `segment_id` | 同一代码内唯一的分段标识，例如 `day_1`、`night_1`；不能使用保留字 `DAY`、`NIGHT`、`ALL` |
| `date_anchor` | `TRADING_DAY` 表示所属交易日；`NIGHT_START` 表示夜盘起始锚点 |
| `start_day_offset` | 相对锚点的开始日期偏移，只接受整数 `0` 或 `1` |
| `start_time` | 开始时钟，按完整分钟对齐，例如 `"21:00"` |
| `end_day_offset` | 相对锚点的结束日期偏移，只接受整数 `0` 或 `1` |
| `end_time` | 结束时钟，按完整分钟对齐，例如 `"01:00"` |

跨午夜的 `21:00～次日01:00` 应声明开始偏移 `0`、结束偏移 `1`，不能仅填写更小的结束时钟。也可拆成午夜前后两个分段，但不能重叠。同代码、同分段标识重复会报错。

文件来源使用 `trading_hours.feather`，不要求时段侧 manifest；文件列名必须为小写 `code`。DolphinDB 来源适配器负责将 `Code` 映射为 `code`。时钟可使用受支持的字符串、`datetime.time` 或有明确单位的时长，不能把数值时间戳当成时钟。

### calendar：交易日快照

`CalendarSnapshot` 包含 `calendar_id`、`rules_version`、`start`、`end`、`days`、`backend`。`start/end` 表示已知自然日期覆盖范围；`days` 是其中非空、有序、唯一的交易日元组。覆盖内的非交易日可返回休市结果，覆盖外则报错，不能把“资料未知”当成休市。

正式运行通常从 `NativeCalendarProvider(...).snapshot()` 或 `ExportedCalendarProvider(...).snapshot()` 取得。导出日期日历使用 CSV 和配套 `.manifest.json`，与时段 Feather 的格式不同。

快照应覆盖查询所需的历史锚点和未来夜盘所属交易日。例如，查询某个交易日的完整窗口，有夜盘时还需要此前一个交易日；查询快照末端的夜盘可能需要尚未覆盖的下一交易日。

### binding：品种与规则绑定

```python
binding = SessionBinding(
    product="ZN",
    hours_code="ZN",
    calendar_id=dates.calendar_id,
    timezone="Asia/Shanghai",
    exchange="SHFE",
    asset_type="FUTURE",
    night_anchor_rule="PREVIOUS_TRADING_DAY",
)
```

前四项必填；`exchange` 默认 `""`，`asset_type` 默认 `"FUTURE"`，夜盘锚点规则默认且仅支持 `"PREVIOUS_TRADING_DAY"`。品种和时段代码会去除首尾空白并转大写；时区须为有效的 `ZoneInfo` 名称，资产类型只接受 `"FUTURE"` 或 `"OPTION"`。

期权可显式将 `hours_code` 绑定为选定标的的时段代码，但服务不会根据合约名推断标的关系。交易所字段也不会自动选择或替换 `calendar_id`。

## 4. 夜盘归属与日期例外

`TRADING_DAY` 分段锚定所属交易日自身。`NIGHT_START` 分段以该所属交易日的**前一个交易日**为起点，不等同于前一个自然日。例如，若快照声明周五和下周一是相邻交易日，周五21:00及其跨到周六凌晨的夜盘均归属下周一。

交易日列表只能确定归属关系，不能证明某天夜盘实际开放。取消夜盘、临时调整时段等需要完整的 `SessionOverride` 集合：

| 字段 | 值与含义 |
| --- | --- |
| `date_basis` | `"TRADING_DAY"` 按所属交易日匹配；`"NIGHT_START"` 按夜盘起始锚点日期匹配 |
| `date` | `datetime.date`，必须在快照范围内且属于其中的交易日 |
| `action` | `"CANCEL"` 取消；`"REPLACE"` 替换 |
| `target` | `"DAY"`、`"NIGHT"`、`"ALL"` 或已有分段标识 |
| `segments` | 替换分段元组，默认 `()`；取消时必须为空 |

取消 `NIGHT` 会同时去除午夜前后的夜盘段；分组替换作用于整组，空替换表示关闭该组。`NIGHT_START` 例外不能作用于 `DAY` 或 `ALL`。单分段替换必须保留分段标识和日期锚点。

同日适用例外相互冲突、替换后窗口重叠会报错，不依赖记录顺序静默选择。部分冲突和重叠在查询展开时才检查，构造成功不代表所有日期都已展开验收。

## 5. 主要方法与返回结果

| 方法 | 返回值与用途 |
| --- | --- |
| `is_trading_day(day)` | 传 `datetime.date`，返回 `bool`；超出快照覆盖范围抛出 `RuntimeError` |
| `previous_trading_day(day)` | 传 `date` 或 `"YYYYMMDD"`，返回严格早于该日期的最近交易日字符串；没有此前交易日时失败 |
| `windows(trading_day)` | 传 `date` 或 `"YYYYMMDD"`，返回按开始时间排序的 `tuple[TradingWindow, ...]`；覆盖内非交易日返回 `()` |
| `window(now)` | 传带时区的 `datetime`，返回包含该时刻的 `TradingWindow`；覆盖内休市时返回 `None` |
| `last_minutes(before_ns, count)` | 返回截至 `before_ns` 已完整结束的最近 `count` 个交易分钟戳，升序排列；不足时失败 |
| `next_minutes(after_ns, count)` | 返回分钟戳严格大于 `after_ns` 的前 `count` 个交易分钟，升序排列；不足时失败 |
| `missing_minutes(after_ns, before_ns)` | 返回断点之后、截至结束边界已完成的所有日历分钟戳；不对比实际收到的行情 |
| `provenance` | 属性，返回来源、规则指纹、日历 manifest 及证据说明的字典 |

`TradingWindow` 包含 `trading_day`（`"YYYYMMDD"` 字符串）、`start`、`end`（带绑定时区的 `datetime`）和 `segment_id`。交易窗口按 `[start, end)` 判断，恰好在结束边界时已经离开该窗口。

分钟查询使用非负整数 UTC 纳秒时间戳；`count` 为 `0` 至 `max_minutes` 的整数，不接受布尔值。`missing_minutes` 要求结束边界晚于断点，它的名称表示应补查的分钟候选，不表示这些分钟的行情一定缺失。

## 6. 分钟时间约定

每个分钟戳为**分钟结束边界减 1ns**。例如 `09:00～09:01` 这根完整分钟用 `09:01:00 - 1ns` 表示。

- 查询历史时，`before_ns` 使用实际截止时刻：`09:01:00` 可包含刚结束的第一分钟；`09:00:30` 不包含尚未结束的分钟。
- 查询未来时，若已完成 bar 的标准化结束边界为 `completed_event_ns`，调用 `next_minutes(completed_event_ns - 1, count)`。
- 将未来分钟戳换成绝对到期边界时，使用 `future_stamps[-1] + 1`。

源行情若按分钟开始标签记录，应由行情层先统一到完成时刻，不能把开始标签直接当成已完成时间，也不能重复加一分钟。这里的未来查询只依据日历规则，不等待对应行情到达。具体跨休市示例见 [next_minutes](next_minutes.md)。

## 7. 调用示例

### 使用内存快照理解构造与查询

以下是可独立运行的演示资料，显式声明三个交易日和简化时段，用于说明接口，不代表交易所正式日历或完整时段。

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo
from bomber.framework.calendars import (
    CalendarSnapshot, SessionBinding, SessionCalendar,
    SessionSegment, TradingHoursSnapshot,
)
from bomber.framework.calendars.sessions import datetime_ns

dates = CalendarSnapshot(
    calendar_id="example.declared",
    rules_version="example-v1",
    start=date(2026, 1, 9),
    end=date(2026, 1, 13),
    days=(date(2026, 1, 9), date(2026, 1, 12), date(2026, 1, 13)),
    backend="declared",
)
hours = TradingHoursSnapshot(
    rows=(
        ("ZN", SessionSegment("day", "TRADING_DAY", 0, "09:00", 0, "15:00")),
        ("ZN", SessionSegment("night", "NIGHT_START", 0, "21:00", 1, "01:00")),
    ),
    source="in-memory-example",
)
binding = SessionBinding("ZN", "ZN", dates.calendar_id, "Asia/Shanghai")
calendar = SessionCalendar(hours, dates, binding, overrides=())

windows = calendar.windows(date(2026, 1, 12))
assert windows[0].trading_day == "20260112"
assert windows[0].start.isoformat() == "2026-01-09T21:00:00+08:00"
assert windows[0].end.isoformat() == "2026-01-10T01:00:00+08:00"

zone = ZoneInfo("Asia/Shanghai")
now = datetime(2026, 1, 12, 14, 58, tzinfo=zone)
assert calendar.window(now).trading_day == "20260112"

completed_event_ns = datetime_ns(now)
future_stamps = calendar.next_minutes(completed_event_ns - 1, 5)
expires_at_ns = future_stamps[-1] + 1
# 后续结束边界依次为周一14:59、15:00、21:01、21:02、21:03。
assert expires_at_ns == datetime_ns(datetime(2026, 1, 12, 21, 3, tzinfo=zone))
assert len(calendar.last_minutes(completed_event_ns, 3)) == 3
```

### 正式入口从资料来源装配

已拥有 `hours`、`dates`、`binding` 时可直接构造本类；通常入口通过公共装配函数读取来源：

```python
from pathlib import Path
from bomber.framework.dataprep.session_inputs import SessionInputs, load_session_inputs

inputs = SessionInputs(
    product="ZN",
    calendar_id="china.sse",  # 显式选择已核对适用的日期规则。
    exchange="SHFE",
    backend="file",
    hours_file=Path("/ABS/PATH/TO/trading_hours.feather"),
    overrides_file=Path("/ABS/PATH/TO/session_overrides.feather"),
)
calendar = load_session_inputs(inputs)
```

此例默认使用原生日历 Provider，运行环境须具备对应编译扩展。也可显式传入导出日期日历 Provider。只有确认整个使用范围无日期例外时，才能用 `no_overrides=True` 替代例外文件配置；装配层不会将资料缺失自动视为没有例外。相关实现见 [session_inputs.py](../../../bomber/framework/dataprep/session_inputs.py)。

## 8. 规则指纹与常见错误

`calendar.provenance` 包含时段内容指纹、交易日历 manifest、绑定及例外组合的 SHA256，证据声明为 `static_snapshot_not_historical_revision_asof`。这些是静态快照身份，不证明历史上何时发布或生效；调用方仍需确认资料适用于请求区间。

`SessionRulesCheckpoint(calendar)` 可参与统一状态管理，恢复时比较规则指纹。更改时段、日期日历及覆盖、绑定或例外可能导致恢复失败，需要明确迁移；它不保存账户或策略状态。

| 现象 | 重点检查 |
| --- | --- |
| 绑定与日历身份不一致 | `binding.calendar_id` 与快照身份是否完全相同 |
| 缺少品种交易时段 | `binding.hours_code` 是否存在；文件是否错误使用 `Code` 列 |
| 日历覆盖不足或缺少此前交易日 | 快照是否保留夜盘锚点及历史分钟所需的更早交易日 |
| 日历未覆盖夜盘所属下一交易日 | 未来覆盖是否包含该夜盘所属交易日 |
| 休市时返回 `None` 或 `()` | 区分 `window` 与 `windows` 的返回类型；覆盖内休市不是错误 |
| 时间必须携带时区 | `window` 的参数应使用带时区的 `datetime` |
| 分钟偏移一分或多算一根 | 是否混用了源开始标签、完成边界和结束边界减1ns |
| 未来/历史完整交易分钟不足 | 是否越过快照范围，或请求数量超过现有完整分钟；不会返回不足数量的部分结果 |
| 节假日前仍生成夜盘 | 是否提供了权威日期例外；仅靠交易日列表不能推导停夜盘 |
| 例外冲突或窗口重叠 | 同日规则是否重复作用，分段替换后的时间范围是否相交 |

相关回归：[交易时段测试](../../../tests/test_trading_hours.py)、[输入装配测试](../../../tests/test_session_inputs.py)。实际使用入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
