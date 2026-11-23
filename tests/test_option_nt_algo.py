"""08真实策略与算法执行闭环；仅SDK时钟、Greeks和柜台传输使用替身。"""
from datetime import date
from dataclasses import replace
from decimal import Decimal as D
from importlib import import_module
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest

from tests import test_direct_ctp  # 初始化无原生SDK的测试环境。
from tests import test_nt_algo_ctp as fixture


class OptionNTAlgoTests(unittest.TestCase):
    setUp = fixture.NTAlgoCtpTests.setUp
    native_modules = fixture.NTAlgoCtpTests.native_modules
    native_account = fixture.NTAlgoCtpTests.native_account
    make_account = fixture.NTAlgoCtpTests.make_account
    tick = fixture.NTAlgoCtpTests.tick

    def setup_strategy(self, *, max_attempts=3):
        self.instrument = type(self.instrument).from_str('MO2612-C-6000.CFFEX')
        future = type(self.instrument).from_str('IM2612.CFFEX')
        original = self.native_account
        with patch.object(self, 'native_account', side_effect=lambda path, modules, **kwargs:
                original(path, modules, extra_instruments=(future,), **kwargs)):
            account, service, broker, (port, other), runtime = self.make_account(
                start=False, max_attempts=max_attempts)
        port.routes = {str(self.instrument): self.instrument, str(future): future}
        port.instrument_limits[future] = service.instrument_limits[future]
        port.budget = import_module('bomber.framework.trader.execution.shared').SharedRiskBudget(10, 50000)
        accounting = next(handler.__self__ for handler in service.client.backend._report_handlers
                          if hasattr(getattr(handler, '__self__', None), 'multipliers'))
        accounting.multipliers[future] = D(1)  # 柜台夹具默认只注册第一份合约的记账乘数。
        service.prices.update(future, 3000, self.now)
        signal = import_module('demos.08_option_vega.vega_signal')
        strategy = import_module('demos.08_option_vega.strategy').OptionVegaStrategy(
            'one', signal.OptionVegaConfig(),
            {'MO2612-C-6000': signal.Contract('MO2612-C-6000', 'IM2612', 6000, D(100),
                date(2026, 1, 1), date(2026, 12, 18), date(2026, 12, 18), D('.2'))},
            {'IM2612': {'contMultNum': 200}},
            {'MO2612-C-6000': self.instrument, 'IM2612': future}, '000852', ())
        self.enterContext(patch.object(strategy, '_greeks', return_value=NS(delta=.5)))

        def submit(intent):
            rows = tuple(dict(strategy_id=port.strategy_id, target_key=key,
                instrument_id=str(instrument), quantity=str(intent.targets.get(key, 0)))
                for key, instrument in port.routes.items())
            port.submit_targets(self.request_type(port.strategy_id, intent.revision,
                port.client_id, intent.ts_event,
                {instrument: intent.targets.get(key, D(0)) for key, instrument in port.routes.items()},
                'DIRECT', metadata={'position_attribution': rows}))

        strategy._bind(NS(submit=submit,
            position=lambda key: service.position_manager.position('one', key),
            strategy_working_quantity=lambda key: service.position_manager.attribution.working_quantity(
                port.client_id, port.routes[key], 'one', key)))
        events = []
        def receive(event):
            events.append(event)
            if hasattr(event, 'status'):
                strategy.on_order_update(event)
            else:
                strategy.on_fill(event)
        port.register_execution_event_handler(receive)
        port._consume_journal = True
        other.register_execution_event_handler(lambda event: None)
        account.start(resume=False)
        service.start()
        port.start()
        other.start()
        account.authorize()
        strategy.option_goals['MO2612-C-6000'] = D(-8)
        strategy._submit({str(self.instrument): D(-8), str(future): D(0)},
                         self.now, self.now, 'ENTRY')
        return account, service, broker, port, runtime, strategy, future, events

    def test_partial_option_fill_hedges_without_restarting_option_task_then_retries_remainder(self):
        account, service, broker, port, runtime, strategy, future, events = self.setup_strategy()
        task_id = next(iter(runtime._tasks))
        option_ref = broker.sent[-1]['OrderRef']
        broker.fill(option_ref, 4)
        port.drain_events()  # 实际期权成交推动期货目标；仍提交完整组合。
        self.assertIn(task_id, runtime._tasks)
        self.assertNotIn(option_ref, broker.canceled)
        self.assertEqual(runtime._tasks[task_id]['attempts'], 1)
        self.assertEqual(broker.sent[-1]['InstrumentID'], 'IM2612')
        broker.fill(broker.sent[-1]['OrderRef'])
        port.drain_events()
        self.tick(service, runtime)
        port.drain_events()  # 内部追价撤单应由08正常接纳。
        self.assertFalse(service._failed)
        service.poll()
        retry = broker.sent[-1]
        self.assertEqual(retry['InstrumentID'], 'MO2612-C-6000')
        self.assertEqual(retry['VolumeTotalOriginal'], 4)
        self.assertEqual(runtime._tasks[task_id]['attempts'], 2)
        broker.fill(retry['OrderRef'])
        port.drain_events()
        self.assertEqual(broker.sent[-1]['InstrumentID'], 'IM2612')
        self.assertEqual(broker.sent[-1]['VolumeTotalOriginal'], 1)
        broker.fill(broker.sent[-1]['OrderRef'])
        port.drain_events()
        self.assertEqual(service.position_manager.position('one', str(self.instrument)), -8)
        self.assertEqual(service.position_manager.position('one', str(future)), 2)
        self.assertEqual(service.position_manager.position('two', 'main'), 0)
        self.assertFalse(runtime.snapshot_state()['tasks'])
        self.assertEqual(account.driver.order_budget['submitted'], 4)
        self.assertEqual(strategy.fills_received, 4)
        reasons = [event.metadata.get('nt_algo_cancel_reason') for event in events
                   if getattr(getattr(event, 'status', None), 'value', None) == 'CANCELED']
        self.assertEqual(reasons, ['retry'])

    def test_changed_target_cancels_only_affected_task_and_keeps_other_owner(self):
        account, service, broker, port, runtime, strategy, future, events = self.setup_strategy()
        # 另一个来源的期权任务不能被08目标更新撤销。
        other = service._ports['two']
        other.routes = {'main': future}
        other.instrument_limits[future] = service.instrument_limits[future]
        other.submit_targets(self.request_type('two', 1, other.client_id, self.now, {future: D(1)},
            'DIRECT', metadata={'position_attribution': (dict(strategy_id='two', target_key='main',
                instrument_id=str(future), quantity='1'),)}))
        other_task = next(key for key, row in runtime._tasks.items() if row['order']['strategy_id'] == 'two')
        option_ref = broker.sent[0]['OrderRef']
        strategy.option_goals['MO2612-C-6000'] = D(-4)
        strategy._submit({str(self.instrument): D(-4), str(future): D(0)}, self.now, self.now, 'ADJUST')
        port.drain_events()
        self.assertEqual(broker.canceled, [option_ref])
        self.assertIn(other_task, runtime._tasks)
        self.assertFalse(service._failed)
        reasons = [event.metadata.get('nt_algo_cancel_reason') for event in events
                   if getattr(getattr(event, 'status', None), 'value', None) == 'CANCELED']
        self.assertEqual(reasons, ['target_update'])

    def test_external_cancel_still_fails_08(self):
        _, _, broker, port, _, _, _, _ = self.setup_strategy()
        broker.cancel_order(broker.sent[-1]['OrderRef'])
        with self.assertRaisesRegex(RuntimeError, '订单CANCELED'):
            port.drain_events()

    def test_attempt_limit_still_fails_08_and_counts_actual_orders(self):
        account, service, _, port, runtime, _, _, _ = self.setup_strategy(max_attempts=1)
        self.tick(service, runtime)
        with self.assertRaisesRegex(RuntimeError, '订单CANCELED'):
            port.drain_events()
        self.assertEqual(account.driver.order_budget['submitted'], 1)

    def test_new_target_cancels_pending_retry_without_resending_old_goal(self):
        account, service, broker, port, runtime, strategy, future, _ = self.setup_strategy()
        self.tick(service, runtime)
        port.drain_events()
        self.assertTrue(runtime.snapshot_state()['tasks'])
        self.assertFalse(broker.active)
        strategy.option_goals['MO2612-C-6000'] = D(0)
        strategy._submit({str(self.instrument): D(0), str(future): D(0)}, self.now, self.now, 'CANCEL')
        service.poll()
        self.assertFalse(runtime.snapshot_state()['tasks'])
        self.assertEqual(len(broker.sent), 1)
        self.assertEqual(account.driver.order_budget['submitted'], 1)
        self.assertFalse(service._failed)

    def test_plain_cancel_unknown_algo_cancel_rejection_and_expiry_still_fail(self):
        _, _, _, port, _, strategy, _, events = self.setup_strategy()
        port.drain_events()
        accepted = events[0]
        cases = (
            ('CANCELED', {}),
            ('CANCELED', {'nt_algo_task_id': 'unknown'}),
            ('CANCELED', {'nt_algo_cancel_reason': 'retry'}),
            ('REJECTED', {'nt_algo_task_id': 'known', 'nt_algo_cancel_reason': 'retry'}),
            ('EXPIRED', {'nt_algo_task_id': 'known', 'nt_algo_cancel_reason': 'retry'}),
        )
        for status, metadata in cases:
            with self.subTest(status=status, metadata=metadata), self.assertRaisesRegex(RuntimeError, '订单'+status):
                strategy.on_order_update(replace(accepted, status=status, metadata=metadata, reason='test'))

    def test_original_task_checkpoint_restores_without_changing_positions_or_daily_count(self):
        account, service, broker, port, runtime, _, _, _ = self.setup_strategy()
        broker.fill(broker.sent[-1]['OrderRef'], 4)
        # 模拟升级前的任务格式：尚无可选stop_reason字段。
        state = runtime.snapshot_state()
        for task in state['tasks'].values():
            task.pop('cancel_sent', None)
        self.assertTrue(all('stop_reason' not in task for task in state['tasks'].values()))
        before_positions = service.position_manager.state()
        before_budget = dict(account.driver.order_budget)
        runtime.restore_state(state)
        self.assertEqual(runtime.snapshot_state(), state)
        self.assertEqual(service.position_manager.state(), before_positions)
        self.assertEqual(account.driver.order_budget, before_budget)
        self.assertEqual(len(broker.sent), 1)
        port.drain_events()  # 旧成交仍可按归属正常补对冲。
        self.assertFalse(service._failed)

    def test_cancel_reason_cannot_be_inherited_from_unverified_metadata(self):
        _, service, _, _, runtime, _, _, _ = self.setup_strategy()
        accepted = service.client.backend._reports[0]
        report = replace(accepted, report_type='CANCELED', metadata={
            **accepted.metadata, 'nt_algo_cancel_reason': 'retry'})
        classified = runtime.annotate_report(report)
        self.assertEqual(classified.metadata['nt_algo_cancel_reason'], 'external_cancel')
        unknown = runtime.annotate_report(replace(report, client_order_id='unknown'))
        self.assertNotIn('nt_algo_cancel_reason', unknown.metadata)

    def test_external_cancel_after_timer_before_cancel_send_is_not_an_internal_retry(self):
        _, service, broker, port, runtime, _, _, events = self.setup_strategy()
        self.now += 10_000_000_000
        runtime._host.advance(self.now)  # 定时器只排队，尚未实际向柜台撤单。
        broker.cancel_order(broker.sent[-1]['OrderRef'])
        with self.assertRaisesRegex(RuntimeError, '订单CANCELED'):
            port.drain_events()
        self.assertEqual(events[-1].metadata['nt_algo_cancel_reason'], 'external_cancel')
        self.assertTrue(service._failed)

    def test_stopping_still_cancels_all_owned_algorithm_legs(self):
        account, service, broker, port, runtime, strategy, future, events = self.setup_strategy()
        strategy.future_goals['IM2612'] = D(1)
        strategy._submit({str(self.instrument): D(-8), str(future): D(1)}, self.now, self.now, 'HEDGE')
        refs = {row['OrderRef'] for row in broker.sent}
        self.assertEqual(len(refs), 2)
        strategy.stopping = True
        service.suspend_strategy('one')
        port.drain_events()
        self.assertEqual(set(broker.canceled), refs)
        self.assertFalse(runtime.snapshot_state()['tasks'])
        self.assertFalse(service._failed)
        self.assertEqual(account.driver.order_budget['submitted'], 2)
        self.assertTrue(all(event.metadata['nt_algo_cancel_reason'] == 'strategy_cancel'
            for event in events if getattr(getattr(event, 'status', None), 'value', None) == 'CANCELED'))


if __name__ == '__main__':
    unittest.main()
