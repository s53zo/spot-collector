import copy
import unittest

from status_report import format_status


def report(**overrides):
    values = dict(uptime='1d 02:03:04', listen_port=7300, primary='primary',
                  client_count=2, client_drops=0, processing_enabled=True,
                  counters={}, sources=[], alignments=[],
                  policy=dict(repeat_seconds=180, move_tolerance_hz=50,
                              max_changes=2))
    values.update(overrides)
    return format_status(**values)


class StatusReportTests(unittest.TestCase):
    def test_empty_report_is_explicit_and_deterministic(self):
        result = report()
        self.assertEqual(result, report())
        self.assertIn('Uptime: 1d 02:03:04 | Listen: 7300 | Primary: primary', result)
        self.assertIn('Clients: 2 | Queue-full disconnects: 0 | Processing: enabled', result)
        self.assertIn('Received lines: 0 (all lines) | Parsed spots: 0', result)
        self.assertIn('Selected: 0 | Repeats suppressed: 0 | Rejected: 0', result)
        self.assertIn('No receiver alignment observations yet.', result)
        self.assertIn('No feeds configured.', result)
        self.assertTrue(result.endswith('\n'))
        self.assertNotIn("{'", result)

    def test_mixed_feeds_include_disabled_zero_and_connection_details(self):
        sources = [
            dict(name='local', role='local', state='TCP up', address='127.0.0.1:7000',
                 direction='in', up='1h 2m', parsed=120, forwarded=70, repeats=40,
                 rejected=10, last_spot='0s ago'),
            dict(name='reference', role='gocluster', state='Login sent',
                 address='reference.example.test:7300', direction='in', up='4m',
                 parsed=80, forwarded=20, repeats=60, rejected=0, last_spot='2s ago'),
            dict(name='primary', role='cluster', state='Down', address='primary:7300',
                 direction='both', up='-', parsed=2, forwarded=2, repeats=0,
                 rejected=0, last_spot='10m ago'),
            dict(name='optional', role='cluster', state='Disabled', address='optional:7300',
                 direction='in', up='-', parsed=0, forwarded=0, repeats=0,
                 rejected=0, last_spot=None),
        ]
        before = copy.deepcopy(sources)
        result = report(sources=sources)
        self.assertEqual(sources, before)
        self.assertRegex(result, r'local\s+local\s+TCP up\s+120\s+70\s+40\s+10\s+0s ago')
        self.assertRegex(result, r'optional\s+cluster\s+Disabled\s+0\s+0\s+0\s+0\s+never')
        self.assertIn('reference.example.test:7300 | Direction: in | Up: 4m', result)
        self.assertIn('Login sent', result)
        self.assertNotIn('authenticated', result.lower())
        self.assertLess(result.index('TCP up'), result.index('Login sent'))

    def test_unavailable_zero_and_negative_adjustment_are_distinct(self):
        result = report(alignments=[
            dict(source='a', band='20m', state='Collecting', samples=0, required=5,
                 offset_hz=None, last_match_age_seconds=None),
            dict(source='b', band='40m', state='Ready', samples=5, required=5,
                 offset_hz=0.0, last_match_age_seconds=0),
            dict(source='c', band='80m', state='Ready', samples=7, required=5,
                 offset_hz=-120.0, last_match_age_seconds=2),
            dict(source='d', band='10m', state='Expired', samples=None, required=5,
                 offset_hz=None, last_match_age_seconds=-1),
        ])
        self.assertRegex(result, r'a\s+-\s+20m\s+Collecting\s+0/5\s+0\s+n/a\s+never')
        self.assertRegex(result, r'b\s+-\s+40m\s+Ready\s+5/5\s+5\s+\+0\.0 Hz\s+0s')
        self.assertRegex(result, r'c\s+-\s+80m\s+Ready\s+7/5\s+7\s+-120\.0 Hz\s+2s')
        self.assertRegex(result, r'd\s+-\s+10m\s+Expired\s+n/a/5\s+n/a\s+n/a\s+-1s')
        self.assertIn('corroborated GoCluster', result)
        self.assertNotIn('%', result)

    def test_changes_do_not_imply_delivery_or_only_selected_corrections(self):
        result = report(counters=dict(
            received_lines=1000, parsed_spots=20, delivered_spots=0,
            suppressed_repeats=14, stale_spots=3, out_of_order_spots=2,
            undated_frequency_conflicts=1, frequency_corrections=12,
            callsign_corrections=4, correction_unavailable=2,
            frequency_change_updates=0))
        self.assertIn('Received lines: 1,000 (all lines) | Parsed spots: 20', result)
        self.assertIn('Selected: 0 | Repeats suppressed: 14 | Rejected: 6', result)
        self.assertIn('stale/future 3, out of order 2, undated move 1', result)
        self.assertIn('Frequency corrections: 12 | Callsign corrections: 4', result)
        self.assertIn('evaluated before repeat suppression', result)
        self.assertIn('include spots subsequently suppressed as repeats', result)
        self.assertIn('Selected = admitted for client fanout; client receipt is not measured.', result)
        self.assertIn('exclude history and processing bypass', result)

    def test_bypassed_processing_can_still_have_recent_source_activity(self):
        result = report(processing_enabled=False, sources=[
            dict(name='local', role='local', state='TCP up', parsed=0,
                 forwarded=0, repeats=0, rejected=0, last_spot='0s ago')])
        self.assertIn('Processing: disabled (bypass)', result)
        self.assertRegex(result, r'local\s+local\s+TCP up\s+0\s+0\s+0\s+0\s+0s ago')
        self.assertIn('including activity while processing is bypassed', result)

    def test_units_column_alignment_and_long_address_details(self):
        result = report(sources=[
            dict(name='local', role='local', state='TCP up', parsed=12345,
                 forwarded=7, repeats=12338, rejected=0, last_spot='2s ago',
                 address='a-long-' + 'example-' * 18 + '.test:7300',
                 direction='in', up='1d'),
            dict(name='reference', role='gocluster', state='Down', parsed=0,
                 forwarded=0, repeats=0, rejected=0, last_spot='never',
                 address='reference:7300', direction='in', up='-'),
        ])
        self.assertIn('Repeat window: 180 s | Move tolerance: 50 Hz | Max updates: 2', result)
        rows = [line for line in result.splitlines()
                if line.startswith('  local ') or line.startswith('  reference ')]
        self.assertEqual(rows[0].index('12,345'), rows[1].index('0') - 5)
        self.assertLessEqual(max(map(len, result.splitlines())), 104)
        self.assertNotIn('Address:', rows[0])
        self.assertIn('      ', result)

    def test_missing_policy_is_unavailable_and_text_remains_plain_ascii(self):
        result = report(uptime='\x1b[31m1h\x1b[0m\nX', primary='primary\t\u2603', policy={})
        self.assertIn(r'Uptime: \x1b[31m1h\x1b[0m\nX', result)
        self.assertIn(r'Primary: primary\t\u2603', result)
        self.assertIn('Repeat window: n/a | Move tolerance: n/a | Max updates: n/a', result)
        self.assertTrue(all(character == '\n' or 32 <= ord(character) <= 126
                            for character in result))
        self.assertNotIn('\x1b', result)


    def test_per_receiver_rows_show_retained_vs_usable_and_rejection_reason(self):
        result = report(counters=dict(current_pending_moves=1, confirmed_moves=2,
                                      expired_moves=3, move_conflicts=4), alignments=[
            dict(source='Server2', receiver='IZ3EAW', band='40M', state='Collecting',
                 samples=0, required=5, retained_samples=2, excluded_samples=2,
                 rejection_reason='self/mirror', offset_hz=None, last_match_age_seconds=None),
            dict(source='Server2', receiver='HB9FX', band='40M', state='Ready',
                 samples=5, required=5, retained_samples=7, excluded_samples=0,
                 rejection_reason=None, offset_hz=-700, last_match_age_seconds=1),
        ])
        self.assertIn('IZ3EAW', result)
        self.assertIn('HB9FX', result)
        self.assertIn('self/mirror', result)
        self.assertIn('Excluded samples: 2', result)
        self.assertRegex(result, r'IZ3EAW\s+40M\s+Collecting\s+0/5\s+2\s+n/a')
        self.assertIn('pending now 1 | Confirmed 2 | Expired 3 | Conflicts 4', result)

    def test_public_rbn_alignment_rows_are_bounded_and_prioritize_usable_evidence(self):
        rows = [dict(source='RBN', receiver='A%dAA' % index, band='20M', state='Collecting',
                     samples=0, required=5, offset_hz=None, last_match_age_seconds=None)
                for index in range(60)]
        rows.append(dict(source='RBN', receiver='Z9READY', band='20M', state='Ready',
                         samples=8, required=5, offset_hz=0, last_match_age_seconds=0))
        result = report(alignments=rows)
        self.assertIn('Z9READY', result)
        self.assertIn('Showing 40 of 61 receiver/band rows; 21 omitted.', result)
        self.assertEqual(len([line for line in result.splitlines() if line.startswith('  RBN ')]), 40)


if __name__ == '__main__':
    unittest.main()
