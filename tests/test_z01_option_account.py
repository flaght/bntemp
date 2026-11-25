"""z01账号使用独立配置/状态，原08和10默认入口保持不变；全程离线。"""
from contextlib import redirect_stdout
from importlib import import_module
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from tests import test_direct_ctp as fixture


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

    def test_cli_overrides_profile_and_explicit_alternative_profile_is_supported(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.check(['--report-dir', str(root/'report'),
                                 '--account-state-file', str(root/'state.json')])
            self.assertEqual(result['report_dir'], str((root/'report').resolve()))
            self.assertEqual(result['state_file'], str((root/'state.json').resolve()))
            self.assertFalse(tuple(root.iterdir()))
        app = import_module('demos.direct_ctp')
        result = self.check(['--config-file='+str(app.PROJECT/'demos/z01_shared_account/startup.yaml'),
                             '--config-id', 'option_z01'])
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
        self.assertEqual(json.loads((path/'config/vega_live.json').read_text()),
            json.loads((app.PROJECT/'demos/08_option_vega/config/vega_live.json').read_text()))
        entry = import_module('demos.08_option_vega.run_live')
        with patch('dotenv.load_dotenv', return_value=False):
            args = entry.shared_arguments(dict(state_file=str(path/'results/direct/signal.json'),
                argv=['--config-file', str(path/'config/strategy.yaml'), '--config-id', 'live_simnow']),
                dict(trading_day='20261125', seconds=86400), resume=False)
        self.assertEqual(args.strategy_config.resolve(), path/'config/vega_live.json')
        self.assertEqual(args.index_feature_config.resolve(), path/'feature/index.json')
        self.assertEqual(args.market_bar_max_age_seconds, 300)
        self.assertEqual(args.config.index_signal_minutes, 1)

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


if __name__ == '__main__':
    unittest.main()
