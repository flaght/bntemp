"""外部品种配置透传、可变持仓周期和入口参数回归。"""
import argparse
import ast
from datetime import date
from decimal import Decimal
from importlib import import_module
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
configuration = import_module('demos.10_hybrid_transformer.signal_config')
adapter_module = import_module('demos.10_hybrid_transformer.model_adapter')
holding = import_module('demos.10_hybrid_transformer.holding')


class ProductConfigTests(unittest.TestCase):
    def load(self, data, product='IF'):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'signals.json'
            path.write_text(json.dumps(data))
            return configuration.load_product_signal(path, product)

    def entry(self, **changes):
        return dict(signal_id='research-if-v2', signal_method='linear_threshold_signal',
                    signal_params={'roll_num': 0, 'threshold': .01, 'upper': .08},
                    hold_bars=3, **changes)

    def test_arbitrary_product_uses_its_own_method_parameters_and_period(self):
        data = {'IF': self.entry(), 'HC': dict(signal_id='hc-custom', signal_method='threshold_signal',
                                             signal_params={'threshold': .04}, hold_bars=7)}
        first = self.load(data, ' if ')
        second = self.load(data, 'HC')
        self.assertEqual((first.signal_method, first.hold_bars), ('linear_threshold_signal', 3))
        self.assertEqual(first.signal_params['upper'], .08)
        self.assertEqual((second.signal_method, second.hold_bars), ('threshold_signal', 7))
        with self.assertRaises(TypeError):
            first.signal_params['upper'] = .5

    def test_unconfigured_product_does_not_fall_back_to_p_or_rb(self):
        with self.assertRaisesRegex(ValueError, '没有品种'):
            self.load({'P': self.entry()})

    def test_missing_fields_duplicate_products_and_bad_periods_are_rejected(self):
        for field in ('signal_id', 'signal_method', 'signal_params', 'hold_bars'):
            entry = self.entry()
            del entry[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load({'IF': entry})
        with self.assertRaisesRegex(ValueError, '重复'):
            self.load({'IF': self.entry(), 'if': self.entry()})
        for value in (0, -1, True, 1.5, '5'):
            entry = self.entry()
            entry['hold_bars'] = value
            with self.subTest(period=value), self.assertRaises(ValueError):
                self.load({'IF': entry})

    def test_example_file_is_explicitly_selectable(self):
        config = configuration.load_product_signal(
            ROOT / 'demos/10_hybrid_transformer/signal_config.example.json', 'P')
        self.assertEqual((config.signal_id, config.signal_method, config.hold_bars),
                         ('1101', 'linear_signal', 5))


class AdapterConfigurationTests(unittest.TestCase):
    def test_registered_method_is_passed_through_without_product_or_id_presets(self):
        workflow = Mock(return_value=SimpleNamespace())
        fake = SimpleNamespace(LiveFlow=workflow, SIGNAL_FUNCTIONS={'custom_registered_signal': object()})
        with patch.object(adapter_module, '_load_live_flow', return_value=fake):
            adapter = adapter_module.MizarWorkflowAdapter(
                release_path='/release/IF', directions_file='/directions/IF.csv',
                code='if', signal_id='custom-id', signal_method='custom_registered_signal',
                signal_params={'threshold': .123, 'roll_num': 0})
        self.assertEqual(adapter.code, 'IF')
        self.assertEqual(workflow.call_args.kwargs['signals_method'], [{
            'signal_method': 'custom_registered_signal',
            'signal_params': {'threshold': .123, 'roll_num': 0}}])

    def test_unknown_method_uses_mizar_registry_and_fails_before_model_loading(self):
        workflow = Mock()
        fake = SimpleNamespace(LiveFlow=workflow, SIGNAL_FUNCTIONS={})
        with patch.object(adapter_module, '_load_live_flow', return_value=fake):
            with self.assertRaisesRegex(ValueError, '未注册'):
                adapter_module.MizarWorkflowAdapter(
                    release_path='/release/IF', directions_file='/directions/IF.csv',
                    code='IF', signal_id='any-id', signal_method='unknown', signal_params={})
        workflow.assert_not_called()


class PeriodParameterTests(unittest.TestCase):
    def test_arbitrary_period_changes_both_expiry_and_number_of_slots(self):
        for period in (1, 3, 5, 7):
            ledger = holding.PeriodHoldings(holding_bars=period, max_contracts=10)
            self.assertEqual(len(ledger.capacities), period)
            self.assertEqual(sum(ledger.capacities), 10)
            opened = ledger.advance('if2601', 1, 'A').opened
            self.assertEqual(opened.expires_bar, period)
            for index in range(1, period):
                self.assertFalse(ledger.advance('if2601', None, index).expired)
            self.assertEqual(ledger.advance('if2601', None, period).expired, (opened,))

    def test_invalid_period_is_rejected_before_division(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                holding.PeriodHoldings(holding_bars=value, max_contracts=10)


class EntryConfigurationTests(unittest.TestCase):
    def test_cli_accepts_other_products_and_passes_config_and_period_override(self):
        # 单独执行真实 main()，替换 run_case，避免导入原生交易引擎。
        path = ROOT / 'demos/10_hybrid_transformer/run_backtest.py'
        tree = ast.parse(path.read_text())
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        run_case = Mock(return_value=Path('/reports/example'))
        namespace = dict(argparse=argparse, date=date, Decimal=Decimal, Path=Path,
                         __doc__='test', __file__=str(path), run_case=run_case)
        exec(compile(ast.Module(body=[main], type_ignores=[]), str(path), 'exec'), namespace)
        args = ['run_backtest.py', '--product', 'IF', '--signal-config', '/signals.json',
                '--hold-bars', '3', '--start-day', '2025-01-01', '--end-day', '2025-01-02',
                '--bars-dir', '/bars', '--contract-struct', '/roles', '--fut-basic', '/basic',
                '--release-path', '/model', '--directions-file', '/directions']
        with patch('sys.argv', args), patch('builtins.print'), patch.object(Path, 'exists', return_value=True):
            namespace['main']()
        self.assertEqual(run_case.call_args.kwargs['product'], 'IF')
        self.assertEqual(run_case.call_args.kwargs['signal_config'], Path('/signals.json'))
        self.assertEqual(run_case.call_args.kwargs['hold_bars'], 3)


if __name__ == '__main__':
    unittest.main()
