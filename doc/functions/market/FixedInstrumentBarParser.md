# FixedInstrumentBarParser

用途：为已经确定真实合约的逐行 OHLCV 数据补充固定身份，转换为标准 Bar，并按显式配置生成携带额外字段的 CustomBar。

- 导入路径：`from bomber.framework.market.replay.parsers.bar import FixedInstrumentBarParser`
- 实际定义：`bomber.framework.market.replay.parsers.bar.fixed.FixedInstrumentBarParser`，是行解析器类。
- [固定身份解析器源码](../../../bomber/framework/market/replay/parsers/bar/fixed.py) · [委托的映射解析器](../../../bomber/framework/market/replay/parsers/bar/mapped.py)
- [解析上下文与标量转换](../../../bomber/framework/market/replay/parsers/base.py) · [Bar 构造及精度转换](../../../bomber/framework/market/basic/base.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个类负责什么

适用于调用方已经知道合约身份，但来源行没有 symbol/exchange 列的场景。例如，文件已经选定为某个真实合约，只需要将其中每一行转换为该合约的行情事件。

它不读取文件，而是接收 Reader 提供的行字典。`parse(row, context)` 复制行内容，补上内部固定 symbol/exchange，再委托 `MappedBarParser` 完成时间解析、OHLCV 校验、精度转换和事件生成。

默认每行只返回一个标准 Bar。显式映射伴生字段，或启用 `preserve_raw_ohlcv` 时，再返回一个 CustomBar，顺序始终为 Bar 在前、CustomBar 在后。

文件选择、交易日和合约身份审计、start/end 时间标签标准化通常由上游 `read_bar_frame` 执行。解析器不选主力、不复权、不聚合周期、不计算信号，也不启动 Feed 或回测引擎。

## 2. 完整构造签名

```python
def __init__(
    self,
    instrument_id: InstrumentId,
    *,
    timestamp: str = "datetime",
    bar_spec: str = "1-MINUTE",
    timezone: str = "Asia/Shanghai",
    available_timestamp: str | None = None,
    available_nanoseconds: str | None = None,
    value: str | None = None,
    open_interest: str | None = None,
    vwap: str | None = None,
    trade_time: str | None = None,
    preserve_raw_ohlcv: bool = False,
) -> None:
    ...
```

除 `instrument_id` 外均只能按名称传入。

| 参数 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `instrument_id` | `InstrumentId` | 必填 | 固定的真实合约身份，例如 InstrumentId.from_str("rb2610.SHFE") |
| `timestamp` | `str` | `"datetime"` | 行中的事件时间列名；应已经表示 Bar 完成时刻 |
| `bar_spec` | `str` | `"1-MINUTE"` | 固定周期描述，转大写后用于事件和 BarType；不执行重采样 |
| `timezone` | `str` | `"Asia/Shanghai"` | 解释没有时区的时间值，构造时通过 ZoneInfo 加载 |
| `available_timestamp` | `str` / `None` | `None` | 可选的到达/可用时间列名，按 ISO 时间解释 |
| `available_nanoseconds` | `str` / `None` | `None` | 可选的到达/可用时间列名，按整数 UTC epoch 纳秒解释 |
| `value` | `str` / `None` | `None` | 成交额来源列名，输出到 factors["value"] |
| `open_interest` | `str` / `None` | `None` | 持仓量来源列名，输出到 factors["open_interest"] |
| `vwap` | `str` / `None` | `None` | 已提供均价的来源列名，输出到 factors["vwap"] |
| `trade_time` | `str` / `None` | `None` | 来源时间标签列名，原值透传到 factors["trade_time"] |
| `preserve_raw_ohlcv` | `bool` | `False` | 将转换为合约精度前的 OHLCV 数值放入 factors |

这些可选字符串参数是**列名**，不是数值或时间常量。设置 `value="amount"` 表示每行必须有有效的 amount 值，并不是给成交额赋常量。

## 3. 固定合约与输入数据约定

### 固定身份不等于逐行核验身份

构造时应传 InstrumentId 对象；类本身不将普通字符串自动转换为该对象。每次解析会补入：

```text
__fixed_symbol__   = instrument_id.symbol.value
__fixed_exchange__ = str(instrument_id.venue)
```

原行即使带有 symbol/exchange 列，也不用于这里的身份选择或一致性检查。若将别的合约文件错误绑定到此解析器，它仍会按固定合约生成事件。因此，上游应先确认路径、行身份和目标合约匹配；`read_bar_frame` 提供的文件/key 校验与这里的事件构造承担不同职责。

输入行不被原地修改。`reset()` 委托内部解析器 reset，当前没有累计成交量、上一个时间戳或其他跨行状态需要清空。

### 必需列与数值规则

默认必需 `datetime`、`open`、`high`、`low`、`close`、`volume`。时间列可通过 timestamp 改名，OHLCV 列名在本解析器中固定，不能传 `open="OpenPrice"` 等参数。

OHLCV 名称不同的来源应先由 BarReadSpec.field_mapping 标准化，或直接使用 `MappedBarParser(BarColumns(...))` 声明完整映射。

| 字段 | 校验规则 |
| --- | --- |
| open / high / low / close | 可转有限 float，严格大于零 |
| volume | 可转有限 float，大于等于零；此解析器不强制源成交量为整数 |
| high | 不小于 open、low、close 的最大值 |
| low | 不大于 open、high、close 的最小值 |
| 配置的 value / open_interest | 必须存在，且为有限非负数 |
| 配置的 vwap | 必须存在，且为有限正数 |
| 配置的 trade_time | 必须存在且通过非空检查，随后原样透传 |

未配置的额外列不会自动提取。文件里即使有 amount、openint、vwap，也不会自动猜它们的用途，更不会把所有列复制到 factors。

## 4. 三种时间分别是什么

| 时间 | 来源 | 作用 |
| --- | --- | --- |
| `ts_event` | timestamp 指定列 | 当前 Bar 的事件/完成时间 |
| `ts_init` | 可用时间列；未指定则等于 ts_event | 到达或可用时间，供回放排序 |
| `factors["trade_time"]` | trade_time 指定列 | 保留来源原标签，不决定事件时间或可用时间 |

### 不会自动移动开始标签

解析器没有 timestamp_label 参数，也不会因 bar_spec="1-MINUTE" 自动加一分钟。若源 09:00 是开始标签，应先用 `BarReadSpec(timestamp_label="start")` 将完成时间变为 09:01，再让本解析器读取标准化后的 datetime。

当前 HybridTransformer 使用 `timestamp="datetime"`、`trade_time="source_timestamp"`，分别保留完成时刻和源标签，避免重复移动时间。

### ISO 时间与整数纳秒

timestamp 和 available_timestamp 经字符串转换后，用 Python `datetime.fromisoformat` 解析，支持常见 ISO 日期时间和结尾 Z。没有时区时使用配置的 timezone，带时区时按其实际时刻计算 UTC epoch 纳秒。

该路径基于 Python datetime 的微秒精度，不能据此承诺保留亚微秒来源时间。规范化完成分钟本来对齐分钟边界时不受此精度差别影响。

需要保留精确可用纳秒时，使用 `available_nanoseconds="available_ns"`。该列经 Decimal 验证为有限整数，再转 int；不会自动把毫秒或秒换算为纳秒。

若两个可用时间参数同时配置，会先解析 available_timestamp，再用 available_nanoseconds 覆盖结果。因此前者即使最终被覆盖，也必须可解析；通常只选择一种来源。

最终要求 `ts_init >= ts_event`，否则报错。仅提供 trade_time 不会改变 ts_init，也不验证该原标签是否早于完成时刻。

## 5. 精度转换与 preserve_raw_ohlcv

解析必须取得该合约的 `InstrumentMeta`。通过 Feed 使用时需提前 `feed.register_instrument(meta)`；这与 `backend.add_instrument(contract)` 注册模拟执行合约是两件事。

构造原生 Bar 时，make_bar 按 meta.price_precision 和 meta.size_precision 格式化数值，再创建 Price 和 Quantity。它按小数位格式化，不是根据 price_increment 任意倍数做跳价校正。

例如 meta 声明价格两位小数、成交量零位小数：

| 输入 | 原生 Bar | preserve_raw_ohlcv=True 时的 factors |
| --- | --- | --- |
| close = 100.567 | 100.57 | close = 100.567 |
| volume = 20.25 | 20 | volume = 20.25 |

这里“原始”表示数值已经通过 float 转换和检查，但还没有按合约小数位格式化；不保证原始字符串格式或任意 Decimal 的全部精度。

开启 preserve_raw_ohlcv 会将 open、high、low、close、volume 五个键放进 factors，即使没配置其他额外字段，也会生成 CustomBar。它不会阻止原生 Bar 进行精度转换。

本解析器允许非负小数源成交量；上游默认 BarReadSpec 则要求整数成交量。示例中的小数用于解释两层行为差别，并不表示上游严格规格会接受同样的原始数据。

## 6. Bar、CustomBar 与返回结果

```python
events = list(parser.parse(row, context))
```

返回 Iterable[ParsedEvent]，当前内部实现返回事件列表。每个 ParsedEvent 包含 data_type、instrument_id、payload、bar_spec。

| 配置 | 每行结果 |
| --- | --- |
| 全部伴生字段为 None，preserve_raw_ohlcv=False | 一个 DataType.BAR，payload 为原生 Bar |
| 配置任一 value/open_interest/vwap/trade_time，或开启 preserve_raw_ohlcv | 先一个 BAR，再一个 CUSTOM_BAR |

CustomBar 的 `.bar` 引用前面生成的同一个 Bar，不是再构造一份独立行情；两者默认具有相同 ts_event 和 ts_init。额外字段进入 `.factors`，按以下固定名称输出：

```text
value         ← value 参数指定的列
open_interest ← open_interest 参数指定的列
vwap          ← vwap 参数指定的列
trade_time    ← trade_time 参数指定的列
open/high/low/close/volume ← preserve_raw_ohlcv=True
```

所有配置字段校验完成后才返回列表。即使基础 Bar 已在内部创建，某个额外字段校验失败时也不会先返回一半事件。

`vwap` 只透传来源正值，解析器不会自动计算或检查它与成交额、成交量、乘数的一致性。需要该口径时由下游 `futures_bar_values` 显式计算、检查；零成交量虽可解析为 Bar，该辅助函数仍会因无法计算本周期 VWAP 而拒绝。

## 7. 当前策略为何同时需要两类事件

HybridTransformer 当前构造为：

```python
parser = FixedInstrumentBarParser(
    instruments[symbol],
    timestamp="datetime",
    available_nanoseconds="available_ns",
    value=value_column,
    open_interest=open_interest_column,
    trade_time="source_timestamp",
    preserve_raw_ohlcv=True,
)
```

其中 instruments[symbol] 为已注册合约身份；value_column 默认为 value，open_interest_column 默认为 openint。来源列 openint 最终输出为 factors["open_interest"]，由下游数值提取接口兼容使用。

原生 Bar 供 `NautilusMarketFeedAdapter` 推进撮合，CustomBar 供策略提取成交额、持仓量和未按合约精度格式化的 OHLCV。两者是不同类型的订阅事件，不应将 CustomBar.bar 再重复送入 Backend 推进一次。

FileReplayFeed 按 ts_init 和生成序号排序。同一行的两事件具有相同可用时间，因此在都被订阅时保留 Bar 在前、CustomBar 在后的顺序；Runtime 另外保证适配器与 Runner 的正确挂载顺序。

解析器负责生成事件，Feed 负责订阅过滤。只订阅 BAR 就不会收到 CustomBar；只订阅 CUSTOM_BAR，也不会因配置了伴生字段就自动让撮合端收到原生 Bar。两条消费者链路需要分别装配。

## 8. 调用示例

### 直接解析单行

以下示例需要可用的 Bomber 行情实体和对应依赖，不读取真实文件；ParserContext 中路径与行号仅用于错误定位。

```python
from pathlib import Path
from bomber.framework.market.basic.base import DataType, InstrumentId, InstrumentMeta
from bomber.framework.market.replay.parsers.bar import FixedInstrumentBarParser
from bomber.framework.market.replay.parsers.base import ParserContext

instrument_id = InstrumentId.from_str("rb2610.SHFE")
meta = InstrumentMeta(instrument_id, price_precision=2, size_precision=0)
context = ParserContext(
    path=Path("/example/rb2610_20260911.feather"),
    line=1,
    get_meta=lambda identity: meta if identity == instrument_id else None,
)
row = dict(
    datetime="2026-09-11T09:01:00+08:00",
    open=100.12, high=101.98, low=99.87, close=100.56, volume=20,
    amount=20000, oi=1234,
    published="2026-09-11T09:01:02+08:00",
    source_timestamp="2026-09-11T09:00:00+08:00",
)
parser = FixedInstrumentBarParser(
    instrument_id,
    available_timestamp="published",
    value="amount",
    open_interest="oi",
    trade_time="source_timestamp",
    preserve_raw_ohlcv=True,
)
events = list(parser.parse(row, context))
assert [event.data_type for event in events] == [DataType.BAR, DataType.CUSTOM_BAR]
assert events[1].payload.bar is events[0].payload
assert events[1].payload.factors["value"] == 20000
assert events[0].payload.ts_init - events[0].payload.ts_event == 2_000_000_000
```

### 将已标准化数据接入 Feed

沿用上一示例导入的类型与 meta，假设真实 Feather 已存在；需要 pandas/pyarrow 及相应运行环境。

```python
from bomber.framework.dataprep import BarReadSpec
from bomber.framework.dataprep.bars import PreparedFrameReader, read_bar_frame
from bomber.framework.market.replay.base import FileReplayFeed

path = Path("/ABS/PATH/TO/rb2610_20260911.feather")
prepared = read_bar_frame(path, BarReadSpec(timestamp_label="start"))
feed = FileReplayFeed("example-fixed-bars")
feed.register_instrument(meta)
parser = FixedInstrumentBarParser(
    instrument_id,
    timestamp="datetime",
    available_nanoseconds="available_ns",
    preserve_raw_ohlcv=True,
)
feed.add_source(path, PreparedFrameReader(prepared.frame), parser, "bar")
feed.subscribe(instrument_id, DataType.BAR, bar_spec="1-MINUTE")
feed.subscribe(instrument_id, DataType.CUSTOM_BAR, bar_spec="1-MINUTE")
feed.connect()
try:
    events = feed.load_events()
finally:
    feed.disconnect()
```

此例假设来源按开始标签记录；read_bar_frame 已完成一次转换，Parser 不再加一分钟。load_events 返回行情 payload 元组，不再包含 ParsedEvent 外层封装；实际回放分发使用 replay 或 replay_step。

## 9. 常见错误与能力边界

| 现象 | 重点检查 |
| --- | --- |
| `instrument is not registered` | ParserContext 或 Feed 是否能查到该合约的 InstrumentMeta |
| 传字符串身份后出现属性错误 | 显式使用 InstrumentId.from_str 创建对象 |
| `missing column value` | 默认 OHLCV、时间列或显式映射的伴生列是否存在且非空 |
| 数值非有限、负成交额/持仓量、非正价格 | 检查原行和映射列名，解析器不会填零或修复 |
| high/low 关系报错 | 检查源 OHLC 及列名，校验发生在合约精度格式化之前 |
| `bar availability must be integer nanoseconds` | 可用纳秒列是否为有限整数，是否误传小数或时间字符串 |
| `bar availability precedes completed bar time` | 是否把开始标签或错误单位当作可用时间 |
| 默认没有 CustomBar | 是否配置了伴生字段或开启 preserve_raw_ohlcv |
| 有 CustomBar 但字段不全 | 未映射列不会自动提取，preserve_raw_ohlcv 只加入 OHLCV |
| 解析生成了 CustomBar，但策略没收到 | Feed 是否订阅 CUSTOM_BAR，bar_spec 与合约身份是否匹配 |
| 时间偏移一分 | 上游是否已转换完成标签，是否误以为 Parser 会根据周期移动时间 |
| 原生数值与模型数值不同 | meta 精度会格式化原生值；需要较高源数值精度时检查 factors |
| 不能接收其他 OHLCV 列名 | 先标准化列名，或使用 MappedBarParser |
| 不同合约资料被标成同一合约 | 固定身份由调用方指定，Parser 不核验原行的 symbol/exchange |

常规字段和转换校验使用 DataLoadError，通常带 `path:line:message`；原生对象构造或类型错误在直接 parse 时也可能直接传播，FileReplayFeed 会为其他加载异常补充上下文。

本解析器没有跨行去重、排序、完整分钟对齐、交易日审计、累计量差分或坏值研究保留策略。这些应由上游 BarReadSpec、数据准备或业务逻辑完成。bar_spec="5-MINUTE" 只声明周期，不会把五行一分钟数据聚合成一行。

相关回归：[Bar 解析器测试](../../../tests/test_bar_parsers.py)，涵盖默认输出、伴生字段、三类时间、精度、坏值定位、Feed 配对顺序和实际入口装配；测试使用真实 Python 解析器与 Feed，原生 Cython 实体由替身替换，不等同于完整原生撮合回归。

相关实现：[FileReplayFeed](../../../bomber/framework/market/replay/base.py)、[期货字段提取与 VWAP 检查](../../../bomber/framework/market/futures.py)、[CustomBar 实体](../../../bomber/framework/market/basic/custom_bar.pyx)。实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

相关说明：[BarReadSpec](../dataprep/BarReadSpec.md)、[NautilusSimExecutionBackend](../trader/NautilusSimExecutionBackend.md)、[SessionCalendar](../calendars/SessionCalendar.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
