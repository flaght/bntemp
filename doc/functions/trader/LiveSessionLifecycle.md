# 公共 Live 启动、恢复与停机

实现：[live/lifecycle.py](../../../bomber/framework/trader/runtime/live/lifecycle.py)。这是 Live 会话编排，放在 Live 下；策略预热、选约、批次和账户对账政策继续由现有业务组件及通道生命周期提供。

## 启动顺序

```text
取得声明的状态文件锁和通道资源
  → 装配并核对检查点接入对象
  → 恢复文件、订单、归属、目标版本及业务约束
  → 启动 Runner，等待行情／执行就绪
  → 准备历史，刷新参考及执行输入，再次核验
  → 校验稳定业务目标，保存／commit_restore
  → 标记 resume_ready
  → 按原模式授权，开放实时决策
```

`LocalExecutionLifecycle` 和 `CtpSessionLifecycle`／`CtpResumeLifecycle` 共用准备及放行动作。Local 的恢复校验为可覆盖方法：默认检查已结束订单、归属、风险上限与目标版本；03 只补充双腿手数及方向。CTP 仍负责真实账户查询和 strict／observe／off 等原政策，不把柜台未知仓位归给策略。

| 公共函数 | 职责 |
| --- | --- |
| `prepare_live_session(session, ready_seconds, lost=None)` | 决策关闭时启动 Runner，公共等待器刷新参考／特征查询，再准备历史并重新核验 |
| `prepare_strategy_history(session)` | 统一历史准备与准备后的输入刷新；原 shared 模块保留同名兼容导出 |
| `activate_live_session(controller, session, restored=False, authorize=None)` | 稳定检查点提交成功后才授权、开放决策；各边界核对常驻停止信号 |
| `commit_session_checkpoint(session, restored=False)` | 在提交锁内核对目标／组合版本、路由贡献和业务组件；新会话 save，恢复复用原 commit_restore |
| `validate_session_checkpoint(session)` | 检查 Manager 与 Runner／执行客户端是否共享目标、组合、仓位及订单状态机 |
| `close_session_gate(session)`／`shutdown_local_session(...)` | 串行关闭决策；按政策保存，保存失败时仍停止 Runner并汇总错误 |

以上可选参数在实际实现中为关键字参数。常规应用继续选择现有生命周期类；仅自定义控制器需要直接调用这些函数，没有新增策略基类或配置 DSL。

## 失败与停止

准备未完成时保持决策关闭，不授权。恢复准备失败保留原文件；新会话准备失败不通过停机保存半初始化状态。缺少恢复文件明确失败，不降级为空仓。

`resume_ready` 表示检查点已成功提交，不表示必须已成交。就绪等待及历史准备遇到常驻停止信号时返回，后续由原 Runtime 正常停机。08 的停止标志、期权行情／指数输入要求和原仓处理仍是业务钩子。

正常本地停机先关决策；已完成准备的 Model 保存，Recording不读 backend、不保存交易文件。CTP 已准备会话继续撤销自身订单、权威核验后保存并断开；未提交准备的会话不会通过收尾保存半成品或撤销未知订单。已成交仓位按原政策保留。

`ManagedLiveRuntime` 在装配／绑定失败且尚未开始最终会话时，以 `session=None` 调用通道收尾，并释放已装配 Runner，阻止错误 Manager 被收尾写入。运行和停机同时失败时保留原异常，停机错误同时写入 `cleanup_errors` 并附到原异常 note。

## 08 共享账户与进程边界

`SharedAccountLiveHost` 继续使用账户生命周期和逐策略信号文件。CTP共享账户提供 `start_preparation()`，宿主延迟暂存账户状态的提交：所有策略恢复、就绪、历史准备和信号核验通过后，`authorize()` 才调用原 `commit_restore()` 并授权。策略恢复失败时，`abort_preparation()` 阻止停机覆盖原账户文件。

账户文件和逐策略信号文件仍是原有两个层次，不新增跨文件事务格式。独立 server／worker 的授权、能力文件和停机顺序保持原适配器职责；其账户服务在发布能力文件前完成本身的权威核验与提交。

## 对应文件

| 范围 | 接入文件与保留职责 |
| --- | --- |
| 公共准备与收尾 | [lifecycle.py](../../../bomber/framework/trader/runtime/live/lifecycle.py)、[local.py](../../../bomber/framework/trader/runtime/live/local.py)、[readiness.py](../../../bomber/framework/trader/runtime/live/readiness.py)、[runtime.py](../../../bomber/framework/trader/runtime/live/runtime.py) |
| CTP恢复与状态锁 | [ctp_recovery.py](../../../bomber/framework/trader/runtime/live/channels/ctp_recovery.py)、[ctp.py](../../../bomber/framework/trader/runtime/live/channels/ctp.py)；声明 state_file 时新建／恢复均持有对应状态锁 |
| 01 EMA | [run_live.py](../../../demos/01_main_ema/run_live.py)、[recovery.py](../../../demos/01_main_ema/recovery.py)；精确 EMA 断点、跨日账本及收盘分钟仍归业务 |
| 03 Calendar | [recovery.py](../../../demos/03_calendar_spread/recovery.py)；重复 Local／CTP start 与停机已删除，保留方向、手数及完整双腿验收 |
| 04 Cross section | [run_live.py](../../../demos/04_cross_section/run_live.py)、[live_runtime.py](../../../demos/04_cross_section/live_runtime.py)、[recovery.py](../../../demos/04_cross_section/recovery.py)；删除重复关闸及Recording收尾，保留组合校验 |
| 08 Vega | [recovery.py](../../../demos/08_option_vega/recovery.py)；删除重复启动、锁及本地保存流程，保留 stopping、选约、行情完整性和估值校验 |
| 08共享账户 | [shared.py](../../../bomber/framework/trader/runtime/live/shared.py)、[ctp_shared.py](../../../bomber/framework/trader/runtime/live/channels/ctp_shared.py)；统一历史准备、暂存提交时机及检查点元数据 |
| 10 Hybrid | [run_live.py](../../../demos/10_hybrid_transformer/run_live.py) 继承公共流程，传入状态路径；模型预热前移prepare_history，字段／模型错误先于提交和授权，批次核验保留 |

## 验证

```bash
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_lifecycle_offline
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_history_offline
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_continuous_offline
```

离线验收使用真实 Python Runtime、JSON状态管理器、策略和账户核心，仅 SDK实体／外部传输使用既有夹具，不构造或代替原生EMA运算。完整原生EMA、所有真实行情／柜台并发联调仍需目标运行环境验证。

模块式可配置预检仍为优先级5，尚未实现。

[检查点接入与运行一致性](RuntimeStateManager.md) · [返回函数索引](../README.md)
