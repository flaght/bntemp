# CtpFuturesBasicProfile

用途：声明 CTP 期货基础模拟所使用的交易场所和账户规则，并按显式条款创建原生期货合约。

- 导入路径：`from bomber.framework.trader import CtpFuturesBasicProfile`
- 实际定义：`bomber.framework.trader.execution.simulation.ctp.CtpFuturesBasicProfile`。
- [Profile 与合约工厂源码](../../../bomber/framework/trader/execution/simulation/ctp.py)
- [通用交易场所配置](../../../bomber/framework/trader/execution/simulation/profile.py) · [Backend 注册逻辑](../../../bomber/framework/trader/execution/simulation/backend.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个类负责什么

这是一个 `@dataclass(frozen=True)` 配置类，包含两项主要能力：

1. `build_backend_config()`：生成可传给 `BacktestEngine.add_venue` 的模拟交易场所配置。
2. `make_instrument(...)`：根据合约身份、价位、乘数、生命周期和保证金比例创建 `FuturesContract`。

它声明基础净持仓、CNY 保证金账户、固定每手手续费等规则。回测引擎的创建、启动、行情推进和资源释放由 `NautilusSimExecutionBackend` 负责；策略和执行客户端负责目标、订单规划及策略级风控。

构造 Profile 不会连接 CTP，不读取合约基础表，不创建实际账户连接，也不会开始撮合。它本身没有 `start()`、`run()` 或 `stop()`。

`frozen=True` 表示构造后不能直接赋值修改配置字段。需要另一套参数时应新建实例，或通过 `dataclasses.replace` 生成新实例；替换 Profile 对象不会自动重配已经注册进引擎的 Venue。

## 2. 构造参数

数据类生成的构造接口相当于：

```python
from decimal import Decimal
from bomber.model import Venue
from bomber.framework.trader import CtpFuturesBasicProfile

profile = CtpFuturesBasicProfile(
    profile_id="ctp-futures-basic",
    starting_balance=Decimal("1000000"),
    commission_per_contract=Decimal("1"),
    venue=Venue("SHFE"),
)
```

所有参数都有默认值。默认 `Venue` 通过 `default_factory` 创建。

| 参数 | 类型 | 默认值 | 含义与约束 |
| --- | --- | --- | --- |
| `profile_id` | `str` | `"ctp-futures-basic"` | 规则配置标识；去除首尾空白后不能为空，但存储时保留原字符串 |
| `starting_balance` | `Decimal` / `int` / `float` / `str` | `Decimal("1000000")` | 模拟账户起始资金，单位 CNY，须大于零 |
| `commission_per_contract` | 同上 | `Decimal("1")` | 每次成交每手的固定手续费金额，单位 CNY，须非负，允许零 |
| `venue` | `Venue` | `Venue("SHFE")` | 当前配置对应的交易场所；建议显式传入与基础资料一致的 `Venue` 对象 |

金额会经 `_decimal` 转为 `Decimal`：已经是 Decimal 则原样使用，否则采用 `Decimal(str(value))`。应优先传 Decimal 或十进制字符串；如果浮点数此前已经产生误差，这一步不会恢复原本期望的数值。

`venue` 字段在这个类的构造阶段没有自行归一化。不要把 `XSGE` 等来源编码直接当成统一交易所身份；应先用 `venue_for_product` 得到 `SHFE` 等框架代码，再构造 `Venue`。

## 3. 三个标识和起始资金的含义

| 标识或参数 | 作用 |
| --- | --- |
| `profile_id` | 标识采用的模拟规则配置；还会写入本 Profile 创建的合约 `info` |
| `venue` | 模拟交易场所身份，例如 `SHFE`；Backend 按它检查重复注册 |
| `backend_id` | 属于 Backend 的执行路由身份，不在这个 Profile 的构造参数中 |
| `starting_balance` | 在注册模拟 Venue 时提供账户起始余额，不是每个合约或每个策略分配一次的独立资金 |

同一 Venue 下注册多个合约并不意味着每个合约各有一百万起始资金。同一个 Backend 也不能靠更换 `profile_id` 来重复注册同一 Venue，重复判断使用的是 `str(profile.venue)`。

资金上限与策略下单限制分属不同层：`starting_balance` 配置模拟账户；`max_abs_position`、`max_order_notional` 等由外围 `RiskLimits` 和风控管理器设置。本 Profile 不接收 `max_notional` 参数。

## 4. build_backend_config 具体生成什么

```python
config = profile.build_backend_config()
backend.add_profile(profile)
```

第一句生成配置字典；第二句内部会再次调用 `build_backend_config()`，再执行 `engine.add_venue(**config)`。通常直接 `add_profile` 即可，不必手动为同一 Venue 再调用一次 `engine.add_venue`。

本方法先创建 `GenericVenueProfile`，再取得它的配置映射。主要显式设置如下：

| 配置项 | 值 | 含义 |
| --- | --- | --- |
| `venue` | `self.venue` | 模拟交易场所 |
| `oms_type` | `OmsType.NETTING` | 使用净持仓模式 |
| `account_type` | `AccountType.MARGIN` | 使用保证金账户 |
| `starting_balances` | `[Money(starting_balance, CNY)]` | 单一 CNY 起始余额 |
| `base_currency` | `CNY` | 账户基础币种 |
| `default_leverage` | `Decimal(1)` | 显式账户杠杆参数；不能据此把保证金误解为全额占款 |
| `margin_model` | `StandardMarginModel()` | 使用合约声明的固定保证金比例模型 |
| `fee_model` | `PerContractFeeModel(Money(commission_per_contract, CNY))` | 按成交手数收取固定费用 |
| `use_position_ids` | `False` | 关闭该模拟配置中的独立持仓 ID 用法 |
| `use_reduce_only` | `True` | 支持只减仓订单约束 |
| `use_market_order_acks` | `True` | 开启市价单确认事件 |
| `bar_execution` | `True` | 允许基于 Bar 推进执行 |
| `trade_execution` | `True` | 允许基于 TradeTick 推进执行 |

其余配置继承 `GenericVenueProfile` 默认值。例如 `book_type=L1_MBP`、`use_message_queue=True`、`liquidity_consumption=False`、`queue_position=False`，并未在此显式提供 `fill_model`、`latency_model` 或额外结算模块。

因此，基础配置不声明队列位置和流动性消耗模型。它也不会仅凭 `bar_execution=True` 就知道分钟内真实成交路径；执行细节仍由引擎及其实际模型决定。

每次调用会生成新的配置容器和模型对象。修改返回字典不会修改冻结的 Profile，也不会自动修改已经注册的 Venue。

## 5. 手续费如何计算

对于本类创建的普通单腿期货合约，固定手续费按本次成交数量计算：

```text
本次手续费 = commission_per_contract × 本次成交手数
```

例如显式设置每手 `1` CNY：

| 成交情形 | 手续费 |
| --- | --- |
| 买入开仓 1 手 | 1 CNY |
| 卖出平仓 1 手 | 1 CNY |
| 完成 1 手开仓再平仓 | 合计 2 CNY |
| 完成 3 手开仓再全部平仓 | 合计 6 CNY |

按每次新增成交计费，不是每笔委托固定一次，也不是按目标仓位收一次。分批成交的费用随各次成交量计算；最终金额经过引擎 Money 币种精度处理。

这个参数是固定金额，不是百分比，计算时不额外乘成交价或合约乘数。本类创建的合约虽然设置 `maker_fee=0`、`taker_fee=0`，模拟 Venue 仍然使用指定的 `PerContractFeeModel` 收费，不能由两个零字段推断没有手续费。

开仓、普通平仓、平今和平昨在此没有差异费率。示例和默认的每手 1 CNY 都是程序配置，不代表任何交易所或期货公司的实际收费标准。

## 6. 保证金、乘数和净持仓

### 保证金比例属于合约

本 Profile 的构造参数中没有 `margin_init`、`margin_maint`。它们在 `make_instrument` 创建每个合约时传入：

```text
名义金额 = 价格 × 手数 × 合约乘数
初始保证金 = 名义金额 × margin_init
维持保证金 = 名义金额 × margin_maint
```

这是当前基础模型对普通线性期货的示例口径。`StandardMarginModel` 使用固定比例，不再将上述保证金除以账户 leverage；当前 Profile 的 `default_leverage=1` 也不表示 `margin_init` 自动变成 100%。

以价格 `3100`、数量 `1` 手、乘数 `10`、初始比例 `0.10`、维持比例 `0.08` 为例：

| 项目 | 计算 | 金额 |
| --- | --- | --- |
| 名义金额 | `3100 × 1 × 10` | 31000 CNY |
| 初始保证金 | `31000 × 0.10` | 3100 CNY |
| 维持保证金 | `31000 × 0.08` | 2480 CNY |

对应回归还检查了开仓后的维持保证金锁定和平仓后的释放。比例不是手续费；资金锁定不等于永久损失，也不能用保证金占用代替完整账户权益和损益计算。

### 当前调用链的默认值在哪里

当前 `dataprep.futures.instrument` 和 `make_instrument` 两个装配函数默认传入：

```python
margin_init = Decimal("0.10")
margin_maint = Decimal("0.08")
```

这些是装配函数默认值，不是 `profile.make_instrument` 的默认值；后者要求调用方显式提供两个比例。当前 HybridTransformer 没有覆盖装配函数的这两个参数，因此使用该默认值，也没有从基础表自动读取交易所实际保证金率。

### NETTING 表示什么

在账户净持仓层面，同一合约的买卖作用于净数量。例如原有多头 3 手，再卖出 1 手，净仓变为多头 2 手；它不表达同时独立持有多头 3 手和空头 1 手的真实柜台双向结构。

减仓订单通过 reduce-only 约束避免扩大或反向建立仓位。内部订单网关将 `CLOSE`、`CLOSE_TODAY`、`CLOSE_YESTERDAY` 都处理为减少持仓语义，这并没有实现今昨仓分账。

## 7. make_instrument 参数与返回值

完整方法签名为：

```python
def make_instrument(
    self,
    symbol: str,
    *,
    underlying: str,
    price_precision: int,
    price_increment: Decimal | int | float | str,
    multiplier: Decimal | int | float | str,
    activation_ns: int,
    expiration_ns: int,
    margin_init: Decimal | int | float | str,
    margin_maint: Decimal | int | float | str,
) -> FuturesContract:
    ...
```

除 `symbol` 外均须按名称传入，所有参数必填。

| 参数 | 含义与约束 |
| --- | --- |
| `symbol` | 原始合约代码，例如 `rb2704`，去首尾空格后不能为空；不要附加 `.SHFE` 后缀 |
| `underlying` | 标的或品种描述，例如 `rb`，去首尾空格后不能为空 |
| `price_precision` | 非负价格小数位数，须与最小价位精度兼容；不是价格跳动单位 |
| `price_increment` | 大于零的最小价格变动，例如 `1` 或 `0.5` |
| `multiplier` | 大于零的合约乘数，例如 `10`，不是下单手数 |
| `activation_ns` | 非负 UTC epoch 纳秒时间戳，表示合约生效边界 |
| `expiration_ns` | UTC epoch 纳秒时间戳，须严格晚于 activation |
| `margin_init` | 初始保证金比例，满足 `0 < margin_init <= 1` |
| `margin_maint` | 维持保证金比例，满足 `0 < margin_maint <= margin_init` |

返回原生 `FuturesContract`，主要字段为：

| 字段 | 本方法设置的内容 |
| --- | --- |
| `id` | `InstrumentId.from_str(f"{symbol去空格}.{profile.venue}")` |
| `raw_symbol` | 去空格后的 symbol；本方法不改变大小写 |
| `asset_class` | 固定 `AssetClass.COMMODITY`，不会根据交易所推断为 INDEX |
| `currency` | 固定 CNY |
| `price_precision`、`price_increment` | 调用方提供的精度与价位 |
| `multiplier` | 转为 Quantity 的合约乘数 |
| `lot_size` | 固定 1 手 |
| `underlying` | 去空格后的 underlying；不自动推断或改变大小写 |
| `activation_ns`、`expiration_ns` | 调用方提供的生命周期边界 |
| `margin_init`、`margin_maint` | 调用方提供的保证金比例 |
| `maker_fee`、`taker_fee` | 均为 Decimal(0)；场所固定费用由 fee_model 负责 |
| `exchange` | `str(profile.venue)` |
| `ts_event`、`ts_init` | 固定为 0，不是资料真实发布时间 |
| `info` | `market_type="CTP_FUTURES_BASIC"` 及当前 `profile_id` |

本方法先对 tick 的 Decimal 去掉无意义尾零，再构造 Price：`1.0` 规范为 `1`，`0.50` 规范为 `0.5`，避免尾零导致价位精度与 `price_precision` 冲突。它不会替调用方自动推断 `price_precision`；外围 `instrument_factory.precision(tick)` 才负责该推导。

直接创建合约不会自动注册到 Backend，也不验证 symbol 是否属于该 Venue 或是否与基础表一致。当前方法固定商品资产类别，仅修改 Venue 不会自动形成适合所有金融期货的资产分类。

## 8. 通过基础资料装配时的字段和时间约定

当前入口先读取基础表，再查询交易所并创建 Profile，随后调用 `dataprep.futures.instrument`。基础字段的主要流向为：

| 基础表或输入 | 合约参数来源 |
| --- | --- |
| 调用方选中的真实合约 symbol | 合约身份 |
| `code` | 品种，外围工厂转小写作为 underlying |
| `exchangeCD` | 映射到统一 Venue，并校验与 Profile 一致 |
| `minChgPriceNum` | tick 和推导的 price_precision |
| `contMultNum` | multiplier |
| `listDate` | activation 的日期来源 |
| `lastTradeDate` | expiration 的日期来源 |
| 装配函数显式参数或其默认值 | margin_init / margin_maint |

这条调用链在 [instrument_factory.py](../../../bomber/framework/trader/instrument_factory.py) 中沿用以下边界：上市日 UTC 00:00 生效，最后交易日 UTC 00:00 加一天作为到期边界。它不是本方法自动推断的交易所最后交易时刻，也不是上海时区的当日收盘时间。

直接调用 `profile.make_instrument` 时，应自行提供明确的 UTC epoch 纳秒边界；不能把秒或毫秒当作纳秒。`SessionCalendar` 负责盘中交易时段和夜盘归属，与这里的合约生命周期边界是两套不同信息。

基础资料装配链还会校验合约行唯一性、品种及交易所一致性、tick、乘数和日期等；本 Profile 本身不读文件，也不承担历史版本的 as-of 发布时间选择。

## 9. 调用示例

### 直接创建配置与合约

以下是演示条款，需要可用的 Bomber 引擎环境；金额、日期和比例均为明确示例，不代表正式交易参数。

```python
from datetime import datetime, timezone
from decimal import Decimal
from bomber.model import Venue
from bomber.framework.trader import CtpFuturesBasicProfile


def utc_ns(year, month, day):
    delta = datetime(year, month, day, tzinfo=timezone.utc) - datetime(
        1970, 1, 1, tzinfo=timezone.utc,
    )
    return delta.days * 86_400_000_000_000


profile = CtpFuturesBasicProfile(
    profile_id="example-ctp-basic",
    starting_balance="1000000",
    commission_per_contract="1",
    venue=Venue("SHFE"),
)
contract = profile.make_instrument(
    "rb2704",
    underlying="rb",
    price_precision=0,
    price_increment=Decimal("1.0"),
    multiplier=Decimal("10"),
    activation_ns=utc_ns(2026, 1, 1),
    expiration_ns=utc_ns(2027, 5, 1),
    margin_init=Decimal("0.10"),
    margin_maint=Decimal("0.08"),
)
assert str(contract.id) == "rb2704.SHFE"
assert str(contract.price_increment) == "1"
assert profile.starting_balance == Decimal("1000000")
```

### 注册到 Backend

沿用上例的 `profile` 和 `contract`：

```python
from bomber.framework.trader import NautilusSimExecutionBackend

backend = NautilusSimExecutionBackend("example-ctp-sim")
try:
    backend.add_profile(profile)
    backend.add_instrument(contract)
    # 继续装配行情、策略与执行客户端，再选择批量或逐事件运行方式。
finally:
    backend.stop()
```

只注册这些对象不会自动产生订单。实际成交需要行情推进和订单来源。

### 当前入口的基础资料调用方式

假设已有完整基础表 `basic`、规范化品种 `product` 和真实合约代码 `symbol`：

```python
from bomber.framework.dataprep.metadata import venue as venue_for_product
from bomber.framework.dataprep.futures import instrument as build_instrument

profile = CtpFuturesBasicProfile(
    starting_balance=Decimal("1000000"),
    commission_per_contract=Decimal("1"),
    venue=Venue(venue_for_product(basic, product)),
)
contract, meta, multiplier = build_instrument(
    basic, profile, product, symbol,
    margin_init=Decimal("0.10"),
    margin_maint=Decimal("0.08"),
)
```

`meta` 用于行情侧合约元信息，`multiplier` 可传给策略计算及风控；这条装配函数返回三个结果，直接 `profile.make_instrument` 只返回合约对象。

## 10. 校验与使用边界

| 情形 | 当前处理 |
| --- | --- |
| profile_id 为空或仅空格 | `ValueError` |
| 起始资金不大于零 | `ValueError` |
| 每手手续费为负 | `ValueError`；零手续费允许 |
| 金额字符串无法转 Decimal | Decimal 转换异常；不会统一包装成 InputError |
| symbol / underlying 为空 | `ValueError` |
| 价格精度为负、tick 或乘数不大于零 | `ValueError`；原生对象还有后续类型和精度校验 |
| activation 为负，或 expiration 不晚于 activation | `ValueError` |
| 初始保证金比例不在 `(0, 1]` | `ValueError` |
| 维持比例不在 `(0, margin_init]` | `ValueError` |
| 同一 Venue 注册第二个 Profile | Backend 拒绝，与 profile_id 是否不同无关 |
| 直接修改冻结字段 | 数据类拒绝赋值 |

这里的本地比较校验不构成完整的有限数和类型检查，例如没有统一调用 `Decimal.is_finite()`；不要据此推断 NaN、Infinity 或任意参数类型都已被妥善验证。当前 HybridTransformer 入口对名义金额上限和费用另有有限性检查，基础资料解析也提供额外校验。

该 Profile 的明确能力范围是基础净持仓模拟：不提供真实 CTP 双向持仓、今昨仓分账、平今平昨费率差异、交易日切换结算、保证金历史版本或交易所时段例外。它也不提供网络订单会话或真实账户登录。

相关回归：[CTP 基础模拟分步测试](../../../tests/run_ctp_simulation.py)，阶段 1 检查配置和合约，阶段 2 读取指定离线行情验证开平仓、手续费和保证金。阶段 2 脚本含固定行情路径，需要实际数据；不能仅凭脚本存在认定当前环境已通过撮合回归。

相关实现：[基础表合约装配](../../../bomber/framework/dataprep/futures.py)、[公共合约工厂](../../../bomber/framework/trader/instrument_factory.py)、[内部订单网关](../../../bomber/framework/trader/execution/simulation/gateway.py)。实际入口：[HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

相关说明：[NautilusSimExecutionBackend](NautilusSimExecutionBackend.md)、[venue_for_product](../dataprep/venue_for_product.md)、[SessionCalendar](../calendars/SessionCalendar.md)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
