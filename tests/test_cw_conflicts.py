"""A crowded CW frequency can carry both misdecodes and genuine stations."""
import time
import unittest

from spot_processing import SpotProcessor


class CwConflictTests(unittest.TestCase):
    def setUp(self):
        self.now = 1794944160  # 19:36Z, 17 November 2026
        self.engine = SpotProcessor(clock=lambda: self.now)

    def send(self, rx, call, frequency=3521.0, role='cluster', source='feed',
             comment='CW 14 dB CQ', history=False, stamp=None):
        if stamp is None:
            stamp = time.strftime('%H%MZ', time.gmtime(self.now))
        raw = ('DX de %s: %.3f %s %s %s' % (rx, frequency, call, comment, stamp)).encode()
        return self.engine.process(source, role, raw, history=history)

    def occupy(self):
        self.send('G4ZFE-#', 'S53ZO')
        self.send('DM5GG-#', 'S53ZO')

    def test_november_17_contest_reports(self):
        # Ordered excerpts from the station's RBN mirror. Times are UTC receipt
        # seconds. The last misdecode came 132 s after the last genuine spot.
        reports = (
            (1794944179, 'PE5TT-#', 'S53ZO', 3521.0),
            (1794944180, 'ON6URE-1-#', 'S53ZO', 3521.0),
            (1794944190, 'ON6URE-2-#', 'S53M', 3521.0),
            (1794944295, 'ON6ZQ-#', 'S53ZO', 3521.0),
            (1794944295, 'GI4DOH-#', 'S53ZO', 3521.0),
            (1794944304, 'DK0KK-#', 'S53N', 3521.0),
            (1794944490, 'DE1LON-#', 'S53ZO', 3520.9),
            (1794944498, 'G4ZFE-#', 'S53ZO', 3520.9),
            (1794944557, 'ON6URE-1-#', 'S53GO', 3520.8),
            (1794944968, 'EA8/DF4UE-#', 'S53ZO', 3520.9),
            (1794944970, 'SE5E-#', 'S53ZO', 3520.9),
            (1794944970, 'IK7YTT-#', 'S53ZX', 3520.9),
            (1794945887, 'DO4DXA-#', 'S53ZO', 3520.6),
            (1794945918, 'LZ3CB-#', 'S53ZO', 3520.6),
            (1794946050, 'OH4KA-#', 'S53ZM', 3520.6),
        )
        selected = []
        for self.now, rx, call, frequency in reports:
            selected.extend(self.send(rx, call, frequency))
        self.assertEqual(self.engine.counts['cw_conflicts_held'], 5)
        self.assertTrue(selected)
        self.assertTrue(all(b' S53ZO ' in raw for raw in selected))

    def test_contest_singletons_and_adjacent_confirmed_call(self):
        self.occupy()
        for call in ('S53GO', 'S53M', 'S53N', 'S53ZX', 'S53ZM'):
            self.assertFalse(self.send('ON6URE-1-#', call, 3520.9))
            self.assertEqual(self.engine.last_evaluation['occupant'], 'S53ZO')
            self.assertNotIn(('80M', 'CW', call), self.engine.repeats)
        self.assertFalse(self.send('ON6URE-2', 'S53M', 3520.9, role='gocluster', source='mirror'))
        output = self.send('HA5E-#', 'S53M', 3520.9)
        self.assertIn(b'3520.900 S53M CW 14 dB CQ', output[0])
        self.assertFalse(self.send('LZ5DI-#', 'S53M', 3520.9))
        self.assertTrue(self.send('DK0KK-#', 'OK5S', 3521.101))

    def test_mirrors_and_disagreeing_frequencies_cannot_establish_occupancy(self):
        self.send('ON6URE-1-#', 'S53ZO', 3520.9)
        self.send('ON6URE-2', 'S53ZO', 3520.9, role='gocluster')
        self.send('DM5GG-#', 'S53ZO', 3521.1)
        self.assertTrue(self.send('DK0KK-#', 'S53GO'))

    def test_mirrors_cannot_release_a_conflict_when_incumbent_expires_first(self):
        self.occupy()
        self.now += 165
        self.assertFalse(self.send('ON6URE-1-#', 'S53M'))
        self.now += 30
        self.assertFalse(self.send('ON6URE-2', 'S53M', role='gocluster'))
        self.now += 149
        self.assertFalse(self.send('ON6URE-1-#', 'S53M'))
        self.now += 1
        self.assertTrue(self.send('ON6URE-1-#', 'S53M'))

    def test_human_other_mode_history_and_expiry(self):
        self.occupy()
        self.assertTrue(self.send('S50A', 'S53M', comment='CW heard calling CQ'))
        self.assertTrue(self.send('DK0KK-#', 'S53GO', comment='RTTY 14 dB'))
        self.assertTrue(self.send('DK0KK-#', 'S53ZX', history=True))
        self.assertFalse(self.send('DK0KK-#', 'S53ZX', stamp=''))
        self.now += 180
        self.assertEqual(self.engine.drain_local_holds(force=True), [])
        self.assertTrue(self.send('DK0KK-#', 'S53GO'))
        self.assertNotIn(('80M', 'CW', 'S53ZX'), self.engine.repeats)

    def test_reset_late_delivery_and_old_wire_time(self):
        self.send('G4ZFE-#', 'S53ZO', source='a')
        self.send('DM5GG-#', 'S53ZO', source='b')
        self.engine.reset_source('b')
        self.assertTrue(self.send('DK0KK-#', 'S53GO'))
        self.send('DM5GG-#', 'S53ZO', source='b', stamp='1932Z')
        self.assertTrue(self.send('DK0KK-#', 'S53M'))
