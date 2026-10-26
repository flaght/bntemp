# 期货手续费与账户系数

`fut_basic` 保存交易所原始标准；实际回测费用由合约标准费用与账户系数共同决定。
本次不改数据库表、不覆盖已有 role 文件。重新补充或导出资料时，原样保留
`tradeCommiNum`、`tradeCommiUnit`、`deliCommiNum`、`deliCommiUnit`。

## 单位与标准化

| 源数值 | 源单位 | 内部每手费用 | 内部成交金额费率 |
|---|---|---|---|
| 2.5000 | 元/手 | 2.5 | 0 |
| 0.0045 | % | 0 | 0.000045 |

`FuturesFee.from_mapping(row)` 读取交易费用，`kind="delivery"` 读取交割费用。
内部 `per_contract` 为人民币元/手，`rate` 为小数比例。源单位允许两端及内部空白、
全角百分号和斜杠；其他单位必须显式适配，不按数值大小猜测。
`source_value`、`source_unit` 保留原始语义。输入若同时含原始字段与
`trade_fee_per_contract`、`trade_fee_rate`，两套值必须一致。

`FutureBasic.trade_fee`、`delivery_fee` 和 `InstrumentSpec` 保留解析结果；
`FutureBasic.trade_fee_per_contract`、`trade_fee_rate` 提供便捷读取。
版本化 `FutureBasicProvider` 继续按原有 as-of 门控选择费率版本。
没有历史版本的数据仍是静态费用假设，不据此声称费用历史完整。

数值须为有限非负数；数值与单位只给一项、未知单位、标准字段冲突均报错。
没有费用列或两项均空表示未知，和明确的零费用不同。

## 计算规则

```text
标准手续费 = 成交手数 × 每手费用
           + 成交价格 × 合约乘数 × 成交手数 × 成交金额费率
实际手续费 = 标准手续费 × 手续费系数
```

全程使用 `Decimal`。每次按实际成交数量和价格计算，系数只应用一次，最后按人民币
分四舍五入（`ROUND_HALF_UP`）。部分成交分别计算和舍入；不提前舍入标准费用。
比例费用按成交金额计算，不按保证金计算。

成交价4000、乘数10、成交2手、费率0.0045%、系数1.2时，标准费用3.6元、实际费用4.32元。
每手2.5元、成交2手、系数1.2时，标准费用5元、实际费用6元。

## 系数与费用来源

`CommissionConfig` 与 `CtpFuturesBasicProfile` 支持以下配置：

| 参数 | 默认 | 作用 |
|---|---|---|
| commission_multiplier | 1 | 全局交易手续费系数 |
| commission_product_multipliers | 空映射 | 按品种覆盖，例如RB→1.5 |
| commission_contract_multipliers | 空映射 | 按真实合约覆盖，例如rb2704.SHFE→1.8 |
| delivery_fee_multiplier | 1 | 独立交割费用系数预留 |
| commission_source | auto | 选择资料费用或兼容固定费用 |

覆盖优先级：带交易所的完整合约代码 → 裸合约代码 → 品种 → 全局。
各层是覆盖关系，不连乘。代码忽略大小写；重复或无效代码拒绝。
系数须为有限正数，可以小于1；合约乘数 `contMultNum` 不受此配置影响。

费用来源模式：

- `auto`：优先使用合约费用，缺失时使用已有 `commission_per_contract` 配置，保持旧文件兼容。
- `metadata`：期货必须有明确费用，注册到Backend时检查，缺失即失败。
- `fixed`：显式沿用每手配置，即使资料中有费用也使用配置值。

推荐资料已经补齐的正式期货回测显式选择 `metadata`。无费用不会默认为零；
`auto`、`fixed` 的兼容每手费用仍采用原入口参数（大部分入口默认1元/手）。
期权未从 `fut_basic` 获取费用，继续使用原来的每手费用配置并应用交易系数。

## 回测入口

01–09 回测命令行共用以下参数；保留原有数据、策略和每手费用参数：

```text
--commission-source metadata
--commission-multiplier 1.2
--commission-product-multiplier RB=1.5
--commission-contract-multiplier rb2704.SHFE=1.8
```

品种与合约覆盖参数可重复指定不同代码。`--delivery-fee-multiplier` 是独立预留项，
普通成交不会扣交割手续费。原来的 `--commission-per-contract` 或 `--commission`
在兼容路径中有效，不叠加到资料中的交易所标准上。

01–07及10的Python入口使用 `commission_config`：

```python
from decimal import Decimal
from bomber.framework.fees import CommissionConfig

fees = CommissionConfig(
    commission_multiplier=Decimal("1.2"),
    commission_product_multipliers={"RB": Decimal("1.5")},
    commission_contract_multipliers={"rb2704.SHFE": Decimal("1.8")},
    commission_source="metadata",
)
# 在原 run_case(...) 参数中增加 commission_config=fees。
# 直接构造Profile时可传入 **fees.profile_kwargs()。
```

公共合约工厂会将标准费用和原始单位写入Instrument.info，不把账户系数写回基础资料。
同一个Venue的费用模型按每个Instrument分别查找费用和系数，支持按手/比例混用。
直接手工创建Instrument的调用方须携带这些费用字段，或使用明确的固定费用配置。

各回测结果目录新增 `commissions.csv` 和 `commission_config.json`。
逐笔CSV包含真实合约、订单、成交量价、合约乘数、费用来源、原始单位、
标准费用、最终系数与实际扣费，可与原生成交报表核对。无成交时仍输出表头和配置。

## 开平与交割边界

`CtpCommissionRule` 保留 `open_per_contract`、`close_per_contract`、
`close_today_per_contract`，新增 `open_rate`、`close_rate`、`close_today_rate`
和 `commission_multiplier`。`calculate(effect, quantity, price, contract_multiplier)`
用于比例计费；旧固定费用调用 `calculate(effect, quantity)` 仍有效。

`CtpCommissionRule.from_mapping(row, commission_multiplier=...)` 将源表通用费用
用于三种开平，属于明确的回测近似；源表没有提供的平今优惠不能推断。
需要精确差异时，分别配置三组固定费用和小数比例。

`CtpExecutionAccounting` 传入真实成交价格和乘数核算，保留 `commission_records`。
可传 `commission_config` 统一应用覆盖规则；此时最终系数覆盖规则对象的系数，避免重复相乘。
基础净持仓Profile仅由原生FeeModel扣费；高保真Profile继续在外部CTP核算中扣费，
不同时启用两条费用路径。

交割费用独立保留，尚未接入交割执行事件；交易手续费系数不影响交割费用。
若源交割单位不是元/手或%，须先增加对应单位的明确换算规则。

## 验证

`tests/test_futures_fees.py` 验证固定/比例费用、系数覆盖、开平差异、分笔成交、
舍入、错误数据、原始字段保留、版本可见性和逐笔CSV。
适配层用SDK实体替身运行真实Python费用模型、Profile、合约工厂和CTP账本；
它不代表原生引擎端到端撮合已经验收。
