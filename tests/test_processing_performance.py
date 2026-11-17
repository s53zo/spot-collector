"""Index ownership and expiry contracts, without timing-dependent test oracles."""
import unittest

from spot_processing import SpotProcessor
from test_spot_processing import START, line, gc


class ProcessingStateTests(unittest.TestCase):
    def assert_index_ownership(self, engine):
        for store in (engine.references, engine.locals, engine.samples,
                      engine.repeats, engine.local_origins, engine.latest):
            self.assertLessEqual(len(store), engine.config['max_entries'])
            self.assertEqual(set(store.versions), set(store))
            self.assertLessEqual(len(store.expiry), 2 * store.limit + 64)
            for name, grouper in store.groupers.items():
                expected = {}
                for key, value in store.items():
                    expected.setdefault(grouper(key, value), []).append(key)
                actual = {group: list(keys) for group, keys in store.indexes[name].items()}
                self.assertEqual(actual, expected)

    def test_indexes_expire_evict_reset_and_clear_with_values(self):
        now = [START]
        engine = SpotProcessor({'max_entries': 7}, clock=lambda: now[0])
        for index in range(40):
            call = 'JA%dABC' % index
            now[0] += 1
            engine.process('l', 'local', line(call, 14010 + index))
            engine.process('gc', 'gocluster', gc(call, 14010 + index))
            self.assert_index_ownership(engine)
        engine.reset_source('gc')
        self.assertFalse(engine.references)
        self.assertFalse(engine.samples)
        self.assert_index_ownership(engine)
        engine.repeats.clear()
        self.assert_index_ownership(engine)
        now[0] += 2000
        engine.snapshot()
        self.assert_index_ownership(engine)
        for store in (engine.references, engine.locals, engine.samples,
                      engine.repeats, engine.local_origins, engine.latest):
            self.assertFalse(store)
            self.assertTrue(all(not index for index in store.indexes.values()))

    def test_hot_key_heap_metadata_remains_bounded(self):
        engine = SpotProcessor({'max_entries': 3}, clock=lambda: START)
        for _ in range(300):
            engine.process('l', 'local', line(frequency=14020.12))
            engine.process('gc', 'gocluster', gc())
            engine.process('anchor', 'cluster', line(origin='C1CCC'))
        self.assert_index_ownership(engine)
        self.assertEqual(len(engine.references), 1)
        self.assertEqual(len(engine.samples), 1)

    def test_rekeyed_unknown_mode_keeps_original_expiry(self):
        now = [START]
        engine = SpotProcessor(clock=lambda: now[0])
        human = line(mode='', report='', extra=b'heard CQ')
        self.assertEqual(engine.process('p', 'cluster', human), [human])
        now[0] += 179
        self.assertFalse(engine.process('l', 'local', line()))
        self.assert_index_ownership(engine)
        now[0] += 1
        self.assertEqual(engine.process('l', 'local', line()), [line()])
        self.assert_index_ownership(engine)

    def test_backward_clock_removes_future_evidence_and_indexes(self):
        now = [START]
        engine = SpotProcessor(clock=lambda: now[0])
        engine.process('l', 'local', line())
        engine.process('gc', 'gocluster', gc())
        now[0] -= 1
        engine.snapshot()
        self.assert_index_ownership(engine)
        self.assertFalse(engine.locals)
        self.assertFalse(engine.references)

    def test_nearby_bins_preserve_population_order_at_boundaries(self):
        now = [START]
        engine = SpotProcessor(clock=lambda: now[0])
        for index, frequency in enumerate((14020.50, 14020.00, 14019.50)):
            engine.process(str(index), 'gocluster', gc('JA%dABC' % index, frequency))
        from spot_processing import parse_spot
        query = parse_spot(line(frequency=14020), START)
        refs = list(engine._nearby(engine.references, query))
        self.assertEqual([obs.spot.call for obs in refs], ['JA0ABC', 'JA1ABC', 'JA2ABC'])
        engine.process('0', 'gocluster', gc('JA0ABC', 14020.50))
        refs = list(engine._nearby(engine.references, query))
        self.assertEqual([obs.spot.call for obs in refs], ['JA1ABC', 'JA2ABC', 'JA0ABC'])
        self.assert_index_ownership(engine)
