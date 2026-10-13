# GAP10 共享宿主 SimNow 成交核对

核对日期：2026-10-13，Asia/Shanghai。柜台交易日：20261014。
远端运行目录：`scripts/integration/shared_account/results/shared-simnow-1791900354405023844`。
证据来自用户贴出的运行日志和该目录的`events.txt`；本机未连接柜台、未报单。

## 结论

共享宿主运行完成、范围外持仓隔离及横截面实际成交归属的本次日志核对通过。
本次只有横截面策略产生执行目标，不能作为两策略共同成交、共享母单拆分或完整
GAP10关闭证据。仍须同日恢复、两个活跃来源的实际归属与故障场景验收。

## 逐笔结果

28条执行回报：14条ACCEPTED、14条FILLED；14个不同订单，OrderRef为2至15。
每个订单序列均为1受理、2全量成交，每次成交1手，成交数量累计14手（含开和平）。
回报ID及成交ID均无重复；本文件未见拒单、撤单或部分成交回报。
所有冻结allocation_legs仅含`cross-rb-hc-beta`，target_key匹配真实合约，
target_revision与该订单request_revision一致；受理/成交使用相同冻结token与份额。

| 目标版本 | OrderRef | 执行过程 | 完成后管理净仓 |
|---|---|---|---|
| 1 | 2、3 | RB卖开1；HC买开1 | RB -1，HC +1 |
| 4 | 4、5、6、7 | RB买平今1、HC卖平今1；随后RB买开1、HC卖开1 | RB +1，HC -1 |
| 5 | 8、9、10、11 | RB卖平今1、HC买平今1；随后RB卖开1、HC买开1 | RB -1，HC +1 |
| 6 | 12、13、14、15 | RB买平今1、HC卖平今1；随后RB买开1、HC卖开1 | RB +1，HC -1 |

由初始管理合约空仓按方向与开平独立重算，期间多空总仓均不为负。
最终rb2701.SHFE多1、空0；hc2701.SHFE多0、空1，与用户停机报告的策略仓位、
执行净仓及柜台final_managed_gross一致。14条FILLED也与横截面fills_received=14一致。
retained_target_retry出现在平仓后续开仓单，其版本与冻结归属仍保持原贡献版本；
不是重复成交或回放旧订单。

## 宿主与持仓

停机报告status=completed，failure=None，账户failed=False、pending=False、draining=[]，
final_active_orders=0。EMA以configured_stop停止后，横截面继续执行后续版本。
范围外MO空1、SC多1、NI多1、I空1、AG空1与预检记录一致；events只涉及RB和HC，
没有将这些范围外仓位赋予新策略。

EMA实际只预热4/5根完整Bar，在300秒配置停止时尚未提交目标，fills_received=0。
这次覆盖独立停止及另一策略继续交易，未覆盖EMA参与的双来源净额/冻结份额拆分。
不把正常退出、单来源14次成交或离线测试替代真实双来源验收。

## 后续约束

远端已产生正式共享账户及策略检查点，并保留RB/HC仓位。应保留全部原文件；
相同TD日、原配置恢复使用--resume，不能删除文件或以空仓新会话接管已成交仓位。
后续源码复核及入口回归确认：宿主seconds与stop_after_seconds不属于恢复身份，
可单独延长EMA停止时间；此前将调度变化视为身份冲突的判断已修正。
策略argv、TD日、环境、路由、参考条款及账户预算仍严格保持原身份，
原策略算法和已有成交归属保持不变。resume_acceptance.json仅将EMA停止时间
由300延至480秒，沿用原账户及策略状态文件，须显式--resume。
本次仅核对用户提供日志，尚未读取原始账户检查点或执行远端恢复。
