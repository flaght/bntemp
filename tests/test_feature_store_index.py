"""特征缓存索引与完整扫描语义一致；增量发布不再遍历全部历史。"""
from random import Random
import unittest

from bomber.framework.datahub.features import Alignment, FeatureError, FeatureHub, FeatureQuery, FeatureUnavailable
from bomber.framework.datahub.features.store import FeatureStore
from tests.test_feature_datahub import registry, requirement, row, M


class ScanStore(FeatureStore):
    """保留原查询的全量捕获作为对照，不使用被测索引选择候选。"""
    def capture_for(self, requirements, query):
        return self.capture_with_health()


class FeatureStoreIndexTests(unittest.TestCase):
    def test_exact_window_and_asof_match_full_scan_for_revisions_visibility_and_quality(self):
        reg = registry()
        indexed, scanned = FeatureHub(reg), FeatureHub(reg, ScanStore(reg))
        random = Random(27)
        records = []
        for event in range(1, 101):
            for revision in range(3):
                records.append(row(event*M, available=(event+revision)*M, revision=revision,
                    score=float(revision), received_ns=(event+revision+random.randrange(3))*M,
                    quality_flags=int(revision == 2 and event % 9 == 0)))
        random.shuffle(records)
        for hub in (indexed, scanned):
            hub.store.publish(records)
        for mode in Alignment:
            for event in range(1, 106):
                query = FeatureQuery(event*M, (event+random.randrange(5))*M)
                demand = requirement(mode, **({'window_ns': 4*M} if mode is Alignment.WINDOW else {}))
                values = []
                for hub in (indexed, scanned):
                    try:
                        snapshot = hub.snapshot((demand,), query)
                        values.append(('READY', snapshot.values, snapshot.generation))
                    except (FeatureError, FeatureUnavailable) as error:
                        values.append((type(error), str(error)))
                self.assertEqual(values[0], values[1], (mode, query))

    def test_incremental_publish_and_exact_query_never_scan_whole_row_dictionary(self):
        store = FeatureStore(registry())
        store.publish(row(event*M) for event in range(1, 10001))
        class NoScan(dict):
            def values(self):
                raise AssertionError('不能扫描全部历史记录')
            def __iter__(self):
                raise AssertionError('不能复制全部历史记录')
        store._rows = NoScan(store._rows)
        store.publish((row(10001*M),))
        hub = FeatureHub(store.registry, store)
        self.assertEqual(hub.snapshot((requirement(),), FeatureQuery(M, 10001*M)).latest('signal').ts_event, M)
        self.assertEqual(len(store.capture_for((requirement(),), FeatureQuery(M, 10001*M))[1]), 1)

    def test_failed_record_or_health_batch_leaves_both_indexes_and_generation_unchanged(self):
        store = FeatureStore(registry())
        store.publish((row(),))
        before = store.capture_with_health()
        for records, health in (((row(2*M), row(score=9.0)), ()),
                               ((row(2*M),), ((('minute.signal', 1, 'IM'), 4*M, 3*M, None),))):
            with self.assertRaises(FeatureError):
                store.publish(records, health_updates=health)
            self.assertEqual(store.capture_with_health(), before)
            with self.assertRaises(FeatureUnavailable):
                FeatureHub(store.registry, store).snapshot((requirement(),), FeatureQuery(2*M, 3*M))
        store.publish((row(2*M),))
        self.assertEqual(FeatureHub(store.registry, store).snapshot(
            (requirement(),), FeatureQuery(2*M, 3*M)).latest('signal').ts_event, 2*M)

    def test_failed_generator_does_not_publish_partial_rows(self):
        store = FeatureStore(registry())
        def broken():
            yield row()
            raise RuntimeError('interrupted input')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            store.publish(broken())
        self.assertEqual(store.capture(), (0, ()))
        self.assertEqual(store.capture_for((requirement(),), FeatureQuery(M, M))[1], ())

    def test_health_and_duplicate_first_receipt_match_original_semantics(self):
        reg = registry()
        stores = (FeatureStore(reg), ScanStore(reg))
        for store in stores:
            store.publish((row(received_ns=2*M),), health_updates=((('minute.signal', 1, 'IM'), M, 3*M, None),))
            store.publish((row(received_ns=4*M),))
            store.publish((), health_updates=((('minute.signal', 1, 'IM'), 3*M, 4*M, 'SOURCE_ERROR'),))
        for now in (M, 2*M, 3*M, 5*M):
            results = []
            for store in stores:
                try:
                    results.append(FeatureHub(reg, store).snapshot((requirement(),), FeatureQuery(M, now)).latest('signal'))
                except FeatureUnavailable as error:
                    results.append(error.code)
            self.assertEqual(results[0], results[1])
        self.assertEqual(stores[0].capture()[1][0].received_ns, 2*M)


if __name__ == '__main__':
    unittest.main()
