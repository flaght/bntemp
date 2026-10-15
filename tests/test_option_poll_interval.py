"""运行频率覆盖不改变恢复身份、缓存或目标；真实缓存/原08装配，网络为夹具。"""
from copy import deepcopy
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS
from tests import test_index_query_feed as query_fixture
from tests import test_option_vega_assembly as option_fixture
from bomber.framework.datahub.features import FeatureError
from scripts.integration.options.shared import configure_index_polling, main


class PollIntervalTests(unittest.TestCase):
    setUp = query_fixture.QueryFeedTests.setUp

    def test_one_second_throttles_actual_queries_without_resetting_cache(self):
        self.feed.connect()
        cache = self.feed.cache
        previous_records = cache.manifest['cached_records']
        before = len(self.source.scripts)
        last = cache.last_query_ns
        cache.set_poll_seconds(1)
        self.now = last+999_999_999
        cache.poll()
        self.assertEqual(len(self.source.scripts), before)
        self.now = last+1_000_000_000
        cache.poll()
        self.assertGreater(len(self.source.scripts), before)
        self.assertEqual(cache.manifest['poll_seconds'], 1)
        self.assertEqual(cache.manifest['cached_records'], previous_records)
        self.assertIsNone(cache.failure)

    def test_invalid_interval_leaves_original_frequency(self):
        cache = self.feed.cache
        for value in (0, -1, 31, float('inf'), float('nan'), True):
            with self.assertRaises(FeatureError):
                cache.set_poll_seconds(value)
            self.assertEqual(cache.poll_ns, 200_000_000)

    def test_command_accepts_one_second_offline_without_opening_state(self):
        with patch('builtins.print'):
            main(['--state-file', '/tmp/vega-offline-not-created.json', '--index-poll-seconds', '1'])


class OptionPollingTests(unittest.TestCase):
    setUp = option_fixture.AssemblyTests.setUp
    build = option_fixture.AssemblyTests.build
    start = option_fixture.AssemblyTests.start
    bars = option_fixture.AssemblyTests.bars

    def test_runtime_override_preserves_saved_identity_and_real_frame_decision(self):
        session, lifecycle = self.build(dfs_index=True)
        identity = deepcopy(session.session_identity)
        cache = session.runner.feature_query_feeds[0].cache
        host = NS(strategies={'option-vega-live': NS(session=session)})
        with patch('builtins.print'):
            record = configure_index_polling(host, 1)
        self.assertEqual(cache.poll_ns, 1_000_000_000)
        self.assertEqual(session.session_identity, identity)
        self.assertEqual(record[0]['saved_poll_seconds'], .2)
        self.assertEqual(record[0]['effective_poll_seconds'], 1)
        self.start(session, lifecycle)
        self.bars(session, '2026-08-13 13:58', omit=('000852',), flush=False)
        session.runner.poll_features()
        self.now += 2_000_000_000
        session.runner.flush_frames()
        self.assertEqual(session.runner.frame_audit[-1]['status'], 'CONSUMED')
        self.assertTrue(session.strategy.entry_audit)
        self.assertEqual(session.session_identity, identity)
        self.assertEqual(session.runner.feature_query_feeds[0].manifest['poll_seconds'], 1)

    def test_omitting_override_preserves_saved_frequency(self):
        session, _ = self.build(dfs_index=True)
        host = NS(strategies={'option-vega-live': NS(session=session)})
        self.assertEqual(configure_index_polling(host, None), [])
        self.assertEqual(session.runner.feature_query_feeds[0].cache.poll_ns, 200_000_000)


if __name__ == '__main__':
    unittest.main()
