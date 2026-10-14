# CU／AL／AG／AU 三策略独立进程运行

## 使用范围

本页为REV26实现的四品种迁移及短时调试流程，不代表固定交付已经通过。当前交付按[固定交付与结束条件](../../../doc/audits/ARCHITECTURE_GAP_REVIEW_2026-10-09.md#固定交付与结束条件)执行：先完成D1单EMA，再完成D2三个独立worker；D1／D2均须实际DDB预热。现有共享工厂尚未完成历史窗口接入，不能把本页的实时重新预热作为正式预热验收。

下文900秒worker／1020秒服务是原调试配置；D2最终版本须覆盖60个有效分钟及逐策略停止恢复，并提供完整替代配置和命令。缺历史资料、没有真实信号或成交时保留对应待验条目，不新增迁移或扩大运行范围来代替验收。本页原仓迁移仅用于确实需要沿用旧检查点的会话，不作为每次正常启动步骤。

本次改动：原仓单独保存为外部基线，策略和CTP执行账本仅记自己的成交；
柜台全账户资金仍查询，同日总仓/今昨桶按“基线＋系统成交”核验。
同合约外部基线首期支持SHFE/INE，平自己今仓，保留外部今昨桶。
外部仓变化或未知活动单仍拒绝；不接管原仓，不自动平仓。

小幅超前行情在CTP接入处最多等待100毫秒，等本机时间追上才发布；
原事件时间和原始接收时间保留。超出100毫秒或等待期间时钟不能追上则行情闭闸。
不改写价格时间，不豁免报单时效。旧配置默认缓冲0；新配置显式声明100。

范围迁移仅支持本次同TD日的CU/AL→CU/AL/AG/AU。旧横截面须没有目标、仓位、
订单或事件历史。EMA、跨期原归属、订单历史、请求日志、消费游标及原订单编号
全部保留，指标重新预热。原账本不覆盖，新目录和新横截面ID单独生成。
若原账户有未决发送、跨期不平衡归属或未知活动单，应保留并核验原报告，不能删文件重试。

配置草案three_realtime_cu_al_ag_au.json没有实时预算证据；下列流程生成的
three_realtime_four_ready.json才是本次迁移配置。账户总量上界为7手，CU主力在
EMA和横截面各算一次。总名义上界按本次买一/卖一盘口、每腿最多1手、买价加1跳
计算，向上取整到10万元，且不低于原300万元。各策略原算法、单腿1手及原
单腿名义上限不变；四个品种参与排名，不承诺各品种必成交或各入选品种都能买足一手。
实时价格/资金仍在实际发送时重新检查。账户最多20笔是原每次服务窗口限制。

## 准备与只读迁移

先同步本次全部文件。分别在旧worker终端按Ctrl+C，等各自输出停机结果，
最后停止旧server并等停机结果。保留旧配置、所有账本和结果目录。
服务、worker仍运行时，迁移会被现有账户/文件写入锁拒绝。

在Linux的pro目录执行以下完整准备块；任一步失败会停止，不启动策略：

```bash
set -e
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro

export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true

sha256sum -c doc/evidence/checksums/GAP10_FOUR_PRODUCTS_BASELINE_20261014_REV26_SHA256.txt

python -u -m tests.run_main_ema_acceptance \
  --only shared-account --only shared-ipc --only ctp-depth-health \
  --only calendar-live --only live-common

python -u -m scripts.integration.shared_account.discover \
  --connect --environment realtime \
  --cross-products CU,AL,AG,AU --ctp-future-buffer-ms 100 \
  --output scripts/integration/shared_account/results/three-four-observed.json

python -u -m scripts.integration.shared_account.prepare_session \
  --output scripts/integration/shared_account/three_realtime_four_ready.json \
  --environment realtime --execution-mode independent \
  --cross-products CU,AL,AG,AU \
  --existing-position-policy preserve_external --ctp-future-buffer-ms 100 \
  --use-observed-budget \
  --observations scripts/integration/shared_account/results/three-four-observed.json

python -u -m scripts.integration.shared_account.migrate_scope \
  --from-config scripts/integration/shared_account/three_realtime.json \
  --to-config scripts/integration/shared_account/three_realtime_four_ready.json \
  --connect

python -u -m scripts.integration.shared_account.preflight \
  --config scripts/integration/shared_account/three_realtime_four_ready.json \
  --resume --connect
```

要求：集中验收通过；discover为observed、orders_submitted=0，预算明细含7条；
迁移为migrated、original_state_unchanged=True；预检为prechecked、orders_submitted=0。
运行须四品种主力及CU跨期双腿都有健康行情。若某品种已收盘或缺数据，应等完整
行情时段，不从排名中自动删掉品种。生成器不覆盖现有配置，迁移不覆盖新检查点；
部分输出或迁移失败须先查看报告，不删除原/新文件绕过检查。

## 四个独立终端启动

每个终端先执行下面公共环境设置，使用原uv-nautilus虚拟环境：

```bash
source /workspace/worker/env/uv-nautilus/bin/activate
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
export CTP_MD_ADDRESS=tcp://182.254.243.31:30011
export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
export CTP_PRODUCTION_MODE=true
```

终端1，唯一账户服务：

```bash
python -u -m scripts.integration.shared_account.server \
  --config scripts/integration/shared_account/three_realtime_four_ready.json \
  --socket /tmp/bomber-three-four/exec.sock \
  --credentials-dir /tmp/bomber-three-four/keys \
  --resume --connect --enable-orders --confirm-simnow
```

看到“账户服务就绪”后，在另外三个终端分别执行下列命令。原EMA/跨期worker文件
存在时使用resume，未启动过时新启动；下面根据配置自己的检查点自动选择该标志，
worker仍严格核对身份和消费版本。

终端2，EMA：

```bash
python -u - <<'PY'
from pathlib import Path
from scripts.integration.shared_account.config import read_config
from scripts.integration.shared_account.worker import main
path = "scripts/integration/shared_account/three_realtime_four_ready.json"
sid = "ema-cu-three"
row = next(r for r in read_config(path)["strategies"] if r["strategy_id"] == sid)
argv = ["--config", path, "--strategy-id", sid,
        "--credentials", f"/tmp/bomber-three-four/keys/{sid}.json",
        "--connect", "--enable-orders", "--confirm-simnow"]
if Path(row["state_file"]).is_file():
    argv.append("--resume")
main(argv)
PY
```

终端3，四品种横截面：

```bash
python -u - <<'PY'
from pathlib import Path
from scripts.integration.shared_account.config import read_config
from scripts.integration.shared_account.worker import main
path = "scripts/integration/shared_account/three_realtime_four_ready.json"
sid = "cross-cu-al-ag-au-three"
row = next(r for r in read_config(path)["strategies"] if r["strategy_id"] == sid)
argv = ["--config", path, "--strategy-id", sid,
        "--credentials", f"/tmp/bomber-three-four/keys/{sid}.json",
        "--connect", "--enable-orders", "--confirm-simnow"]
if Path(row["state_file"]).is_file():
    argv.append("--resume")
main(argv)
PY
```

终端4，跨期：

```bash
python -u - <<'PY'
from pathlib import Path
from scripts.integration.shared_account.config import read_config
from scripts.integration.shared_account.worker import main
path = "scripts/integration/shared_account/three_realtime_four_ready.json"
sid = "calendar-cu-three"
row = next(r for r in read_config(path)["strategies"] if r["strategy_id"] == sid)
argv = ["--config", path, "--strategy-id", sid,
        "--credentials", f"/tmp/bomber-three-four/keys/{sid}.json",
        "--connect", "--enable-orders", "--confirm-simnow"]
if Path(row["state_file"]).is_file():
    argv.append("--resume")
main(argv)
PY
```

这是900秒验收窗口，服务为1020秒。请在服务就绪后的前120秒内启动三个worker；
服务到期会关闭socket。各策略自行结束后再停止server。过程中原仓不自动平仓。

账户服务维护自己持仓的CTP深度估值，worker停止后仍检查风险；不运行策略算法。
正式D1/D2仍须完成DDB历史预热，本页仅覆盖本批迁移与短时运行验证。

## 返回报告

贴回准备阶段的停机结果（包含实际预算明细）、server和三个worker的停机结果。
全部报告目录由每次“记录目录”输出确定，位于配置report_dir下；每个目录有
summary.json和events.txt。新账户路径为
scripts/integration/shared_account/results/three-realtime-independent-20261015-cu-al-ag-au/account.json，
TD须以本次观察为准，不跨日复用。原账户文件保持原路径。

验收必须核对每个成交event_id归属、自己的累计仓位、三个worker游标/回调数、
柜台总仓=外部基线+系统成交仓位，以及最终活动单为0。215项离线通过不是柜台
三策略实际运行证明；GAP10仍待这些报告核验。
