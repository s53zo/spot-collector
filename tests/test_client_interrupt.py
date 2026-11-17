"""Client interrupts retire one session without reaching shared upstreams."""
import asyncio
import unittest

from test_relay import relay, Writer
from stream_protocol import TelnetDecoder


class CountingReader:
    def __init__(self, parts):
        self.parts = iter(parts)
        self.calls = 0

    async def read(self, size):
        self.calls += 1
        return next(self.parts, b'')


class ClientInterruptTests(unittest.IsolatedAsyncioTestCase):
    async def check_interrupt(self, parts, expected_reads):
        r = relay()
        primary, go = Writer(), Writer()
        r.server_connections['primary'] = (None, primary, '', True, 0)
        r.server_connections['go'] = (None, go, '', True, 0)
        other = Writer()
        r.client_writers.append(other)
        r.client_queues[other] = asyncio.Queue(100)
        client = Writer()
        reader = CountingReader(parts + [b'DX 14020 JA1ABC should-not-be-sent\r\n'])
        await asyncio.wait_for(r.handle_client(reader, client), 1)
        self.assertTrue(client.closed)
        self.assertNotIn(client, r.client_writers)
        self.assertNotIn(client, r.client_queues)
        self.assertEqual(reader.calls, expected_reads)
        self.assertEqual(primary.payloads, [])
        self.assertEqual(go.payloads, [])
        self.assertFalse(r.stopping)
        self.assertFalse(other.closed)
        await r._deliver_lines_to_clients([b'other client still receives'])
        self.assertEqual(r.client_queues[other].get_nowait(), b'other client still receives\r\n')
        await r.stop()

    async def test_raw_ctrl_c_without_newline(self):
        await self.check_interrupt([b'\x03'], 1)

    async def test_interrupt_cancels_partial_and_trailing_command(self):
        await self.check_interrupt([b'DX 14020 JA', b'\x031ABC trailing\r\n'], 2)

    async def test_telnet_interrupt_fragmented_at_every_boundary(self):
        wire = b'\xff\xf4'
        for split in range(1, len(wire) + 1):
            with self.subTest(split=split):
                parts = [wire[:split]]
                if wire[split:]:
                    parts.append(wire[split:])
                await self.check_interrupt(parts, len(parts))

    async def test_completed_commands_before_ctrl_c_keep_order(self):
        r = relay()
        primary = Writer()
        r.server_connections['primary'] = (None, primary, '', True, 0)
        client = Writer()
        await r.handle_client(CountingReader([b'SH/U\r\n\x03SH/DX 5\r\n']), client)
        self.assertEqual(primary.payloads, [b'SH/U\r\n'])
        self.assertTrue(client.closed)
        await r.stop()

    async def test_telnet_option_three_is_not_ctrl_c(self):
        r = relay()
        primary = Writer()
        r.server_connections['primary'] = (None, primary, '', True, 0)
        await r.handle_client(CountingReader([b'\xff\xfb\x03SH/U\r\n']), Writer())
        self.assertEqual(primary.payloads, [b'SH/U\r\n'])
        await r.stop()


    async def test_loopback_disconnect_preserves_other_client(self):
        for interrupt in (b'\x03', b'\xff\xf4'):
            with self.subTest(interrupt=interrupt):
                r = relay()
                server = await asyncio.start_server(r.handle_client, '127.0.0.1', 0)
                r.listener = server
                port = server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                other_reader, other_writer = await asyncio.open_connection('127.0.0.1', port)
                try:
                    await asyncio.wait_for(reader.readexactly(7), 1)  # login prompt
                    await asyncio.wait_for(other_reader.readexactly(7), 1)
                    writer.write(interrupt)
                    await writer.drain()
                    self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
                    self.assertFalse(r.stopping)
                    await r._deliver_lines_to_clients([b'other client stays connected'])
                    self.assertEqual(await asyncio.wait_for(other_reader.readline(), 1),
                                     b'other client stays connected\r\n')
                finally:
                    writer.close()
                    other_writer.close()
                    await writer.wait_closed()
                    await other_writer.wait_closed()
                    await r.stop()


class DecoderInterruptTests(unittest.TestCase):
    def test_interrupt_mapping_is_client_only_and_not_subnegotiation(self):
        self.assertEqual(TelnetDecoder().feed(b'a\xff\xf4b'), b'ab')
        decoder = TelnetDecoder(client_interrupts=True)
        self.assertEqual(decoder.feed(b'a\xff\xf4b'), b'a\x03b')
        self.assertEqual(decoder.feed(b'\xff\xfa\x18\x03\xff\xf0c'), b'c')
