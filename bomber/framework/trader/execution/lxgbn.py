"""LXGBN 内嵌 SDK 的执行适配；宿主负责连接、线程与原生绑定。"""
from copy import deepcopy
from decimal import Decimal
from queue import Queue, Full, Empty
import time
import json
import threading
from uuid import uuid4

from bomber.framework.lxgbn import (BindingSnapshot, OrderCommand, OrderSnapshot,
    FillSnapshot, SubmitResult)
from bomber.framework.market.basic.base import InstrumentId
from .contracts import (AmbiguousOrderSubmission, ExecutionBackendKind, ExecutionReport,
                        OrderIntent, OrderType, PositionEffect, OrderBudgetExhausted)

_NO_FILL = object()


def encode_intent(order):
    return dict(strategy_id=order.strategy_id, backend_id=order.backend_id,
        instrument_id=str(order.instrument_id), side=order.side.value, quantity=str(order.quantity),
        order_type=order.order_type.value, price=None if order.price is None else str(order.price),
        position_effect=order.position_effect.value, reduce_only=order.reduce_only, metadata=dict(order.metadata))


def decode_intent(row):
    return OrderIntent(**dict(row, instrument_id=InstrumentId.from_str(row['instrument_id'])))


class LxgbnExecutionBackend:
    kind = ExecutionBackendKind.LIVE

    def __init__(self, backend_id, binding, host, copies, *, max_orders=100, queue_capacity=4096):
        if (type(max_orders) is not int or not 1 <= max_orders <= 2_147_483_647
                or type(queue_capacity) is not int or queue_capacity < 1):
            raise ValueError('订单上限及队列容量须为正整数')
        self.backend_id, self.binding, self.host, self.copies = backend_id, binding, host, copies
        self.driver = self
        self.instrument_id = InstrumentId.from_str(binding.instrument_id)
        self.strategy_id, self.max_orders = binding.strategy_id, max_orders
        self.session_id = uuid4().hex
        self._orders, self._handlers, self._reports, self._ids = {}, [], [], {}
        self._queue = Queue(queue_capacity)
        self._callback_lock = threading.RLock()
        self.cursor = 0
        self.failure = None
        self.cancel_errors = []
        self.rejected = False
        self.is_started = self.is_armed = False
        self._before_send = self._save = self._unsent = None
        self._transition_lock = threading.RLock()
        self.report_check = lambda: True
        self.submission_check = lambda: True

    @property
    def reports(self):
        return tuple(self._reports)

    def bind_durability(self, before_send, save, unsent, *, transition_lock):
        if self.is_started or self._save is not None:
            raise RuntimeError('持久化只能在启动前绑定一次')
        self._before_send, self._save, self._unsent = before_send, save, unsent
        self._transition_lock = transition_lock

    def fail(self, reason):
        self.failure = str(reason)
        self.disarm()

    def start(self):
        if self.failure:
            raise RuntimeError(self.failure)
        self.is_started = True

    def query(self):
        try:
            value = self.host.query_binding()
            if (not isinstance(value, BindingSnapshot) or value.binding != self.binding
                    or not value.revision or type(value.boundary) is not int or value.boundary < self.cursor
                    or any(getattr(value, flag) is not True for flag in
                        ('initialized', 'complete', 'ready', 'normal_limit', 'generic_close', 'acked_is_accepted'))):
                raise RuntimeError('LXGBN绑定查询不完整、身份不符或能力未确认')
            q = Decimal(str(value.owned_quantity))
            if not q.is_finite() or q != q.to_integral_value():
                raise ValueError('LXGBN策略归属无效')
            return value
        except Exception as error:
            self.fail(error)
            raise

    def authorize(self):
        if not self.ready() or self.rejected or self._save is None:
            raise RuntimeError('LXGBN尚未核验或未安装持久化')
        self.is_armed = True

    def disarm(self):
        self.is_armed = False

    def ready(self):
        return self.is_started and self.failure is None and not self.rejected

    def check_order_capacity(self, quantity):
        if len(self._orders) + quantity > self.max_orders:
            raise OrderBudgetExhausted(self.max_orders, len(self._orders), quantity)

    def register_report_handler(self, handler):
        if handler not in self._handlers:
            self._handlers.append(handler)

    def submit_order(self, order):
        with self._transition_lock:
            if not self.ready() or not self.is_armed or not self.submission_check():
                raise RuntimeError('LXGBN执行闸门关闭')
            if self._before_send is None or self._save is None:
                raise RuntimeError('LXGBN缺少写前保存')
            if (order.strategy_id != self.strategy_id or order.backend_id != self.backend_id
                    or order.instrument_id != self.instrument_id or order.order_type is not OrderType.LIMIT
                    or order.position_effect not in {PositionEffect.OPEN, PositionEffect.CLOSE}
                    or order.quantity != order.quantity.to_integral_value()):
                raise ValueError('LXGBN首期仅支持单策略绑定合约的整数普通限价及通用开平')
            self.check_order_capacity(1)
            sdk_id = max(self._ids, default=0) + 1
            key = 'LXG-' + self.session_id + '-' + str(sdk_id)
            self._ids[sdk_id] = key  # 在调用宿主之前登记；允许 send_order 同步触发回调。
            self._orders[key] = dict(intent=encode_intent(order), trades={}, filled='0',
                accepted=False, terminal=None, pending_terminal=None, sequence=0, native_order_id=None)
            try:
                self._before_send(key, order)
            except Exception:
                del self._orders[key], self._ids[sdk_id]
                self.fail('发送前保存失败')
                raise
            deadline = order.metadata.get('actual_send_deadline_ns')
            if deadline is not None and time.time_ns() > deadline:
                self._unsent(key)
                del self._orders[key], self._ids[sdk_id]
                raise RuntimeError('LXGBN写前保存后目标已过期，未发送')
            command = OrderCommand(sdk_id, key, self.binding, order.side.value, order.quantity,
                                   order.price, order.position_effect.value)
            try:
                result = self.host.send_order(command)
                if not isinstance(result, SubmitResult) or result.outcome not in {'queued', 'rejected'}:
                    raise RuntimeError('宿主发送结果不确定')
            except Exception as error:
                self.fail(error)
                raise AmbiguousOrderSubmission('LXGBN发送结果不确定，保留关联/在途，禁止重发') from error
            if result.outcome == 'rejected':
                # 确定未发送才允许释放；宿主若同时回报成交会在 poll 中触发冲突。
                raw = dict(client_order_id=key, instrument_id=self.binding.instrument_id,
                    side=order.side.value, order_quantity=str(order.quantity), native_order_id=None,
                    status='Rejected', cumulative_filled='0', ts_event=time.time_ns(), reason=result.reason, rejection_of='order')
                try:
                    self._apply(raw)
                    if not self.report_check():
                        raise RuntimeError('LXGBN拒单回报处理失败')
                    self._save()
                except Exception as error:
                    self.fail(error)
                    raise AmbiguousOrderSubmission('LXGBN宿主已返回，拒单保存/处理失败，须恢复核对') from error

    def cancel_strategy(self, strategy_id):
        if strategy_id != self.strategy_id or not self.is_started:
            return
        for sdk_id, key in self._ids.items():
            if not self._orders[key]['terminal']:
                try:
                    self.host.cancel_order(sdk_id)
                except Exception as error:
                    self.fail(error)
                    raise

    def _receive(self, sdk_id, order_msg, exec_msg=_NO_FILL):
        # 仅复制与有界入队；异常不能穿过 C++ -> Python 边界。
        try:
            with self._callback_lock:
                if not self.is_started:
                    raise RuntimeError('LXGBN会话未运行，宿主应解除回调')
                order = deepcopy(self.copies.order(order_msg))
                fill = None if exec_msg is _NO_FILL else deepcopy(self.copies.fill(exec_msg))
                if type(sdk_id) is not int or sdk_id not in self._ids:
                    raise RuntimeError('LXGBN未知 clientOrderId，禁止猜测归属')
                if not isinstance(order, OrderSnapshot) or order.binding != self.binding:
                    raise RuntimeError('LXGBN订单快照绑定不符')
                if order.attribute != 1 or order.hedge_flag != 'Speculation':
                    raise RuntimeError('LXGBN首期仅接受普通限价投机回报')
                if fill is not None and (not isinstance(fill, FillSnapshot) or fill.binding != self.binding
                        or fill.native_order_id != order.native_order_id or fill.side != order.side
                        or fill.hedge_flag != 'Speculation'):
                    raise RuntimeError('LXGBN成交与订单身份不符')
                event = fill or order
                if (not isinstance(event.native_order_id, str) or not event.native_order_id.strip()
                        or type(event.ts_event) is not int or event.ts_event < 0):
                    raise RuntimeError('LXGBN原生ID/事件时间无效')
                raw = dict(client_order_id=self._ids[sdk_id], instrument_id=order.binding.instrument_id,
                    side=order.side, order_quantity=str(order.quantity), native_order_id=order.native_order_id,
                    status=order.status if fill is None else 'Fill', ts_event=event.ts_event,
                    cursor=event.delivery_sequence, cumulative_filled=str(order.cumulative_filled),
                    reason=order.reason, rejection_of=order.rejection_of, received_ns=time.time_ns())
                if fill is not None:
                    raw.update(quantity=str(fill.quantity), price=str(fill.price),
                               trade_id=fill.trade_id, position_effect=fill.position_effect)
                self._queue.put_nowait(raw)
                return True
        except Exception as error:
            detail = '有界回报队列已满，须由宿主重放恢复' if isinstance(error, Full) else str(error)
            self.fail('LXGBN回调失败: ' + detail)
            return False

    def onOrderAck(self, clientOrderId, orderMsg):
        return self._receive(clientOrderId, orderMsg)

    def onOrderFill(self, clientOrderId, orderMsg, execMsg):
        return self._receive(clientOrderId, orderMsg, execMsg)

    def onOrderFinished(self, clientOrderId, orderMsg):
        return self._receive(clientOrderId, orderMsg)

    def _emit(self, key, row, status, raw, *, quantity=Decimal(0), price=None, trade_id=None):
        intent = decode_intent(row['intent'])
        row['sequence'] += 1
        metadata = dict(intent.metadata, strategy_id=intent.strategy_id)
        if trade_id is not None:
            metadata['trade_id'] = json.dumps((self.binding.account_id, self.binding.binding_id,
                self.binding.trading_day, self.binding.source_session, raw['native_order_id'], trade_id),
                ensure_ascii=False, separators=(',', ':'))
        metadata['lxgbn_order_id'] = raw.get('native_order_id')
        metadata['lxgbn_received_ns'] = raw.get('received_ns')
        report = ExecutionReport(self.backend_id, key, intent.instrument_id, status, raw['ts_event'],
            filled_quantity=quantity, fill_price=price, order_side=intent.side,
            order_quantity=intent.quantity, position_effect=(raw.get('position_effect') or intent.position_effect),
            reason=(raw.get('reason') or ('LXGBN Rejected; SDK未提供拒单原因' if status == 'REJECTED' else None)), report_id=f'{key}:{row["sequence"]}', sequence=row['sequence'], metadata=metadata)
        for handler in self._handlers:
            handler(report)
        self._reports.append(report)

    def _apply(self, raw):
        key = raw['client_order_id']
        if key not in self._orders:
            raise RuntimeError('LXGBN未知订单回报，拒绝猜测归属')
        row = self._orders[key]
        intent = row['intent']
        if (raw.get('instrument_id') != intent['instrument_id'] or raw.get('side') != intent['side']
                or Decimal(str(raw.get('order_quantity'))) != Decimal(intent['quantity'])):
            raise RuntimeError('LXGBN回报合约/方向/委托量不符')
        native = raw.get('native_order_id')
        if native is not None:
            if row['native_order_id'] not in (None, native):
                raise RuntimeError('LXGBN原生订单ID发生变化')
            if any(other_key != key and other['native_order_id'] == native
                   for other_key, other in self._orders.items()):
                raise RuntimeError('LXGBN原生订单ID被不同请求复用')
            row['native_order_id'] = native
        if raw['status'] == 'Fill':
            effect = PositionEffect(raw['position_effect'])
            if effect is PositionEffect.AUTO or (effect is PositionEffect.OPEN) != (intent['position_effect'] == 'OPEN'):
                raise RuntimeError('LXGBN实际成交开平与意图不符')
        status = raw['status']
        if status == 'Rejected' and raw.get('rejection_of') not in {'order', 'cancel'}:
            raise RuntimeError('LXGBN拒绝事件未声明报单/撤单对象')
        if status == 'Rejected' and raw.get('rejection_of') == 'cancel':
            self.cancel_errors.append(raw.get('reason') or 'LXGBN撤单被拒绝')
            return
        if status in {'Blank', 'Submited'}:
            return
        if status == 'Acked':
            if not row['accepted'] and not row['terminal']:
                row['accepted'] = True
                self._emit(key, row, 'ACCEPTED', raw)
            return
        if status == 'Fill':
            trade_id = raw['trade_id']
            if not isinstance(trade_id, str) or not trade_id.strip():
                raise RuntimeError('LXGBN缺少稳定原生成交ID')
            fact = [str(Decimal(str(raw['quantity']))), str(Decimal(str(raw['price']))), raw['ts_event'], raw['position_effect']]
            previous = row['trades'].get(trade_id)
            if previous is not None:
                if (Decimal(previous[0]), Decimal(previous[1]), *previous[2:]) != (
                        Decimal(fact[0]), Decimal(fact[1]), *fact[2:]):
                    raise RuntimeError('LXGBN相同成交ID内容冲突')
                return
            if row['terminal']:
                raise RuntimeError('LXGBN终态后出现新成交，须核查')
            quantity, price = Decimal(fact[0]), Decimal(fact[1])
            filled = Decimal(row['filled']) + quantity
            total = Decimal(intent['quantity'])
            if (not trade_id or not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value()
                    or not price.is_finite() or price <= 0 or filled > total):
                raise RuntimeError('LXGBN成交数量/价格无效')
            row['trades'][trade_id], row['filled'] = fact, str(filled)
            self._emit(key, row, 'FILLED' if filled == total else 'PARTIALLY_FILLED', raw,
                       quantity=quantity, price=price, trade_id=trade_id)
            if filled == total:
                row['terminal'] = 'FullFilled'
        elif status in {'Cancelled', 'Rejected', 'FullFilled'}:
            previous = row['pending_terminal']
            if previous and (previous['status'], Decimal(previous['cumulative_filled'])) != (
                    status, Decimal(raw['cumulative_filled'])):
                raise RuntimeError('LXGBN终态回报冲突')
            row['pending_terminal'] = raw
        else:
            raise RuntimeError('LXGBN未知原生状态')
        terminal = row['pending_terminal']
        if terminal:
            expected = Decimal(str(terminal['cumulative_filled']))
            filled, total = Decimal(row['filled']), Decimal(intent['quantity'])
            if not expected.is_finite() or not 0 <= expected <= total or filled > expected:
                raise RuntimeError('LXGBN终态累计成交不符')
            if filled == expected:
                if terminal['status'] == 'FullFilled':
                    if filled != total:
                        raise RuntimeError('LXGBN全成数量不符')
                elif row['terminal'] is None:
                    self._emit(key, row, 'CANCELED' if terminal['status'] == 'Cancelled' else 'REJECTED', terminal)
                elif row['terminal'] != terminal['status']:
                    raise RuntimeError('LXGBN终态冲突')
                row['terminal'] = terminal['status']
                if terminal['status'] == 'Rejected':
                    self.rejected = True
                    self.disarm()

    def poll(self):
        if not self.is_started:
            return
        if self.failure:
            raise RuntimeError('LXGBN执行故障已锁存: ' + self.failure)
        try:
            while True:
                try:
                    raw = self._queue.get_nowait()
                except Empty:
                    break
                with self._transition_lock:
                    if type(raw['cursor']) is not int or raw['cursor'] != self.cursor + 1:
                        raise RuntimeError('LXGBN宿主交付游标缺口')
                    self._apply(raw)
                    if not self.report_check():
                        raise RuntimeError('LXGBN公共订单机/归属处理失败')
                    self.cursor = raw['cursor']
                    self._save()
        except Exception as error:
            self.fail(error)
            raise

    def stop(self):
        self.disarm()
        if any(not row['terminal'] for row in self._orders.values()) or not self._queue.empty():
            raise RuntimeError('LXGBN尚有活动单或未处理回报，不能释放会话')
        self.is_started = False

    def snapshot_state(self):
        return deepcopy(dict(version=2, binding=vars(self.binding), session_id=self.session_id,
                             cursor=self.cursor, orders=self._orders, ids={str(k): v for k, v in self._ids.items()}))

    def restore_state(self, state):
        if (self.is_started or state.get('version') != 2 or state.get('binding') != vars(self.binding)
                or type(state.get('cursor')) is not int or state['cursor'] < 0):
            raise ValueError('LXGBN绑定检查点无效')
        session = state.get('session_id')
        if not isinstance(session, str) or len(session) != 32 or any(c not in '0123456789abcdef' for c in session):
            raise ValueError('LXGBN检查点会话无效')
        orders = deepcopy(state['orders'])
        ids = {int(k): v for k, v in state['ids'].items()}
        if set(ids.values()) != set(orders) or len(ids) != len(orders) or any(
                n < 1 or key != 'LXG-' + session + '-' + str(n) for n, key in ids.items()):
            raise ValueError('LXGBN检查点整数ID映射无效')
        for row in orders.values():
            order = decode_intent(row['intent'])
            if (order.backend_id != self.backend_id or order.instrument_id != self.instrument_id
                    or order.strategy_id != self.strategy_id):
                raise ValueError('LXGBN检查点订单绑定不符')
            amounts = [Decimal(fact[0]) for fact in row['trades'].values()]
            if (any(not q.is_finite() or q <= 0 for q in amounts)
                    or sum(amounts, Decimal(0)) != Decimal(row['filled'])
                    or not 0 <= Decimal(row['filled']) <= order.quantity
                    or row['terminal'] not in (None, 'FullFilled', 'Cancelled', 'Rejected')
                    or type(row['sequence']) is not int or row['sequence'] < 0):
                raise ValueError('LXGBN检查点成交/终态无效')
        self.session_id, self.cursor, self._orders, self._ids = session, state['cursor'], orders, ids
