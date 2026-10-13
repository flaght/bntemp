# 期货／期权交易会话通用化专项

日期：2026-10-09，Asia/Shanghai。状态：需求与数据契约整理完成，尚未实施；本次仅新增本文，没有修改运行代码、测试或整体架构评估。

本文专门解决“为 CU、IM 分别编写公共日历和分钟窗口实现”的问题。它是独立实施任务，不新增整体 GAP 编号、不改变任何 GAP 的关闭状态，也不将本专项扩大为完整期货／期权交易引擎改造。

用户提供的 [GAP03_SHA256_2026-10-09.txt](/Users/kerry/work/bomber1/pro/doc/evidence/checksums/GAP03_SHA256_2026-10-09.txt) 是文件摘要与路径组成的同步校验清单，保留原内容；专项需求写在本文。以后实际交付代码时，另建立本专项自己的同步／摘要清单。

## 1. 问题与目标

当前两个公共文件包含三个品种专用类和一个品种选择分支：

- [trading_sessions.py](/Users/kerry/work/bomber1/pro/bomber/framework/trader/runtime/trading_sessions.py)：CopperSessions 只接受 CU；IndexSessions 只接受 IM；load_sessions 按 CU／IM 选择实现。交易时段写在类中，配置主要提供日期与休市信息。
- [history/windows.py](/Users/kerry/work/bomber1/pro/bomber/framework/dataprep/history/windows.py)：ImDayWindow 另写 IM 日盘及分钟枚举，固定单日 240 分钟上限，与 Runtime 的日历规则重复。

目标是用统一会话服务承载交易日查询、会话窗口、完整分钟枚举和历史缺口计算。期货和期权的品种差异由可核验的数据／配置选择；同一会话模型覆盖的新产品只需增加配置，不复制公共类，也不增加产品代码分支。

“支持所有期货／期权”指具备覆盖已配置市场、产品及有效日期的通用会话表达和计算能力。没有权威数据或存在尚未表达的特殊规则时明确拒绝，不能猜测交易时间。该目标不等于所有品种的实盘下单、手续费、保证金或行权结算均已支持。

## 2. 必需的四类数据

以下名称为建议契约，不是当前已经存在的 DolphinDB 表名；可以根据现有数据通过 Provider 映射。

### 2.1 交易日历

| 字段 | 要求 |
| --- | --- |
| calendar_id | 稳定日历标识；不能只按品种名字猜日历 |
| exchange | 标准化交易所身份，保留源编码映射依据 |
| date | 自然日期 |
| is_trading_day | 明确是否交易日 |
| coverage_start / coverage_end | 数据包或版本声明的完整覆盖范围 |
| timezone | 明确的时区名称 |

已声明覆盖范围内须能区分休市、数据缺失和未知日期。不能因为某日没有行情文件就认定休市，也不能仅按工作日猜测实际交易日。历史窗口需要覆盖足够的前序交易日，不能只提供当天日期。

### 2.2 会话模板

| 字段 | 要求 |
| --- | --- |
| session_profile_id | 可复用会话模板标识 |
| segment_id / sequence | 各连续时段的标识及顺序 |
| start_time / end_time | 本段开闭时间，区间采用起含止不含 |
| timezone | 本段使用的时区 |
| anchor_rule | 明确本段自然日期如何定位，不猜夜盘是哪一天 |
| trading_day_rule | 明确本段所属交易日，支持跨午夜和跨休市间隔 |
| valid_from / valid_to | 规则的有效日期范围 |

模板须表达日盘、休息间隔、夜盘和跨午夜结束。anchor_rule／trading_day_rule 采用受控规则，不执行数据消息携带的代码；复杂日期归属可以由 Provider 直接展开为实际区间。

### 2.3 产品／合约与会话绑定

| 字段 | 要求 |
| --- | --- |
| exchange | 交易所 |
| asset_type | FUTURE 或 OPTION |
| product_or_series | 期货品种或期权系列标识 |
| calendar_id / session_profile_id | 选择的日历和会话模板 |
| instrument_id | 可空，非空表示合约级绑定 |
| valid_from / valid_to | 绑定生效范围 |
| underlying_id / inherits_session_from | 期权如继承标的会话，须显式声明来源及例外 |

同一品种的不同到期合约通常可以共享会话配置；合约级例外覆盖产品级配置。期权不能因为代码前缀相似就默认继承期货时段，也不能仅因资产类型为 OPTION 就统一使用某一种日盘模板。

合约身份、上市日和最后交易日可复用已有 [FutureBasic](/Users/kerry/work/bomber1/pro/bomber/framework/datahub/future_basic.py)、[OptionBasic](/Users/kerry/work/bomber1/pro/bomber/framework/datahub/option_basic.py)。目前 OptionBasic 有 underlying，但没有通用期权系列／会话绑定，需由映射数据补齐；基础资料本身不等于已经提供交易时段。

### 2.4 特殊日期／合约覆盖

| 字段 | 要求 |
| --- | --- |
| override_id | 稳定例外标识 |
| calendar_id / exchange | 适用日历及市场 |
| product_or_series / instrument_id | 覆盖范围；明确通用、产品级或合约级 |
| date / trading_day | 明确例外作用于自然日期还是交易日 |
| action | 取消某段、替换时段或提供完整当日区间 |
| replacement_segments | 替换后的实际时段，取消时可空 |
| reason / source | 调整依据与来源 |

用于节前夜盘取消、临时停市、时段调整及合约最后交易日特殊安排。确实没有例外时也须声明所覆盖范围及数据完整性，不能把没有读取到例外表解释为一定没有例外。

建议优先级为：明确合约例外 > 产品例外 > 日历范围例外 > 基础模板；同一优先级的冲突数据应拒绝，不按输入顺序任选一条。实际优先级在实施前依据源表语义定稿。

## 3. 可选的直接交付格式：实际会话区间表

如果 DolphinDB 可以提供已展开的真实会话区间，可以直接映射到统一服务，减少框架内的日期推导。它是“会话模板＋日期覆盖规则”的替代交付方式，不要求上游同时重复维护两份规则。

```text
calendar_id
exchange
asset_type
product_or_series
instrument_id          # 可空，表示产品级区间
trading_day
segment_id
session_start          # 带日期与时区的完整时间，或明确单位的 UTC 时间戳
session_end
timezone
```

每行表示一个实际连续交易区间；夜盘自然日期通过 session_start／session_end 表达，所属交易日通过 trading_day 表达。仍需要完整日历／覆盖声明、产品或合约映射、来源版本与可用时间，才能区分休市和漏记录。

例如多个产品使用同一套已核验会话模型时，共享 profile 或区间数据，不为每个产品生成一个 Python 类。本文不提供猜测的真实市场时段样例。

## 4. 所有数据共同需要的证据

- source：权威来源或上游生产依据。
- version / revision：本次规则版本及修订身份。
- effective_from / effective_to：规则在业务上适用的时间范围，可映射前述 valid 字段。
- available_ns：该版本实际最早可被系统使用的时间；如果只有读取观测时间，明确标记其证据边界，不伪造历史发布时间。
- 时间类型、单位和时区声明：统一转换为内部时间表示，禁止按数值长度猜秒／毫秒／纳秒。
- 覆盖范围、更新频率和更新失败行为：当前版本过期或来源不可用时给出明确状态，不能静默沿用未知规则。

多个相关表应能按同一版本或明确一致性批次装配。Runtime、历史预热和恢复预检使用同一规则快照，避免查询期间更新导致会话与分钟窗口来自不同版本。

## 5. 用户需要提供的具体材料

1. DolphinDB 的数据库路径、相关表名、字段名／类型及时间单位；若数据来自文件，提供对应文件格式和列定义。
2. 四类数据或已展开会话区间表的少量真实样例，包括产品、资产类型及合约身份映射。
3. 日盘、夜盘跨午夜、假期前后、期权会话绑定各一个有来源依据的样例；如果某类暂时没有数据，注明未覆盖。
4. 数据可覆盖的历史与未来日期范围，以及来源版本／发布时间字段；没有字段时说明实际能够提供的证据。
5. 现有 future_basic／option_basic 与品种／期权系列／标的的关联键及交易所编码映射。

不需要为本专项提供账户凭据、历史价格、复权因子、保证金、手续费或策略信号数据。分钟行情仍需由原 HistoryProvider 提供；本专项仅判断“哪些分钟属于应有交易窗口”，不会补造行情。

## 6. 实施范围与接口方向

建立统一的会话读取接口，至少保留现有消费者需要的能力：

```text
is_trading_day(day)
previous_trading_day(day)
window(now)
last_minutes(before_ns, count)
missing_minutes(after_ns, before_ns)
```

装配时由交易所、资产类型、产品／合约及有效时点选择规则。分钟枚举算法共享，历史日视图也是同一服务上的日期限定查询，取消 ImDayWindow 的独立时段算法与 IM 固定上限。

数据读取由统一 Provider／数据访问层完成，在线可从 DolphinDB 获取，离线消费可验证的规则快照；来源策略沿用已明确的项目边界。策略和分钟算法不直接拼数据库查询。

现有调用范围需一并核对：

- [Live 入口](/Users/kerry/work/bomber1/pro/demos/01_main_ema/run_live.py)：将产品分支拆成会话选择、市场身份和已验收运行能力声明。
- [持续运行](/Users/kerry/work/bomber1/pro/demos/01_main_ema/continuous.py) 与 [恢复预检](/Users/kerry/work/bomber1/pro/demos/01_main_ema/check_recovery.py)：消费同一会话服务，恢复时核对保存的身份及规则版本。
- [history 公共导出](/Users/kerry/work/bomber1/pro/bomber/framework/dataprep/history/__init__.py)：审慎处理 ImDayWindow 的兼容入口及弃用路径。
- IM 专项 [预热](/Users/kerry/work/bomber1/pro/scripts/integration/history/warmup.py)、[恢复](/Users/kerry/work/bomber1/pro/scripts/integration/history/resume.py)、[重启探针](/Users/kerry/work/bomber1/pro/scripts/integration/history/restart.py)：可以保留样本限定，但使用公共会话实现，不能另维护 IM 时段。

合约工厂、期权资料 Schema 和仓位接管范围属于 [专项审查](/Users/kerry/work/bomber1/pro/doc/audits/PRODUCT_SPECIALIZATION_AUDIT_2026-10-09.md) 中的另外三类限制，不在本次会话通用化中自动改造。修改会话模型不自动扩大实盘支持或既有交易授权。

## 7. 专项待办与验收标准

- [ ] 核对真实来源 Schema，确定采用模板／例外数据或直接会话区间表；记录缺少的源数据。
- [ ] 固化字段映射、日期归属、版本及冲突优先级；保留期货／期权与合约例外。
- [ ] 实现统一会话 Provider 和服务，Runtime 与 history 共享窗口及分钟枚举。
- [ ] 调整 Live、持续运行、恢复预检和探针的会话装配，消除公共 CU／IM 算法分支。
- [ ] 新增一个已有模型覆盖的期货品种，只增加配置／数据即可查询窗口和分钟，不改公共算法。
- [ ] 新增一个期权系列，显式绑定会话模板或标的继承规则；产品与合约例外均有效。
- [ ] 验证区间开闭、跨午夜、跨休市间隔、禁夜盘、特殊日期、最后交易日及规则版本切换。
- [ ] 验证未知／过期／缺失／冲突规则明确拒绝；同一输入在历史与在线得到相同会话结果。
- [ ] 验证历史窗口不再固定为 IM 的 240 分钟；按配置长度跨已知交易日计算，休市不计缺口，不完整分钟不提前释放。
- [ ] 验证预热不产生历史交易、恢复不重复推进指标，原行情缺失政策和账户／订单对账约束保持各自语义。
- [ ] 完成受影响链路验证后，以独立交接和同步清单记录实现版本及结果；未执行的验证不得标为通过。

## 8. 后续进度记录

| 阶段 | 当前状态 | 证据／下一步 |
| --- | --- | --- |
| 问题分析 | 完成源码静态核对 | 两个公共文件、三个专用类及品种选择函数，见专项审查 |
| 数据要求 | 已整理 | 本文第 2—5 节；实际源表、字段及样例待提供 |
| 接口与方案 | 待源数据核对后定稿 | 不以占位字段声称已有真实 Provider |
| 代码实施 | 未开始 | 本次未改代码 |
| 验证与关闭 | 未开始 | 本文第 7 节独立验收，不改变整体 GAP 记录 |
