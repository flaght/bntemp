# 共享CTP TD超时统一配置（2026-10-22）

## 依据与修改

用户线上请求1032：15.000202秒本地超时，16.396848秒收到无错误最终回报；等待旧请求结束后1033资金、1034/1035仓位及1036订单查询均正常完成。此前超时请求结束等待补丁已在线生效，本次减少这种短暂响应延迟导致的退出；不证明最初延迟的网络/柜台原因。

- 公共channels/ctp.py增加ctp_td_timeout_seconds与工厂默认配置：环境变量CTP_TD_TIMEOUT_SECONDS，默认30秒，有限正数且不超过120秒；无效值在创建Transport之前报错。明确timeout_seconds函数实参优先，保留其他入口显式设置。
- shared_account/run.py的build_host移除硬编码15秒；options/shared.py的observe_day移除固定30秒。两处都由公共工厂解析同一个进程环境值，默认30。多策略持续入口也复用observe_day，server通过build_host使用该值；worker不增加TD连接。
- CtpConnection日志显示各连接client_id及超时值。Transport每个_Pending捕获发送时预算，回报等待用该预算；query_diagnostics和事件带timeout_seconds。认证、登录、结算确认的send/final日志升为INFO以看出阶段耗时；普通成功查询仍为DEBUG，不记录凭据、查询字段或资金内容。
- 配置不进入策略参数、账户JSON或持久化身份；已有检查点及归属恢复无需迁移，不修改用户状态文件。账户恢复、外部基线、订单限额、行情时效、指数查询频率均不变。

## 边界

原超时仍闭闸退出；超时请求保留到匹配最终回报才允许新查询。最多两轮停机查询核验、-2/-3退避等上一包逻辑保留。没有增加登录重试、自动重连或自动恢复报单。30秒能覆盖已观察到的16.397秒样本，但不保证更慢或永久无响应成功。单项等待/请求各有预算，整个启动/停机含多项查询，可能超过30秒。

公共工厂的默认值不改直接CtpTdApiTransport类的默认值，也不覆盖旧入口明确传入的timeout_seconds。例如只读探针仍用--timeout，独立08 run_live仍按原query-timeout。当前用户使用的08 shared入口及公共共享run/server/continuous已接入。配置在连接创建时取值，运行中修改环境不会改变已创建连接；重启后生效。无需重编译CTP扩展。

## 离线验证

427项unittest通过：
- 公共查询生命周期、共享账户、公共Live/渠道/恢复/执行组合：231项；新增默认/覆盖优先级/非法值验证、握手日志不泄露字段、模拟16.396848秒回报在15秒失败而30秒成功。模拟时钟不执行16秒真实等待，也不代表柜台测速。
- 08基础装配/来源：91项。
- 08共享恢复、分钟诊断、共享持续运行及公共窗口调度：56项；实际公共工厂在45秒配置下装配观察连接与正式连接，恢复旧期权空1手并验证配置/账户身份不变，无新委托。
- 真实本机Unix socket及多进程共享执行：49项，允许本机socket后通过，不连接外网或真实柜台。
- 原Transport脚本6组检查通过，仍验证缺最终回报/柜台错误/断线/异步拒单/撤单拒绝等。

行情SDK实体和柜台为离线夹具，其余使用项目真实执行、归属、恢复组件。服务器运行待验。旧验收的历史全量源码哈希不适用于本补充，以本包SHA为准。

## 服务器更新

本补充叠加CTP_QUERY_DRAIN_RECOVERY_20261021，服务器已校验过该包。旧进程正常结束后同步并执行：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/CTP_TD_TIMEOUT_CONFIG_20261022.tar.gz
sha256sum -c doc/evidence/checksums/CTP_TD_TIMEOUT_CONFIG_20261022_SHA256.txt
```

全部OK后恢复原检查点，保留MO时段与原风险限制：

```bash
(
  set -e
  cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
  export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
  export CTP_PRODUCTION_MODE=true
  export CTP_TD_TIMEOUT_SECONDS=30
  export PYTHONUNBUFFERED=1
  /workspace/worker/env/uv-nautilus/bin/python -u -c \
    'import logging; logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"); from scripts.integration.options.shared import main; main()' \
    --connect --enable-orders --confirm-simnow \
    --calendar-id china.cffex --hours-code MO \
    --trading-hours-database dfs://bomber_static \
    --trading-hours-table fut_trading_session --no-session-overrides \
    --state-file /workspace/worker/pj/Chrono/nautilus_trader_demo/pro_new/tests/results/gap08-realtime-simnow/state-1791869411336135873.json \
    --index-poll-seconds 1 --max-session-orders 20 \
    --report-dir tests/results/gap08-shared-continuous --run-forever
)
```

启动应见option-vega-observe与正式option-vega-live的CtpConnection日志均为timeout_seconds=30.0，以及kind=auth/login/settlement的send/final耗时。会话日历仍读取MO，下午窗口到14:57。若仍失败，提供failure、cleanup_error、stop_reconciliation及非零归属仓位；登录装配失败时可能尚无option_report，应保留CtpConnection/CtpQuery阶段日志。无须改检查点、增加手数或主动平仓来验证。
