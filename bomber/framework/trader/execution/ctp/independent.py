"""各进程直连CTP：完整订单身份、策略今昨仓和账户每日报单计数。

没有RPC、账户服务或目标转发。账本与原执行检查点在同一次写入中提交。
"""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sqlite3

from ..contracts import OrderIntent, OrderSide, PositionEffect, ExecutionReportType, OrderBudgetExhausted
from .native_driver import CtpNativeTraderDriver, _serialized_transition
from .td_transport import CtpTdApiTransport
from .limit_planner import IndependentCtpLimitPlanner


D = Decimal
BUCKETS = ('long_today', 'long_yesterday', 'short_today', 'short_yesterday')


class DailyOrderCounter:
    """同机同账户原子限额；数据库只计数，不连接CTP、不存密码。"""
    def __init__(self, path, account, limit):
        self.path, self.account, self.limit = Path(path), account, limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sends(account TEXT, day TEXT, identity TEXT, '
                       'PRIMARY KEY(account,day,identity))')
            db.execute('CREATE TABLE IF NOT EXISTS limits(account TEXT PRIMARY KEY, cap INTEGER)')
            db.execute('INSERT OR IGNORE INTO limits VALUES(?,?)',(account,limit))
            if db.execute('SELECT cap FROM limits WHERE account=?',(account,)).fetchone() != (limit,):
                raise ValueError('共享日计数器的账户报单上限不一致')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30, isolation_level='IMMEDIATE')

    def seed(self, payload):
        with self._connect() as db:
            for row in payload.get('component_states',{}).get('ctp_order_owners',{}).get('records',{}).values():
                db.execute('INSERT OR IGNORE INTO sends VALUES(?,?,?)',
                    (self.account,row['day'],':'.join(row['session'])))
            if 'ctp_order_owners' in payload.get('component_states',{}):
                return
            for rows in payload.get('orders', {}).values():
                for row in rows:
                    identity = row['client_order_id']
                    day = identity.rsplit('-', 2)[-2]
                    db.execute('INSERT OR IGNORE INTO sends VALUES(?,?,?)',
                               (self.account, day, 'legacy:'+identity))

    def check(self, day, count=1):
        with self._connect() as db:
            used, = db.execute('SELECT count(*) FROM sends WHERE account=? AND day=?',
                              (self.account, day)).fetchone()
        if used + count > self.limit:
            raise OrderBudgetExhausted(self.limit, used, count)

    def reserve(self, day, identity):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM sends WHERE account=? AND day=? AND identity=?',
                          (self.account, day, identity)).fetchone():
                return
            used, = db.execute('SELECT count(*) FROM sends WHERE account=? AND day=?',
                              (self.account, day)).fetchone()
            if used >= self.limit:
                raise OrderBudgetExhausted(self.limit, used, 1)
            db.execute('INSERT INTO sends VALUES(?,?,?)', (self.account, day, identity))


class StrategyBuckets:
    """策略/真实合约/多空/今昨桶；每笔订单冻结到原桶，撤单只释放剩余量。"""
    def __init__(self, day):
        self.day, self.positions, self.frozen = day, {}, {}

    def quantities(self, sid, instrument):
        return self.positions.get((sid, str(instrument)), [D(0)]*4)

    def available(self, sid, instrument, short=False):
        key = (sid, str(instrument))
        values = list(self.quantities(*key))
        for row in self.frozen.values():
            if (row['strategy_id'], row['instrument_id']) == key:
                values = [q-D(f) for q, f in zip(values, row['buckets'])]
        if any(q < 0 for q in values):
            raise RuntimeError('策略今昨冻结量超过归属仓位')
        return tuple(values[2:] if short else values[:2])  # today, yesterday

    def reserve(self, identity, order):
        if identity in self.frozen:
            raise RuntimeError('重复冻结订单')
        values = [D(0)]*4
        if order.position_effect is not PositionEffect.OPEN:
            short = order.side is OrderSide.BUY
            today, yesterday = self.available(order.strategy_id, order.instrument_id, short)
            indices = [2, 3] if short else [0, 1]
            if order.position_effect is PositionEffect.CLOSE_TODAY:
                allocation = (order.quantity, D(0))
                if self.quantities(order.strategy_id, order.instrument_id)[3 if short else 1]:
                    raise RuntimeError('仍有可平昨仓，不能先发平今')
            elif order.position_effect is PositionEffect.CLOSE_YESTERDAY:
                allocation = (D(0), order.quantity)
            else:
                amount = min(order.quantity, yesterday)
                allocation = (order.quantity-amount, amount)
            if allocation[0] > today or allocation[1] > yesterday:
                raise RuntimeError('平仓超过本策略可用今昨仓')
            for i, q in zip(indices, allocation):
                values[i] = q
        self.frozen[identity] = dict(strategy_id=order.strategy_id,
            instrument_id=str(order.instrument_id), buckets=[str(q) for q in values])

    def transition(self, identity, order, report_type, quantity):
        key = (order.strategy_id, str(order.instrument_id))
        if quantity:
            row = self.frozen[identity]
            current = self.positions.setdefault(key, [D(0)]*4)
            if order.position_effect is PositionEffect.OPEN:
                current[0 if order.side is OrderSide.BUY else 2] += quantity
            else:
                remaining = quantity
                # 消耗冻结桶，不能按最新目标或柜台剩余仓重新分配。
                for i in (1, 3, 0, 2):
                    amount = min(remaining, D(row['buckets'][i]))
                    current[i] -= amount
                    row['buckets'][i] = str(D(row['buckets'][i])-amount)
                    remaining -= amount
                if remaining or any(q < 0 for q in current):
                    raise RuntimeError('成交超过订单今昨冻结量')
        if report_type in (ExecutionReportType.CANCELED, ExecutionReportType.REJECTED,
                           ExecutionReportType.FILLED):
            self.frozen.pop(identity, None)

    def snapshot_state(self):
        return dict(version=1, trading_day=self.day, positions=[dict(strategy_id=k[0],
            instrument_id=k[1], quantities=[str(q) for q in v]) for k, v in sorted(self.positions.items())],
            frozen=deepcopy(self.frozen))

    def restore_state(self, state):
        if state.get('version') != 1 or state['trading_day'] != self.day:
            raise ValueError('策略今昨仓交易日不一致')
        positions = {}
        for row in state['positions']:
            key = (row['strategy_id'], row['instrument_id'])
            values = [D(q) for q in row['quantities']]
            if key in positions or len(values) != 4 or any(not q.is_finite() or q < 0 or q != q.to_integral_value() for q in values):
                raise ValueError('策略今昨仓无效')
            positions[key] = values
        self.positions, self.frozen = positions, deepcopy(state['frozen'])
        for row in self.frozen.values():
            values = [D(q) for q in row['buckets']]
            if len(values) != 4 or any(not q.is_finite() or q < 0 for q in values):
                raise ValueError('冻结今昨仓无效')
            self.available(row['strategy_id'], row['instrument_id'])
            self.available(row['strategy_id'], row['instrument_id'], True)


class OrderIdentityBook:
    """本地策略ID不会由CTP回传；以原会话三元组和交易所订单号关联。"""
    def __init__(self, broker, investor):
        self.broker, self.investor = broker, investor
        self.day = None
        self.records, self.foreign, self.by_session, self.by_exchange = {}, set(), {}, {}

    @staticmethod
    def session_key(raw):
        return (str(raw.get('FrontID', '')), str(raw.get('SessionID', '')), str(raw.get('OrderRef', '')).strip())

    @staticmethod
    def exchange_key(raw):
        return (str(raw.get('ExchangeID', '')).strip(), str(raw.get('OrderSysID', '')).strip())

    def _index(self):
        self.by_session, self.by_exchange = {}, {}
        for ref, row in self.records.items():
            key = tuple(row['session'])
            if row['day'] == self.day:
                if key in self.by_session:
                    raise ValueError('重复CTP会话订单身份')
                self.by_session[key] = ref
            if row.get('exchange') and row['day'] == self.day:
                key = tuple(row['exchange'])
                if key in self.by_exchange:
                    raise ValueError('重复交易所订单身份')
                self.by_exchange[key] = ref

    def add(self, ref, identity, order, session):
        if ref in self.records or not session.front_id or not session.session_id:
            raise RuntimeError('直连模式必须保存有效FrontID/SessionID，且不能复用本地OrderRef')
        self.records[ref] = dict(client_order_id=identity, strategy_id=order.strategy_id,
            instrument_id=str(order.instrument_id), day=session.trading_day,
            session=[str(session.front_id), str(session.session_id), ref], exchange=None,
            terminal=False, trades=[])
        self._index()

    def accept_order(self, raw):
        if str(raw.get('BrokerID', '')) != self.broker or str(raw.get('InvestorID', '')) != self.investor:
            raise RuntimeError('CTP广播账户身份不一致')
        key, exchange = self.session_key(raw), self.exchange_key(raw)
        ref = self.by_session.get(key)
        if ref is None:
            if exchange[1]:
                if exchange in self.by_exchange:
                    raise RuntimeError('CTP原会话身份与交易所订单号冲突')
                self.foreign.add(exchange)
            return False
        row = self.records[ref]
        if str(raw.get('InstrumentID', ''))+'.'+exchange[0] != row['instrument_id']:
            raise RuntimeError('CTP订单合约与策略映射冲突')
        if exchange[1]:
            if row['exchange'] is not None and tuple(row['exchange']) != exchange:
                raise RuntimeError('CTP订单的交易所订单号改变')
            row['exchange'] = list(exchange)
            self._index()
        return True

    def snapshot_state(self):
        return dict(version=1, broker=self.broker, investor=self.investor, records=deepcopy(self.records))

    def restore_state(self, state):
        if (state.get('version'), state.get('broker'), state.get('investor')) != (1, self.broker, self.investor):
            raise ValueError('订单归属账本账户身份不一致')
        self.records = deepcopy(state['records'])
        for ref, row in self.records.items():
            if not ref.isdigit() or row['session'][2] != ref or not row['strategy_id']:
                raise ValueError('订单归属映射无效')
        self._index()


class IndependentTdTransport(CtpTdApiTransport):
    """只在本进程订单命名空间内提供查询/撤单；账户仓位查询仍保持完整。"""
    def connect(self, *args):
        self._broadcasts = []
        return super().connect(*args)

    def _order_push(self, data):
        row = dict(data)
        with self._cv:
            if not self._ready:
                if len(self._broadcasts) >= 65536:
                    self._front_down(-1)
                    return
                self._broadcasts.append(('order', row))
                return
        if self.accept_order(row):
            self._last_orders[str(row['OrderRef']).strip()] = row
            self._on_order(row)

    def _trade_push(self, data):
        with self._cv:
            if not self._ready:
                if len(self._broadcasts) >= 65536:
                    self._front_down(-1)
                    return
                self._broadcasts.append(('trade', dict(data)))
                return
        self._on_trade(dict(data))

    def activate(self):
        super().activate()
        with self._cv:
            pending, self._broadcasts = self._broadcasts, []
        for kind, row in pending:
            (self._trade_push if kind == 'trade' else self._order_push)(row)

class IndependentCtpDriver(CtpNativeTraderDriver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.book = OrderIdentityBook(self.transport.broker_id, self.transport.investor_id)
        self.transport.accept_order = self._accept_order
        self.buckets, self.daily_counter = None, None
        self.preserve_ambiguous_sends = True
        self._unmatched_trades = []

    def start(self, sink):
        super().start(sink)
        self.book.day = self.trading_day
        self.book._index()

    @_serialized_transition
    def _accept_order(self, raw):
        try:
            accepted = self.book.accept_order(raw)
        except Exception:
            self.on_disconnect(-1)
            raise
        session = self._session
        if not accepted and session is not None and self.book.session_key(raw)[:2] == (str(session.front_id), str(session.session_id)):
            self.on_disconnect(-1)
            raise RuntimeError('当前CTP会话出现没有写前归属的委托，保持闭闸')
        return accepted

    def bind_durability(self, before_send, after_transition, on_unsent=None, **kwargs):
        def reserve(identity, order):
            ref = identity.rsplit('-', 1)[-1]
            self.book.add(ref, identity, order, self._session)
            self.buckets.reserve(identity, order)
            if self.daily_counter is not None:
                self.daily_counter.reserve(self.trading_day, ':'.join(self.book.records[ref]['session']))
            before_send(identity, order)
        def unsent(identity):
            self.buckets.frozen.pop(identity, None)
            self.book.records.pop(identity.rsplit('-', 1)[-1], None)
            self.book._index()
            if on_unsent is not None:
                on_unsent(identity)
        super().bind_durability(reserve, after_transition, unsent, **kwargs)

    def check_order_capacity(self, count):
        super().check_order_capacity(count)
        if self.daily_counter is not None and self.trading_day:
            self.daily_counter.check(self.trading_day, count)

    @_serialized_transition
    def on_order(self, raw):
        if not self._accept_order(raw):
            return
        ref = self.book.by_session[self.book.session_key(raw)]
        row = self.book.records[ref]
        if row['terminal']:
            return
        super().on_order(dict(raw, OrderRef=ref))
        self._drain_trades()

    @_serialized_transition
    def on_trade(self, raw):
        if str(raw.get('BrokerID','')) != self.book.broker or str(raw.get('InvestorID','')) != self.book.investor:
            self.on_disconnect(-1)
            raise RuntimeError('CTP成交账户不一致')
        day = str(raw.get('TradingDay') or self.book.day)
        if self.book.day is not None and day != self.book.day:
            if day < self.book.day:
                return
            self.on_disconnect(-1)
            raise RuntimeError('CTP成交交易日领先当前会话')
        key = self.book.exchange_key(raw)
        ref = self.book.by_exchange.get(key)
        if ref is None:
            if key in self.book.foreign:
                return
            if not key[1] or len(self._unmatched_trades) >= 65536:
                self.on_disconnect(-1)
                raise RuntimeError('成交没有可核验交易所订单身份或缓冲溢出')
            self._unmatched_trades.append(dict(raw))
            return
        row = self.book.records[ref]
        trade = [str(raw.get('TradingDay') or row['day']), key[0], str(raw.get('TradeID', '')).strip()]
        if trade in row['trades']:
            return
        if row['terminal']:
            self.on_disconnect(-1)
            raise RuntimeError('已结束订单出现未知成交，保留账本并要求核验')
        intent = (self._orders[ref][1] if ref in self._orders else next(
            record.intent for record in self._recovery_checkpoint.orders if record.order_ref == ref))
        offset = {PositionEffect.OPEN:'0', PositionEffect.CLOSE:'1',
                  PositionEffect.CLOSE_TODAY:'3', PositionEffect.CLOSE_YESTERDAY:'4'}[intent.position_effect]
        if str(raw.get('Direction','')) != ('0' if intent.side is OrderSide.BUY else '1') or str(raw.get('OffsetFlag','')) != offset:
            self.on_disconnect(-1)
            raise RuntimeError('成交方向或开平标志与持久化委托不一致')
        # 归属来自OrderSysID，Trade.OrderRef只作为附加校验，不用于跨会话匹配。
        if str(raw.get('OrderRef', ref)).strip() != ref:
            self.on_disconnect(-1)
            raise RuntimeError('成交OrderRef与交易所订单身份冲突')
        super().on_trade(dict(raw, OrderRef=ref))

    def _drain_trades(self):
        pending, self._unmatched_trades = self._unmatched_trades, []
        for raw in pending:
            self.on_trade(raw)

    def _emit(self, ref, report_type, raw, **kwargs):
        identity, order = self._orders[ref]
        row = self.book.records[ref]
        quantity = kwargs.get('quantity', D(0))
        self.buckets.transition(identity, order, report_type, quantity)
        if kwargs.get('trade_id') is not None:
            row['trades'].append([str(raw.get('TradingDay') or row['day']),
                str(raw['ExchangeID']).strip(), str(raw['TradeID']).strip()])
        if report_type in (ExecutionReportType.FILLED, ExecutionReportType.CANCELED, ExecutionReportType.REJECTED):
            row['terminal'] = True
        super()._emit(ref, report_type, raw, **kwargs)

    def reconcile_active_orders(self):
        snapshot = super().reconcile_active_orders()
        self._drain_trades()
        # 完整订单查询已区分外部订单；仍未关联的成交不可当成本策略成交。
        unknown = [row for row in self._unmatched_trades
                   if self.book.exchange_key(row) not in self.book.foreign]
        if unknown:
            self.on_disconnect(-1)
            raise RuntimeError('完整订单查询后仍有未归属成交，保持闭闸')
        self._unmatched_trades.clear()
        return snapshot

    def _restore_associations_from_snapshot(self, checkpoint, snapshot):
        # 同交易日断线期间的部分成交：按真实成交查询补记，不以持仓差额推断。
        active = {row.client_order_id: row for row in snapshot.orders}
        missing = []
        adjusted = []
        for record in checkpoint.orders:
            if record.order_ref not in self.book.records:
                raise RuntimeError('旧活动委托缺少完整原会话身份，不能猜测归属')
            reported = active.get(record.client_order_id)
            if reported is not None:
                if reported.cumulative_filled < record.filled_quantity:
                    raise RuntimeError('柜台累计成交量少于本地')
                if reported.cumulative_filled > record.filled_quantity:
                    raw = self.transport.queried_order_states[record.client_order_id]
                    candidates = [dict(row, _recovery_query=True) for row in self.transport.query_trades()
                        if self.book.exchange_key(row) == self.book.exchange_key(raw)
                        and (checkpoint.trading_day, str(row['ExchangeID']).strip(), str(row['TradeID']).strip()) not in record.seen_trades]
                    unique = {str(row['TradeID']).strip(): row for row in candidates}
                    if sum((D(str(row['Volume'])) for row in unique.values()), D(0)) != reported.cumulative_filled-record.filled_quantity:
                        raise RuntimeError('活动委托缺失成交没有完整真实成交证据')
                    missing.extend(unique.values())
                adjusted.append(replace(reported, cumulative_filled=record.filled_quantity,
                    remaining_quantity=record.intent.quantity-record.filled_quantity))
        super()._restore_associations_from_snapshot(checkpoint, replace(snapshot, orders=tuple(adjusted)))
        self._replaying_recovery = True
        try:
            for raw in missing:
                self.on_trade(raw)
        finally:
            self._replaying_recovery = False


class YesterdayFirstPlanner(IndependentCtpLimitPlanner):
    """只发送一个桶；昨仓实际成交后，下一次目标推进才能发送今仓。"""
    def __init__(self, planner, positions, buckets):
        super().__init__(planner, positions)
        self.buckets = buckets

    def plan(self, request):
        planned = []
        rows = request.metadata['position_attribution']
        for instrument, target in request.targets.items():
            keys = [r['target_key'] for r in rows if r['instrument_id'] == str(instrument)]
            current = sum((self.positions.position(request.strategy_id, k) for k in keys), D(0))
            delta = D(target)-current
            if not delta:
                continue
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            quantity, effect = abs(delta), PositionEffect.OPEN
            extra = {}
            if current*delta < 0:
                today, yesterday = self.buckets.available(request.strategy_id, instrument, delta > 0)
                quantity = min(quantity, abs(current), yesterday if yesterday else today)
                if not quantity:
                    continue
                venue = str(instrument).rsplit('.', 1)[-1]
                effect = (PositionEffect.CLOSE if venue in self.planner._planner.generic_close_venues
                          else PositionEffect.CLOSE_YESTERDAY if yesterday else PositionEffect.CLOSE_TODAY)
                if effect is PositionEffect.CLOSE:
                    extra['owned_close_bucket'] = (PositionEffect.CLOSE_YESTERDAY if yesterday else PositionEffect.CLOSE_TODAY).value
            planned.append(OrderIntent(request.strategy_id, request.client_id, instrument, side, quantity,
                position_effect=effect, reduce_only=effect is not PositionEffect.OPEN,
                metadata={**request.metadata, **extra, 'target':str(target), 'owned_position':str(current),
                          'request_revision':request.revision}))
        return self.planner.price_orders(request, planned)
