# MarketSourceConfig

用途：集中声明行情来源及分钟数据语义，校验配置，并提供可写入报告和恢复身份的字段字典。

- 导入路径：`from bomber.framework.market.sources import MarketSourceConfig`
- [配置、参数解析与Feed工厂源码](../../../bomber/framework/market/sources.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 类型与完整构造参数

这是frozen数据类，构造后不能普通赋值修改字段；支持位置参数及关键字参数。构造只校验配置，不读取JSON、环境变量，不访问数据库或连接行情。

```python
def __init__(self, source="ctp", table=None, timestamp_label=None,
             volume_mode=None, max_age_seconds=120,
             ws_base_url=None, market_type="spot"):
    ...
```

| 字段 | 标注类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| source | str | ctp | file、ctp、bn-ws、dolphindb之一 |
| table | str或None | None | DolphinDB共享分钟流表名，不是本地文件路径或参考库名 |
| timestamp_label | str或None | None | DolphinDB分钟时间标签start/end |
| volume_mode | str或None | None | DolphinDB量值语义per-bar/cumulative |
| max_age_seconds | float | 120 | 分钟源时效/就绪预算相关参数，须有限且在(60,120]秒 |
| ws_base_url | str或None | None | Binance WebSocket基础地址覆盖 |
| market_type | str | spot | Binance市场类型spot/futures |

类型标注不是全面运行时类型验证；__post_init__执行下列具体规则。字符串精确匹配，不自动转大小写或去空格。

## 2. 校验规则与边界

source不在四个合法值中抛ValueError。max_age_seconds为NaN、无穷、60及以下或大于120会失败；该检查对所有source都执行，不能因是file或ctp就任意设为0。传字符串等错误类型可能直接触发TypeError，不会自动转浮点数。

dolphindb还要求table为真值，timestamp_label为start/end，volume_mode为per-bar/cumulative。源码不猜测表名；table检查并非完整格式检查，例如不会主动剥掉空白或验证远端表是否存在。

market_type始终必须为spot/futures，即使当前source不是bn-ws。非DolphinDB来源不会在构造时强制检查timestamp_label/volume_mode，也没有验证WebSocket URL是否可达；配置成功不等于数据源健康或所有字段都会被消费。

## 3. 不同来源如何使用

| source | 工厂与数据路径 | 需要注意 |
| --- | --- | --- |
| file | FileReplayFeed | 文件、Parser、合约注册由其他装配声明；本配置没有path字段 |
| ctp | CtpLiveDataFeed，后续可聚合完整分钟 | 凭据和MD配置由生命周期/环境提供；不是table |
| dolphindb | DolphinDbLiveDataFeed订阅共享分钟表 | 必须声明时间标签、量值语义；连接凭据来自环境 |
| bn-ws | BNWSStreamDataFeed | 默认spot；futures选择相应默认URL，可用ws_base_url覆盖 |

create_market_feed构造不连接，连接生命周期由外围管理。默认Binance地址为spot的wss://stream.binance.com:9443或futures的wss://fstream.binance.com，具体取决于market_type；这是当前源码的工厂默认。

start/end描述分钟数据标签，不能用接收时间替代事件时间。per-bar表示每分钟量值；cumulative声明累计语义，后续Feed按相应规则处理，不能把同一累计量重复当每分钟新增量。

max_age_seconds不是所有来源所有健康检查的统一超时。DolphinDB工厂用于startup_grace_seconds及stale_after_seconds，非CTP可提高生命周期ready_seconds；CTP逐合约健康还可使用独立instrument_max_age_seconds，TD查询超时也另行配置。

## 4. identity属性

identity通过dataclasses.asdict返回所有七个字段的新字典，包括None和当前来源可能不使用的字段。修改字典不会改变配置对象。

它用于报告、来源身份和恢复一致性，不是数据内容哈希，不证明连接就绪或订阅成功。配置字段没有用户名/密码，但自定义ws_base_url会原样进入identity，不应把敏感凭据嵌入URL后期待自动脱敏。

## 5. 与参数解析函数的关系

| 接口 | 职责 |
| --- | --- |
| MarketSourceConfig | 保存字段并执行对象自身校验 |
| market_config_from_args | 可选JSON→显式CLI覆盖→DolphinDB表环境回退，构造配置并检查运行组合，回写args |
| source_config | 优先返回args.market_source_config，否则以少数字段构造回退对象 |
| create_market_feed | 按配置创建具体Feed，不启动连接 |

market_config_from_args额外要求回测用file、Live用非file，非CTP只能realtime；domestic_contracts=True时拒绝bn-ws。直接构造MarketSourceConfig不会执行这些业务约束。

source_config回退不读取ws_base_url或market_type，完整JSON解析则可传递它们。要保留完整配置，应先market_config_from_args，再通过source_config访问，或明确传已构造配置。

## 6. 调用示例

```python
from dataclasses import replace
from bomber.framework.market.sources import MarketSourceConfig

default = MarketSourceConfig()
assert default.source == "ctp"

dolphin = MarketSourceConfig(
    source="dolphindb", table="example_minute_stream",
    timestamp_label="end", volume_mode="per-bar", max_age_seconds=90,
)
assert dolphin.identity["timestamp_label"] == "end"
identity = dolphin.identity
identity["table"] = "other"
assert dolphin.table == "example_minute_stream"

binance = MarketSourceConfig(source="bn-ws", market_type="futures")
updated = replace(dolphin, max_age_seconds=120)
assert updated.max_age_seconds == 120
```

replace创建新对象并再次校验，不修改旧配置，也不会自动重新配置已连接Feed或已保存的恢复身份。

## 7. 常见问题

| 现象 | 检查 |
| --- | --- |
| max_age_seconds=60报错 | 下界严格大于60，不是大于等于 |
| DolphinDB缺少字段被拒绝 | 表名、时间标签和量值语义均需声明 |
| file没找到输入文件 | 配置没有文件路径字段，需独立Reader/Parser装配 |
| 配置合法却国内期货入口拒绝 | 入口还有合约市场与运行模式限制 |
| 更新args未改变已有配置 | source_config优先复用已有market_source_config |
| 已创建配置但无行情 | 还需创建、注册、启动并等待Feed就绪 |

相关回归：[行情来源配置与装配测试](../../../tests/test_market_sources.py)。

相关说明：[source_config](source_config.md)、[ExecutionPolicy](../trader/ExecutionPolicy.md)、[StrategyPositionSource](../trader/StrategyPositionSource.md)、[prepare_inputs](../demos/prepare_inputs.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
