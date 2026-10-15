# 08实际跨日恢复与持续仓位管理

2026-10-14，状态：代码及离线验证通过；真实服务器跨日待验。用户保留原MO空1手，不安排固定买回或开仓测试。

## 行为

- 默认每日运行：可以开盘前/午休启动，按公共calendars等待、运行至当天最后窗口结束。
- `--run-forever`：每窗口正常停机、休市等待，下一窗口重建输入并从同一JSON最新代恢复；不要求人工修改交易日或搬运JSON。
- 08继续使用DolphinDB已完成MO/IM一分钟行情，指数000852继续通过声明式`index.minute`特征查询/投影；执行仍是CTP TD。
- 进入每个窗口核验窗口TD与只读柜台TD一致。公共恢复检查归属/终结订单/未知活动单/权威今昨桶与成本，成功才提交新代并授权；没有把账户净仓猜成策略仓位。
- 首份新鲜分钟按自己的恢复仓位继续估值、对冲及正常退出；当天原配置时点正常选约，同日重启保持去重。没有新增固定清仓日或强制测试目标。
- 窗口结束，串行关闭新目标，撤单、回报排空、对账、保存并断开。已成交仓位保留。收盘后迟到分钟不会触发新的交易。
- 当日订单额度从原OMS历史扣除，午休/进程重启不重置。额度耗尽、行情/参考/归属冲突和停机失败明确停止；不盲目自动重启。

## 模块职责

`bomber/framework/calendars`负责交易日期、时段、日期例外；`dataprep/session_inputs.py`复用Provider读取标准规则。新增`trader/runtime/live/continuous.py`只负责运行窗口与等待，无demo、行情源或柜台分支。

现有账户JSON保存归属、执行账本、OMS、成交去重；信号JSON保存08选约及目标。`SharedAccountExecutionService`/`PositionManager`仍是公共仓位管理，JSON只是持久化，不自行运行决策。08快捷入口`options/shared.py`和多策略入口`shared_account/continuous.py`负责装配，不重复公共仓位算法。

## 两种部署布局

同进程：一个共享宿主装配多个08 Runner，执行模式沿用配置中的netted/independent。netted内部抵消有独立审计，不能当作真实柜台成交。

独立进程：配置必须independent，由父调度器启动唯一server及每个worker；只有server连接TD、保存账户JSON。worker仅持有自己的能力和信号文件。新窗口使用新能力文件，账户和信号文件复用。子进程使用独立进程组，父进程处理Ctrl+C/SIGTERM，先正常停止worker再停止server；worker停机失败保留server供诊断，不强杀、不进入下一窗口。

这不是让每个策略启动一个TD写入进程，也不将一份原仓复制给多个策略。08原生文件首次导入仍使用单策略快捷入口；已有多策略共享检查点维持原策略集合。

## 启动命令

先同步本补充包，旧交付的校验清单对应旧版本，不用于校验本次新版本：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_CONTINUOUS_POSITION_20261014.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_CONTINUOUS_POSITION_20261014_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only option-shared
```

原仓常驻管理（删除`--run-forever`即每日至收盘）：

```bash
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true
python -u -m scripts.integration.options.shared \
  --connect --enable-orders --confirm-simnow --run-forever \
  --calendar-id china.cffex --hours-code IM \
  --trading-hours-database dfs://bomber_static \
  --trading-hours-table fut_trading_session --no-session-overrides \
  --state-file /workspace/worker/pj/Chrono/nautilus_trader_demo/pro_new/tests/results/gap08-realtime-simnow/state-1791869411336135873.json
```

2026-10-15启动修正：服务器时段来自`dfs://bomber_static/fut_trading_session`，须显式传入上面两项参数；省略会错误地沿用日表参考库及默认`trading_hours`表。MO/IM基础资料继续读取原日表库，不要把`DDB_REFERENCE_DATABASE`全局改成静态库。`--hours-code IM`显式声明MO的时段继承。`--no-session-overrides`须对应确认没有日期例外的资料范围；若有例外表，则替换成`--session-overrides-table 实际表名`。不因表缺失猜无例外。凭据继续读取项目.env，不打到日志。

已有多08共享配置采用：

```bash
python -u -m scripts.integration.shared_account.continuous \
  --config /absolute/path/shared-session.json \
  --process-layout same-process \
  --connect --enable-orders --confirm-simnow --run-forever \
  --calendar-id china.cffex --hours-code IM \
  --trading-hours-database dfs://bomber_static \
  --trading-hours-table fut_trading_session --no-session-overrides
```

不同进程将`--process-layout`改为`separate-processes`，配置须为independent；不要同时手动启动另一套server/worker。两种布局均支持省略`--run-forever`。配置内策略argv不能固定`--valuation-day`，日期由窗口与TD确定；账户、策略集合、来源和参数保持原恢复身份。不能隐式增加策略或更改执行模式。

## 验证与边界

本机：原08回归81项；公共调度10项；多进程调度/预算/停止信号9项；08共享及跨日19项；既有迁移12项；只读预检9项；CLI3项。上述62项专项合计通过（与81项分别计算）。共享账户65项、Unix IPC/应用11项（包含真实本机三进程socket及替身柜台）、公共Live116项通过。部署校验另见本次JSON。

关键跨日测试实际使用公共账户服务、持仓管理、恢复及原子JSON保存，覆盖首日早盘→午休后→周末后下一交易日，归属空1手保持、每窗口消费新鲜Bar且不发固定测试单。数据库、柜台、行情和不可用SDK实体为离线夹具。独立进程新调度的子进程顺序使用进程替身验证；已有IPC回归使用实际子进程。没有把这些结果登记为服务器真实成交或真实跨日通过。

本批自动跨日支持相同日历的08/MO/IM实时组合。01/03/04仍可用既有共享入口，但历史指标预热及其他交易所独立昨仓不在本次范围。未知漏成交、活动单、合约到期换月或外部仓位冲突仍需严格处理。日历快照含声明覆盖范围，当前原生规则至2026-12-31；覆盖不足停止。运行时不偷偷更新时段规则；变更资料后重启以加载新快照。SIGKILL/断电不保证完成正常停机，恢复仍须权威核验。

服务器只需回传本次运行各窗口报告（rollover、final_gross、逐策略归属、实际frames与entry_audit、停机结果）及下一交易日自动恢复结果；不要求为了验收制造新成交。
