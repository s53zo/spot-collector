import argparse
import asyncio
import contextlib
from collections import deque
import json
import logging
import random
import re
import socket
import time

RECONNECT_INTERVAL = 300  # 5 minutes max delay for reconnect attempts
STATUS_INTERVAL = 300     # 5 minutes between status updates
DRAIN_TIMEOUT = 5         # Seconds to wait on a drain before giving up
DEFAULT_SERVER_TIMEOUT = 300  # Seconds without data before considering an upstream stalled

def configure_logging(debug_enabled):
    """Configure root logger to emit to stdout."""
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
    parser.add_argument('--login-prompt', dest='login_prompt', type=str, default="login: ", help="Login prompt to send to clients upon connection (default: 'login: ')")
    parser.add_argument('--client-timeout', dest='client_timeout', type=int, default=0,
                        help="Optional inactivity timeout (seconds) for client connections; 0 disables the timeout")
    parser.add_argument('--server-timeout', dest='server_timeout', type=int, default=0,
                        help="Optional inactivity timeout (seconds) for upstream server connections; 0 disables the timeout")
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

        defaults.config = config_args.config
        defaults.servers_config_list = servers_config_list
        return defaults

    parser = _create_parser(require_required_flags=True)
    args = parser.parse_args()
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
            server_definitions.append({
                'name': name,
                'address': address,
                'note': note,
                'direction': direction
            })
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

    return server_definitions

class TelnetRelay:
    def __init__(self, server_definitions, notes, listen_port, callsign, login_prompt, client_timeout=None, server_timeout=None, server_directions=None):
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
        self.server_timeout = server_timeout if server_timeout is not None else DEFAULT_SERVER_TIMEOUT
        self.server_directions = server_directions or {}
        self.server_message_times = {server['name']: deque() for server in self.server_definitions}
        self.primary_server_name = self.server_definitions[0]['name']
        logging.debug(f'TelnetRelay initialized with servers: {self.server_definitions}, listen_port: {listen_port}, '
                      f'callsign: {callsign}, notes: {notes}, login_prompt: {login_prompt}, '
                      f'client_timeout: {client_timeout}, server_timeout: {server_timeout}, '
                      f'server_directions: {self.server_directions}')

    def _direction_allows_outbound(self, server_name):
        direction = self.server_directions.get(server_name, 'both')
        return direction in ('out', 'both')

    def _direction_allows_inbound(self, server_name):
        direction = self.server_directions.get(server_name, 'both')
        return direction in ('in', 'both')

    def _iter_server_status(self):
        for server_def in self.server_definitions:
            name = server_def['name']
            connection = self.server_connections.get(name)
            address = connection[2] if connection else server_def['address']
            alive = connection[3] if connection else False
            connected_at = connection[4] if connection else None
            note = self.notes.get(name, server_def.get('note', ''))
            direction = self.server_directions.get(name, server_def.get('direction', 'both'))
            yield name, address, alive, note, direction, connected_at

    def _record_server_message(self, server_name):
        """Track timestamps of inbound messages for simple rate calculation."""
        timestamps = self.server_message_times.get(server_name)
        if timestamps is None:
            return
        now = time.time()
        cutoff = now - 900  # 15 minutes
        timestamps.append(now)
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

    def _compute_rate_per_hour(self, server_name):
        """Compute per-hour rate based on the last 15 minutes of messages."""
        timestamps = self.server_message_times.get(server_name)
        if not timestamps:
            return '-'
        now = time.time()
        cutoff = now - 900
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        # Scale 15-minute count to per-hour rate
        rate = (len(timestamps) * 4)
        return f'{rate}'

    async def _write_with_timeout(self, writer, data, context):
        """Write to a stream with a timeout to avoid stalling on slow peers."""
        if writer.is_closing():
            return False
        writer.write(data)
        try:
            await asyncio.wait_for(writer.drain(), timeout=DRAIN_TIMEOUT)
            return True
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError) as exc:
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
            logging.error(f'Client {context} send queue full; disconnecting')
            await self._cleanup_client(writer)

    async def _client_sender(self, writer, queue, context):
        """Serialize writes to a client so one slow client cannot block others."""
        try:
            while True:
                data = await queue.get()
                if data is None:
                    break
                ok = await self._write_with_timeout(writer, data, context)
                if not ok:
                    break
        finally:
            await self._cleanup_client(writer)

    async def _cleanup_client(self, writer):
        """Remove client state and close the connection."""
        current = asyncio.current_task()
        task = self.client_sender_tasks.pop(writer, None)
        if task is not None and task is not current and not task.done():
            task.cancel()
        queue = self.client_queues.pop(writer, None)
        if queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        if writer in self.client_writers:
            self.client_writers.remove(writer)
        if writer and not writer.is_closing():
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

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

    @staticmethod
    def _strip_telnet_control(data):
        """Remove Telnet negotiation sequences (IAC commands) from a byte stream."""
        result = bytearray()
        i = 0
        length = len(data)
        while i < length:
            byte = data[i]
            if byte == 255:  # IAC
                if i + 1 >= length:
                    break
                command = data[i + 1]
                if command in (251, 252, 253, 254):  # WILL/WONT/DO/DONT + option byte
                    i += 3
                    continue
                if command == 250:  # SB ... IAC SE
                    i += 2
                    while i < length:
                        if data[i] == 255 and i + 1 < length and data[i + 1] == 240:
                            i += 2
                            break
                        i += 1
                    continue
                if command == 255:  # Escaped IAC
                    result.append(255)
                    i += 2
                    continue
                i += 2
                continue
            result.append(byte)
            i += 1
        return bytes(result)

    def _start_connector(self, server_def, force_restart=False):
        """Ensure a single connector task per server."""
        name = server_def['name']
        task = self.connector_tasks.get(name)
        if task and not task.done():
            if not force_restart:
                return
            task.cancel()
        self.connector_tasks[name] = asyncio.create_task(self._maintain_connection(server_def))

    async def _maintain_connection(self, server_def):
        """Maintain a single connection task per server with bounded backoff."""
        name = server_def['name']
        address, port = server_def['address'].split(':')
        delay = 1
        while True:
            try:
                async with self.server_locks[name]:
                    logging.debug(f'Connecting to {name} at {address}:{port}')
                    reader, writer = await asyncio.open_connection(address, int(port))
                    logging.debug(f'Connected to {name} at {address}:{port}')
                    self.server_connections[name] = (reader, writer, f'{address}:{port}', True, time.time())
                    self.handshake_sent[name] = False
                    self._enable_tcp_keepalive(writer, f'server connection {name}')
                delay = 1  # reset backoff on success
                await self.relay_server_data(reader, writer, name)
            except asyncio.CancelledError:
                logging.debug(f'Connector for {name} cancelled')
                break
            except Exception as e:
                logging.error(f'Error connecting to server {address}:{port} - {e}')
            finally:
                connection = self.server_connections.get(name)
                if connection and connection[1] and not connection[1].is_closing():
                    connection[1].close()
                    with contextlib.suppress(Exception):
                        await connection[1].wait_closed()
                self.server_connections[name] = (None, None, f'{address}:{port}', False, None)
                self.handshake_sent[name] = False
            sleep_for = min(delay, RECONNECT_INTERVAL) + random.uniform(0, 1)
            logging.debug(f'Retrying connection to server {name} in {sleep_for:.1f} seconds')
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, RECONNECT_INTERVAL)

    def _build_status_message(self):
        """Compose a more informative status payload for clients."""
        uptime = _format_duration(time.time() - self.start_time)
        client_count = len([w for w in self.client_writers if not w.is_closing()])
        primary = self.primary_server_name
        client_timeout = f'{self.client_timeout}s' if self.client_timeout else 'disabled'
        server_timeout = f'{self.server_timeout}s' if self.server_timeout else 'disabled'

        status_lines = [
            'Spot Collector Status',
            f'Uptime: {uptime}',
            f'Listening on: {self.listen_port}',
            f'Primary server: {primary}',
            f'Clients connected: {client_count}',
            f'Client timeout: {client_timeout}',
            f'Server timeout: {server_timeout}',
            '',
            'Upstream servers:',
        ]

        rows = []
        for server_name, address, alive, note, direction, connected_at in self._iter_server_status():
            state = 'Connected' if alive else 'Disconnected'
            uptime = _format_duration(time.time() - connected_at) if alive and connected_at else '-'
            rate = self._compute_rate_per_hour(server_name)
            rows.append({
                'name': server_name,
                'address': address,
                'state': state,
                'direction': direction,
                'rate': rate,
                'uptime': uptime,
                'note': note or ''
            })

        if rows:
            headers = ('Name', 'Address', 'State', 'Direction', 'Rate/hr', 'Up', 'Note')
            widths = {
                'name': len(headers[0]),
                'address': len(headers[1]),
                'state': len(headers[2]),
                'direction': len(headers[3]),
                'rate': len(headers[4]),
                'uptime': len(headers[5]),
                'note': len(headers[6])
            }

            for row in rows:
                widths['name'] = max(widths['name'], len(row['name']))
                widths['address'] = max(widths['address'], len(row['address']))
                widths['state'] = max(widths['state'], len(row['state']))
                widths['direction'] = max(widths['direction'], len(row['direction']))
                widths['rate'] = max(widths['rate'], len(row['rate']))
                widths['uptime'] = max(widths['uptime'], len(row['uptime']))
                widths['note'] = max(widths['note'], len(row['note']))

            header_line = (
                f"{headers[0]:<{widths['name']}}  "
                f"{headers[1]:<{widths['address']}}  "
                f"{headers[2]:<{widths['state']}}  "
                f"{headers[3]:<{widths['direction']}}  "
                f"{headers[4]:<{widths['rate']}}  "
                f"{headers[5]:<{widths['uptime']}}  "
                f"{headers[6]}"
            )
            divider_line = (
                f"{'-' * widths['name']}  "
                f"{'-' * widths['address']}  "
                f"{'-' * widths['state']}  "
                f"{'-' * widths['direction']}  "
                f"{'-' * widths['rate']}  "
                f"{'-' * widths['uptime']}  "
                f"{'-' * widths['note']}"
            )
            status_lines.append(header_line)
            status_lines.append(divider_line)

            for row in rows:
                status_lines.append(
                    f"{row['name']:<{widths['name']}}  "
                    f"{row['address']:<{widths['address']}}  "
                    f"{row['state']:<{widths['state']}}  "
                    f"{row['direction']:<{widths['direction']}}  "
                    f"{row['rate']:<{widths['rate']}}  "
                    f"{row['uptime']:<{widths['uptime']}}  "
                    f"{row['note']}"
                )
        else:
            status_lines.append('No upstream servers configured.')

        status_lines.append('')
        return '\n'.join(status_lines)

    async def handle_client(self, reader, writer):
        """Handle incoming client connections."""
        client_address = writer.get_extra_info('peername')
        logging.debug(f'New client connection from {client_address}')
        self.client_writers.append(writer)
        self.client_queues[writer] = asyncio.Queue(maxsize=100)
        sender_task = asyncio.create_task(self._client_sender(writer, self.client_queues[writer], f'client {client_address}'))
        self.client_sender_tasks[writer] = sender_task
        self._enable_tcp_keepalive(writer, f'client {client_address}')
        
        try:
            await self._enqueue_client(writer, self.login_prompt.encode(), f'client {client_address}')
            logging.debug(f'Sent login prompt to client {client_address}')

            while True:
                try:
                    if self.client_timeout:
                        data = await asyncio.wait_for(reader.read(1024), timeout=self.client_timeout)
                    else:
                        data = await reader.read(1024)
                    if not data:
                        logging.debug(f'No more data from client {client_address}')
                        break
                    sanitized_data = self._strip_telnet_control(data)
                    if not sanitized_data:
                        logging.debug(f'Ignoring Telnet control data from client {client_address}')
                        continue
                    message = sanitized_data.decode('utf-8', errors='ignore').strip()
                    logging.debug(f'Received data from client {client_address}: {message}')

                    if message.lower() == "status":
                        logging.debug(f'Received "status" command from client {client_address}, sending server status')
                        await self.send_status_to_single_client(writer)
                        continue

                    if message.lower() == "connect":
                        logging.debug(f'Received "connect" command from client {client_address}, reconnecting to all servers')
                        await self.connect_to_all_servers()
                        continue

                    if message.lower() == "list":
                        logging.debug(f'Received "list" command from client {client_address}, sending list of connected clients')
                        await self.list_connected_clients(writer)
                        continue

                    if message.lower() == "uptime":
                        logging.debug(f'Received "uptime" command from client {client_address}, sending server uptime')
                        await self.send_uptime(writer)
                        continue

                    for server_name, (_, server_writer, _, alive, _) in list(self.server_connections.items()):
                        if not alive or not server_writer or server_writer.is_closing():
                            continue
                        if not self._direction_allows_outbound(server_name):
                            continue
                        ok = await self._write_with_timeout(server_writer, sanitized_data, f'server {server_name}')
                        if ok:
                            logging.debug(f'Relayed data from client {client_address} to {server_name}')

                except asyncio.TimeoutError:
                    logging.debug(f'Client {client_address} inactive for {self.client_timeout} seconds')
                    break

        except (ConnectionResetError, BrokenPipeError) as e:
            logging.error(f'Connection to client {client_address} lost: {e}')
        finally:
            logging.debug(f'Closing client connection {client_address}')
            await self._cleanup_client(writer)

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
            if not alive:
                logging.debug(f'Server {server_name} is not connected, attempting to reconnect')
                self._start_connector(server_def, force_restart=True)

    async def relay_server_data(self, reader, writer, server_name):
        """Relay data from server to clients and handle server responses."""
        try:
            while True:
                data = await asyncio.wait_for(reader.read(1024), timeout=self.server_timeout)
                if not data:
                    logging.debug(f'No more data from server {server_name}')
                    break
                logging.debug(f'Received data from server {server_name}: {data}')
                self._record_server_message(server_name)

                lower_chunk = data.lower()
                prompt_match = re.search(rb'(call|callsign|login)\s*:?', lower_chunk)
                if prompt_match and not self.handshake_sent.get(server_name):
                    logging.debug(f'Received login prompt from {server_name}, sending callsign once')
                    await asyncio.sleep(2)  # 2-second delay for login response
                    if server_name == self.primary_server_name:
                        response = f"{self.callsign}\r\n".encode()  # Full callsign for Server1
                    else:
                        base_callsign = strip_callsign_suffix(self.callsign)
                        response = f"{base_callsign}\r\n".encode()  # Stripped callsign for others
                    await self._write_with_timeout(writer, response, f'server {server_name}')
                    self.handshake_sent[server_name] = True

                if self._direction_allows_inbound(server_name):
                    for client_writer in self.client_writers[:]:
                        if client_writer.is_closing():
                            continue
                        await self._enqueue_client(client_writer, data, f'client {client_writer.get_extra_info("peername")}')
                        logging.debug(f'Relayed data from server {server_name} to client')

        except asyncio.TimeoutError:
            logging.error(f'No data from {server_name} for {self.server_timeout} seconds, reconnecting...')
        except (ConnectionResetError, BrokenPipeError) as e:
            logging.error(f'Connection to server {server_name} lost: {e}')
        finally:
            logging.debug(f'Closing connection to server {server_name}')
            self.server_connections[server_name] = (None, None, self.server_connections[server_name][2], False, None)

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
            except Exception as e:
                logging.error(f'Status update task failed: {e}, restarting in 5 seconds')
                await asyncio.sleep(5)

    async def start_relay(self):
        """Start the relay server and manage all connections."""
        logging.debug('Starting relay')

        for server_def in self.server_definitions:
            self.server_connections.setdefault(server_def['name'], (None, None, server_def['address'], False, None))
            self.server_message_times.setdefault(server_def['name'], deque())
            self.handshake_sent.setdefault(server_def['name'], False)

        for server_def in self.server_definitions:
            self._start_connector(server_def)

        status_task = asyncio.create_task(self.send_status_to_clients())

        server = await asyncio.start_server(self.handle_client, '0.0.0.0', self.listen_port)
        logging.debug(f'Relay server started, listening on port {self.listen_port}')

        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            status_task.cancel()
            raise

if __name__ == "__main__":
    args = parse_arguments()

    configure_logging(args.debug)
    if args.debug:
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

    relay = TelnetRelay(server_definitions, notes, args.listen_port, args.callsign, args.login_prompt,
                        client_timeout=client_timeout, server_timeout=server_timeout,
                        server_directions=server_directions)
    asyncio.run(relay.start_relay())
