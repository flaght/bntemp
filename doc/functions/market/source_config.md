# source_config

用途：从参数对象中取得已经解析的MarketSourceConfig，或根据参数字段创建一份回退行情配置。

- 导入路径：`from bomber.framework.market.sources import source_config`
- [函数、配置结构及来源工厂源码](../../../bomber/framework/market/sources.py)
- 核对日期：2026-10-28，Asia/Shanghai。

## 1. 函数签名与用途

```python
def source_config(args):
    ...
```

唯一参数可直接传入MarketSourceConfig，也兼容已有argparse.Namespace或有相应属性的对象，无默认值。返回行情配置，不创建Feed、不连接网络、不读取凭据，不选择执行客户端。

实际逻辑：

1. 已是MarketSourceConfig时直接返回同一对象，供行情工厂显式传入配置。
2. 兼容路径中，`args.market_source_config`存在且为真值时返回同一对象。
3. 否则读取几个已知属性，构造MarketSourceConfig。

已有对象不会复制或重新验证其类型，也不会因其他args字段被修改而自动更新。回退分支新建的对象不会写回args.market_source_config，多次调用可得到内容相同但不同身份的对象。

## 2. 回退字段与默认值

| args属性 | 配置字段 | 属性不存在时 |
| --- | --- | --- |
| market_source | source | "ctp" |
| market_bar_table | table | None |
| market_bar_timestamp | timestamp_label | None |
| market_volume_mode | volume_mode | None |
| market_bar_max_age_seconds | max_age_seconds | 120 |

这里使用getattr默认值，属性已存在但为None时不会自动回退。例如market_source=None会交给配置校验并失败，不能假定等价于未设置。

回退分支没有读取market_config JSON路径或DDB_MARKET_BAR_TABLE，也没有从args提取ws_base_url、market_type；后两项沿用MarketSourceConfig默认None、spot。需要完整优先级时先调用market_config_from_args，再使用此访问函数。

## 3. MarketSourceConfig校验与identity

配置为frozen数据类，主要字段为source、table、timestamp_label、volume_mode、max_age_seconds、ws_base_url、market_type。

- source支持file、ctp、bn-ws、dolphindb，字符串精确匹配。
- max_age_seconds必须有限且满足`60 < max_age_seconds <= 120`，不是任意Tick级时效参数。
- dolphindb要求非空table，并明确timestamp_label为start/end、volume_mode为per-bar/cumulative。
- market_type支持spot/futures。

identity属性通过asdict返回配置字段字典，用于报告和来源身份说明，不包含数据库密码。它不是数据内容哈希或实时连接状态。

source_config只触发配置对象自身校验，不执行完整入口约束；不能依靠它单独判断某个国内期货demo是否允许bn-ws，或Live是否允许file。

## 4. 与market_config_from_args的区别

| 接口 | 工作 |
| --- | --- |
| market_config_from_args | 读取可选JSON，显式CLI覆盖配置，补充DDB表环境值，检查回测/Live和业务约束，回写规范化字段与配置对象 |
| source_config | 优先取已解析对象，否则按少量属性构造回退配置 |
| create_market_feed | 根据配置创建具体Feed，连接仍由外围生命周期负责 |
| assemble_minute_market | 组合来源、分钟行情及健康检查等 |

Hybrid入口通过load_market_config显式读取JSON路径和来源，返回MarketSourceConfig；后续运行器、会话准备和行情工厂直接传递该配置，不再传递命令行对象。其他demo的旧Namespace调用仍兼容。

## 5. 调用示例

```python
from types import SimpleNamespace
from bomber.framework.market.sources import MarketSourceConfig, source_config

fallback = source_config(SimpleNamespace())
assert fallback.source == "ctp"
assert fallback.max_age_seconds == 120

prepared = MarketSourceConfig(
    source="dolphindb",
    table="example_minute_stream",
    timestamp_label="end",
    volume_mode="per-bar",
)
args = SimpleNamespace(market_source_config=prepared, market_source="ctp")
assert source_config(args) is prepared
assert source_config(args).identity["source"] == "dolphindb"
```

已有配置优先，上例不会因market_source="ctp"覆盖prepared。修改配置应重新解析或明确创建新配置，不能只改一个旧args字段期待访问函数同步重建。

## 6. 常见错误与排查

| 现象 | 检查 |
| --- | --- |
| JSON改了却未生效 | 是否重新执行market_config_from_args，已有对象会优先返回 |
| table环境变量没读取 | source_config不处理该环境回退，先执行完整参数解析 |
| None导致未知来源/类型错误 | 属性存在与属性缺失不同，检查参数初始化 |
| 配置取得但没有行情 | 尚未创建、注册和连接Feed |
| source_config通过但入口拒绝 | 完整入口还有Live/回测、国内合约和时间模式约束 |

相关回归：[行情来源测试](../../../tests/test_market_sources.py)。实际入口：[Hybrid实时入口](../../../demos/10_hybrid_transformer/run_live.py)。

相关说明：[CtpMarketSessionLifecycle](../trader/CtpMarketSessionLifecycle.md)、[RuntimeMode](../trader/RuntimeMode.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
