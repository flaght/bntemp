# 策略独立执行与多账户装配（PF2026101101）

用户确认：策略不强制绑定真实账户仓位；同一策略可服务第三方端多个账户，
第三方提供订单及成交回报。策略模板既有目标和回报端口保持，政策由入口装配。
本次未连接MD/TD/DolphinDB或第三方端，未发单、撤单或处理柜台仓位。

## 程序边界

```text
trader/execution/
  policy.py       账户仓位strict/observe/off，策略来源fills/model
  model.py        参考价执行模型，产生带model来源的标准回报
  fanout.py       唯一策略账本来源，按账户倍率分发，隔离跟随回报
  account.py      一物理账户一执行连接，多策略目标净额及归属
  builders.py     装配执行端和分别持久化，构造不联网
  live/client.py  归属成交、标准状态机与可选账户观测
trader/runtime/live/
  local.py               独立执行生命周期，只锁自己的状态文件
  channels/ctp_market.py 独立MD配置，不创建TD或要求TD凭据
  channels/ctp.py        原生交易生命周期，可选账户仓位政策
  channels/ctp_recovery.py 原生成交账本恢复，strict才比较物理仓位
```

01/04算法未修改，没有跨demo导入。StrategyTemplate新增启动前恢复目标版本的
公共方法，Local Runtime不操作策略私有版本字段。run_live只是装配；账户路由
可使用现有ExecutionClientPort替换为原生、BN或第三方端。

strict保持原生行为。observe将柜台快照另存account_observation，不覆盖策略
执行基准；观测查询失败记录account_observation_error。off不查询物理净仓、
总仓或今昨仓，归属成交、去重、在途量和恢复仍生效。原生CTP资金、活动订单
核验及物理账户锁保留；第三方无资金查询能力可使用BackendExecutionClient，
不能伪造ControlledLiveExecutionClient资金就绪。

PositionManager历史account_positions字段保留供Planner使用；独立模式为本
客户端归属成交基准。报告execution_positions与account_positions分开，off的
物理账户值为None；策略account_position在off不可用，position统一查询归属。

## 仓位来源、分发及恢复

fills可显式选择一个执行端作为标准策略账本，只有该端回调进入策略；model
使用ReferencePriceExecutionBackend按当前参考价同步模拟市价成交，标准
ACCEPTED/FILLED回报带稳定模型订单/成交ID及execution_origin=model。不把报单
受理当作成交，不模拟限价排队、费用、滑点或CTP今昨仓，不构成柜台成交证据。

TargetFanoutClient.position_client是唯一策略仓位与回调来源；每个跟随账户的
targets、logical_targets及position_attribution一致按倍率缩放。各端独立规划、
风控及记账。跟随成交不相加成策略仓位，不能共用主策略PositionManager，不能
重复分发至同账户。登录/发送失败隔离为blocked；pending恢复后闭闸，不盲目重发。

attach_execution_persistence安装策略检查点、分发写前/写后日志及回报保存。
跟随账户须使用自己的状态管理器，不把其完整快照塞进策略文件。模型与原生、
strict与off文件不能混用；默认strict旧文件组件格式保留，非默认政策额外保存
execution_policy。模型恢复保留仓位，重新预热后发布新目标。

AccountExecutionService共享一账户客户端，内部净额多个策略完整贡献，保留
归属、版本及旧合约归零目标；停止一租约不会关闭其他策略的连接。服务检查点
保存贡献、组合版本和pending；完整账户PositionManager、订单、Driver及CTP
账本仍由现有RuntimeStateManager保存，账户服务宿主持有物理锁和授权。

装配示例，account_a/account_b为调用方已配置和授权的执行组件：

```python
from bomber.framework.trader.execution.account import AccountExecutionService
from bomber.framework.trader.execution.fanout import AccountExecutionRoute

service_a = AccountExecutionService(account_a.client, checkpoint=account_a_manager.save)
service_b = AccountExecutionService(account_b.client, checkpoint=account_b_manager.save)
# 在账户管理器state_components中注册service_a/service_b，保存贡献与pending。
routes = (AccountExecutionRoute(service_a, 1), AccountExecutionRoute(service_b, 2))
runtime = demo_entry.build_runtime(model_args, account_routes=routes)
```

第二个策略装配相同service_a对象即可共享连接。第三方Adapter须用稳定订单/
成交ID、strategy_id及allocation_token关联ExecutionReport。报单受理期间不会
更新fills来源仓位。策略入口不自动授权跟随账户，厂商网络实现由对应Adapter提供。

本次提供进程内账户服务端口，没有虚构厂商API或跨进程RPC。两个独立
--mode simnow进程仍不能同时直连同一账户；跨进程交易应通过同一个账户服务。
01/04的--mode model可各自连接MD并锁自己的文件，同时运行。

分发端与账户服务分别使用发送串行锁和状态快照锁；网络发送、客户端启动/
停止及检查点保存期间不持有状态锁，异步回报能取得一致的pending快照。

## 模型入口

日期须运行前重新核对。以下沿用用户最近核验的MD20261009、资料日20261008；
此模式expected-trading-day为参考资料交易日，不冒充TD登录结果。环境凭据仍
从.env读取CTP_ACCOUNT_ID/CTP_PASSWORD；仅需CTP_MD_ADDRESS，不需TD地址。

```bash
export CTP_MD_ADDRESS=tcp://182.254.243.31:40011
export CTP_PRODUCTION_MODE=true
EMA_MODEL_STATE="demos/01_main_ema/results/model-ema-$(date +%Y%m%d-%H%M%S).json"
python -u -m demos.01_main_ema.run_live \
  --connect --mode model --strategy-position-source model --account-reconciliation off \
  --simnow-environment replay --replay-md-trading-day 20261009 \
  --product RB --expected-instrument rb2701.SHFE \
  --expected-trading-day 20261009 --expected-source-day 20261008 \
  --reference-source dolphindb --reference-database dfs://bomber_daily \
  --factor-date-basis source --factor-availability source-day-end \
  --instrument-max-age-seconds 30 --fast 3 --slow 5 --quantity 1 \
  --max-notional 50000 --seconds 600 --state-file "$EMA_MODEL_STATE"
```

04可在另一终端运行：

```bash
export CTP_MD_ADDRESS=tcp://182.254.243.31:40011
export CTP_PRODUCTION_MODE=true
CS_MODEL_STATE="demos/04_cross_section/results/model-cross-section-$(date +%Y%m%d-%H%M%S).json"
python -u -m demos.04_cross_section.run_live \
  --connect --mode model --strategy-position-source model --account-reconciliation off \
  --simnow-environment replay --replay-md-trading-day 20261009 \
  --products AU,AG,SC \
  --expected-instrument AU=au2612.SHFE --expected-instrument AG=ag2612.SHFE \
  --expected-instrument SC=sc2611.INE \
  --expected-trading-day 20261009 --expected-source-day 20261008 \
  --reference-database dfs://bomber_daily \
  --factor-date-basis source --factor-availability source-day-end \
  --instrument-max-age-seconds 30 \
  --lookback 3 --rebalance-interval 1 --group-fraction 0.40 \
  --target-notional 2000000 --target-quantity-cap 1 --require-full-groups \
  --max-quantity 1 --max-notional 2000000 --seconds 600 --state-file "$CS_MODEL_STATE"
```

model不接受--enable-orders/--confirm-simnow/--adopt-existing-position。没有
account_routes时这些命令不会向物理账户报单。恢复使用原文件加--resume，不能
重新生成文件名。Recording原行为保留，不更新仓位。原生报单如需仓位脱钩，
在原命令追加--account-reconciliation off --strategy-position-source fills，物理锁保留。

## 文件与验证

完整文件见PF2026101101_SHA256.txt：公共模块、两入口、模板/Runner语义、测试
及两集中验收入口；额外包含已验收35行04/recovery.py作为同步基线依赖。
架构、两README、交接索引、本交接也在新摘要内。历史摘要不改写，不打包。

本机集中live-common 108项通过，exit_code=0（原81+CTP脱钩2+执行组合25）。
实际执行公共PositionManager、归属、Planner、Risk、状态机和执行客户端，
只用SDK标识替身与假柜台。编译及源码清单检查通过。新增原生01入口预计55项、
04预计65项待Linux复测；本机Bar无法加载，不将公共测试当作原生成交证明。
用户此前04 62项、01入口53项通过属于旧基线证据，不能代替本次变更后验收。

```bash
sha256sum -c doc/evidence/checksums/PF2026101101_SHA256.txt
python -u -m tests.run_cross_section_acceptance \
  --only live-common --only cross-section --only entry --only ema-recovery --only continuous
```

公共失败组可用--only execution-composition。真实MD、原生订单与第三方多账户
成交仍需各自授权和状态文件独立验收。

## 后续验收更新（2026-10-11）

用户已提供01/04真实CTP Replay行情驱动模型运行的passed报告，以及04同文件
恢复的passed报告。04从版本6接续到7，第4帧重新预热后才发新目标，原归属
仓位保留并按新信号平仓/反向，最后AU=0、AG=-1、SC=+1写回原文件。
公共回归在MD停机报告修正后为本机110项通过；用户还报告选定离线组全部通过，
归属18项通过。前文108项和预计原生55/65项属于实施时记录，不补造未贴逐组数。

01模型恢复、01/04同时运行和第三方多账户真实成交仍待验收，原生柜台验收
按各自历史证据判断。详见[最新模型验收](HANDOFF_MODEL_REPLAY_2026-10-11.md)
和[结构化证据](../../evidence/live/MODEL_REPLAY_20261011.json)。本次只改文档，
代码保持REV2，当前完整校验清单为PF2026101101_REV3_SHA256.txt，旧清单保留。
