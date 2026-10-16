# IM／IH／IC／IF 四品种横截面联合运行（REV33）

本页用于用户已成交的IM/IF三策略会话扩品种。原01 EMA仍为IM，原03跨期仍为
IM main+secondary；04横截面改为IM、IH、IC、IF，group_fraction=0.5，多空各选两名。
01/03/04历史窗口仍为240根、120帧、21帧。04的21帧要求四个品种同分钟完整，
历史只推进指标、不报单；每合约目标上限仍为1手。

`cffex-four`只读观察使用逐策略因子合约映射，IH/IF与IM/IC分别读取各自条款乘数，
不会复制IM因子或乘数。账户七个归属腿按实际ask+一跳计入预算，向上取整10万元，
不低于原账户预算。横截面每侧预算不低于原300万元，并按两名等分后能覆盖最贵
观察合约一手计算；实际交易仍检查最新价格和资金。总订单额度保留原值及已用计数。

已有成交时必须保留原三个策略ID；即使ID仍叫cross-im-if-three，其实际products
也会变为四品种。账户client_id、仓位、历史订单、事件/请求日志、消费游标、成交
计数、信号版本、CTP OrderRef及去重资料均保留。迁移输出到独立新目录，原账本
逐字节保留；不清仓，不接管新增IH/IC外部仓位，不重发原目标。

## 1. 先正常停止旧会话并同步

依次在原三个worker按一次Ctrl+C，等停机结果；最后停止原账户服务。保留原
im_if_joint.json及其整个报告目录。失败/pending、未结束订单或缺少已交易worker
检查点时先核对原报告，不能删文件或改新ID绕过迁移。

在Linux的pro目录执行：

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
set +e

sha256sum -c doc/evidence/checksums/GAP10_CFFEX_FOUR_EXPANSION_20261016_REV33_SHA256.txt

python -u -m tests.run_main_ema_acceptance \
  --only shared-ipc --only shared-discovery --only cross-history --only cross-live
```

各模块须全部通过。本机公共执行、迁移、历史与参考回归252项通过，柜台/SDK为
替身；cross-live完整原生入口仍须Linux验证，不把它登记为本机通过。

## 2. 只读观察、准备配置、迁移及预检

本批仅同一TD交易日扩品种；不要硬改旧配置日期。先确认仍处于四品种有健康行情
的时段。迁移不启用报单，只有最后恢复账户服务/worker才恢复策略交易。

```bash
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true

python -u -m scripts.integration.shared_account.discover \
  --connect --environment realtime --profile cffex-four \
  --ctp-future-buffer-ms 100 \
  --output scripts/integration/shared_account/results/cffex-four-observed.json
```

确认status=observed、orders_submitted=0，strategy_contracts.cross_section完整包含
IM/IH/IC/IF，各有真实合约、盘口及条款，查看七腿预算后继续。原IM/IF路由必须
保持；若参考合约已经变化，不允许用本次扩品种操作隐含换月。

```bash
python -u -m scripts.integration.shared_account.prepare_session \
  --output scripts/integration/shared_account/cffex_four_joint.json \
  --environment realtime --execution-mode independent --profile cffex-four \
  --from-config scripts/integration/shared_account/im_if_joint.json \
  --observations scripts/integration/shared_account/results/cffex-four-observed.json \
  --use-observed-budget --ctp-future-buffer-ms 100 --seconds 3600
```

生成仅创建新配置，不操作账户或原账本。下面先做迁移声明校验，再进行实际柜台
只读核验与落盘；每条命令成功后才执行下一条。

```bash
python -u -m scripts.integration.shared_account.migrate_scope \
  --from-config scripts/integration/shared_account/im_if_joint.json \
  --to-config scripts/integration/shared_account/cffex_four_joint.json

python -u -m scripts.integration.shared_account.migrate_scope \
  --from-config scripts/integration/shared_account/im_if_joint.json \
  --to-config scripts/integration/shared_account/cffex_four_joint.json \
  --connect
```

必须为status=migrated、migration_mode=cffex_traded_expansion、orders_submitted=0、
original_state_unchanged=true，failure/shutdown_failure为空。原/目标状态文件均受
原有状态锁保护，柜台权威核对通过后才创建目标文件；已有目标不覆盖，失败的部分
输出不自动重试。新增IH/IC若有外部持仓，或原IM/IF路由改变、IPC发送未决，均拒绝。

```bash
python -u -m scripts.integration.shared_account.preflight \
  --config scripts/integration/shared_account/cffex_four_joint.json \
  --resume --connect
```

确认prechecked且orders_submitted=0后，按以下四个终端恢复；此后只使用新配置，
不要同时重启旧im_if_joint会话。下列代码按目标worker文件存在性选择resume，
已交易worker始终恢复原事件游标；此前没启动过的worker允许首次启动。

## 3. 四个终端分别启动

每个终端均先执行公共环境初始化：

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
set +e
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true
```

终端1，唯一账户服务，看到“账户服务就绪”后再启动三个worker：

```bash
python -u -m scripts.integration.shared_account.server \
  --config scripts/integration/shared_account/cffex_four_joint.json \
  --socket /tmp/bomber-cffex-four/exec.sock \
  --credentials-dir /tmp/bomber-cffex-four/keys \
  --resume --connect --enable-orders --confirm-simnow
```

终端2，原EMA：

```bash
python -u - <<'PY'
from pathlib import Path
from scripts.integration.shared_account.config import read_config
from scripts.integration.shared_account.worker import main
p = 'scripts/integration/shared_account/cffex_four_joint.json'
r = next(r for r in read_config(p)['strategies'] if r['kind'] == 'main_ema')
main(['--config', p, '--strategy-id', r['strategy_id'],
      '--credentials', f"/tmp/bomber-cffex-four/keys/{r['strategy_id']}.json",
      *(['--resume'] if Path(r['state_file']).is_file() else []),
      '--connect', '--enable-orders', '--confirm-simnow'])
PY
```

终端3，原跨期：

```bash
python -u - <<'PY'
from pathlib import Path
from scripts.integration.shared_account.config import read_config
from scripts.integration.shared_account.worker import main
p = 'scripts/integration/shared_account/cffex_four_joint.json'
r = next(r for r in read_config(p)['strategies'] if r['kind'] == 'calendar_spread')
main(['--config', p, '--strategy-id', r['strategy_id'],
      '--credentials', f"/tmp/bomber-cffex-four/keys/{r['strategy_id']}.json",
      *(['--resume'] if Path(r['state_file']).is_file() else []),
      '--connect', '--enable-orders', '--confirm-simnow'])
PY
```

终端4，四品种横截面，仍用原策略ID：

```bash
python -u - <<'PY'
from pathlib import Path
from scripts.integration.shared_account.config import read_config
from scripts.integration.shared_account.worker import main
p = 'scripts/integration/shared_account/cffex_four_joint.json'
r = next(r for r in read_config(p)['strategies'] if r['kind'] == 'cross_section')
main(['--config', p, '--strategy-id', r['strategy_id'],
      '--credentials', f"/tmp/bomber-cffex-four/keys/{r['strategy_id']}.json",
      *(['--resume'] if Path(r['state_file']).is_file() else []),
      '--connect', '--enable-orders', '--confirm-simnow'])
PY
```

## 4. 回传与结束

原worker正常停止不会自动清仓，恢复后的新目标继续管理原归属仓位。指标重新
预热，原版本上递增，不把历史窗口转为交易。账户200笔额度不会因迁移重新获得
200笔；额度耗尽仍按原正常停止机制收尾。

先回传四品种观察、迁移summary和新配置，再保留账户服务与三个worker的
summary.json/events.txt、账户及策略检查点。报告根目录为配置中的report_dir，
默认为results/three-realtime-independent-当次TD-im-ih-ic-if。
运行结束仍先停worker、再停账户服务，最后按原TD只读探针对账。

从未启动、没有任何成交/检查点的情况可省略from-config生成全新四品种会话；
它使用新ID和新目录、要求管理范围总仓为零。本用户已成交，不使用此路径。
