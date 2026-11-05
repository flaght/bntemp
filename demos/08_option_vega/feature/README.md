# 原始指数与MA特征完整例子

`generate.py`在本目录完成读取DolphinDB、计算MA和导出两个标准包，不调用`scripts`。
`index.json`是可编辑的来源/Schema示例，默认读取`dfs://min_bar/index_1min`的`000852`。
数据库连接、标准记录、manifest、文件读回及回测可见性使用公共框架。

所有特征代码均在本包，策略根目录不再保留`features.py`：

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 共用声明、信号字段选择、标准文件读取 |
| `generate.py` | 原始指数及MA的计算、导出和命令行入口 |
| `dataframe.py` | 业务DataFrame转换为标准特征记录，与具体公式无关 |
| `example_dataframe.py` | 可直接运行的完整调用示例：MA20/MA60组合、转换、导出与读回 |
| `example_dolphindb.py` | 真实DolphinDB指数输入及自写DataFrame组合公式的完整调用 |
| `backtest.py` | 特征可见时钟、分钟帧与原回测策略适配 |
| `index.json` | 本地来源及Schema声明示例 |
| `index_close.json` | 只读取close的精简来源声明，数据集为`index.close.minute` |

回测与Live均从`demos.08_option_vega.feature`导入声明/读取接口。
生成命令保持`python -m demos.08_option_vega.feature.generate`；原`python -m demos.08_option_vega.features`入口移除。

## 一次生成两套文件

在项目根目录、已安装项目依赖的Python环境执行：

```bash
python -u -m demos.08_option_vega.feature.generate \
  --connect --key 000852 --window 20 \
  --start '2026-09-01T00:00:00+08:00' \
  --end '2026-09-11T23:59:59+08:00' \
  --output-dir tests/results/features/index-000852-20260901-20260911
```

也可执行`python demos/08_option_vega/feature/generate.py`并传相同参数。
连接读取项目根目录`.env`/环境中的`DDB_MARKET_*`，兼容旧`DDB_*`。
`--connect`仅允许只读查询，没有CTP连接或报单。

源表代码若是`SH000852`，加`--source-key SH000852`；`--key 000852`仍是导出后供策略绑定的业务键。
其他库表/列名必须通过`--config /实际路径/index.json`显式声明，不按代码前缀猜测。
数据集版本默认`--dataset index.minute --schema-version 1`；若自有声明是版本3，须相应传3。
默认时间标签为分钟终点，须与真实数据核对；两个文件共用一次原表数据查询。

输出目录包含：

```text
index.csv
index.csv.manifest.json
index.csv.dataset.json
index-ma20.csv
index-ma20.csv.manifest.json
index-ma20.csv.dataset.json
summary.json
```

输出目录可移动；声明使用同目录相对路径。任一目标已存在时，在查询前拒绝覆盖。
计算前检查空数据及预热，失败时不生成空包。各文件包由公共导出器发布，并分别读回核对；
两套包不提供跨文件事务，遇到磁盘错误时查看已有文件并换新目录重试。
成功需看到`status=passed`，以及`index.file_roundtrip=passed`、`ma.file_roundtrip=passed`。

## Python调用接口

已在环境中配置连接凭证时：

```python
from importlib import import_module
from bomber.framework.dolphindb import DolphinDbConnectionConfig

example = import_module("demos.08_option_vega.feature.generate")
report = example.generate_features(
    start_ns=example.timestamp("2026-09-01T00:00:00+08:00"),
    end_ns=example.timestamp("2026-09-11T23:59:59+08:00"),
    output_dir="tests/results/features/index-000852-20260901-20260911",
    connection=DolphinDbConnectionConfig.from_env(purpose="market", read_timeout_seconds=30),
    key="000852",
    window=20,
)
```

此函数不隐式加载`.env`；调用方可先加载它，或直接注入`DolphinDbConnectionConfig`。
可选参数还包括`config,dataset,schema_version,source_key,max_rows,availability`。
改`window=5`即生成`index-ma5.csv`及`research.index.ma5.minute`，不需要新增Provider或文件格式。

`generate_features(..., window=None)`仅生成原始指数。原单指数API
`generate_index_features(args, start_ns, end_ns, connection=...)`保留原调用参数，可指定CSV/Parquet输出路径。
两个入口共用同一查询、业务键映射、时间保留、标准导出及读回流程；双文件模式只增加MA计算和发布。
单指数模式同样生成数据文件、`.manifest.json`、`.dataset.json`及目录下的`summary.json`。

返回值与`summary.json`统一使用相同结构：

```text
status                  passed
index                   原始指数文件报告
ma                      MA文件报告；单指数时为null
coverage                MA预热/缺口统计；单指数时为null
index_availability      原始指数可见时间口径
ma_availability         MA可见时间口径；单指数时为null
```

`index`和`ma`文件报告使用相同字段，包括`rows,schema_fingerprint,computation_version,sha256,
first_event_ns,last_event_ns,max_visibility_delay_seconds,file_roundtrip`。
原单指数API之前顶层的文件字段统一收进`index`，例如改读`report["index"]["file_roundtrip"]`；
预热与缺口统计读取`report["coverage"]`。
相同来源记录与配置下，两模式的原始指数内容及摘要一致；MA只按自己的计算完成时间记录可用性。

## 业务DataFrame转换接口

完整可运行例子在[example_dataframe.py](example_dataframe.py)，从项目根目录执行：

```bash
python -u -m demos.08_option_vega.feature.example_dataframe \
  --output-dir tests/results/features/dataframe-blend-example
```

也可运行`python demos/08_option_vega/feature/example_dataframe.py`并传相同参数。
示例使用120根合成一分钟数据，计算MA20、MA60及`(MA20+MA60)/2`；预热后输出61条记录。
不依赖DolphinDB或CTP。应看到`status=passed`、`synthetic=true`和`file_roundtrip=passed`。
输出`index-blend.csv`、`index-blend.csv.manifest.json`、`index-blend.csv.dataset.json`及`summary.json`。
已有文件时换新输出目录。替换`calculate_features()`中的业务公式，并同步输出字段与计算版本，即可参考相同转换流程。
真实输入的缺口、午休和跨日窗口规则由业务函数明确处理；此合成例子只覆盖一个连续上午时段。

### 从DolphinDB读取真实指数再计算

当前DataFrame计算只依赖`close`时，可改用精简声明`index_close.json`：

```bash
--config demos/08_option_vega/feature/index_close.json \
--dataset index.close.minute
```

该声明只映射`close`，仍保留业务键、日期/分钟映射和频率。`observed`模式不依赖`updateTime`，
因此省略`publication_time`；若要使用`source-time`，须先补充经确认的真实发布时间映射。
读取时可只提供`Code/date/minTime/close`四列，不需要OHLCV及`bar_projection`。
精简声明用于`example_dolphindb.py`与`read_index_dataframe`；`generate.py`和Live默认Bar投影路径
仍使用原`index.json`。使用独立数据集ID，保留已有`index.minute`的Schema身份。

如果先只提取指数DataFrame，使用包内的`read_index_dataframe`，传入连接配置对象：

```python
from importlib import import_module
import pandas as pd
from bomber.framework.dolphindb import DolphinDbConnectionConfig

feature = import_module("demos.08_option_vega.feature")
connection = DolphinDbConnectionConfig.from_env(purpose="market", read_timeout_seconds=30)
df = feature.read_index_dataframe(
    connection,
    "demos/08_option_vega/feature/index.json",
    dataset="index.minute", schema_version=1, key="000852",
    start_ns=int(pd.Timestamp("2026-09-01", tz="Asia/Shanghai").value),
    end_ns=int(pd.Timestamp("2026-09-12", tz="Asia/Shanghai").value)-1,
)
print(df.head())
```

环境变量需已配置，或由调用方先加载`.env`。
内部依次执行`load_feature_bundle(config, connection)`、`bundle.binding(...)`和公共Provider的`read(request)`。
默认声明对应`index_1min`；`key`是源表实际代码，若源表使用`SH000852`就传该值。
返回声明映射后的`open/high/low/close/...`列，整数纳秒索引为`ts_event`，保留全部源修订及
`key/available_ns/source_ns/received_ns/revision/quality_flags/source`元数据列。
这些列使用object保留Decimal、None及整数纳秒；未丢弃旧修订、补齐缺口或重写时间。
无记录时返回带声明列的空DataFrame；查询失败仍抛异常。

真实数据版在[example_dolphindb.py](example_dolphindb.py)。同步代码后，在项目根目录执行：

```bash
python -u -m demos.08_option_vega.feature.example_dolphindb \
  --connect --key 000852 \
  --start '2026-09-01T00:00:00+08:00' \
  --end '2026-09-11T23:59:59+08:00' \
  --output-dir tests/results/features/ddb-index-ma20-ma60-20260901-20260911
```

连接读取项目`.env`/环境的`DDB_MARKET_*`，兼容旧`DDB_*`。默认声明为本目录`index.json`，
来源是`dfs://min_bar/index_1min`。库表或字段不同时用`--config`选择实际声明；
源代码为`SH000852`时加`--source-key SH000852`，导出业务键仍为`000852`。
自有声明的数据集/版本不同时，相应指定`--dataset`和`--schema-version`。

流程对应源码的四步：

1. 公共Provider读取一次真实指数，负责DolphinDB字段和时间转换。
2. 声明组合数据集`research.index.ma20_ma60.minute`的`close/ma20/ma60/index_blend`字段。
3. 直接将`inputs`转为带时区DatetimeIndex的DataFrame，调用`calculate_features(frame)`，再显式调用`dataframe_to_records`；保留最近60行依赖的源时间、质量及实际完成时间。
4. 调用公共`export_features`生成CSV与manifest，保存声明后重新加载Provider逐记录读回；额外保存`summary.json`。

业务公式在`calculate_features()`中，输入仅有`close`列，带时区的DatetimeIndex为业务时间；
返回原索引的已完成特征行。修改公式时同步调整输出Schema、计算版本以及最大依赖窗口。
该真实数据示例采用`row-window`口径：MA20/60按排序后的记录行计算，跨午休、跨日或缺口延续；
前59行预热，不足60行时不会生成空包。这与`generate.py`默认的连续分钟分段MA例子是两个明确不同的业务口径。
成功需看到`status=passed`、`synthetic=false`和`file_roundtrip=passed`，记录数由真实数据覆盖决定。

此例保留当前计算快照语义：输出可用时间是本次计算完成时间，不能直接当作9月份当时已发布的特征。
已有原始查询错误仍须先定位源配置/库表；示例不把查询失败解释为空数据。

新增独立模块`dataframe.py`，接口为：

```python
dataframe_to_records(
    frame, spec, *, key, source, available_ns, source_ns,
    received_ns=None, revision=None, quality_flags=0,
)
```

`frame`是已经算好的业务DataFrame，`spec`是声明生成的DatasetSpec。转换器不计算公式、不连接来源，
也不猜测历史可用时间；结果是公共框架的`tuple[ExternalDataRecord, ...]`。
`moving_average_records`已使用这个接口，其他业务计算也可直接调用，不需要依赖MA函数。

完整调用示例（以下两行数值用于演示格式）：

```python
import json
import time
from pathlib import Path
from importlib import import_module
import pandas as pd
from bomber.framework.datahub.features import DatasetRegistry, dataset_from_definition
from bomber.framework.dataprep.features import export_features

convert = import_module("demos.08_option_vega.feature.dataframe").dataframe_to_records
definition = {
    "dataset_id": "research.index.blend.minute", "schema_version": 1,
    "computation_version": "business-blend-v1",
    "frequency": {"period_ns": 60_000_000_000, "timezone": "Asia/Shanghai"},
    "alignment": "EXACT",
    "fields": [{"name": "index_blend", "kind": "float", "minimum": "0", "exclusive_minimum": True}],
}
spec = dataset_from_definition(definition)
df = pd.DataFrame({"index_blend": [6500.5, 6501.2]}, index=pd.DatetimeIndex([
    "2026-09-01T09:31:00+08:00", "2026-09-01T09:32:00+08:00",
]))
completed_ns = time.time_ns()  # 实际业务计算完成之后取时钟。
records = convert(
    df, spec, key="000852", source="business:index-blend-v1",
    available_ns=completed_ns, received_ns=completed_ns,
    source_ns=pd.Series([stamp.value for stamp in df.index], index=df.index, dtype="Int64"),
)
output = Path("tests/results/features/custom-blend/index-blend.csv")
manifest = export_features(output, DatasetRegistry((spec,)), records)
# 回测读取所用声明，使用与转换相同的Schema。
with Path(str(output)+".dataset.json").open("x", encoding="utf-8") as stream:
    json.dump({"format_version": 1, "datasets": [{"schema": definition,
        "source": {"backend": "file", "path": output.name, "require_manifest": True}}]}, stream, indent=2)
```

输出为数据文件、同名`.manifest.json`和`.dataset.json`。回测传对应文件、声明、数据集ID，
并用`--index-value-field index_blend`选择这个字段。

转换约定：

- 列名对应Schema字段；只转换声明字段，其他辅助计算列不导出。类型、有限值及数值边界由公共解码器核验。
- 时间索引支持带时区DatetimeIndex；整数索引须命名`ts_event`，单位UTC Unix纳秒。若时间原在列中，先显式`df.set_index("ts_event")`。不接受无时区时间或float纳秒，不自行猜单位。
- `key/source/available_ns/source_ns/received_ns/revision/quality_flags`均可传标量或同索引Series。Series必须与DataFrame索引完全一致，不按位置猜测重排。
- 可空字段的NaN、pd.NA转为None；非空字段缺值仍报错。含空值的纳秒列使用`Int64`或object，避免先被Pandas转成float丢精度。
- `revision`省略时沿用`available_ns`；`received_ns`省略时保持None；质量默认0。时间和版本含义由调用方负责提供，不用转换时间覆盖历史时间。
- 同一时刻可有多个业务键或不同修订，但同一数据集/业务键/时刻/修订身份重复时拒绝。业务键必须为字符串，保留前导零。
- 保留输入行顺序，不补齐缺口或删去预热行；这些由业务函数先处理。空DataFrame返回空元组，公共导出器拒绝空包。

上例按本次计算时刻发布，是研究快照；若有真实历史发布时间，应按行提供其Series。转换器本身不能证明数据在过去已可见。

## 计算与时间规则

业务公式直接接收和返回DataFrame，不需要逐条创建`ExternalDataRecord`：

| 接口 | 职责 |
| --- | --- |
| `calculate_moving_average(frame, window)` | 纯计算；输入`close`及可选`segment_id`，返回`close/maN/window` |
| `moving_average_records(..., calculator=...)` | MA来源准备、分段、依赖窗口及计算时钟检查 |
| `dataframe_to_records(frame, spec, ...)` | 与公式无关的DataFrame→标准记录转换 |

适配层传入的DataFrame以整数纳秒`ts_event`为索引，`segment_id`区分缺分钟、午休和跨日后的连续段。
计算函数返回原索引的有序子集，列名与输出Schema一致；预热不足的行不输出。
MA先计算每行依赖的源时间、质量及完成时间，再调用`dataframe_to_records`，由公共`decode_record`核验类型，最后使用统一导出流程。
默认计算使用NumPy窗口及Decimal object数组，保留原34位精度；不将公式写进公共框架。

例如，业务函数可以用普通Pandas计算组合信号：

```python
def calculate_blend(frame, window):
    import pandas as pd
    close = frame["close"].astype(float)
    groups = close.groupby(frame["segment_id"])
    ma20 = groups.transform(lambda values: values.rolling(20, min_periods=20).mean())
    ma60 = groups.transform(lambda values: values.rolling(60, min_periods=60).mean())
    return pd.DataFrame({
        "close": close,
        "ma20": ma20,
        "ma60": ma60,
        "index_blend": (ma20 + ma60) / 2,
    }, index=frame.index).dropna()

records, coverage = moving_average_records(
    inputs, input_spec, blend_spec, key, 60,
    calculator=calculate_blend,
)
```

这里的`blend_spec`须声明`close/ma20/ma60/index_blend`四个float字段，并使用对应公式的独立计算版本，
例如`pandas-float64-ma20-ma60-half-v1`；不能复用默认Decimal MA的数据集身份。
传入60表示最大依赖窗口，适配层按这些输入检查可见性和质量。
该例演示自定义计算接口；默认生成命令仍生成单窗口MA。将组合公式接入生成入口时，
需一起选择其Schema和计算函数，导入导出与回测读取流程不需要另写一套。

MA20使用20根连续、已完成的一分钟收盘价，包含当前Bar，采用Decimal 34位有效数字。
缺分钟、午休或跨日重置窗口，每段前19根预热；不自动读取起点之前的数据。
原始指数保留全部分钟和修订，不因MA预热而裁剪。

本例是**当前计算的研究快照**：MA使用当前可见的最新输入修订，结果可用时间为实际计算完成时间。
原始文件保留Provider提供的可用/接收时间；只映射显式指定的业务键，不重写时间。
默认`observed`不会把今天读取的历史数据变成过去当时可用。
只有确认`updateTime`是该版本真实发布时间时，原表才可加`--availability source-time`；
这不会把MA改成历史发布时间，也不会把全量最新修订选择变成历史时点计算。

因此本例可验证生成、声明、读取和标准文件往返；今天生成9月份的研究快照，
不能直接当作9月份的时点回测输入。真实历史对照仍需历史可见记录或按当时可见版本推进的计算流程。

## 08读取及驱动决策

本包`__init__.py`负责声明加载、标准文件读取，`backtest.py`负责分钟帧；`run_backtest.py`使用这些接口。
在原完整回测命令中，原始指数使用：

```bash
--index-feature-file tests/results/features/index-000852-20260901-20260911/index.csv \
--index-feature-config tests/results/features/index-000852-20260901-20260911/index.csv.dataset.json \
--index-dataset index.minute
```

MA20使用：

```bash
--index-feature-file tests/results/features/index-000852-20260901-20260911/index-ma20.csv \
--index-feature-config tests/results/features/index-000852-20260901-20260911/index-ma20.csv.dataset.json \
--index-dataset research.index.ma20.minute \
--index-value-field ma20
```

上述读取仍受真实可见时间约束。MA文件还保留`close`字段，切换`--index-value-field close`可对比相同样本。
MA用于选约的指数筛选，IV/Greeks仍使用真实期权和同月期货价格。
完整回测参数见[08主说明](../README.md)，本例不另建回测引擎。

离线验证：`python -m unittest tests.test_option_feature_dataframe tests.test_option_feature_example -v`；
含SDK夹具的策略链路验证：`python tests/run_option_vega_offline.py`。
