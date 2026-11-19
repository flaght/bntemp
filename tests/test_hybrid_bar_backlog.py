"""积压分钟只补模型窗口；真实批次/执行/IPC路径使用柜台替身，无网络。"""
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock
import unittest

from tests import test_hybrid_shared_layout as fixtures


class HybridBacklogTests(unittest.TestCase):
    setUp = fixtures.HybridSharedLayoutTests.setUp
    make_account = fixtures.HybridSharedLayoutTests.make_account
    session = fixtures.HybridSharedLayoutTests.session
    start = fixtures.HybridSharedLayoutTests.start
    bar = fixtures.HybridSharedLayoutTests.bar

    def event(self, session, label, *, source=False):
        point = fixtures.hybrid_fixture.dt(label)
        stamp = int((point if source else point + timedelta(minutes=1)).timestamp())*10**9
        if not source:
            stamp -= 1
        bar = fixtures.hybrid_fixture.base.make_bar(instrument_id=session.references.instrument_id,
            meta=session.references.instrument_meta(), ts_event=stamp, ts_init=self.now,
            open=100, high=100, low=100, close=100, volume=2)
        return fixtures.hybrid_fixture.base.make_custom_bar(bar, dict(open=100, high=100, low=100,
            close=100, volume=2, value=200, open_interest=500, trade_time=point))

    def prices(self):
        for instrument in (self.rb, self.p):
            self.service.prices.update(instrument, 100, self.now)

    def test_actual_minute_queue_warms_backlog_once_expires_position_and_recovers_on_fresh_signal(self):
        rb, palm = self.session('RB'), self.session('P')
        self.start(rb, palm)
        self.bar(palm, '2026-10-13 09:00', -1)
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        palm.client.drain_events()
        self.now = fixtures.hybrid_fixture.ns('2026-10-13 09:07:02')
        self.prices()
        predictor = palm.strategy.predictor
        predictor.on_bar = Mock(return_value=dict(predicted_z=0, signal=0))
        predictor.warm_history = Mock(wraps=predictor.warm_history)
        for label in ('2026-10-13 09:01','2026-10-13 09:02','2026-10-13 09:06'):
            palm.bar_feed._enqueue_bar(self.event(palm, label, source=True))
        # 使用实际DDB/TD就绪检查：接收端已有新行情，模型队列中仍有旧分钟。
        old_check, old_health = palm.runner._session_check, palm.runner.instrument_health
        palm.upstream.health_snapshot = NS(state='ready', last_event_received_ns=self.now,
            last_event_ts=fixtures.hybrid_fixture.ns('2026-10-13 09:06:00'))
        palm.runner._session_check = palm.readiness
        palm.runner.instrument_health = palm.bar_feed
        self.assertTrue(palm.runner.session_ready())
        with self.assertLogs('demos.10_hybrid_transformer.run_live', level='WARNING') as logs:
            palm.bar_feed.poll()
        palm.runner._session_check, palm.runner.instrument_health = old_check, old_health
        text = '\n'.join(logs.output)
        self.assertIn('bar_time=', text)
        self.assertIn('now=', text)
        self.assertIn('delay_seconds=', text)
        self.assertIn('实时分钟恢复', text)
        self.assertEqual(predictor.warm_history.call_count, 2)
        self.assertEqual(predictor.on_bar.call_count, 1)
        self.assertEqual(len(self.transport.sent), 2)  # 原开仓和到期平仓，没有积压信号订单。
        closing = self.transport.sent[-1]
        self.assertNotEqual(closing['CombOffsetFlag'], '0')
        self.assertEqual(closing['VolumeTotalOriginal'], 2)
        self.assertFalse(palm.strategy.holdings.active)
        self.assertIsNone(palm.runner.failure)
        self.assertIsNone(palm.bar_feed.failure)
        self.assertFalse(palm.runner.reduce_only)
        self.transport.fill(closing['OrderRef'])
        palm.client.drain_events()
        self.assertEqual(self.service.position_manager.position('hybrid-p', 'p_main'), 0)
        self.assertEqual(palm.strategy.fills_received, 2)
        # 重复旧分钟不能再次更新历史，也不能把恢复后的策略重新切成仅减仓。
        event = self.event(palm, '2026-10-13 09:02')
        palm.runner.publish('model-bars', event.bar)
        palm.runner.publish('model-bars', event)
        self.assertEqual(predictor.warm_history.call_count, 2)
        self.assertFalse(palm.runner.reduce_only)
        self.bar(palm, '2026-10-13 09:08', -1)
        self.assertEqual(self.transport.sent[-1]['CombOffsetFlag'], '0')
        self.assertEqual(self.transport.sent[-1]['VolumeTotalOriginal'], 2)
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        palm.client.drain_events()

    def test_backlog_cancels_pending_open_and_does_not_continue_old_open_target(self):
        rb, palm = self.session('RB'), self.session('P')
        self.start(rb, palm)
        self.bar(palm, '2026-10-13 09:01', -.5)
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        palm.client.drain_events()
        self.bar(palm, '2026-10-13 09:02', -1)
        pending = self.transport.sent[-1]['OrderRef']
        self.now = fixtures.hybrid_fixture.ns('2026-10-13 09:06:30')
        self.prices()
        palm.runner.publish('model-bars', self.event(palm, '2026-10-13 09:03'))
        self.assertTrue(palm.client.reduce_only_check())
        self.service.poll()
        palm.client.drain_events()
        self.assertNotIn(pending, self.transport.active)
        count = len(self.transport.sent)
        palm.runner.continue_execution_target('hybrid-p', 'p_main', self.now, trigger_instrument_id=self.p)
        self.assertEqual(len(self.transport.sent), count)
        self.assertEqual(self.service.position_manager.position('hybrid-p', 'p_main'), -1)
        self.now = fixtures.hybrid_fixture.ns('2026-10-13 09:09:02')
        self.prices()
        palm.runner.expire_due()
        closing = self.transport.sent[-1]
        self.assertNotEqual(closing['CombOffsetFlag'], '0')
        self.assertEqual(closing['VolumeTotalOriginal'], 1)
        self.transport.fill(closing['OrderRef'])
        palm.client.drain_events()
        self.assertEqual(self.service.position_manager.position('hybrid-p', 'p_main'), 0)
        self.assertIsNone(palm.runner.failure)

    def test_inference_that_becomes_stale_cannot_open_or_renew_and_original_expiry_still_closes(self):
        rb, palm = self.session('RB'), self.session('P')
        self.start(rb, palm)
        self.bar(palm, '2026-10-13 09:00', -1)
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        palm.client.drain_events()
        signal_count = len(palm.strategy.signal_records)
        self.now = fixtures.hybrid_fixture.ns('2026-10-13 09:05:02')
        self.prices()
        event = self.event(palm, '2026-10-13 09:04')
        def slow_predict(row):
            palm.strategy.predictor.workflow.history.append(row)
            self.now += 150_000_000_000
            self.prices()
            return dict(predicted_z=-.1, signal=-1)
        palm.strategy.predictor.on_bar = slow_predict
        palm.runner.publish('model-bars', event)
        self.assertTrue(palm.runner.reduce_only)
        self.assertIsNone(palm.strategy.last_prediction)
        self.assertTrue(all(row['signal'] is None for row in palm.strategy.signal_records[signal_count:]))
        self.assertFalse(palm.strategy.holdings.active)
        self.assertEqual(palm.strategy.last_processed_ns, event.ts_event)
        self.assertEqual(len(self.transport.sent), 2)
        self.assertNotEqual(self.transport.sent[-1]['CombOffsetFlag'], '0')
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        palm.client.drain_events()
        self.assertIsNone(palm.runner.failure)

    def test_future_bar_still_fails_with_explicit_timestamps(self):
        palm = self.session('P')
        self.start(palm)
        self.now = fixtures.hybrid_fixture.ns('2026-10-13 09:04:02')
        with self.assertRaisesRegex(RuntimeError, 'bar_time=.*now=.*delay_seconds=-'):
            palm.runner.publish('model-bars', self.event(palm, '2026-10-13 09:05'))
        self.assertIsNotNone(palm.runner.failure)
        self.assertFalse(palm.strategy.predictor.workflow.history)
        self.assertFalse(self.transport.sent)

    def test_backlog_from_other_trading_day_is_not_silently_loaded(self):
        palm = self.session('P')
        self.start(palm)
        with self.assertRaisesRegex(RuntimeError, '不属于当前交易日'):
            palm.runner.publish('model-bars', self.event(palm, '2026-10-12 09:01'))
        self.assertIsNotNone(palm.runner.failure)
        self.assertFalse(palm.strategy.predictor.workflow.history)
        self.assertFalse(self.transport.sent)

    def test_bad_ohlc_amount_is_not_hidden_by_history_only_processing(self):
        palm = self.session('P')
        self.start(palm)
        self.now = fixtures.hybrid_fixture.ns('2026-10-13 09:06:02')
        self.prices()
        event = self.event(palm, '2026-10-13 09:02')
        event.factors['value'] = 1
        with self.assertRaises(ValueError):
            palm.runner.publish('model-bars', event)
        self.assertIsNotNone(palm.runner.failure)
        self.assertFalse(palm.strategy.predictor.workflow.history)
        self.assertFalse(self.transport.sent)

    def test_ipc_backlog_delivers_fills_keeps_close_order_and_resumes_new_signals(self):
        from bomber.framework.trader.execution.ipc import AccountExecutionProtocol
        from bomber.framework.trader.execution.ipc_client import RemoteAccountService, RemoteTraderSession
        _, server_p = self.session('RB'), self.session('P')
        protocol = AccountExecutionProtocol(self.service,
            keys={'hybrid-rb':'rb-key','hybrid-p':'p-key'}, trading_day='20261013')
        self.account.start(resume=False)
        self.service.start()
        self.account.authorize()
        self.addCleanup(self.account.driver.stop)
        class Connection:
            sid='hybrid-p'
            lease=None
            sequence=0
            def call(connection, action, data):
                connection.sequence += 1
                result = protocol.handle(dict(version=1, strategy_id=connection.sid, key='p-key',
                    lease=connection.lease, request_id=f'backlog-{connection.sequence}', action=action, data=data))
                if action == 'attach':connection.lease=result['lease']
                return result
        remote = RemoteAccountService(Connection(), server_p.client.configuration(), protocol.state('hybrid-p'))
        palm = self.session('P', service=remote, driver=RemoteTraderSession(remote, '20261013'))
        palm.client.ready_check=lambda:True
        palm.runner.start()
        palm.runner.accept_bars=True
        self.addCleanup(palm.runner.stop)
        self.bar(palm, '2026-10-13 09:00', -1)
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        palm.client.drain_events()
        self.now = fixtures.hybrid_fixture.ns('2026-10-13 09:07:02')
        self.prices()
        remote.prices.update(self.p, 100, self.now)
        palm.runner.publish('model-bars', self.event(palm, '2026-10-13 09:01'))
        self.assertTrue(protocol.leases['hybrid-p']['reduce_only'])
        closing=self.transport.sent[-1]['OrderRef']
        self.assertNotEqual(self.transport.sent[-1]['CombOffsetFlag'], '0')
        self.service.poll()
        self.assertIn(closing, self.transport.active)
        self.transport.fill(closing)
        palm.client.drain_events()
        self.assertEqual(palm.client.position_manager.position('hybrid-p', 'p_main'), 0)
        self.assertEqual(palm.strategy.fills_received, 2)
        self.bar(palm, '2026-10-13 09:08', -1)
        self.assertFalse(protocol.leases['hybrid-p']['reduce_only'])
        self.assertEqual(self.transport.sent[-1]['CombOffsetFlag'], '0')
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        palm.client.drain_events()
        self.assertFalse(self.service._failed)
        self.assertIsNone(palm.runner.failure)


if __name__ == '__main__':unittest.main()
