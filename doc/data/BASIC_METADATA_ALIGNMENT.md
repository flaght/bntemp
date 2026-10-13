# 基础条款结构与接口对齐

## 模块职责

| 层 | 期货 | 期权 | 职责 |
| --- | --- | --- | --- |
| datahub | future_basic.py | option_basic.py | 不可变条款、字段适配、按时间查询 |
| datahub 公共层 | basic.py | basic.py | 共用文本、数值、日期校验及 BasicProvider 时间门控 |
| dataprep 文件层 | basic.load_future_basic_provider | basic.load_option_basic_provider | 读取文件、复用缓存、保留来源字段、组装查询服务 |
| dataprep 静态场景 | metadata.future_spec / load_futures_basic / load_cffex_futures | metadata.load_options_basic | 场景身份约束、执行必需字段、InstrumentSpec 或旧原始行适配 |
| trader | make_profile_future / make_future | make_option | 使用已准备条款创建原生交易合约，保留当前工厂调用接口 |

参考模型不创建交易合约；交易所别名规范化、品种筛选、CFFEX 限定与交易场景必需字段由 dataprep 负责。两个条款模型均保留原始身份，可供历史版本查询。

## 检查和修正范围

| 检查项 | 处理 |
| --- | --- |
| 包入口引用不存在的模块 | 补齐 option_basic，并增加对称的 future_basic；两者均公开导出模型、Provider、数据集常量 |
| 只有期权有基础条款查询 | 两类 Provider 共用 read / basic_at、版本排序、重复版本拒绝、发布时间及来源年龄校验 |
| 参考查询直接读取 Feather | 文件读取统一放到 dataprep.basic；两类 from_feather 只作兼容委托 |
| 文件读取没有接入 InputSession | 新文件入口统一使用 dataprep.session.read_feather |
| 期权价位字段优先级不一致 | 统一为 tickNum → minChgPriceNum → price_increment，空值可回退，非空无效值拒绝 |
| 静态条款重复独立校验 | 静态场景使用 FutureBasic / OptionBasic 和共享有限正数校验，再适配既有返回结构 |
| 文档只介绍期权条款 | DataHub README 与 dataprep 接口文档同时描述两类能力 |

## 保留的业务差异

期货品种、期权标的与行权价不是同一字段，不强行合并成一个模型。期货主字段使用 minChgPriceNum，期权主字段使用 tickNum，这是源表差异。期货信号资料允许缺乘数、价位，交易资料必须齐备；期权乘数始终必需，参考价位可缺，交易场景价位必需。

商品期货 profile 工厂与 CFFEX 股指工厂使用不同生命周期及模拟配置，本次不改变。统一基础条款不代表统一保证金模型或到期结算模型。现有策略不自动切换到版本化 Provider，带发布版本的静态输入仍拒绝。

角色快照、研究价格、外部目标计划描述不同数据，不应仅为了目录名称对称而复制模块；它们继续沿用各自时间契约。

## 同步范围与运行环境验证

同步 datahub/basic.py、future_basic.py、option_basic.py、__init__.py，dataprep/basic.py、metadata.py，以及新增测试和相关文档。无需改动策略参数或 trader 工厂调用。

在运行环境执行：

```bash
python -m unittest tests.test_basic_alignment tests.test_datahub_option_basic
python tests/test_dataprep_common.py
python tests/test_demo_option_vega.py
python tests/test_demo_option_delta.py
```

新增测试覆盖两类版本门控、来源年龄、重复记录、键身份、价位优先级、文件缓存入口与来源字段保留。以上测试在本次非运行环境中未执行。
