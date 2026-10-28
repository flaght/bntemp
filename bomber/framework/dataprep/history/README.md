# 历史分钟行情Provider

PF2026100911第一阶段源码，远程验证待完成。`HistoryRequest → HistoryProviderFactory → HistoryProvider.read → HistoryBar` 独立于参考资料工厂；策略不连接数据库。导入、构造不联网，协调器显式管理open/read/close，失败也关闭连接。

| 阶段 | 来源规则 |
| --- | --- |
| offline_market_history | 本地file |
| live_market_history / preopen | 文件优先；文件缺失或不足时从配置数据库补足 |
| live_recovery / recovery | 数据库；加载期间已收到的完整实时分钟可补齐对应分钟 |

`HistoryService` 按固定截止点读取，筛选真实合约和区间，完全重复去重、同键冲突拒绝，报告条数、缺口及来源。`missing_policy="fail"` 默认拒绝缺失；显式allow允许仅用实际有效行或零行继续，记录数据库未配置/不可用等原因，不制造价格。Schema错误、内容冲突、未实现的驱动仍失败。交易时效、账户归属及活动订单对账不受此参数豁免。

JSONL和带表头CSV采用标准字段：instrument_id、ts_event，以及close/adjusted_close；OHLC、volume、cumulative_factor可选。时间戳为完整分钟末最后1ns。EMA需要adjusted_close，或close乘该分钟实际cumulative_factor；不使用启动时当前因子回算历史。文件不包含参考角色资料的替代许可。

DolphinDB配置显式声明DFS路径、表名、字段映射、时间单位（ns/ms/s）、时间标签（start/end_ns）和合约格式（full/symbol，symbol须固定venue）。PF0912新增date_column/time_column组合字段，须明确source_timezone=UTC或Asia/Shanghai、time_label=start、time_unit=ns，不同时映射ts_event；查询用concatDateTime/nanotimestamp，显式LONG纳秒常量和北京时间偏移，避免默认INT溢出。只读查询按合约/时间限定，多读一行检测行数截断。连接复用DDB环境配置及超时，表名和行情字段不猜测。当前读取分钟表，尚未实现原始Tick聚合、跨表历史因子连接、历史版本as-of选择、跨主力换月连续行情。

后续MongoDB适配继承同一契约并注册，协调器和策略不变：

```python
from bomber.framework.dataprep.history import HistoryProvider, HistoryProviderFactory

class MongoMinuteProvider(HistoryProvider):
    # open/read/close由实际MongoDB驱动实现。
    # read(request)返回经过时间和合约标准化的HistoryBar元组。
    ...

# 仅在完成三个抽象方法后注册；当前mongodb位置是显式未实现的占位。
HistoryProviderFactory.register("mongodb", MongoMinuteProvider, replace=True)
```

CLI及远程验证见 [实施交接](../../../../doc/HANDOFF/history/HANDOFF_HISTORY_PROVIDER_2026-10-09.md)。

## PF0913显式历史因子与指定日预热

HistoryService的可选price_adjuster在来源合并后计算复权，默认None保持原行为。ReferenceFactorAdjuster使用参考工厂按显式source_days及固定合约读取唯一pcr_cumfactor，不读/覆盖角色main；已有adjusted_close不再乘因子。失败不返回部分处理结果，分钟身份/顺序不可变。因子错误不受allow缺行情豁免。

IM指定交易日使用ImDayWindow枚举两个交易时段共240分钟；不是全年自动日历。JSON可声明factor_source_days和factor_database，demo组装处理器，不把连接放到策略。读取当前历史日期记录，声明来源日及source-day-end/实际available_ns，报告指纹，但不提供历史修订as-of保证。实际固定合约数据和只读EMA预热入口、验证范围见 [PF0913交接](../../../../doc/HANDOFF/history/HANDOFF_HISTORY_WARMUP_2026-10-09.md)。当前源码待远程测试。

## 通用会话窗口

新数据入口使用 `SessionHistoryWindow`（公共 `calendars.SessionCalendar`），
共享交易日快照、静态时段和日期例外。`ImDayWindow` 只保留指定日 IM 探针接口，
分钟枚举已委托同一服务；通用服务不限制为 IM 单日 240 分钟。
数据字段与文件／DolphinDB 装配见 [calendars 说明](../../calendars/README.md)。

正式 EMA 入口默认直接消费 Provider 与原生日历；`--trading-calendar` 不再默认
指向 CU JSON，仅作为显式兼容参数。时段规则选择 `--calendar-id` 和时段来源，
日期例外选择例外源表或明确的无例外声明。测试里的日期通过内存快照固定。


## 交易日日期与累计量额

`DolphinDbHistoryConfig`新增可选 `date_basis=trading` 和 `volume_mode=cumulative`，
默认仍为 `calendar/per-bar`，现有EMA配置和查询不变。交易日模式须以运行时
`session_calendar`传入同一品种、同一时区的SessionCalendar，并配置date_column/time_column。
JSON不携带日历副本；调用方在构建配置时注入日历。开始标签使用start，完成标签使用end。
Provider按请求区间的实际会话确定交易日，再读取这些交易日的有界完整行，转换自然时间并排序。
累计模式要求volume/value同时映射；同一交易日连续交易分钟同时差分，两者不使用不同基准，
OpenInterest不差分。交易日首分钟的前值为0；盘中首行只作基准。零量且非零额、量额倒退、
请求范围内的前值缺失和同键冲突明确失败。盘中首行不能提供完整目标分钟时，由HistoryService覆盖检查拒绝。
午休不重置累计值，跨午夜不重置，切换交易日才重置。声明行数上限包含读取的基准数据。
Hybrid示例为demos/10_hybrid_transformer/history_rb.json；累计模式依据用户截图推断，真实RB源待确认。
