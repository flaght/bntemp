# assemble

用途：把Hybrid已准备的参考、日历和行情对象装配成供ManagedLiveRuntime启动的完整Live会话。

- 实际定义：`demos.10_hybrid_transformer.run_live.assemble`；区别于公共assemble_strategy。
- [函数与入口](../../../demos/10_hybrid_transformer/run_live.py) · [模型适配器](../../../demos/10_hybrid_transformer/model_adapter.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 签名与前置条件

```python
def assemble(inputs, lifecycle, *, product, release_path, directions_file,
             signal_config, market_config, mode='recording', max_contracts=10,
             state_file=None, history_minutes=0, history_config=None, history_file=None,
             resume=False, execution_policy=None,
             max_notional=Decimal('1000000'), limit_offset_ticks=1):
    ...
```

inputs为prepare_inputs返回对象；lifecycle为同一会话控制器，SimNow需要其driver。品种、文件路径和行情配置显式传入，其余选项使用函数默认值。

Runtime在finish_preparation后调用，保证预检结束，再安装正式执行持久化钩子。返回后检查Runner为LIVE，再controller.start。

本函数会初始化模型、读取信号和历史配置、计算恢复身份哈希，不是只有轻量拼接；此处不调用runner.start、历史warm_history或开放报单。

## 2. 输入与主要参数

| 输入 | 使用方式 |
| --- | --- |
| inputs.references | instrument_id、instrument_meta、trading_day、spec.venue/tick/multiplier |
| inputs.calendar / upstream | 交易日历与已构造上游Feed |
| mode | 等于simnow才启用orders，其他走Recording，须依赖入口白名单 |
| signal_config/product | 选定signal_id、signal_method、signal_params、hold_bars |
| release_path/directions_file | 模型目录和方向文件；设备由MODEL_DEVICE声明 |
| MODEL_HISTORY_BARS / MODEL_MIN_RAW_BARS | 模型窗口1500、最低300根，由代码声明 |
| max_contracts / REBALANCE_MINUTES | 仓位上限可从命令行调整，开仓间隔默认1分钟 |
| max_notional / limit_offset_ticks | 函数默认1000000、1跳，SimNow使用 |
| state_file | SimNow持久化文件；Recording入口拒绝 |
| MODEL_BAR_TIMESTAMP | 代码默认start，模型分钟时间标签 |

其他行情、历史和政策参数由辅助函数消费。日期、文件存在性、授权与恢复组合主要由parse_args验证。

## 3. 分钟行情与就绪检查

固定instrument=references.instrument_id，创建CtpLiveProfile('realtime')；assemble_minute_market包装upstream为HYBRID_1M，直接传MarketSourceConfig、单合约、参考交易日、calendar及MODEL_BAR_TIMESTAMP，并注册合约元数据。

因为传入futures_calendar，CTP Tick与数据库分钟路径均使用完整期货分钟包装和对应健康检查，而非不加筛选地输入模型。

SessionReadiness组合行情与执行通道。SimNow使用CtpExecutionChannel并要求订单能力；Recording使用LocalExecutionChannel检查记录客户端已启动。构造检查对象不代表已经就绪。

## 4. 执行分支与风控

| 项目 | Recording | SimNow |
| --- | --- | --- |
| orders | False | True |
| 客户端 | RecordingExecutionClient(CLIENT_ID) | build_ctp_execution创建受控客户端 |
| 仓位/参考价 | 新PositionManager和MarketReferencePriceStore | 执行组件共享仓位与参考价 |
| Backend/账本/Driver | ExecutionComponents内均为None | Live Backend、CTP账本、lifecycle.driver |
| manager | None | RuntimeStateManager |

SimNow的RiskLimits为：单笔数量上限2×max_contracts、绝对净仓上限max_contracts、单笔与持仓名义上限均max_notional、行情年龄上限120秒，乘数取参考条款。反向调仓单笔可达两倍净仓上限，不意味着最终净仓可以翻倍。

build_ctp_execution还接收tick、multiplier、limit_offset_ticks、session_check及account_reconciliation。Recording记录目标而不撮合成交，请求数量不等于成交手数。

## 5. 模型、策略、Runner和绑定

build_strategy函数中明确构造MizarWorkflowAdapter并加载模型资料，再创建HybridTransformerConfig和HybridTransformerStrategy。HybridTransformerConfig取信号文件hold_bars，开启record_signals，交易所及乘数来自参考。

HybridLiveRunner共享execution.positions，配置references、session_check、orders和instrument_health；附加calendar、strategy、client、history_minutes和resuming，将history_last_ns设为-1并创建history_service。build_history可读取历史JSON配置，此时尚未load历史分钟。

缓存参考refresh_on_event为False时，将publication_lock设为Runner提交锁，协调参考发布与目标处理。

| 绑定 | 实际内容 |
| --- | --- |
| DataBinding | data_key=str(instrument)，feed_id=model-bars，该合约CUSTOM_BAR、1-MINUTE |
| ExecutionRoute | strategy.config.target_key映射到执行client_id及该真实合约 |
| RoleGuard | product的main角色，以strategy.last_processed_ns为检查时刻 |

随后公共assemble_strategy完成注册。model-bars是Runner注册键，HYBRID_1M是分钟包装source_id，二者不同。绑定固定真实合约，不等于运行中自动移仓换月。

## 6. 持久化与恢复身份

构造三个额外组件：hybrid保存策略批次和版本；session_rules保存日历规则；live_session保存SessionIdentityCheckpoint(model_identity(release_path=..., directions_file=..., signal_config=..., market_config=..., trading_day=...))。

model_identity对指定模型配置/权重、方向文件、信号文件算SHA-256，另纳入窗口参数、模型标签、行情identity及期望交易日，不是整个发布目录所有文件的通用哈希。

SimNow通过attach_ctp_persistence安装写前/回报后钩子，联合TargetStore、组合协调器、仓位、订单状态机、CTP账本/Driver及额外组件；策略绑定manager.save。安装钩子不代表恢复成功，读取旧状态与恢复核验在生命周期start完成。

Recording也构造上述组件并计算model_identity，但不创建manager、不绑定保存回调。模型历史窗口需后续begin_bars预热，不由策略快照完整保存。

## 7. 返回会话的完整字段

返回SimpleNamespace，不是Runner或回测结果：

| 字段 | 内容 |
| --- | --- |
| runner / strategy | HybridLiveRunner、HybridTransformerStrategy |
| execution / client | ExecutionComponents及其client |
| driver | lifecycle.driver；本地Recording可为None |
| upstream / bar_feed | 原始Feed与完整分钟包装Feed |
| ledger / manager | execution.ledger、持久化管理器；Recording为None |
| references / market_profile | 同一参考缓存与realtime配置 |
| readiness | 组合行情/执行就绪检查 |
| recovery_binding | StrategyPositionBinding，绑定策略、客户端、逻辑目标与真实合约 |

后续正式启动才连接最终通道、等待就绪、恢复或保存状态、预热历史并开放分钟和交易闸门。

## 8. 接线示例和错误边界

```python
from importlib import import_module

entry = import_module("demos.10_hybrid_transformer.run_live")

session = entry.assemble(
    inputs, lifecycle, product="RB", release_path=release_path,
    directions_file=directions_file, signal_config=signal_config,
    market_config=market_config,
)
```

实际调用需要已准备的inputs、有效模型/框架依赖、文件和控制器。正式入口由build_runtime自动接线。函数没有自己的回滚事务；失败由Runtime进入shutdown及资源清理，部分局部对象不能视为已返回可用会话。

| 现象 | 检查 |
| --- | --- |
| 模型初始化失败 | 依赖、发布资料、设备、信号方法及方向文件 |
| 收不到分钟 | CUSTOM_BAR、真实合约、model-bars注册键 |
| 有session尚无预测 | assemble不启动/预热，仍需start和完整实时分钟 |
| Recording无成交/状态文件 | 分支只记录目标，无CTP账本和manager |
| 恢复身份不匹配 | 模型/信号/方向资料、窗口、行情及交易日配置 |

相关回归：[公共装配与Runtime](../../../tests/test_strategy_assembly.py)、[Hybrid实时与预热组件](../../../tests/test_demo_hybrid_live.py)。

相关说明：[ManagedLiveRuntime](../trader/ManagedLiveRuntime.md)、[prepare_inputs](prepare_inputs.md)、[assemble_strategy](../trader/assemble_strategy.md)、[HybridSessionLifecycle](HybridSessionLifecycle.md)、[RiskLimits](../trader/RiskLimits.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
