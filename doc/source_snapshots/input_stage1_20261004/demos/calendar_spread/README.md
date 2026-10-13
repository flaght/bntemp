# 单品种跨期价差回测

通过 `--product` 选择 RB、HC、I、JM 等品种，策略在**所选同一品种**的两个不同到期月份之间做价差均值回归。默认使用角色表的**次主力**（`second` 列）与**远期**（`far` 列）；也可用 `--near-role main --far-role secondary` 研究主力与次主力。`recent` 近期合约不参与选约、Bar 装配或下单。每天使用此前交易日发布的角色记录。

在 `pro` 目录运行：

```bash
export PYTHONPATH=.
export FUT_ROLE_DATA_DIR=/root/bbnt/data/role
export FUT_KLINE_DATA_DIR=/root/bbnt/data/kline
python demos/calendar_spread/run_backtest.py \
  --product RB \
  --start-day 2026-01-05 --end-day 2026-05-29 \
  --near-role secondary --far-role far \
  --quantity 1 --lookback 120 --entry-z 2 --exit-z 0.5 \
  --log-level WARNING
```

例如测试铁矿石时把 `--product RB` 改为 `--product I`；合约角色、Bar 文件与交易所元数据都会按 I 筛选。默认 `--missing-role-policy raise`，所选次主力或远期缺 Bar 即报错。可显式指定 `--missing-role-policy next-available`，在所请求角色范围内寻找当日有 Bar 的替代期限，并在控制台及 `summary.json` 记录替换；该选项依据回测数据文件的当日可用性，严格无前视研究应保持 `raise`。它不会选择 `recent`。

每根同时间戳的两合约 Bar 计算未复权价差 `第一腿收盘价 - 第二腿收盘价`，默认即`次主力 - 远期`。当前价差只与**之前** `--lookback` 根完整同步价差的均值和标准差比较：

```text
z = (当前价差 - 历史均值) / 历史标准差
z <= -entry-z：多第一腿、空第二腿，各 quantity 手
z >= +entry-z：空第一腿、多第二腿，各 quantity 手
|z| <= exit-z：两腿目标都归零
```

窗口标准差为零时 `z=0`。阈值之间保持原方向；达到相反入场阈值可反向。`--rebalance-interval` 控制持仓时按多少根完整同步 Bar 重提目标，默认 5。`--quantity` 是每条腿的**目标手数**，不是一次订单的固定数量；反手可能先平旧方向再开新方向。Bar 窗口单位是根数，不是交易日。此策略依赖价差回归假设，并非无风险套利。

两个角色中的任一真实合约变化时，旧组合的价差窗口被清空。若有旧仓，策略等待旧双腿同时间戳的 Bar 后提交双腿清仓目标；旧仓与在途订单归零后，再等待新双腿同步 Bar，才切换组合。新组合重新预热窗口，避免把不同期限组合的价差混入同一统计窗口。换约日旧合约 Bar 缺失时会打印提示：旧组合已空仓则继续；仍有旧仓或在途订单则停止并报错，避免无法平仓的持仓被忽略。改用次主力降低近期合约到期缺 Bar 的概率，但不能保证所有换约都有旧合约行情。

默认读取 `$FUT_ROLE_DATA_DIR/fut_contract_data.feather`、`$FUT_ROLE_DATA_DIR/fut_basic.feather` 和 `$FUT_KLINE_DATA_DIR`；旧变量 `ROLE_DIR`、`KLINE_DIR` 仍可作为兼容回退，也可用 `--contract-struct`、`--fut-basic`、`--bars-dir` 覆盖。角色资料目录与 Bar 目录必须不同。交易所、合约乘数、最小价格变动、挂牌与到期日从所选品种的 `fut_basic.feather` 记录读取。还可配置起始资金、手续费、保证金、风控上限与 `--log-level`；`--require-fills` 要求至少一笔模拟成交。运行结果位于 `demos/calendar_spread/results/<run-id>/`，含订单、成交、持仓、账户 CSV、JSON 摘要与可选绩效图。

两腿目标同批提交，但不保证交易所层面原子成交。回测使用基础净持仓、固定每手手续费与简化保证金规则；交易成本、合约流动性及跨期限价格关系会影响结果。
