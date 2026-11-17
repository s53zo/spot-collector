"""Independent local busted-call examples; external feeds remain authoritative inputs."""
import unittest
from spot_processing import SpotProcessor, parse_spot
from test_spot_processing import START, line, gc


class LocalCallAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.now = START
        self.p = SpotProcessor({'repeat_seconds': 1}, clock=lambda: self.now)

    def reference(self, call='S53M', origin='DL1AAA', role='gocluster'):
        raw = gc(call, 7010, origin=origin, confidence='V') if role == 'gocluster' else line(call,7010,origin=origin)
        return self.p.process(role, role, raw)

    def test_two_external_receivers_correct_local_without_c_and_keep_payload(self):
        self.reference()
        self.reference(origin='F1AAA', role='cluster')
        self.now += 2
        raw = line('S53MM', 7010.1, origin='TK0C-1', extra=b'original  Q:9 > payload \xff')
        result = self.p.process('local','local',raw)
        self.assertEqual(len(result),1)
        parsed = parse_spot(result[0], self.now)
        self.assertEqual(parsed.call, 'S53M')
        self.assertAlmostEqual(parsed.frequency_hz,7010000,delta=1)
        self.assertEqual(result[0].split(b'CW',1)[1],raw.split(b'CW',1)[1])

    def test_original_external_support_vetoes_replacement(self):
        self.reference(); self.reference(origin='F1AAA',role='cluster')
        self.p.process('primary','cluster',line('S53MM',7010.1,origin='R1AAA'))
        self.now += 2
        raw=line('S53MM',7010.1,origin='TK0C-1')
        self.assertEqual(self.p.process('local','local',raw),[raw])
        self.assertEqual(self.p.snapshot().get('callsign_corrections',0),0)

    def test_same_receiver_copies_cannot_authorize_call_correction(self):
        self.reference(origin='DL1AAA-#')
        self.reference(origin='DL1AAA',role='cluster')
        raw=line('S53MM',7010.1,origin='TK0C-1')
        self.assertEqual(self.p.process('local','local',raw),[])
        self.now+=4
        events=self.p.drain_local_holds()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['outputs'],[raw])

    def test_external_and_short_call_never_enter_local_correction(self):
        self.reference(); self.reference(origin='F1AAA',role='cluster')
        raw=line('S53MM',7010.1,origin='R1AAA')
        self.assertEqual(self.p.process('public','rbn',raw), [])
        self.assertEqual(self.p.last_evaluation['call'], 'S53MM')
        short=line('S5M',7010.1,origin='TK0C-1')
        self.assertEqual(self.p.process('local','local',short), [])
        self.assertEqual(self.p.last_evaluation['call'], 'S5M')
        self.assertEqual(self.p.last_evaluation['reason'], 'cw-conflict-unconfirmed')
