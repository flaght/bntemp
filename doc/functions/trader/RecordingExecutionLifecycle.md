# RecordingExecutionLifecycle

用途：在Live会话中管理“只记录策略目标”的执行生命周期，检查行情与记录客户端是否就绪，以取得目标请求作为运行验收依据。

- 导入路径：`from bomber.framework.trader.runtime.live.local import RecordingExecutionLifecycle`
- 实际定义：继承LocalExecutionLifecycle的控制器类，不是执行客户端或Runtime。
- [生命周期源码](../../../bomber/framework/trader/runtime/live/local.py) · [记录型客户端](../../../bomber/framework/trader/execution/recording.py)
- [ManagedLiveRuntime调用流程](../../../bomber/framework/trader/runtime/live/runtime.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 这个类负责什么

它配合RecordingExecutionClient验证“行情→策略→目标→组合净额→执行路由”链路：启动会话，等待就绪，持续检查行情及参考资料，结束时确认至少记录了一份目标。

记录目标不是下单、模拟成交或真实成交。本类不创建TD、模型撮合后端或物理账户关联，不查询资金，不更新成交仓位。行情来源由外围组装，可以来自CTP或其他受支持来源。

它通常作为ManagedLiveRuntime的controller，由Runtime依次调用prepare、start、poll、verify、shutdown、snapshot和events；本类没有独立run方法。

## 2. 构造参数

本类没有自己的__init__，直接继承LocalExecutionLifecycle：

```python
def __init__(
    self,
    trading_day,
    *,
    resume=False,
    ready_seconds=30,
    state_file=None,
):
    ...
```

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `trading_day` | 必填 | 会话参考交易日，通常为YYYYMMDD字符串；本构造器不校验日期格式 |
| `resume` | False | 进入父类恢复分支；不代表Recording客户端自身具有恢复能力 |
| `ready_seconds` | 30 | 启动后等待runner.session_ready的最长秒数，按monotonic时间比较 |
| `state_file` | None | 可选策略状态路径，prepare时按该路径取得进程锁 |

初始resume_ready=False、driver=None。构造不读环境变量，不连接行情或执行端。

state_file锁按解析后的策略状态文件路径生成哈希，使用/tmp下锁文件和fcntl非阻塞文件锁；同一状态路径被其他持锁者使用时失败。它不是账户锁，也不自动读取或创建策略状态内容。

## 3. start继承了哪些动作

正常、非恢复启动顺序为：

1. 将session.runner.accept_bars设为False，先关闭策略分钟接收入口。
2. 调用runner.start，由已装配Runner启动客户端、来源等。
3. 每0.2秒检查runner.session_ready；runner.failure或等待超时则失败。
4. 刷新session.references，重新检查会话就绪。
5. 若Runner提供prepare_history，先mark_history_live_start，再准备历史，并再次检查就绪。
6. 若session.manager存在，调用save。
7. 设置resume_ready=True，调用runner.begin_bars开放正式分钟处理。

因此session.runner不仅是最小UnifiedStrategyRunner接口，还需具备本会话约定的session_ready、begin_bars等能力。传入任意Runner并不一定可用。

历史加载时分钟入口仍保持关闭；历史加载破坏就绪时不会继续begin_bars。控制器不在内部创建历史服务或参考服务。

## 4. poll、verify与events

| 方法 | 行为 |
| --- | --- |
| `poll(session)` | 检查Runner失败、记录客户端启动状态、会话健康，刷新参考资料并核对版本与逐合约时效；成功返回True |
| `verify(session)` | runner.failure存在或client.requests为空则抛RuntimeError，否则返回None |
| `events(session)` | 无session返回()，否则返回client.requests |
| `event_label` | 固定为“Recording目标”，供Runtime打印新增请求 |

Recording客户端没有report_errors回报通道，本类覆盖父类_execution_failed，使用`not session.client.is_started`判断执行端失效，不会误访问不存在的report_errors属性。

行情检查仍保留：session_ready为False会失败；参考资料通过reference_is_current核验，若无该方法则比较references.spec和runner.fixed_spec；资料变化或stale_instruments非空时调用runner.close_role_gate。

verify要求至少一份目标请求，不要求非零目标或成交。策略未完成预热而一直不产生目标可能失败；记录了清零目标也不意味着发生交易。

poll不会在普通情况下自行设置一个运行时长，它返回True表示继续；ManagedLiveRuntime负责总时长、循环节奏、打印和最终verify。

## 5. shutdown和报告字段

shutdown在session存在时关闭accept_bars，并调用runner.stop，然后返回：

```python
{
    "cleanup_errors": [],
    "final_gross": None,
    "final_active_orders": None,
}
```

None表示没有在这里提供物理账户总仓和活动订单信息，不表示已查证账户为零。该覆盖方法不执行父类模型生命周期的停机save，也不读取backend.reports。

snapshot返回strategy_position_source="none"、account_reconciliation="off"、execution_origin="recording"、声明的trading_day，以及两个None观测交易日字段。直接使用本类时不会自动报告MD真实交易日，即使session.upstream已有该值。

若停机抛异常，异常由外围ManagedLiveRuntime按其清理与报告逻辑处理；返回空cleanup_errors不等于所有外部清理都保证成功。

## 6. 恢复参数的限制

resume参数来自父类。恢复分支会要求manager.restore、已完成订单与归属校验、risk_manager、recovery_binding、策略版本恢复等接口。普通RecordingExecutionClient只记录requests，不提供这整套模型执行恢复接口。

因此不能只给标准Recording会话加resume=True就声称支持恢复。需要调用方明确提供兼容的会话组件；普通记录验收建议保持resume=False。state_file提供锁也不等于状态内容已恢复。

## 7. 与CtpMarketSessionLifecycle组合

当前Hybrid入口声明：

```python
class HybridRecordingLifecycle(RecordingExecutionLifecycle, CtpMarketSessionLifecycle):
    def poll(self, session):
        session.bar_feed.poll()
        result = super().poll(session)
        session.runner.expire_due()
        return result
```

该组合采用Recording的verify、events、shutdown、snapshot；由于Recording没有自定义__init__，构造时沿方法解析顺序找到CtpMarketSessionLifecycle.__init__，因此可以传expected_trading_day和market_source。

这不意味着CTP父类的所有方法都会自动叠加执行。Recording的shutdown/snapshot没有调用super，因此Hybrid这个组合不会自动执行CtpMarketSessionLifecycle保存停机前MD交易日的覆盖逻辑。Main EMA入口为此另行覆盖shutdown/snapshot，明确记录观测值。

## 8. 调用示例与常见问题

```python
from bomber.framework.trader.runtime.live.local import RecordingExecutionLifecycle

controller = RecordingExecutionLifecycle("20261028", ready_seconds=30)
assert controller.driver is None
assert controller.event_label == "Recording目标"
assert controller.events(None) == ()
```

实际运行时把controller传给ManagedLiveRuntime，并提供prepare_inputs、assemble_session和report。会话应装配RecordingExecutionClient及能完成上述就绪、参考和分钟闸门检查的Runner。

| 现象 | 重点检查 |
| --- | --- |
| 没有取得策略目标 | 行情、完整分钟、模型预热及策略是否真正发布目标 |
| 客户端启动状态失效 | RecordingExecutionClient是否被启动或提前停止 |
| 行情健康/心跳失效 | Recording模式仍执行行情检查，不会因为不下单而忽略坏行情 |
| 普通构造传expected_trading_day报错 | 本类直接继承的参数名是trading_day；该关键字用于CTP类或相应多继承组合 |
| 报告观测MD日为None | 本类默认不采集该字段，需对应渠道覆盖 |
| resume时缺少属性 | 父类恢复流程需要模型执行会话接口，普通Recording客户端不具备完整能力 |

相关回归：[执行组件测试](../../../tests/test_execution_composition.py)、[行情来源及历史准备测试](../../../tests/test_market_sources.py)。实际组合：[Hybrid实时入口](../../../demos/10_hybrid_transformer/run_live.py)、[Main EMA实时入口](../../../demos/01_main_ema/run_live.py)。

相关说明：[CtpMarketSessionLifecycle](CtpMarketSessionLifecycle.md)、[UnifiedStrategyRunner](UnifiedStrategyRunner.md)、[RuntimeMode](RuntimeMode.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
