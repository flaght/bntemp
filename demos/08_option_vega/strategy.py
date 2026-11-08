"""认购期权卖方Vega预算策略：决策、归属仓位管理与执行回报处理。

职责边界：
    本文件消费已经标准化的行情，提交本策略的完整目标组合，不连接数据库、
    创建Feed或装配执行路由。回测由run_backtest.py装配，Live由live.py装配。
    IV、Greeks、交易日期限及配仓公式复用vega_signal.py；订单差额、成交归属、
    风控和到期现金结算由公共框架负责。

阅读时区分三类数量：
    actual：框架已记账的本策略净持仓，卖出期权为空头负数；不代表全账户总仓。
    working：本策略仍在途的带方向委托数量；目标已提交不等于已经成交。
    option_goals/future_goals：业务希望达到的目标持仓，不是本次买卖增量。
    Recording只记录目标，没有成交，actual仍可能全为零；Model的actual来自模型成交。

时钟约定：
    signal_ns/valuation_ns是本次行情估值时点；request_ns是提交/消费帧的时点。
    日内时点判断使用Asia/Shanghai的估值时间，不把接收时间当作行情时间。
    每个封闭帧都会检查决策条件，但选约/常规退出只在option_slot，Delta对冲只在
    hedge_slots，主动清仓只在显式final_day的flatten_slot及之后执行。

策略逻辑阅读顺序：
    先看类说明中的业务规则和例子，再看_frame中的执行顺序；定价和配仓公式
    的展开在vega_signal.py的source_years/source_iv/source_greeks/allocate_short。
    注释描述当前实现，示例数字仅用于解释代码，不表示回测或实盘成交结果。
"""
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from bomber.framework.trader.template import StrategyTemplate
from bomber.framework.trader.execution.option_settlement import SETTLEMENT_ORIGIN
from .vega_signal import remaining_trading_days, allocate_short, source_iv, source_years, source_greeks


class OptionVegaStrategy(StrategyTemplate):
    """按源strategy1口径管理认购卖方组合，以及各月份的期货Delta对冲。

    指数用于筛选执行价；IV与Delta使用期权对应的同月期货，而非指数替代值。
    每日选出的空头增量累加到原目标，旧仓不会因选出新合约自动被替换。
    到期生命周期已交由公共结算模块的合约，不再参与普通平仓或对冲计算。

    一、做什么、靠什么信号决定
        在每天的指定时点卖出一组认购期权，以现金Vega预算决定新增手数，
        并在指定对冲时点用同月期货调整已成交期权组合的Delta敞口。
        这不是指数突破、均线金叉或IV高于历史波动率才触发的策略；当前代码
        没有这些开仓条件。指数特征I的直接作用是筛选K>I的执行价。
        即使I来自MA20或(MA20+MA60)/2，IV和Delta的定价输入F仍是同月期货。
        因而K>I不一定等同于相对同月期货K>F，两种价格不能混用。

    二、每天什么时候选，选哪个月、哪些执行价
        只有slot == config.option_slot且该日不在sent_days时尝试选约；
        若任何期权仍有在途委托，本帧不选、不退出，结果为WORKING_ORDERS。
        模板JSON目前选择14:00；不加载模板时以OptionVegaConfig默认值为准。
        错过时点不会在14:01或下一根M5上补做；已有仓位继续按其他条件管理。

        当天options_by_day的合约依次满足：未过停止交易时点、处于上市区间、
        K>I、剩余交易日不少于min_remaining_days、期权/同月期货/指数三个
        缓存时间都等于signal_ns。然后在这些合约中取最近last_day的月份。
        最近合格月可能因原月整体不满足条件而改变；这不是按main字段选主力。
        若所选月的Greeks全不可用，不会再尝试下一个月。

        在该月有效Greeks候选中，每个delta_targets档选择Delta距离最小的
        一张认购。目标Delta是期权合约本身的正Delta，不是卖出后的负敞口。
        一个合约可同时命中多个档；先合并手数再取整，不要求每档选不同合约。

    三、Vega预算如何变成卖出手数
        B = float(config.nav) * config.vega_budget / 100，是sigma变化1.0时
        的现金Vega尺度；vega_greeks也是对sigma的单位1.0敏感度。
        例如nav=40,000,000、vega_budget=1.25时，B=500,000；对应波动率
        上升1个百分点（sigma增加0.01）的线性现金敏感度尺度为5,000。
        nav在这里是固定配置值，没有自动跟随账户净值复利调整。

        对选中的第j个Delta档：w_j=1/(Gamma_j*m_j)，
        D=sum(w_j*Vega_j*m_j)，原始新增手数a_j=B*w_j/D，m_j为期权乘数。
        理想未取整时sum(a_j*Vega_j*m_j)=B；预算由各档共享，不是每档各用B。
        相同合约的a_j先求和，再round、单合约截限；总手数超限时同比缩放并
        向下取整数。最终返回负数增量，细节见allocate_short。
        因为整手、手数上限和行情变化，最终实际Vega不保证恰好等于预算。

    四、新仓与旧仓如何共存，什么时候退出
        每日目标 = 昨日/已有目标 + 当日负增量；不是每日替换成同一固定空头仓。
        例如原目标MO-A=-1，本日新增MO-B=-1，合并后目标为A=-1、B=-1。
        若本日又选中A，原目标-1加新增-1会变成-2；若单合约上限为1，整批
        新增被拒绝，entry=RISK_LIMIT。这里控制的是累计目标，不是累计现金Vega。
        每日新分配B也不是用B减去整个旧组合的Vega后再补足，旧仓可跨日叠加。

        当天选约处理后，在同一option_slot检查所有非零、未截止期权目标：
        源策略退出指标E=C-(F-K)。若E<=0，目标置0，交执行端平仓；
        E与_greeks筛选用的C-max(F-K,0)不同，保留两处源代码各自的口径。
        例如F=7,100、K=7,000、C=100，则E=0，置零；若F=6,800、
        K=7,000、C=30，则E=230，不退出。close_remaining_days不是此退出条件。
        检查的是更新后的目标，包含当天新选目标；增仓与退出最终合并为一次提交，
        不强制先发开仓再发平仓。没有新候选时，仍可检查旧目标是否需要退出。

    五、同月期货如何对冲
        仅在hedge_slots且该日/时点尚未记入hedge_sent时评估。任何期货有在途
        委托时本帧不重算，避免覆盖未结束委托；该条件覆盖本策略全部期货腿。
        对每个月份f，用已经成交的期权手数q_i汇总：
            exposure_f=sum(q_i*m_i*Delta_i)
            future_goal_f=round(-exposure_f/future_multiplier_f)
        例如空1手认购、乘数100、Delta=0.30、期货乘数200：
        exposure=-30，对冲目标round(0.15)=0，所以有期权仓而无期货仓是可能的。
        若q=-10且其他值不变，round(1.5)=2；还必须通过max_future_lots限制。
        上限不够时直接报错，不把2截成1并继续。round沿用Python的半偶舍入。

        对冲手数是最终期货目标，不是本次买卖量；执行端处理已有期货的差额。
        同一帧新选/退出只改变目标，actual快照尚未改变；因此不提前对冲未成交
        新仓，也不假定待平仓期权已消失。成交后通常等下一配置对冲时点重算，
        on_fill本身不会马上触发一次Delta对冲。
        同月任一期权拿不到IV时，保留该月原期货目标，不将未知敞口视为零；
        该时点已标记处理，通常等下一对冲时点，不在后续任意分钟自动补对冲。

    六、什么时候不下单，如何核对日志
        WAIT_SLOT/ALREADY_SELECTED描述每日选约阶段，并不等于策略停止管理。
        NO_VALID_ENTRY：选中月无可用候选；ZERO_ALLOCATION：配仓取整等处理后为空；
        RISK_LIMIT：新增累加后越过手数上限；这些选约结果均不代表已成交。
        即使entry不是TARGET，退出或期货对冲也可能改变目标并产生委托。
        NO_TARGET_CHANGE表示本帧没有触发新的目标提交，不表示actual一定为零。
        TARGET_SUBMITTED只表示完整目标已交框架，订单数/成交数需看独立回报。
        RESUME_WAIT表示恢复后依赖行情未齐，本帧提前返回，不进行选约或对冲。

        final_day及flatten_slot触发的主动清仓优先于选约/对冲；
        flatten_requested仅表示进入清仓分支，不是所有成交仓位已平的证明。
        进程按日历收盘保存与显式最终清仓是两个动作；前者本身不会清空持仓。
        对已截止交易的期权，保留实际数量等待公共结算回报，不能删除本地字典
        就认定风险消失。恢复时沿用本策略成交归属，不将账户其他策略仓位纳入。
    """
    def __init__(self, strategy_id, config, contracts, futures, instruments,
                 index_id, trading_days, final_day=None, options_by_day=None, *,
                 option_last_trade_times=None):
        """接收外部准备的条款、路由和日历，不在策略内读取文件或猜测合约。

        contracts：symbol→Contract，含执行价、乘数、上市/最后交易日及同月future。
        futures：期货symbol→基础条款；对冲手数使用其中的contMultNum乘数。
        instruments：可交易symbol→InstrumentId，供持仓查询及目标提交使用。
        index_id：指数在策略内的数据键（如000852），此参数不是交易InstrumentId。
        trading_days：有序交易日期，用于剩余交易日及IV期限计算。
        final_day：可选主动清仓日；省略时不会因为进程结束而自动平仓。
        options_by_day：每日候选symbol集合，外部已完成文件/条款/对冲资格准备。
        option_last_trade_times：可选的期权停止交易纳秒时点，须覆盖全部期权；
            使用request_ns判断是否已越过截止时点，防止迟到信号产生到期后订单。
        """
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
        # 生命周期标记由外部恢复/停机流程维护；restored在首个帧中消费一次。
        self.restored = False
        self.stopping = False
        self.order_updates_received = 0
        self.fills_received = 0
        # 缓存值为(估值纳秒, 收盘价格)。资金/数量仍用Decimal；定价公式使用float。
        self.latest = {}
        # pending_ts供逐Bar入口延后封闭；resume_ns要求所有持仓依赖更新到恢复之后。
        self.pending_ts = None
        self.resume_ns = None
        self.last_event_ns = -1
        self.option_goals = {key: Decimal(0) for key in self.contracts}
        self.future_goals = {key: Decimal(0) for key in self.futures}
        # IV只缓存(计算时点, 波动率)，使用时会按当前价格/期限重新计算Greeks。
        self.iv_cache = {}
        # 防止同日重复选约、同一日内对冲时点重复执行；恢复组件负责保存这些标记。
        self.sent_days = set()
        self.hedge_sent = set()
        self.submissions = 0
        self.entry_decisions = 0
        self.entry_audit = []
        self.frames = 0
        # 诊断记录：entry_audit保留最近32次选约，signals记录目标动作和等待原因。
        self.signals = []
        self.flatten_requested = False
        self.last_decision = {}

    def on_bar(self, data_key, bar):
        """逐Bar入口：遇到新时间戳时决策上一时点，随后才缓存当前Bar。

        相同时间戳的不同合约可以依次到达，同一数据键不能重复。此入口依赖
        上游的事件排序；时间戳推进本身不保证所有腿齐全，所需价格仍须校验。
        与on_market_frame是两种输入路径，外部不应把同一批行情重复送入两者。
        """
        timestamp = bar.ts_event
        if timestamp < self.last_event_ns:
            raise ValueError("行情时间倒退")
        if self.pending_ts is not None and timestamp > self.pending_ts:
            # 先处理已有缓存，当前新Bar尚不可见，避免上一时点决策读取未来价格。
            if timestamp - self.pending_ts <= self.config.max_market_age_seconds * 1_000_000_000:
                self._frame(self.pending_ts, timestamp)
            else:
                # 午休/隔夜等大缺口不补做旧时点决策，下次决策先等待新场次依赖。
                self.resume_ns = timestamp
                self.signals.append({"signal_ns": self.pending_ts, "request_ns": timestamp,
                                     "action": "GAP_FRAME_SKIPPED"})
        self.last_event_ns = timestamp
        self.pending_ts = timestamp
        if data_key in self.latest and self.latest[data_key][0] == timestamp:
            raise ValueError(f"同一分钟重复行情: {data_key}")
        self.latest[data_key] = (timestamp, float(bar.close.as_decimal()))

    def on_market_frame(self, frame):
        """帧入口：消费上游按接收期限封闭的观测集合，然后进行一次决策。

        封闭表示本帧不再补入迟到观测，不等于所有候选期权都有Bar。M5期权配合
        M1期货/指数时，某些帧只有M1观测；旧期权缓存保留原时间，不伪造M1数据。
        """
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
            # 只更新本帧实际包含的键；缺失键的缓存和时间不会被刷新。
            self.latest[key] = (timestamp, float(observation.price))
        self._frame(timestamp, frame.request_ns)

    def required_market_keys(self):
        """给上游健康检查提供必需腿：有仓、有在途或有目标的合约及其依赖。

        指数始终必需；相关期权的同月期货也必需。未持有、无目标的候选期权
        不必每分钟都有行情，真正选约时再要求三腿严格同一估值时点。
        """
        required = {key for key, instrument in self.instruments.items()
            if self.position(str(instrument)) or self.strategy_working_quantity(str(instrument))
            or self.option_goals.get(key) or self.future_goals.get(key)}
        held = required.intersection(self.contracts)
        required.add(self.index_id)
        required.update(self.contracts[key].future for key in held)
        return required

    def on_fill(self, event):
        """成交归属已由框架记账，此回调统计成交，并清理权威结算后的业务状态。

        普通成交无需在这里手动修改actual，避免重复记账。现金结算事件必须
        晚于声明的停止交易时点，且该期权的本策略净持仓已经由框架归零。
        """
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
        """统计day之后、截至last_day的交易日数，默认核对最小开仓期限。

        只判断阈值时公共函数可接受足够长的日历下界；IV需要完整期限，因此
        _greeks还会单独要求日历覆盖最后交易日。close_remaining_days兼容参数
        不在当前源策略的常规退出判断中使用。
        """
        return remaining_trading_days(self.trading_days, day, last_day,
            self.config.min_remaining_days if threshold is None else threshold)

    def _price(self, symbol, signal_ns):
        """读取最新有效收盘价：不晚于signal_ns，年龄不超过配置上限。

        不要求价格一定来自当前分钟；同分钟约束在选约过滤处单独检查。
        不补查、不向前填充时间，不因Recording/Model而豁免行情时效。
        """
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
        """同月期货价+期权价→IV→Delta/Gamma/Vega；不能用主力期货直接替代。

        source_years保留原公式：开仓使用剩余交易日/252，对冲另加当日剩余
        交易分钟。Vega的折现期限另用自然日差/365，不能与IV期限混为一项。
        若当次时间价值不足或反解不可用，只能使用max_iv_age_seconds内的
        已有IV；缺行情仍由_price报错，不会通过缓存IV绕过价格时效检查。
        """
        info = self.contracts[symbol]
        forward = self._price(info.future, signal_ns)
        premium = self._price(symbol, signal_ns)
        if info.last_day > self.trading_days[-1]:
            raise ValueError(f"{symbol}IV需要完整交易日期限，日历须覆盖最后交易日{info.last_day}")
        days = self._remaining_days(now.date(), info.last_day)
        years = source_years(now, days, hedge=hedge)
        greeks = None
        # 此处的筛选时间价值扣除max(F-K, 0)，与下方源策略退出公式口径不同。
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
                # 缓存只提供sigma，F、K、期限都用当次值；不延长缓存原始时间。
                greeks = source_greeks(forward, info.strike, years, self.config.rate, cached[1],
                    (info.last_day-now.date()).days/365)
        return greeks

    def _frame(self, signal_ns, request_ns):
        """每个封闭时点的主决策，顺序保持源策略及执行约束。

        1. 读取本策略actual/working，保护已截止交易的期权。
        2. 恢复后等相关腿获得新场次价格；未齐时返回RESUME_WAIT。
        3. 若是指定最终清仓日，优先处理清仓并返回，不再增加仓位。
        4. option_slot：最近合格到期月选约、按Vega预算增仓，再检查退出。
        5. hedge_slots：按已成交期权敞口分月份计算期货对冲。
        6. 目标有变化才整体提交一次；执行端根据持仓/在途计算实际委托差额。
        """
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
        # entry/hedge/status是三个维度：本帧可WAIT_SLOT但对冲，也可无新候选但退出旧仓。
        # 实际持仓以positions为准，working表示在途；不能只看status判断是否持仓。

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
            # 这是显式final_day的策略清仓，不是日历午休/收盘时保存持仓的停机操作。
            self.flatten_requested = True
            if not any(working.values()):
                # 有在途先等待回报，避免重复发平仓；后续帧会重新检查。
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
        # 先保存本帧是否到每日期权时点。后面选约会修改sent_days，但本帧退出检查
        # 仍使用这里的option_due，保证“先增仓、后退出”的源顺序只执行一轮。
        if slot == self.config.option_slot and day not in self.sent_days:
            audit = {"signal_ns": signal_ns, "request_ns": request_ns, "date": str(day),
                "slot": slot, "outcome": "WORKING_ORDERS", "candidate_count": 0, "targets": {}}
            self.entry_audit.append(audit)
            del self.entry_audit[:-32]
            if not any(working[key] for key in self.contracts):
                # 只在精确配置时点选约，不在时点之后自动补开；有期权在途则等待，
                # 不把未完成的上一批委托与本次增仓叠加。
                index = self._price(self.index_id, signal_ns)
                candidates = []
                # 执行价大于所加载的指数特征值，且剩余交易日达标；期权、同月
                # 期货和指数必须是当前估值时点。自定义指数值在这里影响执行价筛选。
                eligible = [key for key in self.options_by_day.get(day, ())
                    if key not in expired and self.contracts[key].list_day <= day <= self.contracts[key].last_day
                    and self.contracts[key].strike > index
                    and self._remaining_days(day, self.contracts[key].last_day) >= self.config.min_remaining_days
                    and all(self.latest.get(name, (None,))[0] == signal_ns
                        for name in (key, self.contracts[key].future, self.index_id))]
                nearest = min((self.contracts[key].last_day for key in eligible), default=None)
                # 先确定合格链中最近到期月，再计算该月Greeks；不因该月IV全失败
                # 而自动改选远月。Greeks/配仓无结果均会记录为本日已检查。
                for key in eligible:
                    if self.contracts[key].last_day != nearest:
                        continue
                    info = self.contracts[key]
                    greeks = self._greeks(key, signal_ns, now)
                    if greeks is not None:
                        candidates.append((key, greeks, info.multiplier))
                self.sent_days.add(day)
                # 无有效IV、分配为0或累计手数超限时，也已经消费当天机会，
                # 不在随后的任意分钟重复尝试；sent_days不等于成交成功日期。
                audit["candidate_count"] = len(candidates)
                audit["outcome"] = "NO_VALID_ENTRY"
                if candidates:
                    # 每个Delta档选最近候选，按Gamma权重和现金Vega预算配手数，
                    # 同一合约可命中多个档；allocate_short合并后返回空头负增量。
                    goals = allocate_short(candidates, self.config.delta_targets, self.config.vega_cash_per_unit,
                                           self.config.max_option_lots, self.config.max_total_option_lots)
                    audit["outcome"] = "TARGET" if goals else "ZERO_ALLOCATION"
                    audit["increments"] = {key: str(value) for key, value in goals.items()}
                    audit["targets"] = dict(audit["increments"])
                    audit["future_bindings"] = {key: self.contracts[key].future for key in goals}
                    if goals:
                        # strategy1：每日新增数量累加，其他期权旧仓不被新选约替换。
                        proposed = {key: self.option_goals[key] + Decimal(goals.get(key, 0)) for key in self.contracts}
                        # 配仓函数限制本次增量，这里再限制叠加旧目标后的单合约/总手数。
                        # 超限拒绝整批增量，保留原目标，不静默截断旧仓。
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
                # 原策略退出口径就是C-(F-K)，这里没有max(F-K, 0)。它只在
                # option_slot检查，不是每个Bar都做，也不是按剩余2日强制平仓。
                time_value = close - (forward-info.strike)
                should_close = time_value <= 0
                decision["exit_checks"].append({"key": key, "price": close, "future": info.future,
                    "future_price": forward, "time_value": time_value, "should_close": should_close,
                    "rule": "source_premium_minus_future_minus_strike"})
                if should_close:
                    # 改目标为0，实际仓位要等执行端平仓成交后才归零。
                    self.option_goals[key] = Decimal(0)
                    changed = True

        if self.entry_audit and self.entry_audit[-1]["signal_ns"] == signal_ns:
            # audit.targets是当次分配的增量，audit.option_goals是退出检查后的累计目标。
            # 因此TARGET只证明增量被接受，最终送出的完整组合还要看_submit。
            decision["entry"] = self.entry_audit[-1]["outcome"]
            self.entry_audit[-1]["option_goals"] = {key: str(q) for key, q in self.option_goals.items() if q}
            decision["entry_audit"] = dict(self.entry_audit[-1])
        hedge_token = f"{day}_{slot}"
        hedge_due = slot in self.config.hedge_slots and hedge_token not in self.hedge_sent
        decision["hedge_reasons"] = ["CONFIGURED_SLOT"] if hedge_due else []
        decision["hedge"] = ("WAIT_WORKING_ORDERS" if hedge_due and any(working[key] for key in self.futures)
                             else "EVALUATED" if hedge_due else "NOT_DUE")
        if hedge_due and not any(working[key] for key in self.futures):
            # 有期货在途时本次不标记已处理；无在途才执行同一日/时点一次对冲。
            self.hedge_sent.add(hedge_token)
            # 标记先于IV计算：缺IV的月份会保留旧目标，本时点仍视为已评估。
            # 上游若发现真实故障则直接报错，不通过在下一分钟重放本时点来掩盖。
            totals = {key: 0.0 for key in self.futures}
            closing_without_iv = set()
            for key in self.contracts:
                # 只对冲actual期权仓位，不按尚未成交的新option_goals提前对冲。
                # 因此同一帧刚选出的期权目标，不会被当作已经成交加入本次敞口。
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
                    # 该月份任一期权缺IV，整月保留原期货目标，避免将未知敞口当零。
                    # 这里不会额外触发期权平仓；后续由正常退出和对冲时点继续处理。
                    goals[key] = self.future_goals[key]
                    continue
                # 期权净手数×期权乘数×Delta，除以期货乘数并取反，得到整数期货手数。
                # round沿用Python舍入行为；空头认购通常对应正的期货对冲目标。
                lots = round(-exposure / float(self.futures[key]["contMultNum"]))
                if abs(lots) > self.config.max_future_lots:
                    raise RuntimeError(f"{key}所需对冲{lots}手超过上限，请降低Vega预算")
                goals[key] = Decimal(lots)
            changed = changed or goals != self.future_goals
            self.future_goals = goals

        # changed表示本帧业务曾改变目标，不是“目标已成交”的标记。
        # 某张新选期权随后退出时，changed仍为True，可能提交净值不变的完整目标；
        # 是否需要真的发单继续由执行端按持仓与在途差额判断。
        decision["status"] = "NO_TARGET_CHANGE"
        if changed:
            targets = {str(self.instruments[key]): value for key, value in
                       {**self.option_goals, **self.future_goals}.items()}
            # 显式带零目标，包括换月旧腿；组合提交不代表多腿原子成交。
            self._submit(targets, request_ns, signal_ns, "REBALANCE")

    def _submit(self, targets, request_ns, signal_ns, action):
        """提交完整目标持仓，交公共执行端计算差额、风控、下单和成交归属。

        targets的键是交易InstrumentId字符串，包含应归零的旧腿；默认REPLACE
        更新模式替换本策略的目标组合，不修改其他策略的归属仓位。
        诊断中的TARGET_SUBMITTED只表示目标提交，submissions也不是订单/成交数。
        signals仅展示非零目标；真正传入set_targets的完整组合仍包含零目标。
        """
        self.set_targets(targets, request_ns, metadata={"signal_ts": signal_ns, "action": action})
        self.last_decision.update(status="TARGET_SUBMITTED", action=action,
            targets={key: str(value) for key, value in targets.items() if value
                     or self.last_decision.get("positions", {}).get(key.rsplit(".", 1)[0])})
        self.submissions += 1
        self.signals.append({"signal_ns": signal_ns, "request_ns": request_ns,
                             "action": action, "targets": {key: str(value) for key, value in targets.items() if value}})

    def on_order_update(self, event):
        """根据归属订单回报决定是否报错，由外部生命周期执行停机处理。

        REJECTED始终报错；正常运行中CANCELED/EXPIRED也报错，避免组合只成交
        一部分而继续运行。外部已设置stopping时允许停机撤单；声明到期截止之后
        的期权取消/过期同样允许，因为由公共结算生命周期接管。其余状态只计数。
        """
        self.order_updates_received += 1
        status = getattr(event.status, "value", str(event.status))
        cutoff = next((stamp for key, stamp in self.option_last_trade_times.items()
                       if str(self.instruments[key]) == event.identity.instrument_id), None)
        if status in ("CANCELED", "EXPIRED") and cutoff is not None and event.ts_event >= cutoff:
            return  # 公共生命周期撤销到期余单；拒单仍是硬错误。
        if status == "REJECTED" or (not self.stopping and status in ("CANCELED", "EXPIRED")):
            raise RuntimeError(f"订单{status}: {event.identity.instrument_id} {event.reason}")
