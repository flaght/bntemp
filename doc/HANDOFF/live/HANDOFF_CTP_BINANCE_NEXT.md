# CTP SimNow 与 Binance 交易接入交接（2026-09-25）

## 用户目标与项目约束

- 在 `/Users/kerry/work/bomber1/pro/examples/single_ema` 复用 **同一份** `strategies/ema_cross.py`，分别装配 CTP 与 Binance 的行情、策略目标、组合、仓位、风控和执行回报。后续目标包括 Binance 真实账户交易接入，不能只完成 CTP。
- 用户的 Linux 项目镜像为 `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro`。源代码放在对应项目内，**不要放 `/private/tmp`**；CTP 原生扩展在 `market/native/ctp/binding`，不引入 vn.py。
- 本交接依据用户贴出的运行日志和本地代码。不能把最后一次日志中的仓位、交易日或盘口当作当前柜台状态；启动/下单前重新权威查询。凭据从环境变量读取，不复制到文档或日志。

## CTP：已完成与现状

- 第一套环境：MD `tcp://182.254.243.31:30011`、TD `tcp://182.254.243.31:30001`，适合正常交易时段；用户已验证新鲜 Tick/Bar → EMA Recording，**尚未验证第一套环境 EMA 自动下单**。
- 第二套环境：MD `tcp://182.254.243.31:40011`、TD `tcp://182.254.243.31:40001`，历史交易日回放。用户最近看到 MD/TD 交易日 `20260923`，下次运行须重新核对。TD 登录、结算确认、资金/仓位/活动订单权威查询及单笔买开/卖平今往返均已通过。
- `ema_ctp_simnow.py --simnow-replay` 已在第二套环境完成真实策略链路：5 根 Bar，买开 1 手 `FILLED @3114` → 卖平今 1 手 `FILLED @3113` → 卖开 1 手 `FILLED @3111`，无执行事件冲突。最后一次日志显示 `rb2701.SHFE` **空头持仓 1 手**，原有 `rb2610.SHFE` 多头持仓 1 手不变；现在是否仍持仓未知，先运行只读探针。
- 首次回放成交曾因 CTP 成交回调 `OrderSysID` 为空导致标准事件映射失败。已修复：沿用同笔订单受理回报中的柜台号；仍缺号则传 `None`。修复后的离线测试 `tests/run_p4_ctp_driver.py`、`tests/run_ctp_simnow_order_probe.py` 及上述第二次 SimNow 执行均通过。
- 主入口 `examples/single_ema/ema_ctp_simnow.py` 已接入 `UnifiedStrategyRunner`、TargetStore、PortfolioCoordinator、PositionManager、`CtpClosePlanner`、`PreTradeRiskManager`、原生 CTP TD 和周期性柜台对账。当前仅装配单 EMA、单合约。`--run-forever` 取消固定时长，仍受 `--max-session-orders` 有限上限约束；MD 停滞、交易日变化、TD 断线、回报或对账异常会闭闸停机。带仓或有活动订单不能盲目重启，今昨仓成本、在途订单和策略状态的跨日持久化恢复未完成。
- 只读入口：`python -m scripts.integration.ctp.td_readonly --connect`；2026-10-07 有效实现已迁入 scripts 通道联调目录，旧 examples 路径仅兼容转发。已有目标合约恰好 1 手时，`ctp_simnow_order_probe.py --close-existing` 可在显式核对今昨属性、TD 交易日和限价后发送一笔反向平仓单。具体命令见 `examples/single_ema/README.md`。

## Binance：代码现状与未验收项

- **行情/信号已验证**：用户在 Binance U 本位期货公开 WebSocket 上运行 1 分钟 Kline → 同一 EMA 策略 → Recording，看到 READY 行情和目标请求；它只证明信号链，**没有真实订单**。在线行情入口为 `examples/single_ema/ema_binance_live.py`（默认 Recording）；离线 Binance 回测在 `examples/single_ema/ema_binance_backtest.py`。
- **交易代码已有，但不能写成已通过柜台验收**：`ema_binance_live.py --environment demo --enable-orders --confirm-demo` 会装配 `ControlledLiveExecutionClient`、`PreTradeRiskManager`、Nautilus/Bomber Binance TradingNode 执行客户端及 `BinanceHttpActiveOrderReader`。启动前查询 DEMO 资金/全市场仓位及普通、Algo 活动订单；非空仓或有活动订单拒绝启动。`--environment live` **目前只允许 Recording**，代码明确拒绝真实账户自动下单。
- Binance DEMO 只读探针为 `examples/single_ema/binance_demo_readonly.py --connect-demo --seconds 30`，凭据变量 `BINANCE_DEMO_API_KEY`、`BINANCE_DEMO_API_SECRET`。P3 受控客户端与 P4-BN 全市场活动订单查询的无网络测试已通过；**真实 DEMO 账户的只读查询、模拟订单受理/成交/拒单/撤单、断线恢复仍未由用户运行日志证实**。`ema_binance_live.py` 的 DEMO 下单分支也尚未端到端验收。
- **Binance 真实资金账户交易是后续独立阶段**：目前没有已验收的 LIVE 报单入口。先完成 DEMO 的本次权威账户查询和受控小单回报验收，再审查真实环境身份、全部仓位/订单、下单字段与精度、风险预算、断线/重启恢复和持久化；不能仅把 CLI 从 `demo` 改为 `live`。
- 代码位置：`market/stream/bn` 是统一行情 Feed；`examples/single_ema/binance_demo_readonly.py` 装配 DEMO TradingNode；`trader/execution/live/binance_orders.py` 查询全市场普通/Algo 活动订单；`tests/run_p3_controlled_live.py`、`tests/run_p4_binance_orders.py`、`tests/run_live_backend.py` 是无网络执行测试。详细状态见 `doc/HANDOFF_SIMNOW_BINANCE_DEMO.md` 和 `doc/architecture/STRATEGY_ARCHITECTURE.md`，但应以当前代码与新的实测日志核对。

## 新对话建议顺序

1. 先读本文件、`examples/single_ema/README.md`、`doc/HANDOFF_SIMNOW_BINANCE_DEMO.md`，核对当前工作树及已有代码，不从旧日志推断实时账户状态。
2. CTP 先只读查询第二套环境当前仓位/活动订单。若 `rb2701` 仍有空头 1 手，按当前柜台今昨属性和盘口处理；目标合约无仓、全账户无活动订单后才可再次启动 EMA。第一套环境新鲜行情自动下单留待正常交易时段。
3. **同时推进 Binance**：先运行无网络测试、再运行 Binance DEMO 只读探针并检查账户身份、资金、全市场仓位、普通/Algo 活动订单。之后修正 DEMO 装配中发现的问题并进行受控订单验收。再设计 Binance LIVE 真实账户入口及持久化/重启恢复，不要把 DEMO 成交等同于真实资金验收。
4. 对 CTP 与 Binance 的长期运行都需明确单策略/单合约范围、跨日和带仓重启、账户级组合风险预算、异常后的人工或自动恢复边界。用户希望最终能像正常策略程序一样启动，接入信号、组合、仓位、风控和交易，而非长期停留在探针阶段。

## 可作为新对话首条消息

> 请先读 `/Users/kerry/work/bomber1/pro/doc/HANDOFF_CTP_BINANCE_NEXT.md`。继续完成 `examples/single_ema` 同一 EMA 策略的 CTP 与 Binance 行情、组合、风控和交易接入。CTP 第二套 SimNow 回放 EMA 已完成开仓、平今和反手成交；最后日志留有 `rb2701` 空头 1 手，先权威查询当前状态。Binance 行情到 Recording 已通过，但 DEMO 账户/订单与真实资金账户报单均未验收；`--environment live` 当前不能下单。请同步推进 Binance DEMO 验收与 LIVE 接入设计/实现。所有代码留在项目目录，不放临时目录，不输出凭据。
