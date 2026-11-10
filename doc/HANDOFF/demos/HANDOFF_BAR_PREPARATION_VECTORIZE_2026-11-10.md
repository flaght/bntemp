# 行情准备向量化：XY验证

## 范围

生产代码仅修改`pro/bomber/framework/dataprep/bars.py`。
没有修改`nt-bomber/bomber/framework`、原生内核、策略公式、业务参数或行情文件。
没有持久化缓存和并行加载，也没有跳过异常、按需裁剪合约或缩短回测日期。

1. 以日期时间数组批量转换纳秒；Pandas的s/ms/us/ns存储分辨率被显式处理。
   超出纳秒范围时保留原标量转换错误路径。
2. 以NumPy批量检查常见数值列的有限性；仅有限性掩码使用float64，
   价格、数量、正数比较、整数成交量检查保留原类型，防止大整数精度丢失。
   特殊数值类型保留原标量校验路径。
3. 同类型原生数值OHLC以数组比较，混合类型、可空列仍用原Pandas聚合。
   默认reject和expand_range政策不变；修整先改high再算low，来源价格、警告及原始行号不变。
4. 可用时间和事件时间都已递增的文件无需重新排序；否则按原键稳定排序，
   事件时间倒退仍报错。仅省去无映射rename和默认RangeIndex的重复重建。

## 验证与局限

- 59项输入/多周期/性能统计回归通过，其中12项为本次新增。
- 127项特征、外部数据和模拟时序关联回归通过。
- 与修改前真实读取器进行720组差异对照，数值、dtype、时间、行顺序、问题记录、
  覆盖报告及异常完全一致；覆盖普通/可空/文本/Decimal、缺失/无限值/负数、
  大整数、乱序和三种显式处理政策。
- 本地Pandas 3.0.6、NumPy 2.5.3，真实合成Feather小文件基准：

| 文件样本 | 修改前 | 修改后 | 降幅 |
| --- | ---: | ---: | ---: |
| 300个48行文件（M5） | 1.383048s | 1.065693s | 22.95% |
| 100个240行文件（M1） | 0.514652s | 0.400056s | 22.27% |

一轮预热，随后三轮交替新旧执行取中位数，每轮创建独立InputSession，
不复用标准化缓存；操作系统文件缓存不受控。这是准备阶段的小样本，
不是原生回放或XY全区间收益承诺。本机没有执行原生全区间策略回测。

## 同步和运行

用户已在XY安装`l1-indexed-validation-v1`内核，本轮不用再次编译wheel。
保留XY的策略YAML、特征、结算和行情路径。将交付包放到项目对应目录后：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/BAR_PREPARATION_VECTORIZE_20261110.tar.gz
sha256sum -c doc/evidence/checksums/BAR_PREPARATION_VECTORIZE_20261110_SHA256.txt
```

在实际回测Python中确认导入的读取器内容与本地维护文件一致：

```bash
PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python - <<'PY'
import hashlib
from pathlib import Path
import bomber.framework.dataprep.bars as bars

actual = Path(bars.__file__).resolve()
expected = Path('bomber/framework/dataprep/bars.py').resolve()
print('实际读取器:', actual)
assert hashlib.sha256(actual.read_bytes()).digest() == hashlib.sha256(expected.read_bytes()).digest(), '实际加载了另一份旧framework'
print('读取器内容核验通过')
PY
```

如果仍从site-packages导入旧文件，应按原有framework维护方式同步实际加载的副本，
不要误把整份内核源码覆盖到运行环境；本轮无需重新编译原生扩展。

```bash
TIMEFORMAT='回测进程总耗时：%3R 秒'
time env PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python -u \
  demos/08_option_vega/run_backtest.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id m5_index_blend \
  --log-level WARNING
```

同机保持2026-01-01..2026-10-21、1488480根Bar、原有效OHLC政策、相同内核和配置。
对比最近一次XY基线：加载323.712959s、总计789.960931s、策略决策157.207980s，
选约179、订单1048、成交1060。核对订单/成交价格和数量、手续费、结算、信号及最终权益；
总笔数一致只是第一层检查。仅在XY核对结果及收益后再同步DX。
