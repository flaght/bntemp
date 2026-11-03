# Hybrid：RB/P共用账户，同进程与多进程切换

入口：`python -m demos.10_hybrid_transformer.run_shared`。两种布局复用既有共享执行服务；模型、信号和五分钟批次使用原实现，不增加第二套策略算法。

## 配置

示例位于 `demos/10_hybrid_transformer/shared_rb_p.example.json`。相对路径以配置文件所在目录为基准；读取后固定为绝对路径，子进程沿用同一配置。

- 账户：`client_id=hybrid-transformer-live`，预算4手、200万元名义金额，每个交易日最多100笔。预算需要按实际需求核对。
- RB：策略ID保留 `hybrid-transformer-live`，离散信号，每批上限2手、总上限2手，DolphinDB行情及260根历史预热。沿用原 `prod/01_hybrid_transformer` 配置，模型指纹不会因为增加P而改写。
- P：策略ID `hybrid-p-live`，`pc/10001`模型、159001方向文件、1101连续信号，每批上限2手、总上限2手。示例使用DolphinDB DCE行情及260根历史预热。P的信号、行情和历史分别由`signal_p.json`、`market_dolphindb_p.json`、`history_p.json`声明。
- 两种布局均固定 `execution_mode=independent`，不会因为变成同进程而改用跨策略净额抵消。
- 每个批次独立持有5个交易分钟；连续信号按每批预算量化为整数手，到期与同一时点的新信号合并为净目标。

## 每个品种的独立配置

各策略的`argv`分别声明`--release-path`、`--directions-file`、`--signal-config`、`--market-config`、`--history-config`与`--report-dir`，不会自动沿用另一个品种。`state_file`写在策略条目上，不放进argv；账户委托、成交和仓位归属共用顶层`account_state_file`。

| 字段 | RB | P（当前示例） |
| --- | --- | --- |
| 模型发布 | `1release/rbb/10002` | `1release/pc/10001` |
| 特征方向 | `rbb/rulex/113001/nxt1_ret_5h/cohort_pro.csv` | `pc/rulex/159001/nxt1_ret_5h/cohort_pro.csv` |
| 信号配置 | 原`prod/01_hybrid_transformer/signal_config.json` | `demos/10_hybrid_transformer/signal_p.json` |
| 行情配置 | 原`market_dolphindb_rb.json` | `market_dolphindb_p.json`，DCE来源 |
| 历史配置 | 原`history_rb.json`，读取260根 | `history_p.json`，读取260根 |
| 策略状态 | `shared_RB_signal.json` | `shared_P_signal.json` |
| 报告父目录 | `prod/01_hybrid_transformer/results/live` | `prod/01_hybrid_transformer/results/live/P` |

P按已确认的RB同源规则配置：实时流表`market_bar_state_v2_dce`，历史库`dfs://ctp`、表`bar_1min_dce`，交易所`DCE`。实时流保持`volume_mode=per-bar`，历史表保持`volume_mode=cumulative`，成交额/成交量由历史读取层差分；`open_interest`为持仓量快照，不作差分。字段映射、分钟开始标签、交易日date与timestamp类型的minTime均沿用RB。

两个品种均先尝试加载260根历史，不足的部分继续等待完整实时分钟。市场连接和历史读取使用`DDB_MARKET_*`，主力/条款等参考资料继续使用`DDB_REFERENCE_*`。RB已有检查点对应的信号文件保持原样，不直接追加P条目，以免改变模型/信号指纹。

## 同进程启动

在服务器 `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro` 目录、已配置模型依赖及.env的终端执行：

```bash
/workspace/worker/env/chs-live/bin/python -u \
  -m demos.10_hybrid_transformer.run_shared \
  --config demos/10_hybrid_transformer/shared_rb_p.example.json \
  --process-layout same-process \
  --run-forever \
  --connect --enable-orders --confirm-simnow
```

一个进程运行两个模型/Runner和唯一CTP执行服务。

## 多进程启动

同一配置，只更换布局：

```bash
/workspace/worker/env/chs-live/bin/python -u \
  -m demos.10_hybrid_transformer.run_shared \
  --config demos/10_hybrid_transformer/shared_rb_p.example.json \
  --process-layout separate-processes \
  --run-forever \
  --connect --enable-orders --confirm-simnow
```

调度入口启动一个账户server和两个独立worker，只有server登录TD；server不加载Transformer模型，worker各自计算并通过Unix IPC提交目标。故障不自动重复报单或重启；停机先排空worker回报，再停唯一账户服务。

## 恢复与切换

1. 首次接入时，先在原RB终端Ctrl+C，等待正常撤单、保存完成。示例账户文件沿用原 `state_RB_20261029.json`，不是新建空账本。两个策略信号文件分别保存为 `shared_RB_signal.json`、`shared_P_signal.json`。
2. 原RB账本的客户端ID、策略ID、模型/信号配置、手数、日历、合约必须与示例声明一致。启动时只读核验柜台，原订单历史、归属和批次期限保留；核验通过后由现有持久化机制备份原文件并提交共享格式。参数不符、未知活动单或仓位不一致时停止并保留原文件，不通过清空状态处理。
完成首次转换后，该账户文件由`run_shared`管理；原`run_live`入口不会把共享格式解释成单策略账本。

3. 布局切换时正常停止当前共享会话，再修改 `--process-layout` 启动。保持同一配置及所有状态路径；布局不进入策略身份，不重新计算原批次五分钟期限。两种布局不能同时占用同一账户。
4. `--run-forever`由公共交易日历安排休市等待、午休和下一交易日。配置中的初始trading_day用于声明和离线校验，各运行窗口会自动更新实际TD和前一来源日。主力合约或模型/风险参数变化仍按原严格检查处理。
5. 首版联合调度要求各策略的交易日历、时段和例外规则一致；RB/P分别读取自己的规则，比较不一致就停止，不借用其他品种时段。

## 报告

按各自`--report-dir`归档：示例为`results/live/hybrid-simnow-RB-20261103/`及`results/live/P/hybrid-simnow-P-20261103/`。未声明策略报告目录时沿用顶层`report_dir`；顶层仍保存账户/调度报告。各策略的夜盘、日盘、同日重启继续追加信号、批次与执行事件。进程布局切换不另分策略目录；summary是最新会话，sessions.jsonl保留各次摘要。

旧纳秒目录不会自动迁移。交易恢复仍使用账户文件及各策略信号文件，不使用报告快照。

## 离线检查

去掉连接和报单授权参数即可只校验声明式配置：

```bash
/workspace/worker/env/chs-live/bin/python -u \
  -m demos.10_hybrid_transformer.run_shared \
  --config demos/10_hybrid_transformer/shared_rb_p.example.json \
  --process-layout separate-processes
```

结构检查不加载模型，也不证明服务器上的模型文件、数据库或真实柜台可用。专项测试使用真实策略、批次、共享服务、持久化和Unix socket，模型与柜台为离线替身；真实服务器组合仍需运行验证。
