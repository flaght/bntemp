# build_simnow_transport

用途：从环境变量取得SimNow交易前置与凭据配置，解析运行时请求超时，再调用指定工厂创建交易Transport对象。

- 导入路径：`from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport`
- [函数、必需配置及超时解析源码](../../../bomber/framework/trader/runtime/live/channels/ctp.py)
- [通常使用的Transport实现](../../../bomber/framework/trader/execution/ctp/td_transport.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 这个函数负责什么

它将入口重复的环境配置读取集中起来：先解析超时，再要求BrokerID为9999，读取账户、交易前置和密码，将全部参数交给transport_factory。

通常factory为CtpTdApiTransport，但也可在测试中注入只保存关键字的替身。返回值就是工厂返回的对象，不包装为生命周期、Driver或执行组件。

本函数自身不调用connect、activate，不登录、不查询、不报单、不创建账户锁，也不读取.env文件；入口应在需要时先加载环境配置。工厂本身是否有额外副作用取决于实际传入实现。

## 2. 完整签名与参数

```python
def build_simnow_transport(
    transport_factory,
    *,
    client_id,
    flow_path,
    timeout_seconds=None,
):
    ...
```

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `transport_factory` | 必填 | 接收CTP连接关键字的工厂，通常为CtpTdApiTransport |
| `client_id` | 必填关键字 | 框架中的交易客户端标识，原样传给工厂 |
| `flow_path` | 必填关键字 | CTP原生流文件目录的回退值，环境变量可覆盖 |
| `timeout_seconds` | None | 显式请求超时秒数；None时读取环境变量，再回退30秒 |

client_id和flow_path不是从策略内部猜出的。此函数不自行完整检查client_id，默认Transport会验证连接标识。flow_path仅是原生API流文件路径，不是策略检查点、报告目录或账户锁。

## 3. 环境变量与工厂字段对应

| 来源 | 默认/要求 | 传给工厂 |
| --- | --- | --- |
| 显式client_id | 必填 | client_id |
| CTP_ACCOUNT_ID | 必需非空 | 同时作为account_id和investor_id |
| CTP_BROKER_ID | 必需且为9999 | broker_id固定传9999 |
| CTP_TD_ADDRESS | 必需非空 | front |
| CTP_PASSWORD | 必需非空 | password |
| CTP_APP_ID | 默认空字符串 | app_id |
| CTP_AUTH_CODE | 默认空字符串 | auth_code |
| CTP_TD_FLOW_PATH | 未设置时使用flow_path参数 | flow_path |
| CTP_PRODUCTION_MODE | 默认true | 小写后为1/true/yes/on时True，否则False |
| 超时解析结果 | 默认30秒 | timeout_seconds |

必需变量经required去首尾空格并拒绝空值。其他变量通过os.getenv读取，不统一strip；例如已设置但为空的CTP_TD_FLOW_PATH仍会覆盖显式flow_path，默认Transport连接时再按空路径采用其内部默认目录。

production_mode是原生API创建参数，不是“允许真实交易”的授权开关。函数名和BrokerID限制也不等于全面检查前置地址属于哪个部署环境；完整渠道入口还有自己的环境和会话核验。

此处不读取CTP_MD_ADDRESS，也不构造行情客户端；MD和TD配置分别处理。

## 4. 超时优先级与范围

通过ctp_td_timeout_seconds解析：

```text
显式timeout_seconds（非None）
    → 否则CTP_TD_TIMEOUT_SECONDS
    → 否则30秒
```

要求为可转float的有限数，满足`0 < seconds <= 120`，布尔值不接受。0、负数、空串、NaN、Infinity及超过120都失败，而且在创建Transport之前失败。

显式值优先，因此有效显式值可覆盖无效环境值。返回float后传给工厂。这个配置是运行时请求等待预算，不是整个策略运行秒数，也不是报单确认或成交超时承诺。

**默认值有两层：** 本工厂默认30秒，而直接构造CtpTdApiTransport的类默认是10秒。使用哪一条入口就应按对应规则核对，不要混为一个默认值。

函数用CtpConnection日志记录client_id和最终timeout_seconds，不记录密码、认证码或账户资金。

## 5. 实际调用与后续流程

当前Hybrid实时入口通过：

```python
transport = build_simnow_transport(
    CtpTdApiTransport,
    client_id=CLIENT_ID,
    flow_path="/tmp/bomber-hybrid-td",
    timeout_seconds=args.query_timeout,
)
```

随后将transport交给相应会话生命周期和Driver装配。构造成功只代表配置和对象创建完成，不表示已连接、已登录、已确认结算、已完成账户对账或已允许订单。

CtpNativeTraderDriver及上层受控执行链管理订单身份、回报映射、对账、风控和交易闸门；本工厂不替代它们。

## 6. 不联网示例

下面用工厂替身验证配置传递，全部为虚构测试值，不创建原生API或网络连接：

```python
from types import SimpleNamespace
from unittest.mock import patch
from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport

environment = {
    "CTP_BROKER_ID": "9999",
    "CTP_ACCOUNT_ID": "example-user",
    "CTP_PASSWORD": "example-placeholder",
    "CTP_TD_ADDRESS": "tcp://example.invalid:1234",
    "CTP_TD_TIMEOUT_SECONDS": "45",
}
with patch.dict("os.environ", environment, clear=True):
    config = build_simnow_transport(
        SimpleNamespace,
        client_id="example-simnow",
        flow_path="/tmp/example-td",
        timeout_seconds=15,
    )
assert config.timeout_seconds == 15.0
assert config.account_id == config.investor_id == "example-user"
assert config.broker_id == "9999"
```

实际环境将SimpleNamespace替换为CtpTdApiTransport，并由既有会话生命周期管理连接。不要打印返回对象的凭据字段来验证配置。

## 7. 常见错误与排查

| 现象 | 重点检查 |
| --- | --- |
| 超时配置ValueError | 显式值与环境值的优先级、单位和(0,120]范围 |
| 缺少环境变量 | 是否在调用前加载对应运行环境；函数不自动加载.env |
| 仅允许BrokerID=9999 | 该工厂明确限定SimNow BrokerID，非通用生产凭据入口 |
| 传了flow_path却采用其他目录 | CTP_TD_FLOW_PATH是否存在，包括空字符串覆盖 |
| 创建成功但没有登录 | 工厂不连接，后续由Driver/生命周期调用Transport.connect |
| 等待时间与10秒不同 | 工厂默认30秒，直接类构造默认10秒 |
| 误以为production_mode是下单许可 | 它只是原生创建参数，交易闸门由其他组件实现 |

相关回归：[公共Live配置测试](../../../tests/test_live_common.py)中的SetupTests，验证优先级、非法值、凭据和无连接构造。实际入口：[Hybrid实时入口](../../../demos/10_hybrid_transformer/run_live.py)。

相关说明：[CtpTdApiTransport](CtpTdApiTransport.md)、[CtpMarketSessionLifecycle](CtpMarketSessionLifecycle.md)、[RecordingExecutionLifecycle](RecordingExecutionLifecycle.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
