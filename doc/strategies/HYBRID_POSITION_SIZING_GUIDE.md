# Hybrid Live 手数调整操作手册

更新日期：2026-10-31，Asia/Shanghai。适用入口：`demos.10_hybrid_transformer.run_live`。

手数调整已经接入两个独立启动参数。以后调整手数，无需编辑策略代码或运行手数迁移脚本。
保持原模型、信号文件、五分钟持仓周期和同一个状态文件，修改以下两项即可：

```text
--max-contracts 2
--entry-contracts 2
```

## 1. 两个参数各管什么

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `--max-contracts` | 5 | 所有未到期批次的绝对手数之和的上限，多空抵消不释放预算 |
| `--entry-contracts` | 1 | 每个信号的手数预算，须是1至总上限之间的整数 |

总上限是正整数，不再写死只能设置1至5手。默认值仍为5/1，已有启动命令不加新选项时继续采用原默认。
不改变下单名义金额、资金、行情时效和执行授权等现有风控。

RB当前的`threshold_signal`输出为-1、0、1，所以每批预算2对应多2手、不开新仓、空2手。
连续信号仍按`round_half_up(abs(signal) × 每批预算)`量化；例如0.3乘预算2会量化成1手。
“每批2手”不表示所有实际委托都必须恰好2手：部分成交后的剩余单、今昨仓拆分和平仓仍由执行层处理。

## 2. 开仓与到期规则

每根允许开仓的完整分钟先处理到期批次，再判断：

```text
剩余额度 = 总上限 − 尚未到期批次的绝对手数之和
本次计划手数 <= 剩余额度：建立新批次
本次计划手数 > 剩余额度：不新开，不拆成较小手数，不提前删除旧批次
```

**持仓周期5只决定到期时间，不再把2手总额度拆成(1,1,0,0,0)。**
只要在允许开仓的完整分钟有信号且额度足够，就可以建立新批次，不用等待固定预算槽位。

以总上限2、每批2、持有5个交易分钟为例：

| 时点 | 行为 |
| --- | --- |
| T | 多头信号建立一批2手，原定T+5到期 |
| T+1至T+4 | 额度已满，不再开仓；同向信号不把原批次延到T+6等时点 |
| T+5仍是多头 | 原批次到期、新批次开始，净目标仍为多2手，无需额外平开；新的到期点是T+10 |
| T+5信号为0 | 不建立新批次，平掉到期仓 |
| T+5为空头 | 目标变为空2手，执行层先平旧仓，再推进反向开仓 |

T表示批次建立的完成分钟边界，五分钟按交易日历推进，跨休市时跳过非交易分钟。
总上限4、每批2时，T+1若有信号且仍有额度，会建立独立批次：原批次T+5到期，新批次T+6到期。
中途信号不会覆盖原批次期限。

| 总上限/每批预算 | RB持续离散方向信号下的行为 |
| --- | --- |
| 1/1 | 同时最多一批1手 |
| 2/2 | 同时最多一批2手 |
| 2/1 | 同时最多两批，各1手 |
| 4/2 | 同时最多两批，各2手 |
| 5/1 | 同时最多五批，各1手 |
| 5/2 | 两批共4手后，剩余1手不足整批，不新开 |

到期与当前有效信号一起计算净目标，同向同手数更替不重复平开。
恢复冷模型没有有效新信号时，不用旧预测续持；行情缺失或故障仍按原到期/故障流程处理。
没有有效实时参考价时等待报价，原批次期限不重算，平仓不依赖模型积满260根。

## 3. 每次调整怎么操作

1. 停止正在运行的策略。
2. 在启动命令中修改`--max-contracts`和`--entry-contracts`。
3. 使用原`--state-file`重新启动。

新会话直接使用新参数。恢复旧文件时：

- 手数参数没变：按原状态恢复，批次和到期时间保留。
- 手数参数变了，本地批次、仓位、目标和在途订单均已清零：启动时自动核对管理合约柜台空仓，暂存新的手数配置，正常恢复核验通过后提交。
- 其他策略合约的柜台仓位不阻塞本策略的空仓手数调整。
- 仍有本地旧批次、持仓、待提交目标、未结束订单，或管理合约柜台仍有仓位：保留原检查点并给出提示。先使用原手数参数处理旧仓；若在柜台手工平仓，则按真实情况同步本地后再启动。

例如旧参数持有5手时，不能通过把总上限改成2就抹掉其中3手，也不能延长或截断原批次。
改变参数不会主动替你平仓。只有手数可在上述空仓条件下自动更新；模型身份、信号口径、持仓周期及日历仍执行原恢复一致性检查。

自动更新复用框架已有恢复备份和提交，订单历史、去重信息、当日报单计数、目标版本及模型身份继续保留。
无需另建状态文件，无需运行之前的手数迁移脚本。
旧`reconcile_manual_flat.py`针对手工平仓事实对齐且固定写回5/1，不作为通用调参工具使用。

## 4. 启动命令：最大2手、每批2手

工作目录：`/workspace/worker/pj/Chrono/nautilus_trader_demo/pro`。沿用已有环境变量及`.env`。

```bash
/workspace/worker/env/chs-live/bin/python -u \
  -m demos.10_hybrid_transformer.run_live \
  --connect \
  --run-forever \
  --mode simnow \
  --enable-orders \
  --confirm-simnow \
  --market-source dolphindb \
  --market-config "prod/01_hybrid_transformer/market_dolphindb_rb.json" \
  --product RB \
  --release-path /workspace/worker/pj/Chrono/genuis/mizar/records/ricso2/1release/rbb/10002 \
  --directions-file /workspace/worker/pj/Chrono/genuis/mizar/records/ricso2/rbb/rulex/113001/nxt1_ret_5h/cohort_pro.csv \
  --signal-config "prod/01_hybrid_transformer/signal_config.json" \
  --max-contracts 2 \
  --entry-contracts 2 \
  --history-minutes 260 \
  --history-config "prod/01_hybrid_transformer/history_rb.json" \
  --state-file "prod/01_hybrid_transformer/results/live/state_RB_20261029.json" \
  --report-dir "prod/01_hybrid_transformer/results/live" \
  --no-session-overrides
```

改为4/2，只把上面的总上限改为4。改为5/1，把总上限改为5、每批手数改为1。
这些参数不改变`hold_bars=5`、每分钟检查一次、模型门槛260根或行情数据口径。

## 5. 怎样核对生效

进度日志和停机报告现在同时显示`max_contracts`、`entry_contracts`。
2/2配置出现多头信号时预期显示：

```text
max_contracts=2 entry_contracts=2 ... targets={'rb2701': Decimal('2')} active_batches=1
```

到期同向更替后，目标仍为2手，但账本中的新批次期限向后推进5个交易分钟。
`targets`是策略目标；结合订单、成交与柜台持仓回报确认实际执行。

## 6. 代码与状态在哪里

| 用途 | 本地路径 | 服务器路径 |
| --- | --- | --- |
| Live入口 | `/Users/kerry/work/bomber1/pro/demos/10_hybrid_transformer/run_live.py` | `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/demos/10_hybrid_transformer/run_live.py` |
| 批次预算与到期 | `/Users/kerry/work/bomber1/pro/demos/10_hybrid_transformer/holding.py` | `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/demos/10_hybrid_transformer/holding.py` |
| 联合恢复校验 | `/Users/kerry/work/bomber1/pro/demos/10_hybrid_transformer/checkpoint.py` | `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/demos/10_hybrid_transformer/checkpoint.py` |
| 当前状态文件 | 由`--state-file`指定 | `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/prod/01_hybrid_transformer/results/live/state_RB_20261029.json` |

代码使用`demos`，现有配置和状态继续使用`prod`中的文件。状态文件名日期不控制实际交易日，持续运行沿用同一文件。
`--state-file`包含批次、目标、仓位、订单及恢复身份；报告目录的`strategy_state.json`只是组件快照，不能替代它。

研究入口没有显式声明每批手数时，保留原研究分槽量化，以免无意改动原回测口径；本手册的两个手数选项针对Live入口。
