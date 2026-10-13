# Live策略持仓与恢复（PF2026101036，2026-10-10）

本轮补齐01的Replay已有仓位接管和单次同交易日恢复，并将04已有的账户恢复
流程移入公共CTP渠道模块。行情时间语义与持仓初始化分别选择；CTP 7×24
提供行情回放和可报单的TD，Recording仅描述执行端不报单。
01与04的策略、恢复钩子和状态文件彼此独立，没有新增跨demo源码导入。

## 文件和职责

新增：

- `bomber/framework/trader/runtime/live/recovery.py`：渠道无关的明确策略/客户端/逻辑目标绑定、归属检查、已结束订单检查、目标版本校验和会话身份检查点。
- `bomber/framework/trader/runtime/live/channels/ctp_recovery.py`：同TD恢复生命周期；原文件恢复、原生客户端对账、总仓/净仓/今昨仓/资金/活动订单检查、就绪等待、授权、失败资源释放及原文件保护。
- `demos/01_main_ema/recovery.py`：01自己的身份声明、恢复版本和EMA重新预热政策、目标与仓位核验。
- `tests/test_live_recovery.py`：21项纯Python公共恢复/01钩子回归。
- `tests/test_main_ema_recovery.py`：9项假MD/TD原生实体装配回归，Linux待执行。

修改：

- `bomber/framework/trader/runtime/live/channels/ctp.py`：移除接管与realtime的绑定，继续要求完整一手仓位/成本/今昨方向/账户核验。
- `bomber/framework/trader/live_roles.py`：声明恢复目标版本，暂停保留旧目标的续执行，直到策略发布更高版本的新目标。
- `demos/01_main_ema/run_live.py`：Replay接管、单次`--resume`校验、公共恢复装配、状态身份组件和恢复诊断。
- `demos/04_cross_section/recovery.py`：仅保留04的组合/环境/合约/版本校验钩子；公用流程由公共模块实现。
- `demos/04_cross_section/live_runtime.py`：显式持仓路由绑定，复用公共失败恢复清理。
- `tests/run_main_ema_live.py`：已有仓位与新EMA目标一致时无需重复下单即可通过持仓核验，增加Replay接管夹具能力；原新策略成交验证保持。
- `tests/run_main_ema_acceptance.py`、`tests/run_cross_section_acceptance.py`：增加`ema-recovery`组，`live-common`同时执行公共恢复回归，补全来源清单。

文档：本交接、两demo的README、`doc/architecture/STRATEGY_ARCHITECTURE.md`、公共交接台账；
完整部署摘要`../../evidence/checksums/PF2026101036_SHA256.txt`。仅文件清单与摘要，不打包，不覆盖历史摘要。

## 三种持仓初始化

| 模式 | 参数/文件 | 行为 |
| --- | --- | --- |
| fresh_flat | 新state-file，无接管/恢复标志 | 保留空仓及无活动订单预检 |
| adopt_existing | 新state-file，显式合约及预期±1 | 两轮权威查询一致后初始化策略归属及今昨仓成本账本；不恢复旧信号 |
| resume | `--resume --state-file <原文件>` | 恢复本策略保存的归属、目标版本、订单和账本，再与柜台核验；不接管未知仓位 |

接管和恢复均可选择CTP realtime或Replay，不能同时指定。恢复限定同TD、同真实
合约、已结束本地及柜台订单。账户仍由一个受控策略独占；本轮没有开放多个
策略共享同一账户并发下单。每个策略使用自己的文件；策略/客户端身份不符拒绝。

01单次恢复保留持仓和已保存目标版本，EMA从本次完整分钟重新预热。预热期间
不续执行原目标；预热后的首个新目标即使与原方向一致也发布更高版本，由
执行器根据真实持仓计算差额。相同空一手目标与已归属空一手仓位不会重复卖开。
反向目标仍由公共CTP Planner先平旧仓、确认成交，再由后续行情推进开新方向。
策略`on_fill`只观察公共PositionManager，未增加另一份私有持仓或重复记账。

01单次新文件含`live_session`身份组件，检查策略参数、交易日、MD环境/日期、
真实路由、资料来源/因子及合约条款。旧单次文件没有该组件时拒绝自动恢复；
不从柜台净仓猜测旧信号。可用明确接管流程建立新文件，保留旧文件，不修改
旧JSON或伪造检查点。已有CU持续模式的EMA检查点/历史回补路径和文件格式保持。
04旧文件继续按其已有元数据和组合约束恢复，无需迁移格式。

`resume_ready`表示执行事实已恢复/对账并允许开始新行情预热，不表示EMA已经预热。
恢复/接管验收允许新目标等于已有仓位时本次无新增成交；报告
`verification_basis=target_and_position_reconciliation`，不能作为本次新增成交证据。
空仓新启动仍保留至少一次实际成交的验收要求。停机撤挂单，报告剩余仓位，不自动平仓。

## 验证状态与Linux命令

本机Python3.12：69项纯Python测试通过（原公共21、渠道/Runtime/聚合27、恢复21），
13个新增/修改Python文件编译通过，两验收入口来源文件存在且唯一。
公共测试使用账户/Runner替身；旧目标暂停测试运行真实Runner的方法，01状态
钩子和公共恢复代码直接执行。它们不能替代原生实体端到端验收。
尝试原生`test_main_ema_recovery`与`test_cross_section_live`时，本机导入
`bomber.framework.fixes.Bar`失败，未进入测试用例。未连接柜台/数据库、未发单。

同步本清单后，在Linux的pro目录执行：

```bash
sha256sum -c doc/evidence/checksums/PF2026101036_SHA256.txt
python -u -m tests.run_cross_section_acceptance \
  --only live-common --only ema-recovery --only entry \
  --only cross-section --only continuous --only assembly
```

也可先单独执行新增原生用例：

```bash
python -u -m unittest tests.test_main_ema_recovery
```

若部署遗漏新公共模块，先补齐文件；不要注释账户/恢复校验。

## 用户当前RB空一手的接管命令

此前TD只读为20261009、净仓0/活动订单0；后续01新会话预检返回RB空一手。
本次失败会话报单0且Bar0，因此不能将该仓位来源归因于本次策略，Recording也
没有发单。用户已核对MD20261009、DolphinDB来源及因子日20261008、rb2701.SHFE。
若有可信原01状态文件，优先使用对应恢复；新代码之前的无身份组件旧文件不能
自动升级。下面显式接管当前预期空一手，启动仍会重新查询核对，仓位不符就拒绝：

```bash
CTP_MD_ADDRESS=tcp://182.254.243.31:40011 \
CTP_TD_ADDRESS=tcp://182.254.243.31:40001 \
CTP_PRODUCTION_MODE=true \
python -u -m demos.01_main_ema.run_live \
  --connect --mode simnow --enable-orders --confirm-simnow \
  --simnow-environment replay --replay-md-trading-day 20261009 \
  --product RB --expected-instrument rb2701.SHFE \
  --expected-trading-day 20261009 --expected-source-day 20261008 \
  --reference-source dolphindb --reference-database dfs://bomber_daily \
  --factor-date-basis source --factor-availability source-day-end \
  --reference-timeout 30 --reference-refresh-seconds 5 \
  --reference-max-observation-seconds 30 \
  --reference-max-refresh-duration-seconds 30 \
  --instrument-max-age-seconds 30 \
  --fast 3 --slow 5 --quantity 1 --max-notional 50000 \
  --max-session-orders 4 --seconds 600 \
  --adopt-existing-position rb2701.SHFE --expected-position -1 \
  --state-file "demos/01_main_ema/results/replay-ema-adopt-$(date +%Y%m%d-%H%M%S).json"
```

接管成功并保存后，再运行时删除两个接管参数，使用`--resume`和**同一个原文件**；
其他会话身份、合约和策略参数保持，文件路径不要重新生成时间戳。恢复不会继承
旧报单授权，命令仍要求显式SimNow授权开关。日期变化则退出当前同TD恢复范围。

## REV2：修正恢复测试的分钟夹具

用户Linux执行新增9项原生回归，8项通过，1项在构造恢复前的未完成目标时
因访问尚不存在的第二笔订单而报`IndexError`，尚未进入该用例的恢复流程。
分钟聚合在下一分钟首笔Tick到达时关闭上一分钟Bar；原夹具minute=3价格仍为
3100，minute=4降到3000只创建新桶，尚未触发反向EMA。因此将minute=3价格
提前降为3000，使minute=4关闭下跌Bar，并断言已有两笔订单且第二笔是平今。
后续恢复、重新预热及禁止续执行旧目标的断言保持。

本次只修改`tests/test_main_ema_recovery.py`及本交接记录，未修改生产代码。
本机69项公共测试再次通过，修改后的测试文件编译通过；本机缺少原生Bar实体，
修正后的9项原生回归仍需Linux复测，不能据此宣称整组通过。
历史`../../evidence/checksums/PF2026101036_SHA256.txt`保持；REV2增量摘要为
`../../evidence/checksums/PF2026101036_REV2_SHA256.txt`。同步这两份修改文件与新摘要后执行：

```bash
sha256sum -c doc/evidence/checksums/PF2026101036_REV2_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only ema-recovery
```
