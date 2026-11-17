import asyncio
import unittest
from unittest.mock import patch
from test_relay import relay, Writer


class ShortStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_short_longstatus_detailed_and_both_local(self):
        r=relay();client=Writer();primary=Writer()
        r.client_writers.append(client);r.client_queues[client]=asyncio.Queue(10)
        r.server_connections['primary']=(None,primary,'',True,0)
        with patch.object(r.processor,'alignment_snapshot',wraps=r.processor.alignment_snapshot) as alignment:
            await r._handle_command(client,b'STATUS')
            short=r.client_queues[client].get_nowait().decode()
            self.assertLessEqual(len(short.strip().splitlines()),5)
            self.assertIn('longstatus',short)
            self.assertNotIn('Receiver frequency alignment',short)
            alignment.assert_not_called()
            await r._handle_command(client,b'longstatus')
            detailed=r.client_queues[client].get_nowait().decode()
            self.assertIn('Receiver frequency alignment',detailed)
            self.assertIn('Feeds (process lifetime)',detailed)
            alignment.assert_called_once()
        self.assertEqual(primary.payloads,[])
        await r.stop()

    async def test_periodic_broadcast_uses_short_report(self):
        r=relay();client=Writer();r.client_writers.append(client)
        r.client_queues[client]=asyncio.Queue(10)
        async def end(*args): raise asyncio.CancelledError()
        with patch('test_relay.relay_module.asyncio.sleep',side_effect=end):
            with self.assertRaises(asyncio.CancelledError):
                await r.send_status_to_clients()
        short=r.client_queues[client].get_nowait().decode()
        self.assertLessEqual(len(short.strip().splitlines()),5)
        await r.stop()
