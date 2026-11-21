# DataHub 参考资料与特征查询

2026-11-02按当前源码核对。框架整体、积木化装配及各层职责见[Framework总览](../README.md)；以下历史验证记录按各自版本阅读，不用早期“待实现”描述替代当前代码。

DataHub定义不可变参考资料与特征记录，按业务时间、可见时间及版本提供查询；包括期货／期权基础条款、合约角色与研究价、外部目标计划及通用特征。文件／数据库读取和字段标准化由dataprep的Provider完成；真实Tick／Bar进入market，合约执行、订单和仓位属于trader。

当前已有`DataHub(features=FeatureHub(registry)).feature_snapshot(...)`、强类型Schema、可见修订、EXACT／WINDOW／ASOF及等待帧；标准文件、DolphinDB／原表映射和MongoDB特征适配由[公共Provider](../dataprep/features/README.md)接入。普通特征直接查询DataHub，只有需要真实OHLCV输入时才投影成Bar。特征Provider、参考Provider及历史行情Provider分别按自己的实现判断，不能把MongoDB特征适配等同于MongoDB参考／历史适配均已完成。

**来源按用途选择。** 回测／研究执行使用标准本地资料和特征导出包；导出准备任务可以读库。Live参考与特征通过Provider读取已声明来源，Live历史预热／盘中回补可经[HistoryService](../dataprep/history/README.md)读取DolphinDB，并按明确的阶段、覆盖及文件优先政策处理；不再沿用早期“Live历史行情只能本地”的限制。数据源选择不进入策略业务算法，不静默切换来源，也不伪造未知历史发布时间。具体政策见[来源工厂](../dataprep/sources/README.md)与[历史启动规则](../../../doc/HANDOFF/history/HANDOFF_HISTORY_STARTUP_POLICY_2026-10-09.md)。

08回测与默认DFS Live已共用指数特征声明和可见时间契约，接入在应用装配中完成；通用接口已有不表示所有业务与来源组合均已实际验收。

## 公共帧恢复分项计时

已准备特征帧的读取和恢复在公共组件中测量：`dataprep/replay_inputs.py`负责文件读取，
`features/prepared_frames.py`负责帧及引用，`features/restoration.py`负责与数据集无关的价格和
市场观察值批量恢复。没有把策略选约或信号逻辑移入公共组件；自定义标量特征也走同一恢复入口。
首轮增加计时和独立诊断，随后增加了下述可选Cython后端；策略与特征字段声明保持原样。

| 统计项 | 测量范围 |
| --- | --- |
| `prepared_frames_table_read` | 帧Feather读取和大小/Schema/行数检查 |
| `prepared_observations_table_read` | 合约观察值Feather读取及对应检查 |
| `prepared_frames_to_python` | 帧Arrow Table转Python字典列表 |
| `prepared_observations_to_python` | 观察值Arrow Table转Python列列表 |
| `prepared_observations_columnar_prepare` | Cython路径的字符串字典编码、共享值和整数列数组准备；不包含Decimal时间 |
| `prepared_frame_reference_checks` | 帧顺序、范围、期限和特征记录可见性检查，建立期限映射 |
| `prepared_observation_decimal` | Python每帧转换价格；Cython按唯一价格字符串转换一次，均保留原精度表示 |
| `prepared_observation_objects` | FrameObservation构造、合约字典组装及重复键检查 |
| `prepared_frame_objects` | FeatureQuery/Snapshot、StrategyFrame和FrameOutcome装配，包括不可变市场字典副本 |

`prepared_frames_load`是父级总耗时；`prepared_frames_decode`包含列转换/准备及之后各项。两层计时不能与
子项相加。每帧作为一个计时批次，不为每个价格采样时钟，细项先在本地累加再一次写入统计。
`prepared_frame_rows`、`prepared_observation_rows`、`prepared_observations_restored`及READY/SKIPPED
计数区分“分钟数”和“实际恢复对象数”。独立报告的`restore_other_seconds`包含循环和计时等剩余成本。
分离Decimal和对象装配需要暂存每帧的价格列表，诊断耗时可能与原先合并循环有小幅差异。

独立入口`scripts/profile_prepared_frames.py --replay-inputs ROOT --feature-config DATASET_JSON`仅使用公共
特征声明、文件Provider和帧服务，不加载策略或撮合引擎；支持`--repeat 1..10`、`--verify metadata/full`
及产物目录外的`--output JSON`。特征输入读取单独记为`setup_performance`，不计入各次帧恢复。
重复恢复会复用操作系统缓存，不视为冷盘实验。诊断仍保留当前v1单项EXACT帧格式的限制。

诊断允许读取结构兼容的旧v1产物，核验文件大小/Schema/行数（full还核验SHA256）以及特征文件内容、
数据集、Schema和记录数，报告`current_implementation_matches_artifact`；不核验当前原生SDK，
也不将诊断成功当作正式回测兼容或金融等价验收。正式回测的源码/SDK指纹检查仍严格执行。

后续Cython实现优先放在公共批量恢复边界，保留同契约的Python路径并对照Decimal、时间、修订及
READY/SKIPPED结果。先根据实测决定：磁盘读取本身不能靠编译Python循环解决；Arrow已在原生库中
执行的部分要区分解码和Python对象物化；若瓶颈是数值/时钟批量处理或观察值装配，再评估Cython。
不能仅将策略代码编译或只对一个数据集做特殊分支就登记公共服务加速。

XY随后提供同量单次诊断：45839帧、1488374个观察值（平均32.47个/帧），恢复总耗时9.885198s。
观察值Arrow转Python为4.540992s（45.94%），观察值对象/字典装配3.315605s（33.54%），
帧对象装配1.205315s（12.19%），Decimal转换0.369760s（3.74%），两份文件读取合计0.027955s。
特征文件准备3.269699s单独统计，不包含在上述恢复时间内。这是诊断结果，不登记正式回测提速。

据此公共加速边界优先研究：直接使用Arrow/NumPy列缓冲区，减少整表`to_pydict()`物化；
合约和来源字符串采用共享字典/编码，减少重复转换；由Cython批量循环融合读取与观察值装配，
先保持公开对象/映射契约及Python实现。若Cython仍调用原`to_pydict()`，4.54s这部分不会因
调用方被编译而消失。后端实现与验证范围如下。

### 可选Cython批量恢复

仅新增`features/_restoration_native.pyx`一个扩展和独立`features/setup.py`构建入口。
公共`restoration.py`在运行时对合约、来源和原始价格字符串做Arrow字典编码，只解码唯一字符串，
时间列保留为只读int64数组；Cython直接读取字典代码及整数数组，逐帧批量构造原有
`FrameObservation`和有序合约字典。没有更换公开对象类型、改写持久文件或新增策略分支。
价格字典按原字符串区分，因此`1.0`和`1.00`、正负零的Decimal表示保持原样。

`decode_frames(..., backend="auto")`默认有扩展时使用Cython，未安装扩展时使用原Python路径；
显式`python`用于对照，显式`cython`在扩展缺失时明确报错。独立诊断的对应选项为
`--restore-backend auto/python/cython`，每次结果的`restore_backend`记录实际后端。
没有新增后端注册层。Cython路径的`prepared_observations_to_python`为0；关注新增列准备计时及
`prepared_unique_market_keys/sources/prices`计数，含义是原始字符串唯一值数量。

在项目根目录用目标Python执行`python bomber/framework/datahub/features/setup.py build_ext --inplace`，
只需Cython、setuptools和C编译器，不依赖NT头文件或重新编译交易引擎。每次更新pyx后重新构建。
源码与实际扩展二进制摘要均进入回放输入的实现指纹；正式回测仍需按新指纹生成回放输入版本，
父prepared数据不重做，已有v1可直接用于两种后端的只读诊断。

本机扩展已实际编译，6项后端测试、5项独立诊断及144项关联回归共155项通过。测试覆盖共享字符串、
Decimal表示、Unicode/空值来源、只读/分片列、超过2**53的整数时间、重复键/范围/字典代码拒绝、
可选扩展缺失及自定义特征READY/SKIPPED结果。真实NT验收本机缺SDK跳过，XY随后提供1项通过
（0.817s）：65条行情、23帧、63个观察值，Cython计数为1，临时撮合及财务报表对照通过；
全区间性能与逐笔财务仍待验证。
同机单轮合成样本（45839帧/1488374条观察值、33合约/1来源/5000种价格字符串）逐项摘要相同，
Python解码2.951348s、Cython2.228823s，下降约24.5%；不能据此推算XY真实数据或完整回测收益。

## 基础条款

| 能力 | 期货 | 期权 |
| --- | --- | --- |
| 数据集 | future_basic | option_basic |
| 不可变条款 | FutureBasic | OptionBasic |
| 查询服务 | FutureBasicProvider | OptionBasicProvider |
| 源字段适配 | FutureBasic.from_mapping | OptionBasic.from_mapping |
| 指定时刻查询 | basic_at(symbol, query) | basic_at(symbol, query) |
| 文件准备入口 | dataprep.basic.load_future_basic_provider | dataprep.basic.load_option_basic_provider |

两类模型均保存代码、交易所、币种、生命周期、乘数及最小变动价位；期货另有品种，期权另有标的、认购/认沽、行权价及到期日。代码和交易所标识原样保留，不在参考模型中进行交易所映射或根据代码猜测条款。

期货信号专用条款允许价位及乘数缺失；创建执行合约时必须齐备。期权乘数必须为正，参考条款价位可以缺失，交易场景仍要求有效价位。期权价位优先级为 tickNum → minChgPriceNum → price_increment；期货为 minChgPriceNum → tickNum → price_increment。非空无效值报错，不回退绕过。

## 时间查询与文件适配

两类 Provider 共用 BasicProvider 和既有 ReferenceRecord / AsOfQuery 时间契约：生效、发布时间、来源年龄、日终交易日隔离及重复版本校验保持一致。最新生效版本尚未发布时拒绝查询，不悄悄回退旧版本。已到期合约的历史条款仍可查询，交易资格由调用方判断。

```python
from bomber.framework.datahub import AsOfQuery, DataHub, ReferenceRecord
from bomber.framework.dataprep.basic import load_future_basic_provider, load_option_basic_provider

def record_factory(info, row):
    dataset = "future_basic" if hasattr(info, "product") else "option_basic"
    return ReferenceRecord(
        dataset, info.symbol, info,
        event_ns=int(row["effective_ns"]),
        available_ns=int(row["available_ns"]),
        source_ns=int(row["source_ns"]),
    )

futures = load_future_basic_provider(futures_path, record_factory=record_factory)
options = load_option_basic_provider(options_path, record_factory=record_factory)
hub = DataHub({"future_basic": futures, "option_basic": options})
info = futures.basic_at("rb2605", AsOfQuery(decision_ns))
```

示例时间列必须由调用方根据真实发布时间资料提供，不自动从 date、上市日或自然日推导。文件入口复用 dataprep.session.read_feather 的缓存和输入来源记录。两个 Provider 的 from_feather 作为兼容入口保留，内部委托 dataprep 文件适配器。

现有静态策略继续由 dataprep.metadata 返回 InstrumentSpec 或已规范化原始行；内部使用上述基础条款模型统一校验，但仍拒绝非空 available_ns / source_version。拥有查询 Provider 不代表静态回测入口已经支持盘中条款变更。

## 其他参考资料

外部参考数据库读取归入 [dataprep sources](../dataprep/sources/README.md)。数据库和文件适配器输出标准记录，数据源工厂不放在策略目录；在线期货组合器沿用 SectorRoleAssignment，期权标准行可进入既有 OptionBasic 模型。新增能力不将上市日当作发布时间，也不使静态回测自动获得完整历史发布版本。

| 模块 | 用途 |
| --- | --- |
| temporal | 通用版本、发布及 as-of 时间门控 |
| sector_roles | 多品种角色映射与累计因子快照 |
| role_prices / core | 单品种多角色研究价快照 |
| target_schedule | 外部完整目标计划及可用时间查询 |

这些模型描述不同数据，不要求机械地拥有相同字段。详细时间架构见 [参考资料核心说明](../../../doc/data/DATAHUB_MINIMAL_CORE.md)。

RolePriceStore 按合约与事件时间识别真实收盘记录。完全相同的重复记录只保留一次；同一事件的交易日或价格不同则抛出 ValueError，不依赖输入顺序选择价格。不同时间的分钟序列及不同合约同时报价继续有效，换约锚点仍采用来源交易日最后一条真实收盘。

## 运行环境验证

```bash
python -m unittest tests.test_basic_alignment tests.test_datahub_option_basic
python tests/test_dataprep_common.py
```
