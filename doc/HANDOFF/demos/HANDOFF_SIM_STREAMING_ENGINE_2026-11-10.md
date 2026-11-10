# XY优先验证：逐根引擎推进优化

## 本次范围

保留逐根Bar的`add_data/run/clear_data`顺序和全部数据、订单、定时器、结算规则。
08策略公式、选约、对冲、风险参数及Live/共享CTP入口不变。

1. 公共`execution/simulation/backend.py`首次推进时启动一次，后续仍检查生命周期和时间单调性，
   每根事件仍调用`next_market`行情门控，确保后来出现的合约也能订阅。
2. Backend可选`performance`与08现有统计器共用，新增四项计时：
   `simulation_backend_prepare`、`simulation_add_data`、`simulation_run_including_decisions`、
   `simulation_clear_data`。故障所在阶段也记录，失败后不清理未消费数据、不冒充推进成功。
3. 原生`backtest/engine.pyx`两处身份检查改用`cache.instrument(id)`索引，
   不再构建全部合约ID列表；未知合约仍拒绝。
4. 原生`_run`先判断订单簿类型，L1跳过只针对深度数据的全合约遍历；
   L2/L3仍每批验证，不缓存检查结果，不影响随后新增的数据/交易场所校验。

## 交付与构建

Python侧同步：

- `bomber/framework/trader/execution/simulation/backend.py`
- `demos/08_option_vega/run_backtest.py`
- `tests/test_simulation_market_timing.py`

要求此前P0/P1完整代码已安装，包括`bomber/framework/performance.py`、回放统计和特征回放模块。
保留服务器自己的YAML、数据、特征、结算路径。`SIM_STREAMING_ENGINE_20261110.tar.gz`仅包含
本轮公共Python改动、README、测试、补丁及证据，没有服务器参数文件和行情文件。

内核源码变更位于开发仓库`nautilus_trader/backtest/engine.pyx`。
针对已将命名空间改为`bomber`的源码项目，提供
`doc/evidence/patches/SIM_STREAMING_ENGINE_20261110_BOMBER.patch`。
以下命令在**生成XY当前Bomber wheel的源码项目根目录**运行，替换绝对补丁路径：

```bash
git apply --check --directory=bomber /absolute/pro/doc/evidence/patches/SIM_STREAMING_ENGINE_20261110_BOMBER.patch
git apply --directory=bomber /absolute/pro/doc/evidence/patches/SIM_STREAMING_ENGINE_20261110_BOMBER.patch
```

`--check`不通过说明源码版本/命名空间不同，应先核对差异；不要使用强制或模糊替换。
然后沿用该项目原有的Bomber wheel构建流程，生成匹配XY操作系统、架构、Python版本的wheel，
安装到**实际启动回测的Python环境**。修改已安装的`.pyx`不会更新已编译扩展。
本地Nautilus源码项目构建的是`nautilus_trader`，不能把它的wheel当作Bomber wheel安装。

在XY项目根目录核验：

```bash
PYTHONPATH=. python - <<'PY'
import inspect
import sys
import bomber.backtest.engine as core
from bomber.framework.trader.execution.simulation.backend import NautilusSimExecutionBackend
print('Python:', sys.executable)
print('Core:', core.__file__)
revision = getattr(core, 'STREAMING_VALIDATION_REVISION', None)
print('Core revision:', revision)
print('Backend:', inspect.getfile(NautilusSimExecutionBackend))
assert revision == 'l1-indexed-validation-v1', '当前环境尚未加载新编译的内核'
assert 'performance' in inspect.signature(NautilusSimExecutionBackend).parameters
PY
```

每次启动日志显示`回测内核`，最终`summary.json`和`performance.json`记录`engine_identity`。
`streaming_validation_revision`为null时，运行仅取得Python侧细分统计，不视为原生优化版本。

## XY再DX的验证顺序

XY用户提供的基线（2026-01-01..2026-10-21、实际191日）：

| 项目 | 基线 |
| --- | ---: |
| run_case | 1394.073097秒 |
| simulation_engine_including_decisions | 892.749016秒 |
| strategy_decision | 158.865303秒 |
| 事件 / 决策帧 | 1488480 / 45839 |
| 选约决策 / 订单 / 成交 | 179 / 1048 / 1060 |

1. 保留旧wheel、原结果目录和有效配置。先用几天相同输入做语义回归，再跑原完整区间。
   比较订单方向/数量/价格/状态/时间、成交、手续费、结算、策略信号、最终持仓和账户权益。
   运行ID、UUID生成的结算成交/订单ID不作为金融结果差异；仅比较笔数不够。
2. XY使用同一机器、同一Python、数据、日期、参数和WARNING级别。比较整体`run_case`，
   以及新的add/run/clear分解；`simulation_run_including_decisions`包含策略决策，不能重复相加。
   新计时本身有少量开销，局部测试不代表全区间收益。
3. 结果一致且耗时下降后，再把相同源代码改动部署到DX，wheel按DX实际环境构建。
   DX单独比较优化前后，不能用XY/DX绝对耗时互相代替；需要回退时重新安装保留的旧wheel。

在XY项目根目录执行（沿用服务器自己的YAML）：

```bash
/usr/bin/time -p env PYTHONPATH=. python -u demos/08_option_vega/run_backtest.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id m5_index_blend \
  --log-level WARNING
```

若旧数据仍需`expand_range`，维持原有效配置，不能在性能对照时同时更换OHLC政策。

## 已完成与未完成的验证

- 实际公共网关/Backend时序回归25项通过，SDK边界为替身，不是原生撮合验收。
- 输入、特征回放、特征缓存及性能统计关联回归131项通过。
- 直接执行修改后Cython源码的校验代码块：3项通过，包括L1禁止全合约扫描、
  L1/L2/L3全部数据/深度组合、后续批次、新交易场所和未知合约的验证。
- 使用项目要求的Cython 3.2.8完成整个修改后`engine.pyx`到C++翻译检查。
- 原生运行回归已增加到内核源码仓库
  `tests/unit_tests/backtest/test_engine_streaming_validation.py`，覆盖L1逐根/整批观测顺序、
  L2/L3后续批次缺深度拒绝、Bar/Quote/Trade未知合约拒绝。
  重建环境中还应运行原有`test_engine.py`、`test_backtest_monotonic.py`和`test_engine_timers.py`。
- 本机没有可运行的Bomber/Nautilus编译内核；未构建Linux wheel、未执行原生运行测试、
  未访问XY/DX或DolphinDB/CTP，未声称149万Bar完整回测收益或成交结果已验证。
