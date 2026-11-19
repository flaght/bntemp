"""参考资料短暂失败、原缓存时效和独立减仓链路回归；无网络。"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from importlib import import_module
import unittest

from tests import test_hybrid_shared_layout as fixtures
from bomber.framework.dataprep.live_cache import LiveReferenceCache
from bomber.framework.dataprep.live_cached_role import CachedRoleReferences
from bomber.framework.dataprep.sources.base import ReferenceSourceError, ReferenceDataset, ReferenceQuery
from bomber.framework.datahub.sector_roles import SectorDataUnavailable


class ReferenceRetryTests(unittest.TestCase):
    def test_transient_query_failure_does_not_extend_valid_cache_and_expiry_closes_snapshot(self):
        now = [10_000_000_000]
        assignment = NS(effective_ns=1,available_ns=1)
        cache = LiveReferenceCache(clock_ns=lambda:now[0])
        cache.refresh(lambda:(assignment,'spec',dict(observed_at_ns=now[0],
            freshness_policy={'max_observation_age_seconds':2})))
        def failed(): raise ReferenceSourceError('connection reset')
        now[0] += 1_000_000_000
        self.assertFalse(cache.refresh(failed,transient_errors=(ReferenceSourceError,)))
        self.assertTrue(cache.manifest['ready'])
        self.assertEqual(cache.manifest['observed_at_ns'],10_000_000_000)
        self.assertIs(cache.snapshot(now[0]),assignment)
        now[0] += 2_000_000_000
        self.assertFalse(cache.manifest['ready'])
        with self.assertRaisesRegex(RuntimeError,'过期'):
            cache.snapshot(now[0])
        def corrupt(): raise ValueError('invalid multiplier')
        with self.assertRaisesRegex(ValueError,'invalid multiplier'):
            cache.refresh(corrupt,transient_errors=(ReferenceSourceError,))

    def test_role_retries_on_interval_logs_degradation_and_recovers_without_resetting_expiry(self):
        now = [10_000_000_000]
        source = NS(product='RB',role='main',trading_day=date(2026,11,16),started_ns=1,paths=(),
            factor_date_basis='source',factor_availability='observed-on-read',spec='fixed-spec')
        assignment = NS(effective_ns=1,available_ns=1,instrument=lambda *a:'rb2701',factor=lambda *a:Decimal(1))
        failed = [False]
        calls = []
        def snapshot(stamp):
            calls.append(stamp)
            if failed[0]: raise ReferenceSourceError('fixture timeout')
            source.manifest = dict(factor_date='2026-11-13',observed_at_ns=now[0],
                freshness_policy={'max_observation_age_seconds':2},ready=True)
            return assignment
        source.snapshot=snapshot
        with patch('bomber.framework.dataprep.live_cached_role.time.time_ns',side_effect=lambda:now[0]):
            refs = CachedRoleReferences(source)
            version = refs.version
            now[0] += 1_000_000_000
            failed[0] = True
            with self.assertLogs('bomber.framework.dataprep.live_cached_role',level='WARNING') as logs:
                refs.refresh()
            self.assertIn('fixture timeout','\n'.join(logs.output))
            self.assertFalse(refs.reduce_only)
            self.assertIs(refs.snapshot(now[0]),assignment)
            now[0] += 2_000_000_000
            with self.assertLogs('bomber.framework.dataprep.live_cached_role',level='WARNING'):
                self.assertTrue(refs.reduce_only)
            self.assertEqual(refs.version,version)
            with self.assertRaises(SectorDataUnavailable):refs.snapshot(now[0])
            refs.refresh()
            self.assertEqual(len(calls),2)
            now[0] += 3_000_000_000
            failed[0] = False
            with self.assertLogs('bomber.framework.dataprep.live_cached_role',level='INFO') as recovered:
                refs.refresh()
            self.assertIn('刷新恢复','\n'.join(recovered.output))
            self.assertFalse(refs.reduce_only)
            self.assertTrue(refs.manifest['ready'])


    def test_daily_reference_uses_one_validated_version_across_midnight_and_reloads_for_new_td(self):
        from tests.test_reference_sources import FakeSession
        from bomber.framework.dataprep.sources.dolphindb import DolphinDbReferenceSource, DolphinDbReferenceConfig
        from bomber.framework.dataprep.live_role import SourceRoleReferences
        from bomber.framework.dataprep.reference_freshness import ReferenceFreshnessPolicy
        from zoneinfo import ZoneInfo
        from datetime import datetime
        zone = ZoneInfo('Asia/Shanghai')
        def ns(text):
            return int(datetime.fromisoformat(text).replace(tzinfo=zone).timestamp()) * 10**9
        now = [ns('2026-09-21 21:00:00')]
        deadline = ns('2026-09-22 15:00:00')
        sdk = FakeSession()
        sdk.data['fut_adjustment_factors']['date'] = '2026-09-21'
        source = DolphinDbReferenceSource(DolphinDbReferenceConfig('fixture', 8902, 'user', 'secret'),
            session_factory=lambda **kwargs:sdk)
        source.open()
        self.addCleanup(source.close)
        def provider(day, expected):
            return SourceRoleReferences(source, product='RB', trading_day=day,
                started_ns=now[0], factor_date_basis='source', factor_availability='observed-on-read',
                freshness=ReferenceFreshnessPolicy(expected_source_day=expected), clock_ns=lambda:now[0])
        with patch('time.time_ns', side_effect=lambda:now[0]):
            refs = CachedRoleReferences(provider('20260922', date(2026,9,21)), valid_until_ns=deadline)
            assignment = refs.snapshot(now[0])
            observed = refs.manifest['observed_at_ns']
            calls = len(sdk.calls)
            # 即使数据库断线、源表被更新，盘中固定版本也不重新查询。
            sdk.fail = True
            for point in ('2026-09-21 23:00:00','2026-09-22 00:01:00',
                          '2026-09-22 09:00:00','2026-09-22 14:59:59'):
                now[0] = ns(point)
                refs.refresh()
                self.assertIs(refs.snapshot(now[0]), assignment)
                self.assertFalse(refs.reduce_only)
                self.assertEqual(refs.manifest['observed_at_ns'], observed)
                self.assertEqual(refs.manifest['valid_until_ns'], deadline)
                self.assertEqual(len(sdk.calls), calls)
            with self.assertRaises(SectorDataUnavailable):
                refs.snapshot(assignment.available_ns - 1)
            now[0] = deadline
            with self.assertLogs('bomber.framework.dataprep.live_cached_role', level='WARNING'):
                refs.refresh()
            self.assertTrue(refs.reduce_only)
            with self.assertRaisesRegex(SectorDataUnavailable, '本交易日'):
                refs.snapshot(now[0])
            self.assertEqual(len(sdk.calls), calls)
            # 新交易日必须重新读取，且继续核对声明的资料来源日。
            sdk.fail = False
            sdk.data['fut_contract']['date'] = '2026-09-22'
            sdk.data['fut_adjustment_factors']['date'] = '2026-09-22'
            now[0] = ns('2026-09-22 21:00:00')
            next_refs = CachedRoleReferences(provider('20260923', date(2026,9,22)),
                valid_until_ns=ns('2026-09-23 15:00:00'))
            self.assertGreater(len(sdk.calls), calls)
            self.assertEqual(next_refs.snapshot(now[0]).trading_day, date(2026,9,23))
            self.assertEqual(next_refs.snapshot(now[0]).source_day, date(2026,9,22))
            with self.assertRaisesRegex(SectorDataUnavailable, '来源日不符'):
                provider('20260923', date(2026,9,21))

    def test_daily_reference_does_not_bypass_clock_rollback_or_initial_load_error(self):
        now = [10_000_000_000]
        cache = LiveReferenceCache(clock_ns=lambda:now[0])
        assignment = NS(effective_ns=1, available_ns=1)
        cache.refresh(lambda:(assignment, 'spec', dict(observed_at_ns=now[0], valid_until_ns=100_000_000_000)))
        now[0] += 40_000_000_000
        self.assertIs(cache.snapshot(now[0]), assignment)
        now[0] -= 1_000_000_000
        with self.assertRaisesRegex(RuntimeError, '时钟回退'):
            cache.snapshot(now[0])
        source = NS(product='RB', role='main', trading_day=date(2026,11,16), started_ns=1, paths=(),
            factor_date_basis='source', factor_availability='observed-on-read',
            snapshot=Mock(side_effect=ReferenceSourceError('initial query failed')))
        with self.assertRaisesRegex(ReferenceSourceError, 'initial query failed'):
            CachedRoleReferences(source, valid_until_ns=100_000_000_000)

    def test_source_reconnects_after_query_failure_and_reports_redacted_original_error(self):
        module=import_module('bomber.framework.dataprep.sources.dolphindb')
        first, second = Mock(), Mock()
        first.connect.return_value = second.connect.return_value = True
        first.run.side_effect = TimeoutError('timeout user password-secret')
        second.run.return_value = []
        factory=Mock(side_effect=[first,second])
        source=module.DolphinDbReferenceSource(module.DolphinDbReferenceConfig(
            host='localhost',port=8902,username='user',password='password-secret'),session_factory=factory)
        source.open()
        with self.assertRaises(ReferenceSourceError) as caught:
            source.read(ReferenceDataset.FUTURES_BASIC,ReferenceQuery(products=('RB',)))
        message=str(caught.exception)
        self.assertIn('TimeoutError',message)
        self.assertIn('fut_basic',message)
        self.assertNotIn('password-secret',message)
        with patch.object(module,'normalize_dolphindb_frame',return_value='recovered'):
            self.assertEqual(source.read(ReferenceDataset.FUTURES_BASIC,ReferenceQuery(products=('RB',))),'recovered')
        first.close.assert_called_once()
        self.assertEqual(factory.call_count,2)
        source.close()


class ReferenceExecutionTests(unittest.TestCase):
    setUp = fixtures.HybridSharedLayoutTests.setUp
    make_account = fixtures.HybridSharedLayoutTests.make_account
    session = fixtures.HybridSharedLayoutTests.session
    start = fixtures.HybridSharedLayoutTests.start
    bar = fixtures.HybridSharedLayoutTests.bar

    def test_expired_reference_cancels_open_only_keeps_host_running_and_closes_at_original_deadline(self):
        rb, palm = self.session('RB'), self.session('P')
        self.start(rb,palm)
        self.bar(palm,'2026-10-13 09:01',.5)
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        self.bar(palm,'2026-10-13 09:02',.5)
        pending = self.transport.sent[-1]['OrderRef']
        original_expiry = [batch.expires_at_ns for batch in palm.strategy.holdings.active]
        self.assertIn(pending,self.transport.active)
        palm.references.reduce_only=True
        original_snapshot = palm.references.snapshot
        def unavailable(stamp):raise SectorDataUnavailable('参考缓存观测过期')
        palm.references.snapshot=unavailable
        self.service.poll()
        self.assertNotIn(pending,self.transport.active)
        self.assertEqual(self.service.position_manager.position('hybrid-p','p_main'),1)
        before=len(self.transport.sent)
        palm.runner.continue_execution_target('hybrid-p','p_main',self.now,trigger_instrument_id=self.p)
        self.assertEqual(len(self.transport.sent),before)
        # 其他品种发布目标也不得让P的旧目标重新补开。
        self.bar(rb,'2026-10-13 09:03',1)
        self.assertEqual(len(self.transport.sent),before+1)
        self.assertEqual(self.transport.sent[-1]['InstrumentID'],'rb2701')
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        self.assertEqual([b.expires_at_ns for b in palm.strategy.holdings.active],original_expiry)
        self.now=max(original_expiry)+2_000_000_000
        self.service.prices.update(self.p,100,self.now)
        self.service.prices.update(self.rb,100,self.now)
        palm.runner.expire_due()
        close=self.transport.sent[-1]
        self.assertEqual(close['InstrumentID'],'p2701')
        self.assertEqual(close['Direction'],'1')
        self.assertEqual(close['VolumeTotalOriginal'],1)
        self.assertNotEqual(close['CombOffsetFlag'],'0')
        self.transport.fill(close['OrderRef'])
        palm.client.drain_events()
        self.assertEqual(self.service.position_manager.position('hybrid-p','p_main'),0)
        self.assertIsNone(palm.runner.failure)
        self.assertTrue(palm.runner.accept_bars)
        self.assertFalse(self.service._failed)
        palm.references.reduce_only=False
        palm.references.snapshot=original_snapshot
        self.bar(palm,'2026-10-13 09:09',1)
        self.assertEqual(self.transport.sent[-1]['CombOffsetFlag'],'0')
        self.assertEqual(self.transport.sent[-1]['VolumeTotalOriginal'],2)


    def test_ipc_reduce_only_keeps_pending_close_and_delivers_fill_before_recovery(self):
        from bomber.framework.trader.execution.ipc import AccountExecutionProtocol
        from bomber.framework.trader.execution.ipc_client import RemoteAccountService, RemoteTraderSession
        server_rb, server_p = self.session('RB'), self.session('P')
        protocol = AccountExecutionProtocol(self.service,
            keys={'hybrid-rb': 'rb-key', 'hybrid-p': 'p-key'}, trading_day='20261013')
        self.account.start(resume=False)
        self.service.start()
        self.account.authorize()
        self.addCleanup(self.account.driver.stop)
        class Connection:
            sid = 'hybrid-p'
            lease = None
            sequence = 0
            def call(connection, action, data):
                connection.sequence += 1
                result = protocol.handle(dict(version=1, strategy_id=connection.sid, key='p-key',
                    lease=connection.lease, request_id=f'reduce-{connection.sequence}', action=action, data=data))
                if action == 'attach':
                    connection.lease = result['lease']
                return result
        remote = RemoteAccountService(Connection(), server_p.client.configuration(), protocol.state('hybrid-p'))
        palm = self.session('P', service=remote, driver=RemoteTraderSession(remote, '20261013'))
        palm.client.ready_check = lambda: True
        palm.runner.start()
        palm.runner.accept_bars = True
        self.addCleanup(palm.runner.stop)
        self.bar(palm, '2026-10-13 09:01', 1)
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        palm.client.drain_events()
        self.bar(palm, '2026-10-13 09:06', 0)
        closing = self.transport.sent[-1]['OrderRef']
        self.assertNotEqual(self.transport.sent[-1]['CombOffsetFlag'], '0')
        palm.references.reduce_only = True
        palm.client.drain_events()
        self.assertTrue(protocol.leases['hybrid-p']['reduce_only'])
        self.assertTrue(server_p.client.reduce_only_check())
        self.service.poll()
        self.assertIn(closing, self.transport.active)
        self.transport.fill(closing)
        palm.client.drain_events()
        self.assertEqual(palm.client.position_manager.position('hybrid-p', 'p_main'), 0)
        self.assertIsNone(palm.runner.failure)
        palm.references.reduce_only = False
        self.bar(palm, '2026-10-13 09:07', 1)
        self.assertFalse(protocol.leases['hybrid-p']['reduce_only'])
        self.assertEqual(self.transport.sent[-1]['CombOffsetFlag'], '0')
        self.transport.fill(self.transport.sent[-1]['OrderRef'])
        palm.client.drain_events()
        self.assertFalse(self.service._failed)


if __name__=='__main__':unittest.main()
