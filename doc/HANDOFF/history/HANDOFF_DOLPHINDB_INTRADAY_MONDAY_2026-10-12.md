# DolphinDB盘中行情回补交接：周一新session继续

交接日期2026-10-09，北京时间；计划周一2026-10-12日盘继续IM在线Recording，实际交易日仍以CTP TD/MD核验为准。用户明确当前DolphinDB只有CFFEX分钟行情，没有CU历史分钟；IM/IF没有夜盘，因此该部分暂存，不能用CU夜盘冒充IM在线接续验收。没有创建新聊天、定时任务或远程连接。当前聊天另继续既有SimNow交易流程。

## 1. 新session首先读取

本地项目：/Users/kerry/work/bomber1/pro。远端项目：/workspace/worker/pj/Chrono/nautilus_trader_demo/pro。远端Python：/workspace/worker/env/uv-nautilus/bin/python（uv-nautilus）。本机只处理源码/文档/静态SHA，不运行Python、测试、回测或编译。无已核验远程访问，不自行猜SSH或宣称代跑。

最新源码部署清单：[PF2026100916_SHA256.txt](../../evidence/checksums/PF2026100916_SHA256.txt)。当前handoff文档清单PF2026100917_SHA256.txt只核对本轮文档，不代替源码清单。用户已完成PF0916离线阶段，源码未改时不重复已过测试。

请保持两项用户决定：主力/因子角色偏差暂缓，后续统一修复，不阻塞固定合约验证；20260922 13:00分钟缺口暂缓，不造行/价格、不改时间标签，允许缺失时明确记录。

## 2. 已实现的设计与运行语义

统一HistoryProviderFactory选择行情后端，策略不连接数据库；ReferenceSourceFactory负责因子/条款，与原始分钟后端分离。已支持file、DolphinDB，可注册未来后端；MongoDB仅占位，尚未实现实际查询。

用户四条规则已接入：盘前优先对应文件，无文件或不足则数据库补齐；盘中恢复直接数据库；窗口可声明240等；history-missing-policy默认fail，显式allow可接受真实不足并报告required/loaded/missing。允许缺失不豁免EMA最低初始化、错误合约/无效价格/因子、MD/TD健康和仓位/订单权威对账。

历史及停机回补仅warm_closed推进EMA，不发布历史目标、不撮合历史订单。启动期间实时完整Bar缓冲，合并去重追赶后才放行新Bar；相同分钟冲突失败、重复新Bar不重复推进。正式CU持续恢复仍使用账户、TargetStore、归属、订单、CTP账本及EMA共同generation检查点，柜台查询独立必需。只读指标探针文件不能用作run_live的state-file。

主要代码：

| 责任 | 路径（相对pro） |
| --- | --- |
| 请求/Bar/工厂/服务/文件/数据库 | bomber/framework/dataprep/history/ |
| 显式逐事件日期/合约累计因子 | bomber/framework/dataprep/history/factors.py |
| 单指定日IM窗口（探针） | bomber/framework/dataprep/history/windows.py |
| 正式CU/IM日历选择 | bomber/framework/trader/runtime/trading_sessions.py |
| EMA保存/恢复 | demos/01_main_ema/checkpoint.py |
| 历史配置/回补/实时缓冲 | demos/01_main_ema/history.py |
| 正式运行入口/连续恢复 | demos/01_main_ema/run_live.py、continuous.py |
| 正式CTP生命周期/健康/账户 | bomber/framework/trader/runtime/ctp.py |
| 只读验证入口 | scripts/integration/history/readonly.py、warmup.py、resume.py、restart.py |

EMA恢复曾将保存值作为引擎首笔输入，造成再次加权舍入；现直接保留double，后续按相同公式和计数推进，恢复瞬间精确比较不放宽。该修正已有真实指标恢复证据。

## 3. 实际DolphinDB表与价格口径

原始分钟：dfs://min_bar/cffex_1min。Code为真实合约，例如IM2610/IM2611；date+minTime为北京时间分钟开始。concatDateTime/nanotimestamp转换到UTC，再映射分钟末最后1ns作为ts_event；OHLC原始价不改。volume/turnover源为累计值，本轮不映射为分钟量，EMA只验价格。

因子：dfs://bomber_daily/fut_adjustment_factors，按固定真实合约、明确历史日期读取唯一pcr_cumfactor；adjusted_close=raw_close*pcr_cumfactor，不重复累乘pcr_factor，不拿当前因子覆盖所有历史。factor_source_days逐事件日期声明来源日，必须早于事件日期。无available_ns时记录declared_source_day_end假设；fingerprint为当前读取证据，不冒充历史修订as-of版本。

真实固定验证20260922 IM2610使用20260921因子1.456591。用户截图最新Oct8因子Code IM2610，而角色main IM2612/recent IM2610；Sep18角色recent IM2609与因子IM2610也存在偏差。旧缺role因子默认main且覆盖main行为保留，未修正。expected-instrument只核验固定路由，不证明角色main正确。

CFFEX InstrumentId必须大写IM2610.CFFEX；公共live_role.py已按CFFEX大写、SHFE/INE原小写形成。PF0916曾出现远端小写ID，同步公共文件后6项通过；旧日志无摘要，不能补造部署根因证据。未来不要仅同步demos/tests而遗漏bomber/framework。

## 4. 已取得验收证据（无需重复）

| 阶段 | 实际结果与边界 |
| --- | --- |
| 原始10分钟 | IM2611/20261009 09:30—09:40，10/10；首7059，末7017.8，未预热/报单0 |
| 固定另日复权 | IM2610/20260922 09:30—09:40，10根；因子Sep21 1.456591；首7760→11303.1461600，末7788.8→11345.0959808 |
| allow整日预热 | 要求240、实际239、缺13:00；status allowed_missing、coverage_complete false；EMA fast11235.005399922291/slow11235.989822638036，revision0/目标0/订单0 |
| 指标文件恢复 | 保存/恢复238状态精确一致，新末根到239，与连续递推一致，旧事件及重复新事件抑制；内存目标SHORT -1一次，订单0 |
| 实际停机再启动指标探针 | prepare237保存退出；新Python恢复237→数据库回补14:58到238且目标0→末根到239/内存目标1；两启动标识不同，连续指标精确一致，订单0 |
| PF0915离线 | history-adjustment32项OK，0.937秒，模块exit0/1.206秒，目录main-ema-acceptance-1791549411555222779 |
| PF0916正式IM假柜台 | im-runtime6项OK，0.094秒，模块exit0/1.228秒，目录main-ema-acceptance-1791550921750786768；历史预热→首新Bar Recording、缺口闭闸/日历/身份核对 |
| PF0916兼容回归 | 用户确认末段entry50项OK，1.698秒、模块exit0/2.812秒，入口全部选定离线模块通过；其余详细条数/路径未提供不补造 |

指标文件证据：tests/results/im2610-indicator-1791540272775620835-86497.json。两命令重启证据：tests/results/im2610-restart-state-1791549415478291510-86497.json。这两种都仅指标只读探针，不含生产账户/订单同代恢复能力。

专项细节：[真实IM证据](HANDOFF_HISTORY_IM_EVIDENCE_2026-10-09.md)、[239预热](HANDOFF_HISTORY_WARMUP_2026-10-09.md)、[新实例恢复](HANDOFF_HISTORY_RESUME_2026-10-09.md)、[停机重启](HANDOFF_HISTORY_RESTART_2026-10-09.md)、[正式IM入口](HANDOFF_IM_RUNTIME_2026-10-09.md)。GAP04部分链路已过，完整在线验收仍开放。

## 5. 周一实际在线Recording计划

不要直接沿用20260922历史配置或示例日历。cffex_im_20260921_22.json仅历史/离线示例。新session按本次实际TD、此前交易日和窗口内全部事件日补充权威日历/因子来源配置，不凭自然日减1或夜盘推定IM日期；不宣称支持IF正式入口，当前仅IM。

1. 柜台只读查询实际交易日、账户仓位/活动订单：

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```

2. 核验当前参考资料来源日、固定IM真实合约/条款及对应因子。准备IM日历覆盖当前及预热所需前一交易日，完整列出覆盖区间内交易日期；准备history-config的逐日factor_source_days。若本账户有CU夜盘遗留仓位，正式新Recording的全账户空仓预检会拒绝，不强行接管、不自行平仓。先明确账户隔离或人工确认处理路线。

3. 当前IM原始MD探针，合约从本次参考核验结果确定，勿使用旧报价：

```bash
CTP_SYMBOL="${IM_INSTRUMENT%%.*}" CTP_CONNECT_TIMEOUT=30 \
CTP_MARKET_TIMEOUT=60 CTP_MARKET_SAMPLE_SECONDS=10 \
python -u -m tests.run_ctp_live --stage 4
```

4. 参考及MD核验后使用同一合约启动正式Recording，变量须按本次会话实际赋值：

```bash
python -u -m demos.01_main_ema.run_live \
  --connect --mode recording --simnow-environment realtime \
  --product IM --expected-instrument "$IM_INSTRUMENT" \
  --reference-source dolphindb \
  --factor-date-basis source --factor-availability observed-on-read \
  --expected-trading-day "$IM_TD_DAY" --expected-source-day "$IM_SOURCE_DAY" \
  --trading-calendar "$IM_CALENDAR_FILE" \
  --history-config "$IM_HISTORY_CONFIG" --history-minutes 240 \
  --history-stage auto --history-missing-policy allow \
  --fast 3 --slow 5 --quantity 1 --seconds 600
```

需要盘前文件优先时另加--history-file；盘中recovery直接数据库。真实历史不足仍报告，不填行；历史不发布目标，当前新完整分钟进入Recording。验summary历史审计、计数/EMA、新目标、订单0及正常释放。来源日期/MD健康/合约错误不能通过allow绕过。

IM/CFFEX当前明确不接受mode simnow、run-forever或正式交易恢复；不能为抢先验收只删CLI限制。IM Recording通过后再实施其交易通道、统一账户/订单状态及恢复验收。夜盘不重复尝试IM/IF连接来等待分钟。

## 6. 当前聊天继续SimNow的边界

用户在整理交接后授权继续现有SimNow交易；这是模拟柜台的实际下单，不是生产实盘账户。本轮仍使用既有SHFE/INE品种（之前CU），当前DolphinDB没有CU原始历史分钟，所以fresh新会话不启用history-config/history-file/history-minutes；默认history-minutes0，EMA从实时完整Tick Bar积累3/5分钟预热后自然信号交易。角色/因子/条款参考仍来自DolphinDB，与原始历史分钟缺失是不同数据。

不复用有历史缺口的旧EMA断点，不强制接管未知仓位；若要恢复既有CU检查点，仍需要权威完整回补，否则保持闭闸。先按最新TD/仓位/活动订单只读核验，再核对CU参考来源/主力与原始MD，按实际结果给新状态文件、有限运行时长和显式SimNow授权命令。此前报告/仓位均为历史事实，不当作当前账户已空。保持一手目标及既有风控预算，无自动平仓、无生产账户授权、无本机执行。
