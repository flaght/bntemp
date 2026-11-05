# 原始指数与MA特征完整例子

`generate.py`在本目录完成读取DolphinDB、计算MA和导出两个标准包，不调用`scripts`。
`index.json`是可编辑的来源/Schema示例，默认读取`dfs://min_bar/index_1min`的`000852`。
数据库连接、标准记录、manifest、文件读回及回测可见性使用公共框架。

所有特征代码均在本包，策略根目录不再保留`features.py`：

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 共用声明、信号字段选择、标准文件读取 |
| `generate.py` | 原始指数及MA的计算、导出和命令行入口 |
| `backtest.py` | 特征可见时钟、分钟帧与原回测策略适配 |
| `index.json` | 本地来源及Schema声明示例 |

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

## 计算与时间规则

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

离线验证：`python -m unittest tests.test_option_feature_example -v`；
含SDK夹具的策略链路验证：`python tests/run_option_vega_offline.py`。
