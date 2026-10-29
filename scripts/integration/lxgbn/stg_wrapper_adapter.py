"""LXGBN 可选接线组件，不导入 stgWrapper，不猜测原生报撤单签名。

宿主须注入已确认的交易函数、一致绑定查询和逐回调事实来源。
本模块不启动原生环境，不创建线程，不生成交付序号或持仓快照。
"""
from dataclasses import dataclass
from decimal import Decimal
import threading
from types import MappingProxyType

from bomber.framework.lxgbn import (
    Binding, BindingSnapshot, CallbackCopies, FillSnapshot, OrderCommand,
    OrderSnapshot, SubmitResult,
)


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} 须为 >= {minimum} 的 Python int')
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} 须为非空字符串')
    return value


def _number(value, name, *, positive=False, integral=False):
    if isinstance(value, bool):
        raise ValueError(f'{name} 不能是 bool')
    value = Decimal(str(value))
    if (not value.is_finite() or value < 0 or (positive and value == 0)
            or (integral and value != value.to_integral_value())):
        raise ValueError(f'{name} 数值无效')
    return value


def _native_id(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f'{name} 须为原生整数或非空字符串')
    return _text(str(value), name)


class CommandLinks:
    """本次进程的命令关联；不替代 SDK 检查点或原生查询。"""
    def __init__(self, binding):
        if not isinstance(binding, Binding):
            raise ValueError('须提供已确认的 Binding')
        self.binding = binding
        self._commands, self._native = {}, {}
        self._lock = threading.RLock()

    def register(self, command):
        if not isinstance(command, OrderCommand) or command.binding != self.binding:
            raise ValueError('命令绑定不符')
        key = _integer(command.client_order_id, 'client_order_id', 1)
        _text(command.request_id, 'request_id')
        if (command.side not in {'BUY', 'SELL'} or command.position_effect not in {'OPEN', 'CLOSE'}
                or type(command.attribute) is not int or command.attribute != 1
                or command.hedge_flag != 'Speculation'):
            raise ValueError('仅接受普通限价、投机及 OPEN/CLOSE 命令')
        _number(command.quantity, 'quantity', positive=True, integral=True)
        _number(command.price, 'price', positive=True)
        with self._lock:
            if key in self._commands or any(c.request_id == command.request_id for c in self._commands.values()):
                raise ValueError('命令 ID 已登记，禁止重复发送或覆盖关联')
            self._commands[key] = command

    def command(self, client_order_id):
        key = _integer(client_order_id, 'clientOrderId', 1)
        with self._lock:
            if key not in self._commands:
                raise ValueError('未知 clientOrderId；不能猜测归属或自动接管历史单')
            return self._commands[key]

    def link_native(self, client_order_id, native_order_id):
        self.command(client_order_id)
        native = _native_id(native_order_id, 'native_order_id')
        with self._lock:
            previous = self._native.get(client_order_id)
            if previous is not None and previous != native:
                raise ValueError('同一 SDK 请求的原生订单 ID 发生变化')
            if any(key != client_order_id and value == native for key, value in self._native.items()):
                raise ValueError('原生订单 ID 被不同 SDK 请求复用')
            self._native[client_order_id] = native


class NativeHostPort:
    """注入函数的参数是 Bomber 契约，不表示原生 sendOrder 具有相同签名。"""
    def __init__(self, links, *, send=None, cancel=None, query=None):
        self.links = links
        self._send, self._cancel, self._query = send, cancel, query

    def query_binding(self):
        if any(not callable(fn) for fn in (self._send, self._cancel, self._query)):
            raise RuntimeError('原生 send/cancel/query 接线尚未全部确认，禁止启动')
        snapshot = self._query()
        if not isinstance(snapshot, BindingSnapshot) or snapshot.binding != self.links.binding:
            raise ValueError('原生查询须返回同绑定 BindingSnapshot')
        # 完整性、能力、边界和归属由 SDK 再验证；不从命令表拼造快照。
        return snapshot

    def send_order(self, command):
        if not callable(self._send):
            return SubmitResult('rejected', '原生发送函数尚未接线，确定未发送')
        self.links.register(command)  # 先登记，再调用原生发送；此处不持锁等待原生。
        result = self._send(command)
        if not isinstance(result, SubmitResult) or result.outcome not in {'queued', 'rejected', 'unknown'}:
            raise RuntimeError('原生发送结果未规范化；视为发送不确定，保留关联')
        return result

    def cancel_order(self, client_order_id):
        command = self.links.command(client_order_id)
        if not callable(self._cancel):
            raise RuntimeError('原生撤单函数尚未接线')
        # 注入层拿到完整命令后选择已确认的原生 ID/参数，不猜 cancelOrder(int) 的含义。
        self._cancel(command)


@dataclass(frozen=True)
class NativeEnums:
    side: object
    status: object
    offset: object
    hedge: object

    def __post_init__(self):
        for name in ('side', 'status', 'offset', 'hedge'):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @classmethod
    def from_module(cls, rp):
        """仅从已加载且确认同版的模块读取枚举；不回退到未知整数。"""
        return cls(
            {rp.OrderMsg_dir_t.Buy: 'BUY', rp.OrderMsg_dir_t.Sell: 'SELL'},
            {getattr(rp.status_t, name): name for name in
             ('Blank', 'Submited', 'Acked', 'FullFilled', 'Rejected', 'Cancelled')},
            {rp.OrderMsg_offset_t.Open: 'OPEN', rp.OrderMsg_offset_t.Close: 'CLOSE',
             rp.OrderMsg_offset_t.CloseToday: 'CLOSE_TODAY', rp.OrderMsg_offset_t.CloseYd: 'CLOSE_YESTERDAY'},
            {rp.HedgeFlag_type_t.Speculation: 'Speculation'},
        )

    def read(self, name, value):
        if isinstance(value, bool):
            raise ValueError(f'未知原生 {name} 枚举: bool')
        try:
            return getattr(self, name)[value]
        except (KeyError, TypeError) as error:
            raise ValueError(f'未知或未支持的原生 {name} 枚举') from error


@dataclass(frozen=True)
class CallbackFacts:
    """宿主经核验提供的补充事实；无账户、时间、序号或能力占位默认值。"""
    binding: Binding
    delivery_sequence: int
    order_ts_ns: int
    attribute: int
    fill_ts_ns: int | None = None
    fill_hedge: object = None  # 原生枚举，或由可信订单关联取得的原生值。
    reason: str = ''
    rejection_of: str = 'unknown'


class NativeCallbackCopies:
    def __init__(self, links, enums, *, instrument_code, strategy_id,
                 quantity_field, read_facts):
        if quantity_field not in {'volumeSubmitted', 'volumePlanned'}:
            raise ValueError('必须显式确认固定委托量字段')
        if not callable(read_facts):
            raise ValueError('须提供真实绑定/时间/交付序号等事实读取函数')
        self.links, self.enums = links, enums
        self.instrument_code = _text(instrument_code, 'native instrument_code')
        _native_id(strategy_id, 'native strategy_id')
        self.strategy_id = strategy_id
        self.quantity_field, self.read_facts = quantity_field, read_facts

    def copy(self, kind, client_order_id, order, fill=None):
        if kind not in {'ack', 'fill', 'finished'} or (kind == 'fill') != (fill is not None):
            raise ValueError('回调种类或成交对象不完整')
        command = self.links.command(client_order_id)
        if order.GetInst() != self.instrument_code or order.strategyId != self.strategy_id:
            raise ValueError('原生订单合约/策略不符')
        native_id = _native_id(order.Id, 'Order.Id')
        side = self.enums.read('side', order.direction)
        quantity = _number(getattr(order, self.quantity_field), '固定委托量', positive=True, integral=True)
        cumulative = _number(order.volumeTraded, '累计成交量', integral=True)
        if side != command.side or quantity != command.quantity or cumulative > quantity:
            raise ValueError('原生委托方向/固定委托量/累计量与命令不符')
        effect = self.enums.read('offset', order.offset)
        if (effect == 'OPEN') != (command.position_effect == 'OPEN'):
            raise ValueError('原生订单开平与命令不符')
        status = self.enums.read('status', order.status)
        hedge = self.enums.read('hedge', order.hedgeFlag)
        facts = self.read_facts(kind, command, order, fill)
        if not isinstance(facts, CallbackFacts) or facts.binding != command.binding:
            raise ValueError('回调补充事实的绑定未核验或不符')
        _integer(facts.delivery_sequence, 'delivery_sequence', 1)
        _integer(facts.order_ts_ns, 'order_ts_ns')
        if type(facts.attribute) is not int or facts.attribute != 1:
            raise ValueError('未确认 Normal 属性或原生属性不符')
        if not isinstance(facts.reason, str):
            raise ValueError('reason 须为已解码字符串')
        if status == 'Rejected' and facts.rejection_of not in {'order', 'cancel'}:
            raise ValueError('拒绝通知须明确报单或撤单对象')
        snapshot = OrderSnapshot(command.binding, facts.delivery_sequence, native_id,
            side, quantity, status, cumulative, facts.order_ts_ns, facts.attribute,
            hedge, reason=facts.reason, rejection_of=facts.rejection_of)
        execution = None
        if fill is not None:
            if (_native_id(fill.orderId, 'Trade.orderId') != native_id
                    or fill.strategyId != self.strategy_id
                    or self.enums.read('side', fill.direction) != side):
                raise ValueError('原生成交与订单身份/方向不符')
            fill_effect = self.enums.read('offset', fill.offset)
            if (fill_effect == 'OPEN') != (command.position_effect == 'OPEN'):
                raise ValueError('原生成交开平与命令不符')
            _integer(facts.fill_ts_ns, 'fill_ts_ns')
            fill_hedge = self.enums.read('hedge', facts.fill_hedge)
            execution = FillSnapshot(command.binding, facts.delivery_sequence, native_id,
                _native_id(fill.tradeId, 'Trade.tradeId'), side,
                _number(fill.volume, '单笔成交量', positive=True, integral=True),
                _number(fill.price, '成交价', positive=True), fill_effect,
                facts.fill_ts_ns, fill_hedge)
        self.links.link_native(client_order_id, native_id)
        return snapshot, execution


def _value(value):
    return value


VALUE_COPIES = CallbackCopies(order=_value, fill=_value)


class NativeCallbacks:
    """包装类可委托这三个方法；只做值复制/转发，不调用 poll 或创建线程。"""
    def __init__(self, copies):
        self.copies, self.session = copies, None
        self.last_error = None
        self._lock = threading.RLock()

    def attach(self, session):
        if self.session is not None:
            raise RuntimeError('回调桥只能绑定一次会话')
        self.session = session

    def _receive(self, kind, client_order_id, order, fill=None):
        with self._lock:
            return self._deliver(kind, client_order_id, order, fill)

    def _deliver(self, kind, client_order_id, order, fill):
        try:
            if self.last_error is not None:
                raise RuntimeError('原生回调桥故障已锁存')
            if self.session is None:
                raise RuntimeError('尚未绑定 SDK 会话')
            snapshot, execution = self.copies.copy(kind, client_order_id, order, fill)
            if kind == 'fill':
                result = self.session.onOrderFill(client_order_id, snapshot, execution)
            elif kind == 'ack':
                result = self.session.onOrderAck(client_order_id, snapshot)
            else:
                result = self.session.onOrderFinished(client_order_id, snapshot)
            if result is not True:
                raise RuntimeError('SDK 未接受回调；须保留原生事实并只读核对')
            return True
        except Exception as error:
            # 包括原生属性读取/枚举/宿主事实转换异常，不穿过 C++ 回调边界。
            if self.last_error is None:
                self.last_error = {'callback': kind, 'error_type': type(error).__name__, 'error': str(error)}
            if self.session is not None:
                try:
                    self.session.on_disconnect(str(self.last_error))
                except Exception:
                    pass
            return False

    def onOrderAck(self, clientOrderId, orderMsg):
        return self._receive('ack', clientOrderId, orderMsg)

    def onOrderFill(self, clientOrderId, orderMsg, execMsg):
        return self._receive('fill', clientOrderId, orderMsg, execMsg)

    def onOrderFinished(self, clientOrderId, orderMsg):
        return self._receive('finished', clientOrderId, orderMsg)


class NativeBarCopies:
    """先复制 Bar 值，再由 owner 线程构造标准行情；不处理累计 Tick 量。"""
    def __init__(self, *, instrument_code, instrument_id, bar_specs,
                 timestamp_ns, price_precision, size_precision):
        self.instrument_code = _text(instrument_code, 'instrument_code')
        self.instrument_id = _text(instrument_id, 'instrument_id')
        self.bar_specs = MappingProxyType(dict(bar_specs))
        if not callable(timestamp_ns):
            raise ValueError('须明确原生 Bar 时间转换函数')
        self.timestamp_ns = timestamp_ns
        self.price_precision = _integer(price_precision, 'price_precision')
        self.size_precision = _integer(size_precision, 'size_precision')

    def copy(self, raw):
        if raw.instCode() != self.instrument_code:
            raise ValueError('Bar 合约不符')
        if type(raw.isHistoricBar) is not bool:
            raise ValueError('isHistoricBar 须为 bool')
        if raw.isHistoricBar:
            return None  # 历史预热另走已评审流程，避免历史 Bar 输出实盘目标。
        if raw.timeUnit not in self.bar_specs:
            raise ValueError('未配置原生 Bar 周期映射')
        stamp = _integer(self.timestamp_ns(raw), 'Bar ts_event')
        values = {name: _number(getattr(raw, source), name, positive=name != 'volume')
                  for name, source in (('open', 'openPrice'), ('high', 'highPrice'),
                                       ('low', 'lowPrice'), ('close', 'closePrice'), ('volume', 'volume'))}
        return dict(values, instrument_id=self.instrument_id,
                    bar_type=self.bar_specs[raw.timeUnit], ts_event=stamp)

    def to_event(self, values, *, ts_init):
        """只在 SDK owner 线程调用；输入为 copy 返回的独立普通值。"""
        from bomber.framework.market.basic.base import InstrumentId, InstrumentMeta, make_bar
        instrument = InstrumentId.from_str(self.instrument_id)
        if values['instrument_id'] != self.instrument_id:
            raise ValueError('已复制 Bar 合约不符')
        return make_bar(**dict(values, instrument_id=instrument,
            ts_init=_integer(ts_init, 'Bar ts_init'),
            meta=InstrumentMeta(instrument, price_precision=self.price_precision,
                                size_precision=self.size_precision)))
