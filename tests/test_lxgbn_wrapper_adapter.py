"""厂商导出形状的替身 + 真实 SDK 执行核心；不加载 Windows 库。"""
from dataclasses import replace
from decimal import Decimal
from enum import IntEnum
from importlib import import_module
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace as NS
import unittest

from tests import test_shared_account as shared_fixtures


class Direction(IntEnum):
    Buy = 17
    Sell = 23


class Offset(IntEnum):
    Open = 31
    Close = 32
    CloseToday = 33
    CloseYd = 34


class Status(IntEnum):
    Blank = 101
    Submited = 102
    Acked = 103
    FullFilled = 104
    Rejected = 105
    Cancelled = 106


class Hedge(IntEnum):
    Speculation = 41
    Hedge = 42


class WrapperAdapterTests(unittest.TestCase):
    def setUp(self):
        fixture = shared_fixtures.SharedAccountTests('test_order_budget_is_saved_before_transport_and_not_refunded_after_send')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.modules = fixture.native_modules()
        path = Path(__file__).resolve().parents[1] / 'scripts/integration/lxgbn/stg_wrapper_adapter.py'
        self.a = NS(**runpy.run_path(str(path)))
        self.contracts = import_module('bomber.framework.lxgbn')
        self.binding = self.contracts.Binding('account', 'binding', 'one', '20261029',
                                              'native-session', 'rb2701.SHFE')
        self.links = self.a.CommandLinks(self.binding)
        self.rp = NS(OrderMsg_dir_t=Direction, OrderMsg_offset_t=Offset,
                     status_t=Status, HedgeFlag_type_t=Hedge)
        self.enums = self.a.NativeEnums.from_module(self.rp)
        self.fact = self.a.CallbackFacts(self.binding, 1, 100, 1,
                                         fill_ts_ns=101, fill_hedge=Hedge.Speculation)
        self.copies = self.a.NativeCallbackCopies(self.links, self.enums,
            instrument_code='rb2701', strategy_id=42, quantity_field='volumeSubmitted',
            read_facts=lambda *args: self.fact)
        self.bridge = self.a.NativeCallbacks(self.copies)
        self.sent, self.cancelled = [], []
        self.port = self.a.NativeHostPort(self.links, send=self.send,
                                          cancel=self.cancelled.append, query=self.query)
        self.sequence = 0
        self.owned, self.active = Decimal(0), ()

    def command(self, sdk_id=1, request_id='request-1'):
        return self.contracts.OrderCommand(sdk_id, request_id, self.binding, 'BUY',
                                           Decimal(2), Decimal(3501), 'OPEN')

    def send(self, command):
        self.assertEqual(self.links.command(command.client_order_id), command)
        self.sent.append(command)
        return self.contracts.SubmitResult('queued')

    def query(self):
        return self.contracts.BindingSnapshot(self.binding, 'mock-native-revision',
            self.sequence, self.owned, self.active, initialized=True, complete=True,
            ready=True, normal_limit=True, generic_close=True, acked_is_accepted=True)

    def order(self, **changes):
        fields = dict(Id=501, direction=Direction.Buy, price=3501,
            volumePlanned=99, volumeSubmitted=2, volumeTraded=0, volumeCancelled=0,
            offset=Offset.Open, hedgeFlag=Hedge.Speculation, status=Status.Acked,
            strategyId=42, portfolioId=7, GetInst=lambda: 'rb2701')
        fields.update(changes)
        return NS(**fields)

    def fill(self, **changes):
        fields = dict(tradeId=701, orderId=501, price=3501, volume=1,
                      direction=Direction.Buy, offset=Offset.Open, strategyId=42)
        fields.update(changes)
        return NS(**fields)

    def session(self):
        class Strategy(self.modules.template.StrategyTemplate):
            def __init__(self):
                super().__init__('one')
                self.notifications = []
            def on_trade_tick(self, key, tick):
                self.set_target('main', 2, tick.ts_event)
            def on_order(self, event):
                self.notifications.append(('order', event.status.value, self.position('main')))
            def on_fill(self, event):
                self.notifications.append(('fill', event.quantity, self.position('main')))
        self.strategy = Strategy()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_file = Path(directory.name) / 'state.json'
        base = import_module('bomber.framework.market.basic.base')
        contracts = import_module('bomber.framework.trader.contracts')
        risk = import_module('bomber.framework.trader.execution.risk')
        instrument = base.InstrumentId.from_str(self.binding.instrument_id)
        self.sdk = self.contracts.create_session(strategy=self.strategy, binding=self.binding,
            host=self.port, copies=self.a.VALUE_COPIES,
            data_bindings=(contracts.DataBinding('tick', 'lxg', instrument, base.DataType.TRADE_TICK),),
            target_key='main', price_increment='1',
            risk_limits=risk.RiskLimits(max_order_quantity=5, max_abs_position=5),
            state_file=self.state_file)
        self.bridge.attach(self.sdk)
        self.sdk.start()
        self.sdk.authorize()
        tick = base.TradeTick()
        tick.instrument_id, tick.price, tick.ts_event = instrument, Decimal(3500), 100
        self.sdk.on_market('lxg', tick)
        return self.sdk

    def delivery(self):
        self.sequence += 1
        self.fact = replace(self.fact, delivery_sequence=self.sequence)

    def test_real_sdk_order_fill_notifications_and_copy_lifetime(self):
        sdk = self.session()
        self.delivery()
        self.assertTrue(self.bridge.onOrderAck(1, self.order()))
        self.delivery()
        order, fill = self.order(volumeTraded=1), self.fill()
        results = []
        thread = threading.Thread(target=lambda: results.append(self.bridge.onOrderFill(1, order, fill)))
        thread.start(); thread.join()
        self.assertEqual(results, [True])
        self.assertEqual(self.strategy.position('main'), 0)
        fill.volume = 100  # 原生对象回收/复用不能改变已复制值。
        order.volumeSubmitted = 100
        self.owned, self.active = Decimal(1), (1,)
        sdk.poll()
        self.assertEqual(self.strategy.notifications, [
            ('order', 'ACCEPTED', Decimal(0)),
            ('order', 'PARTIALLY_FILLED', Decimal(1)), ('fill', Decimal(1), Decimal(1))])
        self.delivery()
        self.assertTrue(self.bridge.onOrderFill(1, self.order(volumeTraded=1), self.fill()))
        sdk.poll()
        self.assertEqual(len(self.strategy.notifications), 3)
        sdk.begin_stop()
        self.assertEqual(self.cancelled[0].request_id, self.sent[0].request_id)
        with self.assertRaisesRegex(RuntimeError, '活动单'):
            sdk.finish_stop()
        self.delivery()
        self.active = ()
        self.assertTrue(self.bridge.onOrderFinished(1, self.order(status=Status.Cancelled, volumeTraded=1)))
        sdk.finish_stop()
        self.assertEqual(self.strategy.notifications[-1], ('order', 'CANCELED', Decimal(1)))

    def test_command_registered_before_synchronous_native_callback(self):
        def synchronous(command):
            result = self.send(command)
            self.delivery()
            self.assertTrue(self.bridge.onOrderAck(command.client_order_id, self.order()))
            return result
        self.port._send = synchronous
        sdk = self.session()
        sdk.poll()
        self.assertEqual(self.strategy.notifications, [('order', 'ACCEPTED', Decimal(0))])

    def test_unknown_send_retains_original_command_and_sdk_pending(self):
        self.port._send = lambda command: self.contracts.SubmitResult('unknown', 'native timeout')
        with self.assertRaisesRegex(Exception, '发送结果不确定'):
            self.session()
        self.assertEqual(self.links.command(1).client_order_id, 1)
        self.assertEqual(self.sdk.client.order_state_machine.states()[0].status.value, 'PENDING')
        self.assertFalse(self.sdk.backend.is_armed)
        self.assertTrue(self.state_file.is_file())

    def test_conversion_failure_is_latched_and_does_not_escape_native_callback(self):
        sdk = self.session()
        self.fact = replace(self.fact, fill_ts_ns=None)
        self.assertFalse(self.bridge.onOrderFill(1, self.order(volumeTraded=1), self.fill()))
        self.assertIn('fill_ts_ns', self.bridge.last_error['error'])
        self.assertFalse(sdk.backend.is_armed)
        self.assertFalse(self.bridge.onOrderAck(1, self.order()))
        self.assertEqual(self.strategy.position('main'), 0)

    def test_default_unconfirmed_port_blocks_query(self):
        port = self.a.NativeHostPort(self.links)
        with self.assertRaisesRegex(RuntimeError, '尚未全部确认'):
            port.query_binding()
        self.assertEqual(port.send_order(self.command()).outcome, 'rejected')
        self.assertFalse(self.sent)

    def test_port_never_guesses_native_integer_result(self):
        self.port._send = lambda command: 123
        with self.assertRaisesRegex(RuntimeError, '未规范化'):
            self.port.send_order(self.command())
        self.assertEqual(self.links.command(1).request_id, 'request-1')

    def test_duplicate_sdk_or_request_ids_are_not_sent_twice(self):
        self.port.send_order(self.command())
        for command in (self.command(), self.command(2, 'request-1')):
            with self.assertRaisesRegex(ValueError, '已登记'):
                self.port.send_order(command)
        self.assertEqual(len(self.sent), 1)

    def test_cancel_unknown_id_does_not_call_native(self):
        with self.assertRaisesRegex(ValueError, '未知'):
            self.port.cancel_order(1)
        self.assertFalse(self.cancelled)

    def test_query_uses_actual_provider_and_rejects_wrong_binding(self):
        expected = self.query()
        self.port._query = lambda: expected
        self.assertIs(self.port.query_binding(), expected)
        self.port._query = lambda: replace(expected, binding=replace(self.binding, account_id='other'))
        with self.assertRaisesRegex(ValueError, '同绑定'):
            self.port.query_binding()

    def test_exported_names_and_same_sequence_for_both_snapshots(self):
        self.links.register(self.command())
        order, fill = self.copies.copy('fill', 1, self.order(volumeTraded=1), self.fill())
        self.assertEqual((order.native_order_id, fill.native_order_id, fill.trade_id), ('501', '501', '701'))
        self.assertEqual((order.delivery_sequence, fill.delivery_sequence), (1, 1))
        self.assertEqual(order.quantity, 2)  # 明确取 volumeSubmitted，而非 volumePlanned=99。
        self.assertEqual((order.ts_event, fill.ts_event), (100, 101))

    def test_missing_exported_field_fails_without_fallback(self):
        self.links.register(self.command())
        order = self.order()
        del order.volumeSubmitted
        with self.assertRaises(AttributeError):
            self.copies.copy('ack', 1, order)

    def test_unknown_enum_and_non_speculation_fail(self):
        self.links.register(self.command())
        for fields in ({'direction': 999}, {'direction': True}, {'status': 999}, {'hedgeFlag': Hedge.Hedge}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.copies.copy('ack', 1, self.order(**fields))

    def test_missing_fill_or_unknown_callback_id_returns_false(self):
        for bad in (None, True, 999):
            with self.subTest(client_order_id=bad):
                bridge = self.a.NativeCallbacks(self.copies)
                disconnected = []
                bridge.attach(NS(on_disconnect=disconnected.append))
                self.assertFalse(bridge.onOrderAck(bad, self.order()))
                self.assertEqual(len(disconnected), 1)
        self.links.register(self.command())
        bridge = self.a.NativeCallbacks(self.copies)
        bridge.attach(NS(on_disconnect=lambda reason: None))
        self.assertFalse(bridge.onOrderFill(1, self.order(), None))

    def test_sdk_queue_rejection_is_visible_and_latched(self):
        self.links.register(self.command())
        disconnected = []
        self.bridge.attach(NS(onOrderAck=lambda *args: False, on_disconnect=disconnected.append))
        self.assertFalse(self.bridge.onOrderAck(1, self.order()))
        self.assertIn('SDK 未接受', self.bridge.last_error['error'])
        self.assertEqual(len(disconnected), 1)

    def test_delivery_gap_is_not_renumbered_by_adapter(self):
        sdk = self.session()
        self.fact = replace(self.fact, delivery_sequence=2)
        self.assertTrue(self.bridge.onOrderAck(1, self.order()))
        with self.assertRaisesRegex(RuntimeError, '游标缺口'):
            sdk.poll()
        self.assertFalse(sdk.backend.is_armed)

    def test_cancel_rejection_keeps_real_sdk_working_order(self):
        sdk = self.session()
        self.delivery()
        self.assertTrue(self.bridge.onOrderAck(1, self.order()))
        self.delivery()
        self.fact = replace(self.fact, rejection_of='cancel', reason='native cancel denied')
        self.assertTrue(self.bridge.onOrderFinished(1, self.order(status=Status.Rejected)))
        sdk.poll()
        self.assertEqual(sdk.client.order_state_machine.states()[0].status.value, 'ACCEPTED')
        self.assertEqual(self.strategy.strategy_working_quantity('main'), 2)
        self.assertEqual(sdk.backend.cancel_errors, ['native cancel denied'])
        self.assertEqual(len(self.strategy.notifications), 1)

    def test_wrong_account_contract_strategy_order_and_quantity_fail(self):
        self.links.register(self.command())
        cases = [self.order(GetInst=lambda: 'ag2701'), self.order(strategyId=999),
                 self.order(direction=Direction.Sell), self.order(volumeSubmitted=3),
                 self.order(volumeTraded=3), self.order(offset=Offset.Close)]
        for order in cases:
            with self.assertRaises(ValueError):
                self.copies.copy('ack', 1, order)
        self.fact = replace(self.fact, binding=replace(self.binding, account_id='other'))
        with self.assertRaisesRegex(ValueError, '绑定'):
            self.copies.copy('ack', 1, self.order())

    def test_trade_order_id_direction_strategy_and_values_checked(self):
        self.links.register(self.command())
        for fields in ({'orderId': 999}, {'strategyId': 999}, {'direction': Direction.Sell},
                       {'volume': '0.5'}, {'price': 'NaN'}, {'offset': Offset.Close}, {'tradeId': ''}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.copies.copy('fill', 1, self.order(volumeTraded=1), self.fill(**fields))

    def test_native_id_reassignment_and_reuse_rejected(self):
        self.links.register(self.command())
        self.copies.copy('ack', 1, self.order())
        with self.assertRaisesRegex(ValueError, '发生变化'):
            self.copies.copy('ack', 1, self.order(Id=502))
        self.links.register(self.command(2, 'request-2'))
        with self.assertRaisesRegex(ValueError, '复用'):
            self.copies.copy('ack', 2, self.order())

    def test_rejection_requires_explicit_object(self):
        self.links.register(self.command())
        with self.assertRaisesRegex(ValueError, '报单或撤单'):
            self.copies.copy('finished', 1, self.order(status=Status.Rejected))
        self.fact = replace(self.fact, reason='cancel denied', rejection_of='cancel')
        order, _ = self.copies.copy('finished', 1, self.order(status=Status.Rejected))
        self.assertEqual((order.reason, order.rejection_of), ('cancel denied', 'cancel'))

    def test_tail_space_enum_not_silently_accepted(self):
        rp = NS(**vars(self.rp))
        setattr(rp, 'status_t ', rp.status_t)
        del rp.status_t
        with self.assertRaises(AttributeError):
            self.a.NativeEnums.from_module(rp)

    def test_bar_copy_is_independent_with_explicit_time_and_period(self):
        copies = self.a.NativeBarCopies(instrument_code='rb2701', instrument_id='rb2701.SHFE',
            bar_specs={'native-M1': '1-MINUTE'}, timestamp_ns=lambda raw: raw.timeStamp * 1000,
            price_precision=0, size_precision=0)
        raw = NS(instCode=lambda: 'rb2701', isHistoricBar=False, timeUnit='native-M1',
                 timeStamp=123456, openPrice=3500, highPrice=3502, lowPrice=3498,
                 closePrice=3501, volume=10)
        values = copies.copy(raw)
        raw.closePrice = 9999
        self.assertEqual((values['close'], values['ts_event'], values['bar_type']),
                         (Decimal(3501), 123456000, '1-MINUTE'))
        raw.isHistoricBar = True
        self.assertIsNone(copies.copy(raw))
        raw.isHistoricBar = False
        raw.timeUnit = 'unconfirmed'
        with self.assertRaisesRegex(ValueError, '周期映射'):
            copies.copy(raw)


if __name__ == '__main__':
    unittest.main()
