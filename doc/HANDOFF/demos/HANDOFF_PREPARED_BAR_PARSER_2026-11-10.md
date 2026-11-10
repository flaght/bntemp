# 标准化行情到原生Bar转换优化

## 位置和范围

这轮针对`replay_event_conversion`，即`FileReplayFeed.load_events()`中逐行读取、解析、
构造和收集原生行情事件的过程。旧路径对每行补身份、解析ISO时间、查元数据、创建BarType，
再调用`make_bar`创建Bar以及四个Price、一个Quantity；排序另计。
此前XY通用转换约111.875s，用户最新“事件准备”日志110.713s包含排序。
这些是转换阶段计时，不是读取Feather、策略决策或引擎推进耗时。

生产改动限于pro的三个Python文件：

- `bomber/framework/market/replay/parsers/bar/fixed.py`：增加`PreparedBarParser`。
- `bomber/framework/dataprep/bars.py`：读取器可投影7列，标准化来源显式选择解析路径。
- `demos/08_option_vega/run_backtest.py`：默认prepared，允许generic对照，summary记录选择。

没有修改`nt-bomber/bomber/framework`、原生内核或策略公式，不需要重编译wheel。
通用Fixed/Mapped解析器的默认逻辑和其他入口保持原选择，未自动切换Live行情。

## 优化与契约

1. 已标准化执行帧直接读取`event_ns/available_ns/open/high/low/close/volume`，
   避免装箱datetime、复制无关字段、补身份字典和重复解析ISO时间。
2. 固定InstrumentId来自装配结果，每源加载时绑定一次元数据；BarType创建一次后复用。
   reset/强制重读重新绑定元数据和BarType，修改后的精度可被观察到。
3. 保留正价、有限数值、非负数量、OHLC及接收时间先后校验与文件/行号上下文。
   make_bar和Price/Quantity的浮点格式化、舍入、精度及原生构造路径不变；不缓存价格对象。
4. 原通用ISO解析只有微秒分辨率。含亚微秒event_ns的来源整源回退通用路径，
   不在性能优化中悄悄改变历史时钟；普通M1/M5完成时刻不涉及这一限制。
5. 事件顺序、订阅过滤、原生模拟时钟、next_market和结算链不变。

`add_prepared_bar_source(..., fast_parse=False)`默认继续通用路径。仅08入口按
`--bar-parser {generic,prepared}`选择（默认prepared）；该参数属于启动/回测配置，
可在启动YAML写`bar_parser: prepared`，不进入策略参数JSON。
`prepared_parser_sources/rows`与`prepared_parser_fallback_sources/rows`是实际来源/行数计数。

## 本地验证

- 输入、准备、投影和解析回归73项通过；特征和时序关联128项；通用解析器11项，通过共212项。
- 1500组新旧解析/工厂对照一致，其中925组为预期异常，覆盖舍入、精度、整数可见时钟和上下文错误。
- 原生对象对照测试已添加`tests/test_prepared_bar_parser_native.py`（两项）。
  本机无可运行的编译SDK，原生测试跳过，不能将离线替身验收当作原生通过。
- 离线基准调用实际Python读取器、解析器、make_bar和FileReplayFeed，SDK实体由现有测试替身提供：

| 样本 | 通用 | prepared | 降幅 | BarType构造次数 |
| --- | ---: | ---: | ---: | --- |
| 250源×48行M5（12000事件） | 0.338755s | 0.235532s | 30.47% | 12000→250 |
| 80源×240行M1（19200事件） | 0.494629s | 0.300653s | 39.22% | 19200→80 |

一轮预热后三轮交替执行取中位数，包含事件收集与排序；新旧事件逐条一致。
此基准不包含真实Cython对象性能，不能作为XY完整提速承诺。

## XY部署与原生核验

上传更新包到XY项目对应目录后，在pro根目录执行：

```bash
tar -xzf doc/evidence/archives/PREPARED_BAR_PARSER_20261110.tar.gz
sha256sum -c doc/evidence/checksums/PREPARED_BAR_PARSER_20261110_SHA256.txt

PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python - <<'PY'
import hashlib
from pathlib import Path
from importlib import import_module
for name, relative in (
    ('bomber.framework.dataprep.bars', 'bomber/framework/dataprep/bars.py'),
    ('bomber.framework.market.replay.parsers.bar.fixed', 'bomber/framework/market/replay/parsers/bar/fixed.py'),
    ('demos.08_option_vega.run_backtest', 'demos/08_option_vega/run_backtest.py')):
    actual = Path(import_module(name).__file__).resolve()
    local = Path(relative).resolve()
    print(name, '->', actual)
    assert hashlib.sha256(actual.read_bytes()).digest() == hashlib.sha256(local.read_bytes()).digest(), '实际加载了旧副本'
print('Python转换路径核验通过')
PY

PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python -m unittest \
  tests.test_prepared_bar_parser_native -v
```

原生测试应显示两项测试执行且通过，不应是skipped；不通过时先保留输出，使用generic路径。
运行区间、数据、OHLC政策、内核版本与上一轮保持一致，使用Bash内置time：

```bash
TIMEFORMAT='回测进程总耗时：%3R 秒'
time env PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python -u \
  demos/08_option_vega/run_backtest.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id m5_index_blend --log-level WARNING --bar-parser prepared
```

需同机严格对照时，另跑相同命令，只把`--bar-parser prepared`改为`--bar-parser generic`。
核对事件数1488480、帧45839、选约179、订单1048、成交1060，进一步比较价格、数量、时间、
费用、结算、信号和最终权益。运行ID和UUID生成的结算ID不作为金融差异，笔数相同本身不够。
性能主要比较`replay_event_conversion`、`replay_event_sort`和`run_case`；
之前另一轮加载优化也在部署中，不能把其收益全部归入Bar转换。
