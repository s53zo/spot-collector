"""Status and verbose logging must describe, never alter, spot delivery."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_relay import relay_module, definitions, Writer
from test_spot_processing import START, line, gc


class ReportingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def make_relay(self, verbose=False, processing=None):
        r = relay_module.TelnetRelay(definitions(), {}, 8001, 'TK0C', 'login: ',
                                    processing_config=processing, debug_verbose=verbose)
        r.processor.clock = lambda: START
        client = Writer()
        r.client_writers.append(client)
        r.client_queues[client] = asyncio.Queue(100)
        return r, client

    async def test_verbose_repeat_trace_does_not_change_client_output(self):
        r, client = self.make_relay(True)
        raw = line(frequency=14020.12, extra=b'original Q:3 > text \xff\x1b')
        with self.assertLogs(level='DEBUG') as logs:
            await r._process_server_line('go', gc())
            await r._process_server_line('local', raw)
        self.assertEqual(len(logs.output), 2)
        trace = logs.output[-1]
        self.assertNotIn('\n', trace)
        self.assertNotIn('\x1b', trace)
        for expected in ('src=local', 'DROP', 'reason=repeat', 'freq-basis=input',
                         '14020.120', 'align-reason=missing-reference', '\\xff', '\\x1b'):
            self.assertIn(expected, trace)
        queue = r.client_queues[client]
        self.assertEqual(queue.get_nowait(), gc() + b'\r\n')
        self.assertTrue(queue.empty())
        self.assertEqual(r.source_spot_counts['local']['repeats'], 1)
        await r.stop()

    async def test_status_reports_new_flow_without_mutating_decisions(self):
        r, client = self.make_relay()
        now = time.time()
        r.server_connections['go'] = (None, None, '', True, now)
        r.handshake_sent['go'] = True
        with patch.object(relay_module.logging, 'debug') as debug:
            await r._process_server_line('go', gc())
            await r._process_server_line('local', line())
            self.assertFalse(debug.called)
        before = r.processor.snapshot()
        report = r._build_status_message()
        self.assertEqual(r.processor.snapshot(), before)
        for expected in ('Spot flow', 'Selected: 1', 'Repeats suppressed: 1',
                         'Receiver frequency alignment', 'Feeds', 'Login sent', 'local'):
            self.assertIn(expected, report)
        self.assertNotIn('Processing: {', report)
        self.assertIn('Collecting', report)
        self.assertIn('corroborated GoCluster', report)
        self.assertEqual(r.source_spot_counts['go']['forwarded'], 1)
        await r.stop()

    async def test_bypass_and_control_bytes_stay_single_line(self):
        r, client = self.make_relay(True, {'enabled': False})
        raw = line(extra=b'control\tbyte\x1b > stays')
        with self.assertLogs(level='DEBUG') as logs:
            await r._process_server_line('local', raw)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('BYPASS reason=processing-off', logs.output[0])
        self.assertNotIn('\t', logs.output[0])
        self.assertNotIn('\x1b', logs.output[0])
        self.assertEqual(r.client_queues[client].get_nowait(), raw + b'\r\n')
        self.assertEqual(r.source_spot_counts['local']['parsed'], 0)
        self.assertIsNotNone(r.last_spot_times['local'])
        await r.stop()

    async def test_history_verbose_parser_failure_does_not_change_bypass(self):
        r, client = self.make_relay(True)
        r.history_until = time.monotonic() + 5
        r.history_remaining = 200
        raw = line(report='9' * 5000 + ' dB')
        with self.assertLogs(level='DEBUG') as logs:
            await r._process_server_line('primary', raw)
        self.assertIn('BYPASS reason=history', logs.output[0])
        self.assertEqual(r.client_queues[client].get_nowait(), raw + b'\r\n')
        await r.stop()


    async def test_verbose_explains_pending_move_and_receiver_specific_samples(self):
        from collections import Counter
        r, client = self.make_relay(True)
        r.server_definitions_by_name['local']['role'] = 'rbn'
        original = r.processor
        class Processor:
            counts = Counter()
            config = original.config
            clock = staticmethod(original.clock)
            last_evaluation = None
            def process(self, source, role, raw, history=False):
                self.counts['received_lines'] += 1
                self.counts['parsed_spots'] += 1
                self.counts['pending_moves'] += 1
                self.last_evaluation = dict(call='JA1ABC', frequency_hz=14020700,
                                            frequency_basis='input', reason='pending-corroboration',
                                            anchor_hz=14020000, candidate_hz=14020700,
                                            support=1, required=2, reference_basis='gocluster', receiver='TK0C-1')
                return []
            def alignment_snapshot(self, sources):
                return [dict(source='local', receiver='OTHER', band='20M', state='Ready', samples=9,
                             required=5, retained_samples=9, offset_hz=100, rejection_reason=None),
                        dict(source='local', receiver='TK0C-1', band='20M', state='Collecting', samples=0,
                             required=5, retained_samples=2, offset_hz=None, rejection_reason='missing-reference')]
        r.processor = Processor()
        with self.assertLogs(level='DEBUG') as logs:
            await r._process_server_line('local', line(frequency=14020.7))
        trace = logs.output[0]
        for expected in ('DROP reason=pending-corroboration', 'anchor=14020.000kHz',
                         'candidate=14020.700kHz', 'support=1/2', 'reference=gocluster',
                         'samples=0/5', 'retained=2', 'align-reason=missing-reference'):
            self.assertIn(expected, trace)
        self.assertNotIn('adjust=+100', trace)
        self.assertNotIn('\n', trace)
        self.assertTrue(r.client_queues[client].empty())
        await r.stop()


    async def test_actual_ha8lnn_outlier_trace_and_status(self):
        r, client = self.make_relay(True)
        now = [START]
        r.processor.clock = lambda: now[0]
        first = gc('HA8LNN', 7009.0, origin='W3DAN', confidence='S')
        outlier = gc('HA8LNN', 7009.7, origin='IZ3EAW')
        returned = gc('HA8LNN', 7009.0, origin='HB9FX')
        with self.assertLogs(level='DEBUG') as logs:
            await r._process_server_line('go', first)
            now[0] += 1
            await r._process_server_line('go', outlier)
            now[0] += 1
            await r._process_server_line('go', returned)
        trace = next(item for item in logs.output if 'IZ3EAW' in item)
        self.assertIn('DROP reason=pending-corroboration', trace)
        self.assertIn('support=1/2', trace)
        self.assertIn('anchor=7009.000kHz', trace)
        queue = r.client_queues[client]
        self.assertEqual(queue.get_nowait(), first + b'\r\n')
        self.assertTrue(queue.empty())
        report = r._build_status_message()
        self.assertIn('Confirmed 0', report)
        self.assertIn('Receiver frequency alignment', report)
        self.assertIn('IZ3EAW', report)
        self.assertEqual(r.processor.snapshot().get('frequency_change_updates', 0), 0)
        await r.stop()


class VerboseConfigurationTests(unittest.TestCase):
    def test_cli_and_json_verbose_option(self):
        args = relay_module._create_parser(False).parse_args(['--debug-verbose'])
        self.assertTrue(args.debug_verbose)
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'settings.json'
            data = dict(listen_port=8001, callsign='TK0C', debug_verbose=True,
                        servers=[dict(address='localhost:7300')])
            config.write_text(json.dumps(data))
            with patch('sys.argv', ['spot-collector.py', '--config', str(config)]):
                self.assertTrue(relay_module.parse_arguments().debug_verbose)
            data['debug_verbose'] = 'yes'
            config.write_text(json.dumps(data))
            with patch('sys.argv', ['spot-collector.py', '--config', str(config)]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    relay_module.parse_arguments()

    def test_constructor_rejects_non_boolean_verbose(self):
        with self.assertRaises(ValueError):
            relay_module.TelnetRelay(definitions(), {}, 8001, 'TK0C', 'login: ', debug_verbose=1)
