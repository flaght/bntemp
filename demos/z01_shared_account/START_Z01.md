# z01新账号08直连

从项目`pro`根目录执行。此入口复用原`demos/08_option_vega`策略实现；本目录和
`demos/z01_08_option_vega/config`只维护新账号的配置和运行资料，不需要启动账户服务。
原08/10的入口、配置、持仓和检查点不变。

## 配置位置

- `account.json`：新账号总控，`client_id=option-vega-z01`，每交易日报单上限200。
- `startup.yaml`：新账号08直连启动路径、策略ID和算法参数。
- `../z01_08_option_vega/config/strategy.yaml`：新账号Live行情来源等配置。
- `../z01_08_option_vega/config/vega_live.json`：新账号策略风险参数，初值与原08一致。
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

## 离线验证

不加载凭据、不连接柜台、不生成检查点：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
/workspace/worker/env/chs-live/bin/python -B demos/z01_08_option_vega/run_direct.py
```

确认输出为`config_checked`、策略`option-vega-z01`、`orders_enabled=false`，且路径都位于
`z01_08_option_vega`或`z01_shared_account`。
离线检查通过只证明启动配置可解析，不证明新账号凭据有效或柜台仓位已核验。

## 启动新账号

填好新账号env后：

```bash
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
/workspace/worker/env/chs-live/bin/python -u demos/z01_08_option_vega/run_direct.py \
  --connect --enable-orders --confirm-simnow --run-forever
```

默认采用`limit_reprice`：首次偏移0跳，超时重报每次前进1跳；等待10秒，最多3次实际报单（包含首次）。
这两个偏移分别在`startup.yaml`使用`algo_initial_offset_ticks: 0`和`algo_retry_step_ticks: 1`配置。
配置Tick时首次买入用ask、卖出用bid；未配置Tick时首次使用Bar close。
同日重启继续用原命令和本账号检查点；真实委托/成交恢复须通过柜台核验。

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

启动配置中的相对路径按`pro`工作目录解析；显式命令行参数优先于`startup.yaml`。
原账号继续用原目录和原命令。后续升级交易逻辑时更新原08的实现即可；本复制目录中
除`run_direct.py`以外的Python文件属于复制遗留，本直连入口不会加载它们。
