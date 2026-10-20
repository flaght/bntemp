"""由 Windows 交易宿主驱动的单策略 SDK 会话；不创建解释器或事件线程。"""
from decimal import Decimal
import threading

from bomber.framework.market.basic.base import InstrumentId
from bomber.framework.trader.contracts import ExecutionRoute
from bomber.framework.trader.runner import UnifiedStrategyRunner
from bomber.framework.trader.portfolio import PositionManager
from bomber.framework.trader.persistence import JsonStateStore, RuntimeStateManager
from bomber.framework.trader.execution.live.client import BackendExecutionClient
from bomber.framework.trader.execution.lxgbn import LxgbnExecutionBackend
from bomber.framework.trader.execution.planner import LimitTargetOrderPlanner
from bomber.framework.trader.execution.risk import MarketReferencePriceStore, PreTradeRiskManager


class _HostFeed:
    """只声明宿主推送的标准行情订阅；不连接其他行情源。"""
    def __init__(self):
        self.subscriptions = []
        self.is_connected = False

    def subscribe(self, instrument_id, data_type, **kwargs):
        self.subscriptions.append((instrument_id, data_type, kwargs))

    def connect(self):
        self.is_connected = True

    def disconnect(self):
        self.is_connected = False

    def register_trade_tick_handler(self, handler): pass
    def register_quote_tick_handler(self, handler): pass
    def register_bar_handler(self, handler): pass
    def register_custom_bar_handler(self, handler): pass


class LxgbnSession:
    def __init__(self, runner, client, backend, manager, strategy):
        self.runner, self.client, self.backend = runner, client, backend
        self.manager, self.strategy = manager, strategy
        self._owner = None
        self._started = self._stopping = self._dispatching = False

    def _check_thread(self):
        if self._owner != threading.get_ident():
            raise RuntimeError('会话生命周期/行情/poll 必须由同一宿主线程调用')
        if self._dispatching:
            raise RuntimeError('禁止重入会话调度；原生回调只调用 onOrder* 入队')

    def _validate_idle(self):
        snapshot = self.backend.query()
        if (snapshot.active_order_ids or any(not row.status.is_terminal for row in
                self.client.order_state_machine.states())):
            raise RuntimeError('首期活动单/未决发送须只读恢复，禁止自动重发或推断已撤')
        if snapshot.boundary != self.backend.cursor:
            raise RuntimeError('宿主快照/交付流尚未到同一水位，保持闭闸')
        states = {row.client_order_id: row for row in self.client.order_state_machine.states()}
        if states.keys() != self.backend._orders.keys():
            raise RuntimeError('SDK订单关联与公共订单机不一致')
        terminal = {'FullFilled': 'FILLED', 'Cancelled': 'CANCELED', 'Rejected': 'REJECTED'}
        for key, row in self.backend._orders.items():
            state = states[key]
            if (state.status.value != terminal.get(row['terminal'])
                    or state.filled_quantity != Decimal(row['filled'])
                    or state.order_quantity != Decimal(row['intent']['quantity'])
                    or state.side.value != row['intent']['side']):
                raise RuntimeError('SDK订单关联的成交/终态与公共订单机不一致')
        owned = self.runner.position_manager.account_position(self.client.client_id, self.backend.instrument_id)
        if owned != Decimal(str(snapshot.owned_quantity)):
            raise RuntimeError('宿主策略归属与本地账本不符，不能覆盖账本')

    def start(self, *, resume=False):
        if self._started:
            self._check_thread()
            return
        self._owner = threading.get_ident()
        if resume:
            if self.manager.restore() is None:
                raise RuntimeError('SDK恢复文件不存在')
            if any(not state.status.is_terminal for state in self.client.order_state_machine.states()):
                raise RuntimeError('首期活动单/未决发送须只读恢复，禁止自动重发')
            previous = self.runner.target_store.get(self.strategy.strategy_id)
            if previous is not None:
                self.strategy.restore_target_revision(previous.revision)
        elif self.manager.repository.load() is not None:
            raise RuntimeError('新SDK会话不能覆盖原检查点，请显式恢复')
        self.client.start()
        self.backend.poll()
        self._validate_idle()
        self.manager.save()
        self.runner.start()
        self._started = True

    def authorize(self):
        self._check_thread()
        if not self._started or self._stopping:
            raise RuntimeError('SDK会话未运行或正在停止')
        self.backend.disarm()
        self.poll()
        self._validate_idle()
        self.manager.save()
        self.backend.authorize()

    def poll(self):
        self._check_thread()
        if not self._started:
            raise RuntimeError('SDK会话尚未启动')
        self._dispatching = True
        try:
            self.backend.poll()
        except Exception as error:
            self.backend.fail(error)
            raise
        finally:
            self._dispatching = False

    def on_market(self, feed_id, event):
        self._check_thread()
        if not self._started or self._stopping or self.backend.failure:
            raise RuntimeError('SDK会话不接收新增行情')
        self.poll()
        if feed_id not in self.runner._feeds:
            raise ValueError('未知宿主行情feed_id')
        self._dispatching = True
        try:
            self.runner.publish(feed_id, event)
        except Exception as error:
            self.backend.fail(error)
            raise
        finally:
            self._dispatching = False

    def onOrderAck(self, clientOrderId, orderMsg):
        return self.backend.onOrderAck(clientOrderId, orderMsg)

    def onOrderFill(self, clientOrderId, orderMsg, execMsg):
        return self.backend.onOrderFill(clientOrderId, orderMsg, execMsg)

    def onOrderFinished(self, clientOrderId, orderMsg):
        return self.backend.onOrderFinished(clientOrderId, orderMsg)

    def on_disconnect(self, reason):
        self.backend.fail(reason)

    def begin_stop(self):
        self._check_thread()
        self._stopping = True
        self.backend.disarm()
        self.client.cancel_strategy(self.strategy.strategy_id)

    def finish_stop(self):
        self._check_thread()
        if not self._stopping:
            raise RuntimeError('须先 begin_stop，并继续转发回报/poll 至撤单确认')
        self.poll()
        self._validate_idle()
        self.manager.save()
        self.runner.stop()
        self._started = False
        # 宿主此后解除原生回调，再释放 Python 对象/解释器。


def create_session(*, strategy, binding, host, copies, data_bindings, target_key,
                   price_increment, risk_limits, state_file, client_id='lxgbn',
                   max_orders=100, queue_capacity=4096, state_components=None):
    """首期：一个策略、一份执行绑定、一个合约；构造不连接、不报单。"""
    if strategy.strategy_id != binding.strategy_id:
        raise ValueError('策略与宿主绑定不符')
    instrument = InstrumentId.from_str(binding.instrument_id)
    data_bindings = tuple(data_bindings)
    if not data_bindings or any(b.instrument_id != instrument for b in data_bindings):
        raise ValueError('首期行情必须属于绑定合约')
    positions, prices = PositionManager(), MarketReferencePriceStore()
    backend = LxgbnExecutionBackend(client_id, binding, host, copies,
                                  max_orders=max_orders, queue_capacity=queue_capacity)
    client = BackendExecutionClient(client_id,
        LimitTargetOrderPlanner(positions, prices, {instrument: price_increment}), backend, positions,
        PreTradeRiskManager(client_id, positions, prices, instrument_limits={instrument: risk_limits}),
        account_id=binding.account_id, account_reconciliation='off')
    runner = UnifiedStrategyRunner('live', position_manager=positions)
    runner.add_execution_client(client)
    runner.add_market_observer(prices)
    for feed_id in dict.fromkeys(b.feed_id for b in data_bindings):
        runner.add_data_feed(feed_id, _HostFeed())
    runner.add_strategy(strategy, data_bindings=data_bindings,
        execution_routes=(ExecutionRoute(target_key, client_id, instrument),))
    components = dict(state_components or {})
    if 'execution_backend' in components:
        raise ValueError('execution_backend 是SDK保留检查点键')
    components['execution_backend'] = backend
    manager = RuntimeStateManager(JsonStateStore(state_file), runner.target_store,
        runner.portfolio_coordinator, positions, order_machines={client_id: client.order_state_machine},
        state_components=components)
    manager.enable_execution_autosave(client, backend)
    backend.report_check = lambda: not client.report_errors
    backend.submission_check = lambda: client.is_reconciled and not client.report_errors
    return LxgbnSession(runner, client, backend, manager, strategy)
