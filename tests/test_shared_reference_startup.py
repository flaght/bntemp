"""慢启动后先刷新参考再核验；真实过期检查/宿主/执行账本，仅资料读取和柜台用夹具。"""
from dataclasses import replace
from contextlib import ExitStack
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest

from tests import test_direct_ctp  # 无原生SDK的离线命名空间。
from tests import test_shared_account as fixture


class ReferenceStartupTests(unittest.TestCase):
    setUp = fixture.SharedAccountTests.setUp
    native_modules = fixture.SharedAccountTests.native_modules
    native_account = fixture.SharedAccountTests.native_account
    callback_host = fixture.SharedAccountTests.callback_host
    client = fixture.SharedAccountTests.client
    request = fixture.SharedAccountTests.request

    def references(self, clock):
        module = import_module('bomber.framework.dataprep.live_options')
        service = module.LiveOptionReferences.__new__(module.LiveOptionReferences)
        service.clock_ns = lambda: clock[0]
        service.max_observation_ns = 30_000_000_000
        service.failure = None
        service._snapshot = module.OptionReferenceSnapshot({}, {}, 'fixed-version', clock[0])
        def refresh():
            # 替代数据库成功读回相同条款；snapshot()仍使用真实30秒过期校验。
            service._snapshot = replace(service._snapshot, observed_at_ns=clock[0])
        refs = NS(spec='fixed', refresh=Mock(side_effect=refresh))
        return service, refs

    def test_slow_feed_start_refreshes_expired_snapshot_before_first_ready_check(self):
        clock = [0]
        reference, refs = self.references(clock)
        with TemporaryDirectory() as directory, ExitStack() as resources:
            def prepare(host, broker, rows):
                runner = rows[0].session.runner
                rows[0].session.references = refs
                start = runner.start
                def slow_start():
                    start()
                    clock[0] += 37_000_000_000
                runner.start = slow_start
                runner.session_ready = lambda: reference.snapshot().fingerprint == 'fixed-version'
            host, broker, _ = self.callback_host(Path(directory), prepare=prepare)
            resources.callback(host.stop)
            self.assertTrue(host._prepared)
            self.assertTrue(host.account.client.is_armed)
            self.assertGreater(refs.refresh.call_count, 0)
            self.assertEqual(reference.max_observation_ns, 30_000_000_000)
            self.assertFalse(broker.sent)
            clock[0] += 31_000_000_000
            with self.assertRaisesRegex(RuntimeError, '观测过期'):
                reference.snapshot()

    def test_slow_account_preparation_poll_refreshes_before_next_ready_check(self):
        clock, market_ready = [0], [False]
        reference, refs = self.references(clock)
        with TemporaryDirectory() as directory, ExitStack() as resources:
            def prepare(host, broker, rows):
                rows[0].session.references = refs
                rows[0].session.runner.session_ready = lambda: bool(reference.snapshot()) and market_ready[0]
                poll = host.account.poll
                def slow_poll():
                    poll()
                    clock[0] += 37_000_000_000
                    market_ready[0] = True
                host.account.poll = slow_poll
            host, broker, _ = self.callback_host(Path(directory), prepare=prepare)
            resources.callback(host.stop)
            self.assertTrue(host._prepared)
            self.assertEqual(reference.snapshot().observed_at_ns, clock[0])
            self.assertFalse(broker.sent)

    def test_failed_refresh_or_changed_reference_never_authorizes_or_saves_signal(self):
        for message in ('参考资料查询失败', '期权参考资料版本变化，停止并重新装配'):
            with self.subTest(message=message), TemporaryDirectory() as directory:
                root, captured = Path(directory), {}
                def prepare(host, broker, rows):
                    captured.update(host=host, broker=broker)
                    rows[0].session.references.refresh = Mock(side_effect=RuntimeError(message))
                    host.account.authorize = Mock(wraps=host.account.authorize)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.callback_host(root, prepare=prepare)
                host = captured['host']
                self.assertFalse(host._prepared)
                host.account.authorize.assert_not_called()
                self.assertFalse(captured['broker'].sent)
                self.assertFalse((root/'one.json').exists())
                self.assertFalse((root/'two.json').exists())

    def test_slow_resume_keeps_existing_position_signal_revision_and_order_count(self):
        clock = [0]
        reference, refs = self.references(clock)
        with TemporaryDirectory() as directory, ExitStack() as resources:
            root = Path(directory)
            first, broker, rows = self.callback_host(root)
            resources.callback(first.stop)
            rows[0].session.strategy.set_target('main', 1, self.now)
            broker.fill(broker.sent[-1]['OrderRef'])
            first.poll()
            first.stop()
            budget = dict(first.account.driver.order_budget)
            revision = rows[0].session.strategy._revision
            def prepare(host, restored_broker, restored_rows):
                host.resume = True
                restored_broker.gross = dict(broker.gross)
                restored_rows[0].session.references = refs
                runner = restored_rows[0].session.runner
                start = runner.start
                def slow_start():
                    start()
                    clock[0] += 37_000_000_000
                runner.start = slow_start
                runner.session_ready = lambda: bool(reference.snapshot())
            restored, restored_broker, restored_rows = self.callback_host(root, prepare=prepare)
            resources.callback(restored.stop)
            self.assertTrue(restored._prepared)
            self.assertEqual(restored.service.position_manager.position('one', 'main'), 1)
            self.assertEqual(restored_rows[0].session.strategy._revision, revision)
            self.assertEqual(restored.account.driver.order_budget, budget)
            self.assertFalse(restored_broker.sent)


if __name__ == '__main__':
    unittest.main()
