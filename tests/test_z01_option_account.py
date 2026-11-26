"""08和z01统一启动配置，保留独立账号/状态及旧命令兼容；全程离线。"""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date
from decimal import Decimal
from importlib import import_module
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from tests import test_direct_ctp as fixture
from tests import test_option_nt_algo as option_fixture


class Z01OptionAccountTests(unittest.TestCase):
    setUp = fixture.DirectCtpTests.setUp
    native_modules = fixture.DirectCtpTests.native_modules

    def check(self, argv=None):
        app = import_module('demos.direct_ctp')
        wrapper = import_module('demos.z01_08_option_vega.run_direct')
        output = StringIO()
        with patch.object(app, 'build_direct_host', side_effect=AssertionError('离线禁止连接')), \
                redirect_stdout(output):
            wrapper.main(argv or [])
        return json.loads(output.getvalue())

    def test_wrapper_uses_z01_defaults_and_never_creates_state_or_loads_env(self):
        app = import_module('demos.direct_ctp')
        account = app.PROJECT/'demos/z01_shared_account'
        demo = app.PROJECT/'demos/z01_08_option_vega'
        before = {str(path) for root in (account, demo) for path in root.rglob('results/*')}
        with patch('scripts.integration.shared_account.config.load_environment',
                   side_effect=AssertionError('离线不能加载凭据')):
            result = self.check()
        self.assertEqual(result['strategies'], ['option-vega-z01'])
        self.assertEqual(result['state_file'], str(demo/'results/direct/state.json'))
        self.assertEqual(result['report_dir'], str(demo/'results/direct'))
        self.assertEqual(result['env_file'], str(demo/'config/.env'))
        self.assertEqual(result['counter_file'], str(account/'results/daily_orders.sqlite3'))
        self.assertEqual(result['execution_algorithm'], 'limit_reprice')
        self.assertFalse(result['orders_enabled'])
        self.assertEqual({str(path) for root in (account, demo) for path in root.rglob('results/*')}, before)

    def test_wrapper_defaults_to_own_strategy_yaml_instead_of_account_startup(self):
        from bomber.framework import cli
        app = import_module('demos.direct_ctp')
        with patch.object(cli, 'load_config', wraps=cli.load_config) as loader:
            result = self.check()
        loader.assert_called_once()
        self.assertEqual(Path(loader.call_args.args[0]),
            app.PROJECT/'demos/z01_08_option_vega/config/strategy.yaml')
        self.assertEqual(loader.call_args.args[1], 'direct_nt_algo')
        self.assertEqual(result['strategies'], ['option-vega-z01'])

    def test_matching_profiles_preserve_each_accounts_identity_paths_and_algorithm(self):
        from bomber.framework.cli import load_config
        app = import_module('demos.direct_ctp')
        profiles = []
        for folder, sid, account_folder, state_subdir in (
                ('08_option_vega', 'option-vega-direct', 'shared_account', 'v4/'),
                ('z01_08_option_vega', 'option-vega-z01', 'z01_shared_account', '')):
            with self.subTest(folder=folder):
                demo = app.PROJECT/'demos'/folder
                config_file = demo/'config/strategy.yaml'
                profile = load_config(config_file, 'direct_nt_algo')
                profiles.append(profile)
                self.assertEqual(profile['strategy_id'], sid)
                self.assertEqual(profile['live_config_file'], f'demos/{folder}/config/strategy.yaml')
                self.assertEqual(profile['live_config_id'], 'live_simnow')
                self.assertEqual(profile['account_config'], f'demos/{account_folder}/account.json')
                self.assertEqual(profile['account_state_file'],
                    f'demos/{folder}/results/direct/{state_subdir}state.json')
                self.assertEqual(profile['state_file'],
                    f'demos/{folder}/results/direct/{state_subdir}signal.json')
                self.assertEqual(profile['counter_file'], f'demos/{account_folder}/results/daily_orders.sqlite3')
                self.assertTrue(profile['run_forever'])
                self.assertTrue(all(profile[key] is False
                    for key in ('connect', 'enable_orders', 'confirm_simnow')))
                output = StringIO()
                with patch.object(app, 'build_direct_host', side_effect=AssertionError('离线禁止连接')), \
                        patch('scripts.integration.shared_account.config.load_environment',
                              side_effect=AssertionError('离线不能加载凭据')), redirect_stdout(output):
                    app.main('option_vega', ['--config-file', str(config_file),
                                             '--config-id', 'direct_nt_algo'])
                result = json.loads(output.getvalue())
                self.assertEqual(result['strategies'], [sid])
                for key in ('state_file', 'report_dir', 'counter_file', 'env_file'):
                    source_key = 'account_state_file' if key == 'state_file' else key
                    self.assertEqual(result[key], str((app.PROJECT/profile[source_key]).resolve()))
                self.assertFalse(result['orders_enabled'])
                self.assertEqual(result['nt_algo'], dict(wait_seconds=10.0, max_attempts=3,
                    initial_offset_ticks=0, retry_step_ticks=1))
        self.assertEqual(set(profiles[0]), set(profiles[1]))
        for key in ('execution_driver', 'execution_algorithm', 'algo_wait_seconds',
                    'algo_max_attempts', 'algo_initial_offset_ticks', 'algo_retry_step_ticks'):
            self.assertEqual(profiles[0][key], profiles[1][key], key)

    def test_offline_cli_overrides_preserve_existing_checkpoints_and_counter_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            account_state, signal_state, counter = (root/name
                for name in ('state.json', 'signal.json', 'daily_orders.sqlite3'))
            account_state.write_text(json.dumps({'generation': 23, 'payload': {'positions': {'quantity': '-2'}}}))
            signal_state.write_text(json.dumps({'generation': 17, 'payload': {'revision': 9, 'cursor': 15}}))
            counter.write_bytes(b'unchanged daily counter')
            before = {path: path.read_bytes() for path in (account_state, signal_state, counter)}
            result = self.check(['--account-state-file', str(account_state),
                '--state-file', str(signal_state), '--counter-file', str(counter),
                '--algo-retry-step-ticks', '0'])
            self.assertEqual(result['state_file'], str(account_state.resolve()))
            self.assertEqual(result['counter_file'], str(counter.resolve()))
            self.assertEqual(result['nt_algo']['retry_step_ticks'], 0)
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            self.assertEqual(set(root.iterdir()), set(before))

    def test_cli_overrides_profile_and_explicit_alternative_profile_is_supported(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.check(['--report-dir', str(root/'report'),
                                 '--account-state-file', str(root/'state.json')])
            self.assertEqual(result['report_dir'], str((root/'report').resolve()))
            self.assertEqual(result['state_file'], str((root/'state.json').resolve()))
            self.assertFalse(tuple(root.iterdir()))
        app = import_module('demos.direct_ctp')
        for config_flag, id_flag in (('--config-file', '--config-id'), ('--config_file', '--config_id')):
            with self.subTest(config_flag=config_flag):
                result = self.check([config_flag+'='+str(app.PROJECT/'demos/z01_shared_account/startup.yaml'),
                                     id_flag, 'option_z01'])
                self.assertEqual(result['strategies'], ['option-vega-z01'])

    def test_z01_live_inputs_reference_only_its_copied_configuration(self):
        from bomber.framework.cli import load_config
        from bomber.framework.account_config import read_account_config
        app = import_module('demos.direct_ctp')
        path = app.PROJECT/'demos/z01_08_option_vega'
        live = load_config(path/'config/strategy.yaml', 'live_simnow')
        for key in ('strategy_config', 'market_config', 'index_feature_config', 'report_dir'):
            self.assertTrue((app.PROJECT/live[key]).resolve().is_relative_to(path), key)
        for key in ('strategy_config', 'market_config', 'index_feature_config'):
            self.assertTrue((app.PROJECT/live[key]).is_file(), key)
        original = read_account_config(app.PROJECT/'demos/shared_account/account.json')
        copied = read_account_config(app.PROJECT/'demos/z01_shared_account/account.json')
        self.assertNotEqual(original['account']['client_id'], copied['account']['client_id'])
        self.assertNotEqual(original['account_state_file'], copied['account_state_file'])
        self.assertEqual(copied['account']['max_session_orders'], 200)
        own_parameters = json.loads((path/'config/vega_live.json').read_text())
        entry = import_module('demos.08_option_vega.run_live')
        with patch('dotenv.load_dotenv', return_value=False):
            args = entry.shared_arguments(dict(state_file=str(path/'results/direct/signal.json'),
                argv=['--config-file', str(path/'config/strategy.yaml'), '--config-id', 'live_simnow']),
                dict(trading_day='20261125', seconds=86400), resume=False)
        self.assertEqual(args.strategy_config.resolve(), path/'config/vega_live.json')
        self.assertEqual(args.index_feature_config.resolve(), path/'feature/index.json')
        self.assertEqual(args.market_bar_max_age_seconds, 300)
        self.assertEqual(args.config.index_signal_minutes, 1)
        self.assertEqual(args.config.option_slot, own_parameters['option_slot'])
        self.assertEqual(tuple(args.config.hedge_slots), tuple(own_parameters['hedge_slots']))
        self.assertEqual(args.config.flatten_slot, own_parameters['flatten_slot'])

    def test_daily_order_counters_separate_accounts_even_with_same_database_and_order_identity(self):
        from bomber.framework.trader.execution.ctp.independent import DailyOrderCounter
        from bomber.framework.trader.execution.contracts import OrderBudgetExhausted
        with TemporaryDirectory() as directory:
            path = Path(directory)/'daily.sqlite3'
            first = DailyOrderCounter(path, '9999:original', 2)
            second = DailyOrderCounter(path, '9999:z01', 2)
            first.reserve('20261125', 'same-session-ref')
            first.reserve('20261125', 'second-ref')
            with self.assertRaises(OrderBudgetExhausted):
                first.check('20261125')
            second.check('20261125')
            second.reserve('20261125', 'same-session-ref')
            DailyOrderCounter(path, '9999:z01', 2).check('20261125')
            first.check('20261126')


class Z01CancellationTests(unittest.TestCase):
    """使用z01真实参数和策略ID跑实际执行链；只替换柜台和原生时钟。"""
    native_modules = option_fixture.OptionNTAlgoTests.native_modules
    native_account = option_fixture.OptionNTAlgoTests.native_account
    make_account = option_fixture.OptionNTAlgoTests.make_account
    tick = option_fixture.OptionNTAlgoTests.tick
    setup_strategy = option_fixture.OptionNTAlgoTests.setup_strategy

    def setUp(self):
        option_fixture.OptionNTAlgoTests.setUp(self)
        self.sid = 'option-vega-z01'
        self.config_file = Path(__file__).resolve().parents[1]/'demos/z01_08_option_vega/config/vega_live.json'
        self.original_config = self.config_file.read_bytes()
        values = json.loads(self.original_config)
        signal = import_module('demos.08_option_vega.vega_signal')
        from dataclasses import fields
        names = {field.name for field in fields(signal.OptionVegaConfig)}
        self.config = signal.OptionVegaConfig(**{key: value for key, value in values.items() if key in names})
        self.addCleanup(lambda: self.assertEqual(self.config_file.read_bytes(), self.original_config))

    def episode(self, **kwargs):
        return self.setup_strategy(strategy_id=self.sid, strategy_config=self.config, **kwargs)

    def test_z01_three_limit_attempts_finish_without_aborting_or_resetting_entry_time(self):
        account, service, broker, port, runtime, strategy, _, events = self.episode(
            execution_trading_day='20261013')
        strategy.sent_days.add(date(2026, 10, 13))
        for _ in range(12):
            self.tick(service, runtime)
            port.drain_events()
            if not runtime.snapshot_state()['tasks']:
                break
        self.assertEqual(len(broker.sent), 3)
        self.assertEqual(account.driver.order_budget['submitted'], 3)
        self.assertEqual(strategy.fills_received, 0)
        self.assertEqual(strategy.option_goals['MO2612-C-6000'], 0)
        self.assertEqual(strategy.config, self.config)
        self.assertEqual(strategy.sent_days, {date(2026, 10, 13)})
        self.assertTrue(service.client.is_armed)
        self.assertFalse(service._failed)
        self.assertNotIn(self.sid, service._suspended)
        self.assertFalse(runtime.snapshot_state()['tasks'])
        canceled = [event for event in events if getattr(getattr(event, 'status', None), 'value', '') == 'CANCELED']
        self.assertEqual([event.metadata['nt_algo_cancel_reason'] for event in canceled],
            ['retry', 'retry', 'attempt_limit'])
        self.assertTrue(all(event.identity.strategy_id == self.sid for event in events))

    def check_previous_day_cancel(self, reason):
        account, service, broker, port, runtime, strategy, future, _ = self.episode(
            max_attempts=1, execution_trading_day='20261014')
        broker.fill(broker.sent[-1]['OrderRef'], 4)
        port.drain_events()
        broker.fill(broker.sent[-1]['OrderRef'])
        port.drain_events()
        strategy.sent_days.add(date(2026, 10, 13))
        if reason == 'attempt_limit':
            self.tick(service, runtime)
        else:
            service.suspend_strategy(self.sid)
            service.poll_stopping()
        # 恢复入口以原真实归属接续目标；不恢复未成交的8手意图。
        strategy.option_goals['MO2612-C-6000'] = Decimal(-4)
        strategy.future_goals['IM2612'] = Decimal(1)
        before = (service.position_manager.state(), deepcopy(account.driver.order_budget),
                  strategy.submissions, port._journal_cursor, service.event_journal.ack[self.sid])
        with self.assertLogs('OptionExecutionRecovery', level='WARNING'):
            self.assertEqual(port.drain_events(), 1)
        self.assertEqual(service.position_manager.state(), before[0])
        self.assertEqual(account.driver.order_budget, before[1])
        self.assertEqual(strategy.submissions, before[2])
        self.assertEqual(port._journal_cursor, before[3]+1)
        self.assertEqual(service.event_journal.ack[self.sid], before[4])
        self.assertEqual(service.position_manager.position(self.sid, str(self.instrument)), -4)
        self.assertEqual(service.position_manager.position(self.sid, str(future)), 1)
        self.assertEqual(strategy.sent_days, {date(2026, 10, 13)})
        self.assertEqual(strategy.config, self.config)
        self.assertFalse(service._failed)
        self.assertFalse(runtime.snapshot_state()['tasks'])
        self.assertEqual(len(broker.sent), 2)

    def test_z01_yesterday_attempt_limit_preserves_partial_fills_and_order_count(self):
        self.check_previous_day_cancel('attempt_limit')

    def test_z01_yesterday_stop_cancel_is_consumed_without_reopening_old_orders(self):
        self.check_previous_day_cancel('strategy_cancel')

    def test_z01_external_cancel_still_closes_gate(self):
        _, service, broker, port, _, strategy, _, _ = self.episode(execution_trading_day='20261014')
        broker.cancel_order(broker.sent[-1]['OrderRef'])
        with self.assertRaisesRegex(RuntimeError, '订单CANCELED'):
            port.drain_events()
        self.assertTrue(service._failed)
        self.assertEqual(strategy.config, self.config)


if __name__ == '__main__':
    unittest.main()
