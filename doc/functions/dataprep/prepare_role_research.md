# prepare_role_research

用途：把真实合约行情、合约角色表和复权资料组装成可按时间查询的研究参考存储。

- 导入路径：`bomber.framework.dataprep.scenarios.role_futures.prepare_role_research`
- [函数源码](../../../bomber/framework/dataprep/scenarios/role_futures.py)
- [角色与行情加载实现](../../../bomber/framework/dataprep/references.py)
- [外部复权因子实现](../../../bomber/framework/dataprep/factors.py)
- 核对日期：2026-10-23，Asia/Shanghai。

## 1. 这个函数负责什么

它主要回答以下问题：

1. 需要读取哪些品种的真实合约行情？
2. 每个交易日使用哪份已经可用的合约角色资料？
3. 当时的主力、次主力等角色对应哪个真实合约？
4. 使用外部累计复权因子，还是由现有角色价格存储计算换月衔接因子？
5. 各交易日的首个、最后一个已完成分钟事件是什么时刻？

函数会读取行情来构建这些资料，但不运行模型、不生成交易信号、不发订单，也不会自动把真实行情改成可交易的复权价格。策略的行情订阅和执行路由需要另外配置。

## 2. 函数签名

```python
from bomber.framework.dataprep.scenarios.role_futures import prepare_role_research

def prepare_role_research(
    *,
    bars_dir,
    contract_struct_path,
    products,
    signal_role="main",
    execution_product=None,
    execution_role="main",
    end_day=None,
    minute_roles=(),
    timezone="Asia/Shanghai",
    bar_timestamp="end",
    factors_path=None,
    factor_date_basis=None,
    factor_availability="explicit",
):
    ...
```

`*` 表示所有参数都必须按名称传入。

## 3. 参数速查

| 参数 | 常用类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `bars_dir` | `str` / `Path` | 必填 | 真实合约分钟行情目录 |
| `contract_struct_path` | `str` / `Path` | 必填 | 合约角色表文件路径 |
| `products` | 品种字符串序列 | 必填 | 需要准备信号研究数据的品种，建议使用大写品种代码 |
| `signal_role` | `str` | `"main"` | 日级角色研究模式下，信号行情使用的合约角色 |
| `execution_product` | `str` / `None` | `None` | 执行品种；未传时采用 `products[0]` |
| `execution_role` | `str` | `"main"` | 执行品种需要准备的合约角色 |
| `end_day` | `datetime.date` / `None` | `None` | 行情扫描的截止交易日，包含当天；未传时不限制结束交易日 |
| `minute_roles` | 角色字符串序列 | `()` | 非空时切换为单品种、多角色的分钟研究模式 |
| `timezone` | `str` | `"Asia/Shanghai"` | 解释无时区时间标签及来源日结束时间所用的时区 |
| `bar_timestamp` | `str` | `"end"` | 源分钟 bar 时间标签含义：`"start"` 或 `"end"` |
| `factors_path` | `str` / `Path` / `None` | `None` | 外部累计复权因子文件；不是模型特征文件 |
| `factor_date_basis` | `str` / `None` | `None` | 外部因子的日期含义：`"trading"` 或 `"source"` |
| `factor_availability` | `str` | `"explicit"` | 外部因子的可用时间声明方式 |

## 4. 行情与角色参数详解

### bars_dir：真实合约分钟行情目录

扫描目录及子目录中的 `.feather` 文件。标准文件名包含真实合约和八位交易日，例如：

```text
bars/
├── RB2605_20260105.feather
├── RB2610_20260105.feather
└── RB2605_20260106.feather
```

本步骤主要使用时间和收盘价来准备角色研究资料与换月锚点。后续模型策略需要的完整 OHLCV、成交额、持仓量等，仍需由正式行情读取步骤提供。

文件名日期代表交易日。夜盘自然日期可以与交易日不同，不能仅按时间戳的自然日期推断交易日。

### contract_struct_path：合约角色表

角色表使用 Feather 格式。当前只需要主力时，至少包含：

| trade_date | code | main |
| --- | --- | --- |
| 2026-01-02 | RB | rb2605 |

角色名称与表字段对应如下：

| 角色名称 | 角色表字段 | 含义 |
| --- | --- | --- |
| `main` | `main` | 主力 |
| `secondary` | `second` | 次主力 |
| `near` | `recent` | 近月 |
| `far` | `far` | 远月 |

**这里的 `trade_date` 被当作资料来源日期。** 对交易日 D，代码选择 `source_day < D` 的最新角色记录。例如，2026-01-05 可以采用来源日为 2026-01-02 的资料，不会直接采用标为 2026-01-05 的同日资料。

如果角色表提供 `available_ns`，还会检查这份资料当时是否已经可用。多品种日级场景要求各品种使用的角色资料来源日一致。

### products：需要哪些品种的数据

```python
products=("RB",)              # 单品种；逗号表示单元素元组
products=("RB", "HC", "I")  # 多品种信号研究
```

`products` 必须非空；日级场景中的品种不能重复。`execution_product` 必须属于这组品种，建议所有品种参数统一使用大写代码。

### signal_role 与 execution_role：观察角色和执行角色

`signal_role` 指定计算信号所观察的行情角色，`execution_role` 指定执行品种需要准备的角色。二者可以不同，例如：

```python
products=("RB", "HC", "I")
signal_role="main"
execution_product="RB"
execution_role="secondary"
```

含义是：准备三个品种的主力信号研究资料，同时准备 RB 的次主力执行角色。相同角色不会因此被重复请求。

这组参数声明数据准备需求，不会自动改变策略实际订阅或下单的合约。当前 HybridTransformer 入口还在后续代码中选择 `"main"`；切换角色时必须同步调整策略筛选和执行路由。

### end_day：结束交易日

行情文件按 `trading_day <= end_day` 筛选，包含当天，不表示当日某个盘中结束时刻。

这个函数没有 `start_day` 参数，可能保留更早的数据来准备历史角色和换月锚点。正式回测区间由调用方后续筛选。

### bar_timestamp 与 timezone：分钟行情何时完成

对于一分钟行情，源时间戳为 `09:00` 时：

| bar_timestamp | 源标签含义 | 已完成 bar 的事件时间 |
| --- | --- | --- |
| `"start"` | 统计 `09:00～09:01` 的行情 | `09:01` |
| `"end"` | 统计 `08:59～09:00` 的行情 | `09:00` |

原始标签保存在 `source_timestamp`；标准化后的完成时刻保存在 `datetime`、`event_ns` 等字段。`timezone` 默认是上海时区，用于解释无时区标签。选择错误的标签口径会造成一分钟的可用时间偏移。

详见 [分钟行情读取实现](../../../bomber/framework/dataprep/bars.py)。

## 5. 外部复权因子参数详解

### factors_path：累计复权因子文件

这个参数中的因子用于合约换月和连续研究价格衔接，不能与模型特征或合约乘数混淆。

标准长表字段为：

| 字段 | 含义 |
| --- | --- |
| `trade_date` | 日期；由 `factor_date_basis` 决定如何解释 |
| `code` | 品种，例如 `RB` |
| `symbol` | 真实合约，例如 `rb2605` |
| `role` | `main`、`secondary`、`near` 或 `far` |
| `cumulative_factor` | 有限、正的累计复权因子 |
| `available_ns` | 可用时间；`explicit` 模式下必填 |

也兼容旧字段 `pcr_cumfactor`。没有 `role` 的旧格式只表示主力因子；使用通用字段 `cumulative_factor` 时必须显式提供 `role`。

- 提供文件：采用外部累计因子，检查请求范围内的日期、角色、品种及可用时间。
- 不提供文件：使用现有 `RolePriceStore` 根据真实合约收盘价与换月锚点准备衔接因子。

外部因子行中的 `symbol` 可以覆盖角色表中同角色的合约映射；发生覆盖时会记录 `FACTOR_CONTRACT_OVERRIDE` 诊断。它可能影响合约选择，不能只把文件当作一列乘数。

### factor_date_basis：因子属于哪一天

| 值 | 日期含义 | 对交易日 D 查询什么 |
| --- | --- | --- |
| `"trading"` | 使用交易日 | 查询日期为 D 的因子 |
| `"source"` | 资料来源日 | 查询所选角色资料来源日对应的因子 |

例如交易日为 2026-01-05、角色资料来源日为 2026-01-02：`trading` 查询 2026-01-05 的因子，`source` 查询 2026-01-02 的因子。

```python
factor_date_basis="trading" if factors_path else None
```

这段写法要求外部因子已经按使用交易日对齐。如果文件仍按来源日期保存，应明确使用 `"source"`。没有外部文件时传 `None` 即可。

这个参数只解释因子表日期，不会改变角色表把 `trade_date` 当作资料来源日的约定。

### factor_availability：因子何时允许使用

| 值 | 规则 | 日期口径约束 |
| --- | --- | --- |
| `"explicit"` | 文件必须提供非负整数纳秒时间戳 `available_ns` | 可与 `source` 或 `trading` 配合 |
| `"aligned"` | 上游声明已按使用交易日对齐，并在首个有效 bar 时可用；如果文件带有 `available_ns`，仍尊重该发布时间 | 必须为 `trading` |
| `"source-day-end"` | 假定来源日结束时可用，即所选时区的次日 `00:00` | 必须为 `source` |

函数源码默认是 `explicit`；当前 HybridTransformer 回测入口默认传 `aligned`。这两个默认值不能混为一谈。

当前日级场景要求角色与因子在当天首根已完成 bar 时已经可用。显式发布时间晚于该时点会报错，不会把后面才发布的资料提前用于第一根 bar。

`aligned` 是对上游数据约定的显式声明，不会替调用方证明数据没有使用未来信息。

## 6. minute_roles：另一条加载分支

默认 `minute_roles=()`，使用前述日级角色快照场景。

传入非空角色序列，例如：

```python
minute_roles=("main", "secondary", "far")
```

会切换到 `load_role_research()`，加载单品种多个角色的分钟研究资料。此时：

- `products` 必须恰好有一个品种。
- `end_day` 必须提供。
- 不支持 `factors_path`。
- 请求哪些角色由 `minute_roles` 决定，不再采用日级分支的 `signal_role`、`execution_product`、`execution_role` 组合。

当前 HybridTransformer 调用没有传这个参数，走的是日级场景。

## 7. 返回结果

| 场景 | 返回类型 | 主要字段 |
| --- | --- | --- |
| 默认日级场景 | `LoadedSectorResearch` | `store`、`day_end_ns`、`bars_dir` |
| `minute_roles` 非空 | `LoadedRoleResearch` | `store`、`day_end_ns`、`bar_count`、`file_count` |

日级场景中，`store` 是角色与累计因子的时间快照存储，可以通过 `store.snapshot(as_of_ns)` 查询，再通过快照的 `instrument(product, role)` 获取真实合约。`day_end_ns` 是 `(交易日, 当日最后一个已完成分钟事件时间)` 的序列。

两种场景的 `store` 类型不同，不应假定所有查询方法都相同。实际类型及字段见 [返回结构定义](../../../bomber/framework/dataprep/references.py)。

## 8. 当前策略的调用示例

```python
from datetime import date
from pathlib import Path
from bomber.framework.dataprep.scenarios.role_futures import prepare_role_research

product = "RB"
factors_path = Path("/ABS/PATH/TO/factors.feather")

loaded = prepare_role_research(
    bars_dir=Path("/ABS/PATH/TO/MINUTE_BARS"),
    contract_struct_path=Path("/ABS/PATH/TO/CONTRACT_STRUCT.feather"),
    products=(product,),
    signal_role="main",
    end_day=date(2026, 1, 5),
    execution_product=product,
    execution_role="main",
    bar_timestamp="start",  # 仅当源数据按分钟起点标记时使用
    factors_path=factors_path,
    factor_date_basis="trading" if factors_path else None,
    factor_availability="aligned",  # 前提：上游已按使用交易日对齐
)

day_ends = dict(loaded.day_end_ns)
snapshot = loaded.store.snapshot(day_ends[date(2026, 1, 5)])
main_contract = snapshot.instrument(product, "main")
```

示例时间标签和因子政策均有数据前提，应按实际来源设置。未提供外部因子时，将 `factors_path` 设为 `None`。

[HybridTransformer 实际调用入口](../../../demos/10_hybrid_transformer/run_backtest.py) 中，策略通过角色快照查询主力，再从真实 `CustomBar` 提取模型输入。当前策略没有把累计复权因子自动乘到模型 OHLC 上；角色研究资料和模型输入价格口径需要分别核对。

## 9. 常见错误与排查

| 现象 | 重点检查 |
| --- | --- |
| 没有可用的上一交易日角色记录 | 角色表是否保留了早于使用交易日的资料 |
| 信号品种或执行角色缺失 | `products` 是否包含执行品种，角色列是否齐全 |
| `MISSING_FACTOR` | 因子日期口径、品种和信号角色是否对应 |
| `INVALID_FACTOR_POLICY` | `aligned` 是否搭配 `trading`，`source-day-end` 是否搭配 `source` |
| `REFERENCE_NOT_AVAILABLE` | 角色/因子是否晚于当天首根已完成 bar 才可用，或是否缺少完整角色快照 |
| `FACTOR_CONTRACT_OVERRIDE` 诊断 | 因子表 `symbol` 与角色表同角色合约是否不同 |
| 行情可用时间错一分 | `bar_timestamp` 是否符合供应方标签定义 |
| 改了 execution_role 仍交易主力 | 是否同步修改了后续行情筛选、真实合约选择及执行路由 |

文件身份、字段及时间校验通常抛出带错误码的 `InputError`；空品种列表或不支持的分钟场景组合会直接抛出 `ValueError`，无行情文件等情况也可能抛出 `FileNotFoundError`。

相关回归：[角色场景测试](../../../tests/test_dataprep_role_scene.py)、[外部因子测试](../../../tests/test_dataprep_external_factors.py)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
