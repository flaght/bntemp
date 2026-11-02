# DataHub 参考资料与特征查询

2026-11-02按当前源码核对。框架整体、积木化装配及各层职责见[Framework总览](../README.md)；以下历史验证记录按各自版本阅读，不用早期“待实现”描述替代当前代码。

DataHub定义不可变参考资料与特征记录，按业务时间、可见时间及版本提供查询；包括期货／期权基础条款、合约角色与研究价、外部目标计划及通用特征。文件／数据库读取和字段标准化由dataprep的Provider完成；真实Tick／Bar进入market，合约执行、订单和仓位属于trader。

当前已有`DataHub(features=FeatureHub(registry)).feature_snapshot(...)`、强类型Schema、可见修订、EXACT／WINDOW／ASOF及等待帧；标准文件、DolphinDB／原表映射和MongoDB特征适配由[公共Provider](../dataprep/features/README.md)接入。普通特征直接查询DataHub，只有需要真实OHLCV输入时才投影成Bar。特征Provider、参考Provider及历史行情Provider分别按自己的实现判断，不能把MongoDB特征适配等同于MongoDB参考／历史适配均已完成。

**来源按用途选择。** 回测／研究执行使用标准本地资料和特征导出包；导出准备任务可以读库。Live参考与特征通过Provider读取已声明来源，Live历史预热／盘中回补可经[HistoryService](../dataprep/history/README.md)读取DolphinDB，并按明确的阶段、覆盖及文件优先政策处理；不再沿用早期“Live历史行情只能本地”的限制。数据源选择不进入策略业务算法，不静默切换来源，也不伪造未知历史发布时间。具体政策见[来源工厂](../dataprep/sources/README.md)与[历史启动规则](../../../doc/HANDOFF/history/HANDOFF_HISTORY_STARTUP_POLICY_2026-10-09.md)。

08回测与默认DFS Live已共用指数特征声明和可见时间契约，接入在应用装配中完成；通用接口已有不表示所有业务与来源组合均已实际验收。

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
