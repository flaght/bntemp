"""无柜台接入示例：真实 SDK/行情模型，内存宿主，不连接或发送真实委托。

在已安装本期 wheel 的 Windows Python 3.12 中直接运行此文件。
"""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from bomber.framework.lxgbn import (
    Binding, BindingSnapshot, CallbackCopies, FillSnapshot, OrderSnapshot,
    SubmitResult, create_session,
)
from bomber.framework.market.basic.base import DataType, InstrumentId, make_trade_tick
from bomber.framework.trader.contracts import DataBinding
from bomber.framework.trader.execution.contracts import AmbiguousOrderSubmission
from bomber.framework.trader.execution.risk import RiskLimits
from bomber.framework.trader.template import StrategyTemplate


class TargetStrategy(StrategyTemplate):
    """目标由测试设置，初始只读；set_target 是目标净仓，不是下单数量。"""
    def __init__(self, strategy_id):
        super().__init__(strategy_id)
        self.desired = None
        self.fill_positions = []

    def on_trade_tick(self, data_key, tick):
        if self.desired is not None:
            self.set_target('main', self.desired, tick.ts_event)

    def on_fill(self, event):
        self.fill_positions.append(self.position('main'))


class MemoryHost:
    """仅用于示例的合成事实源；不得复制到生产当作真实查询/序号。"""
    def __init__(self, binding):
        self.binding = binding
        self.sdk = None
        self.commands = []
        self.cancel_requests = []
        self.active = set()
        self.owned = Decimal(0)
        self.sequence = 0
        self.trades = set()
        self.filled = {}
        self.outcome = 'queued'
        self.lock = threading.RLock()

    def query_binding(self):
        with self.lock:
            return BindingSnapshot(self.binding, f'memory-{self.sequence}',
                self.sequence, self.owned, tuple(sorted(self.active)),
                initialized=True, complete=True, ready=True, normal_limit=True,
                generic_close=True, acked_is_accepted=True)

    def send_order(self, command):
        with self.lock:
            self.commands.append(command)
            self.active.add(command.client_order_id)
            return SubmitResult(self.outcome)

    def cancel_order(self, client_order_id):
        with self.lock:
            self.cancel_requests.append(client_order_id)
            # 请求返回不代表已撤；由 emit('Cancelled') 提供事实。

    def emit(self, status, *, index=-1, quantity='1', trade_id='T1',
             cumulative=None, rejection_of='order', sequence_step=1):
        """一个 emit = 一次回调 = 一个交付序号，含重复成交的重新交付。"""
        with self.lock:
            command = self.commands[index]
            sdk_id = command.client_order_id
            native_id = f'memory-order-{sdk_id}'
            stamp = 1_792_547_964_603_975_000
            if status == 'Fill':
                key = (native_id, trade_id)
                if key not in self.trades:
                    self.trades.add(key)
                    q = Decimal(quantity)
                    self.filled[sdk_id] = self.filled.get(sdk_id, Decimal(0)) + q
                    self.owned += q if command.side == 'BUY' else -q
                    if self.filled[sdk_id] == command.quantity:
                        self.active.discard(sdk_id)
            elif status in {'Cancelled', 'FullFilled'} or (status == 'Rejected' and rejection_of == 'order'):
                self.active.discard(sdk_id)
            self.sequence += sequence_step
            filled = self.filled.get(sdk_id, Decimal(0)) if cumulative is None else Decimal(cumulative)
            order = OrderSnapshot(self.binding, self.sequence, native_id,
                command.side, command.quantity, 'Acked' if status == 'Fill' else status,
                filled, stamp, 1, 'Speculation', rejection_of=rejection_of)
            if status == 'Fill':
                fill = FillSnapshot(self.binding, self.sequence, native_id, trade_id,
                    command.side, Decimal(quantity), command.price,
                    command.position_effect, stamp, 'Speculation')
                return self.sdk.onOrderFill(sdk_id, order, fill)
            if status in {'Cancelled', 'Rejected', 'FullFilled'}:
                return self.sdk.onOrderFinished(sdk_id, order)
            return self.sdk.onOrderAck(sdk_id, order)


def assemble(host, state_file):
    strategy = TargetStrategy(host.binding.strategy_id)
    instrument = InstrumentId.from_str(host.binding.instrument_id)
    sdk = create_session(strategy=strategy, binding=host.binding, host=host,
        # 示例已经构造值快照；真实宿主需先校验/复制原生字段，见接入文档。
        copies=CallbackCopies(order=lambda value: value, fill=lambda value: value),
        data_bindings=(DataBinding('tick', 'lxg', instrument, DataType.TRADE_TICK),),
        target_key='main', price_increment='1',
        risk_limits=RiskLimits(max_order_quantity=5, max_abs_position=5),
        state_file=state_file, max_orders=20)
    host.sdk = sdk
    return sdk, strategy, instrument


def push_tick(sdk, instrument):
    stamp = time.time_ns()
    sdk.on_market('lxg', make_trade_tick(instrument, '3500', '1',
                                       'market-probe', stamp, stamp))


class SdkAcceptanceExamples(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix='lxgbn-sdk-example-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.json'
        binding = Binding('memory-account', 'memory-binding', 'probe-strategy',
                          '20261021', 'memory-native-session', 'rb2701.SHFE')
        self.host = MemoryHost(binding)
        self.sdk, self.strategy, self.instrument = assemble(self.host, self.path)
        self.sdk.start()

    def send_target(self, quantity):
        self.strategy.desired = Decimal(quantity)
        push_tick(self.sdk, self.instrument)

    def test_01_readonly_start(self):
        push_tick(self.sdk, self.instrument)
        self.assertFalse(self.host.commands)
        self.assertFalse(self.sdk.backend.is_armed)
        self.sdk.begin_stop()
        self.sdk.finish_stop()

    def test_02_full_fill_duplicate_and_callback_thread(self):
        self.sdk.authorize()
        self.send_target('1')
        command = self.host.commands[0]
        self.assertEqual((command.side, command.quantity, command.price,
                          command.position_effect, command.attribute, command.hedge_flag),
                         ('BUY', Decimal(1), Decimal(3501), 'OPEN', 1, 'Speculation'))
        self.assertTrue(self.host.emit('Acked'))
        results = []
        worker = threading.Thread(target=lambda: results.append(self.host.emit('Fill')))
        worker.start()
        worker.join()
        self.assertEqual(results, [True])
        self.assertEqual(self.strategy.position('main'), 0)  # 尚未 poll
        self.assertTrue(self.host.emit('Fill'))  # 相同成交 ID 重复交付
        self.assertTrue(self.host.emit('FullFilled'))
        self.sdk.poll()
        self.assertEqual(self.strategy.position('main'), 1)
        self.assertEqual(self.strategy.fill_positions, [Decimal(1)])
        self.sdk.begin_stop()
        self.sdk.finish_stop()

    def test_03_partial_fill_and_two_phase_stop(self):
        self.sdk.authorize()
        self.send_target('2')
        self.assertTrue(self.host.emit('Fill', quantity='1'))
        self.sdk.poll()
        self.assertEqual(self.strategy.position('main'), 1)
        self.sdk.begin_stop()
        self.assertEqual(self.host.cancel_requests, [1])
        with self.assertRaisesRegex(RuntimeError, '活动单'):
            self.sdk.finish_stop()
        self.assertTrue(self.host.emit('Cancelled'))
        self.sdk.finish_stop()
        self.assertEqual(self.strategy.fill_positions, [Decimal(1)])

    def test_04_resume_terminal_checkpoint(self):
        self.sdk.authorize()
        self.send_target('1')
        self.assertTrue(self.host.emit('Fill'))
        self.sdk.poll()
        self.sdk.begin_stop()
        self.sdk.finish_stop()
        restored, strategy, instrument = assemble(self.host, self.path)
        restored.start(resume=True)
        self.assertEqual(strategy.position('main'), 1)
        self.assertFalse(restored.backend.is_armed)
        restored.authorize()
        strategy.desired = Decimal(0)
        push_tick(restored, instrument)
        command = self.host.commands[-1]
        self.assertEqual((command.client_order_id, command.side, command.position_effect),
                         (2, 'SELL', 'CLOSE'))
        self.assertTrue(self.host.emit('Fill', trade_id='T2'))
        restored.poll()
        self.assertEqual(strategy.position('main'), 0)
        restored.begin_stop()
        restored.finish_stop()

    def test_05_unknown_send_keeps_pending(self):
        self.host.outcome = 'unknown'
        self.sdk.authorize()
        with self.assertRaises(AmbiguousOrderSubmission):
            self.send_target('1')
        self.assertEqual(len(self.host.commands), 1)
        self.assertEqual(self.sdk.client.order_state_machine.states()[0].status.value, 'PENDING')
        self.assertEqual(self.strategy.strategy_working_quantity('main'), 1)
        self.assertFalse(self.sdk.backend.is_armed)
        self.assertTrue(self.path.is_file())
        # 合成故障用例结束；真实宿主必须保留检查点/原生事实做只读恢复。

    def test_06_delivery_gap_closes_gate(self):
        self.sdk.authorize()
        self.send_target('1')
        self.assertTrue(self.host.emit('Acked', sequence_step=2))
        with self.assertRaisesRegex(RuntimeError, '游标缺口'):
            self.sdk.poll()
        self.assertFalse(self.sdk.backend.is_armed)

    def test_07_wrong_poll_thread_is_rejected(self):
        failures = []
        def wrong_thread():
            try:
                self.sdk.poll()
            except RuntimeError as error:
                failures.append(str(error))
        worker = threading.Thread(target=wrong_thread)
        worker.start()
        worker.join()
        self.assertEqual(len(failures), 1)
        self.assertIn('同一宿主线程', failures[0])
        self.sdk.begin_stop()
        self.sdk.finish_stop()

    def test_08_incomplete_snapshot_blocks_start(self):
        self.sdk.begin_stop()
        self.sdk.finish_stop()
        self.host.query_binding = lambda: BindingSnapshot(
            self.host.binding, 'incomplete', 0, Decimal(0))
        sdk, _, _ = assemble(self.host, Path(self.directory.name) / 'other.json')
        with self.assertRaisesRegex(RuntimeError, '不完整'):
            sdk.start()
        self.assertFalse(self.host.commands)


if __name__ == '__main__':
    unittest.main(verbosity=2)
