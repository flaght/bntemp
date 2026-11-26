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

    def setup_strategy(self, *, max_attempts=3, execution_trading_day=None,
                       strategy_id='one', strategy_config=None):
        self.instrument = type(self.instrument).from_str('MO2612-C-6000.CFFEX')
        future = type(self.instrument).from_str('IM2612.CFFEX')
        original = self.native_account
        def native_account(path, modules, **kwargs):
            from bomber.framework.trader.execution.shared import SharedAccountExecutionService
            register = SharedAccountExecutionService.strategy_client
            def strategy_client(service, sid, **options):
                return register(service, strategy_id if sid == 'one' else sid, **options)
            with patch.object(SharedAccountExecutionService, 'strategy_client', new=strategy_client):
                return original(path, modules, extra_instruments=(future,), **kwargs)
        with patch.object(self, 'native_account', side_effect=lambda path, modules, **kwargs:
                native_account(path, modules, **kwargs)):
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
            strategy_id, strategy_config or signal.OptionVegaConfig(),
            {'MO2612-C-6000': signal.Contract('MO2612-C-6000', 'IM2612', 6000, D(100),
                date(2026, 1, 1), date(2026, 12, 18), date(2026, 12, 18), D('.2'))},
            {'IM2612': {'contMultNum': 200}},
            {'MO2612-C-6000': self.instrument, 'IM2612': future}, '000852', (),
            execution_trading_day=execution_trading_day)
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
            position=lambda key: service.position_manager.position(strategy_id, key),
            strategy_working_quantity=lambda key: service.position_manager.attribution.working_quantity(
                port.client_id, port.routes[key], strategy_id, key)))
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
        _, _, broker, port, _, strategy, _, _ = self.setup_strategy(execution_trading_day='20261014')
        broker.cancel_order(broker.sent[-1]['OrderRef'])
        strategy.option_goals['MO2612-C-6000'] = D(0)
        with self.assertRaisesRegex(RuntimeError, '订单CANCELED'):
            port.drain_events()

    def test_attempt_limit_finishes_point_without_stopping_or_clearing_sent_day(self):
        account, service, _, port, runtime, strategy, _, _ = self.setup_strategy(
            max_attempts=1, execution_trading_day='20261013')
        strategy.sent_days.add(date(2026, 10, 13))
        self.tick(service, runtime)
        port.drain_events()
        self.assertEqual(strategy.option_goals['MO2612-C-6000'], 0)
        self.assertEqual(strategy.sent_days, {date(2026, 10, 13)})
        self.assertFalse(service._failed)
        self.assertNotIn('one', service._suspended)
        self.assertTrue(service.client.is_armed)
        self.assertEqual(account.driver.order_budget['submitted'], 1)

    def test_previous_day_attempt_limit_consumes_one_event_and_preserves_partial_fills(self):
        self.check_historical_cancel_preserves_partial_fills('attempt_limit')

    def test_previous_day_strategy_cancel_preserves_partial_fills_without_resending(self):
        self.check_historical_cancel_preserves_partial_fills('strategy_cancel')

    def check_historical_cancel_preserves_partial_fills(self, reason):
        account, service, broker, port, runtime, strategy, future, _ = self.setup_strategy(
            max_attempts=1, execution_trading_day='20261014')
        broker.fill(broker.sent[-1]['OrderRef'], 4)
        port.drain_events()  # 已入账的4手期权正常产生真实期货对冲。
        broker.fill(broker.sent[-1]['OrderRef'])
        port.drain_events()
        if reason == 'attempt_limit':
            self.tick(service, runtime)
        else:
            service.suspend_strategy('one')
            service.poll_stopping()
        # 与首个恢复帧一致：以真实归属持仓接续，不继续旧的8手目标。
        strategy.option_goals['MO2612-C-6000'] = D(-4)
        strategy.future_goals['IM2612'] = D(1)
        before_positions = service.position_manager.state()
        before_budget = dict(account.driver.order_budget)
        before_cursor = port._journal_cursor
        before_ack = service.event_journal.ack['one']
        before_submissions = strategy.submissions
        with self.assertLogs('OptionExecutionRecovery', level='WARNING') as captured:
            self.assertEqual(port.drain_events(), 1)
        self.assertIn('reason=' + reason, captured.output[0])
        self.assertEqual(port._journal_cursor, before_cursor + 1)
        self.assertEqual(service.event_journal.ack['one'], before_ack)  # 仍由信号保存后确认。
        self.assertEqual(service.position_manager.state(), before_positions)
        self.assertEqual(account.driver.order_budget, before_budget)
        self.assertEqual(strategy.submissions, before_submissions)
        self.assertEqual(strategy.fills_received, 2)
        self.assertEqual(service.position_manager.position('one', str(self.instrument)), -4)
        self.assertEqual(service.position_manager.position('one', str(future)), 1)
        self.assertEqual(len(broker.sent), 2)
        self.assertFalse(runtime.snapshot_state()['tasks'])
        self.assertFalse(service._failed)
        audit = strategy.signals[-1]
        self.assertEqual(audit['action'], 'EXECUTION_ATTEMPTS_EXHAUSTED'
            if reason == 'attempt_limit' else 'HISTORICAL_ORDER_CANCELED')
        self.assertEqual(audit['reason'], reason)
        self.assertEqual(audit['position'], '-4')
        account._validate_account()

    def test_exhausted_option_does_not_cancel_another_active_task(self):
        account, service, broker, port, runtime, strategy, future, events = self.setup_strategy(
            max_attempts=1, execution_trading_day='20261014')
        strategy.future_goals['IM2612'] = D(1)
        strategy._submit({str(self.instrument): D(-8), str(future): D(1)}, self.now, self.now, 'HEDGE')
        port.drain_events()
        option_task = next(key for key, task in runtime._tasks.items()
            if task['order']['instrument_id'] == str(self.instrument))
        self.now += 10_000_000_000
        service.prices.update(self.instrument, 3000, self.now)
        service.prices.update(future, 3000, self.now)
        # 期权耗尽仅结束它自己，期货任务继续执行。
        runtime._host.alarms.pop(option_task)[1]()
        service.poll()
        before_cursor = port._journal_cursor
        before_ack = service.event_journal.ack['one']
        before_budget = dict(account.driver.order_budget)
        with self.assertLogs('OptionExecutionRecovery', level='WARNING') as captured:
            self.assertEqual(port.drain_events(), 1)
        self.assertEqual(len(captured.output), 1)
        canceled = [event for event in events if getattr(getattr(event, 'status', None), 'value', None) == 'CANCELED']
        self.assertEqual([event.metadata['nt_algo_cancel_reason'] for event in canceled],
                         ['attempt_limit'])
        self.assertEqual(strategy.option_goals['MO2612-C-6000'], 0)
        self.assertEqual(strategy.future_goals['IM2612'], 1)
        self.assertEqual(port._journal_cursor, before_cursor + 1)
        self.assertEqual(service.event_journal.ack['one'], before_ack)
        self.assertEqual(account.driver.order_budget, before_budget)
        self.assertEqual(len(broker.sent), 2)
        self.assertEqual(len(broker.active), 1)
        broker.fill(broker.sent[-1]['OrderRef'])
        port.drain_events()
        self.assertFalse(runtime.snapshot_state()['tasks'])
        self.assertFalse(service._failed)
        account._validate_account()

    def test_same_day_strategy_cancel_is_not_treated_as_historical(self):
        _, service, _, port, _, strategy, _, _ = self.setup_strategy(execution_trading_day='20261013')
        service.suspend_strategy('one')
        strategy.option_goals['MO2612-C-6000'] = D(0)
        with self.assertRaisesRegex(RuntimeError, 'algo_cancel_reason=strategy_cancel'):
            port.drain_events()
        self.assertTrue(service._failed)

    def test_previous_day_strategy_cancel_with_unfinished_goal_still_fails(self):
        _, service, _, port, _, _, _, _ = self.setup_strategy(execution_trading_day='20261014')
        service.suspend_strategy('one')
        with self.assertRaisesRegex(RuntimeError, 'algo_cancel_reason=strategy_cancel'):
            port.drain_events()
        self.assertTrue(service._failed)

    def test_attempt_limit_rebases_unfinished_goal_without_claiming_a_fill(self):
        _, service, _, port, runtime, strategy, _, _ = self.setup_strategy(
            max_attempts=1, execution_trading_day='20261014')
        self.tick(service, runtime)
        port.drain_events()
        self.assertEqual(strategy.option_goals['MO2612-C-6000'], 0)
        self.assertEqual(strategy.fills_received, 0)
        self.assertFalse(service._failed)

    def test_previous_session_stop_cancel_with_working_quantity_still_fails(self):
        _, service, _, port, _, strategy, _, _ = self.setup_strategy(
            max_attempts=1, execution_trading_day='20261014')
        service.suspend_strategy('one')
        strategy.option_goals['MO2612-C-6000'] = D(0)
        with patch.object(strategy, 'strategy_working_quantity', return_value=D(1)), \
                self.assertRaisesRegex(RuntimeError, 'algo_cancel_reason=strategy_cancel'):
            port.drain_events()
        self.assertTrue(service._failed)

    def test_same_day_previous_session_stop_cancel_can_be_consumed_after_restore(self):
        _, service, _, port, _, strategy, _, _ = self.setup_strategy(execution_trading_day='20261013')
        service.suspend_strategy('one')
        strategy.option_goals['MO2612-C-6000'] = D(0)
        strategy.execution_started_ns = self.now + 1
        port.drain_events()
        self.assertFalse(service._failed)
        self.assertEqual(strategy.signals[-1]['action'], 'HISTORICAL_ORDER_CANCELED')

    def test_queued_fill_before_exhaustion_callback_hedges_without_reopening_failed_option(self):
        account, service, broker, port, runtime, strategy, future, _ = self.setup_strategy(max_attempts=1)
        broker.fill(broker.sent[-1]['OrderRef'], 4)
        self.tick(service, runtime)  # 先入账成交和耗尽，尚未消费业务回调。
        port.drain_events()
        self.assertEqual([row['InstrumentID'] for row in broker.sent], ['MO2612-C-6000', 'IM2612'])
        self.assertEqual(strategy.option_goals['MO2612-C-6000'], -4)
        self.assertEqual(strategy.future_goals['IM2612'], 1)
        self.assertNotIn('one', service._suspended)
        self.assertFalse(service._failed)
        broker.fill(broker.sent[-1]['OrderRef'])
        port.drain_events()
        self.assertEqual(service.position_manager.position('one', str(self.instrument)), -4)
        self.assertEqual(service.position_manager.position('one', str(future)), 1)
        service.continue_targets('one', self.instrument, self.now)
        self.assertEqual(len(broker.sent), 2)
        account._validate_account()

    def test_future_attempt_limit_keeps_partial_fill_and_consumes_hedge_point(self):
        account, service, broker, port, runtime, strategy, future, _ = self.setup_strategy(max_attempts=1)
        broker.fill(broker.sent[-1]['OrderRef'])
        port.drain_events()  # 期权-8已成交，实际生成期货+2的对冲任务。
        broker.fill(broker.sent[-1]['OrderRef'], 1)
        port.drain_events()
        token = '2026-10-13_10:25'
        strategy.hedge_sent.add(token)
        strategy._pending_scheduled_hedges['IM2612'] = [(token, self.now)]
        self.tick(service, runtime)
        port.drain_events()
        self.assertEqual(strategy.future_goals['IM2612'], 1)
        self.assertFalse(strategy._fill_hedge_targets)
        self.assertFalse(strategy._pending_scheduled_hedges)
        self.assertIn(token, strategy.hedge_sent)
        strategy._drain_fill_hedges(self.now)
        strategy._retry_scheduled_hedge('IM2612', self.now)
        service.continue_targets('one', future, self.now)
        self.assertEqual(len(broker.sent), 2)
        self.assertFalse(service._failed)
        self.assertTrue(service.client.is_armed)
        # 后续真实信号仍能增加到+2，只补剩余的1手。
        self.now += 1
        strategy.future_goals['IM2612'] = D(2)
        strategy._submit({str(self.instrument): D(-8), str(future): D(2)},
                         self.now, self.now, 'SCHEDULED_HEDGE')
        self.assertEqual(broker.sent[-1]['VolumeTotalOriginal'], 1)
        broker.fill(broker.sent[-1]['OrderRef'])
        port.drain_events()
        self.assertEqual(service.position_manager.position('one', str(future)), 2)
        account._validate_account()

    def test_late_exhaustion_callback_does_not_overwrite_a_new_signal(self):
        _, service, broker, port, runtime, strategy, future, _ = self.setup_strategy(max_attempts=1)
        self.tick(service, runtime)
        self.now += 1
        strategy.option_goals['MO2612-C-6000'] = D(-6)
        strategy._submit({str(self.instrument): D(-6), str(future): D(0)},
                         self.now, self.now, 'ENTRY')
        port.drain_events()  # 此时才消费上一任务的耗尽通知。
        self.assertEqual(strategy.option_goals['MO2612-C-6000'], -6)
        self.assertTrue(strategy.signals[-1]['newer_signal'])
        self.assertEqual(len(broker.sent), 2)
        self.assertEqual(len(broker.canceled), 1)
        self.assertEqual(len(runtime.snapshot_state()['tasks']), 1)
        self.assertFalse(service._failed)

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
        strategy.execution_trading_day = date(2026, 10, 14)
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
