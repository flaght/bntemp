# 08按原strategy1.py交易逻辑修改（2026-10-20）

依据用户提供的原文件可执行_noddb逻辑，原样快照保存在 tests/fixtures/option_vega/strategy1.py，SHA256为 `aa72eab3417800f50a4eb9c8bf73968c504fad7f9bfeb698593d0c2f1a72c801`。测试从快照AST提取原数值方法，不导入旧交易SDK、不打开原文件配置中的路径，不把文档注释当成交易实现。实现位于 demos/08_option_vega/strategy.py、vega_signal.py、checkpoint.py、rollover.py，回测与Live共用。

## 交易行为

| 项目 | 修改后 |
| --- | --- |
| 候选 | 生命周期有效、strike大于指数、至少5个剩余交易日；先最近到期月后IV计算；无IV不改选远月；Delta距离相同按输入顺序 |
| 定价 | 同月IM真实价格作F、认购价格反解IV；原二分边界、区间1e-4..5、64轮 |
| 期限 | 选约用剩余交易日/252；对冲另加原函数剩余交易分钟/(240×252)；需完整到期日历 |
| Greeks | Delta/Gamma不折现；Vega按自然日/365折现；不是通用折现导数口径 |
| 配仓 | 原Vega预算及反Gamma权重；重复合约先合并再round（半整数取偶）；当日增量累加到旧目标，其他旧期权目标保留 |
| 退出 | 只在option_slot增仓评价后，用premium-(同月future-strike)≤0归零，不取max，也不用指数算内在价值 |
| 对冲 | 只在hedge_slots评价，每日每时点一次；不再因持仓／目标变化在其他分钟额外触发 |
| 默认时点 | option_slot=13:58，hedge_slots=10:00,11:00,13:30,14:00,14:55；恢复会话继续使用保存的13:36等显式配置 |

`close_remaining_days`不再参与退出，保留字段与原参数校验仅兼容旧检查点；CLI帮助已标明。时间价值日志公式是原文件实际实现，不将它擅自改成通用理论公式。

## 保留的实盘约束与原文件差异

- 原文件跟踪目标数量；08继续按实际成交归属计算对冲，不把目标视为成交，也不把柜台其他策略仓位记给08。同合约允许多个策略拥有各自份额。
- 单合约／总手数、名义金额、订单次数、在途订单闸门不放宽。增量配仓仍受原手数限制，累加后越限则整次拒绝增仓，entry=RISK_LIMIT；已有仓仍评估退出、继续管理。期权部分成交在指定时点按已成交份额对冲，已有期货在途订单时等待。
- 原文件无行情／IV年龄限制；08保留新鲜行情及有限IV缓存（默认300秒），重启清空价格和IV。无IV时跳过该月份重算、保留已有期货目标并记录hedge_missing_iv，避免无依据清掉保护。
- 原文件直接删除到期字典；08不删除真实成交事实。到期后仍有实际仓位则要求核验结算。保留回测末日／显式flatten_day全平及残仓检查。
- 原文件行情缓存可用旧分钟；08继续同分钟选约、接收期限及必需行情闸门。源代码使用期权/指数M5、期货M1订阅，本项目按用户要求继续DolphinDB一分钟行情与自定义指数特征，依配置时点评价。
- 不新增IM主力选择、复权因子、缺少同月期货的跨月替代。该修改不证明主力切换是此前故障原因。盘前就绪超时、14:57阶段缺行情仍是独立问题。

## 检查点与日志

信号状态保持version=1，增加logic_version=strategy1-noddb-v1和hedge_sent。旧文件缺少新增字段仍可恢复，保留原成交归属、配置、sent_days及计数；新鲜行情恢复时仍将目标重建为已归属成交，未成交旧目标不继续开仓。所有来源、账户、风险身份校验继续执行，不改原JSON、不创建新空仓状态。

新增hedge_sent随检查点保存，跨日只保留日历内记录。旧检查点不能证明历史某时点已对冲，但不回放历史分钟；从当前行情按当前指定时点评价。升级前应保留完整账户及信号文件，恢复旧代码需要配套旧备份，不支持只切回旧算法而继续写新版信号状态。

分钟决策含logic_version；entry_audit.increments及兼容targets为新增数量，option_goals为退出后的累计目标，positions为实际归属仓位。非交易时点exit_checks为空、hedge=NOT_DUE是正常等待，不再以每分钟出现退出计算作为健康标准。

## 部署及启动

在旧进程正常停止后同步补充包（勿同时运行旧进程）；保留原账户文件、.vega-signal.json及相关组件状态。现有检查点的MO2611-C-7100空1手不会因本次改算法自动平掉或换月；实际仍以恢复核对和后续策略信号为准。

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_SOURCE_LOGIC_20261020.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_SOURCE_LOGIC_20261020_SHA256.txt
```

全部OK后沿用完整持续运行入口，保持原状态路径及已授权SimNow参数：

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

这条命令沿用原身份恢复及日历窗口管理，Ctrl+C请求正常停止并保留成交仓位，不强制要求新成交。当前单合约1手、总期权2手不变，同一合约已空1手时再次增仓会RISK_LIMIT；不应为追求一次成交临时放大限制。观察首个健康分钟的logic_version、positions、option_goals，以及下一指定时点的entry／hedge。若行情仍缺失或柜台核对失败，应保留原错误报告，不能以该修改当作已解决。

## 离线验证

173项通过：数值/策略/独立归属恢复46项（含新增源文件对照12项）、现有08装配及来源91项、共享跨日与分钟诊断36项。覆盖IV与原方法一致、配仓相同、日内期限、重复Delta取整、两天累加、不同合约旧目标保留、累加越限保持原仓、同月期货退出、固定时点对冲、无IV保留保护、检查点兼容及防重复、完整到期日历要求、真实份额归属、DolphinDB流到目标链。真实数据库／柜台及SDK行情实体使用夹具，无联网、无真实报单；服务器持续运行和真实成交待验。命令及输出摘要见对应_OFFLINE.txt。
