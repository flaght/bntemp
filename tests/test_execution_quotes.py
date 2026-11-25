"""Tick/Bar执行基准、逐次跳价及同代检查点；不连接真实柜台。"""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest

from tests import test_nt_algo_ctp as fixture


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
        service.prices.on_depth_market_data(self.instrument, self.now,
            {'BidPrice1': bid, 'AskPrice1': ask, 'LastPrice': last})

    def test_bar_is_used_without_tick_configuration(self):
        _, service, broker, (port, _), _ = self.make_account()
        self.quote(service, bid=2900, ask=3100)  # 未配置的Tick不会改变执行基准。
        self.submit(port, 1)
        self.assertEqual(broker.sent[-1]['LimitPrice'], 3000)

    def check_three_prices(self, target, expected):
        _, service, broker, (port, _), runtime = self.make_account()
        service.prices.enable_tick_quotes((self.instrument,))
        service.client.planner.planner._offset_ticks = 1
        self.quote(service)
        self.submit(port, target)
        for _ in range(2):
            # 新Tick和Bar大幅变化也不重置步进基准。
            self.now += 1_000_000_000
            self.quote(service, bid=3100, ask=3102, last=3101)
            self.tick(service, runtime)
            service.poll()
        self.assertEqual([row['LimitPrice'] for row in broker.sent], expected)
        broker.fill(broker.sent[-1]['OrderRef'])
        self.assertFalse(runtime.snapshot_state()['tasks'])

    def test_buy_starts_at_ask_plus_one_and_steps_from_actual_previous_price(self):
        self.check_three_prices(1, [3003, 3004, 3005])

    def test_sell_starts_at_bid_minus_one_and_steps_from_actual_previous_price(self):
        self.check_three_prices(-1, [2997, 2996, 2995])

    def test_zero_retry_step_keeps_last_order_price(self):
        _, service, broker, (port, _), runtime = self.make_account(retry_step_ticks=0)
        self.submit(port, 1)
        self.tick(service, runtime, price=3100)
        service.poll()
        self.assertEqual([row['LimitPrice'] for row in broker.sent], [3000, 3000])

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
        prices.on_depth_market_data(self.instrument, old,
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
        self.tick(service2, runtime2)
        service2.poll()
        self.assertEqual(broker2.sent[-1]['LimitPrice'], 3002)
        self.assertEqual(broker2.sent[-1]['VolumeTotalOriginal'], 1)

    def test_quote_configuration_reuses_existing_ctp_feed(self):
        from demos.direct_ctp import configure_execution_quotes
        _, service, _, _, _ = self.make_account()
        feed = NS(get_instrument_meta=lambda instrument: object(),
            register_depth_handler=Mock(), subscribe=Mock())
        host = NS(service=service, strategies={'one': NS(session=NS(upstream=feed)),
                                              'two': NS(session=NS(upstream=feed))})
        configure_execution_quotes(host, {'execution_driver': 'nt_algo_ctp'})
        feed.register_depth_handler.assert_called_once()
        feed.subscribe.assert_called_once()
        self.assertTrue(service.prices.uses_tick_quotes(self.instrument))

    def test_separate_tick_feed_uses_resource_cleanup_without_strategy_callbacks(self):
        from demos.direct_ctp import configure_execution_quotes
        _, service, _, _, _ = self.make_account()
        runner = NS(add_data_feed=Mock())
        feed = NS(register_instrument=Mock(), subscribe=Mock(), register_depth_handler=Mock(),
            connect=Mock(), disconnect=Mock())
        transport = NS(broker_id='9999', investor_id='demo', password='test',
            production_mode=True, client_id='account')
        from bomber.framework.market.basic import base
        with ExitStack() as resources, patch.dict('os.environ', {'CTP_MD_ADDRESS': 'tcp://example.invalid:1'}), \
                patch.object(base, 'InstrumentMeta', lambda instrument, **kwargs: NS(instrument_id=instrument, **kwargs), create=True), \
                patch('bomber.framework.market.sources.create_market_feed', return_value=feed) as factory:
            host = NS(service=service, account=NS(transport=transport), resources=resources,
                strategies={'one': NS(session=NS(upstream=None, runner=runner))})
            configure_execution_quotes(host, {'execution_driver': 'nt_algo_ctp', 'execution_tick_source': 'ctp'})
            factory.assert_called_once()
            runner.add_data_feed.assert_not_called()
            feed.connect.assert_called_once()
            feed.register_depth_handler.assert_called_once()
            self.assertTrue(service.prices.uses_tick_quotes(self.instrument))
        feed.disconnect.assert_called_once()


class ExecutionQuoteConfigTests(unittest.TestCase):
    check_direct = fixture.NTAlgoConfigurationTests.check_direct
    def test_cli_accepts_tick_source_and_zero_or_one_step(self):
        for step in (0, 1):
            result = self.check_direct(['--execution-driver', 'nt_algo_ctp', '--execution-tick-source', 'ctp',
                '--algo-retry-step-ticks', str(step)])
            self.assertEqual(result['execution_tick_source'], 'ctp')
            self.assertEqual(result['nt_algo']['retry_step_ticks'], step)
            self.assertFalse(result['orders_enabled'])

    def test_runtime_rejects_invalid_step(self):
        from bomber.framework.trader.execution.nt_algo import NTAlgoConfig
        for step in (-1, 2, .1, True):
            with self.subTest(step=step), self.assertRaises(ValueError):
                NTAlgoConfig(retry_step_ticks=step)
