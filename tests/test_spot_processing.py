import calendar
import unittest

from spot_processing import SpotProcessor, parse_spot, receiver_identity


START = calendar.timegm((2026, 11, 28, 12, 0, 0))


def line(call='JA1ABC', frequency=14020.0, origin='TK0C-1-#', mode='CW',
         report='12 dB', stamp='1200Z', extra=b'CQ test'):
    return (('DX de %s: %9.2f  %-13s%s %s ' %
             (origin, frequency, call, mode, report)).encode() + extra +
            b' ' + stamp.encode())


def gc(call='JA1ABC', frequency=14020.0, origin='DL1AAA-#', mode='CW',
       confidence='V', stamp='1200Z'):
    prefix = ('DX de %s:' % origin)
    header = (prefix + ('%.2f' % frequency).rjust(25 - len(prefix)) +
              '  ' + call.ljust(12))
    assert len(header) == 39
    return (header + (mode + ' 12 dB CQ').ljust(22) +
            '   JN42 ' + confidence + ' ' + stamp).encode()


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.now = START
        self.engine = SpotProcessor(clock=lambda: self.now)

    def test_local_singleton_and_crossfeed_repeat(self):
        raw = line()
        self.assertEqual(self.engine.process('local', 'local', raw), [raw])
        self.assertEqual(self.engine.process('primary', 'cluster', line(origin='TK0C-1')), [])
        self.assertEqual(self.engine.process('gc', 'gocluster', gc()), [])
        self.assertEqual(self.engine.snapshot()['suppressed_repeats'], 2)

    def test_duplicates_do_not_refresh_lifetime(self):
        self.engine.process('one', 'local', line())
        self.now += 179
        self.assertEqual(self.engine.process('two', 'cluster', line()), [])
        self.now += 1
        self.assertEqual(self.engine.process('two', 'cluster', line()), [line()])

    def test_bounded_moves_and_stale_move(self):
        self.assertTrue(self.engine.process('p', 'cluster', line()))
        for frequency in (14021, 14022):
            self.assertFalse(self.engine.process('p', 'cluster', line(frequency=frequency, origin='A1AAA')))
            self.assertTrue(self.engine.process('p2', 'cluster', line(frequency=frequency, origin='B1BBB')))
        self.assertFalse(self.engine.process('p', 'cluster', line(frequency=14023)))
        self.assertEqual(self.engine.snapshot()['frequency_change_updates'], 2)
        other = SpotProcessor(clock=lambda: self.now)
        other.process('p', 'cluster', line(stamp='1200Z'))
        self.assertFalse(other.process('p', 'cluster', line(frequency=14023, stamp='1159Z')))

    def test_mode_and_band_separate(self):
        self.assertTrue(self.engine.process('l', 'local', line()))
        self.assertTrue(self.engine.process('l', 'local', line(mode='RTTY')))
        self.assertTrue(self.engine.process('l', 'local', line(frequency=7020)))

    def test_signed_missing_zero_and_numeric_comment(self):
        for text, expected in (('-12 dB', -12), ('0 dB', 0), ('CQ', None)):
            parsed = parse_spot(line(report=text), START)
            self.assertEqual(parsed.report, expected)
        self.assertIsNone(parse_spot(line(report='CQ 0800Z'), START).report)
        self.assertIsNone(parse_spot(line(report='999 dB'), START).report)

    def test_markers_do_not_remove_receiver_ssid(self):
        self.assertEqual(receiver_identity('tk0c-1-#'), 'TK0C-1')
        self.assertEqual(receiver_identity('tk0c-1'), 'TK0C-1')
        self.assertNotEqual(receiver_identity('TK0C-1'), receiver_identity('TK0C-2'))

    def test_go_layout_and_exact_payload(self):
        reference = gc(frequency=14020.12)
        self.assertEqual(len(reference), 76)
        self.assertEqual(parse_spot(reference, START, 'gocluster').confidence, 'V')
        self.engine.process('gc', 'gocluster', reference)
        self.now += 180
        # Refresh GC evidence, but do not change its source timestamp artificially.
        reference = gc(frequency=14020.12, stamp='1203Z')
        self.engine.process('gc', 'gocluster', reference)
        self.now += 180
        self.engine.process('gc', 'gocluster', gc(frequency=14020.12, stamp='1206Z'))
        self.engine.process('anchor', 'cluster', line(frequency=14020.12, stamp='1206Z', origin='C1CCC'))
        self.now += 1
        local = line(frequency=14020.24, stamp='1206Z', extra=b'Original  Q:9 Z:15 \xff')
        # Use a different output episode to expose the chosen local header.
        self.engine.repeats.clear()
        result = self.engine.process('l', 'local', local)[0]
        self.assertIn(b'14020.12', result)
        self.assertEqual(result[result.index(b'CW'):], local[local.index(b'CW'):])

    def test_stale_reference_cannot_rewind_local_qsy(self):
        self.engine.process('gc', 'gocluster', gc(stamp='1159Z'))
        raw = line(frequency=14020.2, stamp='1200Z')
        self.engine.repeats.clear()
        self.assertEqual(self.engine.process('l', 'local', raw), [raw])

    def test_local_identity_needs_two_external_receivers_not_a_c_glyph(self):
        for glyph in '?SPVBC':
            engine = SpotProcessor(clock=lambda: START)
            engine.process('gc', 'gocluster', gc(call='JA1ABD', confidence=glyph))
            original = line()
            self.assertEqual(engine.process('l', 'local', original), [])
            self.assertEqual(engine.drain_local_holds(force=True)[0]['outputs'], [original])
        engine = SpotProcessor(clock=lambda: START)
        engine.process('gc', 'gocluster', gc(call='JA1ABD', confidence='V'))
        engine.process('primary', 'cluster', line('JA1ABD', origin='F1BBB'))
        engine.repeats.clear()
        original = line(extra=b'Original comment JA1ABC')
        output = engine.process('l', 'local', original)[0]
        self.assertIn(b'JA1ABD', output[:output.index(b'CW')])
        self.assertEqual(output[output.index(b'CW'):], original[original.index(b'CW'):])
        # Independently seen original identity vetoes the candidate.
        engine.process('other', 'cluster', line('JA1ABC', origin='ON1CCC'))
        engine.repeats.clear()
        self.assertIn(b'JA1ABC', engine.process('l', 'local', original)[0])
        engine = SpotProcessor(clock=lambda: START)
        engine.process('gc', 'gocluster', gc(call='JA1ABCDEFG', confidence='C'))
        engine.repeats.clear()
        self.assertIn(b'JA1ABCDEFX', engine.process('l', 'local', line(call='JA1ABCDEFX'))[0])

    def _train(self, engine=None, residual=120, quantized=False):
        engine = engine or self.engine
        for index in range(5):
            frequency = 14010 + index * 10
            call = 'JA%dABC' % index
            raw = frequency + residual / 1000
            reference = round(frequency * 10) / 10 if quantized else frequency
            engine.process('l', 'local', line(call, raw))
            engine.process('gc', 'gocluster', gc(call, reference))
            engine.process('anchor', 'cluster', line(call, reference, origin='C1CCC'))
            self.now += 10
        return engine

    def test_automatic_alignment_unseen_call_and_reference_expiry(self):
        self._train()
        raw = line('JA9XYZ', 14070.12, extra=b'untouched -12 dB')
        output = self.engine.process('l', 'local', raw)[0]
        self.assertIn(b'14070.00', output)
        self.assertEqual(output[output.index(b'CW'):], raw[raw.index(b'CW'):])
        self.now += 121
        raw = line('JA8XYZ', 14080.12, stamp='1202Z')
        self.assertEqual(self.engine.process('l', 'local', raw), [raw])

    def test_quantized_hour_scale_drift_and_no_cross_band_transfer(self):
        for minute, residual in ((0, 80), (60, 120), (120, 180)):
            self.now = START + minute * 60
            stamp = '%02d00Z' % (12 + minute // 60)
            for index in range(5):
                freq = 14010.03 + index * 10.02
                reference = round(freq * 10) / 10
                call = 'JA%dABC' % index
                self.engine.process('l', 'local', line(call, freq + residual / 1000, stamp=stamp))
                self.engine.process('gc', 'gocluster', gc(call, reference, stamp=stamp))
                self.engine.process('anchor', 'cluster', line(call, reference, origin='C1CCC', stamp=stamp))
                self.now += 10
            raw = line('JA9XYZ', 14090 + residual / 1000, stamp=stamp)
            self.engine.repeats.clear()
            output = self.engine.process('l', 'local', raw)[0]
            measured = parse_spot(output, self.now).frequency_hz
            self.assertLessEqual(abs(measured - 14090000), 50)
            raw_other = line('JA8XYZ', 7090 + residual / 1000, stamp=stamp)
            self.assertEqual(self.engine.process('other-band', 'local', raw_other), [raw_other])

    def test_no_training_on_self_returns(self):
        for index in range(8):
            call = 'JA%dABC' % index
            self.engine.process('l', 'local', line(call, 14010 + index * 10 + .12))
            self.engine.process('gc', 'gocluster', gc(call, 14010 + index * 10,
                                                    origin='TK0C-1-#'))
        self.assertEqual(len(self.engine.samples), 0)
        raw = line('JA9XYZ', 14100.12)
        self.assertEqual(self.engine.process('l', 'local', raw), [raw])

    def test_no_training_on_go_stripped_local_receiver_alias(self):
        for origin in ('TK0C', 'TK0C-#', 'TK0C-2-#'):
            engine = SpotProcessor(clock=lambda: self.now)
            for index in range(5):
                call = 'JA%dABC' % index
                engine.process('l', 'local', line(call, 14010 + index * 10 + .12))
                engine.process('gc', 'gocluster', gc(call, 14010 + index * 10, origin=origin))
                self.now += 10
            self.assertEqual(len(engine.samples), 0)

    def test_simultaneous_reports_do_not_establish_drift_pattern(self):
        for index in range(5):
            call = 'JA%dABC' % index
            self.engine.process('l', 'local', line(call, 14010 + index * 10 + .12))
            self.engine.process('gc', 'gocluster', gc(call, 14010 + index * 10))
        self.assertIsNone(self.engine._offset('l', '20M', self.now))

    def test_scatter_and_resolvable_scale_cannot_create_offset(self):
        self.engine.config['match_tolerance_hz'] = 1000
        self.engine.config['calibration_max_scatter_hz'] = 1000
        for index in range(5):
            f = 14010 + index * 50
            residual = 20 + index * 100
            call = 'JA%dABC' % index
            self.engine.process('l', 'local', line(call, f + residual / 1000))
            self.engine.process('gc', 'gocluster', gc(call, f))
            self.now += 10
        self.assertIsNone(self.engine._offset('l', '20M', self.now))

    def test_reconnect_invalidates_evidence(self):
        self._train()
        self.engine.reset_source('gc')
        self.assertFalse(self.engine.samples)
        self.assertFalse(self.engine.references)
        raw = line('JA9XYZ', 14070.12)
        self.assertEqual(self.engine.process('l', 'local', raw), [raw])

    def test_history_bypasses_both_evidence_and_thinning(self):
        raw = line()
        self.engine.process('l', 'local', raw)
        count = len(self.engine.locals)
        self.assertEqual(self.engine.process('p', 'cluster', raw, history=True), [raw])
        self.assertEqual(len(self.engine.locals), count)

    def test_limits_freshness_unknown_and_midnight(self):
        engine = SpotProcessor({'max_entries': 3}, clock=lambda: self.now)
        for index in range(10):
            engine.process('l', 'local', line('JA%dABC' % index))
        self.assertLessEqual(len(engine.locals), 3)
        self.assertLessEqual(len(engine.repeats), 3)
        self.assertEqual(engine.process('p', 'cluster', b'hello \xff'), [b'hello \xff'])
        self.assertEqual(engine.process('p', 'cluster', line(stamp='1100Z')), [])
        self.now = calendar.timegm((2026, 11, 29, 0, 0, 10))
        self.assertEqual(parse_spot(line(stamp='2359Z'), self.now).observed_at,
                         calendar.timegm((2026, 11, 28, 23, 59, 0)))
        engine.snapshot()
        self.assertEqual(len(engine.locals), 0)

    def test_late_old_frequency_does_not_return_after_repeat_expiry(self):
        self.now = START + 4 * 60
        self.engine.process('l', 'local', line(frequency=14021, stamp='1204Z'))
        self.now += 181
        self.assertEqual(self.engine.process('gc', 'gocluster',
                         gc(frequency=14020, stamp='1203Z')), [])
        self.assertEqual(self.engine.snapshot()['out_of_order_spots'], 1)

    def test_local_competing_station_blocks_c_replacement(self):
        self.engine.process('other-local', 'local', line('JA1ABE', 14020.2))
        self.engine.process('gc', 'gocluster', gc('JA1ABD', confidence='C'))
        self.engine.repeats.clear()
        original = line()
        self.assertEqual(self.engine.process('l', 'local', original), [])
        output = self.engine.drain_local_holds(force=True)[0]['outputs'][0]
        self.assertIn(b'JA1ABC', output)

    def test_missing_human_mode_matches_only_unambiguous_presentation(self):
        human = line(mode='', report='', extra=b'heard calling CQ')
        local = line()
        for first, first_role, second, second_role in (
                (local, 'local', human, 'cluster'),
                (human, 'cluster', local, 'local')):
            engine = SpotProcessor(clock=lambda: START)
            self.assertEqual(engine.process('first', first_role, first), [first])
            self.assertEqual(engine.process('second', second_role, second), [])
        engine = SpotProcessor(clock=lambda: START)
        self.assertTrue(engine.process('l', 'local', local))
        self.assertTrue(engine.process('r', 'cluster', line(mode='RTTY')))
        self.assertEqual(engine.process('p', 'cluster', human), [human])
        self.assertFalse(engine.samples)

    def test_stale_mode_missing_report_cannot_rollback_after_expiry(self):
        human = line(frequency=14020, mode='', report='', stamp='1203Z',
                     extra=b'heard calling CQ')
        self.now = START + 4 * 60
        self.engine.process('l', 'local', line(frequency=14021, stamp='1204Z'))
        self.now += 181
        self.assertEqual(self.engine.process('p', 'cluster', human), [])
        self.assertEqual(self.engine.snapshot()['out_of_order_spots'], 1)
        self.assertFalse(self.engine.samples)

    def test_stale_known_mode_consults_sole_unknown_watermark(self):
        self.now = START + 4 * 60
        human = line(frequency=14021, mode='', report='', stamp='1204Z')
        self.engine.process('p', 'cluster', human)
        self.now += 181
        self.assertEqual(self.engine.process('l', 'local',
                         line(frequency=14020, stamp='1203Z')), [])
        self.assertFalse(self.engine.locals)

    def test_multiple_mode_watermarks_do_not_infer_unknown_mode(self):
        self.now = START + 4 * 60
        self.engine.process('l', 'local', line(frequency=14021, stamp='1204Z'))
        self.engine.process('r', 'cluster', line(frequency=14021, mode='RTTY', stamp='1204Z'))
        self.now += 181
        human = line(frequency=14020, mode='', report='', stamp='1203Z')
        self.assertEqual(self.engine.process('p', 'cluster', human), [human])

    def test_terminal_timestamp_then_locator_preserves_original_payload(self):
        for grid in (b'JN42', b'jn42qx', b'JN42QX12'):
            raw = line(extra=b'original  spacing') + b' ' + grid
            parsed = parse_spot(raw, START)
            self.assertEqual(parsed.observed_at, START)
            self.assertEqual(self.engine.process(grid.decode(), 'local', raw),
                             [raw] if grid == b'JN42' else [])
        for suffix in (b'ZZ42', b'JN42YZ', b'JN42 trailing text'):
            self.assertIsNone(parse_spot(line() + b' ' + suffix, START).observed_at)
        self.assertIsNone(parse_spot(line(stamp='', extra=b'CQ 1200Z in JN42'), START).observed_at)

    def test_timestamp_then_grid_cannot_bypass_newer_frequency(self):
        self.now = START + 240
        self.engine.process('l', 'local', line(frequency=14021, stamp='1204Z'))
        self.now += 181
        old = line(frequency=14020, stamp='1203Z') + b' JN42'
        self.assertEqual(self.engine.process('p', 'cluster', old), [])
        self.assertEqual(self.engine.snapshot()['out_of_order_spots'], 1)

    def test_undated_first_spot_passes_but_cannot_rewind_dated_frequency(self):
        undated = line(frequency=14020, stamp='', extra=b'CQ no wire time')
        self.assertEqual(self.engine.process('p', 'cluster', undated), [undated])
        self.now += 181
        dated = line(frequency=14021, stamp='1203Z')
        self.assertEqual(self.engine.process('l', 'local', dated), [dated])
        self.now += 181
        self.assertEqual(self.engine.process('p', 'cluster', undated), [])
        self.assertEqual(self.engine.snapshot()['undated_frequency_conflicts'], 1)

    def test_non_object_configuration_is_rejected(self):
        for config in (False, True, [], [['enabled', False]], 'off', 0):
            with self.subTest(config=config), self.assertRaises(ValueError):
                SpotProcessor(config)
        self.assertTrue(SpotProcessor(None).config['enabled'])
        self.assertFalse(SpotProcessor({'enabled': False}).config['enabled'])

    def test_config_rejection(self):
        for config in ({'max_entries': 0}, {'max_changes': .5}, {'enabled': 1},
                       {'repeat_seconds': float('nan')}, {'unknown': 1}):
            with self.assertRaises(ValueError):
                SpotProcessor(config)


if __name__ == '__main__':
    unittest.main()
