"""Local decoder correction uses independent external evidence, never familiarity."""
import time
import unittest
from spot_processing import SpotProcessor, parse_spot
from test_spot_processing import START, line, gc


class LocalCallCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.now = START
        self.engine = SpotProcessor(clock=lambda: self.now)

    def raw(self, call, frequency=7010.0, origin='LOCAL1', extra=b'original comment'):
        raw = line(call, frequency, origin=origin, stamp=time.strftime('%H%MZ', time.gmtime(self.now)), extra=extra)
        return raw.replace(('%.2f' % frequency).encode(), ('%.3f' % frequency).encode(), 1)

    def external(self, call='S53M', frequency=7010.0, origin='DL1AAA', role='cluster'):
        raw = self.raw(call, frequency, origin)
        self.engine.process('external-' + origin, role, raw)

    def local(self, call='S53MM', frequency=7010.1):
        raw = self.raw(call, frequency, extra=b'original  Q:3 > text \xff')
        return raw, self.engine.process('local', 'local', raw)

    def confirm(self, call='S53M', frequency=7010.0):
        self.external(call, frequency, 'DL1AAA')
        self.external(call, frequency, 'F1BBB', 'rbn')

    def test_two_external_receivers_correct_without_c_glyph_and_preserve_suffix(self):
        self.engine = SpotProcessor({'repeat_seconds': 1}, clock=lambda: self.now)
        self.confirm()
        self.now += 2
        raw, result = self.local()
        # Correction is observable even if current candidate output is deduped.
        self.assertEqual(self.engine.last_evaluation['call'], 'S53M')
        self.assertEqual(self.engine.last_evaluation['frequency_hz'], 7010000)
        if result:
            self.assertEqual(result[0].split(b'CW',1)[1],raw.split(b'CW',1)[1])
        self.assertEqual(self.engine.snapshot()['local_call_corrected'], 1)

    def test_one_receiver_and_its_mirror_hold_then_release_original(self):
        self.external()
        self.external(origin='DL1AAA-#', role='gocluster')
        raw, result = self.local()
        self.assertEqual(result, [])
        self.assertEqual(self.engine.snapshot()['current_local_call_holds'], 1)
        self.now += 3
        released = self.engine.drain_local_holds()
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]['outputs'], [raw])
        self.assertEqual(released[0]['evaluation']['call'], 'S53MM')
        self.assertEqual(self.engine.snapshot()['local_call_expired'], 1)

    def test_hold_releases_early_after_second_receiver_without_reobservation(self):
        self.external()
        raw, result = self.local()
        before = dict(self.engine.counts)
        held_measurements = dict(self.engine.measurements)
        self.now += 1
        self.external(origin='F1BBB')
        count = self.engine.counts['received_lines']
        events = self.engine.drain_local_holds()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['evaluation']['call'], 'S53M')
        self.assertEqual(self.engine.counts['received_lines'], count)
        self.assertEqual(self.engine.counts['parsed_spots'], before['parsed_spots'] + 1)
        for key, value in held_measurements.items():
            self.assertEqual(self.engine.measurements[key].at, value.at)
        self.assertEqual(self.engine.snapshot()['current_local_call_holds'], 0)

    def test_original_external_support_veto_and_multiple_candidates(self):
        self.confirm()
        self.external('S53MM', 7010.1, 'ON1CCC')
        self.local()
        self.assertEqual(self.engine.last_evaluation['call'], 'S53MM')
        self.assertEqual(self.engine.snapshot()['current_local_call_holds'], 0)
        self.engine = SpotProcessor(clock=lambda: self.now)
        self.confirm('S53M')
        self.confirm('S53MN')
        self.local()
        self.assertEqual(self.engine.last_evaluation['call'], 'S53MM')
        self.assertEqual(self.engine.snapshot()['current_local_call_holds'], 1)
        self.now += 3
        self.assertEqual(self.engine.drain_local_holds()[0]['evaluation']['call'], 'S53MM')

    def test_scope_short_calls_slashes_transpositions_and_distant_candidates(self):
        for local_call, candidate, distance in [('S5M','S53M',100), ('S53M/P','S53M',100),
                                                 ('S53MM','S53M/P',100), ('S35M','S53M',100),
                                                 ('S53MM','S53M',201)]:
            with self.subTest(local=local_call,candidate=candidate,distance=distance):
                self.engine = SpotProcessor(clock=lambda: self.now)
                self.confirm(candidate)
                self.local(local_call, 7010 + distance/1000)
                self.assertEqual(self.engine.last_evaluation['call'], local_call)
                self.assertEqual(self.engine.snapshot()['current_local_call_holds'], 0)

    def test_tolerance_boundary_and_same_receiver_local_evidence_is_not_external(self):
        self.confirm()
        self.local(frequency=7010.2)
        self.assertEqual(self.engine.last_evaluation['call'], 'S53M')
        self.engine = SpotProcessor(clock=lambda:self.now)
        for role, source, origin in [('local','other-local','A1AAA'),('local','third-local','B1BBB')]:
            self.engine.process(source,role,self.raw('S53M',7010,origin))
        raw, output = self.local()
        self.assertEqual(output,[raw])
        self.assertEqual(self.engine.snapshot()['current_local_call_holds'],0)

    def test_unrelated_first_local_and_external_inputs_do_not_hold(self):
        self.confirm('S53M')
        raw, result = self.local('JA1ABC', 7020)
        self.assertEqual(result,[raw])
        for role in ('rbn','gocluster','cluster'):
            raw = self.raw('S53MM',7010.1,'X1'+role.upper())
            self.engine.process(role,role,raw)
            self.assertEqual(self.engine.last_evaluation['call'],'S53MM')
        self.assertEqual(self.engine.snapshot()['current_local_call_holds'],0)

    def test_reset_force_release_bounds_and_expiry(self):
        self.external()
        raw, _ = self.local()
        self.engine.reset_source('local')
        self.confirm()
        events = self.engine.drain_local_holds()
        self.assertEqual(events[0]['evaluation']['call'],'S53MM')
        self.assertEqual(events[0]['outputs'],[raw])
        self.engine = SpotProcessor({'max_entries':1},clock=lambda:self.now)
        self.external()
        self.local()
        self.local('S53MN')
        self.assertLessEqual(self.engine.snapshot()['current_local_call_holds'],1)
        self.engine = SpotProcessor({'max_entries':3},clock=lambda:self.now)
        self.external()
        self.local()
        self.now += 301
        self.assertEqual(self.engine.drain_local_holds()[0]['outputs'],[])

    def test_unknown_mode_original_support_vetoes_but_cannot_authorize_replacement(self):
        self.confirm()
        self.engine.process('primary','cluster',line('S53MM',7010.1,origin='ON1CCC',mode='',report='',stamp='1200Z'))
        self.local()
        self.assertEqual(self.engine.last_evaluation['call'],'S53MM')
        self.assertEqual(self.engine.snapshot()['current_local_call_holds'],0)
        self.engine = SpotProcessor(clock=lambda:self.now)
        for origin in ('DL1AAA','F1BBB'):
            self.engine.process('primary','cluster',line('S53M',7010,origin=origin,mode='',report='',stamp='1200Z'))
        raw, result = self.local()
        self.assertEqual(result,[raw])

    def test_deadline_excludes_late_evidence_but_delayed_timer_uses_prior_evidence(self):
        for second_at in (2.9, 3.0, 3.1):
            with self.subTest(second_at=second_at):
                self.now = START
                self.engine = SpotProcessor(clock=lambda:self.now)
                self.external()
                raw, _ = self.local()
                self.now = START + second_at
                self.external(origin='F1BBB')
                self.now = START + 3.2
                event = self.engine.drain_local_holds()[0]
                self.assertEqual(event['evaluation']['call'], 'S53M' if second_at < 3 else 'S53MM')
                if second_at >= 3:
                    self.assertEqual(event['outputs'],[raw])

    def test_new_local_packet_after_deadline_does_not_rewrite_expired_work(self):
        self.external()
        old, _ = self.local()
        self.now += 3.1
        self.external(origin='F1BBB')
        self.local()  # new report may use fresh evidence independently
        self.assertEqual(self.engine.last_evaluation['call'],'S53M')
        events = self.engine.drain_local_holds()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['evaluation']['call'],'S53MM')
        self.assertEqual(events[0]['outputs'],[old])

    def test_identity_search_covers_tolerance_when_frequency_bins_are_narrow(self):
        self.engine = SpotProcessor({'match_tolerance_hz':50},clock=lambda:self.now)
        self.confirm('S53M',7010.01)
        self.external('S53MM',7010.2,'ON1CCC')
        self.local('S53MM',7010.0)
        self.assertEqual(self.engine.last_evaluation['call'],'S53MM')
        self.assertEqual(self.engine.snapshot()['current_local_call_holds'],0)

    def test_repeated_hold_payload_updates_without_extending_deadline(self):
        self.external()
        _, _ = self.local()
        self.now += 2
        newest, _ = self.local()
        self.now += 1
        self.assertEqual(self.engine.drain_local_holds()[0]['outputs'],[newest])
        self.assertEqual(self.engine.snapshot()['local_call_held'],1)

    def test_qualified_local_frequency_adjustment_precedes_identity_distance(self):
        for index in range(5):
            self.now = START + index*12
            frequency = 7010 + index*10
            call = 'JA%dXYZ' % index
            self.engine.process('local','local',self.raw(call,frequency+.3,'LOCAL1'))
            self.external(call,frequency,'DL1AAA')
            self.external(call,frequency,'F1BBB','rbn')
        self.confirm()
        self.local('S53MM',7010.4)
        self.assertEqual(self.engine.last_evaluation['call'],'S53M')
        self.assertAlmostEqual(self.engine.last_evaluation['frequency_hz'],7010000)

    def test_new_epoch_local_report_does_not_replace_saved_old_hold(self):
        self.external()
        old, _ = self.local()
        self.engine.reset_source('local')
        self.now += 1
        self.confirm()
        self.local()
        self.assertEqual(self.engine.last_evaluation['call'],'S53M')
        events = self.engine.drain_local_holds()
        self.assertEqual(events[0]['outputs'],[old])
        self.assertEqual(events[0]['evaluation']['reason'],'local-call-reset')

    def test_late_reference_authority_cannot_backdate_a_local_offset(self):
        self.engine = SpotProcessor({'pair_window_seconds':120},clock=lambda:self.now)
        for index in range(5):
            self.now = START+index*10
            self.engine.process('local','local',self.raw('JA%dXYZ'%index,7020+index*10+.3,'LOCAL1'))
        self.now = START+50
        self.confirm('S53M',7010.0)  # too far from the unadjusted local report
        self.external('S53MN',7010.3,'ON1CCC')  # one plausible, unsettled variant
        old, output = self.local('S53MM',7010.4)
        self.assertEqual(output,[])
        self.now = START+53.1
        for index in range(5):
            self.external('JA%dXYZ'%index,7020+index*10,'DL1AAA')
            self.external('JA%dXYZ'%index,7020+index*10,'F1BBB','rbn')
        self.assertAlmostEqual(self.engine._offset('local','40M',self.now,'LOCAL1'),300)
        self.assertIsNone(self.engine._offset('local','40M',self.now,'LOCAL1',before=START+53))
        event = self.engine.drain_local_holds()[0]
        self.assertEqual(event['outputs'],[old])
        self.assertEqual(event['evaluation']['call'],'S53MM')
        # Rewinding the receipt clock must not retain future authority under an
        # older measurement timestamp either.
        self.now = START+51
        self.engine.snapshot()
        self.assertIsNone(self.engine._offset('local','40M',self.now,'LOCAL1'))
