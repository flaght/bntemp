# 期货手续费与账户系数

状态：**框架费用能力的服务器原生离线验收已通过**。用户提供报告
`futures-fees-acceptance-1793026876102616993`，六组全部exit=0，
共150项unittest及3项CTP场景。Feather读写、原生撮合手续费、账户现金扣减
和逐笔费用报表一致性已验证。详见本文末尾验收记录。

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

01–07及10的Python入口使用 `commission_config`；01–07保留原兼容行为，10的严格资料约束见下文：

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

## HybridTransformer 入口约束（2026-10-27）

`demos/10_hybrid_transformer/run_backtest.py` 删除了 `commission_per_contract` 参数，
默认使用 `CommissionConfig(commission_source="metadata")`。传入的配置也须声明
`commission_source="metadata"`；`auto` 和 `fixed` 均拒绝，避免费用缺失时回退到示例常数。
原有全局、品种、合约手续费系数及逐笔费用报表继续使用共享框架，不重复实现费率解析。
01–09和共享Profile的兼容行为不变。

本次仅修改Hybrid入口、相关装配用例及说明。本地32项入口/解析/费用规则回归通过，
适配层使用SDK实体替身；此调整尚未重新进行服务器原生撮合或完整Hybrid模型回测。
此前150项unittest及3项CTP场景的通过记录对应下文归档的已验版本。

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
该文件中的适配层用SDK实体替身运行Python费用模型、Profile、合约工厂和CTP账本。
服务器另外运行`tests/test_futures_fees_native.py`，使用真实Bomber扩展和Feather，
其7项原生用例已通过。原先本机缺少pyarrow和原生引擎造成的验证缺项已由这次服务器运行补齐。

## Linux服务器集中验收

沿用04_cross_section的服务器流程：同步下面清单内全部文件及清单本身，
在现有uv-nautilus环境执行。本节命令保留供后续代码变更后的复测使用，
本次已通过的组无需为文档更新重复运行。验收使用离线样本，不连接DolphinDB、CTP或SimNow。

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
export PYTHONPATH=.
sha256sum -c doc/evidence/checksums/FUTURES_FEES_20261026_SHA256.txt || exit 1
/workspace/worker/env/uv-nautilus/bin/python -u -m tests.run_futures_fees_acceptance
```

依次运行environment、rules、native、references、ctp、regression六组，每组独立进程。
environment核验pyarrow、实际Bomber原生扩展及本次framework源码加载路径；缺依赖即失败。
rules只运行原费用测试中的10项纯规则用例；native新增7项原生测试，覆盖真实Feather读写、
同Venue混合费用/系数覆盖、开平仓、原生账户累计费用与现金扣减、报表一致性、零费率、
固定费用覆盖、旧资料回退、缺资料拒绝，以及真实SDK身份下的CTP开平规则。
references复测6项基础条款和36项参考来源用例；ctp复测原有高保真规则脚本；
regression复用原91项受控SDK实体离线回归，不将它当作原生撮合证据。

原生撮合使用声明量价的小样本Bar与独立临时Feather资料，不读取或改写现有role/行情文件。
测试样本手续费只用于确定性断言，不代表交易所当前费率。固定费用合约每次开/平6元，
比例费用合约每次开/平6.48元；混合测试累计24.96元，平仓后现金应为999975.04元。

报告保存到`tests/results/futures-fees-acceptance-<id>/`，包含源码摘要、解释器/模块路径、
每组日志、退出码、耗时和summary.json。native-artifacts下保留输入Feather、
逐笔commissions.csv、账户扣费结果native_result.json和费用配置。
首个失败即停止，修复后可只复测对应组，例如：

```bash
/workspace/worker/env/uv-nautilus/bin/python -u -m tests.run_futures_fees_acceptance --only native
```

## 已通过的服务器验收记录

用户提供的完整服务器控制台输出显示：

| 分组 | 检查数量 | 测试耗时 | 结果 |
|---|---|---|---|
| environment | 原生依赖与源码加载路径检查 | 未单独提供 | exit=0 |
| rules | 10项unittest | 0.019秒 | 全部通过，exit=0 |
| native | 7项unittest | 0.090秒 | 全部通过，exit=0 |
| references | 42项unittest | 2.587秒 | 全部通过，exit=0 |
| ctp | E10a、E10b、E10c三个场景 | 未单独提供 | 全部通过，exit=0 |
| regression | 91项unittest | 0.365秒 | 全部通过，exit=0 |

合计150项unittest，另外3项CTP场景；上表耗时取自测试输出，不是每组子进程总耗时。
服务器环境为Linux、Python 3.12.14、pandas 3.0.3、pyarrow 24.0.0。
Bomber原生引擎和模型扩展从uv-nautilus的site-packages加载，
本次费用及资料模块从项目pro源码加载。源文件校验清单全部匹配。

原生测试已通过同Venue固定/比例费用混用、系数覆盖、开平仓、零费用、旧资料回退、
显式固定费用覆盖、缺资料注册失败以及账户/报表金额一致性检查。
混合费用用例中的累计24.96元、最终现金999975.04元和最终零仓位断言均通过。
日志中的`Pandas4Warning: Timestamp.utcnow is deprecated`为弃用提示，未导致测试失败。

服务器报告目录：

```text
/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/tests/results/futures-fees-acceptance-1793026876102616993/
```

已保存[结构化证据](../evidence/live/FUTURES_FEES_REMOTE_20261026.json)及
[用户远端原始输出](../evidence/live/FUTURES_FEES_REMOTE_20261026.txt)。
结论依据用户提供的服务器输出；summary.json和native-artifacts未单独复制到本机。
本次通过范围是声明样本的服务器原生离线验收，交易所实际费率及期货公司收费协议仍由数据和配置决定。

服务器通过时的清单原字节归档为
[FUTURES_FEES_20261026_TESTED_SHA256.txt](../evidence/checksums/FUTURES_FEES_20261026_TESTED_SHA256.txt)，
SHA256为`8e43fea28b82eccc7dcfb97e67b4e8116cbe7c1886500cf07b44563adf14b497`。
该归档仅用于追溯已验版本。当前`FUTURES_FEES_20261026_SHA256.txt`在2026-10-26更新本文摘要，
并在2026-10-27更新Hybrid入口及装配测试摘要、纳入Hybrid README；此前服务器验收的归档不变。
后续同步及复测使用当前清单，不把归档结果视为对后续代码的重新验收。
