# 01／04公共Live机制交接（PF2026101033，2026-10-10）

## 结论与此前公共化

本轮完成源码抽取与两个入口接入。此前已经做过公共化：PF2026100806抽取live_role/live_roles，PF2026100807增加assembly/builders/ManagedLiveRuntime/CtpSessionLifecycle/reports，PF2026100920接入04组合执行。可查HANDOFF_PARALLEL_BACKTEST_LIVE_2026-10-07.md对应登记。本轮是在既有分层上补齐重复机制。

## 公共模块与边界

| 公共文件 | 职责 |
| --- | --- |
| trader/runtime/live_policy.py | LiveTimePolicy、两入口环境CLI、日期/时效校验、7×24前置配对限制、InstrumentHealthGate逐合约深度检查 |
| trader/runtime/ctp_setup.py | 环境凭据及TD工厂、MD配置工厂、实时/回放分钟Feed选择；构造不连接，支持假传输注入 |
| dataprep/live_cache.py | 控制线程查询、原子发布assignment/specs/manifest；查询不持提交锁，发布与发单互斥；缓存年龄、可见时间及失败门控 |
| dataprep/live_cached_role.py | 01数据库单角色缓存适配，固定条款/角色/累计因子版本；文件参考保留原读取行为 |
| dataprep/live_portfolio.py | 从04移动的PortfolioRoleReferences组合适配，共用LiveReferenceCache；demo保留PortfolioReferences别名兼容旧调用 |
| trader/runtime/ctp_account.py | 权威持仓明细解析为多今/多昨/空今/空昨；有限整数手数、投机标志、方向、交易所和合约验证；可选成本/分桶唯一性核验 |
| market/stream/aggregation.py | 可选after_tick回调，在Bar聚合及分派后推进固定目标，替代04两种私有Feed子类 |
| trader/live_roles.py、runtime/ctp.py | 单角色可选逐合约健康门控、参考版本核验；数据库缓存由公共poll刷新，行情分派与缓存发布共用提交锁 |

01的连续恢复仍自行核对策略归属、单手总仓、重建成本账本及EMA状态，只把柜台行解析交给公共模块。04仍自行核对组合归属、保存账本及revision。EMA历史预热、横截面完整帧、策略公式、目标提交和状态文件序列化不移动。

## 参数及兼容

- 01／04继续使用`--simnow-environment realtime|replay`，默认realtime；replay必填`--replay-md-trading-day YYYYMMDD`。已给出的04命令不需要修改。
- 两入口统一`--instrument-max-age-seconds`，默认10秒，有限范围(0,120]；01新增这个参数。
- realtime逐合约检查事件时间与本机单调接收时间；replay允许历史事件，但仍拒绝未来事件、错误MD日、原始时间倒退、缺失深度和陈旧/未来接收记录。
- 回放Bar按接收时间副本生成；原始上游深度与Tick不修改。after_tick接收到聚合所使用的Tick，replay为接收时间副本。
- 全局MD/TD交易日、READY及最近接收10秒限制保持。合约阈值30秒不会放宽全局10秒。
- 01实际CTP必须具备每合约深度；没有深度接口的非CTP手动Feed继续使用既有健康接口。04无深度接口的手动Feed仍通过显式Tick fallback检查，不豁免真实CTP空深度字典。
- 默认Recording、显式报单授权、账户锁、空仓预检、订单上限、断线、停机撤单、状态文件格式及恢复环境一致性检查保持。
- 数据库缓存过期、查询失败、不满足可见时间会拒绝访问；不延長观察限额。01因子版本变化也会闭闸，避免同一EMA窗口混用版本。

## 本机验证与待验项

本轮无网络、无MD/TD连接、无报单、未改交易状态文件。

- Python 3.12公共纯机制回归：`python -m tests.test_live_common`，21项OK，覆盖两环境、全部固定合约、非法深度、缓存失败/过期/时钟回退、查询与发布锁、持仓方向/今昨/成本/重复桶、配置构造及Feed工厂选择。
- 18个本轮Python文件语法编译通过。
- 额外执行选取实际源码声明、以原生数据实体替身运行的冒烟检查：用户04 replay CLI、三合约接收时间分钟聚合、原始Tick保留、门控及固定目标推进、01缓存不查源与因子版本变化。该检查不是完整引擎集成回归。
- 本机尝试加载`tests.test_cross_section_live tests.test_main_ema_continuous tests.run_main_ema_live`，由于原生Bomber Bar引擎缺失，导入失败，未运行用例；不能将此前58项OK算作本轮回归通过。
- 01新增2个原生集成用例：公共深度门控两环境；缓存Bar回调不查询参考源与因子版本变化闭闸。既有04完整帧、恢复、7×24假柜台全流程等测试继续验证公共实现。

同步以下全部文件到Linux项目后，在`uv-nautilus`环境执行（不要求SimNow开放）：

```bash
sha256sum -c doc/evidence/checksums/PF2026101033_SHA256.txt
python -u -m tests.run_cross_section_acceptance \
  --only live-common --only cross-section --only entry --only continuous
```

集中入口已记录公共新增文件、公共runner/聚合器及01连续代码摘要。上述四组是本轮受影响组；本轮未改native_driver/持久化实现，不需要据此重复所有旧组。若本轮原生回归失败，保留日志修复失败组；通过后再在开放时段验证两种实际前置。停机或超时不作为成交通过。01已有状态文件与04原恢复文件均须保留；不自动清仓或创建新文件替代恢复。

## 修改文件清单

以下18个Python文件，6个新公共模块和1个新测试，其余为接入及集中验收清单更新：

- `bomber/framework/trader/runtime/live_policy.py`
- `bomber/framework/trader/runtime/ctp_setup.py`
- `bomber/framework/trader/runtime/ctp_account.py`
- `bomber/framework/dataprep/live_cache.py`
- `bomber/framework/dataprep/live_cached_role.py`
- `bomber/framework/dataprep/live_portfolio.py`
- `bomber/framework/trader/runtime/ctp.py`
- `bomber/framework/trader/live_roles.py`
- `bomber/framework/market/stream/aggregation.py`
- `demos/01_main_ema/run_live.py`
- `demos/01_main_ema/continuous.py`
- `demos/04_cross_section/run_live.py`
- `demos/04_cross_section/live_runtime.py`
- `demos/04_cross_section/recovery.py`
- `tests/test_live_common.py`
- `tests/run_main_ema_live.py`
- `tests/run_main_ema_acceptance.py`
- `tests/run_cross_section_acceptance.py`

文档另改两demo的README、本交接文档以及公共修改登记；新增PF2026101033_SHA256.txt用于完整同步核验，不打包。策略源文件未修改。

## 回退与状态

回退时应整体回退本轮公共模块、两demo入口/恢复代码和验收清单，不能只回退某一个demo造成导入或语义混版。交易状态文件无迁移，无需修改或还原旧代数文件。源码公共化完成；原生离线集成和两环境在线验收保持待验。跨任务待同步。


## PF2026101034：Linux entry组时钟夹具修正

附件098720ab：entry组52项、4个数据库完整入口错误，均为参考缓存过期。夹具一次sleep推进三分钟但没有控制poll；公共化后查询只在控制线程，故新缓存正确拒绝旧观测。当前仅修改tests/run_main_ema_live.py：数据库正例每5秒推进虚拟时钟并调用真实controller.poll，真实run_session保留；增加不运行控制刷新仍拒绝过期缓存的负例。生产代码及30秒阈值保持。

本机公共21项OK、测试语法OK；实际pump源码与真实缓存的两环境/有无控制刷新四种替身冒烟通过；原生entry仍须运行机复测。其它组结果没有从本附件完整获得，不能补宣称通过。同步改动的测试文件、本文档、公共登记及新的完整当前SHA清单后执行：

```bash
sha256sum -c doc/evidence/checksums/PF2026101034_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only entry
```

PF1033摘要保留原记录，不使用它核验PF1034修改后的文件。本轮新增摘要覆盖相同22个源码/文档文件；已完整同步PF1033的机器只需更新上述3个文件及新摘要，无需打包或重复此前已通过组。


## PF2026101035：按来源分离Live公共层

当前实现已迁移到runtime/live，旧模块仅保留重导出。CTP与BN各自定义独立行情和
执行检查，策略不识别realtime/replay；04时间转换归入Feed。live-common现含48项
纯Python检查。完整本轮文件清单与运行机复测见
[Live渠道交接](HANDOFF_LIVE_CHANNELS_2026-10-10.md)，部署使用PF2026101035_SHA256.txt。
PF1033/PF1034描述与摘要作为历史记录保留，不用于核验本轮变更后的源码。
