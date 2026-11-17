"""Frequency evidence is receiver-owned; delivery rate policy stays separate."""
import time
import random
from statistics import median
import unittest
from spot_processing import SpotProcessor, Observation, parse_spot
from test_spot_processing import START, line, gc


class FrequencyCorroborationTests(unittest.TestCase):
    def setUp(self):
        self.now = START
        self.engine = SpotProcessor(clock=lambda:self.now)

    def send(self, origin, frequency, role='gocluster', call='HA8LNN', source=None, extra=b'original Q:9 > payload'):
        stamp=time.strftime('%H%MZ',time.gmtime(self.now))
        raw=gc(call,frequency,origin=origin,stamp=stamp) if role=='gocluster' else line(call,frequency,origin=origin,stamp=stamp,extra=extra)
        return self.engine.process(source or role,role,raw)

    def test_first_spot_prompt_then_one_outlier_cannot_move(self):
        self.assertTrue(self.send('A1AAA',7009))
        for origin in ('B1BBB','C1CCC','D1DDD'):
            self.assertFalse(self.send(origin,7009))
        self.assertFalse(self.send('I1III',7009.7))
        self.assertEqual(self.engine.last_evaluation['reason'],'pending-corroboration')
        self.assertFalse(self.send('B1BBB',7009))
        self.assertEqual(self.engine.snapshot().get('frequency_change_updates',0),0)

    def test_two_receivers_promote_without_majority(self):
        self.send('A1AAA',7009)
        for origin in ('B1BBB','C1CCC','D1DDD','E1EEE'):
            self.send(origin,7009)
        self.assertFalse(self.send('I1III',7009.7))
        selected=self.send('F1FFF',7009.7,'rbn')
        self.assertEqual(len(selected),1)
        self.assertEqual(parse_spot(selected[0],self.now).frequency_hz,7009700)
        self.assertEqual(self.engine.last_evaluation['support'],2)

    def test_promotion_retires_old_support_even_same_receipt_clock(self):
        self.send('A1AAA',7009)
        self.send('B1BBB',7009)
        self.send('C1CCC',7009.7)
        self.assertTrue(self.send('D1DDD',7009.7))
        self.assertFalse(self.send('A1AAA',7009))
        self.assertTrue(self.send('B1BBB',7009))
        self.assertEqual(self.engine.snapshot()['frequency_change_updates'],2)
        self.send('E1EEE',7010.4)
        self.assertFalse(self.send('F1FFF',7010.4))
        self.assertEqual(self.engine.last_evaluation['reason'],'move-budget-exhausted')

    def test_mirrors_and_unknown_numeric_aliases_cannot_confirm(self):
        self.send('A1AAA',7009)
        self.send('B1BBB-1-#',7009.7,'rbn')
        self.assertFalse(self.send('B1BBB',7009.7,'gocluster'))
        self.assertFalse(self.send('B1BBB-2',7009.7,'cluster'))
        self.assertEqual(self.engine.snapshot().get('frequency_change_updates',0),0)
        self.assertEqual(len(self.engine.pending),1)

    def test_candidate_exact_expiry_never_forces_release(self):
        self.send('A1AAA',7009)
        self.send('B1BBB',7009.7)
        self.now += 10
        self.engine.snapshot()
        self.assertEqual(self.engine.snapshot()['expired_moves'],1)
        self.assertFalse(self.send('C1CCC',7009.7))
        self.assertEqual(self.engine.snapshot().get('confirmed_moves',0),0)

    def test_source_reset_removes_receiver_vote_authority(self):
        self.send('A1AAA',7009)
        self.send('B1BBB',7009.7,source='first')
        self.engine.reset_source('first')
        self.assertFalse(self.send('C1CCC',7009.7))

    def test_gc_preference_is_group_priority_not_single_vote(self):
        observations=[]
        for role,origin,frequency in [('cluster','A1AAA',7009.7),('cluster','B1BBB',7009.7),('gocluster','C1CCC',7010.4)]:
            raw=line('HA8LNN',frequency,origin=origin)
            observations.append(Observation(parse_spot(raw,self.now),role,self.now,role))
        groups=self.engine._groups(observations,self.now)
        self.assertEqual(self.engine._choose_group(groups)['frequency'],7009700)
        observations.append(Observation(parse_spot(line('HA8LNN',7010.4,origin='D1DDD'),self.now),'cluster',self.now,'cluster'))
        self.assertEqual(self.engine._choose_group(self.engine._groups(observations,self.now))['frequency'],7010400)
        observations.append(Observation(parse_spot(line('HA8LNN',7009.7,origin='E1EEE'),self.now),'go',self.now,'gocluster'))
        self.assertIsNone(self.engine._choose_group(self.engine._groups(observations,self.now)))

    def test_move_keeps_triggering_comment_and_gc_frequency(self):
        self.send('A1AAA',7009)
        self.send('B1BBB',7009.71)
        output=self.send('C1CCC',7009.70,'rbn',extra=b'exact  comment \xff Q:7')
        self.assertEqual(len(output),1)
        self.assertIn(b'7009.71',output[0])
        self.assertTrue(output[0].endswith(b'exact  comment \xff Q:7 1200Z'))


class GroupSummaryOracleTests(unittest.TestCase):
    def test_summary_matches_all_maximal_windows_before_and_after_move_filter(self):
        rng=random.Random(816)
        engine=SpotProcessor(clock=lambda:START)
        for _ in range(250):
            observations=[]
            for index in range(rng.randrange(3,40)):
                frequency=7009+rng.randrange(0,150)*.01
                role=rng.choice(('gocluster','rbn','cluster'))
                spot=parse_spot(line('HA8LNN',frequency,origin='A%dAAA'%index),START)
                observations.append(Observation(spot,str(index),START,role))
            values=sorted(observations,key=lambda obs:(obs.spot.frequency_hz,engine._receiver_base(obs.spot.origin)))
            all_groups=[]
            for first in values:
                members=[obs for obs in values if first.spot.frequency_hz <= obs.spot.frequency_hz <= first.spot.frequency_hz+100]
                ids=frozenset(engine._receiver_base(obs.spot.origin) for obs in members)
                if len(members)<2 or any(ids <= group['ids'] for group in all_groups):
                    continue
                gc_members=[obs for obs in members if obs.role=='gocluster']
                freq=gc_members[0].spot.frequency_hz if gc_members else median(obs.spot.frequency_hz for obs in members)
                all_groups.append(dict(ids=ids,gc=bool(gc_members),frequency=freq,observed=START))
            anchor=7009000+rng.randrange(0,150)*10
            for predicate in (None,lambda frequency,observed:abs(frequency-anchor)>500):
                expected=engine._choose_group([group for group in all_groups if predicate is None or predicate(group['frequency'],group['observed'])])
                actual=engine._choose_group(engine._groups(observations,START,window_filter=predicate))
                self.assertEqual(None if expected is None else expected['frequency'],None if actual is None else actual['frequency'])
                self.assertLessEqual(len(engine._groups(observations,START,window_filter=predicate)),6)
