# ManagedLiveRuntime

用途：按统一顺序协调Live会话的预检、输入准备、策略装配、启动、轮询、验收、停机和报告。

- 导入路径：`from bomber.framework.trader.runtime.live.runtime import ManagedLiveRuntime`
- 兼容路径：`from bomber.framework.trader.runtime.managed import ManagedLiveRuntime`
- [实现](../../../bomber/framework/trader/runtime/live/runtime.py) · [控制器与上下文契约](../../../bomber/framework/trader/runtime/live/contracts.py)
- 核对日期：2026-10-29，Asia/Shanghai。

## 1. 职责与适用范围

这是通用运行编排器，类属性mode为RuntimeMode.LIVE。具体通道规则交给controller，输入与策略创建交给两个回调，报告交给report；不内置Hybrid、CTP或报单算法。

构造本身不连接通道，也不调用工厂。每个实例只运行一次会话；成功结果可重复读取，停止后的实例不能重新启动。

## 2. 完整构造签名

```python
def __init__(self, runtime_id, *, controller, prepare_inputs, assemble_session,
             report, seconds, progress=None, monotonic=None, sleep=None, window_context=None):
    ...
```

| 参数 | 默认值 | 作用与约束 |
| --- | --- | --- |
| runtime_id | 必填 | 运行标识，要求strip后非空 |
| controller | 必填关键字 | 实现LiveSessionControllerPort的会话控制器 |
| prepare_inputs | 必填关键字 | callback(context)，返回输入集合 |
| assemble_session | 必填关键字 | callback(inputs, context)，返回会话对象 |
| report | 必填关键字 | 提供begin、path和write的报告对象 |
| seconds | 必填关键字 | 轮询阶段时长，构造仅检查大于0；入口应额外校验有限值 |
| progress | None | poll返回真后调用progress(session)，默认空操作 |
| monotonic | None | 无参数单调时钟，默认time.monotonic，用于计算截止时刻 |
| sleep | None | sleep(seconds)，默认time.sleep，每轮传入0.2 |
| window_context | None | LiveWindowContext；按窗口和停止信号结束，忽略seconds计时，并续接已注册的窗口状态组件 |

两个工厂是可调用对象，不要求函数名一致。返回值没有统一数据类限制，但session必须提供runner，且runner.mode必须是RuntimeMode.LIVE这个枚举成员。

## 3. start的严格顺序

```text
report.begin()
  → controller.prepare(resources) → context
  → prepare_inputs(context) → inputs
  → controller.finish_preparation(context)
  → assemble_session(inputs, context) → session
  → 检查session.runner.mode及检查点绑定对象
  → 如有窗口上下文：绑定窗口并恢复session.window_components
  → controller.start(session, context)
```

LiveRunContext包含trading_day、resources和可写references。resources是Runtime持有的ExitStack，可登记数据源close、锁释放等动作，按后进先出顺序清理。

finish_preparation发生在装配之前，允许CTP结束只读预检连接，再安装正式执行会话的持久化钩子。不能任意调换顺序。

start成功返回None，设置_started=True；已启动时再次start为空操作。准备或装配异常会保存failure，调用stop再传播异常。report.begin位于start的try之外；目录冲突时尚未开始通道准备，run的finally仍会调用stop。

## 4. run的循环和返回值

单次模式在start完成后计算deadline=单调时钟当前值+seconds，因此seconds不包含准备、模型加载、启动和停机耗时。提供window_context时不按seconds到期；每轮先检查窗口结束或停止信号，正常暂停先于健康轮询。详见[公共常驻入口](run_continuous.md)。

每轮先poll(session)。返回False立即退出循环，这一轮不调用progress、打印新增事件或sleep，但仍会verify。返回真则调用progress、获取events并切片打印新增项，再sleep(0.2)。实际周期还包含回调耗时，不是精确5Hz调度。

events应返回支持len和切片、顺序稳定且累计追加的序列；Runtime仅保存已见长度，不按事件ID去重。具体行情推进和状态检查由poll负责。

到时或poll返回False后执行verify，无异常才置status为passed。finally总会stop。run正常返回report.write的结果，通常是summary字典；失败保存failure并抛异常，不用失败字典替代异常。

成功且已有result时，再次run直接返回同一结果；失败或仅手动停止后的实例不能重新run。

## 5. stop的清理与报告

```text
记录已有readiness.last_result
  → controller.shutdown(session, context)
  → ExitStack.close()
  → controller.snapshot(session, context)
  → 汇总cleanup_errors及failure
  → report.write(status, fields, session, events)
```

只有开始过prepare才调用shutdown和snapshot；控制器须容忍session=None、context=None等部分启动状态。装配或检查点绑定失败、尚未尝试controller.start时，shutdown接收session=None，Runtime另行释放已装配Runner，避免收尾写入错误的检查点。资源释放在shutdown之后、snapshot之前，快照应能使用已保存的停机观测。

shutdown、资源关闭或snapshot的普通Exception记录为cleanup_errors，清理继续；有错误则状态改failed。已有启动或运行异常时保留原异常，停机错误附到异常note；只有停机失败时抛RuntimeError。报告写入及最终events读取本身仍可能失败，资源清理先于报告写入完成。

真实RuntimeStateManager会补充checkpoint元数据：状态文件、代次、组件集合、待提交恢复状态和订单客户端。详细流程见[公共会话生命周期](LiveSessionLifecycle.md)及[检查点接入](RuntimeStateManager.md)。

stop先设置_stopped，后续调用为空操作，不会重试失败清理。start后直接stop而没有run/verify，状态仍为初始failed；start只代表启动。start前stop不会连接或写报告，并使实例永久结束。

## 6. Hybrid的回调接线

```python
from bomber.framework.trader.runtime.live.runtime import ManagedLiveRuntime

def make_runtime(args, lifecycle, report, prepare_inputs, assemble):
    return ManagedLiveRuntime(
        "hybrid-transformer-live", controller=lifecycle,
        prepare_inputs=lambda context: prepare_inputs(args, context, lifecycle),
        assemble_session=lambda inputs, context: assemble(args, inputs, lifecycle),
        report=report, seconds=args.seconds,
    )
```

两个lambda把通用签名适配到demo函数。第二个接收context但未直接使用，因为参考、日历及上游行情已装进inputs；lifecycle通过闭包传入，并非Runtime自动添加参数。

## 7. 常见问题

| 现象 | 检查 |
| --- | --- |
| 回调缺参数 | Runtime只传context或inputs/context，args及lifecycle需闭包适配 |
| Historical Runner被拒绝 | 此Runtime只接受LIVE枚举成员 |
| 实际运行超过seconds | 准备、启动、停机及阻塞回调影响总耗时 |
| poll返回False但仍失败 | 退出轮询后仍必须verify和清理 |
| verify通过但最终failed | 停机、资源关闭或快照出现cleanup_errors |
| 重复stop没重试撤单 | stop幂等，失败清理也不会重新执行 |

相关回归：[ManagedRuntimeTests](../../../tests/test_strategy_assembly.py)。

相关说明：[prepare_inputs](../demos/prepare_inputs.md)、[assemble](../demos/assemble.md)、[HybridSessionLifecycle](../demos/HybridSessionLifecycle.md)、[HybridLiveReport](../demos/HybridLiveReport.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
