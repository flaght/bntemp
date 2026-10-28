"""复用现有离线SDK实体夹具，验证实际Hybrid Live装配；不联网、不报单。"""
from pathlib import Path
from importlib import import_module
from types import SimpleNamespace as NS
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
from datetime import date, datetime
import sys
import json
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
fixture = ROOT/'tests/run_option_vega_offline.py'
# 使用已有SDK替身初始化段；后续仍调用真实Runner、装配和Recording客户端。
bootstrap = fixture.read_text().split('import argparse\nparser =')[0]
namespace = {'__file__': str(fixture), '__name__': 'hybrid_sdk_fixture'}
exec(compile(bootstrap, str(fixture), 'exec'), namespace)
trader = sys.modules['bomber.framework.trader']
for suffix, names in [('contracts', ('DataBinding', 'ExecutionRoute')), ('execution.risk', ('RiskLimits',))]:
    imported = import_module('bomber.framework.trader.'+suffix)
    for name in names:
        setattr(trader, name, getattr(imported, name))
ctp = sys.modules['bomber.framework.trader.execution.ctp']
for name, suffix in [('CtpTdApiTransport', 'td_transport'), ('CtpNativeTraderDriver', 'native_driver')]:
    setattr(ctp, name, getattr(import_module(ctp.__name__+'.'+suffix), name))
InstrumentId = namespace['InstrumentId']
InstrumentId.venue = property(lambda self: self.value.rsplit('.', 1)[1])
base = sys.modules['bomber.framework.market.basic.base']
live = import_module('demos.10_hybrid_transformer.run_live')
adapter = import_module('demos.10_hybrid_transformer.model_adapter')
from tests.test_demo_hybrid_live import calendar, ns


class Upstream(base.MarketDataFeed):
    health_snapshot = None
    def connect(self): self._is_connected = True
    def disconnect(self): self._is_connected = False
    def _on_subscription_added(self, request): pass
    def _on_subscription_removed(self, request): pass
    def register_depth_handler(self, handler): self.depth = handler
    def register_health_handler(self, handler): pass


class Predictor:
    def __init__(self, **kwargs):
        self.signal_id = kwargs['signal_id']
        self.workflow = NS(history=[])
    def on_bar(self, row): return dict(predicted_z=.1, signal=1)
    def warm_history(self, rows):
        self.workflow.history = list(rows)
        return len(rows)


class HybridAssemblyTests(unittest.TestCase):
    def test_both_sources_real_runner_targets_dedup_and_clock_expiry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'release').mkdir()
            (root/'directions.csv').write_text('formula,direction\nf,1\n')
            signal = root/'signals.json'
            signal.write_text(json.dumps({'RB': dict(signal_id='1301', signal_method='threshold_signal',
                signal_params={'threshold': .02}, hold_bars=5)}))
            instrument = InstrumentId.from_str('rb2701.SHFE')
            refs = NS(instrument_id=instrument, trading_day=date(2026, 10, 28),
                started_ns=ns('2026-10-28 09:00:00'), refresh_on_event=False,
                spec=NS(venue='SHFE', currency='CNY', tick=Decimal(1), multiplier=Decimal(10)),
                snapshot=lambda stamp: NS(instrument=lambda p, r: 'rb2701', factor=lambda p, r: Decimal(1)))
            refs.instrument_meta = lambda: base.InstrumentMeta(instrument, price_precision=0, multiplier=Decimal(10))
            for source in ('ctp', 'dolphindb'):
                with self.subTest(source=source):
                    args = ['--connect', '--product', 'RB', '--release-path', str(root/'release'),
                        '--directions-file', str(root/'directions.csv'), '--signal-config', str(signal),
                        '--expected-trading-day', '20261028', '--expected-source-day', '20261027', '--market-source', source,
                        '--no-session-overrides']
                    if source == 'dolphindb':
                        market = root/'market.json'
                        market.write_text(json.dumps(dict(source='dolphindb', table='fixture',
                            timestamp_label='end', volume_mode='per-bar')))
                        args += ['--market-config', str(market)]
                    parsed = live.parse_args(args)
                    with patch.object(adapter, 'MizarWorkflowAdapter', Predictor):
                        session = live.assemble(
                            NS(references=refs, calendar=calendar(), upstream=Upstream('fixture')), NS(driver=None),
                            product=parsed.product, release_path=parsed.release_path,
                            directions_file=parsed.directions_file, signal_config=parsed.signal_config,
                            market_config=parsed.market_source_config, max_contracts=parsed.max_contracts, bar_directory=root/'market')
                    self.assertEqual(session.bar_feed.config.source, source)
                    session.runner._validate_configuration()
                    now = [ns('2026-10-28 09:02:02')]
                    session.runner._clock_ns = lambda: now[0]
                    session.runner._session_check = lambda: True
                    session.runner.instrument_health = None
                    session.runner.start()
                    try:
                        session.runner.accept_bars = True
                        session.runner.first_complete_bar_ns = 0
                        stamp = ns('2026-10-28 09:02:00')-1
                        bar = base.make_bar(instrument_id=instrument, meta=refs.instrument_meta(),
                            ts_event=stamp, ts_init=now[0], open=100, high=100, low=100, close=100, volume=2)
                        event = base.make_custom_bar(bar, dict(open=100, high=100, low=100, close=100,
                            volume=2, value=2000, open_interest=500,
                            trade_time=datetime.fromisoformat('2026-10-28T09:01:00+08:00')))
                        session.runner.publish('model-bars', bar)
                        session.runner.publish('model-bars', event)
                        self.assertEqual(session.strategy.bars_used, 1)
                        self.assertTrue(session.client.requests)
                        self.assertEqual(dict(session.client.requests[-1].logical_targets), {'rb_main': Decimal(2)})
                        count = len(session.client.requests)
                        session.runner.publish('model-bars', event)
                        self.assertEqual(session.strategy.bars_used, 1)
                        self.assertEqual(len(session.client.requests), count)
                        now[0] = ns('2026-10-28 09:07:02')
                        session.runner.expire_due()
                        self.assertFalse(session.strategy.holdings.active)
                        self.assertEqual(dict(session.client.requests[-1].logical_targets), {'rb_main': Decimal(0)})
                    finally:
                        session.runner.stop()


    def test_simnow_checkpoint_wiring_without_starting_channels(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'release').mkdir()
            (root/'directions.csv').write_text('formula,direction\nf,1\n')
            signal=root/'signals.json'
            signal.write_text(json.dumps({'RB':dict(signal_id='1301',signal_method='threshold_signal',
                signal_params={'threshold':.02},hold_bars=5)}))
            args=live.parse_args(['--connect','--mode','simnow','--enable-orders','--confirm-simnow',
                '--state-file',str(root/'state.json'),'--product','RB','--release-path',str(root/'release'),
                '--directions-file',str(root/'directions.csv'),'--signal-config',str(signal),
                '--expected-trading-day','20261028','--expected-source-day','20261027',
                '--no-session-overrides'])
            instrument=InstrumentId.from_str('rb2701.SHFE')
            refs=NS(instrument_id=instrument,trading_day=date(2026,10,28),
                started_ns=ns('2026-10-28 09:00:00'),refresh_on_event=False,
                spec=NS(venue='SHFE',currency='CNY',tick=Decimal(1),multiplier=Decimal(10)))
            refs.instrument_meta=lambda:base.InstrumentMeta(instrument,price_precision=0,multiplier=Decimal(10))
            driver=Mock(driver_id=live.CLIENT_ID,account_id='fixture')
            with patch.object(adapter,'MizarWorkflowAdapter',Predictor):
                session=live.assemble(
                    NS(references=refs,calendar=calendar(),upstream=Upstream('fixture')),NS(driver=driver),
                    product=args.product,release_path=args.release_path,directions_file=args.directions_file,
                    signal_config=args.signal_config,market_config=args.market_source_config,
                    mode=args.mode,max_contracts=args.max_contracts,state_file=args.state_file,bar_directory=root/'market')
            self.assertIs(session.strategy._checkpoint.__self__,session.manager)
            self.assertIs(session.manager.state_components['hybrid'].strategy,session.strategy)
            self.assertIs(session.manager.target_store,session.runner.target_store)
            self.assertFalse(session.execution.backend.is_started)
            driver.start.assert_not_called()
            driver.submit_order.assert_not_called()
            self.assertFalse((root/'state.json').exists())
            before=live.model_identity(release_path=args.release_path,directions_file=args.directions_file,
                signal_config=args.signal_config,market_config=args.market_source_config,
                trading_day=args.expected_trading_day)
            (root/'directions.csv').write_text('formula,direction\nf,-1\n')
            self.assertNotEqual(before,live.model_identity(
                release_path=args.release_path,directions_file=args.directions_file,
                signal_config=args.signal_config,market_config=args.market_source_config,
                trading_day=args.expected_trading_day))


    def test_resume_accepts_ctp_realtime_warmup_but_requires_database_if_history_enabled(self):
        from contextlib import redirect_stderr
        from io import StringIO
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'release').mkdir()
            (root/'directions.csv').write_text('formula,direction\nf,1\n')
            (root/'signals.json').write_text('{}')
            (root/'state.json').write_text('{}')
            command=['--connect','--mode','simnow','--enable-orders','--confirm-simnow',
                '--resume','--state-file',str(root/'state.json'),'--market-source','ctp',
                '--product','RB','--release-path',str(root/'release'),
                '--directions-file',str(root/'directions.csv'),'--signal-config',str(root/'signals.json'),
                '--expected-trading-day','20261028','--expected-source-day','20261027',
                '--no-session-overrides','--history-minutes','0']
            args=live.parse_args(command)
            self.assertTrue(args.resume)
            self.assertEqual(args.history_minutes,0)
            self.assertIsNone(args.history_config)
            self.assertIsNone(args.history_file)
            (root/'bars.csv').write_text('instrument_id,ts_event,close\n')
            with redirect_stderr(StringIO()),self.assertRaises(SystemExit):
                live.parse_args(command+['--history-minutes','300','--history-file',str(root/'bars.csv')])

    def test_restored_batch_expires_while_ctp_model_window_is_still_warming(self):
        class WarmingPredictor(Predictor):
            def on_bar(self, row):
                self.workflow.history.append(dict(row))
                return None
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'release').mkdir()
            (root/'directions.csv').write_text('formula,direction\nf,1\n')
            signal=root/'signals.json'
            signal.write_text(json.dumps({'RB':dict(signal_id='1301',signal_method='threshold_signal',
                signal_params={'threshold':.02},hold_bars=5)}))
            instrument=InstrumentId.from_str('rb2701.SHFE')
            refs=NS(instrument_id=instrument,trading_day=date(2026,10,28),
                started_ns=ns('2026-10-28 09:00:00'),refresh_on_event=False,
                spec=NS(venue='SHFE',currency='CNY',tick=Decimal(1),multiplier=Decimal(10)),
                snapshot=lambda stamp:NS(instrument=lambda p,r:'rb2701',factor=lambda p,r:Decimal(1)))
            refs.instrument_meta=lambda:base.InstrumentMeta(instrument,price_precision=0,multiplier=Decimal(10))
            def session(predictor, resume=False):
                with patch.object(adapter,'MizarWorkflowAdapter',predictor):
                    result=live.assemble(NS(references=refs,calendar=calendar(),upstream=Upstream('ctp-fixture')),
                        NS(driver=None),product='RB',release_path=root/'release',directions_file=root/'directions.csv',
                        signal_config=signal,market_config=live.load_market_config(source='ctp'),resume=resume,bar_directory=root/'market')
                result.runner._session_check=lambda:True
                result.runner.instrument_health=None
                return result
            def event(label):
                stamp=ns(label)+live.MINUTE-1
                bar=base.make_bar(instrument_id=instrument,meta=refs.instrument_meta(),ts_event=stamp,
                    ts_init=stamp,open=100,high=100,low=100,close=100,volume=2)
                return base.make_custom_bar(bar,dict(open=100,high=100,low=100,close=100,
                    volume=2,value=2000,open_interest=500,
                    trade_time=datetime.fromisoformat(label).replace(tzinfo=calendar().zone)))
            original=session(Predictor)
            original.runner._clock_ns=lambda:ns('2026-10-28 09:02:02')
            original.runner.start()
            try:
                original.runner.accept_bars=True
                original.runner.first_complete_bar_ns=0
                seed=event('2026-10-28 09:01:00')
                original.bar_feed._emit_values(instrument,seed.ts_event,
                    {key:Decimal(str(seed.factors[key])) for key in
                     ('open','high','low','close','volume','value','open_interest')})
                saved=live.HybridCheckpoint(original.strategy).snapshot_state()
                targets=dict(original.runner.target_store.all())
                expiry=original.strategy.holdings.active[0].expires_at_ns
            finally:
                original.runner.stop()
            restored=session(WarmingPredictor,resume=True)
            live.HybridCheckpoint(restored.strategy).restore_state(saved)
            restored.runner.target_store.restore(targets)
            restored.runner.hold_restored_target(restored.strategy.strategy_id,restored.strategy._revision)
            now=[ns('2026-10-28 09:03:02')]
            restored.runner._clock_ns=lambda:now[0]
            restored.runner.history_service.load=Mock(side_effect=AssertionError('must not read historical bars'))
            restored.runner.start()
            try:
                restored.runner.begin_bars()
                self.assertTrue(restored.runner.accept_bars)
                self.assertEqual(len(restored.strategy.predictor.workflow.history),1)
                self.assertEqual(restored.strategy.holdings.active[0].expires_at_ns,expiry)
                self.assertFalse(restored.client.requests)
                now[0]=ns('2026-10-28 09:05:02')
                current=event('2026-10-28 09:04:00')
                restored.bar_feed._emit_values(instrument,current.ts_event,
                    {key:Decimal(str(current.factors[key])) for key in
                     ('open','high','low','close','volume','value','open_interest')})
                self.assertEqual(restored.runner.bar_archive.recorded_bars,2)
                self.assertEqual(len(restored.strategy.predictor.workflow.history),2)
                self.assertIsNone(restored.strategy.last_prediction)
                self.assertEqual(len(restored.strategy.holdings.active),1)
                self.assertFalse(restored.client.requests)
                now[0]=expiry+2_000_000_000
                restored.runner.expire_due()
                self.assertFalse(restored.strategy.holdings.active)
                self.assertEqual(dict(restored.client.requests[-1].logical_targets),{'rb_main':Decimal(0)})
                restored.runner.history_service.load.assert_not_called()
            finally:
                restored.runner.stop()


    def test_ctp_session_hours_use_static_database_independently_from_daily_references(self):
        from contextlib import ExitStack
        cases=[({},None,'dfs://bomber_static','fut_trading_session'),
               ({'DDB_TRADING_HOURS_DATABASE':'dfs://custom_static','DDB_TRADING_HOURS_TABLE':'custom_hours'},
                None,'dfs://custom_static','custom_hours'),
               ({'DDB_TRADING_HOURS_DATABASE':'dfs://custom_static'},
                'dfs://explicit_static','dfs://explicit_static','fut_trading_session')]
        for environment,explicit,expected_database,expected_table in cases:
            with self.subTest(environment=environment,explicit=explicit),ExitStack() as resources:
                source=Mock()
                references=NS(spec=NS(venue='SHFE'),
                    snapshot=lambda stamp:NS(source_day=date(2026,10,27)))
                reference_config=NS(database='dfs://bomber_daily')
                upstream=Upstream('test-md')
                context=NS(trading_day='20261028',resources=resources)
                with patch.dict(live.os.environ,environment,clear=True), \
                     patch.object(live.DolphinDbReferenceConfig,'from_env',return_value=reference_config) as config, \
                     patch.object(live.ReferenceSourceFactory,'create_for',return_value=source), \
                     patch.object(live,'SourceRoleReferences',return_value=references), \
                     patch.object(live,'CachedRoleReferences',side_effect=lambda value:value), \
                     patch.object(live,'load_session_inputs',return_value=calendar()) as load_hours, \
                     patch.object(live,'create_market_feed',return_value=upstream):
                    result=live.prepare_inputs(context,NS(),product='RB',expected_source_day='20261027',
                        market_config=live.load_market_config(source='ctp'),no_session_overrides=True,
                        reference_database='dfs://bomber_daily',trading_hours_database=explicit)
                actual=load_hours.call_args.args[0]
                self.assertEqual(actual.database,expected_database)
                self.assertEqual(actual.hours_table,expected_table)
                self.assertEqual(actual.exchange,'SHFE')
                self.assertTrue(actual.no_overrides)
                config.assert_called_once_with(database='dfs://bomber_daily',read_timeout_seconds=15)
                self.assertIs(result.upstream,upstream)
                source.open.assert_called_once()
            source.close.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
