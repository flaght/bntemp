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
                            market_config=parsed.market_source_config, max_contracts=parsed.max_contracts)
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
                    mode=args.mode,max_contracts=args.max_contracts,state_file=args.state_file)
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
