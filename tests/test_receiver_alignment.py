"""Relative offsets belong to receivers and representations, never whole feeds."""
import time
import unittest
from spot_processing import SpotProcessor, parse_spot
from test_spot_processing import START, line, gc


class ReceiverAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.now=START
        self.engine=SpotProcessor(clock=lambda:self.now)

    def send(self,source,role,receiver,call,frequency):
        stamp=time.strftime('%H%MZ',time.gmtime(self.now))
        raw=gc(call,frequency,origin=receiver,stamp=stamp) if role=='gocluster' else line(call,frequency,origin=receiver,stamp=stamp)
        return self.engine.process(source,role,raw)

    def train(self,role='rbn',source='raw',receiver='A1AAA',residual=700):
        for index in range(5):
            self.now=START+index*12
            call='JA%dABC'%index;frequency=14010+index*10
            self.send(source,role,receiver,call,frequency+residual/1000)
            self.send('go','gocluster','B1BBB',call,frequency)
            self.send('raw','rbn','B1BBB',call,frequency)  # same receiver mirrored
            self.send('raw','rbn','C1CCC',call,frequency)

    def row(self,source,receiver,band='20M'):
        return next(row for row in self.engine.alignment_snapshot([source]) if row['receiver']==receiver and row['band']==band)

    def test_one_event_cannot_create_transferable_adjustment(self):
        self.send('raw','rbn','A1AAA','JA1ABC',14020.7)
        self.send('go','gocluster','B1BBB','JA1ABC',14020)
        self.send('raw','rbn','C1CCC','JA1ABC',14020)
        self.assertEqual(self.row('raw','A1AAA')['samples'],1)
        self.assertIsNone(self.row('raw','A1AAA')['offset_hz'])

    def test_receiver_isolation_and_heldout_raw_adjustment(self):
        self.train()
        row=self.row('raw','A1AAA')
        self.assertEqual(row['state'],'Ready')
        self.assertEqual(row['samples'],5)
        self.assertEqual(row['offset_hz'],-700)
        output=self.send('raw','rbn','A1AAA','JA9XYZ',14070.7)
        self.assertEqual(parse_spot(output[0],self.now).frequency_hz,14070000)
        other=self.send('raw','rbn','D1DDD','JA8XYZ',14080.7)
        self.assertEqual(parse_spot(other[0],self.now).frequency_hz,14080700)
        self.assertEqual(self.row('raw','D1DDD')['samples'],0)

    def test_remote_gc_adjustment_is_learned_in_its_own_representation(self):
        self.train(role='gocluster',source='go-subject')
        output=self.send('go-subject','gocluster','A1AAA','JA9XYZ',14070.7)
        self.assertEqual(parse_spot(output[0],self.now).frequency_hz,14070000)
        raw=self.send('raw-other','rbn','A1AAA','JA8XYZ',14080.7)
        self.assertEqual(parse_spot(raw[0],self.now).frequency_hz,14080700)

    def test_reference_expiry_and_source_reset_fall_back_to_original(self):
        self.train()
        self.engine.reset_source('go')
        self.assertEqual(self.row('raw','A1AAA')['samples'],0)
        raw=self.send('raw','rbn','A1AAA','JA9XYZ',14070.7)
        self.assertEqual(parse_spot(raw[0],self.now).frequency_hz,14070700)
        self.train()
        self.now+=121
        self.assertEqual(self.row('raw','A1AAA')['state'],'Expired')
        raw=self.send('raw','rbn','A1AAA','JA8XYZ',14080.7)
        self.assertEqual(parse_spot(raw[0],self.now).frequency_hz,14080700)

    def test_old_subjects_do_not_acquire_fake_time_coverage(self):
        for index in range(5):
            call='JA%dABC'%index;frequency=14010+index*10
            self.send('raw','rbn','A1AAA',call,frequency+.7)
            self.send('go','gocluster','B1BBB',call,frequency)
            self.send('raw','rbn','C1CCC',call,frequency)
        before=dict(self.engine.samples)
        self.now+=30
        self.send('go','gocluster','B1BBB','JA4ABC',14050)
        self.assertIsNone(self.row('raw','A1AAA')['offset_hz'])
        for key,value in before.items():
            if key[:2]==('raw','A1AAA'):
                self.assertEqual(self.engine.samples[key][0],value[0])

    def test_mirrored_subject_does_not_supply_its_own_reference(self):
        self.send('raw','rbn','A1AAA-1','JA1ABC',14020.7)
        self.send('go','gocluster','A1AAA','JA1ABC',14020)
        self.send('raw','rbn','B1BBB','JA1ABC',14020)
        self.assertEqual(self.row('raw','A1AAA-1')['samples'],0)
        self.assertIsNone(self.row('raw','A1AAA-1')['offset_hz'])

    def test_waiting_receivers_are_not_skipped_when_queue_shrinks(self):
        for index in range(100):
            self.send('raw','rbn','X%dAAA'%index,'HA8LNN',14020 if index<50 else 14020.7)
        self.send('go','gocluster','G1AAA','HA8LNN',14020)
        self.send('go','gocluster','G2AAA','HA8LNN',14020)
        for _ in range(20):
            self.send('go','gocluster','G2AAA','HA8LNN',14020)
        owners = {key[:3] for key in self.engine.samples}
        self.assertTrue(all(('raw','X%dAAA'%index,'20M') in owners for index in range(100)))

    def test_retry_authority_is_fixed_size_and_ladder_groups_are_linear(self):
        for index in range(100):
            self.send('raw','rbn','X%dAAA'%index,'HA8LNN',14020+index*.005)
        for value in self.engine.calibration_retry.values():
            self.assertIsInstance(value[1],bytes)
            self.assertEqual(len(value[1]),32)
        observations=list(self.engine.measurements.values())
        groups=self.engine._groups(observations,self.now)
        self.assertLessEqual(len(groups),6)
        self.assertLessEqual(sum(len(group['members']) for group in groups),6*len(observations))

    def test_band_isolation_and_index_bounds(self):
        self.train()
        raw=self.send('raw','rbn','A1AAA','JA9XYZ',7070.7)
        self.assertEqual(parse_spot(raw[0],self.now).frequency_hz,7070700)
        for store in (self.engine.measurements,self.engine.votes,self.engine.pending,self.engine.samples,self.engine.rejection,self.engine.calibration_retry):
            self.assertLessEqual(len(store),self.engine.config['max_entries'])
            self.assertLessEqual(len(store.expiry),2*store.limit+64)
            for name,grouper in store.groupers.items():
                expected={}
                for key,value in store.items(): expected.setdefault(grouper(key,value),set()).add(key)
                self.assertEqual({group:set(keys) for group,keys in store.indexes[name].items()},expected)
