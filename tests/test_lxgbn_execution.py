"""宿主调用项目 SDK；真实 Runner/OMS/归属/持久化，原生宿主端口使用替身。"""
from dataclasses import replace
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from tests import test_execution_composition as fixtures
from tests import test_shared_account as shared_fixtures
from bomber.framework.lxgbn import (Binding, BindingSnapshot, OrderSnapshot, FillSnapshot,
    SubmitResult, CallbackCopies, create_session)


class LxgbnExecutionTests(unittest.TestCase):
    setUp = fixtures.ExecutionCompositionTests.setUp
    native_modules = shared_fixtures.SharedAccountTests.native_modules

    def session(self, directory, *, resume=False, capacity=4096, start=True, authorize=True):
        modules = self.native_modules()
        class Strategy(modules.template.StrategyTemplate):
            def __init__(self):
                super().__init__('one')
                self.target = 1
                self.fills = []
            def on_trade_tick(self, key, event):
                self.set_target('main', self.target, event.ts_event)
            def on_fill(self, event):
                self.fills.append((event, self.position('main')))
        binding = Binding('account', 'bound', 'one', '20261020', 'native-session', str(self.instrument))
        class Host:
            def __init__(self):
                self.sent, self.cancelled = [], []
                self.seq = 0
                self.snapshot = BindingSnapshot(binding, 'revision-1', 0, Decimal(0),
                    initialized=True, complete=True, ready=True, normal_limit=True,
                    generic_close=True, acked_is_accepted=True)
            def query_binding(self):
                return replace(self.snapshot, boundary=self.seq)
            def send_order(self, command):
                self.sent.append(command)
                return SubmitResult('queued')
            def cancel_order(self, client_id):
                self.cancelled.append(client_id)
        host, strategy = Host(), Strategy()
        copies = CallbackCopies(lambda raw: OrderSnapshot(**raw), lambda raw: FillSnapshot(**raw))
        contracts = import_module('bomber.framework.trader.contracts')
        session = create_session(strategy=strategy, binding=binding, host=host, copies=copies,
            data_bindings=(contracts.DataBinding('tick', 'lxg', self.instrument, fixtures.DataType.TRADE_TICK),),
            target_key='main', price_increment='1', risk_limits=self.risk.RiskLimits(
                max_order_quantity=5, max_abs_position=10), state_file=Path(directory)/'state.json',
            queue_capacity=capacity)
        self.host, self.sdk, self.strategy, self.binding = host, session, strategy, binding
        if start:
            session.start(resume=resume)
            if authorize:
                session.authorize()
        return session

    def send(self, quantity=1):
        self.strategy.target = quantity
        base = import_module('bomber.framework.market.basic.base')
        tick = base.TradeTick()
        tick.instrument_id, tick.price, tick.ts_event = self.instrument, Decimal(3000), time.time_ns()
        self.sdk.on_market('lxg', tick)

    def event(self, status='Fill', *, quantity=1, cumulative=1, price=3000, trade='trade1', sdk_id=None,
              rejection_of='order', reason='', mutation=None):
        host = self.host
        command = host.sent[-1]
        sdk_id = command.client_order_id if sdk_id is None else sdk_id
        host.seq += 1
        native_id = 'native' + str(sdk_id)
        order = dict(binding=self.binding, delivery_sequence=host.seq, native_order_id=native_id,
            side=command.side, quantity=command.quantity, status='Acked' if status == 'Fill' else status,
            cumulative_filled=Decimal(cumulative), ts_event=100, rejection_of=rejection_of, reason=reason,
            attribute=1, hedge_flag='Speculation')
        if mutation:
            order.update(mutation)
        if status == 'Fill':
            fill = dict(binding=self.binding, delivery_sequence=host.seq, native_order_id=native_id,
                trade_id=trade, side=command.side, quantity=Decimal(quantity), price=Decimal(price),
                position_effect=command.position_effect, ts_event=100, hedge_flag='Speculation')
            result = self.sdk.onOrderFill(sdk_id, order, fill)
        elif status in {'Cancelled', 'Rejected', 'FullFilled'}:
            result = self.sdk.onOrderFinished(sdk_id, order)
        else:
            result = self.sdk.onOrderAck(sdk_id, order)
        return result, order

    def state(self):
        return self.sdk.client.order_state_machine.states()[0]

    def test_host_pushes_market_strategy_sends_normal_command_without_extra_tif(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send()
            command = self.host.sent[0]
            self.assertIs(type(command.client_order_id), int)
            self.assertEqual((command.attribute, command.position_effect, command.price), (1, 'OPEN', 3001))
            self.assertNotIn('time_in_force', vars(command))
            self.assertEqual(self.state().status.value, 'PENDING')
            self.assertEqual(self.strategy.position('main'), 0)
            self.assertIsNone(self.sdk.client.account_observation)

    def test_write_ahead_is_visible_in_host_before_send(self):
        with TemporaryDirectory() as d:
            self.session(d)
            original = self.host.send_order
            def send(command):
                state = json.loads((Path(d)/'state.json').read_text())['payload']
                self.assertEqual(state['orders']['lxgbn'][0]['status'], 'PENDING')
                self.assertIn(command.request_id, [row['client_order_id'] for row in state['positions']['attribution']['orders'].values()])
                return original(command)
            self.host.send_order = send
            self.send()
            self.assertEqual(len(self.host.sent), 1)

    def test_callback_before_host_send_returns_only_queues_then_poll_updates_strategy(self):
        with TemporaryDirectory() as d:
            self.session(d)
            original = self.host.send_order
            def send(command):
                result = original(command)
                self.assertTrue(self.event()[0])
                self.assertEqual(self.strategy.position('main'), 0)
                return result
            self.host.send_order = send
            self.send(); self.sdk.poll()
            self.assertEqual(self.strategy.position('main'), 1)
            self.assertEqual(self.strategy.fills[0][1], 1)

    def test_unique_fills_and_duplicate_ack_fill_finished_are_idempotent(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(5)
            self.event('Acked', cumulative=0); self.event('Acked', cumulative=0)
            self.event(quantity=2, cumulative=2, trade='E1'); self.event(quantity=2, cumulative=2, trade='E1')
            self.event(quantity=3, cumulative=5, trade='E2')
            self.event('FullFilled', cumulative=5); self.event('FullFilled', cumulative=5)
            self.sdk.poll()
            self.assertEqual(self.strategy.position('main'), 5)
            self.assertEqual([row[0].quantity for row in self.strategy.fills], [2, 3])
            self.assertEqual(self.state().status.value, 'FILLED')
            self.assertFalse(self.sdk.client.report_errors)
            self.assertIsNone(self.strategy.fills[0][0].identity.venue_order_id)

    def test_partial_fill_before_ack_and_terminal_before_last_fill(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(5)
            self.event(quantity=2, cumulative=2)
            self.event('Acked', cumulative=2)
            self.event('Cancelled', cumulative=3)
            self.sdk.poll()
            self.assertFalse(self.state().status.is_terminal)
            self.event(quantity=1, cumulative=3, trade='E2')
            self.sdk.poll()
            self.assertEqual(self.state().status.value, 'CANCELED')
            self.assertEqual(self.strategy.position('main'), 3)
            self.assertEqual(self.sdk.runner.position_manager.working_quantity('lxgbn', self.instrument), 0)

    def test_same_price_quantity_time_different_trade_ids_both_count(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(2)
            self.event(trade='a'); self.event(trade='b', cumulative=2)
            self.sdk.poll()
            self.assertEqual(self.strategy.position('main'), 2)

    def test_reused_trade_id_conflict_latches_failure(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(3)
            self.event(); self.event(price=3001)
            with self.assertRaisesRegex(RuntimeError, '内容冲突'):
                self.sdk.poll()
            self.assertFalse(self.sdk.backend.is_armed)

    def test_cancel_rejection_keeps_working_order(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(2)
            self.event('Acked', cumulative=0)
            self.event('Rejected', cumulative=0, rejection_of='cancel', reason='already pending')
            self.sdk.poll()
            self.assertEqual(self.state().status.value, 'ACCEPTED')
            self.assertEqual(self.sdk.runner.position_manager.working_quantity('lxgbn', self.instrument), 2)
            self.assertEqual(self.sdk.backend.cancel_errors, ['already pending'])

    def test_async_reject_has_no_fill_and_releases_working(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send()
            self.event('Rejected', cumulative=0, reason='risk rejected'); self.sdk.poll()
            self.assertEqual(self.state().status.value, 'REJECTED')
            self.assertEqual(self.strategy.position('main'), 0)
            self.assertFalse(self.sdk.backend.is_armed)

    def test_definitely_unsent_host_rejection_is_standard_report(self):
        with TemporaryDirectory() as d:
            self.session(d)
            self.host.send_order = lambda cmd: SubmitResult('rejected', 'local validation')
            self.send()
            self.assertEqual(self.state().status.value, 'REJECTED')
            self.assertEqual(self.sdk.runner.position_manager.working_quantity('lxgbn', self.instrument), 0)

    def test_unknown_send_retains_pending_and_recovery_does_not_resend(self):
        with TemporaryDirectory() as d:
            self.session(d)
            original = self.host.send_order
            def send(command):
                original(command)
                raise TimeoutError('host failure after enqueue')
            self.host.send_order = send
            with self.assertRaises(self.contracts.AmbiguousOrderSubmission):
                self.send()
            self.assertEqual(self.state().status.value, 'PENDING')
            self.assertEqual(self.sdk.runner.position_manager.working_quantity('lxgbn', self.instrument), 1)
            self.assertFalse(self.sdk.backend.is_armed)
            with self.assertRaisesRegex(RuntimeError, '只读恢复'):
                self.session(d, resume=True)
            self.assertFalse(self.host.sent)

    def test_write_failure_prevents_host_send(self):
        with TemporaryDirectory() as d:
            self.session(d)
            self.sdk.manager.repository.save = lambda *a, **kw: (_ for _ in ()).throw(OSError('disk full'))
            with self.assertRaises(OSError):
                self.send()
            self.assertFalse(self.host.sent)
            self.assertFalse(self.sdk.backend.is_armed)

    def test_save_failure_after_fill_latches_gate(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(); self.event()
            self.sdk.manager.repository.save = lambda *a, **kw: (_ for _ in ()).throw(OSError('disk full'))
            with self.assertRaises(OSError):
                self.sdk.poll()
            with self.assertRaisesRegex(RuntimeError, '故障已锁存'):
                self.sdk.poll()
            self.assertFalse(self.sdk.backend.is_armed)

    def test_callback_copies_native_object_before_return(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send()
            _, raw = self.event('Cancelled', cumulative=0)
            raw['cumulative_filled'] = Decimal(100)
            self.sdk.poll()
            self.assertEqual(self.state().status.value, 'CANCELED')

    def test_unknown_id_and_converter_exception_do_not_escape_native_callback(self):
        for bad_id in (999, True):
            with TemporaryDirectory() as d:
                self.session(d); self.send()
                self.assertFalse(self.event('Acked', cumulative=0, sdk_id=bad_id)[0])
                self.assertFalse(self.sdk.backend.is_armed)
        with TemporaryDirectory() as d:
            self.session(d)
            self.assertFalse(self.sdk.onOrderAck(1, object()))

    def test_bounded_queue_overflow_is_visible_and_closes_gate(self):
        with TemporaryDirectory() as d:
            self.session(d, capacity=1); self.send(2)
            self.assertTrue(self.event('Acked', cumulative=0)[0])
            self.assertFalse(self.event()[0])
            self.assertIn('回调失败', self.sdk.backend.failure)
            with self.assertRaises(RuntimeError):
                self.sdk.poll()

    def test_delivery_gap_and_unknown_status_require_recovery(self):
        for mutation in ({'delivery_sequence': 2}, {'status': 'Expired'}):
            with TemporaryDirectory() as d:
                self.session(d); self.send()
                self.event('Acked', cumulative=0, mutation=mutation)
                with self.assertRaises(RuntimeError):
                    self.sdk.poll()
                self.assertFalse(self.sdk.backend.is_armed)

    def test_unsupported_native_attribute_or_hedge_flag_is_not_silently_downgraded(self):
        for mutation in ({'attribute': 2}, {'hedge_flag': 'Hedge'}):
            with TemporaryDirectory() as d:
                self.session(d); self.send()
                self.assertFalse(self.event('Acked', cumulative=0, mutation=mutation)[0])
                self.assertFalse(self.sdk.backend.is_armed)

    def test_new_trade_after_cancel_is_detected(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(2)
            self.event('Cancelled', cumulative=0); self.sdk.poll()
            self.event()
            with self.assertRaisesRegex(RuntimeError, '终态后'):
                self.sdk.poll()

    def test_binding_query_rejects_partial_wrong_ownership_and_unconfirmed_capability(self):
        for fields in ({'complete': False}, {'initialized': False}, {'generic_close': False},
                       {'acked_is_accepted': False}, {'owned_quantity': Decimal(1)}):
            with TemporaryDirectory() as d:
                self.session(d, start=False)
                self.host.snapshot = replace(self.host.snapshot, **fields)
                with self.assertRaises(RuntimeError):
                    self.sdk.start()
                self.assertFalse(self.sdk.backend.is_armed)
                self.assertFalse(self.host.sent)

    def test_recovery_preserves_ids_trade_dedup_positions_and_target_revision(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(); self.event(); self.sdk.poll()
            self.host.snapshot = replace(self.host.snapshot, owned_quantity=Decimal(1))
            self.sdk.begin_stop(); self.sdk.finish_stop()
            key = self.state().client_order_id
            seq = self.host.seq
            self.session(d, start=False)
            self.host.seq = seq
            self.host.snapshot = replace(self.host.snapshot, owned_quantity=Decimal(1))
            self.sdk.start(resume=True); self.sdk.authorize()
            self.assertEqual(self.state().client_order_id, key)
            self.assertEqual(self.strategy.position('main'), 1)
            self.assertEqual(self.strategy._revision, 1)
            self.send(2)
            self.assertEqual(self.host.sent[-1].client_order_id, 2)

    def test_stop_waits_for_real_cancel_while_host_keeps_forwarding_callbacks(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send()
            self.sdk.begin_stop()
            self.assertEqual(self.host.cancelled, [1])
            with self.assertRaisesRegex(RuntimeError, '活动单'):
                self.sdk.finish_stop()
            self.event('Cancelled', cumulative=0)
            self.sdk.finish_stop()
            self.assertFalse(self.sdk.backend.is_started)
            self.assertFalse(self.strategy.is_started)

    def test_foreign_strategy_cannot_cancel_binding_orders(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send()
            self.sdk.backend.cancel_strategy('other')
            self.assertFalse(self.host.cancelled)

    def test_host_disconnect_closes_gate_without_owning_host_lifecycle(self):
        with TemporaryDirectory() as d:
            self.session(d)
            self.sdk.on_disconnect('disconnected')
            self.assertFalse(self.sdk.backend.is_armed)
            with self.assertRaises(RuntimeError):
                self.send()

    def test_lifecycle_thread_is_owned_by_host_and_callbacks_can_arrive_elsewhere(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send()
            errors = []
            def worker():
                self.assertTrue(self.event()[0])
                try:
                    self.sdk.poll()
                except RuntimeError as error:
                    errors.append(str(error))
            thread = threading.Thread(target=worker)
            thread.start(); thread.join()
            self.assertEqual(len(errors), 1)
            self.sdk.poll()
            self.assertEqual(self.strategy.position('main'), 1)

    def test_binding_conflict_never_changes_strategy_position(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send()
            wrong = replace(self.binding, account_id='other-account')
            self.assertFalse(self.event('Acked', cumulative=0, mutation={'binding': wrong})[0])
            self.assertEqual(self.strategy.position('main'), 0)
            self.assertFalse(self.sdk.backend.is_armed)

    def test_blank_and_submited_are_not_acceptance(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send()
            self.event('Blank', cumulative=0); self.event('Submited', cumulative=0)
            self.sdk.poll()
            self.assertEqual(self.state().status.value, 'PENDING')
            self.assertFalse(self.sdk.backend.reports)

    def test_callback_after_stop_is_rejected_without_escaping_native_boundary(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(); self.event(); self.sdk.poll()
            self.host.snapshot = replace(self.host.snapshot, owned_quantity=Decimal(1))
            self.sdk.begin_stop(); self.sdk.finish_stop()
            self.assertFalse(self.event('FullFilled')[0])

    def test_rejected_state_save_failure_does_not_release_working_twice(self):
        with TemporaryDirectory() as d:
            self.session(d)
            self.host.send_order = lambda cmd: SubmitResult('rejected', 'validation')
            save = self.sdk.manager.repository.save
            calls = []
            def fail_second(*args, **kwargs):
                calls.append(1)
                if len(calls) == 2:
                    raise OSError('disk full after local reject')
                return save(*args, **kwargs)
            self.sdk.manager.repository.save = fail_second
            with self.assertRaises(self.contracts.AmbiguousOrderSubmission):
                self.send()
            self.assertEqual(self.sdk.runner.position_manager.working_quantity('lxgbn', self.instrument), 0)
            self.assertFalse(self.sdk.backend.is_armed)

    def test_reverse_target_closes_before_later_target_opens(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(); self.event(); self.sdk.poll()
            self.send(-1)
            self.assertEqual(len(self.host.sent), 2)
            self.assertEqual(self.host.sent[-1].position_effect, 'CLOSE')
            self.event(); self.sdk.poll()
            self.assertEqual(self.strategy.position('main'), 0)
            self.send(-1)
            self.assertEqual(self.host.sent[-1].position_effect, 'OPEN')
            self.event(); self.sdk.poll()
            self.assertEqual(self.strategy.position('main'), -1)

    def test_equivalent_decimal_formatting_is_not_a_conflicting_duplicate(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(2)
            self.event(quantity='1.0', price='3000.0')
            self.event(quantity='1', price='3000')
            self.event('Cancelled', cumulative=1)
            self.event('Cancelled', cumulative='1.0')
            self.sdk.poll()
            self.assertEqual(self.strategy.position('main'), 1)
            self.assertEqual(len(self.strategy.fills), 1)

    def test_reused_native_order_id_in_same_binding_faults(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(); self.event(); self.sdk.poll()
            self.send(2)
            self.event('Acked', cumulative=0, mutation={'native_order_id': 'native1'})
            with self.assertRaisesRegex(RuntimeError, '不同请求复用'):
                self.sdk.poll()
            self.assertFalse(self.sdk.backend.is_armed)

    def test_failed_reauthorization_cannot_leave_old_authorization_open(self):
        with TemporaryDirectory() as d:
            self.session(d)
            self.assertTrue(self.sdk.backend.is_armed)
            self.host.snapshot = replace(self.host.snapshot, owned_quantity=Decimal(1))
            with self.assertRaisesRegex(RuntimeError, '归属'):
                self.sdk.authorize()
            self.assertFalse(self.sdk.backend.is_armed)

    def test_double_digit_integer_ids_survive_json_checksum_and_load(self):
        with TemporaryDirectory() as d:
            self.session(d)
            for index in range(12):
                self.send(1 if index % 2 == 0 else 0)
                self.event(); self.sdk.poll()
            state = self.sdk.manager.repository.load()
            ids = state.payload['component_states']['execution_backend']['ids']
            self.assertEqual(len(ids), 12)
            self.assertIn('12', ids)
            self.assertEqual(self.strategy.position('main'), 0)

    def test_fill_callback_without_execution_object_is_not_misread_as_ack(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send()
            _, raw = self.event('Acked', cumulative=0)
            self.assertFalse(self.sdk.onOrderFill(1, raw, None))
            self.assertFalse(self.sdk.backend.is_armed)

    def test_tampered_checkpoint_fill_total_is_rejected(self):
        with TemporaryDirectory() as d:
            self.session(d); self.send(); self.event(); self.sdk.poll()
            checkpoint = self.sdk.backend.snapshot_state()
            next(iter(checkpoint['orders'].values()))['filled'] = '2'
            self.sdk.backend.is_started = False
            with self.assertRaisesRegex(ValueError, '成交/终态'):
                self.sdk.backend.restore_state(checkpoint)


if __name__ == '__main__':
    unittest.main()
