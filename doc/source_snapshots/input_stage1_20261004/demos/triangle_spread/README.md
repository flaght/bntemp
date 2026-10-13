# 黑色系三腿相对价值回测

这个策略对应“IM 多、IH 和 IC 空；或者反向”的**三腿方向结构**，在黑色系可先用 `RB` 为主腿、`HC,I` 为两条对冲腿进行研究。RB、HC 都是钢材，I 是铁矿石；三者有产业关联，但价格关系可能长期变化。这是统计相对价值交易，**不是无风险三角套利**，三腿下单也不保证同时成交。品种和权重均可配置。

在 `pro` 目录运行：

```bash
export PYTHONPATH=.
export FUT_ROLE_DATA_DIR=/root/bbnt/data/role
export FUT_KLINE_DATA_DIR=/root/bbnt/data/kline
python demos/triangle_spread/run_backtest.py \
  --anchor RB --hedges HC,I --hedge-weights 0.5,0.5 \
  --start-day 2026-01-05 --end-day 2026-05-29 \
  --lookback 120 --entry-z 2 --exit-z 0.5 \
  --target-notional 300000 --rebalance-interval 5 \
  --log-level WARNING
```

## 命令参数

| 参数 | 作用 |
| --- | --- |
| `--anchor RB` | 主腿品种 RB。 |
| `--hedges HC,I` | 两个对冲品种 HC、I；三个品种都按交易日选择主力合约。 |
| `--hedge-weights 0.5,0.5` | HC、I 各占对冲侧目标名义金额的一半，两个权重之和必须为 1。 |
| `--start-day 2026-01-05`、`--end-day 2026-05-29` | 请求的回测日期范围。 |
| `--lookback 120` | 用当前 Bar **之前 120 根三品种同步 Bar**的价差计算均值和标准差；不是 120 个交易日。 |
| `--entry-z 2` | 价差偏离历史均值达到 2 个标准差时入场。 |
| `--exit-z 0.5` | 已持仓时，偏离缩小到 0.5 个标准差以内则平仓。 |
| `--target-notional 300000` | RB 腿目标名义金额约 30 万元；按示例权重，HC、I 各约 15 万元。 |
| `--rebalance-interval 5` | 持仓期间每 5 根同步 Bar 重新计算目标手数；方向变化、平仓或换月立即更新。 |
| `--log-level WARNING` | 只显示 WARNING、ERROR 级别的框架日志。 |

这些参数设置的是**目标仓位**：实际手数要除以真实合约价格和乘数后向下取整；实际订单还取决于当前持仓。例如由多 RB、空 HC 和 I 反转时，会涉及平旧方向和开新方向的订单。

`--anchor` 是主腿；`--hedges` 是两个不同的对冲品种。三者均使用上一交易日角色表确定的当日**主力真实合约**。信号价为真实合约收盘价乘因果复权因子；订单仍在真实合约的未复权 Bar 上模拟成交。换月时旧合约目标清零，新主力按当前方向重新设目标，换月日需要旧、新合约 Bar。

首个完整同步 Bar 的三品种复权价作为归一化基准。每根完整同步 Bar 计算

```text
spread = log(RB / RB基准) - 0.5 × log(HC / HC基准) - 0.5 × log(I / I基准)
z = (当前spread - 前120根spread均值) / 前120根spread标准差
```

计算当前 `z` 时窗口只含**之前**的同步 Bar。`z <= -2` 表示主腿相对偏低，目标为**多 RB、空 HC、空 I**；`z >= +2` 表示主腿相对偏高，目标反向。持仓后 `|z| <= 0.5` 则三腿目标归零。达到入场阈值时可直接反向；窗口标准差为零时 `z=0`。`--lookback`、`--entry-z`、`--exit-z` 控制这些阈值，窗口单位是同步 Bar 根数，不是交易日。

`--target-notional 300000` 表示主腿目标名义金额 30 万元；权重 `0.5,0.5` 表示两条对冲腿各约 15 万元，组合总名义金额约 60 万元。实际手数按 `目标金额 / (真实收盘价 × 合约乘数)` 向下取整；若任一腿不足一手，回测报错，避免悄悄变成两腿。权重必须为正且合计为 1。`--rebalance-interval` 控制持仓期间按多少根同步 Bar 重新配手数；方向变化、平仓或换月会立即更新目标。此配比只按名义金额，**不保证价格敏感度或产业因子中性**。

默认从 `$FUT_ROLE_DATA_DIR/fut_contract_data.feather`、`$FUT_ROLE_DATA_DIR/fut_basic.feather`、`$FUT_KLINE_DATA_DIR` 读取数据；旧变量 `ROLE_DIR`、`KLINE_DIR` 仍可作为兼容回退。`--contract-struct`、`--fut-basic`、`--bars-dir` 可覆盖路径；角色资料目录与 Bar 目录必须不同。交易所、价格最小变动和乘数均从元数据读取。起始资金、每手手续费、保证金、风控上限和日志级别也可传参。回测会检查请求区间覆盖与所需 Bar；结果保存在 `demos/triangle_spread/results/<run-id>/`，包括订单、成交、持仓、账户 CSV、JSON 摘要和可选绩效图。`--no-tearsheet` 跳过图表，`--require-fills` 要求至少一笔成交。
