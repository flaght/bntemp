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
08读取`work1/env1`，10读取`work2/env2`；策略YAML中的路径相对于项目根目录。

继续使用已核验的公共账户账本和各自原信号检查点。若实际文件在其他位置，在`account.json`中填写绝对路径。
本次精简不修改账户身份、策略ID、手数、批次、归属或已使用报单额度；恢复时仍核验原有订单与仓位。
三个程序默认常驻。停止时先停止策略，等待撤单、回报确认及保存完成，再停止账户服务。
