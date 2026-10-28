# HybridSessionLifecycle

用途：在CTP受控会话与同交易日恢复流程上，追加Hybrid目标/批次一致性校验、完整分钟推进和持仓到期处理，定义Hybrid实时运行验收。

- 实际定义：`demos.10_hybrid_transformer.run_live.HybridSessionLifecycle`，demo专用控制器。
- [类与装配入口](../../../demos/10_hybrid_transformer/run_live.py)
- [恢复父类](../../../bomber/framework/trader/runtime/live/channels/ctp_recovery.py) · [CTP会话父类](../../../bomber/framework/trader/runtime/live/channels/ctp.py)
- [Hybrid检查点](../../../demos/10_hybrid_transformer/checkpoint.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 继承关系与用途

```python
class HybridSessionLifecycle(CtpResumeLifecycle, CtpSessionLifecycle):
    ...
```

恢复Mixin优先处理resume场景，普通流程进入CtpSessionLifecycle；Hybrid仅覆盖validate_restored_session、poll和verify。prepare、start、shutdown等仍由父类协作实现。

当前build_runtime仅在mode=simnow时创建它，并传orders=True；Recording使用另一个HybridRecordingLifecycle，不能混为同一套验收。

构造创建Driver但不登录；prepare/start会在Runtime调用时实际连接并执行账户核验和就绪检查。

## 2. 构造参数来自父类

CtpResumeLifecycle接收`*args, resume=False, **kwargs`，消费resume后将其他参数传给CtpSessionLifecycle：

```python
controller = HybridSessionLifecycle(
    transport,
    md_front=md_front,
    orders=True,
    max_session_orders=100,
    expected_trading_day="20261028",
    resume=False,
    account_reconciliation="off",
)
```

上面为当前入口风格，并非全部类默认值。父类默认orders=False、max_session_orders=4、account_reconciliation="strict"，Hybrid入口显式覆盖它们。

| 参数 | 父类默认 | 含义 |
| --- | --- | --- |
| transport | 必填 | 已构造的TD Transport，BrokerID须为9999 |
| md_front | 必填关键字 | MD地址；使用非CTP行情时入口可传None |
| orders | False | 是否启用受控交易执行，恢复要求True |
| max_session_orders | 4 | 会话报单数量上限，须正数；Hybrid入口默认100 |
| environment | realtime | 行情时间环境，Hybrid CLI只允许realtime |
| replay_md_trading_day | None | 通用父类的回放MD日期；Hybrid CLI不开放该运行组合 |
| expected_trading_day | None | 期望TD交易日，Hybrid入口必填 |
| resume | False | 恢复既有状态，要求orders且声明交易日，不能同时接管 |
| adopt_instrument / expected_position | None | 通用父类的显式仓位接管参数，须成对且仅strict支持 |
| scope_to_reference_instruments | False | 是否限制柜台仓位核验范围为参考合约集合 |
| account_reconciliation | strict | strict/observe/off，Hybrid入口默认off |
| lock_namespace / legacy_lock_namespaces | bomber-ctp / () | 账户锁命名空间及兼容命名空间 |
| ready_seconds / reconcile_seconds | 30 / 10 | 就绪等待和周期核验间隔，秒，须正数 |

签名没有state_file参数；状态文件通过组装session.manager时配置。Hybrid CLI要求SimNow显式交易开关、确认及状态文件，并验证新会话/恢复文件关系，这些是入口约束，不是此类自动解析CLI。

## 3. 新会话和恢复启动

新会话prepare取得账户锁、启动只读Driver预检，按政策检查资金、活动订单及必要仓位，核验TD交易日；参考准备后停止预检会话。start再启动最终执行会话、等待MD/TD健康、准备历史、保存状态、开放客户端交易闸门及完整分钟入口。

恢复prepare也取得锁，核验当前TD日、资金和无活动订单。恢复start先闭分钟入口、恢复RuntimeStateManager，再调用Hybrid覆盖校验，启动客户端并核验账户与账本，等待行情和参考就绪，提交恢复状态后才开放。

恢复未就绪时shutdown保护原状态文件，不用失败启动覆盖，也不自动处置未知柜台订单。正常停机由父类撤单、查最终订单及必要总仓、保存状态并停止Runner/Driver；不是自动平掉全部持仓。

off政策不等于无TD：仍会进行交易日、资金、活动订单、订单归属和执行状态检查，只是不以柜台总仓覆盖策略成交账本。

## 4. Hybrid恢复校验钩子

```python
def validate_restored_session(self, session, day):
    ...
```

依次执行：

1. validate_ctp_restored_positions：账本交易日必须相同，合约属于恢复绑定，并核验归属及订单完成状态。
2. HybridCheckpoint.validate_targets：批次/策略保存目标与Runner.target_store的目标及版本一致。
3. runner.hold_restored_target：暂停旧目标的续执行，等待策略发布更高版本新目标。
4. 返回strategy._revision给恢复流程用于记录。

恢复的是持仓义务与执行状态，不会重放过去委托。模型历史窗口应按Hybrid入口要求重新预热，不能只恢复strategy_state.json就继续。

## 5. poll的实际顺序

```text
session.bar_feed.poll()
    → 父类会话健康、参考、报单额度及周期对账检查
    → session.runner.expire_due()
    → 返回父类的继续/停止结果
```

先推进已经到达的完整分钟，再处理独立到期时钟，避免同一边界顺序颠倒。即使父类返回False，当前实现仍会调用一次expire_due，后者会检查accept_bars等状态；前一步抛异常则不继续后续步骤。

父类报单额度达到后会关闭accept_bars并disarm，等待收尾期限再返回False；不只是检查是否收到新预测。

expire_due调用策略on_time处理既有批次期限，并在条件允许时继续固定目标执行。会话保持轮询才能推进这些独立时钟职责，不能把poll当成没有副作用的状态读取。

## 6. verify如何判定通过

Hybrid覆盖父类“必须有成交”的验收：

- runner.failure存在或client.report_errors非空，失败。
- 非恢复会话若strategy.bars_used为0，失败，提示没有处理完整实时分钟。
- 恢复场景不以bars_used非零作为额外要求。

没有强制预测非零、强制开仓或要求至少FILLED。零信号可以合法通过，是否成交由报告单独说明；passed只能按这套验收解释，不能当作真实交易闭环必然完成。

## 7. 报告、事件和调用方式

events与event_label继承CTP会话：orders=True时报告Backend执行回报。snapshot还组合resume_ready、restored_generation及父类TD/MD观测、政策、订单计数、审计和差异等。

实际入口将controller交给ManagedLiveRuntime，后者管理prepare→start→poll→verify→shutdown→snapshot和报告写出。此类没有自己的run方法，不应绕过入口预检随意调用connect或报单。

数字demo目录可用importlib访问：

```python
from importlib import import_module

entry = import_module("demos.10_hybrid_transformer.run_live")
HybridSessionLifecycle = entry.HybridSessionLifecycle
assert "verify" in HybridSessionLifecycle.__dict__
```

模块导入需对应依赖，但上例不创建交易会话。正式运行使用entry.parse_args/build_runtime的完整校验与装配路径。

## 8. 常见问题

| 现象 | 检查 |
| --- | --- |
| 构造拒绝BrokerID | 本链路明确限制SimNow9999 |
| state_file关键字错误 | 文件配置给会话manager/入口参数，不直接传生命周期 |
| 恢复交易日或目标不一致 | 检查账本、恢复绑定、批次及TargetStore版本 |
| 有旧目标但未立即续单 | 恢复刻意hold旧版本，重新预热后需新目标 |
| 没有成交但运行通过 | Hybrid允许零信号，查看bars_used、预测和成交报告 |
| off仍检查账户资金和订单 | off不豁免TD健康、资金及活动订单核验 |
| 停机仍有持仓 | 默认撤单并报告，不承诺自动全平 |

相关回归：[Hybrid实时组件测试](../../../tests/test_demo_hybrid_live.py)、[Hybrid到期与批次测试](../../../tests/test_demo_hybrid_holdings.py)、[公共会话与报告测试](../../../tests/test_strategy_assembly.py)。实际入口：[run_live.py](../../../demos/10_hybrid_transformer/run_live.py)。

相关说明：[HybridLiveReport](HybridLiveReport.md)、[execution_policy_from_args](../trader/execution_policy_from_args.md)、[build_simnow_transport](../trader/build_simnow_transport.md)、[CtpTdApiTransport](../trader/CtpTdApiTransport.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
