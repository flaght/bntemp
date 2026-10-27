# PreparedFrameReader

用途：把已经准备好的 DataFrame 适配成回放源需要的逐行读取接口，复用内存中的标准化结果，避免重复读取和时间转换。

- 导入路径：`from bomber.framework.dataprep.bars import PreparedFrameReader`
- 实际定义：`bomber.framework.dataprep.bars.PreparedFrameReader`，是 Reader 类，不是读取文件的函数。
- [类及准备结果注册源码](../../../bomber/framework/dataprep/bars.py)
- [RowReader 协议](../../../bomber/framework/market/replay/readers/base.py) · [FileReplayFeed 调用逻辑](../../../bomber/framework/market/replay/base.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个类负责什么

公共数据准备先通过 `read_bar_frame` 读取、校验和标准化行情，得到 `BarLoadResult.frame`。回放 Feed 则需要一个 Reader，按行返回数据，再交给 Parser 构造行情事件。

PreparedFrameReader 将这两层连接起来：

```text
Feather 文件
    → read_bar_frame：读取、校验、标准化
    → BarLoadResult.frame
    → PreparedFrameReader：按行提供字典
    → FixedInstrumentBarParser 或其他 Parser：构造行情事件
    → FileReplayFeed：按订阅、可用时间排序并分发
```

本类不再次读取 Feather，不重新调整 start/end 时间标签，不校验行情值，也不生成 Bar 或 CustomBar。它只把已有表格交给后续行解析器。

## 2. 完整接口与参数

当前实现很短：

```python
class PreparedFrameReader:
    suffixes = frozenset({".feather"})

    def __init__(self, frame):
        self.frame = frame.copy(deep=True)

    def read(self, path):
        for index, row in enumerate(self.frame.to_dict("records"), 1):
            yield index, row
```

| 接口或参数 | 常用类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `PreparedFrameReader(frame)` | frame 为 pandas.DataFrame | 必填 | 保存一份 deep=True 的 DataFrame 副本 |
| `read(path)` | path 通常为 Path | 必填，但不使用其内容 | 返回逐行迭代器，与 RowReader.read 接口保持一致 |
| `suffixes` | frozenset[str] | 仅 `.feather` | 向 Feed 声明关联来源的扩展名 |
| `reader.frame` | DataFrame | 构造时的副本 | 当前读取内容；它是公开可变属性，不是冻结对象 |

按模块从 bars 导入；当前 `bomber.framework.dataprep.__init__` 没有将此类作为顶层导出。这个类没有 reset、start、connect、close 等生命周期方法。

## 3. 输入表与返回结果

### 不规定行情列，但下游 Parser 会规定

Reader 本身不要求 datetime、OHLCV 或 symbol 等列，也不检查列名唯一性、空表或数值类型。约定的输入是已经准备好的 DataFrame，通常直接传 `prepared.frame`。

如果后续使用 FixedInstrumentBarParser，表中就应满足它要求的时间、OHLCV 和已配置额外字段；若直接将不合规表传给本类，错误通常到 Parser 才出现。

### read 返回什么

```python
reader = PreparedFrameReader(frame)
rows = reader.read(path)
```

rows 是生成器，逐项产生：

```text
(1, {列名: 第一行值, ...})
(2, {列名: 第二行值, ...})
...
```

- 字典键为 DataFrame 列名，不包含原 DataFrame 索引。
- 编号始终从 1 开始，代表当前内存表的位置，不是原文件物理行号。
- 保留当前表的行顺序，不排序、不按日期筛选、不去重。
- 不主动将日期转成字符串、价格转成原生 Price 或数量转成 Quantity；具体标量形式遵循 pandas.to_dict("records") 的转换。
- 空表正常产生零行，不由 Reader 抛出空行情错误。

若上游已经排序、筛选或重置索引，这里的编号对应处理后的结果。Feed 用这个编号构造 ParserContext，最终错误信息里的 path:line 不能据此直接当作原文件行号。

## 4. path 为什么存在却不读取

RowReader 协议统一要求 `read(path)`，因此本类保留该参数。但具体返回内容完全由 reader.frame 决定，换一个 path 不会切换到另一个文件，也不会检查文件内容是否发生变化。

直接调用 reader.read 时，即使 path 不存在，也不会因为这个路径报错。但通过当前 FileReplayFeed 使用时，外围仍会：

1. 在 add_source 时依据 suffixes 检查扩展名，要求 `.feather`，后缀比较会转小写。
2. 在 load_events 时解析路径并检查该文件确实存在。
3. 把路径和 Reader 输出的编号交给 ParserContext，用于错误定位。

因此，内存 Reader 不读文件，不等于可以在 Feed 中随便传不存在的虚拟路径。标准调用应保留原始结果的 `result.path`。

如果需要真正从指定路径重新读取 Feather，应使用存储层的 FeatherReader；不要用修改 read(path) 参数的方法期待 PreparedFrameReader 刷新内容。

## 5. 复制、重复读取与内存开销

### 构造时复制表格

`frame.copy(deep=True)` 使 Reader 不直接持有调用方的同一个 DataFrame。通常在创建 Reader 后修改输入表的普通数值单元格，不会改变已经保存在 Reader 中的对应值。

这里采用 pandas 的深复制语义，不是对所有单元格对象执行递归 deepcopy。若 object 列中保存可变 list、dict 等对象，嵌套对象仍可能共享，不应将它理解为任意复杂对象完全隔离。

reader.frame 本身仍可修改。新一次 read 会读取它当时的内容，因此应避免注册数据源后再修改 Reader 内部表格；Feed 已缓存的事件也不会因这种修改自动失效。

### 每次调用都可从头读取

read 不保存跨调用游标。一个生成器消费完就结束，但重新调用 reader.read(path) 会创建新生成器，再从第 1 行开始编号。

生成器形式不表示底层按块读取：首次推进该生成器时，`to_dict("records")` 会先为整张表生成行字典列表，再逐项 yield。已有 DataFrame 副本之外，还会有这一整份记录列表的开销；不适合将其当成超大文件的分块读取器。

对普通标量行字典的修改不会写回 DataFrame；包含可变对象的单元格仍须遵守上述共享限制。实际生成和过滤的行情事件，后续还可能被 Feed 缓存。

## 6. 时间规则与其他组件的分工

| 组件 | 职责 |
| --- | --- |
| `BarReadSpec` | 声明源时间、字段、交易日和校验规则 |
| `read_bar_frame` | 执行读取与标准化，生成 datetime、event_ns、available_ns 等 |
| `PreparedFrameReader` | 将已经存在的列和值逐行提供给 Parser |
| `FixedInstrumentBarParser` | 固定合约身份，解析时间和字段，构造标准 Bar 与可选 CustomBar |
| `FileReplayFeed` | 调用 Reader/Parser，按订阅过滤并按 ts_init 排序事件 |

例如源 09:00 为一分钟开始标签，read_bar_frame 已将完成时间标准化为 09:01。Reader 原样提供该完成标签和 source_timestamp，不再加一分钟；Parser 读取 datetime 和 available_ns 即可。

Reader 不会根据列名猜测谁是事件时间、谁是可用时间，也不会检查 available_ns 是否早于 event_ns。上游读取规则与下游 Parser 的相应检查仍然需要保留。

## 7. 调用示例

### 直接读取内存表

以下示例需要 pandas，不读取路径所指文件：

```python
from pathlib import Path
import pandas as pd
from bomber.framework.dataprep.bars import PreparedFrameReader

frame = pd.DataFrame(
    [dict(close=100.5, volume=20), dict(close=101.0, volume=30)],
    index=[10, 20],
)
reader = PreparedFrameReader(frame)
frame.loc[10, "close"] = 999

rows = list(reader.read(Path("/example/not-read.feather")))
assert rows == [
    (1, {"close": 100.5, "volume": 20}),
    (2, {"close": 101.0, "volume": 30}),
]
# 原 DataFrame 索引10、20未写入行字典，输出重新按1、2编号。
assert list(reader.read(Path("/example/another-name.feather"))) == rows
```

### 当前策略注册已准备结果的方式

假设已有正确的真实文件 path、合约身份 instrument_id，以及完成合约元信息注册的 feed：

```python
from bomber.framework.dataprep import BarReadSpec
from bomber.framework.dataprep.bars import PreparedFrameReader, read_bar_frame
from bomber.framework.market.replay.parsers.bar import FixedInstrumentBarParser

prepared = read_bar_frame(path, spec=BarReadSpec(timestamp_label="start"))
parser = FixedInstrumentBarParser(
    instrument_id,
    timestamp="datetime",
    available_nanoseconds="available_ns",
    preserve_raw_ohlcv=True,
)
feed.add_source(
    prepared.path,
    PreparedFrameReader(prepared.frame),
    parser,
    "bar",
)
```

这里假设来源确实按开始标签记录，实际应按来源设置。add_source 注册 Reader/Parser 组合，不会立刻解析；仍需配置对应订阅、连接并触发回放。Parser 的伴生字段和 preserve_raw_ohlcv 决定是否额外生成 CustomBar，Reader 不参与该选择。

如果使用 `add_prepared_bar_source(feed, result, instrument_id)`，公共函数会执行用途、周期及身份检查，并在内部创建本 Reader 和标准执行 Parser。当前 HybridTransformer 为额外模型字段显式配置 Parser，因此直接调用 feed.add_source。

### SourceSpec.reader 的包装

`SourceSpec.reader()` 内部相当于 `PreparedFrameReader(self.result.frame)`，返回新的 Reader；此时还没开始迭代。对同一个 SourceSpec 多次调用 reader，每次都会再创建一份表格副本。

## 8. 常见问题与排查

| 现象 | 重点检查 |
| --- | --- |
| 传入文件路径后提示没有 copy 或其他类型错误 | 构造参数应是 DataFrame，不是文件路径或 BarLoadResult；传 result.frame |
| 更改 read(path) 路径，内容却没变 | Reader 只使用构造时保存的内存表，不按 path 重读 |
| 直接 read 能成功，Feed 却报路径不存在 | Feed 在 Reader 外围检查文件存在性 |
| Feed 拒绝 CSV 后缀 | 本 Reader 声明 suffixes 仅为 .feather；不要把它当成通用文件读取器 |
| 行号与源文件不同 | 编号对应当前表的位置，从1开始；原索引和上游处理前位置不保留 |
| 时间没有再加一分钟 | 属于正常行为，时间转换由 read_bar_frame 完成 |
| 出现缺列或坏值错误 | 检查上游准备和 Parser 配置，Reader 不校验或修复行情 |
| 空表没有报错 | Reader 直接输出零行，是否允许空数据由上游或业务决定 |
| 内存占用高 | 构造复制 DataFrame，迭代又整体生成 records；它不是分块读取方案 |
| 修改 Reader 后回放仍是旧事件 | Feed 可能已有事件缓存，Reader 修改不自动使其失效；正常流程应固定准备结果 |

本类没有专门的异常包装，错误通常由对象的 copy/to_dict 方法或下游 Parser 产生；通过 Feed 加载时，Feed 会按其逻辑补充文件和行上下文。

相关回归：[公共输入测试](../../../tests/test_dataprep_common.py) 中的 test_fixed_bundle_consumes_prepared_rows_without_second_shift，检查准备结果经 SourceSpec.reader 传出后不会二次移动时间。实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)、[期权 Delta 回测](../../../demos/09_option_delta/run_backtest.py)。

相关说明：[BarReadSpec](BarReadSpec.md)、[FixedInstrumentBarParser](../market/FixedInstrumentBarParser.md)、[input_session](input_session.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
