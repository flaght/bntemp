# Bomber 校验清单与历史证据索引

这里集中保存原 HANDOFF 目录中的 50 份 SHA256 清单与 4 份补丁、同步脚本和删除清单。任务实施与运行结果继续查[交接索引](../HANDOFF/README.md)。

## 2026-10-13 CU静态交易会话与清理证据

[结构化记录](live/TRADING_SESSIONS_CU_PASS_20261013.json)保留用户实际DolphinDB／Feather会话对照JSON及原粘贴内容SHA256：CU5段、10月12/13日的10个窗口、600个最近分钟时间点及930个区间分钟时间点一致。原远程报告文件名未提供，不补造。文件清理组43项的本地报告、第二套TD/MD只读和CU Replay的3/5根实时Bar进度分别记录；没有把时间点枚举写成实际行情预热，也没有把进度写成最终Recording通过。

[清理交付摘要](checksums/EMA_SESSION_PROVIDER_CLEANUP_20261013_SHA256.txt)描述当轮代码／数据版本；后续并行代码改动需单独核验，旧清单不重写为新版本。三份旧日历已移除，7条停夜盘记录保存CSV及Feather，Live例外表尚未导入。三个历史行情JSON的配置统一、实际历史预热／接续、真实期权资料和历史规则as-of仍待完成，见[专项台账](../data/TRADING_SESSION_GENERALIZATION_2026-10-09.md)。

[本次文档与证据摘要](checksums/TRADING_SESSION_STATUS_DOCS_20261013_SHA256.txt)只校验状态更新文件，不是运行代码部署清单；本次未重新运行行情／柜台验收。

## 2026-10-13 实时期权 Recording 目标链证据

用户返回 08 的实时 CTP LAST + DolphinDB DFS Recording 完整输出：约 15 分钟正常运行、15 帧消费、11:19 的 MO2611-C-7300 空 1 手目标到达客户端，18 根过期候选被忽略。由控制台 summary 提取并调用既有报告复核函数，target_chain 检查通过，见 [精简证据](live/OPTION_VEGA_REALTIME_RECORDING_20261013.json) 和 [08 运行说明](../../demos/08_option_vega/README.md)。本次没有柜台报单、成交或线上指数超时样本；运行机原 summary 与进程退出码没有单独读取。

同日用户补充实时 SimNow 原报告字段：13:36 选约 MO2611-C-7400 空 1 手，1 次报单、1 次归属成交，策略/执行/柜台净仓一致，status=passed、停机活动订单 0、清理无错误。见 [实时 SimNow 开仓证据](live/OPTION_VEGA_REALTIME_SIMNOW_FILL_20261013.json)。成交价格与柜台单号未提供，原状态读回、重启恢复、实际平仓及非零期货对冲成交仍待核验。

后续同日恢复 resume_ready=true，但该持仓合约原始深度事件年龄 30.172 秒超过配置的 30 秒，运行 failed、报单 0，停机仍保留空 1 手、活动订单 0、清理无错误。该片段确认恢复启动完成，持续运行至收盘与主动/定时正常停止尚未通过，单独记录于上述证据的 resume_followup，不覆盖原开仓通过结论。

原始行情对比又得到两个不同阶段样本：14:46—14:48 单合约 75 条、最大间隔 9.097 秒；14:57—14:59 全链 105 个全部订阅成功，整体 884 条、最大间隔 1.959 秒，IM2611 持续更新，而持仓期权仅 1 条。后者已进入期权收盘集合竞价，不能直接据此归因订阅数量；交易阶段规则、观测事实与推断分别记在上述证据及 [08 说明](../../demos/08_option_vega/README.md)。诊断命令已修正，下午采样须在 14:57 前完成；连续阶段的恢复超时问题仍未定位。

## 2026-10-14 开盘后的原始行情对比准备

[同连接全链／缩减／全链探针](commands/OPTION_VEGA_RAW_ABA_PROBE.py) 重新读取当日 MO/IM 条款，随后关闭参考连接，仅 MD 依次采样三段，每段默认 300 秒；期权 MO2611-C-7400 及 IM2611 连续订阅，不读取昨日检查点、不连接 TD、不报单。本地无网络边界及回调检查通过。用户已补齐 09:41:47—09:56:51 三段及切换确认：107 合约 FULL_1 的整体最大接收间隔 31.55 秒，缩减到 2 合约后为 1.94 秒，恢复全链 FULL_2 为 13.91 秒；订阅/退订/重订分别 107/107、105/105、105/105，最终 completed、failure=null、cleanup_errors=[]。完整对比复现超过 30 秒的原始接收空档并支持订阅负载关联，具体上游/原生回调/进程原因仍未确定，不登记正式 Feed 通过。增强版探针新增空档起止与前后源时间、控制线程停顿和进程 CPU 诊断，尚无增强版线上结果。见 [精简证据](live/OPTION_VEGA_RAW_ABA_20261014.json) 及 [08 说明](../../demos/08_option_vega/README.md)。

## 2026-10-14 DolphinDB 一分钟推送改造

按用户要求完成 08 市场流接入补齐：共享流表的实际列名解析、零成交分钟及累计量差分、真实接收时间、持续观测的健康检查、分钟标签与必需腿时效、转换失败拒绝，以及不连接 CTP MD 的 Recording 路径。CTP TD 执行仍保留。共享表名已由后续截图确认，结构与业务语义仍待核验；本机没有新连接、报单或状态迁移。

用户随后确认已更新远端，并提供发布状态截图：市场分钟流 `stream_cffex_1min`、指数分钟流 `stream_index_1min`。表名已登记，字段结构、实际当日新行和分钟/量语义仍未验证；不将本地源码或截图当成远端文件独立核验。新增 [只读原始分钟推送探针](commands/OPTION_VEGA_DOLPHIN_STREAM_PROBE.py) 输出实际列顺序、三键原始行与接收时间，用实际适配 + 假 SDK 验证两流订阅、结构映射、三键记录及失败清理；没有真实网络采样。该探针不运行策略，不关联旧持仓。

本地 72 项 SDK 实体替身下的公共装配/归属/特征/推送回归、43 项策略及入口回归、28 项参考数据/日历回归、29 项通道回归、11 项报告复核回归共 183 项均通过；另有原生 DolphinDB Driver 无网络探针通过。76 项当前来源摘要预检与独立 entry 组通过。参考源和两项日历依赖在此前工作中已变更，本轮未改它们的源码；按当前回归通过版本纳入新部署摘要，旧证据摘要保持原值。

交付见 [运行文件及文档摘要](checksums/OPTION_VEGA_DOLPHIN_PUSH_20261014_SHA256.txt)、[源码包](archives/OPTION_VEGA_DOLPHIN_PUSH_20261014.tar.gz)、[离线范围及待验事项](live/OPTION_VEGA_DOLPHIN_PUSH_20261014.json) 和 [Recording 命令](commands/OPTION_VEGA_DOLPHIN_RECORDING.sh)。部署包包含两项验收入口，须整体同步后校验；运行说明及共享流表只读发现语句见 [08 文档 3.6](../../demos/08_option_vega/README.md)。当前不能把客户端回归当成真实推送、柜台成交或旧持仓迁移通过。

已确认表名后的补充交付为 [REV2 摘要](checksums/OPTION_VEGA_DOLPHIN_PUSH_20261014_REV2_SHA256.txt) 和 [REV2 源码包](archives/OPTION_VEGA_DOLPHIN_PUSH_20261014_REV2.tar.gz)，包含新的只读原始推送探针和最新文档，运行模块及验收入口字节保持初次交付版本。原包和原摘要不覆盖，仍描述当次交付；若原代码已部署，本轮诊断只需补充探针文件。

用户第一次探针实际运行 failed：客户端 actionName 含连字符被服务器拒绝，指数又缺 date/minTime；市场 15 列结构已读到，但三条监控行情均未收到。该结果不登记为订阅成功。最新 [REV3 源码包](archives/OPTION_VEGA_DOLPHIN_PUSH_20261014_REV3.tar.gz) 和 [REV3 摘要](checksums/OPTION_VEGA_DOLPHIN_PUSH_20261014_REV3_SHA256.txt) 修正两处任务名生成并添加启动前字符校验；只读探针迁入 scripts，新增 market/index/both 范围，原始指数不再猜测 date/minTime，正式策略仍保留严格时间校验与 DFS 指数路径。76 项主回归 + 111 项相关回归通过，77 文件版本预检及 entry 组通过；该版交付时修正版线上结果待返回，后续接收见下文。见 [失败证据及修正范围](live/OPTION_VEGA_DOLPHIN_ACTION_FIX_20261014.json)，旧交付包不覆盖。

修正版市场探针结果现已返回：[11:27—11:29 接收证据](live/OPTION_VEGA_DOLPHIN_MARKET_RECEIPT_20261014.json) 登记 market/120 秒 completed、无错误及缺失键，实际收到 IM2611/MO2611-C-7400 两个连续业务分钟。累计量证据为 IM 增量 47、MO 增量 1，MO 成交额增加 7200，与 72 × 100 相符。实际 NumPy 日期/TIME 样本的本地转换回归通过，主回归增至 77 项；当前没有新的运行代码修改。下一步用已部署 REV3 在下午做 Recording，同分钟帧及非零目标仍待验，指数流未验收，end 标签仅由接收时序推断。

样本回归与最新证据补充为 [REV4 摘要](checksums/OPTION_VEGA_DOLPHIN_PUSH_20261014_REV4_SHA256.txt) / [REV4 包](archives/OPTION_VEGA_DOLPHIN_PUSH_20261014_REV4.tar.gz)，更新测试、验收入口摘要和文档；原 REV3 运行模块字节保持不变，已部署者可直接运行现有 Recording 命令，无需为这次样本补充重启或更新运行模块。旧交付包保留原版。

13:22 Recording 的 [旧分钟失败证据与修正](live/OPTION_VEGA_DOLPHIN_OLD_MINUTE_20261014.json) 已返回：市场流转换 90 根、无转换错误，指数 13:22 投影成功后又发布 13:21，触发时间倒退；没有消费帧或生成目标。最新 [REV5 包](archives/OPTION_VEGA_DOLPHIN_PUSH_20261014_REV5.tar.gz) / [REV5 摘要](checksums/OPTION_VEGA_DOLPHIN_PUSH_20261014_REV5_SHA256.txt) 修正公共查询 Bar 流跨轮询顺序，旧请求审计 OLDER_THAN_PROJECTED，旧帧缺指数按原期限跳过。保留真实倒退及期限检查；新增实际 Runner/Recording 回归，本地 81 + 111 项及 entry 组通过。完整线上 Recording 仍待复测，旧包保持原版。

后续 [13:40 Recording 复测证据](live/OPTION_VEGA_DOLPHIN_RECORDING_RECEIPT_20261014.json) 已返回 events.txt 和 summary 尾部：运行 passed、市场 1533 根且无转换错误、指数 13:34—13:49 共 16 个分钟投影、缺指数跳过 0；13:40:20.612690 记录 MO2611-C-7300 空 1 手目标，柜台报单 0、清理无错误。107 个合约目标中的 106 个为 0，不是 107 笔订单。初次片段缺完整帧/选约审计，原记录保留当时的待验状态；后续正式复核如下。本轮没有新运行代码或交易动作，REV5 包及其历史摘要保持原字节。

初次片段状态的 [文档与复测证据摘要](checksums/OPTION_VEGA_DOLPHIN_RECORDING_RECEIPT_20261014_SHA256.txt) 保留原值，无需重新部署运行模块。

用户随后执行原报告 --summary 复核，返回 [Recording 完整目标链通过](live/OPTION_VEGA_DOLPHIN_RECORDING_ACCEPTANCE_20261014.json)：scope=target_chain、status=passed、errors=[]，市场流/指数查询/指数送达/目标链四项均为 true；frames=16、skipped_index_frames=0、runtime_failure=null。13:40 共 49 个候选，目标 MO2611-C-7300 空 1 手、同月期货绑定 IM2611；同分钟、接收期限及实际请求一致性已由公共复核函数核对。只读模式 worker_exit_code=null 正常。没有线上超时样本，也不构成 DolphinDB 市场下的柜台成交、旧仓恢复或运行至收盘验收。最新 [正式验收文档与证据摘要](checksums/OPTION_VEGA_DOLPHIN_RECORDING_ACCEPTANCE_20261014_SHA256.txt) 描述本次状态更新，原片段证据和运行源码包不覆盖。

用户要求继续验证后，补充 [SimNow 原仓只读预检](../../scripts/integration/options/simnow_preflight.py)，核对原状态和同一柜台当前 MO/IM 总仓/今昨仓、资金及活动订单，其他品种保持外部归属；不发单、不撤单、不改原状态、不开闸。原 20261013 / CTP 身份不能直接用于当前 20261014 / DolphinDB 恢复。9 项专项及原 81 项主回归通过，线上预检仍待返回；跨日与来源迁移未实施，SimNow 新来源成交仍待验。[预检补充包](archives/OPTION_VEGA_SIMNOW_PREFLIGHT_20261014.tar.gz)、[摘要](checksums/OPTION_VEGA_SIMNOW_PREFLIGHT_20261014_SHA256.txt)、[范围说明](live/OPTION_VEGA_SIMNOW_PREFLIGHT_20261014.json) 不覆盖既有 REV5 运行包。

首轮 [预检缺失原文件](live/OPTION_VEGA_SIMNOW_PREFLIGHT_MISSING_STATE_20261014.json) 返回：五项部署摘要均 OK，sample-1791958587861857268 的 failure 为指定原 state 文件不存在。读取失败在 TD 连接之前发生，未报单、未改状态；不能推断柜台仓位已平或文件已被删除。下一步用现有 TD 只读探针取得当前账户事实，并查找原文件/08 SimNow 检查点及备份；summary/events 不能代替检查点，不重建空状态绕过归属。当前真实原仓核对、迁移及新来源成交均未通过。本次 [缺失状态文档与证据摘要](checksums/OPTION_VEGA_SIMNOW_PREFLIGHT_MISSING_STATE_20261014_SHA256.txt) 只更新状态记录，不修改运行代码或原补充包。

## 2026-10-11模型运行与恢复证据

同日08期权Vega源码新增，使用独立清单
[PF2026101102_SHA256.txt](checksums/PF2026101102_SHA256.txt)。145项策略/公共回归
及SDK实体替身下的08装配与归属回归通过；原生回测、行情流和柜台交易待验，见
[08交接](../HANDOFF/live/HANDOFF_OPTION_VEGA_2026-10-11.md)。

用户随后反馈远程两组43项离线测试通过，记录于
[OPTION_VEGA_OFFLINE_20261011.json](live/OPTION_VEGA_OFFLINE_20261011.json)。
10项装配使用目标环境SDK实体，行情/执行仍为夹具；本次公共组及实际行情/交易
待验。代码保持PF2026101102，文档和证据更新后的完整摘要为
[PF2026101102_REV2_SHA256.txt](checksums/PF2026101102_REV2_SHA256.txt)，
远程代码部署继续使用原`PF2026101102_RUNTIME_SHA256.txt`。

后续[归属回归证据](live/OPTION_VEGA_ATTRIBUTION_20261011.json)记录attribution19项
OK和集中入口所选组通过摘要，含新增自身冻结份额用例；live-common/assembly
明细未在该摘录展开，不补造计数。当前含文档的完整清单为
[PF2026101102_REV3_SHA256.txt](checksums/PF2026101102_REV3_SHA256.txt)，运行代码不改。

[MODEL_REPLAY_20261011.json](live/MODEL_REPLAY_20261011.json)保存用户远程输出的
01模型运行、04模型运行、04同文件恢复的最终摘要和逐笔模型成交，并记录原粘贴
文本SHA256、报告目录与北京时间。三项均passed；04恢复接续版本6→7，第4帧才
执行新目标，旧仓位保留，目标/归属/执行账本一致。所有回报为model来源，
不能作为物理柜台或第三方多账户成交证明。

本轮只更新文档/证据，代码保持PF2026101101 REV2；
[PF2026101101_REV3_SHA256.txt](checksums/PF2026101101_REV3_SHA256.txt)是当前完整
01/04当轮清单，包含当时代码基线与文档/证据。旧清单保留历史哈希，不重写为当前版本。
01恢复、双策略同时运行和第三方真实成交仍待验收，详见
[模型验收交接](../HANDOFF/live/HANDOFF_MODEL_REPLAY_2026-10-11.md)。

## 整理后的校验规则

2026-10-10 分类迁移更新了清单中的文档路径，并对改动过的 Markdown 重算摘要。运行代码的历史摘要保留原值；因此早期清单仍描述各自的历史代码版本，不能要求所有早期清单同时匹配当前源码。文档摘要重算只说明文档文件字节变化，不构成新的运行验收证据。

原始清单、整理前文档和原有外部文档引用已完整保存在[迁移前备份](archives/DOCUMENTS_BEFORE_2026-10-10.tar.gz)。按正文关系进一步调整分类前的材料见[内容复核前备份](archives/DOCUMENTS_BEFORE_CONTENT_REVIEW_2026-10-10.tar.gz)。新旧路径见[迁移记录](../MIGRATION_2026-10-10.md)。[本次文档整理清单](checksums/DOCUMENTATION_20261010_SHA256.txt)校验整理后的文档、路径对照、历史清单及原始材料。

所有 SHA256 清单的文件路径均相对于 pro 项目根目录。例如在项目根目录校验本次文档整理结果：

```bash
sha256sum -c doc/evidence/checksums/DOCUMENTATION_20261010_SHA256.txt
```

## 原有同步与证据清单

| 清单组 | 对应任务 | 文件 |
| --- | --- | --- |
| GAP03 | 参考来源、时效、时钟修正与关闭记录 | [GAP03_CLOCK_FIX_SHA256_2026-10-09.txt](checksums/GAP03_CLOCK_FIX_SHA256_2026-10-09.txt)； [GAP03_CLOSURE_SHA256_2026-10-09.txt](checksums/GAP03_CLOSURE_SHA256_2026-10-09.txt)； [GAP03_SHA256_2026-10-09.txt](checksums/GAP03_SHA256_2026-10-09.txt) |
| ONLINE_PARAMETERS | 在线参数记录 | [ONLINE_PARAMETERS_20261010_SHA256.txt](checksums/ONLINE_PARAMETERS_20261010_SHA256.txt) |
| PF2026100902 | 策略归属仓位闭环 | [PF2026100902_SHA256.txt](checksums/PF2026100902_SHA256.txt) |
| PF2026100903 | 统一订单回调 | [PF2026100903_SHA256.txt](checksums/PF2026100903_SHA256.txt) |
| PF2026100904 | 执行闸门诊断 | [PF2026100904_SHA256.txt](checksums/PF2026100904_SHA256.txt) |
| PF2026100905 | CTP 与 Binance 联合会话 | [PF2026100905_SHA256.txt](checksums/PF2026100905_SHA256.txt) |
| PF2026100906 | 资金查询并发 | [PF2026100906_SHA256.txt](checksums/PF2026100906_SHA256.txt) |
| PF2026100907 | 已撤回的固定目标方案 | [PF2026100907_SHA256.txt](checksums/PF2026100907_SHA256.txt) |
| PF2026100908 | 全新 EMA 空仓验收 | [PF2026100908_SHA256.txt](checksums/PF2026100908_SHA256.txt) |
| PF2026100910 | CU EMA 持续运行 | [PF2026100910_SHA256.txt](checksums/PF2026100910_SHA256.txt) |
| PF2026100911 | 历史 Provider 与启动恢复 | [PF2026100911_SHA256.txt](checksums/PF2026100911_SHA256.txt) |
| PF2026100912 | 固定 IM Schema 与只读读取 | [PF2026100912_SHA256.txt](checksums/PF2026100912_SHA256.txt) |
| PF2026100913 | 历史因子与 EMA 预热 | [PF2026100913_SHA256.txt](checksums/PF2026100913_SHA256.txt) |
| PF2026100914 | 指标文件恢复 | [PF2026100914_SHA256.txt](checksums/PF2026100914_SHA256.txt) |
| PF2026100915 | 独立进程停机重启 | [PF2026100915_SHA256.txt](checksums/PF2026100915_SHA256.txt) |
| PF2026100916 | 正式 IM Recording | [PF2026100916_SHA256.txt](checksums/PF2026100916_SHA256.txt) |
| PF2026100917 | 盘中回补接续计划 | [PF2026100917_SHA256.txt](checksums/PF2026100917_SHA256.txt) |
| PF2026100918 | SimNow 只读观察记录 | [PF2026100918_SHA256.txt](checksums/PF2026100918_SHA256.txt) |
| PF2026100919 | CU 交易总结与策略接手 | [PF2026100919_SHA256.txt](checksums/PF2026100919_SHA256.txt) |
| PF2026100920 | 04 横截面实时装配 | [PF2026100920_EVIDENCE_SHA256.txt](checksums/PF2026100920_EVIDENCE_SHA256.txt)； [PF2026100920_REV2_SHA256.txt](checksums/PF2026100920_REV2_SHA256.txt)； [PF2026100920_SHA256.txt](checksums/PF2026100920_SHA256.txt) |
| PF2026100921 | 04 预期合约参数 | [PF2026100921_EVIDENCE_SHA256.txt](checksums/PF2026100921_EVIDENCE_SHA256.txt)； [PF2026100921_SHA256.txt](checksums/PF2026100921_SHA256.txt) |
| PF2026100922 | 逐合约行情新鲜度 | [PF2026100922_SHA256.txt](checksums/PF2026100922_SHA256.txt) |
| PF2026100923 | 延迟成交 Bar 时效 | [PF2026100923_SHA256.txt](checksums/PF2026100923_SHA256.txt) |
| PF2026100924 | 部署清单与预检 | [PF2026100924_EVIDENCE_SHA256.txt](checksums/PF2026100924_EVIDENCE_SHA256.txt)； [PF2026100924_SHA256.txt](checksums/PF2026100924_SHA256.txt) |
| PF2026100925 | 逐合约深度时效参数 | [PF2026100925_SHA256.txt](checksums/PF2026100925_SHA256.txt) |
| PF2026100926 | 参考刷新移出行情回调 | [PF2026100926_EVIDENCE_SHA256.txt](checksums/PF2026100926_EVIDENCE_SHA256.txt)； [PF2026100926_SHA256.txt](checksums/PF2026100926_SHA256.txt) |
| PF2026101027 | 目标手数上限 | [PF2026101027_SHA256.txt](checksums/PF2026101027_SHA256.txt) |
| PF2026101028 | 多路实时 Bar 按分钟汇合 | [PF2026101028_EVIDENCE_SHA256.txt](checksums/PF2026101028_EVIDENCE_SHA256.txt)； [PF2026101028_SHA256.txt](checksums/PF2026101028_SHA256.txt) |
| PF2026101029 | 固定组合目标续执行 | [PF2026101029_EVIDENCE_SHA256.txt](checksums/PF2026101029_EVIDENCE_SHA256.txt)； [PF2026101029_SHA256.txt](checksums/PF2026101029_SHA256.txt) |
| PF2026101030 | 同日组合恢复与执行时间 | [PF2026101030_SHA256.txt](checksums/PF2026101030_SHA256.txt) |
| PF2026101031 | 恢复夹具断线闭闸 | [PF2026101031_EVIDENCE_SHA256.txt](checksums/PF2026101031_EVIDENCE_SHA256.txt)； [PF2026101031_SHA256.txt](checksums/PF2026101031_SHA256.txt) |
| PF2026101032 | 恢复预热与公共状态过渡 | [PF2026101032_EVIDENCE_SHA256.txt](checksums/PF2026101032_EVIDENCE_SHA256.txt)； [PF2026101032_SHA256.txt](checksums/PF2026101032_SHA256.txt) |
| PF2026101033 | 公共 Live 机制 | [PF2026101033_SHA256.txt](checksums/PF2026101033_SHA256.txt) |
| PF2026101034 | 时钟夹具修正 | [PF2026101034_SHA256.txt](checksums/PF2026101034_SHA256.txt) |
| PF2026101035 | Live 渠道分层 | [PF2026101035_EVIDENCE_SHA256.txt](checksums/PF2026101035_EVIDENCE_SHA256.txt)； [PF2026101035_SHA256.txt](checksums/PF2026101035_SHA256.txt) |
| PF2026101036 | 策略持仓接管、恢复及 REV2/REV3 修正 | [PF2026101036_REV2_SHA256.txt](checksums/PF2026101036_REV2_SHA256.txt)； [PF2026101036_REV3_SHA256.txt](checksums/PF2026101036_REV3_SHA256.txt)； [PF2026101036_SHA256.txt](checksums/PF2026101036_SHA256.txt) |

## 历史补丁与同步材料

| 文件 | 用途 |
| --- | --- |
| [PF2026100902_MAIN_EMA.patch](patches/PF2026100902_MAIN_EMA.patch) | PF0902 主力 EMA 历史补丁 |
| [PF2026100902_SYNC_ASSEMBLY.py](patches/PF2026100902_SYNC_ASSEMBLY.py) | PF0902 装配同步脚本，保留源码 |
| [PF2026100903_HISTORICAL.patch](patches/PF2026100903_HISTORICAL.patch) | PF0903 历史回调补丁 |
| [PF2026100908_DELETE_FILES.txt](patches/PF2026100908_DELETE_FILES.txt) | PF0908 撤回固定目标方案时的删除清单 |

补丁、同步脚本和删除清单的原始字节保持不变。它们描述当时的交付操作，使用前须按对应版本核对；本次整理未执行脚本、应用补丁或删除业务源码。
