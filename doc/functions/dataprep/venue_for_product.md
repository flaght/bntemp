# venue_for_product（metadata.venue）

用途：从期货基础资料表中查找某个品种对应的唯一交易所，并将来源交易所编码转换为框架统一使用的字符串。

- 当前入口导入：`from bomber.framework.dataprep.metadata import venue as venue_for_product`
- 实际定义：`bomber.framework.dataprep.metadata.venue`；`venue_for_product` 是调用文件里的别名，不是另外一个函数。
- [函数及交易所映射源码](../../../bomber/framework/dataprep/metadata.py)
- [实际调用入口](../../../demos/10_hybrid_transformer/run_backtest.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个函数负责什么

给定已经读入的基础资料 `basic` 和品种代码 `product`，函数执行以下步骤：

1. 检查基础表的必需字段及列名唯一性。
2. 从 `basic.code` 中筛选指定品种的所有记录。
3. 将这些记录的 `exchangeCD` 转换为统一交易所代码。
4. 检查归一化后是否恰好只有一个交易所。
5. 返回该交易所字符串，例如 `"SHFE"`。

这里查的是“这一品种在传入基础资料中归属哪个交易所”。一个品种可以有很多合约月份，只要它们归一化后的交易所一致，就可以返回结果。

函数不内置“品种代码到交易所”的列表，不会根据品种字母或文件名猜交易所。查不到资料或资料冲突时明确失败，不设置默认交易所。

## 2. 函数签名与导入别名

源码签名没有类型注解，两个参数都必填：

```python
def venue(basic, product):
    ...
```

调用时可以使用原名或别名：

```python
from bomber.framework.dataprep.metadata import venue
from bomber.framework.dataprep.metadata import venue as venue_for_product

# 两个名字指向同一个函数。
assert venue is venue_for_product
```

当前 HybridTransformer 使用别名，是为了让 `venue_for_product(basic, product)` 表达“按品种查询交易所”；后续局部变量 `venue` 则保存转换后的 `Venue` 对象。主力 EMA 入口使用 `_venue_for_product`，指向的也是同一函数。

## 3. 参数速查

| 参数 | 常用类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `basic` | `pandas.DataFrame` | 必填 | 已读入内存的期货基础资料表，通常来自 `read_feather(fut_basic)` |
| `product` | `str` | 必填 | 品种代码，例如 `"RB"`、`"ZN"`；不是具体合约或合约身份字符串 |

函数不会将文件路径读成 DataFrame，也不会主动转换 `product` 的类型。传路径、字典或 `None` 等不符合约定的对象，可能直接产生 `AttributeError` 等普通 Python 异常，不保证包装成输入诊断。

### product 的匹配规则

源码使用：

```python
rows = basic.loc[
    basic.code.astype(str).str.strip().str.upper().eq(product.upper())
]
```

基础表的 `code` 值会转字符串、去除首尾空格并转大写；传入的 `product` **只转大写，不去空格**。

| 输入情形 | 结果 |
| --- | --- |
| 表中 `code=" rb "`，传 `product="RB"` | 可以匹配 |
| 表中 `code="RB"`，传 `product="rb"` | 可以匹配 |
| 表中 `code="RB"`，传 `product=" RB "` | 不能匹配 |
| 表中 `code="RB"`，传 `product="rb2610"` | 不会提取品种字母，不能匹配该品种行 |
| 表中 `code="RB"`，传 `product="rb2610.SHFE"` | 不会解析合约身份，不能匹配该品种行 |

调用方通常先执行 `product = product.strip().upper()`。当前 HybridTransformer 和主力 EMA 入口已经这样处理，但这不是本函数自身提供的去空格行为。

## 4. 输入表格式与校验范围

虽然查询只实际使用 `code` 和 `exchangeCD` 的值，函数会先调用通用 `validate_basic(basic)`，要求列名唯一，且至少包含以下七列：

| 字段 | 基础资料含义 | 本函数是否使用字段值 |
| --- | --- | --- |
| `symbol` | 真实合约，例如 `rb2610` | 否，只检查列存在 |
| `code` | 品种代码，例如 `RB` | 是，筛选目标品种 |
| `exchangeCD` | 来源交易所编码，例如 `XSGE` | 是，归一化并检查唯一性 |
| `contMultNum` | 合约乘数 | 否，只检查列存在 |
| `minChgPriceNum` | 最小变动价位 | 否，只检查列存在 |
| `listDate` | 上市日期 | 否，只检查列存在 |
| `lastTradeDate` | 最后交易日期 | 否，只检查列存在 |

允许有其他列，列名区分大小写。只有 `code`、`exchangeCD` 两列的简化映射表，即使能表达交易所关系，也会因缺少其他必需列而失败。

本函数不验证合约月份、合约与品种是否对应、乘数和价位是否为正、日期是否有效，也不要求合约行唯一。重复记录只要映射到同一交易所，就不会在此报错。后续构造真实合约时仍须进行条款校验。

函数没有日期或 `as_of` 参数，不按回测区间、上市日期、到期日期或资料发布时间过滤记录。传入表中该品种的全部记录都会参与交易所一致性检查，包括旧合约和不在本次实际选约集合里的合约。

## 5. 交易所编码如何归一化

每条匹配记录通过 `venue_of(row)` 处理：先对 `exchangeCD` 执行 `str(...).strip().upper()`，再查源码中的 `VENUES` 字典。

| 接受的来源编码 | 返回字符串 |
| --- | --- |
| `XSGE`、`SHFE` | `SHFE` |
| `XDCE`、`DCE` | `DCE` |
| `XZCE`、`CZCE` | `CZCE` |
| `XSIE`、`INE` | `INE` |
| `CCFX`、`XCFX`、`CFFEX` | `CFFEX` |
| `XGFE`、`GFEX` | `GFEX` |

这是当前源码接受的编码集合，不代表会自动接受所有供应商缩写。`" xsge "` 可以转换为 `"SHFE"`；`"CZC"`、`"SHF"`、空字符串、空值或其他未登记编码会报错，不会原样返回。

唯一性检查发生在归一化之后。因此，同一品种同时出现 `XSGE` 和 `SHFE` 可以通过；同时出现 `XSGE` 和 `XDCE` 则分别变成 `SHFE`、`DCE`，产生冲突。

不匹配目标品种的行不会执行交易所映射，其未知交易所编码不影响此次查询；但整张表的列名唯一性和必需字段仍会先被检查。目标品种只要有一条记录包含未知编码，就会失败，不会跳过坏行或按多数票决定交易所。

## 6. 返回结果及入口中的作用

成功返回 `str`，当前可能值为 `SHFE`、`DCE`、`CZCE`、`INE`、`CFFEX`、`GFEX`。不会返回 `None`、列表、交易所对象或具体合约。

实际入口中的语句：

```python
venue = Venue(venue_for_product(basic, product))
```

分两步完成：

1. `venue_for_product(basic, product)` 从基础资料得到统一字符串，例如 `"SHFE"`。
2. `Venue("SHFE")` 将字符串包装为框架使用的交易所标识对象。

在 HybridTransformer 中，该对象传给 `CtpFuturesBasicProfile(..., venue=venue)`，随后配置模拟后端并创建、注册真实合约。`HybridTransformerConfig` 另外接收 `venue=str(venue)`。

因此，交易所查询是合约注册前的资料一致性步骤。它本身不会添加账户、配置交易时段、选择主力合约或注册行情；具体交易日历仍由传入的 `SessionCalendar` 负责。

函数不修改输入 DataFrame，也不创建独立查询缓存。使用 `read_feather` 读取时可复用 `input_session` 的文件读取缓存，但每次调用本函数仍会重新筛选和遍历匹配行。

## 7. 调用示例

### 最小完整的内存示例

以下数据仅用于说明函数行为，不作为正式合约条款资料：

```python
import pandas as pd
from bomber.framework.dataprep.metadata import venue as venue_for_product

basic = pd.DataFrame([
    dict(symbol="rb2610", code="RB", exchangeCD="XSGE",
         contMultNum=10, minChgPriceNum=1,
         listDate="2025-10-01", lastTradeDate="2026-10-15"),
    dict(symbol="rb2701", code=" rb ", exchangeCD=" shfe ",
         contMultNum=10, minChgPriceNum=1,
         listDate="2026-01-01", lastTradeDate="2027-01-15"),
])

assert venue_for_product(basic, "RB") == "SHFE"
assert venue_for_product(basic, "rb") == "SHFE"

# 所有匹配合约的来源编码归一化后相同，即可返回唯一交易所。
```

### 读取基础资料并生成 Venue 对象

```python
from pathlib import Path
from bomber.model import Venue
from bomber.framework.dataprep.metadata import venue as venue_for_product
from bomber.framework.dataprep.session import input_session, read_feather


@input_session
def load_venue(*, fut_basic: Path, product: str):
    product = product.strip().upper()
    basic = read_feather(fut_basic)
    return Venue(venue_for_product(basic, product))


if __name__ == "__main__":
    venue = load_venue(
        fut_basic=Path("/ABS/PATH/TO/fut_basic.feather"),
        product="RB",
    )
```

### 检查冲突错误

沿用前面的内存示例：

```python
from bomber.framework.dataprep.contracts import InputError

conflicting = basic.copy(deep=True)
conflicting.loc[1, "exchangeCD"] = "XDCE"

try:
    venue_for_product(conflicting, "RB")
except InputError as exc:
    assert exc.issue.code == "CONFLICTING_METADATA"
    print(exc)
```

这表示传入资料为同一品种声明了多个交易所。应修复或按已经明确的资料范围准备输入表，不能随便取第一行来绕过冲突。

## 8. 错误与排查

| 错误码或现象 | 触发原因 | 排查方向 |
| --- | --- | --- |
| `MISSING_FIELD` | 必需列缺失，或任意列名重复 | 核对七个字段、大小写和重复列；仅重复列时错误信息中的缺失字段列表可能为空 |
| `INVALID_METADATA: fut_basic has no product ...` | 按规则筛选后没有记录 | 品种是否传成真实合约；参数是否含空格；基础表是否包含该品种 |
| `INVALID_METADATA: Unsupported exchange ...` | 目标品种某行的交易所编码不在映射表中 | 查该品种全部行的 `exchangeCD`，检查空值及未支持的供应商编码 |
| `CONFLICTING_METADATA: ... maps to several venues ...` | 归一化后存在多个交易所 | 查同一品种跨合约记录是否混入错误交易所或错误品种代码 |
| `AttributeError` 等普通异常 | `basic` 不是约定的 DataFrame，或 `product` 不是字符串等 | 先保证输入对象类型；函数不提供完整类型校验 |
| 返回正确，但后续合约构建失败 | 这里只校验交易所关系，没有验证所有条款值 | 检查具体合约行的唯一性、品种归属、日期、乘数、价位等 |

以上三个输入错误码通过 `fail(...)` 抛出 `InputError`，该类型继承 `ValueError`。异常的结构化诊断位于 `exc.issue`；有活动输入会话时还会记录到 `session.issues`。本函数不接收来源路径，也不会自动为问题附上具体文件路径或坏行行号，需要调用方根据输入表定位。

若外围使用 `@input_session` 且实际传入可识别的 `report_dir`，失败诊断可由装饰器写出；本函数自身不写文件、不输出报告。

相关接口：`venue_of(row)` 归一化单行交易所；`selected_products(basic, products)` 对多个品种分别调用本函数并返回映射；`contract_rows` 和 `futures.instrument` 负责后续具体合约查询与构造。

相关说明：[input_session](input_session.md)、[SessionCalendar](../calendars/SessionCalendar.md)、[数据准备 API 用法](../../../bomber/framework/dataprep/API_USAGE.md)。实际调用：[HybridTransformer](../../../demos/10_hybrid_transformer/run_backtest.py)、[主力 EMA](../../../demos/01_main_ema/run_backtest.py)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
