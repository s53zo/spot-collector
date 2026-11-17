"""Owner-selected frequency policy and public-RBN regressions."""
import time
import unittest
from spot_processing import SpotProcessor, parse_spot
from test_spot_processing import START, line, gc


class CorroborationAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.now = START
        self.engine = SpotProcessor(clock=lambda: self.now)

    def send(self, receiver, frequency, role='gocluster', source=None, call='HA8LNN'):
        stamp = time.strftime('%H%MZ', time.gmtime(self.now))
        raw = (gc(call, frequency, origin=receiver, stamp=stamp) if role == 'gocluster'
               else line(call, frequency, origin=receiver, stamp=stamp))
        return self.engine.process(source or role, role, raw)

    def test_ha8lnn_single_outlier_never_moves_anchor(self):
        self.assertTrue(self.send('W3DAN', 7009.0))
        for receiver in ('HG8A', 'K5TR', 'S50ARX'):
            self.assertFalse(self.send(receiver, 7009.0))
        self.now += 6
        self.assertFalse(self.send('IZ3EAW', 7009.7))
        self.assertFalse(self.send('HB9FX', 7009.0))
        self.assertEqual(self.engine.snapshot().get('frequency_change_updates', 0), 0)

    def test_mirrored_receiver_is_one_vote_and_two_need_no_majority(self):
        self.assertTrue(self.send('W3DAN', 7009.0))
        for receiver in ('HG8A', 'K5TR', 'S50ARX', 'HB9FX', 'DL1AAA'):
            self.send(receiver, 7009.0)
        self.now += 1
        self.assertFalse(self.send('IZ3EAW-#', 7009.7))
        self.assertFalse(self.send('IZ3EAW', 7009.7, 'cluster', 'primary'))
        selected = self.send('F1AAA', 7009.7, 'rbn', 'Server2')
        self.assertEqual(len(selected), 1)
        self.assertEqual(parse_spot(selected[0], self.now).frequency_hz, 7009700)
        self.assertEqual(self.engine.snapshot()['frequency_change_updates'], 1)

    def test_unconfirmed_move_expires_without_forced_output(self):
        self.send('W3DAN', 7009.0)
        self.assertFalse(self.send('IZ3EAW', 7009.7))
        self.now += 11
        self.engine.snapshot()
        self.assertFalse(self.send('F1AAA', 7009.7, 'cluster'))
        self.assertEqual(self.engine.snapshot().get('frequency_change_updates', 0), 0)

    def test_public_rbn_per_receiver_alignment_not_endpoint_pool(self):
        for index in range(5):
            self.now = START + index * 12
            call = 'JA%dABC' % index
            frequency = 14010 + index * 10
            self.send('A1AAA', frequency + .12, 'rbn', 'Server2', call)
            self.send('B1BBB', frequency, 'gocluster', 'GoCluster', call)
            self.send('B1BBB', frequency, 'rbn', 'Server2', call)  # mirror, not another witness
            self.send('C1CCC', frequency, 'rbn', 'Server2', call)
        rows = self.engine.alignment_snapshot(['Server2'])
        subject = [r for r in rows if r.get('receiver') == 'A1AAA' and r['band'] == '20M']
        self.assertEqual(len(subject), 1)
        self.assertGreaterEqual(subject[0]['samples'], 5)
        self.assertEqual(subject[0]['state'], 'Ready')
        self.assertAlmostEqual(subject[0]['offset_hz'], -120, delta=10)
        result = self.send('A1AAA', 14070.12, 'rbn', 'Server2', 'JA9XYZ')
        self.assertTrue(result)
        self.assertAlmostEqual(parse_spot(result[0], self.now).frequency_hz, 14070000, delta=10)

    def test_remote_gocluster_receiver_adjustment_uses_other_receivers(self):
        for index in range(5):
            self.now = START + index * 12
            call = 'K%dABC' % index
            frequency = 14010 + index * 10
            self.send('IZ3EAW', frequency + .7, 'gocluster', 'GoCluster', call)
            self.send('B1BBB', frequency, 'gocluster', 'GoCluster', call)
            self.send('C1CCC', frequency, 'rbn', 'Server2', call)
        rows = self.engine.alignment_snapshot(['GoCluster'])
        subject = next(r for r in rows if r.get('receiver') == 'IZ3EAW' and r['band'] == '20M')
        self.assertEqual(subject['state'], 'Ready')
        self.assertAlmostEqual(subject['offset_hz'], -700, delta=10)
        selected = self.send('IZ3EAW', 14070.7, 'gocluster', 'GoCluster', 'K9XYZ')
        self.assertEqual(len(selected), 1)
        self.assertAlmostEqual(parse_spot(selected[0], self.now).frequency_hz, 14070000, delta=10)
