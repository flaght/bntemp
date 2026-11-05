# 08策略与最新公共框架接线审查（2026-11-05）

代码基线为开始审查时的aabee59。用户要求按最新框架优化08，检查公共模块冗余并接入新增期权结算。本轮读取了framework总览、公共装配、特征/行情帧、模拟Backend/客户端、成交归属、结算资料/值契约/原生模块、08与12入口及08策略/检查点。结论按实现与已执行验证分别列示，不把组件存在当成整包验收。

## 复用与保留的边界

| 能力 | 当前公共实现 | 08处理 |
| --- | --- | --- |
| 文件索引、条款、分钟规范化与覆盖诊断 | dataprep.paths / metadata / scenarios.option_chain / session | 保留现有接线，不在策略内重做读取 |
| 指数版本、可见时间与EXACT帧对齐 | dataprep.features / datahub.features / market.frames | 保留与Live一致的契约；不放宽manifest或时效 |
| 策略/Feed/执行装配 | trader.assembly + execution.builders | 08 assembly只保留业务绑定、风险限额；没有新建执行或仓位模块 |
| 订单、成交归属与账户 | SimulationExecutionClient / PositionAttributionLedger / 原生ExecEngine | 结算也经过公共事件链；策略不修改公共仓位或现金 |
| 到期撤单与现金结算 | OptionCashSettlementModule + OptionSettlement | 本轮08正式回测接入，使用条款时刻与显式最终价 |
| 实时/常驻与共享账户 | runtime.live / shared / IPC | 继续使用既有能力；本轮不把模拟结算迁入CTP |
| IV、源Greeks、Gamma加权Vega分配、同月对冲 | 08 vega_signal | 保留原strategy1口径，属于业务算法，不移入公共执行层 |

FrameBuffer的在线封帧与历史特征可见时钟回放承担不同输入时序，不能仅因都处理“分钟”而删去一套。期权到期现金结算、CTP每日结算账本、最终结算资料读取也有不同职责；本轮组合既有组件，不合并为一套同时猜时间、定价和改仓的服务。

## 已修改

1. 08使用公共最终结算资料加载器与Backend生命周期参数；期权统一以CASH_SETTLEMENT构造，防止内核与公共模块重复到期处理。控制时钟最多推进到请求结束日末，显式终值可用时刻超过该日则拒绝并要求延长区间。
2. 来源按基础条款last_day映射至公共last_trade契约，不从月份猜日期。没有实际到期残仓可不提供终值；到期后实际仍持仓且缺终值，由公共模块明确失败。零终值、非tick整数倍终值、独立结算费用和可用时间均保留。
3. 08策略只在回测装配显式提供生命周期。到期已停止交易的腿保留实际数量等待权威结算，不为其继续算IV/等待旧报价/发普通清仓或新增目标；公共到期撤单视为预期事件，提前取消及拒单仍失败。
4. 结算成交先由公共账户/归属账本更新，再通知策略清理对应目标、IV/报价缓存。回调不提交新订单，不改其他腿目标；后续重平衡使用已清理目标。同一结算回报去重由公共订单状态机完成。
5. 新增settlements.csv及summary结算来源/加载数/实际结算数，signals保留OPTION_CASH_SETTLEMENT记录；交易手续费继续由公共FeeModel处理，结算费来自终值资料或独立参数，不重复按普通交易手续费计算。
6. 将08/12重复的三个结算CLI参数集中至已有dataprep.option_settlements模块。原12参数名/默认值不变。移除08每帧构建但已无读取用途的last_actual状态及其排序；检查点仍为版本1，不变更旧持久化字段。取消08回测导入时load_dotenv副作用。扩展Backend配置阶段资源清理范围。

选约顺序、日内交易/对冲时点、原IV/Greeks算法、每日增量配仓、合约/总量限额以及Live的自有仓位规则保持原样。没有改用主力期货替代同月估值。末日flatten仍为成功条件之一，结算不会掩盖其他腿残仓或回报错误。

## 验证与未完成项

243项离线测试通过：08原装配/特征/归属/来源91项 + 新增结算11项 = 102；原策略与源公式、Live、公共结算资料/模块、12契约及模拟时序90项；08迟到/跨日/分钟日志/查询间隔51项。新增集成使用真实Runner、SimulationExecutionClient和归属账本，两个策略同合约分别归属，模拟结算报告验证账户与各策略份额先归零、08目标随后清理、重复报告不再次处理且没有普通新委托。它不模拟原生资金撮合。

另提供tests/test_option_vega_native.py两项完整原生回测：临时Feather交易腿、标准指数特征包及显式日历，跨期权到期后继续次日；验证终值0/120.13与费用/时间/终态，缺最终价必须失败。当前Python没有bomber.backtest扩展，两项明确跳过，未计入243项通过。已单独核验这些夹具的条款、日历、特征导出及最终结算资料准备；仍不能代替原生引擎执行。

尚需目标环境执行：

```bash
python -m unittest tests.test_option_vega_native tests.test_option_by_pos_native -v
```

再用用户真实行情、指数包和官方终值做完整日期区间回测，核对settlements/fills/account/positions。不能把这次优化称为收益改进或已完成真实数据验收。保证金仍为基础比例近似；本次没有实现期货完整交割、实物期权行权、Live到期归属重建或多策略净额对锁仓位的模拟现金结算。共享Live账户里其他策略仓位仍不自动归08；模拟结算能力不用于重置CTP检查点。

## 使用

原08回测命令保持有效，跨期权到期且有残仓时追加真实资料，例如：

```bash
--option-settlements /path/to/official-option-daily \
--settlement-available-time 16:00 \
--exercise-fee-per-contract 0
```

目录需含到期日YYYY-MM-DD.feather及code/trade_date/settlPrice；标准文件可显式提供available_at/fee_per_contract。终值来自官方资料，不根据普通close猜测。没有该日期资料时不建议把最后收盘价重命名为终值。

## 本机回归命令

在pro根目录用具备pandas/pyarrow等依赖的Python运行；本机命令额外设PYTHONPATH=.validation/packages:.，离线入口用SDK实体夹具，不联网：

```bash
python tests/run_option_vega_offline.py
python -m unittest tests.test_demo_option_vega tests.test_option_source_logic tests.test_option_vega_live tests.test_option_daily_settlements tests.test_option_expiry_module tests.test_option_by_pos_framework tests.test_simulation_market_timing -q
```

其余51项在同一离线SDK引导后独立载入，见evidence中的本次离线记录。原生测试必须在安装真实扩展的进程单独运行。
