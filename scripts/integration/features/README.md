# 自定义分钟特征：构建、导出、回测和Live

这个例子使用 `example.bar_return.minute`：一分钟内从开盘价到收盘价的收益率，
`return_1m = (close - open) / open`，不是相邻两根Bar的close收益率。
配置见[minute_return.example.json](minute_return.example.json)，教学入口见[example.py](example.py)。
Payload由声明式Schema生成，字段是有限float，实例不可变；普通已计算特征只需增加声明和映射。
Schema不执行计算公式；新算法由计算函数实现，已有数据库结果直接映射。
没有新增特征专用Provider、Feed、Channel，没有数据库写入或策略报单。

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
