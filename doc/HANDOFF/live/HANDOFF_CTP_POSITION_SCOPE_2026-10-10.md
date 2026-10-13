# CTP策略管理合约范围（PF2026101037）

2026-10-10，用户确认RB不是04策略仓位，要求忽略它并继续AU/AG/SC测试。
已只读核验TD20261009、rb2701.SHFE净仓-1、活动订单0；DolphinDB资料日
20261008，主力au2612.SHFE/ag2612.SHFE/sc2611.INE，参考核验通过。
本轮只修改本地源码、测试和文档，未连接实际柜台、未报单或平仓。

## 行为

04显式选择由本次参考资料真实合约确定的管理范围。预检阶段先登录并核对
资金/全账户活动订单；资料确定范围后核对范围内总仓为空，再安装执行与
持久化组件。最终登录后再核对一次。范围内未知仓位不能被新文件掩盖，
多空双向但净仓为零也不能通过；范围外仓位不会自动接管到04。

PositionManager仍接收并保存全账户净仓，不丢弃RB账户事实，RB在该会话中
作为未归属仓位存在；其所有权不推断为01或04。04账本、目标、风险限额和
下单只包含绑定合约。Backend额外拒绝范围外订单。保存/恢复的策略归属
校验仍拒绝其他策略、其他客户端、范围内未归属仓位和异常工作订单。
恢复总仓及今昨仓明细仅核对管理范围，范围外账户事实由本次登录重新查询。

定期对账及停机使用同一范围。报告final_gross继续包含全账户总仓；
final_managed_gross包含04管理范围总仓；external_gross明确列出范围外总仓。
04不撤销RB历史仓位，不根据RB决定组合目标。停机只撤本策略已知委托，不平仓。

仍保留全账户无活动订单检查、资金检查及账户单写锁。停止01进程后，其
持仓可以留在柜台；本轮未实现两个进程并发报单、同合约共享归属或外部委托
回报过滤。01未启用范围启动，保持原接管/恢复约束。策略源码不改，demo不互引。

## 修改文件

生产Python（6）：

- bomber/framework/trader/runtime/live/recovery.py：公共InstrumentPositionScope及显式范围内归属检查。
- bomber/framework/trader/runtime/live/channels/ctp.py：阶段化预检、绑定范围、周期/停机核对和报告。
- bomber/framework/trader/runtime/live/channels/ctp_recovery.py：同一范围恢复总仓、今昨仓及所有权。
- bomber/framework/trader/execution/builders.py：可选执行合约范围，声明须匹配配置，范围外订单拒绝。
- demos/04_cross_section/run_live.py：显式选择范围政策。
- demos/04_cross_section/live_runtime.py：由参考真实合约装配范围并传给执行端及恢复端。

测试（2）：tests/test_live_recovery.py新增11项纯Python回归；
tests/test_cross_section_live.py新增4项原生实体/假API测试，并使该demo测试夹具
遵守其管理范围。完整realtime/Replay生命周期测试覆盖范围外空一手存在，
04仍能预检、登录、生成两腿订单、成交、停机且范围外仓位保留。

文档（4）：本交接、04 README、doc/architecture/STRATEGY_ARCHITECTURE.md、
doc/HANDOFF/README.md。新增摘要位于
doc/evidence/checksums/PF2026101037_SHA256.txt，覆盖本轮12个修改/新增文件；
不改写历史部署清单，不打包。前提为用户已经同步PF1036及修正的源码基线。

## 验证

本机80项纯Python公共回归通过（原69项+新增11项）；8个修改Python文件
编译通过。真实公共生命周期、恢复方法和Backend范围检查直接执行，
账户/Runner/原生基类用替身。原生Bar实体本机无法加载，04新增4项及修改后
62项原生组需Linux执行，不能将80项纯Python结果当作实际成交证据。

同步摘要所列文件后，在远程pro执行：

```bash
sha256sum -c doc/evidence/checksums/PF2026101037_SHA256.txt
python -u -m tests.run_cross_section_acceptance \
  --only live-common --only cross-section --only entry --only ema-recovery --only continuous
```

范围涉及公共生命周期与CTP恢复，因此既核验04，也复测01的入口、恢复及
持续模式；没有新失败或改动时不重复同组。04验收入口SOURCES已经包含这些文件，
本轮无需再修改入口。

## 实际04 Replay命令

以下日期/合约来自本次只读结果，运行时仍核对，不作为永久参数。
使用独立04新状态文件，现有RB仓位保留。不添加01接管参数或使用01文件。

```bash
export CTP_MD_ADDRESS=tcp://182.254.243.31:40011
export CTP_TD_ADDRESS=tcp://182.254.243.31:40001
export CTP_PRODUCTION_MODE=true

CS_STATE_FILE="demos/04_cross_section/results/replay-cross-section-$(date +%Y%m%d-%H%M%S).json"
printf '04状态文件：%s\n' "$CS_STATE_FILE"

python -u -m demos.04_cross_section.run_live \
  --connect --mode simnow --enable-orders --confirm-simnow \
  --simnow-environment replay --replay-md-trading-day 20261009 \
  --products AU,AG,SC \
  --expected-instrument AU=au2612.SHFE \
  --expected-instrument AG=ag2612.SHFE \
  --expected-instrument SC=sc2611.INE \
  --expected-trading-day 20261009 --expected-source-day 20261008 \
  --reference-database dfs://bomber_daily \
  --factor-date-basis source --factor-availability source-day-end \
  --reference-timeout 30 --reference-refresh-seconds 5 \
  --reference-max-observation-seconds 30 \
  --reference-max-refresh-duration-seconds 30 \
  --instrument-max-age-seconds 30 \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.40 \
  --target-notional 2000000 --target-quantity-cap 1 --require-full-groups \
  --max-quantity 1 --max-notional 2000000 \
  --max-session-orders 20 --seconds 600 \
  --state-file "$CS_STATE_FILE"
```

确认报告managed_instruments只有三腿、external_gross包含范围外RB、RB未产生04
所有权或订单，再核对完整同步帧、目标、成交、各腿策略/账户仓位一致和未归属
仓位为零（管理范围内）。后续同TD恢复保留所有参数，添加--resume，使用同一
CS_STATE_FILE；不重新生成路径，不把范围外仓位写入04目标。
