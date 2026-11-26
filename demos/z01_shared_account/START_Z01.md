# z01新账号08直连

从项目`pro`根目录执行。此入口复用原`demos/08_option_vega`策略实现；本目录和
`demos/z01_08_option_vega/config`只维护新账号的配置和运行资料，不需要启动账户服务。
08与z01使用相同的启动参数形式，分别维护各自的账号及检查点；10入口保持原样。

## 配置位置

- `account.json`：新账号总控，`client_id=option-vega-z01`，每交易日报单上限200。
- `../z01_08_option_vega/config/strategy.yaml`：`direct_nt_algo`维护本账号直连启动路径、策略ID和算法参数，`live_simnow`维护Live行情来源。
- `startup.yaml`：仅兼容旧命令，直连入口不再默认读取；新配置统一维护上面的`direct_nt_algo`。
- `../z01_08_option_vega/config/vega_live.json`：本账号独立维护的策略风险参数与交易时点。
- `../z01_08_option_vega/config/.env`：本入口真正加载的CTP、DolphinDB环境文件。

先在服务器创建新`.env`（已有文件时不要覆盖）：从`.env.example`填写新账号的CTP字段，
并填入原08可用的DDB行情/参考资料连接。两账号可读取同一DDB行情；CTP_ACCOUNT_ID、
CTP_PASSWORD等必须是新账号。若复制原env，也须替换CTP身份和下列flow目录：

```dotenv
CTP_TD_FLOW_PATH=/tmp/bomber-z01-option-vega-td
CTP_MD_FLOW_PATH=/tmp/bomber-z01-option-vega-md
```

直连入口读取上述08配置`.env`，不读取本目录`account.env`。
`account.env.example`是旧账户服务入口模板，不是本次直连入口的凭据来源。
新账号不用复制原账号的`state.json`、`signal.json`、`results`或计数数据库。

执行Tick使用本账号 `market_dolphindb.json` 的 `tick_table: "mCFFEX"`、
`tick_source: "tick"`；在本账号`.env`填写 `DDB_TICK_HOST`、`DDB_TICK_PORT`、
`DDB_TICK_USERNAME`、`DDB_TICK_PASSWORD`。Tick与Bar可以来自不同DolphinDB节点，
完整配置及订阅日志见[执行报价说明](../../doc/architecture/NT_ALGO_CTP_RUN_GUIDE.md#执行报价与逐次跳价)。

## 离线验证

不加载凭据、不连接柜台、不生成检查点：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
/workspace/worker/env/chs-live/bin/python -B demos/z01_08_option_vega/run_direct.py \
  --config-file demos/z01_08_option_vega/config/strategy.yaml \
  --config-id direct_nt_algo
```

确认输出为`config_checked`、策略`option-vega-z01`、`orders_enabled=false`，且路径都位于
`z01_08_option_vega`或`z01_shared_account`。
离线检查通过只证明启动配置可解析，不证明新账号凭据有效或柜台仓位已核验。

## 启动新账号

填好新账号env后：

```bash
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
/workspace/worker/env/chs-live/bin/python -u demos/z01_08_option_vega/run_direct.py \
  --config-file demos/z01_08_option_vega/config/strategy.yaml \
  --config-id direct_nt_algo \
  --connect --enable-orders --confirm-simnow --run-forever
```

默认采用`limit_reprice`：首次偏移0跳，超时重报每次前进1跳；等待10秒，最多3次实际报单（包含首次）。
这两个偏移在本账号`config/strategy.yaml`的`direct_nt_algo`中，分别使用
`algo_initial_offset_ticks: 0`和`algo_retry_step_ticks: 1`配置。
配置Tick时首次买入用ask、卖出用bid；未配置Tick时首次使用Bar close。
同日重启继续用原命令和本账号检查点；真实委托/成交恢复须通过柜台核验。
旧的z01短命令仍兼容，省略配置参数时自动选用本账号的`direct_nt_algo`。

### 恢复昨日撤单异常

z01直连实际加载原`demos/08_option_vega/strategy.py`及公共NT执行层，
不会加载复制目录中遗留的`strategy.py`。同步原08及公共执行层的最新修复后，
算法追价的`retry`、目标变化的`target_update`撤单正常处理；三次尝试耗尽的
`attempt_limit`只结束该合约本次执行，保留已成交持仓和已用报单额度，继续等待后续信号。
昨日尚未消费的算法结果按原检查点及真实归属恢复，不重发旧任务。
未知/外部撤单、拒单、在途或持仓不一致仍保留原保护，不能一律忽略CANCELED。

本账号JSON/YAML、开仓/对冲/清仓时点、state.json、signal.json及计数数据库继续使用原值。
原08的13:40单次重选维护不用于本次z01恢复。先正常停止z01原进程，
再使用上面的z01启动命令恢复；无需删除检查点或清除选约标记。

可在原环境执行以下离线回归，不连接柜台：

```bash
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
/workspace/worker/env/chs-live/bin/python -B -m unittest \
  tests.test_z01_option_account tests.test_option_nt_algo -v
```

测试使用z01自己的JSON时点及`option-vega-z01`身份，覆盖三次撤单结束、昨日结果消费、
部分成交保留、计数与回报游标保留，以及外部异常撤单仍闭闸。

原08使用完全相同的参数形式，只替换入口和配置目录：

```bash
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
/workspace/worker/env/chs-live/bin/python -u demos/08_option_vega/run_direct.py \
  --config-file demos/08_option_vega/config/strategy.yaml \
  --config-id direct_nt_algo \
  --connect --enable-orders --confirm-simnow --run-forever
```

原08启动项沿用`demos/08_option_vega/results/direct/v4/state.json`及`signal.json`，
z01沿用自己的`results/direct/state.json`及`signal.json`。实际使用其他路径的部署应继续
传入原`--account-state-file`和`--state-file`覆盖配置，不能切换到空账本。

## 状态及报告

```text
demos/z01_08_option_vega/results/direct/
  state.json                         本账号执行恢复检查点
  signal.json                        本账号08信号恢复检查点
  option-vega-z01-YYYYMMDD/           本账号08报告
  direct-ctp-runtime-YYYYMMDD/        本账号宿主报告
demos/z01_shared_account/results/
  daily_orders.sqlite3               本账号共享日计数（留给同账号其他策略共用）
```

两个真实账号分别计数。以后在z01账号下增加10时，须使用相同的z01账户总控、
同一计数数据库，以及10自己的独立检查点和报告。不能通过给同一真实账号另建计数文件绕开总控。

启动配置中的相对路径按`pro`工作目录解析；显式命令行参数优先于所选YAML启动项。
原账号继续用原目录和原命令。后续升级交易逻辑时更新原08的实现即可；本复制目录中
除`run_direct.py`以外的Python文件属于复制遗留，本直连入口不会加载它们。
