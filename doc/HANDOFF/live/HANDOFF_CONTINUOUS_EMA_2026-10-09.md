# PF2026100910 CU EMA持续运行

**最新历史恢复需求**：见[盘前/盘中历史启动规则](../history/HANDOFF_HISTORY_STARTUP_POLICY_2026-10-09.md)。用户明确盘前文件优先、无文件DolphinDB回退，盘中恢复可用DolphinDB／MongoDB等Provider，并通过参数允许历史不足/缺失启动。本文后续JSONL必齐检查描述的是当前实现，不是新规则的最终设计；新来源选择/参数尚未实现和验收。

用户选择模式2：跨休市、日夜盘和重启续接。状态：源码及18项无网络测试已写，未运行；尚不能作为已通过的常驻交易部署。遵守本机只做源码/文档/摘要核对、Linux uv-nautilus远程执行约定。PF0908的真实EMA首笔成交和GAP01关闭证据保留，不作为本次持续模式验收证据。

## 当前行为

`--run-forever`用于realtime SimNow、CU、一手目标、DolphinDB参考资料。同一个状态文件保存EMA快慢值/计数/事件断点/目标版本，以及已有归属、订单关联和CTP账本。休市或本TD报单额度用完则等待；窗口结束撤活动订单、权威查询并保存，持仓不自动平掉。SIGINT/SIGTERM执行相同清理。

下一窗口重新登录并核对日历/MD/TD、参考来源日、固定真实合约和条款、全账户净仓/总仓、策略归属、活动订单和今昨仓成本。日历限定2026年，未知日期、换主力、未知订单、对账冲突、行情断线/停滞和数据库失败均退出闭闸，不盲目循环重启。跨TD订单关联只允许本地与柜台活动订单都为空，今昨仓按本次权威明细恢复。回调次数是累计值，最后一个回调对象不跨进程伪造。

沿用上期所[交易时段](https://www.shfe.cn/specialtopic/investor/trade/)和[2026休市安排](https://www.shfe.com.cn/services/calenderandholidays/holiday/)；夜盘TD按交易日历确定，包括周五夜盘属于下周一。参考来源日为此前交易日，仍受公共ReferenceFreshnessPolicy核验；不移除并行GAP03的来源/时效限制。

## 分钟行情前置条件

重启和重新连接会错过完整或部分分钟。缺口检查按交易窗口枚举完整分钟，休市不计；未知无成交分钟也不补零。没有足够的权威分钟文件便保持闭闸。收盘最后分钟仅在墙钟越过结束边界时取出并预热；中途停止不把残缺分钟变成完整Bar。

`--recovery-bars`消费本地JSONL，每行一个已完整结束的真实合约分钟：

```json
{"ts_event":1791524279999999999,"instrument_id":"cu2611.SHFE","adjusted_close":"163483.619245625"}
```

示例数值仅示意格式，不是可用于恢复的实际行情。ts_event使用分钟结束前最后1ns；adjusted_close必须来自真实完整分钟收盘价与该分钟适用的累计因子。禁止使用当前报价、旧目标metadata或当前因子猜造历史序列。重复、合约错误、非有限/非正价格、缺少分钟均失败；先检查全部需要的分钟，再只预热EMA，不重放目标或交易回报。补齐后第一根新完整实时Bar按正常策略产生最新目标。文件必须持续覆盖重新连接期间的缺口；该原型入口没有独立行情采集器，也不自动从数据库获取历史行情。

2026-10-09用户明确：本次EMA恢复分钟缺口应从DolphinDB获取，暂时保存问题，原因后续说明；暂停缺口接入，不继续索要本地文件路径，也不启动恢复报单。DolphinDB表名、字段、历史修订/因子口径及与实时分钟衔接方式尚未确定，后续说明后再实施，不把当前JSONL接口当作已完成GAP04。此决定限定本次恢复回补，不自行扩展到所有历史行情的数据源边界。

已取得远程证据：continuous报告`tests/results/main-ema-acceptance-1791528381842372528`，18项、0.069秒、OK，模块退出0、1.221秒。只读预检读取generation=5、cu2611.SHFE、EMA bars=5、已发布目标-1；断点2026-10-09 13:42:59+08:00，截止14:47:17.346263+08:00，缺13:43至14:46共64根完整分钟，正确闭闸。64根仅是当时截止的缺口，后续恢复需重新计算实际范围。原检查点和策略归属保留；未启动恢复或报单。

### 只读恢复预检

2026-10-09 entry新版远程50项通过（1.734秒，模块2.936秒）；continuous尚待明确新版通过汇总。新增`check_recovery.py`仅在内存恢复EMA，核验检查点校验和、旧报告迁移证据、目标版本、日历覆盖和完整分钟文件覆盖，不连接CTP、不保存状态、不发布目标。本机未执行该脚本。

先仅复测`python -u -m tests.run_main_ema_acceptance --only continuous`，预期18项全部OK。然后在远程pro目录执行：

```bash
python -u -m demos.01_main_ema.check_recovery \
  --state-file demos/01_main_ema/results/gap01-fresh-cu-1791524242765065117-86497.json \
  --resume-report demos/01_main_ema/results/simnow-1791524243708841796/summary.json
```

没有分钟文件且存在缺口时应打印缺口数/首末分钟，并以1退出，这是资料缺失预检，不能标为恢复通过。已有JSONL后，在相同命令追加`--bars /真实文件路径`；需要可重复比较时，两次均追加相同带时区的`--before`截止时刻。文件其他格式须先据字段/时间口径/因子来源进行适配，不通过修改时间戳或复制当前价格补造。检查通过仍只证明截止时刻覆盖；继续运行时必须有当日缓存更新或独立回补来源。

## 已有EMA空仓恢复

当前已知检查点：`demos/01_main_ema/results/gap01-fresh-cu-1791524242765065117-86497.json`；报告：`demos/01_main_ema/results/simnow-1791524243708841796/summary.json`。已知该EMA从空仓SELL OPEN1，最后策略/账户均-1，未归属0、活动0。这里的“空仓”指一手空头；不要重新作为新策略接管。

旧PF0908状态没有EMA组件。首次迁移提供`--resume --resume-report`；仅接受首次预热恰好slow根、指标/目标/版本/成交/状态路径完整一致的已通过报告。以后使用新组件检查点，不再用旧报告猜断点。恢复配置和当前合约必须一致；已停止之后漏掉的分钟仍需权威行情补齐。修改前自行备份原状态，不删除或直接编辑校验和/归属数据。

## 远程离线验证

按[PF2026100910清单](../../evidence/checksums/PF2026100910_SHA256.txt)同步整组源码及测试，保留本机共享入口包含的GAP03变更及其依赖。已有GAP03清单若摘要不同，先确认当前两条线完整源码，不能用旧摘要覆盖新增恢复检查。

Linux pro根目录：

```bash
sha256sum -c doc/evidence/checksums/PF2026100910_SHA256.txt
python -u -m tests.run_main_ema_acceptance \
  --only continuous --only state --only recovery --only autosave \
  --only controlled --only assembly --only entry --only historical --only joint
```

新增continuous覆盖日历边界/周末/节假日/过期、EMA预热及递推断点、同代状态/缺失组件/回滚、缺口与只预热、收盘完整桶、每日额度、跨TD订单查询、清理失败与监督器失败退出。已有组回归公共持久化/客户端/聚合/装配行为。未运行这些测试，未写入“通过”。通过后仍须远程实际重启已知仓位、至少一轮休市衔接/日夜盘TD变化、分钟接续和停机核对。

## 后续启动参数

以下是完成离线验证与历史分钟接入后的参数模板；`/实际路径/cu2611_adjusted_minutes.jsonl`必须换成已核验、持续更新的真实文件。不要现在直接照抄启动报单。

```bash
python -u -m demos.01_main_ema.run_live \
  --connect --mode simnow --simnow-environment realtime --product CU \
  --run-forever --reference-source dolphindb \
  --factor-date-basis source --factor-availability observed-on-read \
  --fast 3 --slow 5 --quantity 1 --max-notional 600000 \
  --max-session-orders 20 --enable-orders --confirm-simnow \
  --resume \
  --state-file demos/01_main_ema/results/gap01-fresh-cu-1791524242765065117-86497.json \
  --resume-report demos/01_main_ema/results/simnow-1791524243708841796/summary.json \
  --recovery-bars /实际路径/cu2611_adjusted_minutes.jsonl
```

不填写固定20261009/20261008日期，监督器在每次窗口按日历及柜台核对；报单额度按TD累计，状态中的已终结订单也计数，进程重启不重置同TD预算。20是模板显式上限，可以调低。服务窗口status=passed只代表该窗口正常结束及清理，不能替代成交验收。断线失败不会自动拉起，避免重复发送不确定订单。后台管理须待恢复实测后配置，不能用shell无限重启掩盖失败。

## PF2026100911历史Provider增量（待远程验证）

用户已授权将盘前文件优先、数据库回退、盘中数据库回补和显式允许缺失接入源码。新入口沿用持续状态/权威对账，在MD/TD就绪后历史预热并缓冲实时分钟；启用history参数后替代旧JSONL回补路径。MongoDB实际驱动尚未实现，DolphinDB真实表映射待确认。本文件此前18项通过不覆盖本次新增源码，使用PF2026100911新摘要和复测命令，详见[实施交接](../history/HANDOFF_HISTORY_PROVIDER_2026-10-09.md)。旧EMA仓位已由外部工具平掉，其检查点仅保留只读诊断，不能在线恢复。GAP04继续开放。
