# 期货与期权交易会话通用化专项

初稿日期：2026-10-09。更新日期：**2026-10-13，Asia/Shanghai**。状态：**静态会话核心与CU真实来源／窗口对照已完成；完整专项尚未关闭**。整体GAP关闭状态保持独立，不因本次局部验收调整。

## 0. 当前实现、验收与剩余工作

| 项目 | 当前状态 | 证据与边界 |
| --- | --- | --- |
| 统一会话算法 | 已实现 | `calendars.SessionCalendar`承担窗口和分钟枚举；Runtime旧类只作兼容入口；历史窗口调用同一服务 |
| DolphinDB／Feather时段来源 | 已实现，CU真实对照通过 | `dfs://bomber_static/fut_trading_session` 与 `ROLE_DIR/fut_trading_session.feather`；七字段、无时段manifest |
| 交易日期Provider | 已实现 | `china.sse`／`china.cffex`，维护范围1991-01-01至2026-12-31，超界拒绝；不按品种猜日历 |
| CU实际窗口／分钟对照 | 通过 | 10月12/13日共10个窗口；最近600个完整分钟时间点、区间930个交易分钟时间点，两来源一致 |
| 日期例外 | 来源契约已实现，CU旧数据已迁移 | 7条停夜盘记录保存为标准CSV及Feather；远程例外表尚未导入，本次真实窗口验收使用无例外声明 |
| 旧demo日历清理 | 本地完成 | CU/IM/IF三份日历JSON移除，测试与验收清单更新；清理后`trading-hours`43项本地通过，当前`im-runtime`远程结果待提供 |
| 静态期权会话绑定 | 接口与离线用例已实现 | 显式`asset_type=OPTION`及`hours_code`可选规则；尚无本次真实期权系列数据验收、合约级优先覆盖或历史版本选择 |
| 历史行情预热 | 独立未完成项 | CU历史分钟源尚无；三个`history_*.json`仍在，历史源配置尚未统一；时间点枚举不代表读取了分钟价格 |
| 当前CU Replay Recording | 仅取得运行进度 | TD/要求MD=20261012，CU主力cu2611.SHFE，来源日2026-10-09；`history_enabled=False`，只见3/5根实时Bar预热，无最终summary |
| 历史规则修订与as-of | 尚未实现 | 当前内容指纹证明静态快照一致，不证明当时已生效／可见的规则版本 |

实际输出与本地验证范围见[结构化证据](../evidence/live/TRADING_SESSIONS_CU_PASS_20261013.json)，部署参数见[01当前说明](../../demos/01_main_ema/README.md)。已登记的远程IM 9项、history-provider 41项、GAP03 22项和entry 55项属于此前验证范围，不能替代清理后版本的远程复验。

当前选用**复用时段模板＋日期Provider＋显式绑定＋日期例外**，不逐交易日重复存储普通时段。实际时段字段为：

```text
code, segment_id, date_anchor, start_day_offset, start_time, end_day_offset, end_time
```

DolphinDB边界将`Code`改为`code`，并查询`string(start_time)`／`string(end_time)`避免TIME经SDK表示为1970日期时间戳。Feather直接使用小写`code`与时钟值；规则解析不猜数值时间单位。`TRADING_DAY`锚定所属交易日，`NIGHT_START`锚定此前交易日期，0/1偏移表示自然日期跨午夜。相邻分段不重复计算分钟，区间起含止不含。

例外字段为`code, override_id, date_basis, date, action, target, segments`。文件与数据库使用相同标准化；没有配置或读取失败不解释为空，已核对的无例外范围须显式声明。旧CU例外见[迁移数据](../../scripts/integration/reference_data/configs/session_overrides_cu_2026.csv)。

下一步：先统一历史源配置和因子来源数据，明确可用的历史分钟源；用户计划于2026-10-14开盘验证实际历史预热与实时衔接。CU尚无历史分钟资料，不预写“已读取600根行情”；无最终报告不标记Replay Recording完成。随后验收真实期权会话资料及日期例外部署；历史规则版本／as-of仍按完整目标推进。

下文四类数据、完整版本契约、合约覆盖与展开表描述保留为目标设计；首期实际支持范围以上表及七字段格式为准。

## 1. 问题与目标

2026-10-09审查时，[trading_sessions.py](/Users/kerry/work/bomber1/pro/bomber/framework/trader/runtime/trading_sessions.py)包含CU/IM专用日历和选择分支，[history/windows.py](/Users/kerry/work/bomber1/pro/bomber/framework/dataprep/history/windows.py)重复分钟枚举。当前这两个文件已经委托统一服务；旧名称保留兼容，ImDayWindow仅为单指定日探针。原问题依据见[品种专用实现审查](/Users/kerry/work/bomber1/pro/doc/audits/PRODUCT_SPECIALIZATION_AUDIT_2026-10-09.md)。

目标是通过统一的会话接口和有版本的数据，覆盖已配置的期货、期权交易会话。新增一个已有会话模型适用的品种，只增加资料与配置，不复制类、不追加产品分支。历史窗口、Live、持续运行及恢复预检消费同一会话规则。

“支持所有期货／期权”在本专项中指会话表达能力可以扩展到这些资产，实际可用范围由提供的数据和已实现的会话模型决定。完成会话通用化不等于所有品种的交易 Adapter、行情 Schema、期权行权／结算、费用或风控已经支持。

## 2. 需要提供的四类数据

以下是完整目标契约；首期实际支持的静态字段及版本限制见第0节，不表示当前解析器接受所有目标字段。

| 数据集 | 必需内容 | 解决的问题 |
| --- | --- | --- |
| 交易日历 | calendar_id、适用交易所、date、is_trading_day、明确覆盖起止日期 | 区分交易日、已知休市与未知日期，查询前后交易日 |
| 会话模板 | session_profile_id、timezone、分段编号、开始／结束时间、日盘／夜盘标识、自然日期定位与交易日归属规则 | 表达各类时段、跨午夜和休息间隔；不在品种类中写死时间 |
| 品种／系列绑定 | exchange、asset_type、product_or_series、calendar_id、session_profile_id、生效起止时间；必要时绑定具体 instrument_id | 根据期货品种、期权系列或合约选择规则，避免根据代码猜测 |
| 特殊日期覆盖 | 适用范围、业务日期／交易日、取消或替换时段、完整替换区间、原因及生效信息 | 节前停夜盘、临时停市／调整、合约最后交易日特殊时段等例外 |

没有例外的数据集可以为空，但必须声明所覆盖范围内已核验的例外完整性；“没有读到记录”不能自动解释为没有例外。

会话模板若通过规则展开夜盘，必须明确夜盘锚点、实际自然日期与所属交易日之间的关系，并使用权威日历定位。不能仅使用“自然日加一”或固定工作日偏移处理假期。

### 2.1 期权需要补充的绑定

期权可以显式绑定自己的会话模板，也可以声明继承指定标的的会话模板并提供例外。继承关系必须来自资料，不能默认所有期权与标的时段相同。

现有 [FutureBasic](/Users/kerry/work/bomber1/pro/bomber/framework/datahub/future_basic.py) 已提供期货合约、交易所、品种、上市日和最后交易日；[OptionBasic](/Users/kerry/work/bomber1/pro/bomber/framework/datahub/option_basic.py) 已提供期权合约、交易所、标的、上市日、最后交易日和到期日。这些可以复用，但期权系列标识、会话绑定／继承及特殊时段需要明确补充。

会话模块不需要价格、复权因子、保证金或 Greeks。行权／结算业务也不应混入交易分钟枚举；本专项只读取它们对交易可用区间的明确影响。

### 2.2 公共来源与时间字段

以上数据至少需要以下公共证据：

- 稳定记录键和 Schema 版本。
- 规则生效起止时间、实际可用时间、修订号。
- 来源标识、版本或内容指纹、更新时间。
- 覆盖范围、完整性声明及必要质量标记。

历史查询须同时满足“当时已生效”和“当时已可见”，不能将新规则或事后修订提前用于旧回测。源没有真实历史发布时间时，显式记录观测下界，不补造发布时间。

## 3. 可选的上游展开时段表（未实现）

本轮采用复用静态分段，不要求每天存储重复区间。若上游已有展开区间表，未来可新增对应Provider；当前尚未接受该格式。以下保留目标设计，不能将其作为本轮的交付字段。

建议每行表示一个实际交易区间：

```text
calendar_id
exchange
asset_type                 # FUTURE / OPTION
product_or_series
instrument_id              # 可空；用于合约级例外
trading_day                # 所属交易日
session_id
session_start              # 完整日期时间，包含自然日期
session_end                # 完整日期时间，包含自然日期
timezone
effective_from
effective_to
available_ns
revision
source
```

接口内部统一转换为 UTC 纳秒，保留源时区及转换口径。每个区间采用 `[session_start, session_end)`，时间戳单位必须声明。

夜盘自然日期与 trading_day 分开保存。还需提供日历覆盖与完整性资料，否则某日没有时段记录时，无法区分已知休市和数据缺失。空的 interval 集合只有在覆盖及休市／取消信息完整时才可解释为休市。

展开表中如同时存在品种级与合约级记录，应先按声明的覆盖规则解析成唯一有效区间；不能简单合并后重复计算分钟。模板／绑定／例外方案同样必须声明冲突处理，禁止依赖读取顺序选择版本。

## 4. 公共接口与分层

建议提供统一 SessionCalendarPort，至少承载以下能力；名字沿用现有调用习惯，签名在实施时统一：

| 能力 | 语义 |
| --- | --- |
| resolve | 根据交易所、资产类型、品种／系列／合约及决策时点选择适用规则 |
| is_trading_day / previous_trading_day | 查询覆盖范围内的权威交易日，未知日期明确拒绝 |
| windows / window | 返回指定交易日全部区间，或查询当前时刻所属区间及交易日 |
| missing_minutes | 根据真实会话区间枚举断点后的完整分钟，不制造缺失行情 |
| last_minutes | 跨所需交易日取最近 N 根完整交易分钟，数量上限由服务资源政策表达，不固定为 IM 的 240 |
| provenance | 返回本次使用的日历、绑定、模板／区间版本及来源证据 |

职责建议：

- dataprep Provider 负责文件／DolphinDB 的读取与字段标准化。
- DataHub 负责参考规则的版本、as-of 可见性及数据集查询门面。
- 公共会话服务负责区间查询、时间边界和分钟枚举，接收已选定的规则数据。
- Runtime、历史服务、Demo 入口与恢复预检消费相同接口；策略信号不连接数据库，也不判断 CU／IM 来选择会话算法。

离线消费导出的本地规则数据；Live 经配置的 Provider 获取规则，沿用正式来源政策。会话服务本身不区分规则来自文件还是 DolphinDB。

分钟枚举仅表示应该检查哪些完整时间桶，不证明该桶一定有成交或数据库一定有记录。缺失行情的拒绝／显式允许策略仍由历史服务处理。

## 5. 实施范围与顺序

1. 核对源数据，确定采用“模板＋日历＋绑定＋例外”或“已展开区间表”的交付形式，冻结字段、时间和完整性契约。
2. 实现标准规则 DTO、Provider 及公共会话查询接口，用 CU、IM 现有样例验证旧行为。
3. 让 trading_sessions.py 与 history/windows.py 统一消费该服务；旧 CopperSessions／IndexSessions／ImDayWindow 若需兼容，只作为薄适配，不保留独立规则算法。
4. 同步迁移 EMA Live 的日历选择、continuous 监督器及 check_recovery，避免留下 CopperSessions 直接构造或产品分支。
5. 验收探针可继续限定 IM 样本，但应调用通用服务；公共 history 包不再以 IM 探针模型作为通用入口。
6. 产品到交易所、会话模板、运行模式及已验收交易能力分别声明。通用日历不能自动开放未验收品种报单。

合约工厂 CFFEX 固定、期权源 Schema 和一手仓位接管属于同次审查发现的相关问题，不自动并入本专项实施；需要另行明确范围，以免会话改造扩展成全部交易模块重写。

## 6. 验收清单

- [x] 增加一个已有会话模型适用的期货品种，仅增加数据／配置，不修改公共会话算法；离线用例验证，交易授权限制保持独立。
- [ ] 增加一个期权系列，显式绑定或继承会话规则，合约级例外可覆盖且无重复时段。
- [x] 静态模型的日盘、休息间隔、无夜盘、跨午夜、周末／假期、取消夜盘及替换区间通过离线验证；CU普通窗口取得真实两源对照。
- [x] 开始／结束边界、已完成分钟、跨日最近 N 根和断点后缺口枚举一致；休市时间不计入交易分钟。
- [x] 首期日期超出覆盖、规则缺失、分段／例外冲突和来源读取失败明确拒绝；历史版本完整性不属于已实现范围。
- [ ] 历史模式只使用当时可见的适用规则，修订与新规则不泄漏到更早决策。
- [x] CU相同静态规则下，文件与DolphinDB返回相同区间和分钟序列，保留内容指纹及日历规则版本。
- [x] Live历史装配、持续监督和恢复预检统一消费同一服务；直接Provider快照检查点拒绝规则身份变化。当前版本完整在线运行仍须验收。
- [ ] CU／IM 原有受影响行为通过回归；旧类兼容期不产生第二套规则来源。
- [x] 未验收交易能力仍被明确限制；会话通用化验收不代替实际柜台交易验收。

## 7. 数据交付清单与当前状态

已提供并核验的CU时段源为`dfs://bomber_static/fut_trading_session`，文件位于`/workspace/data/temp_bomber1/ctp/role/fut_trading_session.feather`，七字段及5个分段的实际对照通过。KLINE_DIR不是时段资料目录。日期覆盖由Provider维护，无需旧demo日历JSON。

仍需提供和部署：适用范围内完整的Live日期例外表、真实期权系列绑定／覆盖数据、历史规则修订和实际可用时间，以及实际历史分钟行情源。三份`history_*.json`仍配置CFFEX源与限定日期因子映射，尚未完成统一历史源整理。凭据沿用既有环境，不写入资料或证据。

整体状态为部分完成；本次静态会话与CU对照通过不关闭GAP-04、GAP-06、GAP-07或GAP-08。
