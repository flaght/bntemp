# 08与公共同合约多策略归属恢复（2026-10-16）

## 问题与最终行为

用户原检查点generation=85、TD=20261016，系统账本仅MO2611-C-7100空1手；柜台另有其他策略开的IM2610空1手。旧08用候选合约范围核对物理总仓，误将候选路由范围当成所有权边界，导致同日恢复被拦。前一轮仅刷新范围外基线，未处理候选范围内的其他来源仓位。

本次08共享持续入口默认启用独立订单及`preserve_external`。成交归属不按品种或合约独占：两个策略可以持有同一合约的同向或反向份额。系统CTP账本记录本账户执行服务的成交；每个策略目标和可平量只来自自己的归属。柜台多空总仓分解为系统账本加外部基线，不用净仓代替多空总量。候选路由集合仍用于合约注册、风险和执行权限，不认定柜台同合约全部仓位归属。

同进程策略共用一个账户执行服务；不同进程通过同一执行服务的IPC获得独立目标、成交回报和恢复状态。保留物理账户唯一写入端。独立的未知TD终端并发交易无法仅凭柜台快照证明归属，不能用放开账户锁或自动改写策略数量替代共享执行服务。已有外部基线未知变化、未知活动订单、未决发送仍闭闸。

## 恢复及平仓

- 原共享version1基线可沿用原命令恢复：核验原归属、终态订单及系统成交账本，稳定查询柜台总仓/今昨明细，保留原有份额，其余只记外部基线，不创建新的策略归属。
- 仅允许原持仓政策续接为`preserve_external`；策略ID、账户、算法参数、参考、风险预算、历史订单、成交去重保持原核验。原策略已有非零份额不得被清空或替换。
- 旧strict模式保存在PositionManager的范围外物理净仓转为独立外部观测；PositionManager只保留系统成交净仓。
- 最终恢复对账通过后才按原机制备份原代并提交version2外部基线。查询失败、未知活动单、已归属容量不足、原账本/归属不一致时原文件不变且不报单。
- CFFEX/DCE/CZCE等按配置使用通用CLOSE；外部多空总量保留，物理今昨桶可能变化，不能承诺某个柜台物理桶属于某策略。明确今昨平仓的SHFE/INE使用系统自有账本桶规划平今/平昨，数量不超过发起策略份额。
- 移除了独立归属只允许CFFEX跨日总仓的分支；独立策略按各交易所正确开平标志管理已记账今昨仓，不按品种排除所有权。
- 跨日保留本系统总量和成交成本，逻辑昨日桶须通过柜台容量核验。不会拿混合外部份额的柜台均价覆盖系统成本；系统成本账不等同柜台结算成本/逐日盯市损益。
- 已有version2同日不自动接受未知基线变化。其他系统策略应在同一服务注册，通过已知成交更新总账，不能靠每次重建外部基线吞掉未解释成交。

## 同步与启动

停止旧08进程后，只同步本包及其SHA清单，不删除或手改原JSON，不覆盖旧完整交付包：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_OWNED_POSITIONS_20261016.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_OWNED_POSITIONS_20261016_SHA256.txt
```

全部OK后沿用原启动命令，无需新参数：

```bash
(
  set -e
  cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro
  export CTP_TD_ADDRESS=tcp://182.254.243.31:30001
  export CTP_PRODUCTION_MODE=true
  export PYTHONUNBUFFERED=1
  /workspace/worker/env/uv-nautilus/bin/python -u -c \
    'import logging; logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"); from scripts.integration.options.shared import main; main()' \
    --connect --enable-orders --confirm-simnow \
    --calendar-id china.cffex --hours-code IM \
    --trading-hours-database dfs://bomber_static \
    --trading-hours-table fut_trading_session --no-session-overrides \
    --state-file /workspace/worker/pj/Chrono/nautilus_trader_demo/pro_new/tests/results/gap08-realtime-simnow/state-1791869411336135873.json \
    --index-poll-seconds 1 --max-session-orders 20 \
    --report-dir tests/results/gap08-shared-continuous --run-forever
)
```

首次迁移日志应出现`成交归属与外部基线已核验，待恢复最终对账提交`，这是暂存核验，仍须出现`08共享会话就绪`。若原柜台/账本没有新变化，首个有效分钟决策仍应显示7100空1手，IM2610不会被认成08持仓。最终报告`rollover.ownership_baseline`记录外部基线及归属依据。其他共享配置显式使用`execution_mode=independent`、`position_scope=strategy_instruments`、`existing_position_policy=preserve_external`；没有把全账户strict配置静默改成该政策。

## 离线证据与范围

共349项测试通过：08基础81、共享恢复26、指数轮询5、公共共享账户83、公共生命周期120、IPC/多进程/范围迁移34。持久化、归属、规划和IPC为真实组件，SDK实体/柜台/行情为离线夹具。

新增/扩展场景覆盖：五类交易所的同合约两策略同向份额与外部双向仓；CFFEX外部基线与相反策略份额；只关闭发起策略、其他策略份额及外部总量保留；SHFE已记账昨仓平仓；旧08同日迁移、候选范围内额外期货、同合约额外多空仓、跨日恢复、成本和订单历史保留；原归属容量不足不覆盖检查点；IPC目标/回报隔离及三个真实工作进程附着同一账户端时不接管外部仓位。

本机未连接DolphinDB/CTP、未向SimNow提交实际订单。服务器恢复和连续管理仍待线上结果。14:57窗口方案未包含在本次修改中。旧全量验收的历史源码指纹不作为本补充源码的版本清单，以本包SHA为准。
