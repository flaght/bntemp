# CTP SimNow EMA 交接（2026-09-25）

> 后续还需包含 Binance DEMO 与真实账户接入；完整新对话交接请优先阅读 `HANDOFF_CTP_BINANCE_NEXT.md`。

## 目标与位置

- 项目源码：`/Users/kerry/work/bomber1/pro`；用户的 Linux 镜像目录是 `/workspace/worker/pj/Chrono/nautilus_trader_demo/pro`。代码必须留在对应项目中，不放到 `/private/tmp`。
- 复用 `examples/single_ema/strategies/ema_cross.py`：CTP MD → 1 分钟 Bar → EMA 目标 → `UnifiedStrategyRunner` 的 TargetStore/PortfolioCoordinator → PositionManager/CTP 今昨仓 Planner → PreTradeRiskManager → 原生 CTP TD → 标准执行事件与柜台对账。原生绑定位于 `market/native/ctp/binding`，不依赖 vn.py。
- 当前运行入口：`examples/single_ema/ema_ctp_simnow.py`；单笔开仓/往返/已有仓位平仓探针：`examples/single_ema/ctp_simnow_order_probe.py`；只读 TD 入口已于 2026-10-07 迁至 `scripts/integration/ctp/td_readonly.py`，旧 examples 路径仅兼容转发，见 [CTP 通道联调说明](../../../scripts/integration/ctp/README.md)。

## 已由用户在 Linux SimNow 验证

- 第一套环境 MD `tcp://182.254.243.31:30011` 提供新鲜行情，EMA Recording 目标已出现；第一套环境 TD `30001` 的 EMA 自动下单尚未验收，需在正常交易时段进行。
- 第二套环境 MD `tcp://182.254.243.31:40011`、TD `tcp://182.254.243.31:40001`。第二套环境使用历史交易日回放，用户最近看到 MD 与 TD 交易日均为 `20260923`；下次运行前必须重新查询，不能默认仍是该日。
- 原生 TD 登录、结算确认、资金/仓位/活动订单查询通过；单笔买开与卖平今往返成交通过。
- 回放 EMA 首次买开成交后，CTP 成交回调 `OrderSysID` 为空，标准事件映射报 `venue_order_id不能为空`。已修复：Driver 沿用该订单受理回报的柜台号；仍缺号则事件身份使用 `None`。更新后的 `tests/run_p4_ctp_driver.py` 和 `tests/run_ctp_simnow_order_probe.py` 已由用户在 Linux 运行通过。
- 修复后的第二次 EMA 回放：`bars_seen=5, reports=6`；买开 1 手 `FILLED @3114` → 卖平今 1 手 `FILLED @3113` → 卖开 1 手 `FILLED @3111`，无执行事件冲突。**最后一次日志**显示 `rb2701.SHFE` 空头持仓 1 手，原有 `rb2610.SHFE` 多头持仓 1 手不变。该仓位是历史快照，不代表当前状态；下一步先做只读权威查询。

## 当前入口的能力与限制

- `ema_ctp_simnow.py` 已直接接入策略信号、组合目标路由、仓位/在途量、单合约风控、CTP 报单、执行回报及周期性资金/多空总仓对账。当前只装配 **一个 EMA 策略和一个合约**；不是多策略账户级风险预算系统。
- 第二套环境需显式 `--simnow-replay --expected-replay-trading-day YYYYMMDD`；仅在 MD/TD 交易日一致、原始行情最近 10 秒内到达时放行旧事件时间。正常模式仍检查信号与墙钟 120 秒时效。
- `--run-forever` 表示不按固定秒数停机，仍有 `--max-session-orders` 有限报单上限（持续模式可设 1～100）。MD 停滞、交易日变化、TD 断线、回报冲突或对账失败都会闭闸停机并尝试撤销本策略活动订单。它不是跨日自动恢复服务。
- 启动要求目标合约 **无持仓**、全账户 **无活动订单**。第二套环境旧合约仓位可显式 `--allow-other-positions`，运行期间该仓位必须不变。尚未实现带仓/带活动订单重启、今昨仓成本与策略状态持久化恢复；停机可能留仓，不能盲目重启。
- `ctp_simnow_order_probe.py --close-existing` 可对恰好 1 手的已有目标仓位执行显式平仓：空头用 `--side BUY`，多头用 `--side SELL`；今/昨仓属性由用户核对后指定 `--position-effect CLOSE_TODAY` 或 `CLOSE_YESTERDAY`。须同时指定 TD 实际交易日和限价；发单需要 `--submit --confirm-simnow`。

## 新对话先做什么

1. 先读本文件及 `examples/single_ema/README.md` 的 CTP 段；在 Linux 用 `ctp_td_readonly.py --connect` 查询 **当前** 柜台仓位和活动订单，确认上次的 `rb2701` 空头是否仍存在。凭据仅从项目 `.env`/环境变量读取，不输出密码或认证码。
2. 若要再次启动 EMA，先核对并处理目标合约留仓；用 `tests/run_ctp_live.py --stage 4` 获取 **当前** MD 交易日、盘口与涨跌停；用只读预检再执行 `--close-existing`。不要凭旧日志选择价格、今昨仓或交易日。
3. 用户的当前问题是：能否把 `examples/single_ema` 当作正常启动的策略信号、仓位、风控、组合管理程序。准确答复：**单策略单合约的单交易日 SimNow 运行链已具备且成交验收通过；多策略组合级风控、带仓重启、订单/策略状态恢复和跨日自动运行仍未完成。** 若用户要求这些能力，继续在项目内实现，并用无网络测试与 SimNow 分阶段验收。
4. 第一套环境新鲜行情 EMA 自动下单与 Binance DEMO 报单分别待独立验收。不要把 SimNow 验收描述为真实资金账户验收。

## 参考命令（运行前核对交易日、目标仓位和活动订单）

```bash
CTP_TD_ADDRESS='tcp://182.254.243.31:40001' CTP_PRODUCTION_MODE=1 \
python -u -m scripts.integration.ctp.td_readonly --connect --timeout 30

CTP_MD_ADDRESS='tcp://182.254.243.31:40011' CTP_SYMBOL=rb2701 \
CTP_PRODUCTION_MODE=1 CTP_MARKET_SAMPLE_SECONDS=5 \
python -u tests/run_ctp_live.py --stage 4
```

详细限时/持续运行、已有仓位平仓命令见 `examples/single_ema/README.md`；不要直接复制旧日期或旧限价。
