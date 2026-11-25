"""Tick/Bar执行基准、逐次跳价及同代检查点；不连接真实柜台。"""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest

# 本机缺完整SDK时复用既有行情实体夹具；Feed、转换、缓存和执行规则仍为实际实现。
from tests import test_direct_ctp as sdk_fixture
from tests import test_nt_algo_ctp as fixture
from tests.run_hybrid_live_offline import base as market_base
from bomber.framework.market.stream.dolphin import DolphinDbLiveDataFeed, DolphinDbConfig, DolphinDbStreamSpec
from bomber.framework.market.native.dolphin.driver import OfficialDolphinDbDriver
from bomber.framework.trader.execution.risk import MarketReferencePriceStore as LivePriceStore
from bomber.framework.trader.execution.contracts import OrderSide as LiveSide


class ExecutionQuoteTests(unittest.TestCase):
    setUp = fixture.NTAlgoCtpTests.setUp
    native_modules = fixture.NTAlgoCtpTests.native_modules
    native_account = fixture.NTAlgoCtpTests.native_account
    make_account = fixture.NTAlgoCtpTests.make_account
    submit = fixture.NTAlgoCtpTests.submit
    request = fixture.NTAlgoCtpTests.request
    client = fixture.NTAlgoCtpTests.client
    tick = fixture.NTAlgoCtpTests.tick

    def quote(self, service, *, bid=2998, ask=3002, last=3000):
        service.prices.on_tick_snapshot(self.instrument, self.now,
            {'BidPrice1': bid, 'AskPrice1': ask, 'LastPrice': last})

    def test_bar_is_used_without_tick_configuration(self):
        _, service, broker, (port, _), _ = self.make_account()
        self.quote(service, bid=2900, ask=3100)  # 未配置的Tick不会改变执行基准。
        with self.assertLogs('NTAlgoExecution', level='INFO') as captured:
            self.submit(port, 1)
            service.prices.update(self.instrument, 3100, self.now)
            broker.fill(broker.sent[-1]['OrderRef'])
        self.assertEqual(broker.sent[-1]['LimitPrice'], 3000)
        self.assertIn('price_source=bar_close reference_price=3000 offset_ticks=0 tick_size=1 '
                      'order_price=3000', captured.output[0])
        self.assertIn('initial_price_source=bar_close initial_reference_price=3000 '
                      'last_order_price=3000', captured.output[-1])

    def check_three_prices(self, target, expected, source, reference):
        _, service, broker, (port, _), runtime = self.make_account()
        service.prices.enable_tick_quotes((self.instrument,))
        service.client.planner.planner._offset_ticks = 1
        self.quote(service)
        with self.assertLogs('NTAlgoExecution', level='INFO') as captured:
            self.submit(port, target)
            for _ in range(2):
                # 新Tick和Bar大幅变化也不重置步进基准，日志须保留实际定价依据。
                self.now += 1_000_000_000
                self.quote(service, bid=3100, ask=3102, last=3101)
                self.tick(service, runtime)
                service.poll()
            broker.fill(broker.sent[-1]['OrderRef'])
        self.assertEqual([row['LimitPrice'] for row in broker.sent], expected)
        self.assertFalse(runtime.snapshot_state()['tasks'])
        attempts = [line for line in captured.output if 'NT算法报单准备:' in line]
        self.assertEqual(len(attempts), 3)
        self.assertIn(f'price_source={source} reference_price={reference} offset_ticks=1 '
                      f'tick_size=1 order_price={expected[0]}', attempts[0])
        for index in (1, 2):
            self.assertIn(f'attempt={index + 1}', attempts[index])
            self.assertIn(f'price_source=previous_order reference_price={expected[index - 1]} '
                          f'offset_ticks=1 tick_size=1 order_price={expected[index]}', attempts[index])
        self.assertIn(f'strategy={port.strategy_id} instrument={self.instrument}', captured.output[-1])
        self.assertIn(f'initial_price_source={source} initial_reference_price={reference} '
                      f'last_order_price={expected[-1]}', captured.output[-1])

    def test_buy_starts_at_ask_plus_one_and_steps_from_actual_previous_price(self):
        self.check_three_prices(1, [3003, 3004, 3005], 'ask', 3002)

    def test_sell_starts_at_bid_minus_one_and_steps_from_actual_previous_price(self):
        self.check_three_prices(-1, [2997, 2996, 2995], 'bid', 2998)

    def test_zero_retry_step_keeps_last_order_price(self):
        _, service, broker, (port, _), runtime = self.make_account(retry_step_ticks=0)
        self.submit(port, 1)
        self.tick(service, runtime, price=3100)
        service.poll()
        self.assertEqual([row['LimitPrice'] for row in broker.sent], [3000, 3000])

    def test_tick_updated_during_planning_is_not_future_relative_to_actual_risk_read(self):
        _, service, broker, (port, _), _ = self.make_account()
        service.prices.enable_tick_quotes((self.instrument,), max_age_ns=10_000_000_000)
        self.quote(service)
        request_ns = self.now
        original = service.client.planner.plan
        def plan(request):
            self.now += 10_000_000
            service.prices.on_tick_snapshot(self.instrument, request_ns + 7_801_781,
                {'LastPrice': 3000, 'BidPrice1': 2998, 'AskPrice1': 3002})
            return original(request)
        self.enterContext(patch.object(service.client.planner, 'plan', side_effect=plan))
        self.submit(port, 1)
        self.assertEqual(len(broker.sent), 1)
        self.assertEqual(broker.sent[-1]['LimitPrice'], 3002)
        self.assertEqual(service._sources[port.strategy_id]['ts_event'], request_ns)
        self.assertFalse(service._failed)
        self.assertFalse(service._pending)
        broker.fill(broker.sent[-1]['OrderRef'])
        self.assertEqual(service.position_manager.position(port.strategy_id, 'main'), 1)

    def test_budget_check_uses_quote_read_time_after_async_tick_update(self):
        _, service, _, (port, _), _ = self.make_account()
        service.prices.enable_tick_quotes((self.instrument,), max_age_ns=10_000_000_000)
        request_ns = self.now
        self.now += 10_000_000
        service.prices.on_tick_snapshot(self.instrument, request_ns + 7_801_781,
            {'LastPrice': 3000, 'BidPrice1': 2998, 'AskPrice1': 3002})
        rows = (dict(strategy_id=port.strategy_id, target_key='main',
            instrument_id=str(self.instrument), quantity='1'),)
        service._check_budget(rows, port.budget, request_ns)

    def risk_order(self):
        return self.contracts.OrderIntent('one', 'account', self.instrument,
            'BUY', 1, order_type='LIMIT', price=3002, position_effect='OPEN')

    def test_tick_ahead_of_actual_local_clock_is_still_rejected(self):
        _, service, broker, _, _ = self.make_account()
        service.prices.enable_tick_quotes((self.instrument,), max_age_ns=10_000_000_000)
        service.prices.on_tick_snapshot(self.instrument, self.now + 1_000_000,
            {'LastPrice': 3000, 'BidPrice1': 2998, 'AskPrice1': 3002})
        with self.assertRaises(self.risk.RiskRejected):
            service.client.risk_manager.check((self.risk_order(),), now_ns=self.now)
        self.assertEqual(broker.sent, [])

    def test_old_request_time_cannot_rescue_expired_tick(self):
        _, service, broker, _, _ = self.make_account()
        service.prices.enable_tick_quotes((self.instrument,), max_age_ns=10_000_000_000)
        self.quote(service)
        request_ns = self.now
        self.now += 11_000_000_000
        with self.assertRaises(self.risk.RiskRejected):
            service.client.risk_manager.check((self.risk_order(),), now_ns=request_ns)
        self.assertEqual(broker.sent, [])

    def test_read_snapshot_still_expires_at_later_risk_check(self):
        _, service, broker, _, _ = self.make_account()
        service.prices.enable_tick_quotes((self.instrument,), max_age_ns=10_000_000_000)
        self.quote(service)
        snapshot = service.prices.get(self.instrument)
        self.now += 121_000_000_000
        with patch.object(service.prices, 'get', return_value=snapshot), \
                self.assertRaises(self.risk.RiskRejected):
            service.client.risk_manager.check((self.risk_order(),), now_ns=self.now)
        self.assertEqual(broker.sent, [])

    def test_bar_risk_check_keeps_explicit_time_and_rejects_future_bar(self):
        _, service, broker, _, _ = self.make_account()
        request_ns = self.now
        self.now += 10_000_000
        service.prices.update(self.instrument, 3000, request_ns + 7_801_781)
        with self.assertRaises(self.risk.RiskRejected) as caught:
            service.client.risk_manager.check((self.risk_order(),), now_ns=request_ns)
        self.assertIn(self.risk.RiskViolationCode.MARKET_STALE, {row.code for row in caught.exception.violations})
        self.assertEqual(broker.sent, [])

    def test_option_fractional_tick_is_applied_without_float_accumulation(self):
        _, service, broker, (port, _), runtime = self.make_account()
        planner = service.client.planner.planner
        planner._tick_size, planner._offset_ticks = {self.instrument: D('.2')}, 1
        service.prices.enable_tick_quotes((self.instrument,))
        self.quote(service, bid='118.2', ask='118.4', last='118.3')
        self.submit(port, 1)
        for _ in range(2):
            self.tick(service, runtime)
            service.poll()
        self.assertEqual([row['LimitPrice'] for row in broker.sent], [118.6, 118.8, 119])

    def test_legacy_empty_algo_state_migrates_without_changing_positions_or_count(self):
        account, _, broker, (port, _), runtime = self.make_account()
        self.submit(port, 1)
        broker.fill(broker.sent[-1]['OrderRef'])
        import json
        payload = json.loads(account.manager.repository.path.read_text())['payload']
        before = deepcopy(payload)
        payload['component_states']['nt_algo']['config'].pop('retry_step_ticks')
        result = account._adapt_algorithm_checkpoint(payload)
        self.assertEqual(result['component_states']['nt_algo'], runtime.snapshot_state())
        self.assertEqual(result['positions'], before['positions'])
        self.assertEqual(result['component_states']['shared_account'], before['component_states']['shared_account'])

    def test_legacy_active_task_is_rejected_instead_of_guessing_last_price(self):
        account, _, _, (port, _), _ = self.make_account()
        self.submit(port, 1)
        import json
        payload = json.loads(account.manager.repository.path.read_text())['payload']
        state = payload['component_states']['nt_algo']
        state['config'].pop('retry_step_ticks')
        for task in state['tasks'].values():
            task.pop('last_price')
        with self.assertRaisesRegex(ValueError, '禁止切换'):
            account._adapt_algorithm_checkpoint(payload)

    def test_configured_tick_does_not_fall_back_to_bar_or_last_price(self):
        _, service, broker, (port, _), runtime = self.make_account()
        prices = service.prices
        prices.enable_tick_quotes((self.instrument,))
        self.assertIsNone(prices.get(self.instrument))
        self.quote(service)
        self.submit(port, 1)
        self.tick(service, runtime)
        for bid, ask in ((0, 3002), (2998, 0), (3002, 2998), (2998, float('inf'))):
            self.quote(service, bid=bid, ask=ask)
            service.prices.update(self.instrument, 3000, self.now)
            service.poll()
            self.assertEqual(len(broker.sent), 1)
            self.assertIsNone(prices.get(self.instrument))
        self.quote(service)
        service.poll()
        self.assertEqual(broker.sent[-1]['LimitPrice'], 3003)

    def test_unchanged_depth_refreshes_freshness_and_old_depth_cannot_overwrite_it(self):
        _, service, _, _, _ = self.make_account()
        prices = service.prices
        prices.enable_tick_quotes((self.instrument,))
        self.quote(service)
        old = self.now
        self.now += 121_000_000_000
        self.quote(service)  # 没有价格变化，仍收到新的原始快照。
        prices.on_tick_snapshot(self.instrument, old,
            {'BidPrice1': 1, 'AskPrice1': 2, 'LastPrice': 1})
        quote = prices.get_quote(self.instrument)
        self.assertEqual((quote.bid_price, quote.ask_price, quote.last_price), (2998, 3002, 3000))
        self.assertEqual(prices.get(self.instrument).ts_event, self.now)

    def test_stale_tick_blocks_retry_even_with_fresh_bar(self):
        _, service, broker, (port, _), runtime = self.make_account()
        service.prices.enable_tick_quotes((self.instrument,))
        self.quote(service)
        self.submit(port, 1)
        self.tick(service, runtime)
        self.now += 121_000_000_000
        service.prices.update(self.instrument, 3100, self.now)
        service.poll()
        self.assertEqual(len(broker.sent), 1)
        self.quote(service)
        service.poll()
        self.assertEqual(broker.sent[-1]['LimitPrice'], 3003)

    def test_restart_keeps_last_actual_price_after_second_attempt(self):
        path = Path(self.enterContext(TemporaryDirectory())) / 'account.json'
        account, service, broker, (port, _), runtime = self.make_account(path)
        self.submit(port, 2)
        self.tick(service, runtime)
        service.poll()
        broker.fill(broker.sent[-1]['OrderRef'], 1)
        task_id = next(iter(runtime._tasks))
        self.assertEqual(D(runtime._tasks[task_id]['last_price']), 3001)
        account.driver.stop()
        account2, service2, broker2, ports2, runtime2 = self.make_account(path, start=False)
        broker2.active = {key: dict(value) for key, value in broker.active.items()}
        broker2.gross = dict(broker.gross)
        account2.start(resume=True)
        service2.start()
        for item in ports2:
            item.start()
        account2.authorize()
        self.submit(ports2[0], 2, revision=2)  # 原恢复闸门要求新的策略目标版本。
        with self.assertLogs('NTAlgoExecution', level='INFO') as captured:
            self.tick(service2, runtime2)
            service2.poll()
            broker2.fill(broker2.sent[-1]['OrderRef'])
        self.assertEqual(broker2.sent[-1]['LimitPrice'], 3002)
        self.assertEqual(broker2.sent[-1]['VolumeTotalOriginal'], 1)
        self.assertIn('price_source=previous_order reference_price=3001 offset_ticks=1 '
                      'tick_size=1 order_price=3002', captured.output[0])
        self.assertIn('initial_price_source=bar_close initial_reference_price=3000 '
                      'last_order_price=3002', captured.output[-1])

    def test_no_tick_table_does_not_implicitly_use_ctp_quotes(self):
        from demos.direct_ctp import configure_execution_quotes
        _, service, _, _, _ = self.make_account()
        feed = NS(register_depth_handler=Mock(), subscribe=Mock())
        host = NS(service=service, strategies={'one': NS(session=NS(upstream=feed))})
        configure_execution_quotes(host, {'execution_driver': 'nt_algo_ctp'})
        feed.register_depth_handler.assert_not_called()
        self.assertFalse(service.prices.uses_tick_quotes(self.instrument))

    def test_dolphin_tick_uses_bar_connection_and_stays_out_of_strategy_callbacks(self):
        from demos.direct_ctp import configure_execution_quotes
        from bomber.framework.market.sources import MarketSourceConfig
        _, service, broker, ports, _ = self.make_account()
        connection = NS(host='market.test', port=8848, username='market-user', password='fixture',
            streaming_port=0, keep_alive_seconds=60)
        source = MarketSourceConfig(source='dolphindb', table='bars', timestamp_label='end',
            volume_mode='cumulative', tick_table='ticks')
        bar = NS(config=connection, market_source_config=source,
            get_instrument_meta=lambda instrument: NS(instrument_id=instrument))
        runner = NS(add_data_feed=Mock())
        feed = NS(register_instrument=Mock(), subscribe=Mock(), register_tick_snapshot_handler=Mock(),
            connect=Mock(), disconnect=Mock())
        with ExitStack() as resources, \
                patch('bomber.framework.market.sources.create_dolphin_tick_feed', return_value=feed) as factory:
            host = NS(service=service, resources=resources, strategies={str(index): NS(session=NS(
                upstream=None, feeds={'market-bars': bar}, client=port, runner=runner))
                for index, port in enumerate(ports)})
            configure_execution_quotes(host, {'execution_driver': 'nt_algo_ctp'})
            factory.assert_called_once_with(['ticks'], 'execution-quotes-0',
                market_config=connection, max_age_seconds=10, dce_use_receive_date=False)
            feed.subscribe.assert_called_once()
            runner.add_data_feed.assert_not_called()
            callback = feed.register_tick_snapshot_handler.call_args.args[0]
            callback(self.instrument, self.now, {'LastPrice': 3000, 'BidPrice1': 2998, 'AskPrice1': 3002})
            self.submit(ports[0], 1)
            self.assertEqual(broker.sent[-1]['LimitPrice'], 3002)
        feed.disconnect.assert_called_once()

    def test_future_venues_merge_tables_on_same_dolphin_connection(self):
        from demos.direct_ctp import configure_execution_quotes
        from bomber.framework.market.sources import MarketSourceConfig
        _, service, _, ports, _ = self.make_account()
        other = type(self.instrument).from_str('p2701.DCE')
        service.instrument_limits[other] = service.instrument_limits[self.instrument]
        ports[1].routes = {'main': other}
        connection = NS(host='market.test', port=8848, username='market-user', password='fixture',
            streaming_port=0, keep_alive_seconds=60)
        sessions = {}
        for port, instrument, table in zip(ports, (self.instrument, other), ('shfe_ticks', 'dce_ticks')):
            bar = NS(config=connection, market_source_config=MarketSourceConfig(source='dolphindb',
                table='bars', timestamp_label='start', volume_mode='per-bar', tick_table=table),
                get_instrument_meta=lambda key: NS(instrument_id=key))
            sessions[port.strategy_id] = NS(session=NS(upstream=bar, client=port))
        feed = NS(register_instrument=Mock(), subscribe=Mock(), register_tick_snapshot_handler=Mock(),
            connect=Mock(), disconnect=Mock())
        with ExitStack() as resources, \
                patch('bomber.framework.market.sources.create_dolphin_tick_feed', return_value=feed) as factory:
            configure_execution_quotes(NS(service=service, resources=resources, strategies=sessions),
                {'execution_driver': 'nt_algo_ctp'})
            factory.assert_called_once_with(['dce_ticks', 'shfe_ticks'], 'execution-quotes-0',
                market_config=connection, max_age_seconds=10, dce_use_receive_date=False)
            self.assertEqual(feed.subscribe.call_count, 2)
            self.assertTrue(service.prices.uses_tick_quotes(other))

    def test_configured_tick_cannot_silently_fall_back_when_instrument_metadata_is_missing(self):
        from demos.direct_ctp import configure_execution_quotes
        _, service, _, (port, _), _ = self.make_account()
        bar = NS(config=NS(host='ddb', port=8848, username='user', password='fixture',
            streaming_port=0, keep_alive_seconds=60),
            market_source_config=NS(tick_table='ticks', tick_max_age_seconds=10),
            get_instrument_meta=lambda instrument: None)
        host = NS(service=service, strategies={'one': NS(session=NS(upstream=bar, client=port))})
        with self.assertRaisesRegex(ValueError, '未注册到对应Bar行情源'):
            configure_execution_quotes(host, {'execution_driver': 'nt_algo_ctp'})


class ExecutionQuoteConfigTests(unittest.TestCase):
    check_direct = fixture.NTAlgoConfigurationTests.check_direct

    def test_old_ctp_source_is_rejected_before_connection(self):
        from contextlib import redirect_stderr
        from io import StringIO
        with redirect_stderr(StringIO()) as output, self.assertRaises(SystemExit):
            self.check_direct(['--execution-driver', 'nt_algo_ctp', '--execution-tick-source', 'ctp'])
        self.assertIn('market_dolphindb.json', output.getvalue())

    def test_tick_fields_stay_in_original_market_json_without_changing_bar_identity(self):
        from bomber.framework.market.sources import MarketSourceConfig, market_config_from_args
        import json
        original = dict(source='dolphindb', table='bars', timestamp_label='end', volume_mode='cumulative')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'market_dolphindb.json'
            path.write_text(json.dumps({**original, 'tick_table': 'ticks', 'tick_max_age_seconds': 10,
                'tick_dce_use_receive_date': True}))
            config = market_config_from_args(NS(market_config=path), domestic_contracts=True)
        self.assertEqual(config.tick_table, 'ticks')
        self.assertEqual(config.tick_max_age_seconds, 10)
        self.assertTrue(config.tick_dce_use_receive_date)
        self.assertEqual(config.identity, MarketSourceConfig(**original).identity)
        self.assertIsNone(MarketSourceConfig(**original).tick_table)
        for values in ({'tick_table': 'dfs://ticks/tick'}, {'tick_table': ''},
                {'tick_max_age_seconds': 0}, {'tick_max_age_seconds': float('nan')},
                {'tick_max_age_seconds': True}, {'tick_dce_use_receive_date': 'true'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                MarketSourceConfig(**original, **values)

    def test_zero_or_one_step_is_still_configurable(self):
        for step in (0, 1):
            result = self.check_direct(['--execution-driver', 'nt_algo_ctp', '--algo-retry-step-ticks', str(step)])
            self.assertEqual(result['nt_algo']['retry_step_ticks'], step)

    def test_runtime_rejects_invalid_step(self):
        from bomber.framework.trader.execution.nt_algo import NTAlgoConfig
        for step in (-1, 2, .1, True):
            with self.subTest(step=step), self.assertRaises(ValueError):
                NTAlgoConfig(retry_step_ticks=step)


class DolphinExecutionTickTests(unittest.TestCase):
    class Session:
        def __init__(self, columns):
            self.columns, self.subscriptions = columns, []
        def connect(self, *args): return True
        def enableStreaming(self, *args): pass
        def run(self, query): return self.columns
        def subscribe(self, **kwargs): self.subscriptions.append(kwargs)
        def unsubscribe(self, *args): pass
        def close(self): pass

    def setUp(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.now = [int(datetime(2026, 11, 25, 13, 30, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1e9)]
        self.enterContext(patch('time.time_ns', side_effect=lambda: self.now[0]))
        self.instrument = market_base.InstrumentId.from_str('MO2612-C-6000.CFFEX')
        self.meta = market_base.InstrumentMeta(self.instrument, price_precision=1, price_increment=D('.2'))

    def make_feed(self, columns=None, table='ticks', *, dce_use_receive_date=False):
        columns = columns or ('Code', 'AskPrice1', 'LastPrice', 'BidPrice1', 'ActionDay',
            'UpdateTime', 'UpdateMillisec', 'Volume', 'BidVolume1', 'AskVolume1', 'extra')
        session = self.Session(columns)
        driver = OfficialDolphinDbDriver(session_factory=lambda **kwargs: session)
        spec = DolphinDbStreamSpec.tick(table, auto_schema=True, dce_use_receive_date=dce_use_receive_date)
        feed = DolphinDbLiveDataFeed(DolphinDbConfig('ddb.test', 8848, 'user', 'fixture', streams=(spec,)),
            driver_factory=lambda: driver)
        feed.register_instrument(self.meta)
        feed.subscribe(self.instrument, market_base.DataType.QUOTE_TICK)
        self.addCleanup(feed.disconnect)
        return feed, session

    def row(self, seconds=0, **values):
        return dict(Code='MO2612-C-6000', ActionDay='20261125', UpdateTime=f'13:30:{seconds:02d}',
            UpdateMillisec=0, LastPrice=118.3, BidPrice1=118.2, AskPrice1=118.4,
            Volume=100, BidVolume1=2, AskVolume1=3, extra='unused', **values)

    def publish(self, session, row):
        session.subscriptions[0]['handler']([row.get(column) for column in session.columns])

    def test_reordered_server_schema_delivers_unchanged_tick_and_respects_quote_expiry(self):
        feed, session = self.make_feed()
        prices = LivePriceStore()
        prices.update(self.instrument, 99, self.now[0])
        prices.enable_tick_quotes((self.instrument,), max_age_ns=10_000_000_000)
        feed.register_tick_snapshot_handler(prices.on_tick_snapshot)
        feed.connect()
        self.assertEqual(session.subscriptions[0]['offset'], -1)
        self.publish(session, self.row())
        self.assertEqual(prices.get_for_order(self.instrument, LiveSide.BUY).price, D('118.4'))
        self.assertEqual(prices.get_for_order(self.instrument, LiveSide.SELL).price, D('118.2'))
        self.now[0] += 9_000_000_000
        self.publish(session, self.row(9))  # 报价和累计成交量不变，派生事件会被去重。
        self.assertEqual(prices.get(self.instrument).ts_event, self.now[0])
        self.assertEqual(feed.manifest['received_ticks'], 2)
        self.assertEqual(prices.get_quote(self.instrument).last_price, D('118.3'))
        self.now[0] += 11_000_000_000
        prices.update(self.instrument, 123, self.now[0])
        self.assertIsNone(prices.get_for_order(self.instrument, LiveSide.BUY))
        self.publish(session, self.row(20))
        self.assertEqual(prices.get_for_order(self.instrument, LiveSide.BUY).price, D('118.4'))

    def test_missing_bid_ask_schema_fails_before_subscribing(self):
        feed, session = self.make_feed(('Code', 'LastPrice', 'BidPrice1', 'ActionDay', 'UpdateTime'))
        with self.assertRaisesRegex(ValueError, 'AskPrice1'):
            feed.connect()
        self.assertEqual(session.subscriptions, [])

    def test_raw_ctp_market_49_columns_and_night_date_route_rb_and_p(self):
        import numpy as np
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.instrument = market_base.InstrumentId.from_str('rb2701.SHFE')
        self.meta = market_base.InstrumentMeta(self.instrument, price_precision=0, price_increment=D(1))
        columns = ('TradingDay', 'ExchangeID', 'LastPrice', 'UpdateTime', 'UpdateMillisec',
            'BidPrice1', 'AskPrice1', 'ActionDay', 'InstrumentID', 'BidVolume1', 'AskVolume1', 'Volume')
        columns += tuple(f'extra_{index}' for index in range(37))
        feed, session = self.make_feed(columns, table='ctp_market')
        other = market_base.InstrumentId.from_str('p2701.DCE')
        feed.register_instrument(market_base.InstrumentMeta(other, price_precision=0, price_increment=D(2)))
        feed.subscribe(other, market_base.DataType.QUOTE_TICK)
        prices = LivePriceStore()
        prices.enable_tick_quotes((self.instrument, other), max_age_ns=10_000_000_000)
        feed.register_tick_snapshot_handler(prices.on_tick_snapshot)
        self.now[0] = int(datetime(2026, 11, 25, 21, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()) * 10**9 + 125_000_000
        feed.connect()
        self.assertEqual(session.subscriptions[0]['tableName'], 'ctp_market')
        for symbol, exchange, bid, ask in (('RB2701', 'SHFE', 3000, 3001), ('p2701', 'DCE', 8000, 8002)):
            row = dict(TradingDay=np.datetime64('2026-11-26', 'D'),
                ActionDay=np.datetime64('2026-11-25', 'D'),
                UpdateTime=np.datetime64('1970-01-01T21:00:00', 's'), UpdateMillisec=np.int32(125),
                InstrumentID=symbol, ExchangeID=exchange, LastPrice=bid,
                BidPrice1=bid, AskPrice1=ask, BidVolume1=2, AskVolume1=3, Volume=100)
            self.publish(session, row)
        self.assertEqual(prices.get_for_order(self.instrument, LiveSide.BUY).price, 3001)
        self.assertEqual(prices.get_for_order(other, LiveSide.SELL).price, 8000)
        self.assertEqual(prices.get(self.instrument).ts_event, self.now[0])
        self.assertEqual(prices.get(other).ts_event, self.now[0])
        self.assertIsNone(feed.conversion_failure)

    def test_tick_schema_without_either_identifier_is_rejected_before_subscribing(self):
        feed, session = self.make_feed(('LastPrice', 'BidPrice1', 'AskPrice1', 'ActionDay', 'UpdateTime'))
        with self.assertRaisesRegex(ValueError, 'Code.*InstrumentID'):
            feed.connect()
        self.assertEqual(session.subscriptions, [])

    def test_second_type_midnight_is_not_treated_as_missing_time(self):
        import numpy as np
        from datetime import datetime
        from zoneinfo import ZoneInfo
        feed, session = self.make_feed()
        prices = LivePriceStore()
        prices.enable_tick_quotes((self.instrument,), max_age_ns=10_000_000_000)
        feed.register_tick_snapshot_handler(prices.on_tick_snapshot)
        self.now[0] = int(datetime(2026, 11, 25, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()) * 10**9
        row = self.row()
        row.update(ActionDay=np.datetime64('2026-11-25', 'D'),
            UpdateTime=np.datetime64('1970-01-01T00:00:00', 's'))
        feed.connect()
        self.publish(session, row)
        self.assertEqual(prices.get(self.instrument).ts_event, self.now[0])
        self.assertIsNone(feed.conversion_failure)

    def test_dce_trading_date_policy_matches_live_night_rows_without_masking_stale_data(self):
        import numpy as np
        from datetime import datetime
        from zoneinfo import ZoneInfo
        cases = (
            # enabled, venue, receive time, ActionDay/TradingDay, clock, expected natural timestamp, valid
            (False, 'DCE', '2026-11-25T21:55:57.934', '2026-11-26', '21:55:57', '2026-11-26T21:55:57.934', False),
            (True, 'DCE', '2026-11-25T21:55:57.934', '2026-11-26', '21:55:57', '2026-11-25T21:55:57.934', True),
            (True, 'SHFE', '2026-11-25T21:55:57.934', '2026-11-26', '21:55:57', '2026-11-26T21:55:57.934', False),
            (True, 'DCE', '2026-11-25T21:55:57.934', '2026-11-24', '21:55:57', '2026-11-24T21:55:57.934', False),
            (True, 'DCE', '2026-11-25T21:55:57.934', '2026-11-26', '21:54:00', '2026-11-25T21:54:00.934', False),
            (True, 'DCE', '2026-11-26T00:00:00.100', '2026-11-26', '23:59:59', '2026-11-25T23:59:59.934', True),
        )
        zone = ZoneInfo('Asia/Shanghai')
        def ns(value):
            point = datetime.fromisoformat(value).replace(tzinfo=zone)
            return int(point.timestamp()) * 10**9 + point.microsecond * 1000
        for enabled, venue, received, declared, clock, expected, valid in cases:
            with self.subTest(enabled=enabled, venue=venue, received=received, declared=declared, clock=clock):
                self.now[0] = ns(received)
                symbol = 'p2701' if venue == 'DCE' else 'rb2701'
                self.instrument = market_base.InstrumentId.from_str(f'{symbol}.{venue}')
                self.meta = market_base.InstrumentMeta(self.instrument, price_precision=0,
                    price_increment=D(1), exchange=venue)
                columns = ('InstrumentID', 'TradingDay', 'ActionDay', 'UpdateTime', 'UpdateMillisec',
                    'LastPrice', 'BidPrice1', 'AskPrice1', 'Volume', 'BidVolume1', 'AskVolume1')
                feed, session = self.make_feed(columns, table='ctp_market', dce_use_receive_date=enabled)
                prices = LivePriceStore()
                prices.enable_tick_quotes((self.instrument,), max_age_ns=10_000_000_000)
                feed.register_tick_snapshot_handler(prices.on_tick_snapshot)
                row = dict(InstrumentID=symbol, TradingDay=np.datetime64(declared, 'D'),
                    ActionDay=np.datetime64(declared, 'D'),
                    UpdateTime=np.datetime64(f'1970-01-01T{clock}', 's'), UpdateMillisec=np.int32(934),
                    LastPrice=8303, BidPrice1=8303, AskPrice1=8304, Volume=100, BidVolume1=2, AskVolume1=3)
                feed.connect()
                self.publish(session, row)
                self.assertEqual(prices.get_quote(self.instrument).ts_event, ns(expected))
                self.assertEqual(prices.get(self.instrument) is not None, valid)
                self.assertEqual(str(row['TradingDay']), declared)
                self.assertIsNone(feed.conversion_failure)
                feed.disconnect()

    def test_tick_factory_reuses_exact_bar_connection_without_reading_another_environment(self):
        from bomber.framework.market.sources import create_dolphin_tick_feed
        config = DolphinDbConfig('bar-host', 8849, 'bar-user', 'fixture', streaming_port=8899,
            streams=(DolphinDbStreamSpec.bar('bars', auto_schema=True),))
        with patch.dict('os.environ', {'DDB_MARKET_HOST': 'other-host'}, clear=True):
            feed = create_dolphin_tick_feed(('shfe_ticks', 'dce_ticks'), 'execution-quotes',
                market_config=config, max_age_seconds=10, dce_use_receive_date=True)
        self.assertEqual((feed.config.host, feed.config.port, feed.config.username, feed.config.password,
            feed.config.streaming_port), ('bar-host', 8849, 'bar-user', 'fixture', 8899))
        self.assertEqual([stream.table_name for stream in feed.config.streams], ['shfe_ticks', 'dce_ticks'])
        self.assertTrue(all(stream.auto_schema and not stream.columns for stream in feed.config.streams))
        self.assertTrue(all(stream.dce_use_receive_date for stream in feed.config.streams))
