# CTP策略空仓基线维护

模块：`bomber.framework.trader.runtime.live.channels.ctp_maintenance`。
源码：[ctp_maintenance.py](../../../bomber/framework/trader/runtime/live/channels/ctp_maintenance.py)。
核对日期：2026-11-08。

用于已在柜台平仓后对齐本地状态，或在后续交易日显式归档结果无法重建的旧平仓单。
维护过程只查询柜台，不发送报单、撤单，不运行模型，不调整交易参数。原有正常Live启动命令不变。

## 两种操作

| 操作 | 条件 | 订单记录 |
| --- | --- | --- |
| 普通空仓对齐 | 原检查点同一交易日，本地订单均终结，无Driver在途关联及工作量 | 完整保留历史 |
| 归档旧未决平仓单 | 柜台已进入后续交易日，指定策略只有已受理/部分成交的旧平仓单，其他订单均终结 | 将原订单、Driver关联、冻结计划归档，结果记为UNKNOWN |

两者均要求柜台管理范围内没有活动委托。账户多空总仓须等于候选策略归属仓位加原外部基线；
指定策略对齐后归属为0，其他策略和外部基线原样保留。多空分别核对，不用净仓为0代替空仓。
归档模式拒绝旧开仓单、未知发送及多个策略共同管理同一合约；其他策略尚有未终结订单也会阻止提交。
期权按整个策略的期权及期货对冲组合核验，不支持仅清理一条腿而忽略剩余风险。

归档不是补录撤单或成交。无法取得的真实平仓价格、时刻和盈亏保留为未知，不从限价或仓位差额推断。
维护不直接改成新交易日；成功后仍由正常恢复流程完成交易日身份及今昨仓结转核验。

## 公共函数与策略职责

- `align_flat_execution(payload, *, strategy_id, instruments, archive_unknown_close=False)`：
  构造执行账本候选及审计信息，调整本策略归属、工作量、目标及必要的订单关联；不写文件。
- `reconcile_flat_checkpoint(*, state_file, transport, prepare, signal_files=(), archive_unknown_close=False)`：
  持有账户及文件写入锁，登录查询，两次核验柜台，调用业务准备函数，完整备份并CAS原子提交。
- `reconcile_shared_flat(config, *, strategy_id, transport, align_signal, archive_unknown_close=False)`：
  共享账户流程，复用上面两项能力。`align_signal`接收原业务状态、新账户目标版本、路由和原目标来源，
  以及关键字`signal_revision`；返回`(新业务状态, 附加目标metadata)`。

框架不导入demo或识别策略名称。Hybrid的`flat_state.py`负责清理批次和待提交目标；
期权的`reconcile_flat.py`负责归零期权/期货目标，保留`sent_days`、`hedge_sent`、
`flatten_requested`及业务计数，避免把已处理时点重新当成未执行。

共享维护只提交账户文件，在目标metadata的`checkpoint_alignment`保存同代业务副本。
恢复时若策略文件落后，`SharedStrategySession`通过各策略自己的恢复函数加载副本；
信号文件稍后按正常保存流程接续。副本版本不符会拒绝恢复。期权旧业务文件若落后且缺少同代副本，
维护也拒绝猜测选约/对冲标记。维护期间锁定所有共享策略文件，不覆盖其他策略文件。

## Hybrid入口

原入口继续可用：

```bash
python -u -m demos.10_hybrid_transformer.reconcile_manual_flat \
  --state-file /实际路径/原独立账户检查点.json --confirm-manual-flat
```

现在保留原最大手数及每批手数，不再默认迁移到5/1。业务时间位置及回报计数也保留。
手数调整使用原有显式配置及空仓参数迁移流程；`run_live --confirm-manual-flat`内部先对齐、再单独执行手数校验转换。

共享旧入口默认归档模式，保持现有命令兼容：

```bash
python -u -m demos.10_hybrid_transformer.reconcile_shared_flat \
  --config demos/10_hybrid_transformer/config/shared_rb_p.json \
  --strategy-id hybrid-transformer-live --confirm-broker-flat
```

追加`--manual-flat`切换为普通同日对齐，要求本地旧订单已经终结。

## 期权入口

独立账户普通对齐：

```bash
python -u -m demos.08_option_vega.reconcile_flat \
  --state-file /实际路径/原独立Live检查点.json --confirm-manual-flat
```

共享账户普通对齐：

```bash
python -u -m demos.08_option_vega.reconcile_flat \
  --config /实际路径/原共享配置.json \
  --strategy-id 原option_vega策略ID --confirm-manual-flat
```

两种布局如需显式归档后续交易日的旧未决平仓单，追加`--archive-unknown-close`。
入口不加载期权参考或研究模型，只读取原检查点和共享配置。Model/Recording状态不用于本维护操作。

## 操作顺序与文件

1. 正常停止占用同一账户/检查点的进程，确认需要对齐的策略确已空仓。
2. 使用原状态文件/共享配置执行对应维护命令，不通过换文件或删文件绕过检查。
3. 成功后查看备份和审计，再按原Live命令恢复。

提交前在原账户文件旁写入`.before-flat-alignment-*.bak`及`.flat-alignment-*.json`。
旧版工具生成的其他名称备份保留。核验、查询或业务转换失败时原文件不变；CAS阻止覆盖已被更新的版本。
工具不解决网络断线，不自动处理实际仍持有仓位的策略。
