# 公共账户运行

本目录只保留当前三进程运行所需的入口和配置：

```text
account.py          账户入口
account.json        纯账户配置
account.env.example 账户环境示例
launch.yaml         启动参数
README.md           运行说明
```

账户恢复、柜台连接和IPC由`bomber.framework.trader.runtime.live.account`提供；
08和10在自己的原生入口装配策略，使用框架`RemoteStrategyRuntime`连接账户。
本目录没有旧布局、迁移、策略工厂、worker启动器或验收探针。

启动前必须已有公共账户账本。`account_state_file`是恢复文件路径，
不是首次启动时自动生成空账本的输出路径。默认路径相对于`account.json`所在目录，
即项目内的`demos/shared_account/results/account/state.json`。

如果以前已生成纯账户账本，例如还在
`scripts/integration/shared_account/results/account/state.json`，
将`account_state_file`填为该文件的实际绝对路径，继续使用原文件。
纯账户账本包含`payload.component_states.execution_registry`；
旧Hybrid的`account_rb_p.json`或08独立账本不能直接作为新入口的恢复文件。

当前08按全新策略运行：ID为`option-vega-fresh`，独立信号文件为
`demos/08_option_vega/results/live/state/worker_signal_fresh.json`。
保留10的原账户和RB/P信号；不再导入旧08的成交账本、信号、版本号或选约/对冲标记。
旧08若有柜台残余仓位，禁单核验后保留为外部基线，不归属新08，也不自动平仓。

首次需把原Hybrid账户迁入公共账本并登记全新08。仍使用原迁移工具，
日常运行不依赖它，也不向本目录增加启动器。停止旧账户和策略后执行：

```bash
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
  /workspace/worker/env/chs-live/bin/python -u \
  -m scripts.integration.shared_account.migrate_account \
  --account-config demos/shared_account/account.json \
  --source-config scripts/integration/shared_account/legacy_workers.json \
  --trading-day "$VEGA_TD_DAY" \
  --fresh-option --connect --apply
```

`VEGA_TD_DAY`须为柜台当前交易日；新08模式不接受`--option-checkpoint`或`--option-signal`。
`legacy_workers.json`仅描述迁移来源，源账本路径、原策略参数和额度必须与原文件匹配。
移除`--connect --apply`可先只检查文件。完整命令进行禁单柜台核验并生成目标公共账本，
原Hybrid和旧08文件不改写。目标账户和新08信号路径均须未占用。
输出`migrated`后，日常只使用下面三个启动命令；新08后续重启恢复其新检查点，不重复清零。

在项目根目录分别打开三个终端，先启动账户：

```bash
PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python -u demos/shared_account/account.py \
  --config-file demos/shared_account/launch.yaml --config-id workers_account
```

账户服务就绪后启动10（RB/P在同一进程）：

```bash
PYTHONPATH=.:/workspace/worker/pj/KiChaos IMPLUSE_VERSION=all \
  /workspace/worker/env/chs-live/bin/python -u -m demos.10_hybrid_transformer.run_shared \
  --config-file demos/10_hybrid_transformer/config/strategy.yaml --config-id workers_hybrid
```

再启动08：

```bash
PYTHONPATH=. /workspace/worker/env/uv-nautilus/bin/python -u demos/08_option_vega/run_live.py \
  --config-file demos/08_option_vega/config/strategy.yaml --config-id workers_option
```

账户JSON内相对路径以该JSON所在目录为基准。默认账户env是`account.env`，
公共账本是`results/account/state.json`，报告写到`results/account/`。
08读取`demos/08_option_vega/config/.env`，10读取`demos/10_hybrid_transformer/config/.env`；
策略YAML中的路径相对于项目根目录。

继续使用已核验的公共账户账本、10的原信号检查点和08的新信号检查点。
若实际公共账本在其他位置，在`account.json`中填写绝对路径。
10的身份、手数、批次和归属保持不变；公共账户保留原Hybrid报单历史及已使用额度。
三个程序默认常驻。停止时先停止策略，等待撤单、回报确认及保存完成，再停止账户服务。
