# 三策略200笔调试窗口（交易日20261016）

本次用户明确允许将账户每服务窗口真实报单上限从20提高为200；不是取消限制。
三个策略合计200笔，撤单和无实际委托的TARGET_SUBMITTED日志不按真实报单计数。
运行时长3600秒，总量7手、名义预算340万元和原策略算法/参数不变。

先同步新JSON；停止全部旧worker，再停止旧账户服务。旧故障账本不写、不恢复。
本配置建立独立的新账本，包含上次AG空1/AL多1在内的当前柜台全部原仓作为
新会话外部基线，不自动平掉；原会话归属保留于原账本，不能把两份账本相加当作
同一个运行中的账户系统。新策略从自身零仓位开始，可能再建立同向新仓。
这不是旧会话恢复，也未修复达到限额后异常退出的问题；达到200仍可能停止。
交易日或资料日改变时不要直接复用本配置。全程不添加--resume。

终端1，先只读预检：

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
set +e
export PYTHONPATH=.
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true

python -u -m scripts.integration.shared_account.preflight \
  --config scripts/integration/shared_account/three_realtime_20261016_200.json \
  --connect
```

仅当status为prechecked后启动服务：

```bash
python -u -m scripts.integration.shared_account.server \
  --config scripts/integration/shared_account/three_realtime_20261016_200.json \
  --socket /tmp/bomber-three-20261016-200/exec.sock \
  --credentials-dir /tmp/bomber-three-20261016-200/keys \
  --connect --enable-orders --confirm-simnow
```

账户服务就绪后，两分钟内分别启动以下三个终端。

终端2，main_ema：

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
set +e
export PYTHONPATH=.
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true

python -u -m scripts.integration.shared_account.worker \
  --config scripts/integration/shared_account/three_realtime_20261016_200.json \
  --strategy-id ema-cu-three \
  --credentials /tmp/bomber-three-20261016-200/keys/ema-cu-three.json \
  --connect --enable-orders --confirm-simnow
```

终端3，calendar_spread：

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
set +e
export PYTHONPATH=.
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true

python -u -m scripts.integration.shared_account.worker \
  --config scripts/integration/shared_account/three_realtime_20261016_200.json \
  --strategy-id calendar-cu-three \
  --credentials /tmp/bomber-three-20261016-200/keys/calendar-cu-three.json \
  --connect --enable-orders --confirm-simnow
```

终端4，cross_section：

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
set +e
export PYTHONPATH=.
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true

python -u -m scripts.integration.shared_account.worker \
  --config scripts/integration/shared_account/three_realtime_20261016_200.json \
  --strategy-id cross-cu-al-ag-au-three \
  --credentials /tmp/bomber-three-20261016-200/keys/cross-cu-al-ag-au-three.json \
  --connect --enable-orders --confirm-simnow
```

正常停止：先在三个worker终端分别按一次Ctrl+C，等待各自停机结果，最后停止账户服务。
剩余仓位不自动平仓。报告目录：

```text
/workspace/worker/pj/Chrono/nautilus_trader_demo/pro/scripts/integration/shared_account/results/three-realtime-independent-20261016-cu-al-ag-au-200
```

若异常停止，不重复按新会话启动同一文件。保留目录的account.json、各策略检查点、
服务和worker的summary.json/events.txt供核查。
