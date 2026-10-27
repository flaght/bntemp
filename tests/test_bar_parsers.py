"""执行真实解析器、行情工厂和 FileReplayFeed；只替换未安装的 Cython 实体。"""
from __future__ import annotations

import abc
import ast
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace as NS
from typing import Any, Callable, Mapping, Sequence
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class Identity(str):
    @classmethod
    def from_str(cls, value):
        return cls(value)

    @property
    def symbol(self):
        return NS(value=self.rsplit('.', 1)[0])

    @property
    def venue(self):
        return self.rsplit('.', 1)[1]


class Scalar(Decimal):
    @classmethod
    def from_str(cls, value):
        return cls(value)

    def as_double(self):
        return float(self)


class Entity:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def load_module(name, path):
    spec = spec_from_file_location(name, ROOT / path)
    module = module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_framework():
    # 只抽取纯 Python 工厂/订阅实现，绕过模块头的 Cython/numpy 导入。
    basic = ModuleType('bomber.framework.market.basic.base')
    basic.__dict__.update(dict(abc=abc, dataclass=dataclass, field=field,
        Decimal=Decimal, Enum=Enum, auto=auto, Any=Any, Callable=Callable,
        Mapping=Mapping, Sequence=Sequence, InstrumentId=Identity, BarType=Identity,
        Price=Scalar, Quantity=Scalar,
        **{name: type(name, (Entity,), {}) for name in ('Bar', 'CustomBar', 'TradeTick', 'QuoteTick')}))
    path = ROOT / 'bomber/framework/market/basic/base.py'
    names = {'DataType', 'SubscriptionRequest', 'InstrumentMeta', 'make_bar',
             'make_custom_bar', 'MarketDataFeed'}
    selected = [node for node in ast.parse(path.read_text()).body if getattr(node, 'name', None) in names]
    selected.insert(0, ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0))
    tree = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    with patch.dict(sys.modules, {basic.__name__: basic}):
        exec(compile(tree, str(path), 'exec'), basic.__dict__)
        context = load_module('bomber.framework.market.replay.parsers.base',
                              'bomber/framework/market/replay/parsers/base.py')
        mapped = load_module('bomber.framework.market.replay.parsers.bar.mapped',
                             'bomber/framework/market/replay/parsers/bar/mapped.py')
        fixed = load_module('_fixed_bar_parser_check', 'bomber/framework/market/replay/parsers/bar/fixed.py')
        feed = load_module('_bar_replay_feed_check', 'bomber/framework/market/replay/base.py')
        futures = load_module('_bar_futures_check', 'bomber/framework/market/futures.py')
    return NS(basic=basic, context=context, mapped=mapped, fixed=fixed, feed=feed, futures=futures)


class BarParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fw = load_framework()

    def setUp(self):
        self.id = Identity('p2601.DCE')
        self.meta = self.fw.basic.InstrumentMeta(self.id, price_precision=2, size_precision=0)
        self.context = self.fw.context.ParserContext(Path('/bars/example.feather'), 7,
                            lambda identity: self.meta if identity == self.id else None)
        self.row = dict(datetime='2026-10-23 09:01:00',
                        source_timestamp=datetime(2026, 10, 23, 9, 0),
                        open='100.123', high='101.987', low='99.876', close='100.567',
                        volume='20.25', amount='20382.75', oi='1234.5')
        self.completed = self.fw.context.parse_iso_timestamp(
            self.row['datetime'], self.context, self.fw.mapped.ZoneInfo('Asia/Shanghai'))
        self.row['available_ns'] = self.completed + 2_000_000_000

    def parser(self, **changes):
        options = dict(value='amount', open_interest='oi', trade_time='source_timestamp',
                       preserve_raw_ohlcv=True, available_nanoseconds='available_ns')
        options.update(changes)
        return self.fw.fixed.FixedInstrumentBarParser(self.id, **options)

    def test_default_fixed_parser_still_only_emits_native_bar(self):
        before = dict(self.row)
        events = list(self.fw.fixed.FixedInstrumentBarParser(self.id).parse(self.row, self.context))
        self.assertEqual([e.data_type for e in events], [self.fw.basic.DataType.BAR])
        self.assertEqual(events[0].payload.ts_init, self.completed)
        self.assertEqual(events[0].payload.close, Decimal('100.57'))
        self.assertEqual(self.row, before)

    def test_precision_and_three_time_semantics_survive_futures_extraction(self):
        events = list(self.parser().parse(self.row, self.context))
        self.assertEqual([e.data_type for e in events],
                         [self.fw.basic.DataType.BAR, self.fw.basic.DataType.CUSTOM_BAR])
        bar, custom = (e.payload for e in events)
        self.assertIs(custom.bar, bar)
        self.assertEqual(bar.close, Decimal('100.57'))
        self.assertEqual(bar.volume, 20)
        self.assertEqual(custom.factors['close'], 100.567)
        self.assertEqual(custom.factors['volume'], 20.25)
        self.assertEqual(custom.factors['open_interest'], 1234.5)
        self.assertIs(custom.factors['trade_time'], self.row['source_timestamp'])
        for event in (bar, custom):
            self.assertEqual(event.ts_event, self.completed)
            self.assertEqual(event.ts_init, self.row['available_ns'])
        values = self.fw.futures.futures_bar_values(custom, contract_multiplier=10)
        self.assertEqual((values['close'], values['volume'], values['openint']), (100.567, 20.25, 1234.5))
        self.assertAlmostEqual(values['vwap'], 20382.75 / 20.25 / 10)

    def test_existing_mapped_optional_fields_keep_their_default_shape(self):
        columns = self.fw.mapped.BarColumns('symbol', 'exchange', 'datetime',
            'open', 'high', 'low', 'close', 'volume', value='amount', open_interest='oi')
        events = list(self.fw.mapped.MappedBarParser(columns).parse(
            dict(self.row, symbol='p2601', exchange='dce'), self.context))
        self.assertEqual(events[1].payload.factors, dict(value=20382.75, open_interest=1234.5))

    def test_mapped_aliases_and_non_minute_spec_propagate_to_both_events(self):
        columns = self.fw.mapped.BarColumns(symbol='contract', exchange='venue', timestamp='end',
            open='o', high='h', low='l', close='c', volume='v', value='cash',
            open_interest='interest', trade_time='source', bar_spec='period', available_timestamp='available')
        parser = self.fw.mapped.MappedBarParser(columns, exchange_aliases={'XDCE': 'DCE'}, preserve_raw_ohlcv=True)
        row = dict(contract='p2601', venue='XDCE', end=self.row['datetime'], o=100.123,
                   h=101.987, l=99.876, c=100.567, v=20.25, cash=20382.75,
                   interest=0, source=self.row['source_timestamp'], period='5-minute', available='2026-10-23 09:01:02')
        events = list(parser.parse(row, self.context))
        self.assertEqual([e.bar_spec for e in events], ['5-MINUTE', '5-MINUTE'])
        self.assertEqual(events[1].payload.factors['open'], 100.123)
        self.assertEqual(events[1].payload.ts_init, self.row['available_ns'])
        self.assertEqual(events[1].instrument_id, self.id)

    def test_raw_only_or_source_time_only_can_emit_a_companion(self):
        for options, fields in ((dict(preserve_raw_ohlcv=True), {'open', 'high', 'low', 'close', 'volume'}),
                                (dict(trade_time='source_timestamp'), {'trade_time'})):
            events = list(self.fw.fixed.FixedInstrumentBarParser(self.id, **options).parse(self.row, self.context))
            self.assertEqual(len(events), 2)
            self.assertEqual(set(events[1].payload.factors), fields)

    def test_optional_vwap_is_passed_through_without_recomputation(self):
        events = list(self.parser(vwap='avg').parse(dict(self.row, avg=100.5), self.context))
        self.assertEqual(events[1].payload.factors['vwap'], 100.5)

    def test_bad_fields_fail_with_source_location_before_any_event_is_returned(self):
        cases = [('amount', None), ('amount', -1), ('oi', float('nan')), ('oi', -1),
                 ('source_timestamp', None), ('source_timestamp', ' '), ('close', float('inf')),
                 ('high', 99), ('low', 102), ('volume', -1),
                 ('available_ns', self.completed - 1), ('available_ns', '1.5')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(self.fw.context.DataLoadError, 'example.feather:7:'):
                    list(self.parser().parse(dict(self.row, **{field: value}), self.context))

    def test_explicit_extra_columns_are_required_and_not_guessed(self):
        for name in ('amount', 'oi', 'source_timestamp'):
            row = dict(self.row)
            del row[name]
            with self.subTest(column=name), self.assertRaises(self.fw.context.DataLoadError):
                list(self.parser().parse(row, self.context))

    def test_zero_volume_is_valid_for_bar_but_rejected_by_vwap_helper(self):
        events = list(self.parser().parse(dict(self.row, volume=0, amount=0), self.context))
        self.assertEqual(len(events), 2)
        with self.assertRaisesRegex(ValueError, '成交量为零'):
            self.fw.futures.futures_bar_values(events[1].payload, contract_multiplier=10)

    def test_fixed_bar_spec_and_reset_preserve_configuration(self):
        parser = self.parser(bar_spec='5-MINUTE')
        for _ in range(2):
            parser.reset()
            events = list(parser.parse(self.row, self.context))
            self.assertEqual([e.bar_spec for e in events], ['5-MINUTE', '5-MINUTE'])
            self.assertEqual(events[1].payload.factors['value'], 20382.75)

    def test_file_replay_preserves_pair_order_and_delayed_availability(self):
        class Rows:
            def __init__(self, row): self.row = row
            def read(self, path): yield 1, self.row
        feed = self.fw.feed.FileReplayFeed('parser-check')
        feed.register_instrument(self.meta)
        for kind in (self.fw.basic.DataType.BAR, self.fw.basic.DataType.CUSTOM_BAR):
            feed.subscribe(self.id, kind, '1-MINUTE')
        delivered = []
        feed.register_bar_handler(lambda bar: delivered.append(('bar', bar)))
        feed.register_custom_bar_handler(lambda bar: delivered.append(('custom', bar)))
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'bars.feather'
            path.touch()
            feed.add_source(path, Rows(self.row), self.parser(), 'bar')
            feed.connect()
            try:
                summary = feed.replay()
                self.assertEqual((summary.bars, summary.custom_bars), (1, 1))
                self.assertEqual([kind for kind, _ in delivered], ['bar', 'custom'])
                self.assertIs(delivered[1][1].bar, delivered[0][1])
                self.assertEqual(delivered[1][1].ts_init, self.row['available_ns'])
            finally:
                feed.disconnect()

    def _run_demo_entry(self, commission_config=None, **extra):
        # 执行当前 run_case 的真实函数体，外部引擎/模型替换为离线组件。
        from dataclasses import asdict, is_dataclass
        from datetime import date, timedelta
        import json
        path = ROOT / 'demos/10_hybrid_transformer/run_backtest.py'
        function = next(n for n in ast.parse(path.read_text()).body
                        if isinstance(n, ast.FunctionDef) and n.name == 'run_case')
        function.decorator_list = []
        days = (date(2026, 10, 23), date(2026, 10, 24))
        assignments = [NS(instrument=lambda *args: 'p2601'), NS(instrument=lambda *args: 'p2605')]
        loaded = NS(day_end_ns=dict(zip(days, (1, 2))),
                    store=NS(snapshot=lambda end: assignments[end - 1]))
        class Report:
            empty = True
            def __len__(self): return 0
            def to_csv(self, path, **kwargs): Path(path).write_text('')
        trader = NS(**{name: lambda *args: Report() for name in ('generate_orders_report',
            'generate_fills_report', 'generate_positions_report', 'generate_account_report')})
        backend = NS(engine=NS(trader=trader), add_profile=Mock(), add_instrument=Mock(),
                     write_commission_report=Mock())
        feed = self.fw.feed.FileReplayFeed('entry-check')
        executed = []
        class Runtime:
            def __init__(self, *args): pass
            def run(self):
                feed.connect()
                executed.extend(feed.load_events())
                return NS(backend_result=NS(run_id='check'))
            def stop(self): feed.disconnect()
        class Frame:
            columns = set(self.row)
            def __init__(self, row): self.row = row
        class Reader:
            def __init__(self, frame): self.frame = frame
            def read(self, path): yield 1, self.frame.row
        def instrument(basic, profile, product, symbol):
            identity = Identity(symbol + '.DCE')
            return NS(id=identity), self.fw.basic.InstrumentMeta(identity), Decimal(10)
        build = Mock(return_value=NS(positions=object(), client=NS(client_id='sim', report_errors=[])))
        strategy = NS(bars_used=300, signal_records=[], batch_records=[],
                      holdings=NS(capacities=(2, 2, 2, 2, 2), active=()), last_targets={},
                      last_main='p2605', last_target=Decimal(0), unavailable_events=0)
        basic = NS(columns={'code', 'symbol', 'exchangeCD', 'contMultNum', 'minChgPriceNum',
                            'listDate', 'lastTradeDate'})
        from bomber.framework.fees import CommissionConfig
        profile_builder = Mock(side_effect=lambda **kwargs: NS(**kwargs))
        namespace = dict(date=date, timedelta=timedelta, Decimal=Decimal, Path=Path,
            CommissionConfig=CommissionConfig,
            asdict=asdict, is_dataclass=is_dataclass, json=json,
            load_product_signal=lambda *args: NS(signal_id='test', hold_bars=5,
                signal_method='linear_signal', signal_params={}),
            prepare_role_research=lambda **kwargs: loaded, read_feather=lambda path: basic,
            Venue=str, venue_for_product=lambda *args: 'DCE', NautilusSimExecutionBackend=lambda *args: backend,
            BacktestEngineConfig=lambda **kwargs: NS(**kwargs), TraderId=str,
            LoggingConfig=lambda **kwargs: NS(**kwargs), CtpFuturesBasicProfile=profile_builder,
            FileReplayFeed=lambda *args: feed, build_instrument=instrument,
            HybridTransformerConfig=lambda **kwargs: NS(target_key='p_main', **kwargs),
            BarReadSpec=lambda **kwargs: NS(**kwargs),
            read_bar_frame=lambda path, **kwargs: NS(frame=Frame(self.row)),
            FixedInstrumentBarParser=self.fw.fixed.FixedInstrumentBarParser, PreparedFrameReader=Reader,
            build_simulation_execution=build, RiskLimits=lambda **kwargs: NS(**kwargs),
            MizarWorkflowAdapter=lambda **kwargs: object(), HybridTransformerStrategy=lambda *args, **kwargs: strategy,
            NautilusMarketFeedAdapter=lambda *args, **kwargs: object(), MarketStreamBinding=lambda *args: args,
            UnifiedStrategyRunner=lambda *args, **kwargs: object(), RuntimeMode=NS(HISTORICAL='historical'),
            DataBinding=lambda *args: args, ExecutionRoute=lambda *args: args,
            StrategyBindings=lambda **kwargs: NS(**kwargs), assemble_strategy=Mock(),
            UnifiedHistoricalRuntime=Runtime, DataType=self.fw.basic.DataType,
            write_input_reports=Mock(), HybridCheckpoint=lambda strategy: NS(snapshot_state=lambda: {}))
        def assemble(*args, **kwargs):
            for identity in ('p2601.DCE', 'p2605.DCE'):
                for kind in (self.fw.basic.DataType.BAR, self.fw.basic.DataType.CUSTOM_BAR):
                    feed.subscribe(identity, kind, '1-MINUTE')
        namespace['assemble_strategy'].side_effect = assemble
        tree = ast.Module(body=[ast.ImportFrom(module='__future__',
            names=[ast.alias(name='annotations')], level=0), function], type_ignores=[])
        exec(compile(ast.fix_missing_locations(tree), str(path), 'exec'), namespace)
        with TemporaryDirectory() as directory, patch.dict(sys.modules, {'pandas': NS(DataFrame=lambda rows: Report())}):
            directory = Path(directory)
            def bar_path(root, symbol, day):
                result = directory / f'{symbol}_{day}.feather'
                result.touch()
                return result
            namespace['bar_path'] = bar_path
            output = namespace['run_case'](product='P', start_day=days[0], end_day=days[1],
                bars_dir=directory, contract_struct=directory / 'roles', fut_basic=directory / 'basic',
                release_path=directory, directions_file=directory / 'directions',
                signal_config=directory / 'signals', report_dir=directory, max_contracts=10,
                calendar=NS(product='P', provenance={'sha256': 'fixture'}),
                value_column='amount', open_interest_column='oi',
                commission_config=commission_config, **extra)
            self.assertTrue((output / 'summary.json').is_file())
            namespace['write_input_reports'].assert_called_once_with(output)
            backend.write_commission_report.assert_called_once_with(output)
        build.assert_called_once()
        self.assertEqual(len(executed), 6)  # 首日新主力一对；换月日新旧两合约各一对。
        for bar, custom in zip(executed[::2], executed[1::2]):
            self.assertIs(custom.bar, bar)
            self.assertIn('open_interest', custom.factors)
            self.assertEqual(custom.factors['close'], 100.567)
        profile_builder.assert_called_once()
        return profile_builder.call_args.kwargs

    def test_demo_entry_uses_framework_parser_and_builds_execution_once_for_two_days(self):
        profile = self._run_demo_entry()
        self.assertEqual(profile['commission_source'], 'metadata')
        self.assertEqual(profile['commission_multiplier'], Decimal(1))
        self.assertNotIn('commission_per_contract', profile)

    def test_demo_passes_explicit_fee_multipliers_to_framework_without_multiplying_them(self):
        from bomber.framework.fees import CommissionConfig
        config = CommissionConfig(commission_source='metadata', commission_multiplier='1.2',
            commission_product_multipliers={'P': '1.5'},
            commission_contract_multipliers={'p2601.DCE': '1.8'}, delivery_fee_multiplier='2')
        profile = self._run_demo_entry(config)
        self.assertEqual({key: profile[key] for key in config.profile_kwargs()}, config.profile_kwargs())
        self.assertNotIn('commission_per_contract', profile)

    def test_demo_rejects_automatic_and_fixed_fee_fallbacks(self):
        from bomber.framework.fees import CommissionConfig
        for source in ('auto', 'fixed'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "commission_source='metadata'"):
                self._run_demo_entry(CommissionConfig(commission_source=source))
        with self.assertRaisesRegex(ValueError, "commission_source='metadata'"):
            self._run_demo_entry(commission_config={})

    def test_demo_rejects_removed_fixed_fee_argument(self):
        with self.assertRaisesRegex(TypeError, 'commission_per_contract'):
            self._run_demo_entry(commission_per_contract=Decimal(0))


if __name__ == '__main__':
    unittest.main()
