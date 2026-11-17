"""Deferred local-call checks use the normal source-owned fanout path."""
import asyncio
from collections import Counter
import unittest

from test_relay import relay, Writer
from test_spot_processing import START, line, gc


class HeldProcessor:
    def __init__(self):
        self.now = START
        self.clock = lambda: self.now
        self.config = {'enabled': True}
        self.counts = Counter()
        self.last_evaluation = None
        self.pending = None
        self.force_calls = 0

    def process(self, source, role, raw, history=False):
        self.counts.update(received_lines=1, parsed_spots=1, local_call_held=1)
        self.pending = (source, raw)
        self.last_evaluation = {'call': 'S53MM', 'frequency_hz': 7010100,
                               'frequency_basis': 'input', 'reason': 'local-call-pending',
                               'held': True, 'call_candidate': 'S53M', 'call_support': 1}
        return []

    def drain_local_holds(self, force=False):
        self.force_calls += bool(force)
        if self.pending is None or not force and self.now < START + 3:
            return []
        source, raw = self.pending
        self.pending = None
        delta = dict(delivered_spots=1, local_call_released=1, local_call_expired=1)
        self.counts.update(delta)
        evaluation = dict(self.last_evaluation, held=False, reason='local-call-release-original')
        return [dict(source=source, input=raw, outputs=[raw], delta=delta, evaluation=evaluation)]

    def alignment_snapshot(self, sources=None):
        return []


class LocalCallDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setup_relay(self):
        r = relay()
        r.debug_verbose = True
        r.processor = HeldProcessor()
        client = Writer()
        r.client_writers.append(client)
        r.client_queues[client] = asyncio.Queue(100)
        return r, client

    async def test_deadline_timer_releases_without_new_input_and_no_reobservation(self):
        r, client = self.setup_relay()
        raw = line('S53MM', 7010.1, extra=b'Original > Q:9 comment')
        with self.assertLogs(level='DEBUG') as logs:
            await r._process_server_line('local', raw)
            last_spot = r.last_spot_times['local']
            self.assertTrue(r.client_queues[client].empty())
            r.local_call_release_task = asyncio.create_task(r._local_call_release_loop())
            r.processor.now += 3
            output = await asyncio.wait_for(r.client_queues[client].get(), 1.5)
        self.assertEqual(output, raw + b'\r\n')
        self.assertIn('HOLD', logs.output[0])
        self.assertIn('call-candidate=S53M', logs.output[0])
        self.assertIn('call-support=1/2', logs.output[0])
        self.assertIn('SELECT', logs.output[-1])
        self.assertEqual(r.source_spot_counts['local']['parsed'], 1)
        self.assertEqual(r.source_spot_counts['local']['forwarded'], 1)
        self.assertEqual(r.last_spot_times['local'], last_spot)
        self.assertEqual(r.processor.counts['received_lines'], 1)
        await r.stop()
        self.assertIsNone(r.local_call_release_task)

    async def test_stop_resolves_valid_pending_before_client_retirement(self):
        r, client = self.setup_relay()
        raw = line('S53MM', 7010.1)
        with self.assertLogs(level='DEBUG'):
            await r._process_server_line('local', raw)
            await r.stop()
        self.assertEqual(r.processor.force_calls, 1)
        self.assertIsNone(r.processor.pending)
        self.assertEqual(r.source_spot_counts['local']['forwarded'], 1)
        self.assertTrue(client.closed)
        self.assertFalse(r.client_handlers)

    async def test_real_engine_quiet_hold_releases_original_comment(self):
        r = relay()
        now = [START]
        r.processor.clock = lambda: now[0]
        client = Writer()
        r.client_writers.append(client)
        r.client_queues[client] = asyncio.Queue(100)
        await r._process_server_line('go', gc('S53M', 7010, origin='DL1AAA'))
        r.client_queues[client].get_nowait()
        raw = line('S53MM', 7010.1, extra=b'unchanged  spacing \xff')
        await r._process_server_line('local', raw)
        self.assertTrue(r.client_queues[client].empty())
        before = r.processor.counts['received_lines']
        r.local_call_release_task = asyncio.create_task(r._local_call_release_loop())
        now[0] += 3
        selected = await asyncio.wait_for(r.client_queues[client].get(), 1.5)
        self.assertEqual(selected, raw + b'\r\n')
        self.assertEqual(r.processor.counts['received_lines'], before)
        await r.stop()

    async def test_real_engine_second_external_receiver_resolves_early(self):
        r = relay()
        r.processor.clock = lambda: START
        client = Writer()
        r.client_writers.append(client)
        r.client_queues[client] = asyncio.Queue(100)
        await r._process_server_line('go', gc('S53M', 7010, origin='DL1AAA'))
        r.client_queues[client].get_nowait()
        raw = line('S53MM', 7010.1, extra=b'unchanged original')
        await r._process_server_line('local', raw)
        self.assertTrue(r.client_queues[client].empty())
        last = r.last_spot_times['local']
        await r._process_server_line('primary', line('S53M', 7010, origin='F1BBB'))
        self.assertEqual(r.processor.snapshot()['current_local_call_holds'], 0)
        self.assertEqual(r.processor.counts['local_call_corrected'], 1)
        self.assertEqual(r.processor.counts['received_lines'], 3)
        self.assertEqual(r.source_spot_counts['local']['parsed'], 1)
        self.assertEqual(r.last_spot_times['local'], last)
        self.assertTrue(r.client_queues[client].empty())  # corrected call is an existing repeat
        await r.stop()

    async def test_startup_owns_and_joins_release_timer(self):
        r = relay()
        for source in r.server_definitions:
            source['enabled'] = False
        runner = asyncio.create_task(r.start_relay())
        try:
            for _ in range(100):
                if r.listener is not None:
                    break
                await asyncio.sleep(.005)
            self.assertIsNotNone(r.local_call_release_task)
            task = r.local_call_release_task
            await asyncio.wait_for(r.stop(), 1)
            self.assertTrue(task.done())
            self.assertIsNone(r.local_call_release_task)
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    async def test_stop_joins_producer_before_final_hold_drain(self):
        r, client = self.setup_relay()
        raw = line('S53MM', 7010.1)
        started = asyncio.Event()
        async def producer():
            started.set()
            try:
                await asyncio.Future()
            finally:
                r.processor.pending = ('local', raw)
                r.processor.last_evaluation = {'call': 'S53MM', 'frequency_hz': 7010100,
                    'frequency_basis': 'input', 'reason': 'local-call-pending', 'held': True}
        task = asyncio.create_task(producer())
        r.connector_tasks['local'] = task
        await started.wait()
        with self.assertLogs(level='DEBUG'):
            await asyncio.wait_for(r.stop(), 1)
        self.assertTrue(task.done())
        self.assertIsNone(r.processor.pending)
        self.assertEqual(r.source_spot_counts['local']['forwarded'], 1)
