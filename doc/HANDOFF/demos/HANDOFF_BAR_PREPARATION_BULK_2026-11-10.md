# 第二轮行情准备优化与Bar对象转换位置

## 改动

生产代码仍只修改独立维护的`pro/bomber/framework/dataprep/bars.py`，
不修改`nt-bomber/bomber/framework`或原生内核，不需要重编译wheel。

采样发现小文件上反复创建Pandas列、Series和掩码的累计开销明显，因此：

1. 一次组装时间列，保留原有列的位置以及新列的追加顺序、dtype、attrs、索引名称与flags。
   非普通列索引保留原逐列插入路径。
2. 可用时间已经经过时区标准化，直接统一到纳秒后复用，不重复从整数解析日期时间。
3. 普通NumPy数值列不再重复`to_numeric`和写回；在原整数/浮点类型上直接比较和检查整手数。
   非有限、可空、文本、Decimal、扩展类型等保留兼容路径。

上一轮的时间单位、OHLC拒绝/顺序修整、零价处理、乱序拒绝及缓存隔离规则全部保留。

## 本地验证

- 输入、多周期和性能统计64项通过；特征、外部数据及模拟时序127项通过，共191项。
- 与上一版720组对照，数值、dtype、时间、行顺序、问题记录、覆盖报告及异常一致。
- 真实合成Feather，一轮预热后交替执行新旧三轮取中位数，每次新建InputSession：

| 样本 | 上一版 | 本版 | 降幅 |
| --- | ---: | ---: | ---: |
| 300个48行M5文件 | 0.994718s | 0.657637s | 33.89% |
| 100个240行M1文件 | 0.332868s | 0.221765s | 33.38% |

这些数据是本机小文件准备基准，不能直接视为XY全区间收益。
本机未执行原生完整策略回测；操作系统文件缓存不受控。

## XY同步与对照

保留已生效的`l1-indexed-validation-v1`内核、原YAML、数据与OHLC政策，只同步Python读取器。
在XY项目根目录执行：

```bash
tar -xzf doc/evidence/archives/BAR_PREPARATION_BULK_20261110.tar.gz
sha256sum -c doc/evidence/checksums/BAR_PREPARATION_BULK_20261110_SHA256.txt

PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python - <<'PY'
import hashlib
from pathlib import Path
import bomber.framework.dataprep.bars as bars
actual = Path(bars.__file__).resolve()
local = Path('bomber/framework/dataprep/bars.py').resolve()
print('实际读取器:', actual)
assert hashlib.sha256(actual.read_bytes()).digest() == hashlib.sha256(local.read_bytes()).digest(), '仍加载了另一份framework'
assert hasattr(bars, '_attach_time_columns'), '尚未加载本轮读取器'
print('第二轮读取器核验通过')
PY

TIMEFORMAT='回测进程总耗时：%3R 秒'
time env PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python -u \
  demos/08_option_vega/run_backtest.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id m5_index_blend --log-level WARNING
```

如果实际加载位置为site-packages且内容不同，应按已有framework维护方式更新该Python副本。
比较同一机器、Python、内核、191个交易日、1488480根Bar；最近XY基线：

| 项目 | 基线 |
| --- | ---: |
| bar_read_normalize_validate | 252.148737s |
| feather_disk_read | 58.343362s |
| run_case | 719.843958s |
| 决策帧 / 选约 / 订单 / 成交 | 45839 / 179 / 1048 / 1060 |

同时核对价格、数量、费用、结算和最终权益；不能只凭总笔数判断金融结果等价。

## Bar对象转换究竟在哪里

| 顺序 | 代码 | 工作 |
| --- | --- | --- |
| 入口 | `demos/08_option_vega/feature/backtest.py:run_replay` | 调用`feed.load_events()` |
| 文件遍历 | `bomber/framework/market/replay/base.py:FileReplayFeed.load_events` | 遍历回放源、获取行、创建解析上下文、过滤订阅并收集事件 |
| 行读取 | `bomber/framework/dataprep/bars.py:PreparedFrameReader.read` | 从已标准化的DataFrame迭代行，不重新读取Feather |
| 合约身份 | `bomber/framework/market/replay/parsers/bar/fixed.py:FixedInstrumentBarParser.parse` | 补充固定合约和交易所身份 |
| 时间与数值 | `bomber/framework/market/replay/parsers/bar/mapped.py:MappedBarParser.parse` | 解析时间、可用时钟、OHLCV、合约元数据及校验 |
| 对象构造 | `bomber/framework/market/basic/base.py:make_bar` | 按标的精度构造原生Price、Quantity、BarType和Bar |

其输出是撮合可消费的原生事件，不是原始DataFrame。
最近XY的`replay_event_conversion=111.874586s`包括上述行读取/解析/构造/订阅过滤，
`replay_event_sort=1.267674s`单独记录排序；随后才封闭特征帧和推进模拟撮合。
本轮只调整加载准备，不改变此转换链路或撮合行为。
