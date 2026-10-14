# 01／03／04 三策略同账户运行

2026-10-14后续补充：工厂已支持08 `option_vega`；共享宿主/worker支持其DolphinDB
分钟与指数特征查询、封帧及选约去重检查点。实时CFFEX已终结订单的前日归属账本
可按当日权威今昨仓/成本核验后恢复。下文REV19的“仅同日”是原交付边界；SHFE等
独立昨仓仍拒绝，跨日未知活动单／未决发送仍拒绝。08入口、原仓导入和范围见
[08共享恢复交接](../../../doc/HANDOFF/live/HANDOFF_OPTION_VEGA_SHARED_ROLLOVER_2026-10-14.md)。

需求见doc/HANDOFF/gaps/GAP10_THREE_STRATEGY_REQUIREMENTS_2026-10-14.md。
execution_mode=netted：同进程净额执行，内部抵消单独记录，不虚构柜台成交。
execution_mode=independent：一个账户服务及三个独立worker，订单不跨策略抵消。
账户服务持有唯一TD、写入锁、编号、资金/订单查询和同代检查点；worker只有自己的
归属仓位镜像及MD，不使用柜台总仓覆盖策略仓位。原策略算法/Runner保持。

独立开空不得平另一来源多仓；反手先平自己的仓位，终态后开反向仓。同合约
在途订单串行规划，其他目标保留并由新行情推进。初始管理范围须空仓，仅支持
同TD日已终结订单恢复；昨仓、未决请求、活动订单、跨期不平衡双腿保持拒绝。
这不是跨日/未知活动单恢复实现。范围外持仓按原基线保护，不默认平仓。

Unix socket及策略能力文件仅供同机同用户使用；文件0600，不打印/提交令牌。
请求先写入原账户检查点；相同请求ID和内容不重复发送，冲突/未决请求拒绝重试。
事件与订单同代保存，worker回调后保存游标再确认；按游标至少一次补交。仓位
由账户归属镜像替换，不再把回调增量加一遍。自定义外部回调副作用须按事件ID
幂等，不能宣称任意外部副作用恰好一次。拒单/状态不明仍按原账户保护闭闸。

## Linux集中验收

在pro根目录执行，失败则不报单：

```bash
sha256sum -c doc/evidence/checksums/GAP10_THREE_STRATEGIES_20261014_REV19_SHA256.txt
python -u -m tests.run_main_ema_acceptance \
  --only shared-account --only shared-ipc --only calendar-live --only live-common
```

本地共享65、母单15、IPC/应用10、公共116，共206项离线通过；SDK类型/柜台
为替身，socket确有三个操作系统进程。完整calendar-live在本地因缺少
bomber.framework.fixes.Bar未运行，其28项须Linux原生环境验证。以下是验收
指令，不能当作replay已经通过的证据。

## 16:00 replay准备（北京时间2026-10-14）

保留.env账户与数据库配置，仅覆盖本批终端前置。每个worker终端也设置这三个变量：

```bash
export CTP_MD_ADDRESS=tcp://182.254.243.31:40011
export CTP_TD_ADDRESS=tcp://182.254.243.31:40001
export CTP_PRODUCTION_MODE=true

python -u -m scripts.integration.shared_account.discover \
  --connect --environment replay \
  --output scripts/integration/shared_account/results/three-replay-observed.json

python -u -m scripts.integration.shared_account.prepare_session \
  --output scripts/integration/shared_account/three_replay.json \
  --environment replay --execution-mode independent \
  --observations scripts/integration/shared_account/results/three-replay-observed.json

python -u -m scripts.integration.shared_account.preflight \
  --config scripts/integration/shared_account/three_replay.json --connect
```

discover只读观察TD、资料来源和原始MD日；生成器读取成功观察，不猜日期。
预检核对逐合约行情、环境、时效、资料和管理范围空仓，不运行算法或发单。
配置为EMA CU、横截面CU/AL、跨期CU secondary/far，新ID与四个独立检查点，
900秒，归属预算5手/300万元、最多20笔。旧230万会话不改。实际资金、价格和
各策略预算仍门控，缺资料/行情/管理范围非空时禁止自动替换合约或扩大额度。
生成器不覆盖同名配置；恢复须原文件及resume，不能删账本绕过限制。

## 四终端启动（会实际报单）

集中验收和预检通过后，先运行终端1，看到“账户服务就绪”再启动终端2/3/4。
四个终端均在pro目录并使用相同replay前置。服务比worker窗口多留120秒。

终端1：

```bash
python -u -m scripts.integration.shared_account.server \
  --config scripts/integration/shared_account/three_replay.json \
  --socket /tmp/bomber-three-replay/exec.sock \
  --credentials-dir /tmp/bomber-three-replay/keys \
  --connect --enable-orders --confirm-simnow
```

终端2：

```bash
python -u -m scripts.integration.shared_account.worker \
  --config scripts/integration/shared_account/three_replay.json \
  --strategy-id ema-cu-three \
  --credentials /tmp/bomber-three-replay/keys/ema-cu-three.json \
  --connect --enable-orders --confirm-simnow
```

终端3：

```bash
python -u -m scripts.integration.shared_account.worker \
  --config scripts/integration/shared_account/three_replay.json \
  --strategy-id cross-cu-al-three \
  --credentials /tmp/bomber-three-replay/keys/cross-cu-al-three.json \
  --connect --enable-orders --confirm-simnow
```

终端4：

```bash
python -u -m scripts.integration.shared_account.worker \
  --config scripts/integration/shared_account/three_replay.json \
  --strategy-id calendar-cu-three \
  --credentials /tmp/bomber-three-replay/keys/calendar-cu-three.json \
  --connect --enable-orders --confirm-simnow
```

分别Ctrl-C停止worker：关闭新目标、服务撤单、排空回调、保存游标，然后解绑。
强杀/断线由30秒租约到期停止该来源；仓位保留，不自动平仓。同日重启worker
用原命令加resume，账户服务不重启；重新预热，不发送旧信号。账户服务最后停。
服务同日无活动/未决订单恢复加resume，worker重新读取新能力文件。遗留socket
先确认旧进程与账户状态，不能删除活动socket接管账户。

回传四个新报告目录的summary.json/events.txt，核对三策略信号和真实成交、
归属、确认游标、服务汇总/最终总仓/活动单，以及停止重启一个worker时其他两个
继续。无真实信号/成交不能仅凭completed登记通过；跨期腿失败单独核对。

## 同进程netted验收

同一成功观察可生成全新three_replay_netted.json，生成参数改execution-mode=netted。
用scripts.integration.shared_account.run加config/connect/enable-orders/confirm-simnow运行。
必须先确认管理范围空仓，不能复用independent账本或在其持仓未处理时新建netted会话。

## 21:00 realtime门槛

北京时间2026-10-14 21:00是计划，不能代替验收。replay三策略原生装配、成交归属、
独立停止/恢复通过后，四个终端改MD30011/TD30001前置，重新discover（environment
realtime，另一个观察文件），生成three_realtime.json，执行preflight。夜盘TD日
由新登录确认；不要猜成自然日。启动命令改用该配置及/tmp/bomber-three-realtime
的socket/能力文件。replay与realtime独立状态/flow/报告，不复制或覆盖旧账本。
未通过、管理范围有旧仓或跨日时不自动启动新实时账本、不自动清仓/迁移。
用户同步代码、在Linux执行并回传报告；本地没有连接真实柜台。
