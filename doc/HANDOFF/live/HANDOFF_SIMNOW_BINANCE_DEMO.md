# CTP SimNow / Binance DEMO 专项交接

> CTP 与 Binance 两条交易链的当前状态及新对话入口见 `HANDOFF_CTP_BINANCE_NEXT.md`。

> 本文只记录模拟盘联调的现状与下一步，不复制架构设计。整体架构见 `../../architecture/ARCHITECTURE_FIXED.md`、`../../architecture/ARCHITECTURE.md`，详细实施状态见 `../../architecture/STRATEGY_ARCHITECTURE.md`；总交接见 `../coordination/HANDOFF_2026-09-23.md`。

## 目标与安全边界

使用同一份 `examples/single_ema/strategies/ema_cross.py` 策略，分别装配 CTP SimNow 和 Binance U 本位期货 DEMO：行情 Feed → EMA 目标 → 组合/路由 → Planner/Risk → 受控执行端 → 订单、成交、仓位及账户回报。策略不按交易端写分支。

Recording 请求只能证明“策略产生了目标”，不能证明柜台接单或成交。无网络测试通过也不等于模拟盘验收通过。两个执行端均应默认禁单；未经明确授权、权威对账和风险限制，不开启新增风险，更不连接真实资金账户发单。

## Binance DEMO：已证实与待验证

- **已证实**：真实期货 1 分钟 Kline → EMA → Recording 已由用户运行 `BN_EMA_TIMEOUT=210 python -u tests/run_single_ema_online.py --stage 4` 验证通过。产生 `client_id='recording-only'` 的目标请求，行情状态 READY；没有连接账户或报单。
- **无网络测试已报告通过**：P3 账户读取、活动订单安全恢复、故障闭闸；P4-BN 全市场普通/Algo 活动订单读取。细节和测试入口见 `../../architecture/STRATEGY_ARCHITECTURE.md`。
- **真实 DEMO 尚待验收**：环境和账户身份、资金/全市场仓位/普通及 Algo 活动订单的本次权威查询、断线恢复、重新授权，以及模拟报单的受理、部分成交、撤单、拒单和回调。
- 当前只读入口：`examples/single_ema/binance_demo_readonly.py --connect-demo --seconds 30`。该探针故意禁止 `arm_demo`，不会下单。凭据环境变量是 `BINANCE_DEMO_API_KEY` 与 `BINANCE_DEMO_API_SECRET`；绝不把值写进日志或文档。
- 行情端点：U 本位期货 Kline 用 `wss://fstream.binance.com/market`，`bookTicker` 用 `/public`。闭合 1 分钟 Bar 的健康阈值须按 Bar 周期配置；目前 Stage 4 的 90 秒仅是 Recording 探针设置。

## CTP SimNow：已证实与待验证

- **无网络测试已报告通过**：P4-CTP 报单字段、TraderApi Transport/Driver 生命周期、完整权威查询、回报去重、状态恢复、自动保存、今昨仓账本核对与默认禁单。具体入口见 `tests/run_p4_ctp_*.py` 和详细台账。
- **已由用户在 SimNow 柜台验证**：第一套环境 MD `30011` 登录、订阅及新鲜 Tick/Bar → EMA Recording；第二套环境 MD `40011`、TD `40001` 登录、结算确认、资金/净仓/活动订单查询。第二套环境行情为历史回放，不能绕过 120 秒墙钟时间戳闭闸触发 EMA 自动报单。
- **第二套环境下单往返已通过**：2026-09-25 用户运行 `examples/single_ema/ctp_simnow_order_probe.py --round-trip`，`rb2701.SHFE` 买开1手限价3114收到 `ACCEPTED`、`FILLED`，成交价3114；卖平今1手限价3111收到 `ACCEPTED`、`FILLED`，成交价3113。最终柜台净仓 `rb2701=0`，旧仓 `rb2610=1` 不变，无活动订单。离线账户保护和 EMA 分阶段反手测试均通过。
- **第二套环境回放EMA执行已通过**：显式`--simnow-replay`入口核对MD/TD交易日和原始行情持续到达、测试合约初始无持仓、全账户无活动订单、其他合约旧仓不变，最多4笔订单。2026-09-25复测得到5根Bar、6份执行回报：买开1手`FILLED @3114`，卖平今1手`FILLED @3113`，卖开1手`FILLED @3111`；没有再次出现标准执行事件冲突。停机柜台为`rb2701`空头持仓1手、原有`rb2610`多头持仓1手不变。后续重启前须先核对并处理`rb2701`空头持仓，确认无活动订单。详情见`examples/single_ema/README.md`。
- **回报映射修复已复测**：首次回放EMA买开成交后，CTP成交回调`OrderSysID`为空字符串导致标准事件映射闭闸。现沿用同订单受理回报的柜台号，确实缺号时映射为`None`；离线测试和上述第二次SimNow执行均通过。
- **长期运行限制**：当前 EMA 入口尚无活动订单及今昨仓成本账本的持久化恢复；带仓或带活动订单不能直接重启。模拟柜台验收不代表真实资金账户可用。
- **单交易日持续运行入口**：`examples/single_ema/ema_ctp_simnow.py --run-forever --max-session-orders N`不设固定运行时间；报单上限、行情停滞、断线、交易日变化或对账失败均会闭闸停机，不执行带仓自动恢复。第二套环境仍须显式`--simnow-replay --expected-replay-trading-day YYYYMMDD`；第一套环境留待正常交易时段验证。

## 分级验收顺序

1. 核对当前工作树、活动文件、环境配置和账户类型；不读取或输出密钥内容。先跑无网络回归。
2. 分别完成 **只读** 柜台连接和本次权威查询。超时、缺字段、未知活动订单、账本冲突不得视为空仓/空订单，必须闭闸。
3. 完成 **Recording** 行情链。Binance Stage 4 与 CTP 第一套环境均已验证；检查事件时间、EMA 预热、目标和行情健康状态。
4. CTP 第二套环境的一手开仓/平今往返及回放EMA开仓、平今、反手均已验证；下一步是先处理回放EMA留下的测试合约空头持仓，再于第一套环境交易时段验证新鲜行情EMA受控自动报单。Binance DEMO报单仍待独立验收。
5. 验证断线与重启：自动禁单，重新取得活动订单、持仓和资金权威快照，人工重新授权后才可继续。最后比较离线回测与在线执行的**目标语义**，不要求模拟撮合价与柜台成交价完全相同。

第 1～3 级通过不能写成“实盘无缝切换完成”。每端独立验收，结果再同步到 `../../architecture/STRATEGY_ARCHITECTURE.md`。

## 新对话第一条消息

“请先读`doc/HANDOFF_SIMNOW_BINANCE_DEMO.md`和`examples/single_ema/README.md`。CTP第二套环境已完成一手往返及回放EMA策略到柜台的开仓、平今和反手链路；当前回放EMA停机留下`rb2701`空头持仓1手，先权威核对并处理后才可重启。之后在第一套环境交易时段验收新鲜行情EMA。Binance DEMO单独验收。不要把SimNow模拟成交等同于真实资金账户验收。”
