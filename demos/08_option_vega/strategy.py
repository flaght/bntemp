"""认购期权卖方与同月期货对冲：管理真实持仓、在途订单和清仓。"""
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from bomber.framework.trader.template import StrategyTemplate
from bomber.framework.trader.execution.option_settlement import SETTLEMENT_ORIGIN
from .vega_signal import remaining_trading_days, allocate_short, source_iv, source_years, source_greeks


class OptionVegaStrategy(StrategyTemplate):
    """纯数值函数负责选约配仓，策略负责行情时序和实际敞口执行。"""
    def __init__(self, strategy_id, config, contracts, futures, instruments,
                 index_id, trading_days, final_day=None, options_by_day=None, *,
                 option_last_trade_times=None):
        super().__init__(strategy_id)
        self.config = config
        self.contracts = dict(contracts)
        self.futures = dict(futures)
        self.instruments = dict(instruments)
        self.index_id = index_id
        self.trading_days = tuple(trading_days)
        self.final_day = final_day
        self.options_by_day = options_by_day or {}
        self.option_last_trade_times = dict(option_last_trade_times or {})
        if ((option_last_trade_times is not None and set(self.option_last_trade_times) != set(self.contracts))
                or any(type(stamp) is not int or stamp < 0 for stamp in self.option_last_trade_times.values())):
            raise ValueError("现金结算生命周期须覆盖全部期权，时间须为非负整数纳秒")
        self.restored = False
        self.stopping = False
        self.order_updates_received = 0
        self.fills_received = 0
        self.latest = {}
        self.pending_ts = None
        self.resume_ns = None
        self.last_event_ns = -1
        self.option_goals = {key: Decimal(0) for key in self.contracts}
        self.future_goals = {key: Decimal(0) for key in self.futures}
        self.iv_cache = {}
        self.sent_days = set()
        self.hedge_sent = set()
        self.submissions = 0
        self.entry_decisions = 0
        self.entry_audit = []
        self.frames = 0
        self.signals = []
        self.flatten_requested = False
        self.last_decision = {}

    def on_bar(self, data_key, bar):
        """进入新分钟时处理前一完整分钟，确保决策不读取尚未送达的行情。"""
        timestamp = bar.ts_event
        if timestamp < self.last_event_ns:
            raise ValueError("行情时间倒退")
        if self.pending_ts is not None and timestamp > self.pending_ts:
            # 此时上一分钟全体行情已到齐；新分钟行情尚未加入策略缓存。
            if timestamp - self.pending_ts <= self.config.max_market_age_seconds * 1_000_000_000:
                self._frame(self.pending_ts, timestamp)
            else:
                self.resume_ns = timestamp
                self.signals.append({"signal_ns": self.pending_ts, "request_ns": timestamp,
                                     "action": "GAP_FRAME_SKIPPED"})
        self.last_event_ns = timestamp
        self.pending_ts = timestamp
        if data_key in self.latest and self.latest[data_key][0] == timestamp:
            raise ValueError(f"同一分钟重复行情: {data_key}")
        self.latest[data_key] = (timestamp, float(bar.close.as_decimal()))

    def on_market_frame(self, frame):
        """所有输入已封闭；来源和时间适配由市场层完成，不读接收时钟估值。"""
        timestamp = frame.valuation_ns
        if timestamp <= self.last_event_ns:
            raise ValueError("行情帧时间倒退或重复")
        if self.last_event_ns >= 0 and timestamp - self.last_event_ns > self.config.max_market_age_seconds * 1_000_000_000:
            self.resume_ns = timestamp
        if self.restored:
            # 恢复已成交事实，不继续旧会话尚未完成的开仓目标。
            self.option_goals = {key: self.position(str(self.instruments[key])) for key in self.contracts}
            self.future_goals = {key: self.position(str(self.instruments[key])) for key in self.futures}
            self.resume_ns = timestamp
            self.restored = False
        self.last_event_ns = timestamp
        for key, observation in frame.observations.items():
            self.latest[key] = (timestamp, float(observation.price))
        self._frame(timestamp, frame.request_ns)

    def required_market_keys(self):
        required = {key for key, instrument in self.instruments.items()
            if self.position(str(instrument)) or self.strategy_working_quantity(str(instrument))
            or self.option_goals.get(key) or self.future_goals.get(key)}
        held = required.intersection(self.contracts)
        required.add(self.index_id)
        required.update(self.contracts[key].future for key in held)
        return required

    def on_fill(self, event):
        self.fills_received += 1
        if event.metadata.get("execution_origin") == SETTLEMENT_ORIGIN:
            key = next((key for key, instrument in self.instruments.items()
                        if str(instrument) == event.identity.instrument_id), None)
            if key not in self.option_last_trade_times:
                raise RuntimeError("收到未声明现金结算生命周期的成交")
            if event.ts_event < self.option_last_trade_times[key] or self.position(str(self.instruments[key])):
                raise RuntimeError("现金结算尚未权威了结本策略仓位")
            # 归属账本已经按真实结算回报更新；这里只清理业务目标，不伪造成交或重入下单。
            self.option_goals[key] = Decimal(0)
            self.iv_cache.pop(key, None)
            self.latest.pop(key, None)
            self.signals.append({"signal_ns": event.ts_event, "request_ns": event.ts_event,
                "action": "OPTION_CASH_SETTLEMENT", "instrument": key,
                "quantity": str(event.quantity), "price": str(event.price)})

    def _remaining_days(self, day, last_day, threshold=None):
        """按已知交易日检查开仓或退出阈值，不把未来日期缺失视为临近到期。"""
        return remaining_trading_days(self.trading_days, day, last_day,
            self.config.min_remaining_days if threshold is None else threshold)

    def _price(self, symbol, signal_ns):
        """查询新鲜真实价格，缺失时附带时间、仓位和在途数量报错。"""
        value = self.latest.get(symbol)
        if value is None or not 0 <= signal_ns - value[0] <= self.config.max_market_age_seconds * 1_000_000_000:
            signal_time = datetime.fromtimestamp(signal_ns / 1e9, ZoneInfo("Asia/Shanghai"))
            last_time = None if value is None else datetime.fromtimestamp(value[0] / 1e9, ZoneInfo("Asia/Shanghai"))
            age = None if value is None else (signal_ns - value[0]) / 1e9
            instrument = self.instruments.get(symbol)
            position = None if instrument is None else self.position(str(instrument))
            working = None if instrument is None else self.strategy_working_quantity(str(instrument))
            goal = self.option_goals.get(symbol, self.future_goals.get(symbol))
            raise RuntimeError(
                f"持仓计算缺少新鲜行情: {symbol}; 决策时间={signal_time.isoformat()} "
                f"最新报价时间={None if last_time is None else last_time.isoformat()} "
                f"报价年龄秒={age} 限制秒={self.config.max_market_age_seconds} "
                f"实际仓位={position} 目标仓位={goal} 在途数量={working}"
            )
        return value[1]

    def _greeks(self, symbol, signal_ns, now, *, hedge=False):
        """以同月期货和期权价格反解风险指标，必要时使用有限期波动率缓存。"""
        info = self.contracts[symbol]
        forward = self._price(info.future, signal_ns)
        premium = self._price(symbol, signal_ns)
        if info.last_day > self.trading_days[-1]:
            raise ValueError(f"{symbol}IV需要完整交易日期限，日历须覆盖最后交易日{info.last_day}")
        days = self._remaining_days(now.date(), info.last_day)
        years = source_years(now, days, hedge=hedge)
        greeks = None
        if premium - max(forward - info.strike, 0) >= self.config.min_time_value:
            iv = source_iv(forward, info.strike, years, self.config.rate, premium)
            if iv is not None:
                greeks = source_greeks(forward, info.strike, years, self.config.rate, iv,
                    (info.last_day-now.date()).days/365)
        if greeks is not None:
            self.iv_cache[symbol] = (signal_ns, greeks.iv)
        else:
            cached = self.iv_cache.get(symbol)
            if cached and signal_ns - cached[0] <= self.config.max_iv_age_seconds * 1_000_000_000:
                greeks = source_greeks(forward, info.strike, years, self.config.rate, cached[1],
                    (info.last_day-now.date()).days/365)
        return greeks

    def _frame(self, signal_ns, request_ns):
        """按实际仓位检查退出、每日卖方选约、期货对冲及末日清仓。"""
        self.frames += 1
        now = datetime.fromtimestamp(signal_ns / 1e9, ZoneInfo("Asia/Shanghai"))
        day, slot = now.date(), now.strftime("%H:%M")
        actual = {key: self.position(str(instrument)) for key, instrument in self.instruments.items()}
        working = {key: self.strategy_working_quantity(str(instrument)) for key, instrument in self.instruments.items()}
        expired = {key for key, stamp in self.option_last_trade_times.items() if request_ns >= stamp}
        # 已停止交易的残仓交给公共结算模块。保留真实数量，不能发普通平仓或重开单。
        for key in expired:
            self.option_goals[key] = actual[key]
        self.last_decision = decision = {
            "logic_version": "strategy1-noddb-v1", "status": "EVALUATING", "positions": {k: str(v) for k, v in actual.items() if v},
            "working": {k: str(v) for k, v in working.items() if v},
            "entry_slot": self.config.option_slot,
            "entry": "ALREADY_SELECTED" if day in self.sent_days else
                     "DUE" if slot == self.config.option_slot else
                     "WAIT_SLOT" if slot < self.config.option_slot else "SLOT_PASSED",
            "hedge": "NOT_EVALUATED", "exit_checks": [],
            "awaiting_settlement": sorted(key for key in expired if actual[key])}

        if self.resume_ns is not None:
            # 隔夜/午休恢复时，各腿首Bar可相差一分钟；等待更新，不用上一场次报价。
            required = {key for key in self.instruments if actual[key] or working[key]
                        or self.option_goals.get(key) or self.future_goals.get(key)}
            required.difference_update(expired)
            held_options = required.intersection(self.contracts)
            if held_options:
                required.add(self.index_id)
                required.update(self.contracts[key].future for key in held_options)
            missing = [key for key in sorted(required)
                       if key not in self.latest or not self.resume_ns <= self.latest[key][0] <= signal_ns
                       or signal_ns - self.latest[key][0] > self.config.max_market_age_seconds * 1_000_000_000]
            if missing:
                if signal_ns - self.resume_ns <= self.config.max_market_age_seconds * 1_000_000_000:
                    decision.update(status="RESUME_WAIT", missing=missing)
                    self.signals.append({"signal_ns": signal_ns, "request_ns": request_ns,
                                         "action": "RESUME_WAIT", "missing": missing})
                    return
                self._price(missing[0], signal_ns)  # 超时后输出含时间/仓位的诊断。
                raise RuntimeError(f"场次恢复后行情仍未更新: {missing}")
            self.resume_ns = None

        if day == self.final_day and slot >= self.config.flatten_slot:
            self.flatten_requested = True
            if not any(working.values()):
                if (any(q for key, q in actual.items() if key not in expired)
                        or any(q for key, q in self.option_goals.items() if key not in expired)
                        or any(self.future_goals.values())):
                    for key, position in actual.items():
                        if position and key not in expired:
                            self._price(key, signal_ns)
                    self.option_goals = {key: actual[key] if key in expired else Decimal(0) for key in self.contracts}
                    self.future_goals = {key: Decimal(0) for key in self.futures}
                    targets = {str(self.instruments[key]): value for key, value in
                               {**self.option_goals, **self.future_goals}.items()}
                    self._submit(targets, request_ns, signal_ns, "FINAL_FLAT")
            decision.update(status="FINAL_FLAT_CHECK" if decision["status"] == "EVALUATING" else decision["status"])
            return

        changed = False
        # 到期仍有实际仓位必须核验结算，不能像原脚本删除字典冒充平仓。
        for key in self.contracts:
            if day > self.contracts[key].last_day and actual[key] and key not in expired:
                raise RuntimeError(f"{key}已过最后交易日但仍有实际持仓")
        option_due = slot == self.config.option_slot and day not in self.sent_days
        if slot == self.config.option_slot and day not in self.sent_days:
            audit = {"signal_ns": signal_ns, "request_ns": request_ns, "date": str(day),
                "slot": slot, "outcome": "WORKING_ORDERS", "candidate_count": 0, "targets": {}}
            self.entry_audit.append(audit)
            del self.entry_audit[:-32]
            if not any(working[key] for key in self.contracts):
                index = self._price(self.index_id, signal_ns)
                candidates = []
                eligible = [key for key in self.options_by_day.get(day, ())
                    if key not in expired and self.contracts[key].list_day <= day <= self.contracts[key].last_day
                    and self.contracts[key].strike > index
                    and self._remaining_days(day, self.contracts[key].last_day) >= self.config.min_remaining_days
                    and all(self.latest.get(name, (None,))[0] == signal_ns
                        for name in (key, self.contracts[key].future, self.index_id))]
                nearest = min((self.contracts[key].last_day for key in eligible), default=None)
                for key in eligible:
                    if self.contracts[key].last_day != nearest:
                        continue
                    info = self.contracts[key]
                    greeks = self._greeks(key, signal_ns, now)
                    if greeks is not None:
                        candidates.append((key, greeks, info.multiplier))
                self.sent_days.add(day)
                audit["candidate_count"] = len(candidates)
                audit["outcome"] = "NO_VALID_ENTRY"
                if candidates:
                    goals = allocate_short(candidates, self.config.delta_targets, self.config.vega_cash_per_unit,
                                           self.config.max_option_lots, self.config.max_total_option_lots)
                    audit["outcome"] = "TARGET" if goals else "ZERO_ALLOCATION"
                    audit["increments"] = {key: str(value) for key, value in goals.items()}
                    audit["targets"] = dict(audit["increments"])
                    audit["future_bindings"] = {key: self.contracts[key].future for key in goals}
                    if goals:
                        # strategy1：每日新增数量累加，其他期权旧仓不被新选约替换。
                        proposed = {key: self.option_goals[key] + Decimal(goals.get(key, 0)) for key in self.contracts}
                        if (any(abs(q) > self.config.max_option_lots for q in proposed.values())
                                or sum(abs(q) for q in proposed.values()) > self.config.max_total_option_lots):
                            audit["outcome"] = "RISK_LIMIT"
                            audit["rejected_increments"] = audit["targets"]
                            audit["targets"] = {}
                        else:
                            self.option_goals = proposed
                            self.entry_decisions += 1
                            changed = True
                else:
                    self.signals.append({"signal_ns": signal_ns, "request_ns": request_ns,
                                         "action": "NO_VALID_ENTRY", "date": str(day)})

        # strategy1在期权交易时点先增仓，再评估所有期权的时间价值退出。
        if option_due and not any(working[key] for key in self.contracts):
            self.sent_days.add(day)
            for key, goal in tuple(self.option_goals.items()):
                if not goal or key in expired:
                    continue
                info = self.contracts[key]
                close = self._price(key, signal_ns)
                forward = self._price(info.future, signal_ns)
                time_value = close - (forward-info.strike)
                should_close = time_value <= 0
                decision["exit_checks"].append({"key": key, "price": close, "future": info.future,
                    "future_price": forward, "time_value": time_value, "should_close": should_close,
                    "rule": "source_premium_minus_future_minus_strike"})
                if should_close:
                    self.option_goals[key] = Decimal(0)
                    changed = True

        if self.entry_audit and self.entry_audit[-1]["signal_ns"] == signal_ns:
            decision["entry"] = self.entry_audit[-1]["outcome"]
            self.entry_audit[-1]["option_goals"] = {key: str(q) for key, q in self.option_goals.items() if q}
            decision["entry_audit"] = dict(self.entry_audit[-1])
        hedge_token = f"{day}_{slot}"
        hedge_due = slot in self.config.hedge_slots and hedge_token not in self.hedge_sent
        decision["hedge_reasons"] = ["CONFIGURED_SLOT"] if hedge_due else []
        decision["hedge"] = ("WAIT_WORKING_ORDERS" if hedge_due and any(working[key] for key in self.futures)
                             else "EVALUATED" if hedge_due else "NOT_DUE")
        if hedge_due and not any(working[key] for key in self.futures):
            self.hedge_sent.add(hedge_token)
            totals = {key: 0.0 for key in self.futures}
            closing_without_iv = set()
            for key in self.contracts:
                if not actual[key] or key in expired:
                    continue
                greeks = self._greeks(key, signal_ns, now, hedge=True)
                info = self.contracts[key]
                if greeks is None:
                    # 源代码跳过无IV合约；实盘保留该月份既有对冲，不因缺值清零。
                    closing_without_iv.add(info.future)
                    decision.setdefault("hedge_missing_iv", []).append(key)
                    continue
                totals[info.future] += float(actual[key]) * float(info.multiplier) * greeks.delta
            goals = {}
            for key, exposure in totals.items():
                if key in closing_without_iv:
                    goals[key] = self.future_goals[key]  # 先平期权，保留既有对冲直到实际平仓。
                    continue
                lots = round(-exposure / float(self.futures[key]["contMultNum"]))
                if abs(lots) > self.config.max_future_lots:
                    raise RuntimeError(f"{key}所需对冲{lots}手超过上限，请降低Vega预算")
                goals[key] = Decimal(lots)
            changed = changed or goals != self.future_goals
            self.future_goals = goals

        decision["status"] = "NO_TARGET_CHANGE"
        if changed:
            targets = {str(self.instruments[key]): value for key, value in
                       {**self.option_goals, **self.future_goals}.items()}
            # 显式带零目标，包括换月旧腿；组合提交不代表多腿原子成交。
            self._submit(targets, request_ns, signal_ns, "REBALANCE")

    def _submit(self, targets, request_ns, signal_ns, action):
        """提交完整目标，保留信号时间与请求时间以核对决策时序。"""
        self.set_targets(targets, request_ns, metadata={"signal_ts": signal_ns, "action": action})
        self.last_decision.update(status="TARGET_SUBMITTED", action=action,
            targets={key: str(value) for key, value in targets.items() if value
                     or self.last_decision.get("positions", {}).get(key.rsplit(".", 1)[0])})
        self.submissions += 1
        self.signals.append({"signal_ns": signal_ns, "request_ns": request_ns,
                             "action": action, "targets": {key: str(value) for key, value in targets.items() if value}})

    def on_order_update(self, event):
        """订单拒绝、取消或过期时停止，避免静默留下不完整对冲。"""
        self.order_updates_received += 1
        status = getattr(event.status, "value", str(event.status))
        cutoff = next((stamp for key, stamp in self.option_last_trade_times.items()
                       if str(self.instruments[key]) == event.identity.instrument_id), None)
        if status in ("CANCELED", "EXPIRED") and cutoff is not None and event.ts_event >= cutoff:
            return  # 公共生命周期撤销到期余单；拒单仍是硬错误。
        if status == "REJECTED" or (not self.stopping and status in ("CANCELED", "EXPIRED")):
            raise RuntimeError(f"订单{status}: {event.identity.instrument_id} {event.reason}")


def risk_limits(config, contracts, instruments, multipliers, max_notional):
    from bomber.framework.trader.execution.risk import RiskLimits
    return {instrument: RiskLimits(
        max_order_quantity=(config.max_option_lots if key in contracts else config.max_future_lots) * 2,
        max_abs_position=config.max_option_lots if key in contracts else config.max_future_lots,
        max_order_notional=max_notional, max_abs_position_notional=max_notional,
        max_market_age_ns=config.max_market_age_seconds * 1_000_000_000,
        contract_multiplier=multipliers[instrument]) for key, instrument in instruments.items()}


def assemble_strategy_components(runner, execution, *, config, contracts, futures, instruments,
        index_key, index_id, trading_days, options_by_day, feeds, feed_by_key,
        final_day=None, strategy_id="option-vega", option_last_trade_times=None,
        bar_specs_by_key=None):
    from bomber.framework.market.basic.base import DataType
    from bomber.framework.trader.assembly import StrategyBindings, assemble_strategy
    from bomber.framework.trader.contracts import DataBinding, ExecutionRoute
    all_ids = {**instruments, index_key: index_id}
    if set(feed_by_key) != set(all_ids):
        raise ValueError("行情绑定须覆盖期权、期货及指数")
    if bar_specs_by_key is not None and set(bar_specs_by_key) != set(all_ids):
        raise ValueError("行情周期须覆盖期权、期货及指数")
    strategy = OptionVegaStrategy(strategy_id, config, contracts, futures, instruments,
        index_key, trading_days, final_day, options_by_day,
        option_last_trade_times=option_last_trade_times)
    bindings = StrategyBindings(
        data=tuple(DataBinding(key, feed_by_key[key], item, DataType.BAR,
            "1-MINUTE" if bar_specs_by_key is None else bar_specs_by_key[key])
            for key, item in all_ids.items()),
        execution=tuple(ExecutionRoute(str(item), execution.client.client_id, item)
            for item in instruments.values()))
    assemble_strategy(runner, strategy, feeds=feeds, execution=execution, bindings=bindings)
    return strategy
