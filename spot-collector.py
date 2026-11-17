import argparse
import asyncio
import contextlib
from collections import Counter
import json
import logging
from pathlib import Path
import random
import re
import socket
import time

from spot_processing import SpotProcessor, parse_spot
from status_report import format_status
from stream_protocol import LineFramer, TelnetDecoder, ProtocolError, login_prompt, encode_telnet

RECONNECT_INTERVAL = 300  # 5 minutes max delay for reconnect attempts
STATUS_INTERVAL = 300     # 5 minutes between status updates
DRAIN_TIMEOUT = 5         # Seconds to wait on a drain before giving up

def configure_logging(debug_enabled):
    """Configure console logging; verbose spot traces use the same handlers."""
    if logging.getLogger().handlers:
        # Respect existing configuration
        return
    level = logging.DEBUG if debug_enabled else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

def _create_parser(require_required_flags):
    parser = argparse.ArgumentParser(
        description="Telnet Relay Script for relaying data between multiple Telnet servers and clients.\n"
                    "This script connects to multiple Telnet servers and relays data from clients to these servers. "
                    "The first server receives the full callsign as provided, while other servers receive a modified "
                    "callsign without the suffix (e.g., '-23'). The script also handles 'status', 'connect', 'list', "
                    "and 'uptime' commands from clients to manage connections and display information."
    )
    parser.add_argument('--config', type=str, help="Path to a JSON configuration file containing arguments")
    parser.add_argument('--server1', type=str, required=require_required_flags, help="Address and port of the first server in the format address:port. Data from clients will be relayed only to this server. (for example local DX 14001 S53ZO CQing)")
    parser.add_argument('--server1-direction', dest='server1_direction', type=str, choices=['in', 'out', 'both'], default='both', help="Direction of message flow for server1: 'in' (server to clients), 'out' (clients to server), or 'both'")
    parser.add_argument('--server2', type=str, required=require_required_flags, help="Address and port of the second server in the format address:port")
    parser.add_argument('--server2-direction', dest='server2_direction', type=str, choices=['in', 'out', 'both'], default='in', help="Direction of message flow for server2: 'in' (server to clients), 'out' (clients to server), or 'both'")
    parser.add_argument('--server3', type=str, help="Address and port of the third server in the format address:port")
    parser.add_argument('--server3-direction', dest='server3_direction', type=str, choices=['in', 'out', 'both'], default='in', help="Direction of message flow for server3: 'in' (server to clients), 'out' (clients to server), or 'both'")
    parser.add_argument('--server4', type=str, help="Address and port of the fourth server in the format address:port")
    parser.add_argument('--server4-direction', dest='server4_direction', type=str, choices=['in', 'out', 'both'], default='in', help="Direction of message flow for server4: 'in' (server to clients), 'out' (clients to server), or 'both'")
    parser.add_argument('--listen-port', dest='listen_port', type=int, required=require_required_flags, help="Port on which the relay listens for incoming connections")
    parser.add_argument('--callsign', type=str, required=require_required_flags, help="Callsign to send when 'call:' or 'login:' is received. The full callsign is sent to the first server (e.g., S53M-23); a modified version without the suffix is sent to others (e.g., S53M).")
    parser.add_argument('--note1', type=str, help="Note for the first server")
    parser.add_argument('--note2', type=str, help="Note for the second server")
    parser.add_argument('--note3', type=str, help="Note for the third server")
    parser.add_argument('--note4', type=str, help="Note for the fourth server")
    parser.add_argument('--debug', action='store_true', help="Enable debug logging")
    parser.add_argument('--debug-verbose', dest='debug_verbose', action='store_true',
                        help="Log each spot with same-line decision details; does not modify client output")
    parser.add_argument('--login-prompt', dest='login_prompt', type=str, default="login: ", help="Login prompt to send to clients upon connection (default: 'login: ')")
    parser.add_argument('--client-timeout', dest='client_timeout', type=int, default=0,
                        help="Optional inactivity timeout (seconds) for client connections; 0 disables the timeout")
    parser.add_argument('--server-timeout', dest='server_timeout', type=int, default=0,
                        help="Optional inactivity timeout (seconds) for upstream server connections; 0 disables the timeout")
    parser.add_argument('--rbn-dedupe', dest='rbn_dedupe', action='store_true', help="Deprecated compatibility flag; source-aware processing is enabled by default")
    parser.add_argument('--rbn-dwell', dest='rbn_dwell', type=int, default=10, help="Deprecated; accepted but unused by source-aware processing")
    parser.add_argument('--rbn-limbo', dest='rbn_limbo', type=int, default=300, help="Deprecated; accepted but unused by source-aware processing")
    parser.add_argument('--rbn-respot', dest='rbn_respot', type=int, default=180, help="Compatibility alias for spot_processing.repeat_seconds (default: 180)")
    parser.add_argument('--rbn-minqual', dest='rbn_minqual', type=int, default=2, help="Deprecated; accepted but unused by source-aware processing")
    parser.add_argument('--rbn-maxqual', dest='rbn_maxqual', type=int, default=9, help="Deprecated; accepted but unused by source-aware processing")
    parser.add_argument('--rbn-search-khz', dest='rbn_search_khz', type=int, default=5, help="Deprecated; accepted but unused by source-aware processing")
    parser.add_argument('--rbn-max-deviants', dest='rbn_max_deviants', type=int, default=5, help="Deprecated; accepted but unused by source-aware processing")
    parser.add_argument('--rbn-inrush', dest='rbn_inrush', type=int, default=15, help="Deprecated; accepted but unused by source-aware processing")
    parser.add_argument('--rbn-trace', dest='rbn_trace', action='store_true', help="Deprecated; accepted but unused by source-aware processing")
    parser.add_argument('--spot-processing', type=json.loads, default={}, help="JSON object of source-aware processing settings")
    return parser

def parse_arguments():
    initial_parser = argparse.ArgumentParser(add_help=False)
    initial_parser.add_argument('--config', type=str, help="Path to a JSON configuration file containing arguments")
    config_args, _ = initial_parser.parse_known_args()

    if config_args.config:
        parser = _create_parser(require_required_flags=False)
        defaults = parser.parse_args([])
        try:
            with open(config_args.config, 'r') as config_file:
                config_data = json.load(config_file)
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f'Unable to load JSON config {config_args.config}: {exc}')

        if not isinstance(config_data, dict):
            parser.error(f'Config file {config_args.config} must contain a JSON object.')

        servers_config_list = None
        if 'servers' in config_data:
            servers_config_list = config_data.pop('servers')

        for key, value in config_data.items():
            if not hasattr(defaults, key):
                parser.error(f'Unknown configuration key: {key}')
            if isinstance(value, str) and key.endswith('_direction'):
                value = value.lower()
            setattr(defaults, key, value)

        if servers_config_list is not None:
            if not isinstance(servers_config_list, list) or not servers_config_list:
                parser.error('Config file must provide a non-empty "servers" list.')
            for required_value, required_name in ((defaults.listen_port, 'listen_port'),
                                                 (defaults.callsign, 'callsign')):
                if required_value in (None, ''):
                    parser.error(f'Missing required configuration value: {required_name}')
        else:
            required_fields = ['server1', 'server2', 'listen_port', 'callsign']
            for field in required_fields:
                if getattr(defaults, field) in (None, ''):
                    parser.error(f'Missing required configuration value: {field}')

        valid_directions = {'in', 'out', 'both'}
        for name in ['server1_direction', 'server2_direction', 'server3_direction', 'server4_direction']:
            direction_value = getattr(defaults, name, None)
            if direction_value and direction_value not in valid_directions:
                parser.error(f'Invalid direction for {name.replace("_", " ")}: {direction_value}. '
                             f'Use one of {", ".join(sorted(valid_directions))}.')

        if not isinstance(defaults.spot_processing, dict):
            parser.error('spot_processing must be a JSON object')

        if not isinstance(defaults.debug_verbose, bool):
            parser.error('debug_verbose must be a boolean')
        defaults.config = config_args.config
        defaults.servers_config_list = servers_config_list
        return defaults

    parser = _create_parser(require_required_flags=True)
    args = parser.parse_args()
    if not isinstance(args.spot_processing, dict):
        parser.error('spot_processing must be a JSON object')
    args.config = None
    args.servers_config_list = None
    return args

def strip_callsign_suffix(callsign):
    """
    Strip the suffix from the callsign if it exists. 
    The suffix is defined as '-' followed by one or more digits.
    """
    return re.sub(r'-\d+$', '', callsign)

def _normalize_direction(direction, default_value):
    if direction is None:
        return default_value
    direction = str(direction).lower()
    if direction not in ('in', 'out', 'both'):
        raise ValueError(f'Invalid direction "{direction}". Use one of in/out/both.')
    return direction


def _format_duration(seconds):
    """Return a human-friendly duration string."""
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours}h {minutes}m {secs}s'
    if minutes:
        return f'{minutes}m {secs}s'
    return f'{secs}s'

def build_server_definitions(args):
    """
    Build a list of server definitions that can grow beyond the legacy 4-slot layout.
    Each definition is a dict with keys: name, address, note, direction.
    """
    server_definitions = []

    if args.servers_config_list is not None:
        for idx, entry in enumerate(args.servers_config_list):
            if not isinstance(entry, dict):
                raise ValueError('Each entry in "servers" must be an object with address and optional name/note/direction.')
            address = entry.get('address')
            if not address:
                raise ValueError('Each server entry in "servers" must include an "address".')
            name = entry.get('name') or f'Server{idx + 1}'
            note = entry.get('note', '')
            default_direction = 'both' if idx == 0 else 'in'
            direction = _normalize_direction(entry.get('direction'), default_direction)
            server_definitions.append(dict(entry, name=name, address=address, note=note, direction=direction))
    else:
        legacy_servers = [
            ('Server1', args.server1, args.note1, args.server1_direction, 'both'),
            ('Server2', args.server2, args.note2, args.server2_direction, 'in'),
            ('Server3', args.server3, args.note3, args.server3_direction, 'in'),
            ('Server4', args.server4, args.note4, args.server4_direction, 'in')
        ]
        for name, address, note, direction, default_direction in legacy_servers:
            if address:
                server_definitions.append({
                    'name': name,
                    'address': address,
                    'note': note or '',
                    'direction': _normalize_direction(direction, default_direction)
                })

    if not server_definitions:
        raise ValueError('At least one server must be configured.')

    seen = set()
    for idx, server in enumerate(server_definitions):
        name = server['name']
        if not isinstance(name, str) or not name or name.lower() in seen:
            raise ValueError('Server names must be nonempty and unique')
        seen.add(name.lower())
        if not isinstance(server.get('enabled', True), bool):
            raise ValueError('enabled must be true or false')
        if not isinstance(server['address'], str):
            raise ValueError('Server address must be a string')
        host, port = server['address'].rsplit(':', 1)
        if not host or not 1 <= int(port) <= 65535:
            raise ValueError('Server address must contain a host and valid port')
        role = server.setdefault('role', 'cluster' if idx == 0 else 'local')
        if role == 'local' and host.lower().rstrip('.') == 'telnet.reversebeacon.net':
            role = server['role'] = 'rbn'
            logging.warning('Source %s is public RBN; treating local role as rbn so receivers are not pooled', name)
        if role not in ('cluster', 'local', 'gocluster', 'rbn'):
            raise ValueError('Server role must be cluster, local, gocluster or rbn')
        if role in ('gocluster', 'rbn') and server['direction'] != 'in':
            raise ValueError('GoCluster and RBN sources must have direction in')
        for value in (server.get('callsign'),):
            if value is not None and (not isinstance(value, str) or not value or len(value) > 64 or not value.isascii() or any(c.isspace() for c in value)):
                raise ValueError('Per-server callsign must be one token of at most 64 characters')
        settings_file = server.get('settings_file')
        if settings_file is not None:
            if not isinstance(settings_file, str) or not settings_file:
                raise ValueError('settings_file must be a nonempty path')
            if 'startup_commands' in server:
                raise ValueError('Use settings_file or startup_commands, not both')
            base = Path(args.config).resolve().parent if getattr(args, 'config', None) else Path.cwd()
            path = base / settings_file
            try:
                with path.open('rb') as settings:
                    payload = settings.read(16385)
                if len(payload) > 16384:
                    raise ValueError('settings_file exceeds 16384 bytes')
                if any(byte < 32 and byte not in (10, 13) or byte > 126 for byte in payload):
                    raise ValueError('settings_file must contain printable ASCII commands and line endings')
                text = payload.decode('ascii')
            except (OSError, UnicodeError) as exc:
                raise ValueError('Unable to read settings_file %s: %s' % (path, exc)) from exc
            commands = [line.strip() for line in text.splitlines()
                        if line.strip() and not line.lstrip().startswith('#')]
            if any(any(ord(char) < 32 or ord(char) > 126 for char in line) for line in commands):
                raise ValueError('settings_file commands must be printable ASCII')
            server['startup_commands'] = commands
        commands = server.get('startup_commands', [])
        if not isinstance(commands, list) or len(commands) > 16 or any(not isinstance(c, str) or len(c) > 512 or '\n' in c or '\r' in c for c in commands):
            raise ValueError('startup_commands must contain at most 16 single-line commands')
        ready = server.get('ready_text', '')
        if not isinstance(ready, str) or len(ready) > 256:
            raise ValueError('ready_text must be a string of at most 256 characters')
        if commands and not ready:
            raise ValueError('startup_commands require explicit ready_text from the server greeting')
    return server_definitions

class TelnetRelay:
    def __init__(self, server_definitions, notes, listen_port, callsign, login_prompt, client_timeout=None, server_timeout=None, server_directions=None, rbn_config=None, processing_config=None, debug_verbose=False):
        if not isinstance(debug_verbose, bool):
            raise ValueError('debug_verbose must be a boolean')
        self.debug_verbose = debug_verbose
        self.source_spot_counts = {item['name']: Counter() for item in server_definitions}
        self.last_spot_times = {item['name']: None for item in server_definitions}
        self.server_definitions = server_definitions
        self.notes = notes
        self.listen_port = listen_port
        self.callsign = callsign
        self.login_prompt = login_prompt
        self.client_writers = []
        self.client_queues = {}
        self.client_sender_tasks = {}
        self.server_connections = {}  # server_name: (reader, writer, address, alive, connected_at)
        self.connector_tasks = {}
        self.server_locks = {server['name']: asyncio.Lock() for server in self.server_definitions}
        self.handshake_sent = {}
        self.start_time = time.time()  # Track when the server started
        self.client_timeout = client_timeout
        self.server_timeout = server_timeout if server_timeout and server_timeout > 0 else None
        self.server_directions = server_directions or {}
        self.primary_server_name = self.server_definitions[0]['name']
        self.server_definitions_by_name = {item['name']: item for item in server_definitions}
        self.streams = {}
        self.cleanup_tasks = set()
        self.reconnect_events = {item['name']: asyncio.Event() for item in server_definitions}
        self.client_handlers = set()
        self.client_drop_count = 0
        self.history_until = 0.0
        self.history_remaining = 0
        self.stopping = False
        self.listener = None
        self.local_call_release_task = None
        if processing_config is not None and not isinstance(processing_config, dict):
            raise ValueError('spot_processing must be an object')
        self.processing_config = dict(processing_config) if processing_config is not None else {}
        if rbn_config and 'respot_time' in rbn_config:
            self.processing_config.setdefault('repeat_seconds', rbn_config['respot_time'])
        self.processor = SpotProcessor(self.processing_config)

    def _direction_allows_outbound(self, server_name):
        source = self.server_definitions_by_name[server_name]
        if source.get('role') in ('gocluster', 'rbn'):
            return False
        direction = self.server_directions.get(server_name, source.get('direction', 'both'))
        return direction in ('out', 'both')

    def _direction_allows_inbound(self, server_name):
        source = self.server_definitions_by_name[server_name]
        direction = self.server_directions.get(server_name, source.get('direction', 'both'))
        return direction in ('in', 'both')

    async def _deliver_lines_to_clients(self, lines):
        for line in lines:
            payload = line + b'\r\n'
            for writer in self.client_writers[:]:
                await self._enqueue_client(writer, payload, 'client')

    async def _write_with_timeout(self, writer, data, context):
        """Write Telnet application bytes with a bounded drain."""
        if writer.is_closing():
            return False
        try:
            writer.write(encode_telnet(data))
            await asyncio.wait_for(writer.drain(), timeout=DRAIN_TIMEOUT)
            return True
        except (asyncio.TimeoutError, OSError, RuntimeError) as exc:
            logging.error(f'Write to {context} failed or timed out: {exc}')
            return False

    async def _enqueue_client(self, writer, data, context):
        """Queue data for a client; drop the client if it falls behind."""
        queue = self.client_queues.get(writer)
        if queue is None:
            return
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            self.client_drop_count += 1
            logging.error(f'Client {context} send queue full; disconnecting')
            await self._cleanup_client(writer)

    async def _client_sender(self, writer, queue, context):
        """Serialize writes to a client so one slow client cannot block others."""
        try:
            while not self.stopping and not writer.is_closing():
                data = await queue.get()
                if data is None:
                    break
                ok = await self._write_with_timeout(writer, data, context)
                if not ok:
                    break
        finally:
            await self._cleanup_client(writer)

    async def _close_writer(self, writer):
        """Join graceful close, forcing transport retirement if draining stalls."""
        if writer is None:
            return
        waiter = None
        transport = getattr(writer, 'transport', None)
        try:
            writer.close()
            waiter = asyncio.ensure_future(writer.wait_closed())
            try:
                # wait_closed shares its protocol future. A timeout must not
                # cancel that future before abort can publish connection_lost.
                await asyncio.wait_for(asyncio.shield(waiter), DRAIN_TIMEOUT)
                return
            except (asyncio.TimeoutError, OSError, RuntimeError):
                if transport is not None:
                    transport.abort()
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), min(DRAIN_TIMEOUT, 1))
                except (asyncio.TimeoutError, OSError, RuntimeError):
                    pass
        except asyncio.CancelledError:
            if transport is not None:
                transport.abort()
            raise
        except (OSError, RuntimeError):
            if transport is not None:
                transport.abort()
        finally:
            if waiter is not None:
                if not waiter.done():
                    waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    async def _cleanup_client(self, writer):
        task = self.client_sender_tasks.get(writer)
        if task is asyncio.current_task():
            self.client_sender_tasks.pop(writer, None)
        elif task is not None:
            task.cancel()
            task.add_done_callback(lambda done: self.client_sender_tasks.pop(writer, None) if self.client_sender_tasks.get(writer) is done else None)
        self.client_queues.pop(writer, None)
        if writer in self.client_writers:
            self.client_writers.remove(writer)
        # Removal and close are immediate; the bounded join cannot block fanout.
        if not writer.is_closing():
            writer.close()
            if len(self.cleanup_tasks) < 128:
                close_task = asyncio.create_task(self._close_writer(writer))
                self.cleanup_tasks.add(close_task)
                close_task.add_done_callback(self.cleanup_tasks.discard)
            else:
                transport = getattr(writer, 'transport', None)
                if transport is not None:
                    transport.abort()

    def _enable_tcp_keepalive(self, writer, context):
        """Enable TCP keepalive on the provided writer's socket when possible."""
        sock = writer.get_extra_info('socket')
        if sock is None:
            logging.debug(f'No socket to configure keepalive for {context}')
            return

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

            if hasattr(socket, 'TCP_KEEPIDLE'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            elif hasattr(socket, 'TCP_KEEPALIVE'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 60)

            if hasattr(socket, 'TCP_KEEPINTVL'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)

            if hasattr(socket, 'TCP_KEEPCNT'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)

            logging.debug(f'Enabled TCP keepalive for {context}')
        except OSError as e:
            logging.debug(f'Failed to enable TCP keepalive for {context}: {e}')

    async def _process_server_line(self, name, line):
        if not self._direction_allows_inbound(name):
            return
        # Retire expired holds before new input can supply late authority.
        await self._drain_local_call_holds()
        source = self.server_definitions_by_name[name]
        history = (name == self.primary_server_name and
                   time.monotonic() < self.history_until and self.history_remaining > 0)
        if history and line.upper().startswith(b'DX DE '):
            self.history_remaining -= 1
        elif history and line.rstrip().endswith(b'>'):
            self.history_remaining = 0
        role = source.get('role', 'cluster' if name == self.primary_server_name else 'local')
        before = dict(getattr(self.processor, 'counts', {}))
        outputs = self.processor.process(name, role, line, history=history)
        after = getattr(self.processor, 'counts', {})
        delta = {key: value - before.get(key, 0) for key, value in after.items()}
        await self._record_spot_result(name, role, line, outputs, delta, history)
        # New external evidence can settle a local hold before its deadline.
        await self._drain_local_call_holds()

    async def _record_spot_result(self, name, role, line, outputs, delta,
                                  history=False, evaluation=None, observed=True):
        metrics = self.source_spot_counts[name]
        for field, counter in (('parsed', 'parsed_spots'), ('forwarded', 'delivered_spots'),
                               ('repeats', 'suppressed_repeats'), ('held', 'local_call_held')):
            metrics[field] += delta.get(counter, 0)
        metrics['rejected'] += sum(delta.get(key, 0) for key in
                                  ('stale_spots', 'out_of_order_spots', 'undated_frequency_conflicts'))
        is_spot = bool(re.match(rb'^DX\s+de\s', line, re.I))
        if is_spot and observed:
            self.last_spot_times[name] = time.monotonic()
        if self.debug_verbose and is_spot:
            self._log_spot_decision(name, role, line, outputs, delta, history,
                                    evaluation=evaluation)
        await self._deliver_lines_to_clients(outputs)

    async def _drain_local_call_holds(self, force=False):
        drain = getattr(self.processor, 'drain_local_holds', None)
        if drain is None:
            return
        for event in drain(force=force):
            name = event['source']
            if name not in self.server_definitions_by_name:
                logging.warning('Discarded local call release for unknown source %s', name)
                continue
            role = self.server_definitions_by_name[name].get('role', 'local')
            await self._record_spot_result(name, role, event['input'], event['outputs'],
                                           event['delta'], evaluation=event['evaluation'],
                                           observed=False)

    async def _local_call_release_loop(self):
        while not self.stopping:
            await asyncio.sleep(.25)
            await self._drain_local_call_holds()

    async def _process_rbn_data(self, name, data):
        # Compatibility entry point: it now processes all source roles equally.
        decoder, framer = self.streams.setdefault(name, (TelnetDecoder(), LineFramer(cluster_prompts=True)))
        for line in framer.feed(decoder.feed(data)):
            await self._process_server_line(name, line)

    def _start_connector(self, server_def, force_restart=False):
        """Ensure a single connector task per server."""
        name = server_def['name']
        task = self.connector_tasks.get(name)
        if task and not task.done():
            if force_restart:
                self.reconnect_events[name].set()
            return
        self.connector_tasks[name] = asyncio.create_task(self._maintain_connection(server_def))

    async def _maintain_connection(self, server_def):
        name = server_def['name']
        host, port = server_def['address'].rsplit(':', 1)
        host = host.strip('[]')
        delay = 1
        while not self.stopping:
            writer = None
            try:
                async with self.server_locks[name]:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 30)
                    self.server_connections[name] = (reader, writer, server_def['address'], True, time.time())
                    self.handshake_sent[name] = False
                    self.processor.reset_source(name)
                    if name == self.primary_server_name:
                        self.history_remaining = 0
                    self.streams[name] = (TelnetDecoder(), LineFramer(cluster_prompts=True))
                    self._enable_tcp_keepalive(writer, 'server ' + name)
                await self.relay_server_data(reader, writer, name)
                if self.handshake_sent.get(name):
                    delay = 1
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, asyncio.TimeoutError) as exc:
                logging.error('Upstream %s disconnected: %s', name, exc)
            finally:
                try:
                    await self._close_writer(writer)
                finally:
                    current = self.server_connections.get(name)
                    if current is None or current[1] is writer:
                        self.server_connections[name] = (None, None, server_def['address'], False, None)
                        self.handshake_sent[name] = False
                        self.streams.pop(name, None)
                        self.processor.reset_source(name)
            if not self.stopping:
                event = self.reconnect_events[name]
                try:
                    await asyncio.wait_for(event.wait(), min(delay, RECONNECT_INTERVAL) + random.uniform(0, 1))
                except asyncio.TimeoutError:
                    pass
                event.clear()
                delay = min(delay * 2, RECONNECT_INTERVAL)

    @staticmethod
    def _single_line(value):
        """Keep diagnostics on one physical line without terminal control bytes."""
        if isinstance(value, bytes):
            return ''.join(chr(byte) if 32 <= byte <= 126 else '\\x%02x' % byte
                           for byte in value)
        return TelnetRelay._single_line(str(value).encode('ascii', 'backslashreplace'))

    def _log_spot_decision(self, name, role, line, outputs, delta, history, evaluation=None):
        evaluated = getattr(self.processor, 'last_evaluation', None) if evaluation is None else evaluation
        if history:
            action, reason = 'BYPASS', 'history'
        elif not self.processor.config['enabled']:
            action, reason = 'BYPASS', 'processing-off'
        elif evaluated and evaluated.get('reason'):
            # Deferred releases intentionally have no new parsed/input count.
            reason = evaluated['reason']
            action = 'HOLD' if evaluated.get('held') else ('SELECT' if outputs else 'DROP')
        elif not delta.get('parsed_spots'):
            action, reason = 'BYPASS', 'unrecognized-format'
        elif delta.get('suppressed_repeats'):
            action, reason = 'DROP', 'repeat'
        elif delta.get('stale_spots'):
            action, reason = 'DROP', 'stale'
        elif delta.get('out_of_order_spots'):
            action, reason = 'DROP', 'out-of-order'
        elif delta.get('undated_frequency_conflicts'):
            action, reason = 'DROP', 'undated-frequency-conflict'
        else:
            action = 'SELECT' if outputs else 'DROP'
            reason = 'frequency-move' if delta.get('frequency_change_updates') else 'new-or-window-expired'
        fields = ['src=' + self._single_line(name), 'role=' + self._single_line(role),
                  action, 'reason=' + reason]
        try:
            parsed = parse_spot(line, self.processor.clock(), role)
        except (ValueError, OverflowError):
            # Bypass/history records must not become parser failures just
            # because verbose diagnostics are enabled.
            parsed = None
        if parsed is not None:
            fields.append('band=' + parsed.band)
            if parsed.confidence:
                fields.append('GC=' + parsed.confidence)
            if evaluated:
                fields.append('freq=%.3f%s kHz' % (
                    parsed.frequency_hz / 1000,
                    '->%.3f' % (evaluated['frequency_hz'] / 1000)
                    if evaluated['frequency_hz'] != parsed.frequency_hz else ''))
                fields.append('freq-basis=' + evaluated.get('frequency_basis', 'input'))
                if evaluated['call'] != parsed.call:
                    fields.append('call=' + parsed.call + '->' + evaluated['call'])
                if evaluated.get('anchor_hz') is not None:
                    fields.append('anchor=%.3fkHz' % (evaluated['anchor_hz'] / 1000))
                if evaluated.get('candidate_hz') is not None:
                    fields.append('candidate=%.3fkHz' % (evaluated['candidate_hz'] / 1000))
                if 'support' in evaluated:
                    fields.append('support=%s/%s' % (evaluated['support'], evaluated.get('required', 2)))
                if evaluated.get('reference_basis'):
                    fields.append('reference=' + self._single_line(evaluated['reference_basis']))
                if evaluated.get('call_candidate'):
                    fields.append('call-candidate=' + self._single_line(evaluated['call_candidate']))
                if 'call_support' in evaluated:
                    fields.append('call-support=%s/2' % evaluated['call_support'])
            if role in ('local', 'rbn', 'gocluster'):
                rows = self.processor.alignment_snapshot([name])
                alignment = next((row for row in rows if row['band'] == parsed.band and
                                  row.get('receiver', parsed.origin) == parsed.origin), None)
                if alignment:
                    fields.append('align=' + alignment['state'])
                    fields.append('samples=%d/%d' % (alignment['samples'], alignment['required']))
                    fields.append('retained=%s' % alignment.get('retained_samples', alignment['samples']))
                    if alignment.get('rejection_reason'):
                        fields.append('align-reason=' + self._single_line(alignment['rejection_reason']))
                    if alignment['offset_hz'] is not None:
                        fields.append('adjust=%+.1fHz' % alignment['offset_hz'])
        if not outputs and (delta.get('frequency_corrections') or delta.get('callsign_corrections')):
            fields.append('changes=evaluated-only')
        logging.debug('%-76s | %s', self._single_line(line), ' '.join(fields))

    def _build_status_message(self):
        """Report spot decisions and current evidence without claiming receipt."""
        now = time.monotonic()
        rows = []
        for source in self.server_definitions:
            name = source['name']
            connection = self.server_connections.get(name)
            alive = bool(connection and connection[3])
            if not source.get('enabled', True):
                state = 'Disabled'
            elif not alive:
                state = 'Down'
            else:
                state = 'Login sent' if self.handshake_sent.get(name) else 'TCP up'
            last = self.last_spot_times[name]
            rows.append(dict(self.source_spot_counts[name], name=name,
                             role=source.get('role', 'cluster' if name == self.primary_server_name else 'local'),
                             state=state, address=source['address'],
                             direction=self.server_directions.get(name, source.get('direction', 'both')),
                             up=_format_duration(max(0, time.time() - connection[4]))
                                if alive and connection[4] is not None else '-',
                             last_spot=_format_duration(max(0, now - last)) + ' ago' if last is not None else '-'))
        snapshot = self.processor.snapshot()
        local_sources = [source['name'] for source in self.server_definitions
                         if source.get('role', 'cluster' if source['name'] == self.primary_server_name else 'local') in ('local', 'rbn', 'gocluster')
                         and source.get('enabled', True)]
        return format_status(
            uptime=_format_duration(max(0, time.time() - self.start_time)),
            listen_port=self.listen_port, primary=self.primary_server_name,
            client_count=sum(not writer.is_closing() for writer in self.client_writers),
            client_drops=self.client_drop_count, processing_enabled=self.processor.config['enabled'],
            counters=snapshot, sources=rows,
            alignments=self.processor.alignment_snapshot(local_sources),
            policy=self.processor.config)

    async def _handle_command(self, writer, line):
        message = line.strip().lower()
        local = {b'status': self.send_status_to_single_client,
                 b'list': self.list_connected_clients, b'uptime': self.send_uptime}
        if message in local:
            await local[message](writer)
            return
        if message == b'connect':
            await self.connect_to_all_servers()
            return
        for name, (_, upstream, _, alive, _) in list(self.server_connections.items()):
            if not alive or upstream is None or not self._direction_allows_outbound(name):
                continue
            if self.server_definitions_by_name[name].get('role') in ('gocluster', 'rbn'):
                continue
            if name == self.primary_server_name and re.match(rb'^(?:sh(?:ow)?)[ /]+dx(?:\s|$|/)', message):
                # Plain text has no request IDs. Bound the primary-only history
                # bypass to 200 spot rows / 5 seconds; concurrent live primary
                # spots during that interval may also bypass repeat suppression.
                self.history_until = time.monotonic() + 5
                self.history_remaining = 200
            await self._write_with_timeout(upstream, line + b'\r\n', 'server ' + name)

    async def handle_client(self, reader, writer):
        if self.stopping or max(len(self.client_handlers), len(self.client_sender_tasks), len(self.cleanup_tasks)) >= 128:
            writer.close()
            transport = getattr(writer, 'transport', None)
            if transport is not None:
                transport.abort()
            return
        handler = asyncio.current_task()
        self.client_handlers.add(handler)
        self.client_writers.append(writer)
        queue = asyncio.Queue(maxsize=100)
        self.client_queues[writer] = queue
        self.client_sender_tasks[writer] = asyncio.create_task(self._client_sender(writer, queue, 'client'))
        self._enable_tcp_keepalive(writer, 'client')
        decoder, framer = TelnetDecoder(client_interrupts=True), LineFramer(4096)
        try:
            await self._enqueue_client(writer, self.login_prompt.encode(), 'client')
            while not self.stopping and not writer.is_closing():
                data = await asyncio.wait_for(reader.read(4096), self.client_timeout)
                if not data:
                    break
                application, interrupted, _ = decoder.feed(data).partition(b'\x03')
                for line in framer.feed(application):
                    await self._handle_command(writer, line)
                if interrupted:
                    # Discard an incomplete command and all bytes after Ctrl+C;
                    # normal cleanup retires only this client's connection.
                    break
        except (OSError, asyncio.TimeoutError, ProtocolError):
            pass
        finally:
            await self._cleanup_client(writer)
            self.client_handlers.discard(handler)

    async def list_connected_clients(self, writer):
        """List all currently connected clients."""
        clients = "\n".join([str(writer.get_extra_info('peername')) for writer in self.client_writers])
        status_message = f"Connected clients:\n{clients}\n"
        peer = writer.get_extra_info('peername')
        await self._enqueue_client(writer, status_message.encode(), f'client {peer}')

    async def send_uptime(self, writer):
        """Send the uptime of the relay server."""
        uptime_seconds = time.time() - self.start_time
        uptime_message = f"Server Uptime: {uptime_seconds:.2f} seconds\n"
        peer = writer.get_extra_info('peername')
        await self._enqueue_client(writer, uptime_message.encode(), f'client {peer}')

    async def send_status_to_single_client(self, writer):
        """Send the server connection status to a single client."""
        status_message = self._build_status_message()
        peer = writer.get_extra_info('peername')
        await self._enqueue_client(writer, status_message.encode(), f'client {peer}')

    async def connect_to_all_servers(self):
        """Attempt to reconnect to all servers that are currently not connected."""
        for server_def in self.server_definitions:
            server_name = server_def['name']
            connection = self.server_connections.get(server_name)
            address = connection[2] if connection else server_def['address']
            alive = connection[3] if connection else False
            if not alive and server_def.get('enabled', True):
                logging.debug(f'Server {server_name} is not connected, attempting to reconnect')
                self._start_connector(server_def, force_restart=True)

    async def relay_server_data(self, reader, writer, server_name):
        source = self.server_definitions_by_name[server_name]
        decoder, framer = self.streams.setdefault(server_name, (TelnetDecoder(), LineFramer(cluster_prompts=True)))
        setup_sent = False
        async def maybe_login(value):
            if self.handshake_sent.get(server_name) or not login_prompt(value):
                return
            identity = source.get('callsign')
            if not identity:
                identity = self.callsign if server_name == self.primary_server_name else strip_callsign_suffix(self.callsign)
            if not await self._write_with_timeout(writer, identity.encode('ascii') + b'\r\n', 'login ' + server_name):
                raise ConnectionError('Unable to send login to ' + server_name)
            self.handshake_sent[server_name] = True
        try:
            while not self.stopping:
                data = await asyncio.wait_for(reader.read(4096), self.server_timeout)
                if not data:
                    break
                for line in framer.feed(decoder.feed(data)):
                    await maybe_login(line)
                    if (not setup_sent and self.handshake_sent.get(server_name) and
                            source.get('ready_text') and source['ready_text'].encode() in line):
                        for command in source.get('startup_commands', []):
                            if not await self._write_with_timeout(writer, command.encode() + b'\r\n', 'setup ' + server_name):
                                return
                        setup_sent = True
                    await self._process_server_line(server_name, line)
                if not self.handshake_sent.get(server_name):
                    prompt = framer.take_prompt()
                    if prompt is not None:
                        await maybe_login(prompt)
                        if self._direction_allows_inbound(server_name):
                            await self._deliver_lines_to_clients([prompt])
        except asyncio.TimeoutError:
            logging.info('Upstream %s idle timeout', server_name)
        # Connector retains and explicitly closes its writer in every exit path.

    async def send_status_to_clients(self):
        """Periodically send connection status to all connected clients."""
        while True:
            try:
                status_message = self._build_status_message()
                for client_writer in self.client_writers[:]:
                    if not client_writer.is_closing():
                        peer = client_writer.get_extra_info('peername')
                        await self._enqueue_client(client_writer, status_message.encode(), f'client {peer}')
                await asyncio.sleep(STATUS_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f'Status update task failed: {e}, restarting in 5 seconds')
                await asyncio.sleep(5)

    async def stop(self):
        self.stopping = True
        if self.local_call_release_task is not None:
            self.local_call_release_task.cancel()
            await asyncio.gather(self.local_call_release_task, return_exceptions=True)
            self.local_call_release_task = None
        # Retire spot producers before force-draining, so an already pending
        # upstream read cannot enqueue a new hold after the final drain.
        input_tasks = [task for task in self.connector_tasks.values()
                       if task is not asyncio.current_task()]
        for task in input_tasks:
            task.cancel()
        await asyncio.gather(*input_tasks, return_exceptions=True)
        # Resolve accepted held work before clients retire. Ordinary shutdown
        # still does not promise that every queued output reaches a client.
        await self._drain_local_call_holds(force=True)
        if self.listener is not None:
            self.listener.close()
            await self.listener.wait_closed()
        tasks = list(self.connector_tasks.values()) + list(self.client_handlers) + list(self.client_sender_tasks.values())
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t is not asyncio.current_task()), return_exceptions=True)
        for writer in self.client_writers[:]:
            await self._cleanup_client(writer)
        if self.cleanup_tasks:
            await asyncio.gather(*list(self.cleanup_tasks), return_exceptions=True)

    async def start_relay(self):
        self.stopping = False
        for definition in self.server_definitions:
            self.server_connections[definition['name']] = (None, None, definition['address'], False, None)
            if definition.get('enabled', True):
                self._start_connector(definition)
        status_task = asyncio.create_task(self.send_status_to_clients())
        self.local_call_release_task = asyncio.create_task(self._local_call_release_loop())
        try:
            self.listener = await asyncio.start_server(self.handle_client, '0.0.0.0', self.listen_port)
            async with self.listener:
                await self.listener.serve_forever()
        finally:
            status_task.cancel()
            await asyncio.gather(status_task, return_exceptions=True)
            await self.stop()

if __name__ == "__main__":
    args = parse_arguments()

    configure_logging(args.debug or args.debug_verbose)
    if args.debug or args.debug_verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    try:
        server_definitions = build_server_definitions(args)
    except ValueError as exc:
        logging.error(str(exc))
        raise SystemExit(1)
    notes = {server['name']: server.get('note', '') for server in server_definitions}
    server_directions = {server['name']: server.get('direction', 'both') for server in server_definitions}

    client_timeout = args.client_timeout if args.client_timeout > 0 else None
    server_timeout = args.server_timeout if args.server_timeout > 0 else None

    rbn_config = {
        'enabled': bool(args.rbn_dedupe),
        'dwell_time': args.rbn_dwell,
        'limbo_time': args.rbn_limbo,
        'respot_time': args.rbn_respot,
        'cache_time': args.rbn_limbo * 12,  # keep cache warm roughly an hour by default
        'min_quality': args.rbn_minqual,
        'max_quality': args.rbn_maxqual,
        'search_khz': args.rbn_search_khz,
        'max_deviants': args.rbn_max_deviants,
        'inrush_delay': args.rbn_inrush,
    }

    if args.rbn_trace:
        rbn_config['trace_fn'] = lambda msg: logging.info(f"RBN {msg}")
        if not args.rbn_dedupe:
            logging.warning("--rbn-trace requested but --rbn-dedupe is not enabled")

    async def run_configured_relay():
        # Python 3.7 synchronization primitives bind to the current event loop.
        # Construct them inside the same loop that owns connections and stop.
        relay = TelnetRelay(server_definitions, notes, args.listen_port, args.callsign, args.login_prompt,
                            client_timeout=client_timeout, server_timeout=server_timeout,
                            server_directions=server_directions, rbn_config=rbn_config,
                            processing_config=args.spot_processing, debug_verbose=args.debug_verbose)
        await relay.start_relay()

    asyncio.run(run_configured_relay())
