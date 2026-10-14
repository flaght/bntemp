"""同/不同进程日历调度的参数、唯一TD及正常停机顺序；不连接柜台。"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace as NS
import signal
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.integration.shared_account.continuous import ProcessWindowRuntime, SharedWindowRuntime, config_for_day
from bomber.framework.calendars.sessions import TradingWindow
from bomber.framework.trader.runtime.live.continuous import StopSignal


class SharedContinuousTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'environment': 'realtime', 'execution_mode': 'independent', 'trading_day': '20261013',
            'seconds': 600, 'report_dir': str(self.root), 'account_state_file': str(self.root/'account.json'),
            'account': {'max_session_orders': 20}, 'strategies': [
                {'strategy_id': sid, 'kind': 'option_vega', 'state_file': str(self.root/(sid+'.json')),
                 'argv': ['--option-slot', '13:36']} for sid in ('vega1', 'vega2')]}
        zone = ZoneInfo('Asia/Shanghai')
        self.window = TradingWindow('20261014', datetime(2026, 10, 14, 13, tzinfo=zone),
                                    datetime(2026, 10, 14, 15, tzinfo=zone), 'afternoon')

    def test_config_preserves_strategy_and_execution_mode_changes_only_runtime_day(self):
        before = deepcopy(self.config)
        new = config_for_day(self.config, '20261014')
        self.assertEqual(self.config, before)
        self.assertEqual(new['strategies'], before['strategies'])
        self.assertEqual(new['trading_day'], '20261014')
        for layout in ('netted', 'independent'):
            self.config['execution_mode'] = layout
            self.assertEqual(config_for_day(self.config, '20261014')['execution_mode'], layout)

    def test_unsupported_history_strategy_or_pinned_day_is_explicit(self):
        self.config['strategies'][0]['kind'] = 'main_ema'
        with self.assertRaisesRegex(ValueError, '历史预热'):
            config_for_day(self.config, '20261014')
        self.config['strategies'][0]['kind'] = 'option_vega'
        self.config['strategies'][0]['argv'] += ['--valuation-day=20261013']
        with self.assertRaisesRegex(ValueError, '固定'):
            config_for_day(self.config, '20261014')

    def processes(self, *, fail_worker=False):
        commands, stopped, processes = [], [], []
        now = [self.window.start]
        def popen(command, *, start_new_session):
            self.assertTrue(start_new_session)  # Ctrl+C只由父调度器安排worker先停，避免TD提前断开。
            commands.append(command)
            role = command[3].rsplit('.', 1)[-1]
            proc = NS(returncode=None)
            proc.poll = lambda: proc.returncode
            def terminate():
                stopped.append(role)
                proc.returncode = 2 if fail_worker and role == 'worker' else 0
            proc.terminate = terminate
            processes.append(proc)
            if role == 'server':
                credentials = Path(command[command.index('--credentials-dir')+1]); credentials.mkdir()
                for row in self.config['strategies']:
                    (credentials/(row['strategy_id']+'.json')).write_text('{}')
                self.assertLess(len(command[command.index('--socket')+1].encode()), 104)
            if len(commands) == 3:
                now[0] = self.window.end
            return proc
        runtime = ProcessWindowRuntime(self.config, self.window, Event(), 17, popen=popen, clock=lambda: now[0])
        return runtime, commands, stopped, processes

    def test_separate_workers_stop_before_single_account_and_carry_remaining_budget(self):
        runtime, commands, stopped, _ = self.processes()
        self.assertEqual(runtime.run()['status'], 'completed')
        self.assertEqual([cmd[3].rsplit('.', 1)[-1] for cmd in commands], ['server', 'worker', 'worker'])
        self.assertEqual(stopped, ['worker', 'worker', 'server'])
        self.assertEqual(commands[0][commands[0].index('--remaining-orders')+1], '17')
        for command in commands[1:]:
            self.assertNotIn('--remaining-orders', command)
            self.assertIn('--resume', command)

    def test_worker_shutdown_failure_keeps_account_service_for_diagnosis(self):
        runtime, _, stopped, processes = self.processes(fail_worker=True)
        with self.assertRaisesRegex(RuntimeError, '停机失败'):
            runtime.run()
        self.assertEqual(stopped, ['worker', 'worker'])
        self.assertIsNone(processes[0].returncode)

    def test_separate_process_mode_rejects_netted_without_launch(self):
        runtime, commands, _, _ = self.processes()
        self.config['execution_mode'] = 'netted'
        with self.assertRaisesRegex(ValueError, 'independent'):
            runtime.run()
        self.assertEqual(commands, [])

    def test_same_process_uses_one_shared_host_for_both_strategies(self):
        saved = NS(payload={'orders': {'account': [{'client_order_id': 'CTP-9999-user-20261014-1'}]}})
        adapter = SharedWindowRuntime(self.config, NS(provenance={'fixture': True}), self.window, Event(), 'same-process')
        with patch('bomber.framework.trader.persistence.JsonStateStore') as store, \
                patch('scripts.integration.options.shared.observe_day', return_value='20261014'), \
                patch('scripts.integration.options.shared.run_session', return_value={'status': 'completed'}) as run:
            store.return_value.load.return_value = saved
            adapter.run()
            self.assertEqual(len(run.call_args.args[0]['strategies']), 2)
            self.assertEqual(run.call_args.kwargs['session_order_budget'], 19)
            run.assert_called_once()

    def test_td_mismatch_or_exhausted_budget_never_starts_shared_host(self):
        adapter = SharedWindowRuntime(self.config, NS(provenance={}), self.window, Event(), 'same-process')
        saved = NS(payload={'orders': {}})
        with patch('bomber.framework.trader.persistence.JsonStateStore') as store, \
                patch('scripts.integration.options.shared.observe_day', return_value='20261013'), \
                patch('scripts.integration.options.shared.run_session') as run:
            store.return_value.load.return_value = saved
            with self.assertRaisesRegex(RuntimeError, 'TD'):
                adapter.run()
            run.assert_not_called()
        saved.payload = {'orders': {'account': [{'client_order_id': f'CTP-9999-user-20261014-{n}'} for n in range(20)]}}
        with patch('bomber.framework.trader.persistence.JsonStateStore') as store, \
                patch('scripts.integration.options.shared.observe_day', return_value='20261014'), \
                patch('scripts.integration.options.shared.run_session') as run:
            store.return_value.load.return_value = saved
            with self.assertRaisesRegex(RuntimeError, '额度'):
                adapter.run()
            run.assert_not_called()

    def test_operator_stop_before_process_spawn_does_not_connect(self):
        runtime, commands, _, _ = self.processes()
        runtime.stop.set()
        self.assertEqual(runtime.run()['status'], 'completed')
        self.assertEqual(commands, [])

    def test_stop_signal_sets_event_and_restores_handlers(self):
        before = signal.getsignal(signal.SIGTERM)
        with StopSignal() as event:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            self.assertTrue(event.is_set())
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)


if __name__ == '__main__':
    unittest.main()
