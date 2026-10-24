"""五周期批次的到期义务、整数总预算和策略调用回归；原生实体用替身。"""
from datetime import datetime, timedelta
from decimal import Decimal
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from random import Random
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
holding = import_module("demos.10_hybrid_transformer.holding")


class HoldingLedgerTests(unittest.TestCase):
    def test_fixed_integer_budgets_sum_to_total_including_small_limits(self):
        for maximum, expected in ((10, (2, 2, 2, 2, 2)), (11, (3, 2, 2, 2, 2)),
                                  (1, (1, 0, 0, 0, 0))):
            self.assertEqual(holding.PeriodHoldings(holding_bars=5, max_contracts=maximum).capacities, expected)

    def test_latest_zero_does_not_erase_unexpired_batch(self):
        ledger = holding.PeriodHoldings(holding_bars=5, max_contracts=10)
        opened = ledger.advance('p2601', 1, 'A').opened
        for index in range(1, 5):
            update = ledger.advance('p2601', 0, f'A+{index}')
            self.assertEqual(update.active, (opened,))
            self.assertEqual(update.targets, {'p2601': Decimal(2)})
            self.assertFalse(update.expired)
        update = ledger.advance('p2601', 0, 'A+5')
        self.assertEqual(update.expired, (opened,))
        self.assertFalse(update.active)

    def test_same_direction_renews_expiry_without_changing_net_target(self):
        ledger = holding.PeriodHoldings(holding_bars=5, max_contracts=10)
        for index in range(5):
            previous = ledger.advance('p2601', 1, index)
        self.assertEqual(previous.targets['p2601'], 10)
        update = ledger.advance('p2601', 1, 5)
        self.assertEqual(update.targets, previous.targets)
        self.assertEqual(update.expired[0].batch_id, 0)
        self.assertEqual(update.opened.expires_bar, 10)
        self.assertNotIn(0, [batch.batch_id for batch in update.active])

    def test_opposite_batches_keep_their_separate_expiry_obligations(self):
        ledger = holding.PeriodHoldings(holding_bars=5, max_contracts=10)
        targets = [ledger.advance('p2601', signal, index).targets.get('p2601', 0)
                   for index, signal in enumerate((1, -1, None, None, None, None, None))]
        self.assertEqual(targets, [2, 0, 0, 0, 0, -2, 0])

    def test_none_prediction_still_expires_old_batch(self):
        ledger = holding.PeriodHoldings(holding_bars=5, max_contracts=10)
        ledger.advance('p2601', 1, 0)
        for index in range(1, 6):
            update = ledger.advance('p2601', None, index)
        self.assertEqual(len(update.expired), 1)
        self.assertFalse(update.active)

    def test_invalid_signal_does_not_mutate_ledger(self):
        ledger = holding.PeriodHoldings(holding_bars=5, max_contracts=10)
        ledger.advance('p2601', 1, 0)
        before = ledger.active
        for signal in (float('nan'), float('inf'), 1.1):
            with self.assertRaises(ValueError):
                ledger.advance('p2601', signal, 1)
            self.assertEqual(ledger.bar_index, 0)
            self.assertEqual(ledger.active, before)

    def test_total_gross_and_net_stay_bounded_for_mixed_signals(self):
        rng = Random(731)
        for maximum in range(1, 24):
            ledger = holding.PeriodHoldings(holding_bars=5, max_contracts=maximum)
            for index in range(100):
                update = ledger.advance('p2601' if index < 50 else 'p2605',
                                        rng.choice((1, -1, .3, -.7, 0, None)), index)
                self.assertLessEqual(update.gross_contracts, maximum)
                self.assertLessEqual(sum(abs(q) for q in update.targets.values()), maximum)
                self.assertEqual(len({b.slot for b in update.active}), len(update.active))

    def test_net_execution_matches_fixed_five_period_batch_pnl_before_costs(self):
        prices = [100, 101, 102, 103, 104, 105, 103]
        ledger = holding.PeriodHoldings(holding_bars=5, max_contracts=10)
        net_pnl = Decimal(0)
        batch_pnl = Decimal(0)
        for index, signal in enumerate((1, -1, None, None, None, None, None)):
            update = ledger.advance('p2601', signal, index)
            for batch in update.expired:
                batch_pnl += batch.quantity * (prices[index] - prices[batch.opened_bar])
            if index + 1 < len(prices):
                net_pnl += update.targets.get('p2601', 0) * (prices[index + 1] - prices[index])
        self.assertEqual(net_pnl, batch_pnl)
        self.assertEqual(net_pnl, 6)

    def test_small_budget_has_no_fractional_or_inflated_batches(self):
        ledger = holding.PeriodHoldings(holding_bars=5, max_contracts=1)
        opened = [ledger.advance('p2601', 1, index).opened for index in range(11)]
        self.assertEqual([index for index, batch in enumerate(opened) if batch], [0, 5, 10])
        self.assertEqual(holding.signal_to_contracts(.3, 2), 1)
        self.assertEqual(holding.signal_to_contracts(-1, 0), 0)


class TemplateStub:
    def __init__(self, strategy_id):
        self.targets = []

    def set_targets(self, targets, timestamp, **kwargs):
        self.targets.append((targets, timestamp, kwargs))


class SectorUnavailable(LookupError):
    pass


def load_strategy():
    name = 'demos.10_hybrid_transformer._holding_strategy_check'
    spec = spec_from_file_location(name, ROOT / 'demos/10_hybrid_transformer/strategy.py')
    module = module_from_spec(spec)
    stubs = {name: module}
    for path, attrs in {
        'bomber.framework.market.basic.base': {'CustomBar': type('CustomBar', (), {})},
        'bomber.framework.trader.template': {'StrategyTemplate': TemplateStub},
        'bomber.framework.datahub.sector_roles': {'SectorDataUnavailable': SectorUnavailable},
    }.items():
        stub = ModuleType(path)
        stub.__dict__.update(attrs)
        stubs[path] = stub
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


strategy_module = load_strategy()


class ModelAdapterShapeTests(unittest.TestCase):
    def test_workflow_wide_and_explicit_signal_columns_reach_strategy_contract(self):
        adapter_module = import_module('demos.10_hybrid_transformer.model_adapter')

        class OneRow:
            def __init__(self, row):
                self.columns = tuple(row)
                self.iloc = [row]

            def __len__(self):
                return 1

        for code, method, signal in (('P', 'linear_signal', .3),
                                     ('RB', 'threshold_signal', -1)):
            for column in (code, 'signal'):
                with self.subTest(code=code, column=column):
                    adapter = object.__new__(adapter_module.MizarWorkflowAdapter)
                    adapter.code, adapter.signal_method = code, method
                    adapter.signal_id = '1101' if code == 'P' else '1301'
                    adapter.workflow = SimpleNamespace(on_bar=Mock(return_value=OneRow({
                        column: signal, 'method': method, 'predicted_z': .015})))
                    result = adapter.on_bar({'code': code, 'trade_time': 'A'})
                    self.assertEqual(result['signal'], signal)
                    self.assertEqual(result['predicted_z'], .015)


class StrategyHoldingTests(unittest.TestCase):
    def make(self, *, rebalance=1, product='P', target_keys=None, hold_bars=5):
        self.main = product.lower() + '2601'
        self.venue = 'DCE' if product == 'P' else 'SHFE'
        config = strategy_module.HybridTransformerConfig(
            product, self.venue, '1101' if product == 'P' else '1301', 10,
            hold_bars, rebalance, Decimal(10), record_signals=True)
        self.predictor = SimpleNamespace(on_bar=Mock(return_value={'predicted_z': .1, 'signal': 1}))
        roles = SimpleNamespace(snapshot=Mock(side_effect=lambda ns: SimpleNamespace(
            instrument=lambda product, role: self.main)))
        self.strategy = strategy_module.HybridTransformerStrategy(
            'check', roles, self.predictor, config, target_keys=target_keys)
        return self.strategy

    def event(self, index, symbol=None, venue=None):
        instrument = SimpleNamespace(symbol=symbol or self.main, venue=venue or self.venue)
        return SimpleNamespace(ts_event=(index + 1) * 60_000_000_000,
            bar=SimpleNamespace(bar_type=SimpleNamespace(instrument_id=instrument)),
            factors=dict(open=100.123, high=102.456, low=99.789, close=101.234,
                         volume=2.5, value=2530.85, open_interest=20,
                         trade_time=datetime(2025, 1, 2, 9, 0) + timedelta(minutes=index)))

    def send(self, index, **kwargs):
        event = self.event(index, **kwargs)
        self.strategy.on_custom_bar(str(event.bar.bar_type.instrument_id), event)
        return event

    def test_pool_grows_to_ten_then_renews_without_redundant_target(self):
        strategy = self.make()
        for index in range(6):
            self.send(index)
        self.assertEqual([next(iter(row[0].values())) for row in strategy.targets], [2, 4, 6, 8, 10])
        self.assertEqual(strategy.holdings.active[-1].expires_bar, 10)
        self.assertEqual(strategy.batch_records[-2]['event'], 'expire')
        self.assertEqual(strategy.batch_records[-1]['event'], 'open')
        self.assertEqual(self.predictor.on_bar.call_args[0][0]['vwap'], 101.234)

    def test_no_prediction_does_not_suppress_expiry(self):
        strategy = self.make()
        self.send(0)
        self.predictor.on_bar.return_value = None
        for index in range(1, 6):
            self.send(index)
        self.assertEqual(strategy.targets[-1][0], {'p_main': Decimal(0)})
        self.assertFalse(strategy.holdings.active)

    def test_expiry_is_not_delayed_by_admission_interval(self):
        strategy = self.make(rebalance=2)
        for index in range(6):
            self.send(index)
        self.assertFalse(strategy.signal_records[-1]['rebalance_due'])
        self.assertEqual(strategy.signal_records[-1]['expired_contracts'], 2)
        self.assertEqual(strategy.targets[-1][0], {'p_main': Decimal(4)})

    def test_roll_keeps_old_contract_until_its_own_expiry(self):
        strategy = self.make(target_keys={'p2601': 'old', 'p2605': 'new'})
        self.send(0)
        self.main = 'p2605'
        self.send(1)
        self.assertEqual(strategy.targets[-1][0], {'old': Decimal(2), 'new': Decimal(2)})
        self.predictor.on_bar.return_value = None
        for index in range(2, 6):
            self.send(index)
        self.assertEqual(strategy.targets[-1][0], {'old': Decimal(0), 'new': Decimal(2)})
        self.send(6)
        self.assertEqual(strategy.targets[-1][0], {'old': Decimal(0), 'new': Decimal(0)})

    def test_nonmain_duplicate_and_bad_data_do_not_advance_holding_clock(self):
        strategy = self.make()
        self.send(0)
        self.send(0)
        self.send(1, symbol='p2605')
        self.send(1, venue='SHFE')
        event = self.event(1)
        event.factors['value'] = None
        with self.assertRaises(ValueError):
            strategy.on_custom_bar(str(event.bar.bar_type.instrument_id), event)
        self.assertEqual(strategy.holdings.bar_index, 0)
        self.assertEqual(self.predictor.on_bar.call_count, 1)

    def test_missing_per_contract_route_rejects_early_roll(self):
        strategy = self.make()
        self.send(0)
        self.main = 'p2605'
        with self.assertRaisesRegex(ValueError, 'target_keys'):
            self.send(1)
        self.assertEqual(strategy.holdings.bar_index, 0)

    def test_discrete_short_and_continuous_signals_use_slot_budget(self):
        strategy = self.make(product='RB')
        self.predictor.on_bar.return_value = {'predicted_z': -.1, 'signal': -1}
        self.send(0)
        self.assertEqual(strategy.last_target, -2)
        strategy = self.make()
        self.predictor.on_bar.return_value = {'predicted_z': .015, 'signal': .3}
        self.send(0)
        self.assertEqual(strategy.last_target, 1)

    def test_other_product_uses_configured_period_without_swapping_total_budget(self):
        strategy = self.make(product='HC', hold_bars=3)
        self.assertEqual(strategy.holdings.capacities, (4, 3, 3))
        self.assertEqual(strategy.holdings.max_contracts, 10)
        self.send(0)
        self.assertEqual(strategy.holdings.active[0].expires_bar, 3)
        self.predictor.on_bar.return_value = None
        for index in range(1, 4):
            self.send(index)
        self.assertEqual(strategy.last_target, 0)
        self.assertEqual(strategy.signal_records[-1]['holding_bars'], 3)


if __name__ == '__main__':
    unittest.main()
