# HybridLiveReport

用途：在公共Live运行报告上追加Hybrid策略信号、持仓批次记录及策略状态快照。

- 导入路径：`demos.10_hybrid_transformer.run_live.HybridLiveReport`；数字目录可用importlib.import_module加载。
- [类与实际构造入口](../../../demos/10_hybrid_transformer/run_live.py) · [父类LiveRunReport](../../../bomber/framework/trader/runtime/reports.py)
- [HybridCheckpoint状态结构](../../../demos/10_hybrid_transformer/checkpoint.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 这个类负责什么

继承LiveRunReport，仅覆盖write，在公共summary.json和events.txt之外保存Hybrid特有文件。它不运行策略、不恢复持仓、不查询柜台，也不是持久化管理器。

ManagedLiveRuntime通常先report.begin创建目录，在停止和快照汇总后调用write。构造报告对象本身不创建目录。

## 2. 继承的构造参数

```python
def __init__(self, report_dir, *, prefix="live", metadata=None,
             describe=None, clock_ns=None):
    ...
```

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| report_dir | 必填 | 报告根目录，转Path |
| prefix | "live" | 子目录前缀；Hybrid实际传hybrid-recording或hybrid-simnow |
| metadata | None | 固定报告字段，构造时复制外层字典 |
| describe | None | describe(session)返回策略统计字典，默认空字典 |
| clock_ns | None | 用于生成目录名，默认time.time_ns |

begin创建`report_dir/prefix-时间戳`，exist_ok=False，目录冲突不会覆盖；path属性在begin之前为None。不要在未begin时直接write。

## 3. write参数与文件

```python
def write(self, *, status, fields, session, events):
    ...
```

四项均为必填关键字。status为运行状态；fields为生命周期等汇总字段；session为已组装会话或None；events为控制器提供的目标请求或执行回报序列。

| 文件 | 条件与内容 |
| --- | --- |
| signals.csv | session非None时，将strategy.signal_records转DataFrame，index=False |
| batches.csv | session非None时，将strategy.batch_records转DataFrame，index=False |
| strategy_state.json | session非None时，写HybridCheckpoint(strategy).snapshot_state() |
| summary.json | 父类写固定metadata、describe结果、fields和最终status |
| events.txt | session非None时，父类逐条str(event)写入，供诊断 |

先写三个Hybrid文件，再调用父类。session=None时跳过策略文件与events.txt，仍可写失败summary。空记录是否含CSV列头由DataFrame输入结构决定，不保证预先生成完整表头。

summary合并优先级由低到高为metadata、describe(session)、fields、status，后者覆盖同名键。父类JSON使用default=str；Hybrid的strategy_state写入未使用default=str，依赖检查点本身给出可序列化结构。

返回父类生成的summary字典，不返回文件路径、DataFrame或模型预测。path属性保存实际目录；没有自动HTML或绩效图生成。

## 4. 当前入口额外记录什么

build_runtime的metadata包含mode、product和source_config(args).identity。describe包括bars_used、last_prediction、last_targets、active_batches、订单更新/成交计数和history审计。

strategy_state包含配置、持仓批次、目标版本、最后处理时间、合约、目标和计数等；不保存完整模型历史窗口或模型权重，也不等价于RuntimeStateManager的完整账户、订单、策略联合恢复文件。

Recording的events是目标请求，SimNow的events是执行回报，两者内容不同。summary通过不代表必定有成交，Hybrid会话允许合法零信号。

## 5. 写入与失败边界

文件按顺序写入，不是整体原子事务。CSV或strategy_state失败时，不会在本方法内自动回滚或继续写summary；可能留下部分文件。调用write会重写已有同名文件，防目录覆盖的保护在begin阶段。

此类需要pandas来写CSV，也要求session.strategy具备Hybrid字段。不能拿任意策略会话调用后期待自动推断记录格式。

## 6. 仅构造报告对象的示例

数字demo路径使用importlib加载：

```python
from importlib import import_module
from pathlib import Path

HybridLiveReport = import_module("demos.10_hybrid_transformer.run_live").HybridLiveReport
report = HybridLiveReport(
    Path("reports/hybrid"),
    prefix="hybrid-recording",
    metadata={"mode": "recording", "product": "RB"},
)
assert report.path is None
```

模块导入仍需要其框架依赖，但不会调用main或连接柜台。完整运行应将report交给ManagedLiveRuntime，由后者调用begin/write；不要为了生成报告说明实际启动交易会话。

## 7. 常见问题

| 现象 | 检查 |
| --- | --- |
| path为None或写文件时报错 | 是否先由Runtime调用begin |
| 没有signals/batches文件 | 是否尚未write，或会话组装失败导致session=None |
| summary无成交但passed | Hybrid验收允许无成交，查看会话规则和信号 |
| strategy_state不能单独恢复全会话 | 它不是完整RuntimeStateManager文件，也没有模型窗口 |
| 文件只写了一部分 | 顺序写入不提供整体原子性，检查pandas、序列化及磁盘异常 |

相关回归：[公共策略装配和报告测试](../../../tests/test_strategy_assembly.py)、[Hybrid实时组件测试](../../../tests/test_demo_hybrid_live.py)。

相关说明：[HybridSessionLifecycle](HybridSessionLifecycle.md)、[source_config](../market/source_config.md)、[RecordingExecutionLifecycle](../trader/RecordingExecutionLifecycle.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
