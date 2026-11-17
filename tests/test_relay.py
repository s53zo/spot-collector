"""Transport/consumer fixtures use fake streams or loopback only, not N1MM."""
import asyncio
import importlib.util
import pathlib
import socket
import io
import json
import runpy
import sys
import tempfile
import contextlib
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('spot_collector_relay', ROOT / 'spot-collector.py')
relay_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay_module)


def definitions():
    return [dict(name='primary', address='127.0.0.1:1', direction='both', role='cluster'),
            dict(name='local', address='127.0.0.1:2', direction='in', role='local'),
            dict(name='go', address='127.0.0.1:3', direction='in', role='gocluster', callsign='TK0C-23')]


def relay():
    return relay_module.TelnetRelay(definitions(), {}, 0, 'TK0C-9', 'login: ')


class Writer:
    def __init__(self, blocked=False):
        self.payloads = []
        self.closed = False
        self.blocked = blocked

    def is_closing(self):
        return self.closed

    def write(self, data):
        self.payloads.append(data)

    async def drain(self):
        if self.blocked:
            await asyncio.Future()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        if self.blocked:
            await asyncio.Future()

    def get_extra_info(self, name):
        return None


class Reader:
    def __init__(self, parts):
        self.parts = iter(parts)

    async def read(self, size):
        return next(self.parts, b'')


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_sources_never_interleave_inside_spot(self):
        r = relay()
        client = Writer()
        r.client_writers.append(client)
        r.client_queues[client] = asyncio.Queue(100)
        local = b'DX de TK0C-1-#: 14020.12 JA1ABC CW 12 dB CQ 1200Z'
        remote = b'DX de DL1AAA: 14040.00 K1ABC CW 20 dB CQ 1200Z'
        # Freeze wall time so wire-age admission is deterministic.
        r.processor.clock = lambda: 1735732800.0  # 2025-01-01 12:00 UTC
        await r._process_rbn_data('local', local[:20])
        await r._process_rbn_data('primary', remote + b'\r\n')
        await r._process_rbn_data('local', local[20:] + b'\r\n')
        q = r.client_queues[client]
        self.assertEqual([q.get_nowait(), q.get_nowait()], [remote + b'\r\n', local + b'\r\n'])
        await r.stop()

    async def test_fragmented_login_and_explicit_identity(self):
        r = relay()
        writer = Writer()
        await r.relay_server_data(Reader([b'lo', b'gin: ']), writer, 'go')
        self.assertEqual(writer.payloads, [b'TK0C-23\r\n'])
        self.assertTrue(r.handshake_sent['go'])
        other = Writer()
        r.handshake_sent['local'] = False
        await r.relay_server_data(Reader([b'Welcome, all callsigns accepted.\r\n']), other, 'local')
        self.assertEqual(other.payloads, [])

    async def test_local_commands_and_outbound_route(self):
        r = relay()
        primary, go, local = Writer(), Writer(), Writer()
        for name, writer in [('primary', primary), ('go', go), ('local', local)]:
            r.server_connections[name] = (None, writer, '', True, 0)
        downstream = Writer()
        await r.handle_client(Reader([b'sta', b'tus\r\nuptime\r\nDX 14020 JA1ABC CQ\r\n']), downstream)
        self.assertEqual(primary.payloads, [b'DX 14020 JA1ABC CQ\r\n'])
        self.assertEqual(go.payloads, [])
        self.assertEqual(local.payloads, [])
        await r.stop()

    async def test_history_bypass_is_bounded_and_primary_only(self):
        r = relay()
        primary = Writer()
        r.server_connections['primary'] = (None, primary, '', True, 0)
        await r._handle_command(Writer(), b'SH/DX 10')
        self.assertEqual(r.history_remaining, 200)
        seen = []
        class Processor:
            def process(self, source, role, line, history=False):
                seen.append((source, history))
                return [line]
        r.processor = Processor()
        await r._process_server_line('primary', b'DX de A: old')
        await r._process_server_line('local', b'DX de A: new')
        r.history_remaining = 0
        await r._process_server_line('primary', b'DX de A: new')
        self.assertEqual(seen, [('primary', True), ('local', False), ('primary', False)])

    async def test_slow_client_removal_does_not_wait_for_close(self):
        r = relay()
        slow, fast = Writer(True), Writer()
        for writer in (slow, fast):
            r.client_writers.append(writer)
            r.client_queues[writer] = asyncio.Queue(1)
        r.client_queues[slow].put_nowait(b'full')
        await asyncio.wait_for(r._deliver_lines_to_clients([b'new']), .2)
        self.assertTrue(slow.closed)
        self.assertEqual(r.client_queues[fast].get_nowait(), b'new\r\n')
        self.assertEqual(r.client_drop_count, 1)
        for task in list(r.cleanup_tasks):
            task.cancel()
        await r.stop()

    async def test_disabled_timeout_and_connector_eof_closes_writer(self):
        r = relay()
        self.assertIsNone(r.server_timeout)
        writer = Writer()
        async def dial(*args):
            r.stopping = True
            return Reader([]), writer
        async def end_sleep(*args):
            r.stopping = True
        with patch.object(relay_module.asyncio, 'open_connection', dial), patch.object(relay_module.asyncio, 'sleep', end_sleep):
            await r._maintain_connection(definitions()[0])
        self.assertTrue(writer.closed)
        self.assertFalse(r.server_connections['primary'][3])

    async def test_client_admission_and_cleanup_owners_have_hard_caps(self):
        r = relay()
        dummy = object()
        r.client_handlers = {dummy for _ in range(1)}
        # Distinct retained owners fill the configured transport admission cap.
        r.client_handlers = set(range(128))
        rejected = Writer(True)
        await asyncio.wait_for(r.handle_client(Reader([]), rejected), .2)
        self.assertTrue(rejected.closed)
        self.assertFalse(r.cleanup_tasks)
        r.client_handlers.clear()
        r.cleanup_tasks = set(range(128))
        extra = Writer(True)
        await r._cleanup_client(extra)
        self.assertEqual(len(r.cleanup_tasks), 128)
        r.cleanup_tasks.clear()

    async def test_connect_wakes_existing_owner_without_task_churn(self):
        r = relay()
        owner = asyncio.create_task(asyncio.Event().wait())
        r.connector_tasks['primary'] = owner
        for _ in range(100):
            r._start_connector(definitions()[0], force_restart=True)
        self.assertIs(r.connector_tasks['primary'], owner)
        self.assertTrue(r.reconnect_events['primary'].is_set())
        self.assertFalse(r.cleanup_tasks)
        await r.stop()

    async def test_setup_commands_wait_for_explicit_ready_greeting(self):
        r = relay()
        r.server_definitions_by_name['go'].update(ready_text='READY TK0C-23', startup_commands=['SET DIAG OFF'])
        writer = Writer()
        await r.relay_server_data(Reader([b'login: ', b'READY TK0C-23\r\nREADY TK0C-23\r\n']), writer, 'go')
        self.assertEqual(writer.payloads, [b'TK0C-23\r\n', b'SET DIAG OFF\r\n'])

    async def test_loopback_n1mm_compatible_consumer_and_join(self):
        r = relay()
        server = await asyncio.start_server(r.handle_client, '127.0.0.1', 0)
        r.listener = server
        reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
        await reader.readexactly(len(b'login: '))
        wire = b'DX de TK0C-1-#: 14020.12 JA1ABC CW 12 dB CQ 1200Z'
        r.processor.clock = lambda: 1735732800.0
        await r._process_rbn_data('local', wire[:17])
        await r._process_rbn_data('local', wire[17:] + b'\r\n')
        # The existing primary and GC return the same local observation.
        primary_copy = b'DX de TK0C-1: 14020.12 JA1ABC CW 12 dB CQ 1200Z'
        await r._process_rbn_data('primary', primary_copy + b'\r\n')
        def gc_line(call, frequency, origin):
            prefix = b'DX de ' + origin + b':'
            header = prefix + b' ' * (25 - len(prefix) - len(frequency)) + frequency + b'  ' + call
            return header.ljust(39) + b'CW 20 dB'.ljust(25) + b'JN58 V 1200Z'
        go_copy = gc_line(b'JA1ABC', b'14020.12', b'TK0C-1')
        unique_gc = gc_line(b'K1ABC', b'14040.12', b'DL1AAA')
        self.assertEqual(len(unique_gc), 76)
        await r._process_rbn_data('go', go_copy + b'\r\n' + unique_gc + b'\r\n')
        first = await asyncio.wait_for(reader.readline(), 1)
        second = await asyncio.wait_for(reader.readline(), 1)
        self.assertEqual(first, wire + b'\r\n')
        self.assertEqual(second, unique_gc + b'\r\n')
        self.assertEqual(first.split()[3:5], [b'14020.12', b'JA1ABC'])
        self.assertEqual(second[64:76], b'JN58 V 1200Z')
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readline(), .05)
        writer.close()
        await writer.wait_closed()
        await r.stop()
        self.assertFalse(r.client_writers)
        self.assertFalse(r.client_handlers)


    async def test_postlogin_prompts_do_not_swallow_or_bypass_first_spot(self):
        raw = b'DX de DL1AAA: 14020.00 JA1ABC CW 12 dB comment > stays 1200Z'
        prompt = b'TK0C de CLUSTER 16-Nov-2026 12:00 UTC> '
        for parts in ([b'login: ', prompt, raw+b'\r\n', raw+b'\r\n'],
                      [b'login: ', prompt+raw+b'\r\n'+raw+b'\r\n'],
                      [b'login: ', prompt[:12], prompt[12:]+raw[:8], raw[8:]+b'\r\n'+raw+b'\r\n']):
            r=relay(); r.processor.clock=lambda:1735732800.0
            client=Writer();r.client_writers.append(client);r.client_queues[client]=asyncio.Queue(100)
            await r.relay_server_data(Reader(parts),Writer(),'primary')
            payloads=[]
            while not r.client_queues[client].empty():payloads.append(r.client_queues[client].get_nowait())
            self.assertEqual([v for v in payloads if b'DX de ' in v],[raw+b'\r\n'])
            self.assertTrue(any(v.rstrip()==prompt.rstrip() for v in payloads))
            await r.stop()

    async def test_login_and_postlogin_prompt_all_split_positions(self):
        login=b'login: '
        prompt=b'TK0C de CLUSTER 16-Nov-2026 12:00 UTC> '
        spot=b'DX de DL1AAA: 14020.00 JA1ABC CW 12 dB comment > stays 1200Z'
        after_login=prompt+spot+b'\r\n'+spot+b'\r\n'
        # Greeting follows the login exchange, but either side can split at
        # every byte boundary, including immediately after the login colon.
        for login_split in range(len(login)+1):
            for split in range(len(after_login)+1):
                r=relay();r.processor.clock=lambda:1735732800.0
                client=Writer();upstream=Writer()
                r.client_writers.append(client);r.client_queues[client]=asyncio.Queue(100)
                parts=[part for part in (login[:login_split],login[login_split:],
                                         after_login[:split],after_login[split:]) if part]
                try:
                    await r.relay_server_data(Reader(parts),upstream,'primary')
                    payloads=[]
                    while not r.client_queues[client].empty():
                        payloads.append(r.client_queues[client].get_nowait())
                    self.assertEqual(upstream.payloads,[b'TK0C-9\r\n'],(login_split,split))
                    self.assertEqual([v for v in payloads if b'DX de ' in v],
                                     [spot+b'\r\n'],(login_split,split))
                finally:
                    await r.stop()

    async def test_telnet_iac_preserved_to_client_and_primary(self):
        from stream_protocol import TelnetDecoder
        r=relay();r.processor.clock=lambda:1735732800.0
        client=Writer();queue=asyncio.Queue(100)
        r.client_writers.append(client);r.client_queues[client]=queue
        sender=asyncio.create_task(r._client_sender(client,queue,'client'))
        r.client_sender_tasks[client]=sender
        application=b'DX de DL1AAA: 14020 JA1ABC CW 12 dB byte \xffX tail 1200Z'
        await r._process_rbn_data('primary',application.replace(b'\xff',b'\xff\xff')+b'\r\n')
        for _ in range(20):
            if client.payloads:break
            await asyncio.sleep(0)
        self.assertEqual(TelnetDecoder().feed(b''.join(client.payloads)),application+b'\r\n')
        primary=Writer();r.server_connections['primary']=(None,primary,'',True,0)
        command=b'DX 14020 JA1ABC byte \xffX'
        downstream=Writer()
        await r.handle_client(Reader([command.replace(b'\xff',b'\xff\xff')+b'\r\n']),downstream)
        self.assertEqual(TelnetDecoder().feed(b''.join(primary.payloads)),command+b'\r\n')
        await asyncio.wait_for(r.stop(),1)

    async def test_failed_login_closes_epoch_and_reconnects(self):
        r=relay();attempts=[]
        class FailedWriter(Writer):
            async def drain(self):raise OSError('failed login')
        class WaitingReader:
            first=True
            async def read(self,size):
                if self.first:self.first=False;return b'login: '
                await asyncio.Future()
        first=FailedWriter();second=Writer()
        async def dial(*args):
            attempts.append(1)
            if len(attempts)==1:return WaitingReader(),first
            r.stopping=True
            return Reader([]),second
        r.reconnect_events['primary'].set()
        with patch.object(relay_module.asyncio,'open_connection',dial):
            await asyncio.wait_for(r._maintain_connection(definitions()[0]),1)
        self.assertEqual(len(attempts),2)
        self.assertTrue(first.closed);self.assertTrue(second.closed)
        self.assertFalse(r.server_connections['primary'][3])

    async def test_cancelled_close_aborts_and_joins_owned_waiter(self):
        r=relay();started=asyncio.Event();finished=asyncio.Event()
        class Transport:
            aborted=False
            def abort(self):self.aborted=True
        class ClosingWriter(Writer):
            transport=Transport()
            async def wait_closed(self):
                started.set()
                try:await asyncio.Future()
                finally:finished.set()
        writer=ClosingWriter()
        task=asyncio.create_task(r._close_writer(writer))
        await asyncio.wait_for(started.wait(),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task,1)
        self.assertTrue(writer.transport.aborted)
        self.assertTrue(finished.is_set())

    async def test_buffered_slow_client_close_aborts_and_retires_handler(self):
        r=relay()
        server=await asyncio.start_server(r.handle_client,'127.0.0.1',0)
        r.listener=server
        reader,peer=await asyncio.open_connection('127.0.0.1',server.sockets[0].getsockname()[1])
        await reader.readexactly(len(b'login: '))
        peer.transport.pause_reading()
        writer=r.client_writers[0]
        writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,4096)
        writer.write(b'x'*(8*1024*1024))
        self.assertGreater(writer.transport.get_write_buffer_size(),0)
        try:
            with patch.object(relay_module,'DRAIN_TIMEOUT',.02):
                await r._cleanup_client(writer)
                deadline=asyncio.get_running_loop().time()+2
                while r.cleanup_tasks or r.client_handlers:
                    self.assertLess(asyncio.get_running_loop().time(),deadline,
                                    'socket or handler survived bounded cleanup')
                    await asyncio.sleep(.005)
            self.assertEqual(writer.get_extra_info('socket').fileno(),-1)
            self.assertTrue(writer.transport.is_closing())
            self.assertFalse(r.client_sender_tasks)
        finally:
            peer.transport.abort()
            await peer.wait_closed()
            await r.stop()


class ConfigTests(unittest.TestCase):
    def test_explicit_roles_logins_and_disabled_placeholder(self):
        args = relay_module._create_parser(False).parse_args([])
        args.servers_config_list = [dict(name='primary', address='localhost:7300'),
                                    dict(name='gc', address='example.invalid:7300', role='gocluster', direction='in', callsign='TK0C-23', enabled=False)]
        result = relay_module.build_server_definitions(args)
        self.assertEqual(result[1]['callsign'], 'TK0C-23')
        self.assertFalse(result[1]['enabled'])
        args.servers_config_list[1]['direction'] = 'both'
        with self.assertRaises(ValueError):
            relay_module.build_server_definitions(args)

    def test_invalid_startup_and_duplicate_names(self):
        args = relay_module._create_parser(False).parse_args([])
        args.servers_config_list = [dict(address='localhost:7300', startup_commands=['SET DIAG OFF'])]
        with self.assertRaises(ValueError):
            relay_module.build_server_definitions(args)
        args.servers_config_list = [dict(name='a', address='localhost:7300'), dict(name='A', address='localhost:7301')]
        with self.assertRaises(ValueError):
            relay_module.build_server_definitions(args)


    def test_nonobject_processing_rejected_in_cli_and_json_before_dial(self):
        for value in (None, False, [], [['enabled', False]], 'wrong'):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                config=pathlib.Path(folder)/'bad.json'
                config.write_text(json.dumps(dict(servers=definitions(),listen_port=8000,
                                                  callsign='TK0C',spot_processing=value)))
                for argv in ([str(ROOT/'spot-collector.py'),'--config',str(config)],
                             [str(ROOT/'spot-collector.py'),'--server1','localhost:1','--server2','localhost:2',
                              '--listen-port','8000','--callsign','TK0C','--spot-processing',json.dumps(value)]):
                    with patch.object(sys,'argv',argv), contextlib.redirect_stderr(io.StringIO()), \
                         patch.object(asyncio,'open_connection') as dial, \
                         patch.object(asyncio,'start_server') as listener:
                        with self.assertRaises(SystemExit) as raised:
                            runpy.run_path(str(ROOT/'spot-collector.py'),run_name='__main__')
                        self.assertEqual(raised.exception.code,2)
                        dial.assert_not_called();listener.assert_not_called()

    def test_nonobject_processing_constructor_rejected(self):
        for value in (False, [], [['enabled',False]], 'wrong'):
            with self.assertRaises(ValueError):
                relay_module.TelnetRelay(definitions(),{},0,'TK0C','login: ',processing_config=value)


class RawRbnConfigurationTests(unittest.TestCase):
    def make_args(self, servers):
        args = relay_module._create_parser(False).parse_args([])
        args.servers_config_list = servers
        return args

    def test_explicit_rbn_role_is_receive_only(self):
        args = self.make_args([dict(name='primary', address='localhost:7300'),
                               dict(name='raw', address='raw.example.invalid:7000',
                                    role='rbn', direction='in')])
        sources = relay_module.build_server_definitions(args)
        self.assertEqual(sources[1]['role'], 'rbn')
        r = relay_module.TelnetRelay(sources, {}, 0, 'TK0C', 'login: ')
        self.assertFalse(r._direction_allows_outbound('raw'))
        args.servers_config_list[1]['direction'] = 'both'
        with self.assertRaises(ValueError):
            relay_module.build_server_definitions(args)

    def test_known_public_rbn_legacy_local_role_is_normalized(self):
        args = self.make_args([dict(name='primary', address='localhost:7300'),
                               dict(name='raw', address='TELNET.REVERSEBEACON.NET:7000',
                                    role='local', direction='in')])
        with self.assertLogs(level='WARNING') as logs:
            sources = relay_module.build_server_definitions(args)
        self.assertEqual(sources[1]['role'], 'rbn')
        self.assertIn('not pooled', logs.output[0])
        # Build normalization does not rewrite caller-owned JSON/config data.
        self.assertEqual(args.servers_config_list[1]['role'], 'local')

    def test_ordinary_local_endpoint_remains_local(self):
        args = self.make_args([dict(name='primary', address='localhost:7300'),
                               dict(name='local', address='192.168.1.44:7300', direction='in')])
        self.assertEqual(relay_module.build_server_definitions(args)[1]['role'], 'local')
