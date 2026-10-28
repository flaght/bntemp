# CtpMarketSessionLifecycle

用途：为独立策略执行会话提供CTP行情侧配置，复用本地执行生命周期，并在停机前保存实际观测的MD交易日用于报告。

- 导入路径：`from bomber.framework.trader.runtime.live.channels.ctp_market import CtpMarketSessionLifecycle`
- 实际定义：继承LocalExecutionLifecycle的控制器类。
- [CTP行情生命周期源码](../../../bomber/framework/trader/runtime/live/channels/ctp_market.py) · [父类生命周期](../../../bomber/framework/trader/runtime/live/local.py)
- [CTP渠道配置与MD配置工厂](../../../bomber/framework/trader/runtime/live/channels/ctp.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 这个类负责什么

它在父类本地执行流程上增加三个动作：验证参考交易日、准备行情时间政策及可选CTP MD配置、保存停止前真实观测的行情交易日。

它不创建TraderApi或TD transport，不要求CTP_TD_ADDRESS，不查询物理账户资金或锁定物理交易账户。但CTP行情登录本身仍需要配置行情用户及密码，不能把“无交易账户关联”理解为“无需行情登录资料”。

本类本身不创建或连接MD Feed；外围输入准备根据md_front、md_credentials和profile创建行情组件，Runner启动时连接。

直接使用本类仍继承LocalExecutionLifecycle的模型执行验证：默认检查client.report_errors和模型FILLED回报。它并不是默认只记录目标的Recording控制器，需要记录模式时使用对应组合。

## 2. 完整构造参数

```python
def __init__(
    self,
    *,
    expected_trading_day,
    environment="realtime",
    replay_md_trading_day=None,
    resume=False,
    state_file=None,
    market_source="ctp",
):
    ...
```

所有参数只能按名称传入。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `expected_trading_day` | 必填 | 有效YYYYMMDD字符串，作为参考会话交易日，不能是date对象 |
| `environment` | "realtime" | CtpLiveProfile时间政策，支持realtime或replay |
| `replay_md_trading_day` | None | replay下原始MD交易日；replay必填有效YYYYMMDD，realtime不得设置 |
| `resume` | False | 父类独立策略模型状态恢复开关 |
| `state_file` | None | 可选策略状态文件路径，prepare时取得该路径对应锁 |
| `market_source` | "ctp" | 精确等于ctp时读取MD连接配置；其他值跳过CTP凭据读取 |

构造先验证expected_trading_day，再初始化父类、创建CtpLiveProfile，并按market_source读取配置。日期不会根据当前日期自动填充，也不证明该日实际属于交易日历或MD已经登录到该日。

本类不暴露ready_seconds参数，父类初始化使用默认30秒；不要把它误认为完整复制了父类所有构造关键字。

market_source不是此处的完整来源白名单验证，大小写也不自动归一化。拼错ctp可能导致跳过凭据分支，不能当作已经选中了有效替代渠道；完整来源验证由外围配置负责。

## 3. 环境变量与凭据对象

market_source="ctp"时构造即读取环境变量，但不执行网络登录：

| 环境变量 | 是否必需/默认 | 用途 |
| --- | --- | --- |
| CTP_MD_ADDRESS | 必需 | MD行情前置地址，保存为md_front |
| CTP_ACCOUNT_ID | 必需 | 行情登录user_id |
| CTP_PASSWORD | 必需 | 行情登录密码 |
| CTP_BROKER_ID | 默认9999 | 行情broker_id，此构造器不强制其为9999 |
| CTP_PRODUCTION_MODE | 默认true | 小写后为1/true/yes/on时True，其余为False |

required帮助函数会去除必需变量首尾空格并拒绝空值。broker_id和production_mode的读取规则不完全相同，不能假定全部字段都被统一strip和校验。

md_credentials为冻结的CtpMarketCredentials，包含broker_id、user_id、password、production_mode；它含敏感资料，不应直接写入日志或报告。其他market_source下md_front和md_credentials均为None。

父类driver保持None，本类不创建TD driver。外围build_md_config可将这些资料传给MD配置工厂；它不需要为此先创建交易会话。

## 4. realtime与replay时间政策

profile为CtpLiveProfile，继承MarketTimeProfile：

| 政策 | bar_time_basis | tick_timestamp取值 | 原始MD日期 |
| --- | --- | --- | --- |
| realtime | event | tick.ts_event | 使用会话参考交易日 |
| replay | receive | tick.ts_init | 使用显式replay_md_trading_day |

profile.expected_md_day(trading_day)在replay下返回replay_md_trading_day，否则返回传入参考日。它允许历史MD日期与当前参考会话日期分开表达。

构造时间政策不会自动把来源时间戳重写、连接特定回放前置或验证所有地址。完整渠道的端口配对、逐合约健康、接收心跳和历史数据准入应由外围入口及对应适配器检查。

## 5. 继承的启动、轮询和恢复

本类没有覆盖prepare、finish_preparation、start、poll、verify、events，直接继承LocalExecutionLifecycle：

1. prepare在有state_file时取得策略状态锁，返回LiveRunContext(trading_day, resources)。
2. start先关闭accept_bars，恢复分支检查状态、订单、归属和仓位上限，并恢复策略目标版本。
3. 启动Runner，等待session_ready；刷新参考资料，按可选钩子准备历史，确认仍就绪。
4. 保存可用状态、设置resume_ready并begin_bars。
5. poll检查Runner失败、client.report_errors、会话就绪，刷新参考，检查版本和逐合约陈旧数据。
6. verify要求无执行回报错误，且Backend报告中至少有FILLED，否则运行验收失败。

events默认返回session.client.backend.reports，event_label继承“模型执行回报”。因此单纯提供一个RecordingExecutionClient直接作为本类的模型会话客户端并不匹配，后者没有这些回报接口。

session还须具备runner、references、manager、client及相应恢复资料。它不是适用于任意对象的通用start/stop包装器。

恢复时，若manager.restore返回None会失败；还会核验已完成订单、仓位归属与上限，恢复策略revision并在Runner支持时暂停旧目标续执行。状态锁和expected_trading_day不会替代这些校验。

## 6. 停机观测值与报告

本类shutdown先保存`session.upstream.latest_trading_day`，设置_md_shutdown=True，再调用父类shutdown。父类关闭accept_bars，按条件保存状态，并在finally中runner.stop。

父类仅在非恢复会话或已resume_ready时保存停机状态；恢复未就绪时保留原恢复状态，不用失败启动覆盖它。返回字段还包括model_reports、resume_state_preserved和未知物理账户状态的None值。

snapshot先取得父类模型执行字段：strategy_position_source="model"、account_reconciliation="off"、execution_origin="model"、model="reference-price"、trading_day等。停机前通常读取当前upstream观测日，停机后使用预先保存的_shutdown_md_day。

这解决Feed停止后可能清空latest_trading_day的问题。若真实观测值原本是None，报告仍应为None，不能拿expected_trading_day补齐成“已观测”。observed_td_trading_day保持None，因为这条生命周期没有TD观测。

声明参考日与观测MD日是不同证据：trading_day表示期望的参考会话，observed_md_trading_day表示来源实际返回的日期。记录了期望日不等于行情已通过日期一致性校验。

## 7. 与Recording多继承组合

```python
class RecordingCtpLifecycle(RecordingExecutionLifecycle, CtpMarketSessionLifecycle):
    pass
```

此顺序使构造使用CTP类的__init__，而_execution_failed、verify、events、shutdown、snapshot优先使用Recording类实现；start/poll继续复用本地父类机制。

方法解析并不自动把两个父类所有覆盖方法串起来。Recording.shutdown/snapshot不调用super，因此这个简单组合不会执行CTP类的停机MD日缓存及模型报告逻辑。需要观测MD日报告时，应像Main EMA入口那样显式覆盖并保留该值，不能只看类名就推断报告字段已采集。

当前HybridRecordingLifecycle正是该继承顺序，并在poll前推进bar_feed、之后处理持仓到期；其记录目标验收仍由Recording分支负责。

## 8. 调用示例

以下只构造配置，不建立行情或交易连接；CTP分支应由用户在运行环境提供必需变量，不在代码中硬编码凭据：

```python
from bomber.framework.trader.runtime.live.channels.ctp_market import CtpMarketSessionLifecycle

# 适用前提：后续实际装配独立模型执行客户端及对应Runner。
controller = CtpMarketSessionLifecycle(
    expected_trading_day="20261028",
    environment="realtime",
    market_source="ctp",
)
assert controller.driver is None
assert controller.profile.bar_time_basis == "event"
```

若来源由外围显式配置为dolphindb，使用market_source="dolphindb"会跳过本类的CTP MD凭据读取；并不意味着该行代码已经创建DolphinDB连接或完成来源检查。

记录模式组合的构造示意：

```python
from bomber.framework.trader.runtime.live.local import RecordingExecutionLifecycle
from bomber.framework.trader.runtime.live.channels.ctp_market import CtpMarketSessionLifecycle


class RecordingCtpLifecycle(RecordingExecutionLifecycle, CtpMarketSessionLifecycle):
    pass


controller = RecordingCtpLifecycle(
    expected_trading_day="20261028",
    market_source="dolphindb",
)
assert controller.md_credentials is None
assert controller.event_label == "Recording目标"
```

## 9. 常见错误与排查

| 现象 | 重点检查 |
| --- | --- |
| 日期构造ValueError | expected_trading_day是否为有效、规范YYYYMMDD字符串 |
| 缺少MD变量 | market_source精确为ctp时会在构造阶段读取必需变量 |
| 没有TD地址却能构造 | 属于MD独立配置的正常行为，不创建交易会话 |
| verify要求模型成交 | 直接使用本类继承模型验收，Recording应使用对应控制器或组合 |
| Feed停机后报告丢交易日 | 是否调用本类shutdown，或被Recording覆盖方法截断 |
| observed_md日为None | 是否真的收到并保留了来源观测，不能用期望值替换 |
| ready_seconds关键字报错 | 本类构造没有暴露该参数，使用父类默认30秒 |
| 状态锁冲突 | 是否多个进程使用相同策略状态路径，而不是物理账户占用 |

相关回归：[执行组件中的Recording和MD停机报告测试](../../../tests/test_execution_composition.py)、[渠道政策测试](../../../tests/test_live_common.py)、[行情来源测试](../../../tests/test_market_sources.py)。实际入口：[Hybrid实时入口](../../../demos/10_hybrid_transformer/run_live.py)、[Main EMA实时入口](../../../demos/01_main_ema/run_live.py)。

相关说明：[RecordingExecutionLifecycle](RecordingExecutionLifecycle.md)、[UnifiedStrategyRunner](UnifiedStrategyRunner.md)、[RuntimeMode](RuntimeMode.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
