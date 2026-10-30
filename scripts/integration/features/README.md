# 自定义分钟特征：构建、导出、回测和Live

这个例子使用 `example.bar_return.minute`：一分钟内从开盘价到收盘价的收益率，
`return_1m = (close - open) / open`，不是相邻两根Bar的close收益率。
配置见[minute_return.example.json](minute_return.example.json)，教学入口见[example.py](example.py)。
Payload由声明式Schema生成，字段是有限float，实例不可变；普通已计算特征只需增加声明和映射。
Schema不执行计算公式；新算法由计算函数实现，已有数据库结果直接映射。
没有新增特征专用Provider、Feed、Channel，没有数据库写入或策略报单。

Live 示例和只读探针默认读取 `DDB_MARKET_*` 连接，逐项兼容旧 `DDB_*`。
`scripts.integration.features.read_only` 查询参考类特征时可加 `--dolphindb-purpose reference`，
改用 `DDB_REFERENCE_*`；连接示例见[双服务器配置](../../../demos/market_sources/dolphindb.env.example)。

## 从真实指数分钟行情计算MA5并导出

2026-10-30新增的`export-ma`复用本目录教学入口和公共Provider：

```text
DolphinDB index_1min → index.minute记录 → 收盘价连续5根求均值
    → 声明生成的不可变Payload → 标准CSV/Parquet + manifest + 文件数据集配置
```

在远程Linux的`pro`目录、`uv-nautilus`环境中运行（示例区间为此前已核验有数据的日期；
可替换为库中其他已结束的区间）：

```bash
python -u -m scripts.integration.features.example export-ma \
  --connect --key 000852 --window 5 \
  --start 2026-10-12T14:18:00+08:00 \
  --end 2026-10-12T14:24:00+08:00 \
  --output tests/results/index-000852-ma5-20261012.csv
```

连接自动读取项目`.env`的公共market用途配置，源映射默认来自
`bomber/framework/dataprep/features/configs/features.json`，实际源为`dfs://min_bar/index_1min`。
可用`--config`、`--dataset`、`--schema-version`选择其他包含数值close字段的一分钟数据集。
它只读取数据库，不创建表或写回。输出已存在时换新路径，不覆盖历史文件。

本例公式为`MA5[t] = (close[t-4] + ... + close[t]) / 5`，包含当前已收盘Bar。
使用Decimal、34位有效数字计算，不提前按交易价格精度舍入。
缺分钟、午休间隔或跨日清空窗口，每段前四根仅预热，不输出不完整MA5。
因此它是“连续分钟段内MA5”；若策略需要跨午休延续五根交易Bar，应按真实会话日历
另外明确窗口口径，不能直接把缺失Bar当作休市。查询不会自动向start之前读取预热。
若上述区间仍为此前7条连续Bar，则输出应为14:22—14:24三条（未重新连接验证）。

默认导出数据集`research.index.ma5.minute`，Payload为：

```python
{"close": Decimal("..."), "ma5": Decimal("..."), "window": 5}
```

三个文件须一起保留：

```text
index-000852-ma5-20261012.csv
index-000852-ma5-20261012.csv.manifest.json
index-000852-ma5-20261012.csv.dataset.json
```

`.dataset.json`保存完整声明式Schema和相对文件路径，可直接装配文件Provider。
命令完成后会逐记录读回验证，并打印`file_roundtrip=passed`、预热条数、窗口重置/缺口和最后三条值。
把输出扩展名改为`.parquet`即可走同一导出接口；改`--window 10`则生成MA10和相应Schema，
不新增Python文件、Provider或Payload类。

读回例子（查询截止时刻取导出之后，用于校验文件；策略回测须使用自己的模拟时钟）：

```python
import time
from bomber.framework.dataprep.features import load_feature_bundle, FeatureRequest

bundle = load_feature_bundle("tests/results/index-000852-ma5-20261012.csv.dataset.json", None)
with bundle.provider("research.index.ma5.minute") as provider:
    now = time.time_ns()
    batch = provider.read(FeatureRequest(
        "research.index.ma5.minute", 1, ("000852",),
        start_ns=0, end_ns=now, as_of_ns=now,
    ))
    for record in batch.records:
        print(record.ts_event, record.payload.close, record.payload.ma5)
```

本例是当前历史快照的研究导出：输入最后更新时间不是首次可用证据，输出的`available_ns`
取本次输入观测后的实际计算完成时刻，保留源质量标记并生成独立的计算修订，
报告输入来源、Schema和记录内容指纹，不改成历史分钟结束时间。
本例选取源记录的当前最新可见修订，不声称导出了完整的历史修订序列。
文件可以正常导入，但不能以此声称这些MA5在原历史分钟当时就已可见。
在策略中逐Bar计算MA指标仍可使用指标模块；只有需要独立交付的研究数据才需要此导出。

新增四项离线用例，目标Linux环境执行：

```bash
python -u -m unittest tests.test_feature_example_ma -v
```

本机只静态审阅，未运行新用例、脚本、Python或数据库；不重复此前核心已通过组。

## 1. 从已收盘Bar构建

`example.py`的`build_feature()`使用通用类型；核心代码为：

```python
spec = dataset_from_definition(schema_definition)
payload = spec.payload_type(return_1m=(close - open) / open)
record = ExternalDataRecord(
    dataset_id=spec.dataset_id, schema_version=1, key="000852",
    payload=payload, ts_event=minute_end_ns,
    available_ns=calculation_completed_ns,
    source_ns=minute_end_ns, revision=revision,
    source="my-bar-computation",
    received_ns=calculation_completed_ns,
)
```

`minute_end_ns`是Bar结束标签；计算完成时刻必须晚于输入实际可见时刻。
Live计算完成后取实际`time.time_ns()`，回测使用模拟时钟；不要把分钟标签当作实际可用时刻。
修订与Schema版本分开管理，重复相同输入不要人为产生新修订。
增加基差、波动率等已计算字段时，把新的dataset加入同一清单，通用Provider不增加文件。

## 2. 导出

在远程Linux的`pro`目录和`uv-nautilus`环境中执行：

```bash
python -u -m scripts.integration.features.example build \
  --output tests/results/minute-return-example.csv
```

此命令用三个**合成**Bar构建三条特征，输出值预期为`0.01、-0.01、0.005`。
示例时钟为2026-10-12上海13:01/13:02/13:03，市场Bar在结束后0.2秒可见；
三个特征分别在结束后0.4秒、6秒、0.4秒可见。它们是教学数据，不是当天真实行情。
第二条故意晚到以演示超时规则。

通用导出API是`export_features(path, registry, records)`，自动生成
`minute-return-example.csv.manifest.json`，保存Schema指纹、内容摘要和标准时间规则。
改扩展名为`.parquet`即可使用同一接口导出Parquet（目标环境需有Parquet引擎）。
正式读取须同时部署数据文件与manifest；已有目标拒绝覆盖，重复演示请换新输出路径。

## 3. 回测导入和对齐

```bash
python -u -m scripts.integration.features.example backtest \
  --input tests/results/minute-return-example.csv
```

入口使用`FeatureProviderFactory.create_for("offline_feature", "file", path, registry)`
读取并核验manifest，然后交给`replay_feature_frames()`。
预期顺序为13:01 `READY`、13:02 `SKIPPED`、13:03 `READY`。
等待期限从市场Bar接收时刻起算5秒，第二条特征在6秒才到，跳过后不补旧决策。
文件提前载入内存不代表提前可见；模拟队列只在`visible_ns`到达时向DataHub发布。

这是特征接入/对齐的回放例子，没有策略交易和收益报表。
真实策略回测把自己的完整市场Bar帧提供给同一个对齐入口，而不是换一套特征规则。
不要把真实库以`observed`下载的历史记录改写成历史分钟结束即已可用。

## 4. Live需求声明与刷新

Live例子使用已核验的真实`dfs://min_bar/index_1min.pct_change`，
在三条实样中该字段与分钟内收益率一致；直接声明映射为`return_1m`。
和合成示例共享同一Schema，来源身份不同。新增已计算特征时主要改JSON。
`volume/turnover`累计语义未确认，本例不消费它们；`updateTime`是最后更新时间，
因此保持`observed`，不把它当作首次发布时间。

交易时段执行：

```bash
python -u -m scripts.integration.features.example live \
  --connect --key 000852 --seconds 180
```

入口自动读取项目`.env`，使用已有DolphinDB连接；只读当前上海日历日09:31—15:00范围。
Live模式只监视当前已收盘槽的`WAITING/VISIBLE`及修订变化，不产生策略决策或交易目标，
不自动跨日或声明休市日历。当天数据尚未写入时显示等待，源查询/类型/质量/过期错误会失败。

当前外部特征的“订阅”由需求声明和公共轮询缓存组成：

```python
bundle = load_feature_bundle(config_path, connection)
cache = FeatureQueryCache(
    bundle.provider("example.bar_return.minute"),
    dataset_id="example.bar_return.minute", schema_version=1,
    keys=("000852",), start_ns=day_start_ns, end_ns=day_end_ns,
    poll_seconds=0.2,
)
requirements = (FeatureRequirement(
    "minute_return", "example.bar_return.minute", 1, "000852",
    alignment=Alignment.EXACT,
),)
cache.open()
cache.poll()  # 控制线程调用，生命周期结束时cache.close()
snapshot = cache.hub.feature_snapshot(
    requirements, FeatureQuery(bar_end_ns, decision_now_ns),
)
value = snapshot.latest("minute_return").payload.return_1m
```

只有`cache.poll()`读取数据库；策略`on_bar`等回调查询DataHub。
当前没有原生数据库推送订阅或`subscribe_feature()`API，不能把轮询称作SDK流订阅。

策略决策需要有限等待时，使用与回测相同的装配器：

```python
assembler = FeatureFrameAssembler(cache.hub, requirements, wait_ns=5_000_000_000)

# 市场回调收到完整、已收盘、未重复的Bar帧：
assembler.offer(frame_id, bar_end_ns, market_received_ns, {"bar": market_bar})

# 运行控制线程持续刷新，并用查询真正完成后的时刻推进期限：
cache.poll()
for outcome in assembler.poll(time.time_ns()):
    if outcome.status == "READY":
        value = outcome.frame.features.latest("minute_return").payload.return_1m
        # 策略读取value并决定目标。
    else:
        # 记录SKIPPED及reason，保持正常指标推进，不补发该旧帧目标。
        pass
```

如果Live希望在本地从Bar实时计算，则在Bar回调复用`build_feature()`，
计算完成后通过`hub.features.store.publish((record,))`发布，再推进同一装配器。
这与选择数据库已计算结果是两种显式来源，消费方同一数据集/业务键选择其中一种。
数据来源、计算版本、首次可见时刻和修订身份须保持一致，不能同时混写或伪造发布时间。

例子只经过本机静态审阅；按项目约定未运行Python、测试、编译或数据库。
远程执行后才能记录结果；此前核心模块的验收不等同新例子已经运行。
