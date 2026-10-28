# CtpTdApiTransport

用途：封装项目自有CTP原生TdApi，管理连接握手、同步请求与异步分片回报聚合、账户查询、原生报撤单传输及资源释放。

- 导入路径：`from bomber.framework.trader.execution.ctp import CtpTdApiTransport`
- 实际定义：`bomber.framework.trader.execution.ctp.td_transport.CtpTdApiTransport`。
- [Transport源码](../../../bomber/framework/trader/execution/ctp/td_transport.py) · [Driver与会话契约](../../../bomber/framework/trader/execution/ctp/native_driver.py)
- [查询拒绝错误](../../../bomber/framework/trader/execution/ctp/query_errors.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 这个类负责什么

CTP的请求和回报由不同接口、线程及分片表达。Transport将认证、登录、结算确认和查询请求关联到各自request_id，等待匹配的最终回报，再向调用方返回完整结果。

同时将订单、成交和断线推送转给Driver，提供原生报单、撤单发送入口。它不把策略目标规划成订单，不实现RiskLimits、策略归属、账户锁或上层交易授权。

构造只保存配置及本地状态；connect才延迟导入bomber_ctp_td.TdApi并创建连接。测试可注入td_api_base假API，不需要真实柜台或vn.py Gateway。

## 2. 完整构造签名

```python
def __init__(
    self, *,
    client_id: str,
    account_id: str,
    front: str,
    broker_id: str,
    investor_id: str,
    password: str,
    app_id: str,
    auth_code: str,
    td_api_base: type | None = None,
    flow_path: str = "",
    production_mode: bool = True,
    timeout_seconds: float = 10.0,
) -> None:
    ...
```

所有参数只能按名称传入。app_id/auth_code也必须显式提供，但允许空串。

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| client_id | 必填 | 框架交易客户端身份，写入账户和订单快照 |
| account_id | 必填 | 框架账户身份；登录及柜台行核验主要使用investor_id |
| front | 必填 | CTP交易前置地址，不是MD地址 |
| broker_id | 必填 | 经纪商身份，直接类构造不限制为9999 |
| investor_id | 必填 | 登录UserID及柜台InvestorID核验身份 |
| password | 必填 | 登录密码 |
| app_id / auth_code | 必填，可为空 | 认证资料，auth_code非空才发送认证请求 |
| td_api_base | None | 测试注入API基类，否则connect导入bomber_ctp_td.TdApi |
| flow_path | 空串 | 原生流文件目录，连接时展开~；空值回退/tmp/bomber-ctp-td |
| production_mode | True | 传给createFtdcTraderApi的原生创建参数 |
| timeout_seconds | 10.0 | 每次请求及前置连接等待预算，单位秒 |

前六个身份/地址/凭据字段strip后不得为空，但保存时不统一裁剪。直接构造仅检查timeout_seconds大于零，没有工厂层完整的有限性、布尔值和120秒上限检查；正常入口应使用build_simnow_transport的解析规则。

本类不直接读取环境变量。工厂会将环境配置作为这些关键字传入，工厂默认超时为30秒，与类默认10秒不同。

## 3. connect：实际连接与握手

```python
session = transport.connect(on_order, on_trade, on_disconnect)
```

三个回调接收订单字段映射、成交字段映射及断线原因整数。成功返回CtpTraderSession，包含broker_id、investor_id、trading_day、max_order_ref，供Driver建立身份关联。

主要顺序：

1. 拒绝尚持有API实例时重复connect，设置回调并清理会话关联缓存。
2. 创建CallbackApi，连接时才创建流文件目录及原生TraderApi。
3. 订阅私有、公有主题，注册交易前置并init。
4. 等待前置连接或断线，超时明确失败。
5. auth_code非空时认证；然后登录。
6. 要求唯一登录记录，核对BrokerID和UserID，取得交易日与MaxOrderRef。
7. 发送结算信息确认，要求唯一且账户匹配的确认记录。
8. 检查连接期间无断线和早到未归属订单/成交推送，保存会话并返回。

连接中的异常会触发close清理。各阶段分别使用请求预算，不是整个握手总共只允许timeout_seconds秒。登录和结算确认是实际柜台操作，构造Transport与调用connect不是同一行为。

缺少项目原生扩展时，connect抛出说明bomber_ctp_td不可用的RuntimeError，不自动安装或换用其他Gateway。

## 4. activate、就绪和断线

connect成功后还没有设置_ready=True。Driver在建立会话身份、回报接收和订单引用映射后调用activate，才允许权威查询和报撤单接口使用。

activate检查API、会话、断线和早到推送状态，但不代表上层执行客户端已经完成对账或开放交易授权。Transport本身没有独立的策略交易闸门，直接调用低层send_order不会替代外围风控。

就绪前收到订单或成交推送会记录为早到事件，不简单缓存后随意补发；握手/激活会因此保持失败，避免未归属订单被忽略。

断线会取消就绪、唤醒等待请求，并在此前已ready时调用on_disconnect。通用onRspError也会进入失效处理。不提供自动重连、自动恢复订单或自动重新授权。

## 5. 同步查询如何等待异步最终回报

请求通过_request_lock串行化，使用Condition等待回调；回调必须同时匹配kind和request_id，才可追加该请求的数据。收到last=True才表示请求分片结束。

收到错误但未收到最终分片，不会释放查询槽位。请求函数返回非零时视为未受理，抛CtpQueryRejected，不把它解释为一份空快照。

### 超时不等于旧请求已结束

本地超时时清除旧请求已收集的行，但保留未结束请求身份。后续查询先等待旧请求最终回报；等不到就抛“旧请求尚未结束”，不会发送新请求覆盖它。

迟到最终回报只用于释放旧请求占用，旧行不会作为下一次权威快照。错误kind、旧request_id的最终回报不能错误地解锁当前请求。

发送查询调用自身抛异常时，也不能证明API未受理，保留该请求直到匹配结束或断线，不盲目重发。

本Transport没有自动重试。CtpQueryRejected仅在reqQry方法返回-2/-3时标记retryable=True，供上层有界只读查询策略判断；认证/登录/结算、报单和撤单不能据此自动重试。

## 6. 公开查询接口与返回值

全部业务查询须在activate之后；每次都发起新请求并等待完整最终回报，不以旧缓存冒充权威结果。

| 接口 | 返回内容 |
| --- | --- |
| query_positions() | `{真实合约字符串: Decimal净仓}`，多头为正、空头为负，同合约汇总 |
| query_gross_positions() | `{真实合约字符串: (多头总数, 空头总数)}`，用于辨别净额为0但仍有双向仓位 |
| query_position_details() | tuple[只读Mapping, ...]，保留每行持仓方向与可用诊断字段 |
| query_account() | AccountStateEvent，包含各币种余额、权益、可用资金和占用保证金 |
| query_active_orders() | ActiveOrderSnapshot，包含仍属于活动状态的订单及原始/成交/剩余数量 |
| query_diagnostics() | 本地请求状态与最近审计记录，不发送柜台查询 |

仓位查询核验BrokerID/InvestorID、真实合约、ExchangeID、多空方向和数量。净仓不能替代总仓检查，例如多2空2净仓为0，总仓仍非零。

query_position_details实际使用reqQryInvestorPosition，不是另一份逐笔持仓明细API。返回InstrumentID、ExchangeID、PosiDirection和Decimal Position；存在时保留PositionDate、TodayPosition、YdPosition、HedgeFlag、PositionCost。今昨字段缺失保持未知，不推断为0；结果不带账户身份，不修改本地仓位账本。

资金查询要求有完整记录，核对AccountID等于investor_id，并拒绝重复币种或缺少Balance/Available/CurrMargin。total和equity都映射Balance，不是额外计算策略收益。

活动订单查询跳过已全部成交、已撤和明确报单拒绝的记录，未知状态失败。client_order_id根据当前会话交易日与OrderRef生成；资金和订单快照各自有递增revision，时间戳使用本地time.time_ns，不是柜台原始事件时间。

各查询是独立快照，分别调用净仓、总仓和明细并不是共享同一次请求的原子结果。柜台状态可能在调用间变化，需要上层核验一致性。

## 7. send_order与cancel_order

`send_order(fields)`接收已经由Driver/原生订单构造器生成的CTP字段映射，复制后调用reqOrderInsert，保存request_id与字段关联。返回None只表示发送函数没有同步拒绝，不代表订单已接受或成交。

异步报单错误会与本次发送记录关联、核对身份，转换为订单拒绝推送；重复拒绝按OrderRef抑制。找不到可信关联或身份冲突时转失效处理，不猜测订单归属。

`cancel_order(order_ref)`需要此前订单推送中保存的FrontID、SessionID、InstrumentID、ExchangeID映射，才能构造reqOrderAction。仅有OrderRef数字不足以任意撤单；query_active_orders不会在这里自动替代订单推送关联。

撤单请求返回不代表已撤。异步撤单拒绝不能当作CANCELED成功，Transport会使连接逻辑失效，要求外围重新核验活动订单。报撤单均不自动重发。

此层不接收策略目标或OrderIntent，不计算限价和数量，不做RiskLimits检查，不生成策略级仓位归属；正常业务通过CtpNativeTraderDriver和受控执行客户端调用，不绕过它们直接传柜台字段。

## 8. 诊断与停止

query_diagnostics返回timeout_seconds、当前pending的kind/request_id/timed_out/final_received及最近最多32条审计记录。审计包含请求类型、编号、耗时、预算和阶段，不记录凭据、查询字段或资金数据。

close取消ready，设置断线，清空会话与请求关联并唤醒等待者。已init的API调用exit，已创建但未完成init的API调用release。它不会先自动撤掉所有订单或平仓，最终账户核验、保存状态及停机撤单由外围生命周期负责。

close清除API引用后重复调用通常无实际API可释放；直接类没有永久禁止重连的标志，但重新创建连接不等于Driver恢复、订单归属和交易闸门都可以安全复用。

## 9. 仅构造对象的示例

下面使用虚构配置，只构造不连接，不需要原生TdApi实际启动：

```python
from bomber.framework.trader.execution.ctp import CtpTdApiTransport

transport = CtpTdApiTransport(
    client_id="example-td",
    account_id="example-user",
    front="tcp://example.invalid:1234",
    broker_id="9999",
    investor_id="example-user",
    password="example-placeholder",
    app_id="",
    auth_code="",
    flow_path="/tmp/example-ctp-td",
    timeout_seconds=30,
)
assert transport.query_diagnostics()["pending"] is None
transport.close()
```

正常入口使用build_simnow_transport取得配置，再由会话生命周期和Driver管理connect/activate/close。不要为了查看对象说明或验证参数而实际连接柜台。

## 10. 常见错误与排查

| 现象 | 重点检查 |
| --- | --- |
| 构造缺少app_id/auth_code | 两者可为空，但没有参数默认值 |
| 缺少bomber_ctp_td | 原生扩展是否安装且与当前Python/平台匹配 |
| 前置连接或最终回报超时 | 分清连接等待、具体查询及旧请求等待，查看query_diagnostics |
| connect后仍不允许查询 | Driver是否完成映射并调用activate，是否出现断线或早到推送 |
| 旧请求尚未结束 | 不要覆盖pending或盲目重试，等待匹配最终分片或按外围失败处理 |
| 仓位/资金账户不匹配 | broker、investor与回报身份是否相同，不接受其他账户记录 |
| 净仓为0却无法通过空仓检查 | 检查gross多空总仓及活动订单 |
| 发送成功但无成交 | API受理、订单接受、成交是不同阶段，查看Driver执行回报 |
| 撤单缺少映射 | 是否取得该OrderRef对应订单推送及FrontID/SessionID |
| close后仍有柜台订单 | close只释放传输资源，不能代替撤单和最终状态核验 |

相关回归：[原生Transport假API测试](../../../tests/run_p4_ctp_td_transport.py)、[查询超时/迟到/拒绝回归](../../../tests/test_ctp_query_lifecycle.py)、[公共配置测试](../../../tests/test_live_common.py)。这些测试使用假柜台，不证明真实前置当前可用。

相关说明：[build_simnow_transport](build_simnow_transport.md)、[CtpMarketSessionLifecycle](CtpMarketSessionLifecycle.md)、[RiskLimits](RiskLimits.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
