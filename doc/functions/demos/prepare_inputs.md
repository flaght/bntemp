# prepare_inputs

用途：为Hybrid Live读取国内期货参考资料、加载交易日历并创建上游行情对象，供后续assemble使用。

- 实际定义：`demos.10_hybrid_transformer.live_session.prepare_inputs`，不是框架通用同名函数。
- [函数定义](../../../demos/10_hybrid_transformer/live_session.py) · [接线入口](../../../demos/10_hybrid_transformer/run_live.py) · [参考缓存](../../../bomber/framework/dataprep/live_cached_role.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 签名与调用时机

```python
def prepare_inputs(args, context, lifecycle):
    ...
```

三个参数均必填，无默认值。Runtime先调用controller.prepare，再通过lambda调用本函数；返回后先finish_preparation，再assemble。本函数会实际打开参考数据库、读取资料和日历输入。

| 参数 | 预期对象 | 作用 |
| --- | --- | --- |
| args | 已通过Hybrid parse_args的Namespace | 品种、参考源、日期、日历和行情配置 |
| context | LiveRunContext | 提供会话trading_day、清理栈，接收references |
| lifecycle | Recording或SimNow控制器 | 传给行情工厂，提供配置或调整就绪等待预算 |

context.trading_day由控制器提供，不是函数自行猜测当天自然日。手工构造args会绕过入口校验，本函数不会重新检查全部CLI组合。

## 2. 读取哪些参数

| 参数组 | 字段与入口默认 |
| --- | --- |
| 品种与日期 | product、expected_source_day必填；交易日用context.trading_day |
| 参考连接 | reference_database=None、reference_timeout=15秒；其余从DolphinDbReferenceConfig.from_env读取 |
| 因子语义 | factor_date_basis=source、factor_availability=observed-on-read，入口经live_factor_policy规范化 |
| 缓存刷新 | reference_refresh_seconds=5秒，入口允许0至30 |
| 日历输入 | session_inputs_from_args识别的规则及交易日历参数，交易所取references.spec.venue |
| 行情来源 | source_config(args)，Hybrid完整解析允许CTP/DolphinDB；固定environment=realtime |

expected_source_day须YYYYMMDD，表示资料来源日，不要求简单等于交易日。参考源固定DolphinDB；即使行情也是DolphinDB，两者仍是不同用途的配置与连接。

## 3. 逐步执行过程

1. 创建DolphinDbReferenceConfig，使用LIVE_REFERENCE用途创建参考source。
2. 先把source.close登记到context.resources，再source.open，保证后续失败有关闭路径。
3. 创建SourceRoleReferences并包装为CachedRoleReferences，传入品种、交易日、当前纳秒时间、因子政策和刷新配置；限定SHFE/INE、CNY，并要求来源日新鲜度匹配。
4. 缓存构造即refresh读取；写context.references，取当前snapshot，额外检查source_day与expected_source_day精确一致。
5. 按参考交易所加载SessionCalendar，输入用途仍为LIVE_REFERENCE。
6. create_market_feed构造上游，source_id为hybrid-market，flow_path为/tmp/bomber-hybrid-md；此处不启动行情连接。

缓存不代表永不刷新或自动无限后台刷新；后续控制器/Runner负责相应刷新和健康流程。本函数建立初始可用参考。

## 4. 返回值和副作用

返回SimpleNamespace：

| 字段 | 内容 | 后续使用 |
| --- | --- | --- |
| references | CachedRoleReferences | 角色合约、条款、参考日期及因子 |
| calendar | SessionCalendar | 完整分钟、休市、历史预热与持仓期限 |
| upstream | 原始行情Feed | assemble包装为完整期货分钟输入 |

同时修改context.references并登记数据源清理。返回值没有runner、strategy、client或manager，也不包含模型历史DataFrame。

参考source.open可能联网，行情工厂只构造不连接。非CTP分钟源且lifecycle有ready_seconds时，工厂可能把就绪等待提高到至少max_age_seconds。不能把整个prepare_inputs称为“不联网”。

## 5. 与assemble和历史预热的边界

prepare_inputs提供当前合约、日历和上游行情。assemble创建分钟包装、执行账户、模型、策略、Runner及绑定。

本函数不加载模型权重、不读取模型历史分钟、不预测、不启动Runner、不开放交易。模型加载在assemble的MizarWorkflowAdapter构造中；历史加载与warm_history在后续HybridLiveRunner.begin_bars阶段。

## 6. 调用接线示例

```python
from importlib import import_module

entry = import_module("demos.10_hybrid_transformer.live_session")

def input_factory(args, lifecycle):
    return lambda context: entry.prepare_inputs(args, context, lifecycle)
```

上例只建立回调；调用时会打开参考源，须用已校验args及控制器context。完整入口由build_runtime接线、Runtime管理清理，无需手动重复open。

## 7. 失败与排查

| 现象 | 检查 |
| --- | --- |
| 参考连接或读取失败 | DolphinDB参考配置、超时和LIVE_REFERENCE用途 |
| 来源日不一致 | expected_source_day、参考发布内容及新鲜度 |
| 交易所/币种拒绝 | 当前入口限定SHFE/INE和CNY |
| 有行情对象但无数据 | 尚未启动正式会话连接 |
| 后续装配失败如何关闭源 | Runtime执行资源栈清理 |

相关源码：[参考适配器](../../../bomber/framework/dataprep/live_role.py)、[日历输入](../../../bomber/framework/dataprep/session_inputs.py)、[行情工厂](../../../bomber/framework/market/sources.py)。

相关说明：[ManagedLiveRuntime](../trader/ManagedLiveRuntime.md)、[assemble](assemble.md)、[source_config](../market/source_config.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
