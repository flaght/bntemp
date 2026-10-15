# 稀疏行情与分钟线触发排查

入口：`dolphin_tick_bar_probe.py`。只连接配置中的DolphinDB，独立订阅标准化tick和分钟流，不连接CTP、不运行策略、不下单。可与策略并行运行，使用独立订阅名，结束后取消自己的订阅。

在项目根运行（使用已装DolphinDB SDK、python-dotenv的项目环境）：

```bash
python -u scripts/integration/options/dolphin_tick_bar_probe.py --seconds 600
```

默认监控五个过期候选 `MO2610-C-8600,8500,8400,8200,8100`（每个实际代码均有完整前缀）、持仓对照 `MO2611-C-7400`、期货对照 `IM2611`。可用 `--keys` 传入完整代码的逗号分隔列表。

默认表名 `stream_ctp_cffex_std`、`stream_cffex_1min`；如实际名称不同，用 `--tick-table`、`--bar-table` 覆盖。探针读取实际schema，至少要求Code；不会猜测未知时间列或把盘口映射为成交。输出schema后应核对tick是否包含Volume和BidPrice1/BidVolume1/AskPrice1/AskVolume1；没有这些列时不会推断对应统计。

输出目录：`tests/results/option-vega-tick-bar-probe/sample-*/`。

- `observations.jsonl`：所有匹配合约的新tick和Bar、实际接收时间、完整原始字段。Bar附带当时最后观察到的tick。
- `summary.json`：各合约tick/Bar行数、累计成交量增加次数、成交量未变而盘口变化次数、最大tick接收间隔、最后tick和Bar。
- 终端每10秒输出 `DDB_PROBE_COUNTS`，每个Bar输出 `DDB_TICK_BAR`。不会逐tick刷屏。

先比较异常合约与活跃对照，然后对照原始行的tradeTime/UpdateTime/UpdateMillisec/receivedTime（实际以schema为准）与Bar的date/minTime/updateTime。

| 现象 | 排查方向 |
|---|---|
| 异常合约tick稀疏，后续tick附近才输出旧Bar | 事件时间按合约聚合，等待下一条tick触发；需服务器聚合脚本确认 |
| tick持续、成交量不变、盘口变化，但Bar缺失 | 检查成交量过滤、仅成交驱动的分钟生成逻辑 |
| tick持续且业务时间新，Bar业务时间仍落后 | 分钟聚合或其上游处理 |
| 独立探针Bar及时，策略接收晚 | 策略订阅与处理链路 |
| 所有监控合约均无tick | 优先查表名、订阅、交易时段和上游连接，不能直接判断低活跃度 |

标准化流可能已经过滤原始CTP回调；若该流也稀疏，仍需核对标准化前的数据或服务端过滤代码。空样本不自动判故障。接收间隔不是网络延迟，接收相邻也不证明触发因果。采样应选连续交易时段；跨午休的长间隔需单独解释。Ctrl+C会尽量清理并保存已采数据。

本地离线测试：`python -m unittest tests.test_dolphin_tick_bar_probe -v`。尚未连接实际DolphinDB采样。
