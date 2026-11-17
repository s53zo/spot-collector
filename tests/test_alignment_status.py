"""Alignment status reports existing evidence without supplying new authority."""
import copy
import unittest

from spot_processing import SpotProcessor
from test_spot_processing import START, gc, line


class AlignmentStatusTests(unittest.TestCase):
    def setUp(self):
        self.now = START
        self.engine = SpotProcessor(clock=lambda: self.now)

    def train(self, residual=120):
        for index in range(5):
            frequency = 14010 + index * 10
            call = 'JA%dABC' % index
            self.engine.process('l', 'local', line(call, frequency + residual / 1000))
            self.engine.process('gc', 'gocluster', gc(call, frequency))
            self.engine.process('anchor', 'cluster', line(call, frequency, origin='C1CCC'))
            self.now += 10

    def row(self, source='l', band='20M'):
        return next(row for row in self.engine.alignment_snapshot([source])
                    if row['band'] == band)

    def put_samples(self, frequencies, residuals=None, interval=10):
        residuals = residuals or [120] * len(frequencies)
        for index, (frequency, residual) in enumerate(zip(frequencies, residuals)):
            self.engine._put(self.engine.samples, ('l', 'TK0C-1', '20M', 'JA%dABC' % index),
                             (self.now, frequency, residual, 'DL1AAA,C1CCC', 'gc', (('DL1AAA','gc'),('C1CCC','anchor'))))
            self.now += interval

    def test_ready_reports_applied_signed_adjustment_and_zero_is_available(self):
        self.train()
        row = self.row()
        self.assertEqual({key: row[key] for key in ('source','band','state','samples','required','offset_hz','last_match_age_seconds')}, {
            'source': 'l', 'band': '20M', 'state': 'Ready', 'samples': 5,
            'required': 5, 'offset_hz': -120, 'last_match_age_seconds': 10,
        })
        raw = line('JA9XYZ', 14070.12)
        self.assertIn(b'14070.00', self.engine.process('l', 'local', raw)[0])
        self.assertEqual(self.engine.last_evaluation['frequency_basis'], 'alignment')

        self.engine = SpotProcessor(clock=lambda: self.now)
        self.train(residual=0)
        row = self.row()
        self.assertEqual(row['state'], 'Ready')
        self.assertIsNotNone(row['offset_hz'])
        self.assertEqual(row['offset_hz'], 0)

    def test_no_data_and_observed_unmatched_band_are_distinct(self):
        rows = self.engine.alignment_snapshot(['never', 'never'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'No data')
        self.assertIsNone(rows[0]['offset_hz'])
        self.assertEqual(self.engine.alignment_snapshot(), [])
        self.engine.process('l', 'local', line())
        row = self.row()
        self.assertEqual(row['state'], 'Collecting')
        self.assertEqual(row['samples'], 0)
        self.assertIsNone(row['offset_hz'])
        self.assertIsNone(row['last_match_age_seconds'])
        self.now += self.engine.config['pair_window_seconds'] + 1
        self.assertEqual(self.engine.alignment_snapshot(['l'])[0]['state'], 'No data')

    def test_partial_population_counts_distinct_calls_and_ages_without_refresh(self):
        for _ in range(3):
            self.engine.process('l', 'local', line(frequency=14020.12))
            self.engine.process('gc', 'gocluster', gc())
            self.engine.process('anchor', 'cluster', line(origin='C1CCC'))
            self.now += 1
        before = dict(self.engine.samples)
        first = self.row()
        self.now += 4
        second = self.row()
        self.assertEqual(first['state'], 'Collecting')
        self.assertEqual(first['samples'], 1)
        self.assertEqual(second['samples'], 1)
        self.assertEqual(second['last_match_age_seconds'],
                         first['last_match_age_seconds'] + 4)
        self.assertIsNone(second['offset_hz'])
        self.assertEqual(dict(self.engine.samples), before)

    def test_expired_matches_remain_unavailable_until_existing_retention_expires(self):
        self.train()
        self.now += self.engine.config['reference_ttl_seconds'] + 1
        row = self.row()
        self.assertEqual(row['state'], 'Expired')
        self.assertEqual(row['samples'], 0)
        self.assertIsNone(row['offset_hz'])
        self.assertEqual(row['last_match_age_seconds'], 131)
        self.now += self.engine.config['calibration_window_seconds']
        self.assertEqual(self.engine.alignment_snapshot(['l'])[0]['state'], 'No data')

    def test_reference_ttl_boundary_matches_current_offset_eligibility(self):
        self.put_samples([14010000 + index * 10000 for index in range(5)])
        self.now = START + self.engine.config['reference_ttl_seconds']
        self.assertEqual(self.row()['state'], 'Ready')
        self.now += 0.001
        row = self.row()
        self.assertEqual(row['state'], 'Collecting')
        self.assertEqual(row['samples'], 4)
        self.assertIsNone(row['offset_hz'])

    def test_coverage_checks_and_existing_scatter_or_slope_rejection(self):
        self.put_samples([14010000 + index * 10000 for index in range(5)], interval=0)
        self.assertEqual(self.row()['state'], 'Checking')
        self.assertIsNone(self.row()['offset_hz'])

        self.engine.samples.clear()
        self.put_samples([14010000 + index * 100 for index in range(5)])
        self.assertEqual(self.row()['state'], 'Checking')

        self.engine.samples.clear()
        self.put_samples([14010000 + index * 10000 for index in range(5)],
                         [120, 300, 120, 120, 120])
        self.assertEqual(self.row()['state'], 'Unsettled')
        self.assertIsNone(self.row()['offset_hz'])

        self.engine.samples.clear()
        self.engine.config['calibration_max_scatter_hz'] = 1000
        self.put_samples([14010000 + index * 10000 for index in range(5)],
                         [20, 120, 220, 320, 420])
        self.assertEqual(self.row()['state'], 'Unsettled')
        self.assertIsNone(self.row()['offset_hz'])

    def test_self_exclusion_reconnect_and_endpoint_band_independence(self):
        self.train()
        self.engine.process('l', 'local', line('K1OTHER', 7020))
        rows = self.engine.alignment_snapshot(['l', 'other'])
        self.assertEqual([(row['source'], row['band'], row['state']) for row in rows],
                         [('l', '40M', 'Collecting'), ('l', '20M', 'Ready'),
                          ('other', '--', 'No data')])
        self.engine.process('other', 'local',
                            line('K2OTHER', 7020, origin='DL1AAA-1-#'))
        row = self.row()
        self.assertEqual(row['state'], 'Ready')
        self.assertEqual(row['samples'], 5)
        self.engine.reset_source('gc')
        self.assertEqual(self.row()['state'], 'Collecting')
        self.engine.reset_source('l')
        self.assertEqual(self.engine.alignment_snapshot(['l'])[0]['state'], 'No data')

    def test_repeated_snapshots_create_no_retained_state_or_counter_changes(self):
        self.train()
        self.engine.snapshot()
        fields = set(self.engine.__dict__)
        store_names = ('references', 'locals', 'samples', 'repeats', 'local_origins', 'latest')
        before = {}
        for name in store_names:
            store = getattr(self.engine, name)
            before[name] = (copy.deepcopy(dict(store)), copy.deepcopy(store.indexes),
                            dict(store.versions), list(store.expiry), store.serial)
        evaluation = dict(self.engine.last_evaluation)
        counters = self.engine.snapshot()
        for _ in range(30):
            self.engine.alignment_snapshot(['l', 'unseen'])
        self.assertEqual(self.engine.snapshot(), counters)
        self.assertEqual(set(self.engine.__dict__), fields)
        for name in store_names:
            current = getattr(self.engine, name)
            entries, indexes, versions, expiry, serial = before[name]
            self.assertEqual(current, entries)
            self.assertEqual(current.indexes, indexes)
            self.assertEqual(current.versions, versions)
            self.assertEqual(current.expiry, expiry)
            self.assertEqual(current.serial, serial)
        self.assertEqual(self.engine.last_evaluation, evaluation)

    def test_status_follows_existing_eviction_and_backward_clock_cleanup(self):
        self.engine = SpotProcessor({'max_entries': 3}, clock=lambda: self.now)
        for index in range(30):
            self.engine.process('local-%d' % index, 'local', line('JA%dABC' % index))
            self.engine.alignment_snapshot()
        self.assertEqual(len(self.engine.alignment_snapshot()), 3)
        self.now -= 1
        self.assertEqual(self.engine.alignment_snapshot(), [])


class LastEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.engine = SpotProcessor(clock=lambda: START)

    def test_suppressed_spot_retains_the_evaluated_header_only(self):
        self.engine.process('gc', 'gocluster', gc())
        self.engine.process('anchor', 'cluster', line(origin='C1CCC'))
        self.assertEqual(self.engine.process('l', 'local', line(frequency=14020.12)), [])
        self.assertEqual(self.engine.last_evaluation['call'], 'JA1ABC')
        self.assertEqual(self.engine.last_evaluation['frequency_hz'], 14020000)
        self.assertEqual(self.engine.last_evaluation['frequency_basis'], 'gocluster')

    def test_input_and_call_replacement_report_the_chosen_basis(self):
        self.engine.process('l', 'local', line())
        self.assertEqual(self.engine.last_evaluation['frequency_basis'], 'input')
        self.engine = SpotProcessor(clock=lambda: START)
        self.engine.process('gc', 'gocluster', gc(call='JA2ABD', confidence='V'))
        self.engine.process('primary', 'cluster', line('JA2ABD', origin='F1BBB'))
        self.engine.process('l', 'local', line(call='JA2ABC'))
        self.assertEqual(self.engine.last_evaluation['call'], 'JA2ABD')
        self.assertEqual(self.engine.last_evaluation['frequency_hz'], 14020000)

    def test_bypass_and_unparsed_input_clear_previous_evaluation(self):
        for mode in ('history', 'disabled', 'unparsed', 'stale'):
            self.engine.config['enabled'] = True
            self.engine.process('l', 'local', line())
            self.assertIsNotNone(self.engine.last_evaluation)
            if mode == 'history':
                self.engine.process('l', 'local', line(), history=True)
            elif mode == 'disabled':
                self.engine.config['enabled'] = False
                self.engine.process('l', 'local', line())
            elif mode == 'unparsed':
                self.engine.process('l', 'local', b'unknown traffic')
            else:
                self.engine.process('l', 'local', line(stamp='1100Z'))
            self.assertIsNone(self.engine.last_evaluation)


if __name__ == '__main__':
    unittest.main()
