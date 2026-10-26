# BarReadSpec

用途：声明原始 Bar 的字段、时间标签、时区、交易日和数值校验规则，供公共行情准备函数使用。

- 导入路径：`from bomber.framework.dataprep import BarReadSpec`
- 实际定义：`bomber.framework.dataprep.contracts.BarReadSpec`，是冻结数据类，不是读取函数。
- [配置结构源码](../../../bomber/framework/dataprep/contracts.py) · [读取与标准化实现](../../../bomber/framework/dataprep/bars.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个类负责什么

`BarReadSpec` 描述“如何解释和检查这份行情”，主要确定：

1. 必须有哪些数值字段，原始列名如何对应标准列名。
2. 哪一列是时间，源时间表示开始还是结束，使用什么时区和周期。
3. 文件交易日与时间戳自然日期之间采用什么约定。
4. 何时认为这一根 Bar 已经完成并可被使用。
5. 数值无效时立即失败，还是保留记录并附研究诊断。

构造配置不读取文件、不检查实际行情。`read_bar_frame(path, spec=spec)` 才读取 Feather 并执行规则。它也不重采样、补齐缺失分钟、生成信号或启动回测。

数据类为 `@dataclass(frozen=True)`。规范使用元组字段时可哈希，因此也参与输入会话的标准化缓存键。需要调整规则时创建新实例或使用 `dataclasses.replace`，不要直接修改属性。

## 2. 完整构造参数与默认值

```python
from bomber.framework.dataprep import BarReadSpec

spec = BarReadSpec(
    required_fields=("open", "high", "low", "close", "volume"),
    timestamp_column=None,
    timezone="Asia/Shanghai",
    timestamp_label="end",
    interval_seconds=60,
    trading_day_policy="exchange",
    value_policy="execution_strict",
    available_column=None,
    volume_semantics="incremental",
    require_integer_volume=True,
    require_minute_alignment=True,
    field_mapping=(),
)
```

所有字段均有默认值，`BarReadSpec()` 表示默认的一分钟执行 OHLCV 读取规格。

| 参数 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `required_fields` | `tuple[str, ...]` | OHLCV 五列 | 必须存在并执行数值校验的标准字段名 |
| `timestamp_column` | `str` / `None` | `None` | 显式时间列名；未指定时先查 `datetime`，再查 `timestamp` |
| `timezone` | `str` | `"Asia/Shanghai"` | 无时区时间的解释时区，以及带时区时间转换后的目标时区 |
| `timestamp_label` | `str` | `"end"` | 源时间是完成边界 `end`，还是开始边界 `start` |
| `interval_seconds` | `int` | `60` | Bar 周期秒数，至少为 1；用于开始标签转换和边界对齐检查 |
| `trading_day_policy` | `str` | `"exchange"` | 采用文件声明交易日，或严格要求同一自然日的 `day_session` |
| `value_policy` | `str` | `"execution_strict"` | 数值处理策略，另支持 `close_strict`、`research_audited` |
| `available_column` | `str` / `None` | `None` | 可选的实际可用时间列；未指定时假设完成即可用 |
| `volume_semantics` | `str` | `"incremental"` | 当前仅支持每根 Bar 的增量成交量 |
| `require_integer_volume` | `bool` | `True` | volume 被列为必需字段时，是否要求成交量为整数 |
| `require_minute_alignment` | `bool` | `True` | 完成时间是否必须按 interval_seconds 边界对齐 |
| `field_mapping` | `tuple[tuple[str, str], ...]` | `()` | `(标准列名, 原始列名)` 对组成的元组 |

字段名和策略值按实现精确匹配，建议沿用标准小写字段。类型注解不等于对任意类型都进行了运行时检查；元组字段应传元组，传列表可能在活动会话计算缓存键时产生不可哈希错误。

## 3. required_fields 与字段映射

### required_fields 控制检查范围

默认要求 `open`、`high`、`low`、`close`、`volume`。`execution_strict` 下不能省略其中任何一列，可以额外添加字段，例如 `open_interest`。

其他策略可显式缩小字段集合：

```python
close_spec = BarReadSpec(
    required_fields=("close",),
    value_policy="close_strict",
)
```

必需字段不只是存在性声明：读取器会对它们执行 `pd.to_numeric(errors="coerce")` 和数值校验。`volume`、`open_interest` 允许零，其他必需字段要求大于零；全部都要求有限数。

因此，不宜把可能合法为负或零的模型指标任意放进 `required_fields`。额外字段未列入其中时，一般保留，但不会自动获得相同的数值检查。当前 HybridTransformer 的 `value`、`openint` 由入口另外检查列是否存在，并映射给后续 Parser。

### field_mapping 的方向

映射对的顺序为 `(标准列名, 原始列名)`，不是常见 rename 字典的 `(旧名, 新名)`：

```python
mapped_spec = BarReadSpec(
    timestamp_column="datetime",
    field_mapping=(
        ("datetime", "DateTime"),
        ("open", "OpenPrice"),
        ("high", "HighPrice"),
        ("low", "LowPrice"),
        ("close", "ClosePrice"),
        ("volume", "MinuteVolume"),
    ),
)
```

读取时先重命名，再找时间列及必需字段，因此 `timestamp_column`、`required_fields` 和 `available_column` 都应使用映射后的列名。若原表同时有 `OpenPrice` 和 `open`，映射后出现重复列会失败。

每个标准列和原始列应明确一一对应。构造器没有全面检查映射冲突；实现先将元组转字典，重复标准键可能被覆盖，不应依赖这种行为选择列。

## 4. 时间标签、时区与周期

### timestamp_label：何时完成

对于周期 60 秒、源标签为 `09:00` 的行情：

| 设置 | 源标签含义 | 输出完成时刻 |
| --- | --- | --- |
| `timestamp_label="start"` | 统计从 09:00 开始的一分钟 | 09:01 |
| `timestamp_label="end"` | 统计到 09:00 结束的一分钟 | 09:00 |

`start` 将源时间加一次 `interval_seconds`；`end` 不移动。输出保留 `source_timestamp`，完成时间写入 `datetime`、`event_ns`、`bar_ns`。已经标准化的完成时间不能再次按 start 加一分钟。

这里的 `event_ns` 是**完成边界本身**，不同于 `SessionCalendar` 分钟查询使用的“结束边界减 1ns”。将已完成 Bar 接入 `next_minutes` 时，按日历接口要求传 `event_ns - 1`。

### timezone：本地化与转换

无时区时间按 `timezone` 本地化；已经带时区的时间转换到指定时区，保留同一绝对时刻。例如上海 `09:30` 与 UTC `01:30Z` 表示相同时刻。

输出纳秒通过 Timestamp 的 `.value` 获取，避免把来源数组的微秒存储单位直接误当纳秒。建议来源使用语义明确的 datetime 或带时区字符串；读取器没有“自动识别秒/毫秒 epoch”的配置参数，不能随意将数值时间列当成秒或毫秒输入。

### interval_seconds 与对齐

`require_minute_alignment=True` 时，完成纳秒必须能被 `interval_seconds × 10^9` 整除。默认 60 秒意味着整分钟边界，`09:30:01` 会失败。关闭该选项仅跳过此检查，不会把时间自动取整。

`interval_seconds` 不会将一分钟行情聚合成五分钟。即使规格可声明其他周期，现有 `add_prepared_bar_source` 执行注册入口仍要求 60 秒，与现有一分钟绑定一致；不能仅改该参数就认为整条回测链支持任意周期。

## 5. trading_day_policy：交易日如何确定

没有显式 `key` 时，`read_bar_frame` 从 `真实合约_YYYYMMDD.feather` 推导默认期货身份和交易日；其他资产或明确身份可传 `BarFileKey`。规格本身不含品种或交易日参数。

若数据存在 `trade_date`，会先检查全部记录是否与 `key.trading_day` 一致。两种政策还有以下差别：

| 政策 | 具体规则 | 典型适用数据 |
| --- | --- | --- |
| `exchange` | 信任上游文件或显式 key 的交易日声明；没有 trade_date 时，源自然日期不能晚于声明交易日 | 已按交易日归档、可能含前一自然日期夜盘的商品行情 |
| `day_session` | 源时间和转换后的完成时间，其自然日期均须等于声明交易日 | 已确认只有同日日盘的数据 |

例如 `RB2610_20260911.feather` 中的 `2026-09-10 21:01`，可以在 exchange 约定下归属 9 月 11 日；day_session 会拒绝。又如同日 `23:59` 的 start 标签加一分钟跨到次日，day_session 仍会拒绝。

`exchange` 不代表接入了交易所日历，也不会按固定自然日偏移重建夜盘归属。缺少 trade_date 时，活动输入会话会记录 `exchange_trading_day_trusted_from_file_identity` 假设。休市、节假日前停夜盘和精确交易窗口需要另外使用可靠的日历资料。

## 6. available_column：完成时间与可用时间

Bar 在 `09:01` 完成，不等于资料一定在 `09:01` 到达。两者用不同字段表达：

- 未指定可用时间列：`available_ns = event_ns`，表示假设完成即可用。
- 指定 `available_column="published"`：该列按同一时区规则解析，写入 available_ns / available_datetime。
- 可用时间早于完成时间：抛出 `INVALID_TIMESTAMP`。

```python
delayed_spec = BarReadSpec(
    timestamp_label="start",
    available_column="published",
)
# 源 datetime 为09:00，完成时间为09:01；published 可为09:01:02。
```

读取器会先保存原始可用时间列的值，再写入规范化时间字段，避免重命名后的输出覆盖来源值。后续标准执行 Parser 使用 available_ns 作为初始化/到达时间依据。

结果按 `available_ns`、`event_ns` 稳定排序后，若事件时间发生倒退，会抛出 `UNSUPPORTED_CAPABILITY`。显式可用时间支持延迟声明，但不代表支持任意乱序、迟到或跨源同步轨迹。

## 7. value_policy：三种数值策略

| 策略 | 字段要求与坏值行为 | 使用边界 |
| --- | --- | --- |
| `execution_strict` | 构造时要求至少 OHLCV；必需字段数值无效立即失败 | 标准执行 Bar 输入 |
| `close_strict` | 必需字段由调用方声明；所有声明字段无效时仍立即失败 | 常用来只读研究收盘价，应同时显式设置 required_fields=("close",) |
| `research_audited` | 必需字段数值无效时保留该行，生成 WARNING 诊断和 input_quality 标记 | 由研究选约逻辑识别并拒绝坏候选，不可直接当执行行情 |

`close_strict` 不会自动减少默认 OHLCV 字段；仅写 `BarReadSpec(value_policy="close_strict")` 仍然沿用五个默认字段。

三种策略对必需字段的基础数值规则相同：要求有限；volume / open_interest 非负，其他字段严格为正；volume 默认还必须是整数。

`research_audited` 会将无法数值化的内容转成 NaN，负数等可数值化坏值仍保留，并通过 `BarLoadResult.issues` 和 `input_quality="INVALID_BAR_FIELDS"` 标记。它不填零、不前填价格，也不自动删除坏行。

**研究审计模式并非忽略所有错误。** 空文件、重复列、缺字段、身份不符、时间无效和重复完成时间等仍然失败；当 required_fields 同时包含 OHLC 四列时，违反高低价关系也仍然抛出 `INVALID_OHLC`。

标准执行注册函数 `add_prepared_bar_source` 要求 `execution_strict`，不能只通过改变读取策略就把研究审计结果直接送入执行。

## 8. 成交量与 OHLC 约束

`volume_semantics` 当前仅接受 `incremental`。这是一项数据语义声明，不会替调用方判断来源是否真的为增量。累计成交量需要在上游按明确的交易日切换和重置规则转换；传 `cumulative` 会在构造时失败，不会自动差分。

`require_integer_volume=True` 且 volume 属于必需字段时，必须为非负整数，浮点存储的 `1.0` 可以通过，`1.5` 不可以。设为 False 可允许非负小数，但仍执行有限性和非负检查；下游合约数量规则可能另有要求。

零成交量允许保留。结果若包含 volume 列，会添加 `zero_volume_bar` 标记；这不表示最后成交时间发生了更新。

required_fields 同时包含 OHLC 时检查：

```text
high >= max(open, low, close)
low  <= min(open, high, close)
```

仅存在额外 OHLC 列但没有全部列入 required_fields，不会自动执行该完整关系检查。标准执行规格因强制要求 OHLCV，会执行此检查。

## 9. 返回结果与调用示例

`BarReadSpec(...)` 返回规格对象；`read_bar_frame` 返回 `BarLoadResult`，包含 `key`、绝对路径 `path`、规范化 `frame`、采用的 `spec` 和诊断元组 `issues`。Frame 主要新增或规范化以下字段：

| 字段 | 含义 |
| --- | --- |
| `source_timestamp` | 已按指定时区解释的源时间 |
| `datetime` | Bar 完成时间 |
| `event_ns`、`bar_ns` | 完成边界的 UTC epoch 纳秒 |
| `trading_day` | 文件或显式 key 声明的交易日 |
| `available_ns`、`available_datetime` | 可使用或到达时间 |
| `input_quality` | research_audited 模式下的逐行质量标记 |
| `zero_volume_bar` | 有 volume 列时的零成交标记 |

输入会话按文件版本、key 和完整 spec 缓存准备结果，返回帧使用深拷贝。规格不同会分别标准化，但可复用原始 Feather 读取缓存。

### 当前策略的一分钟执行规格

以下代码需要真实存在且格式正确的 Feather 文件：

```python
from pathlib import Path
from bomber.framework.dataprep import BarReadSpec
from bomber.framework.dataprep.bars import read_bar_frame
from bomber.framework.dataprep.session import input_session


@input_session
def prepare_bars(path, *, source_label):
    spec = BarReadSpec(timestamp_label=source_label)
    return read_bar_frame(path, spec=spec)


if __name__ == "__main__":
    loaded = prepare_bars(
        Path("/ABS/PATH/TO/rb2610_20260911.feather"),
        source_label="start",  # 前提：该来源确实按分钟开始标记。
    )
    frame = loaded.frame
```

HybridTransformer 实际使用 `BarReadSpec(timestamp_label=bar_timestamp)`，入口默认 bar_timestamp 为 end，其余参数沿用规格默认值。源数据的时间约定需要独立确认，不能由默认值证明。

### 仅检查收盘价或保留研究坏值

```python
from bomber.framework.dataprep import BarReadSpec

close_spec = BarReadSpec(
    required_fields=("close",),
    value_policy="close_strict",
)
research_spec = BarReadSpec(
    required_fields=("close", "volume", "open_interest"),
    value_policy="research_audited",
    trading_day_policy="day_session",
)
```

研究结果的质量标记应由下游研究逻辑消费，不能将无效值直接用于选约、模型计算或撮合。

## 10. 构造校验与常见错误

构造时检查 timestamp_label、interval_seconds 下限、交易日政策、数值政策、增量成交量声明、required_fields 重复以及 execution_strict 的 OHLCV 完整性，不会提前校验路径、列是否存在、时区可用性或整份行情。

| 现象 | 重点检查 |
| --- | --- |
| `Invalid completed-bar time specification` | 标签是否为 start/end，周期是否至少为1 |
| `Unknown trading-day policy` / `Unknown value policy` | 是否使用支持的精确字符串 |
| `Duplicate required fields` | required_fields 是否重复 |
| `Execution bars require OHLCV` | execution_strict 是否省略了 OHLCV 必需列 |
| 累计成交量声明报错 | 是否已在上游完成显式增量转换 |
| `MISSING_FIELD` / `INVALID_SCHEMA` | 检查原列、映射方向、映射后的时间列名、重复列和空文件 |
| `INVALID_TIMESTAMP` | 检查周期边界、空时间以及可用时间是否早于完成时间 |
| `DUPLICATE_BAR` | 同一文件中是否存在重复的完成时间 |
| `IDENTITY_MISMATCH` | 文件/key 的合约、交易日、可选 Venue 与行内容是否一致，交易日政策是否合适 |
| `INVALID_BAR_FIELDS` | 必需数值是否有限、符号是否允许、成交量是否为整数 |
| `INVALID_OHLC` | 高低价是否覆盖开收盘，是否有错误列映射 |
| `UNSUPPORTED_CAPABILITY` | 到达排序是否导致事件时间倒退，或执行注册周期是否不是60秒 |
| `INVALID_SOURCE_PURPOSE` | 是否将研究读取结果送入仅接受 execution_strict 的标准执行注册入口 |
| 活动会话出现不可哈希错误 | required_fields、field_mapping 及其中的映射对是否均按元组声明 |

构造约束通常抛出 ValueError；读取阶段通过 fail 抛出的结构化错误为 InputError，但 pandas 时间解析、时区或对象类型错误也可能直接传播。通过规格校验不代表行情覆盖完整，间隔缺口也不等于缺失交易分钟；交易分钟完整性需要结合日历单独判断。

相关回归：[公共行情输入测试](../../../tests/test_dataprep_common.py)。实际调用：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

相关说明：[input_session](input_session.md)、[SessionCalendar](../calendars/SessionCalendar.md)、[prepare_role_research](prepare_role_research.md)、[数据准备 API 用法](../../../bomber/framework/dataprep/API_USAGE.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
