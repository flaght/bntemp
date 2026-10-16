# IM/IF 三策略独立进程联合验收

2026-10-16。本批接入原01主力EMA、03跨期、04横截面，共用一个账户服务、一个TD；
三个worker可分别启动、停止。执行模式为independent，反向目标分别真实报单，
策略仓位只来自自己收到的真实成交归属，不采用物理账户净仓作为策略仓位。

| 策略ID | 品种／角色 | 启动历史 | 原策略参数 |
| --- | --- | --- | --- |
| ema-im-three | IM main | 240根原始分钟；原01因子处理 | fast=3，slow=5，1手 |
| calendar-im-three | IM main + secondary | 120个完整双腿分钟帧 | lookback=120，rebalance=5，entry=2，exit=0.5，1手／腿 |
| cross-im-if-three | IM/IF main | 21个完整复权分钟帧 | lookback=20，rebalance=5，group_fraction=0.5，最多1手／合约 |

历史采用原有DolphinDB读取、china.cffex会话、recovery和missing=fail，只预热指标。
开始实时决策后才允许新目标。当前角色合约由本次只读观察固定，不直接沿用之前
Recording中的代码。每腿名义限额300万元；账户总限额按实际ask+一跳、乘数和
五个归属腿计算，向上取整到10万元，最低300万元。IM在不同策略出现时分别计入。
价格变化使预算不足时仍会被原风控拒绝，不自动提高额度。

REV32修正合约发现口径：contracts保留角色结构表，strategy_contracts列出每个策略
实际路由。01/04沿用原有的因子表同角色合约优先规则；03不使用因子，按结构表选双腿。
因此EMA的IM main可能与跨期的IM main是不同月份，不能用一个全局主力代码替代。
发现流程会查询实际路由并订阅其盘口，按逐策略路由计算预算；旧的仅角色表观察
不再用于生成IM/IF配置。reference_manifests保留各策略资料来源/因子证据。

新会话要求管理合约的多空总仓均为零、全账户无活动单；其他合约持仓继续保留。
不要通过修改账本、删除状态或preserve_external接管已有IM/IF仓位；通用平仓
交易所当前不支持同合约外部基线。账户服务持有整个物理账户锁，不得另开TD报单服务。

## 1. Linux 同步后的无网络验收

先停止上一轮worker及账户服务，保留报告和账本。以下命令均在pro目录执行。
不在交互终端设置set -e或使用exit；单条命令失败就停在该步检查报告。

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
set +e

sha256sum -c doc/evidence/checksums/GAP10_STRATEGY_REFERENCE_DISCOVERY_20261016_REV32_SHA256.txt

python -u -m tests.run_main_ema_acceptance \
  --only shared-account --only shared-ipc \
  --only shared-discovery \
  --only entry --only calendar-live --only cross-live \
  --only calendar-history --only cross-history \
  --only live-common --only history-adjustment --only option-shared
```

sha256必须全部OK，选定模块必须全部exit_code=0。本机已通过共享执行、实际Unix
socket账户服务及回调收尾测试；本机缺原生Bar/指标及dotenv，完整原生入口和01
历史调整组及连续共享回归尚待本次Linux运行，不把依赖导入失败登记为功能通过。

REV32仅修复只读发现/配置/预算。本轮REV31原生模块已经通过时，只需复测：

```bash
python -u -m tests.run_main_ema_acceptance --only shared-ipc --only shared-discovery
```

## 2. 终端1：观察、生成一次配置、预检、启动账户服务

IM/IF需要在实际有行情的CFFEX交易时段进行。流程使用固定配置名和策略ID，
不要求每次换V版本。以下是首次新会话；若配置或状态已存在，保留它们，按文末
恢复规则处理，不重复执行生成命令。日期及合约变化时重新核验，不能硬改旧账本。

```bash
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true

python -u -m scripts.integration.shared_account.discover \
  --connect --environment realtime --profile im-if \
  --ctp-future-buffer-ms 100 \
  --output scripts/integration/shared_account/results/im-if-observed.json
```

确认status=observed、orders_submitted=0，strategy_contracts覆盖三个策略实际路由，
查看budget_upper_bound中的逐腿价格、乘数及金额后执行：

已存在上一版im_if_joint.json时，先执行本节下方的未启动配置备份，再运行生成命令。

```bash
python -u -m scripts.integration.shared_account.prepare_session \
  --output scripts/integration/shared_account/im_if_joint.json \
  --environment realtime --execution-mode independent --profile im-if \
  --observations scripts/integration/shared_account/results/im-if-observed.json \
  --use-observed-budget --ctp-future-buffer-ms 100 --seconds 3600

python -u -m scripts.integration.shared_account.preflight \
  --config scripts/integration/shared_account/im_if_joint.json --connect
```

若上一版已生成im_if_joint.json、仅预检合约不符且没有任何账户/策略状态文件，
先在下面的状态锁保护下备份原配置，再执行上述生成命令。配置名和策略ID不必改。
存在任何检查点或有进程持锁都会拒绝，不改旧账本，不自动迁移已有交易。

```bash
python - <<'PY'
from contextlib import ExitStack
from pathlib import Path
import time
from scripts.integration.shared_account.config import read_config
from bomber.framework.trader.runtime.live.local import strategy_state_lock

p = Path('scripts/integration/shared_account/im_if_joint.json')
if p.exists():
    c = read_config(p)
    states = [Path(c['account_state_file']), *(Path(r['state_file']) for r in c['strategies'])]
    with ExitStack() as locks:
        for state in states:
            locks.enter_context(strategy_state_lock(state))
        if any(state.exists() for state in states):
            raise SystemExit('已有运行账本，保留原配置并停止；不能按未启动配置重建')
        backup = p.with_name(p.name + '.before-reference-fix-' + str(time.time_ns()))
        p.rename(backup)
        print('原配置已备份：', backup)
PY
```

确认status=prechecked、orders_submitted=0后启动服务：

```bash
python -u -m scripts.integration.shared_account.server \
  --config scripts/integration/shared_account/im_if_joint.json \
  --socket /tmp/bomber-im-if-joint/exec.sock \
  --credentials-dir /tmp/bomber-im-if-joint/keys \
  --connect --enable-orders --confirm-simnow
```

看到“账户服务就绪”后保留终端1运行，再开三个终端。
生成配置的账户额度为200笔，计母单笔数，不按成交笔数计。
3600秒为各worker完成预热后的运行窗口；服务额外保留120秒启动缓冲。
若预热和分终端启动超过缓冲，报告只能按实际共同运行时长验收，不能算满60分钟。

## 3. 终端2：01 EMA

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
set +e
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true

python -u -m scripts.integration.shared_account.worker \
  --config scripts/integration/shared_account/im_if_joint.json \
  --strategy-id ema-im-three \
  --credentials /tmp/bomber-im-if-joint/keys/ema-im-three.json \
  --connect --enable-orders --confirm-simnow
```

## 4. 终端3：03 跨期

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
set +e
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true

python -u -m scripts.integration.shared_account.worker \
  --config scripts/integration/shared_account/im_if_joint.json \
  --strategy-id calendar-im-three \
  --credentials /tmp/bomber-im-if-joint/keys/calendar-im-three.json \
  --connect --enable-orders --confirm-simnow
```

## 5. 终端4：04 横截面

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
set +e
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true

python -u -m scripts.integration.shared_account.worker \
  --config scripts/integration/shared_account/im_if_joint.json \
  --strategy-id cross-im-if-three \
  --credentials /tmp/bomber-im-if-joint/keys/cross-im-if-three.json \
  --connect --enable-orders --confirm-simnow
```

## 6. 结束、额度耗尽和恢复

正常等待worker到时结束，再停止账户服务。提前结束时先分别在三个worker按一次
Ctrl+C，等各自停机结果打印完，再在服务按一次Ctrl+C。保留持仓，不自动平仓。

额度用尽或不足下一完整订单批次时，停止新目标、撤销已知活动订单、交付停止边界
成交，等待各worker确认事件游标和停止。正常结果为status=completed、
stop_reason=order_budget_exhausted，failure/shutdown_failure为空，final_active_orders=0。
账户和策略归属保留。额度只剩一笔时不会发送需要两笔的跨期批次。
该流程也会结束本次服务窗口，不在后台自动重置额度。

撤单失败、确认超时、拒单导致恢复标记、未知订单或账本不一致仍属于故障，继续
failed并保留证据。服务保留主故障和shutdown_failure，收尾异常不覆盖最初故障。

同日、同配置且账本存在时，账户服务命令加--resume；每个worker仅在自己的
state_file存在时加--resume。不再次运行prepare_session，不删除socket或账本。
正常退出会删除服务自己的socket；异常遗留socket先核对没有持有者再处理。
普通resume保留已用额度，200笔用尽后不会再获得200笔。连续运行入口原有的
显式新窗口额度机制保留；本次联合验收不自动续额度。

## 7. 回传证据

配置的report_dir给出本次报告根目录，脚本也会打印各自“记录目录”。先回传
无网络验收summary.json、只读观察和预检结果，再回传本次完整报告根目录及
im_if_joint.json。报告包括服务、三个worker的summary.json/events.txt、账户
account.json和三个策略检查点；不用复制TD/MD flow或凭据目录。

核对01初始240根、03初始120双腿帧、04初始21完整帧，missing=0；历史本身不报单。
随后核对各策略新目标、真实订单与成交归属、跨期双腿及停止后的游标确认；最后
柜台只读active_orders=0、总仓与归属/外部基线一致。没有特定信号或成交时登记
未触发，不用Recording目标替代真实成交验收。

```bash
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30
```
