import argparse
import asyncio
import json
import logging
import re
import socket
import time

RECONNECT_INTERVAL = 300  # 5 minutes max delay for reconnect attempts
STATUS_INTERVAL = 300     # 5 minutes between status updates

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

        for key, value in config_data.items():
            if not hasattr(defaults, key):
                parser.error(f'Unknown configuration key: {key}')
            if isinstance(value, str) and key.endswith('_direction'):
                value = value.lower()
            setattr(defaults, key, value)

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
        return defaults

    parser = _create_parser(require_required_flags=True)
    args = parser.parse_args()
    args.config = None
    return args

def strip_callsign_suffix(callsign):
    """
    Strip the suffix from the callsign if it exists. 
    The suffix is defined as '-' followed by one or more digits.
    """
    return re.sub(r'-\d+$', '', callsign)

class TelnetRelay:
    def __init__(self, servers, notes, listen_port, callsign, login_prompt, client_timeout=None, server_timeout=None, server_directions=None):
        self.servers = servers
        self.notes = notes
        self.listen_port = listen_port
        self.callsign = callsign
        self.login_prompt = login_prompt
        self.client_writers = []
        self.server_connections = {}  # server_name: (reader, writer, address, alive)
        self.start_time = time.time()  # Track when the server started
        self.client_timeout = client_timeout
        self.server_timeout = server_timeout
        self.server_directions = server_directions or {}
        logging.debug(f'TelnetRelay initialized with servers: {servers}, listen_port: {listen_port}, '
                      f'callsign: {callsign}, notes: {notes}, login_prompt: {login_prompt}, '
                      f'client_timeout: {client_timeout}, server_timeout: {server_timeout}, '
                      f'server_directions: {self.server_directions}')

    def _direction_allows_outbound(self, server_name):
        direction = self.server_directions.get(server_name, 'both')
        return direction in ('out', 'both')

    def _direction_allows_inbound(self, server_name):
        direction = self.server_directions.get(server_name, 'both')
        return direction in ('in', 'both')

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

    async def connect_to_server(self, address, port, server_name):
        """Connect to a server with exponential backoff on failure."""
        logging.debug(f'Connecting to server {address}:{port}')
        delay = 1  # Start with 1-second delay
        while True:
            try:
                reader, writer = await asyncio.open_connection(address, port)
                logging.debug(f'Connected to server {address}:{port}')
                self.server_connections[server_name] = (reader, writer, f'{address}:{port}', True)
                self._enable_tcp_keepalive(writer, f'server connection {server_name}')
                asyncio.create_task(self.relay_server_data(reader, writer, server_name))
                return
            except Exception as e:
                logging.error(f'Error connecting to server {address}:{port} - {e}')
                logging.debug(f'Retrying connection to server {address}:{port} in {delay} seconds')
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_INTERVAL)

    async def handle_client(self, reader, writer):
        """Handle incoming client connections."""
        client_address = writer.get_extra_info('peername')
        logging.debug(f'New client connection from {client_address}')
        self.client_writers.append(writer)
        self._enable_tcp_keepalive(writer, f'client {client_address}')
        
        try:
            writer.write(self.login_prompt.encode())
            await writer.drain()
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
                    message = data.decode('utf-8').strip()
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

                    for server_name, (_, server_writer, _, alive) in list(self.server_connections.items()):
                        if not alive or not server_writer or server_writer.is_closing():
                            continue
                        if not self._direction_allows_outbound(server_name):
                            continue
                        server_writer.write(data)
                        await server_writer.drain()
                        logging.debug(f'Relayed data from client {client_address} to {server_name}')

                except asyncio.TimeoutError:
                    logging.debug(f'Client {client_address} inactive for {self.client_timeout} seconds')
                    break
                except UnicodeDecodeError as e:
                    logging.error(f'Failed to decode data from client {client_address}: {e}')
                    break

        except (ConnectionResetError, BrokenPipeError) as e:
            logging.error(f'Connection to client {client_address} lost: {e}')
        finally:
            logging.debug(f'Closing client connection {client_address}')
            if writer in self.client_writers:
                self.client_writers.remove(writer)
            writer.close()
            await writer.wait_closed()

    async def list_connected_clients(self, writer):
        """List all currently connected clients."""
        clients = "\n".join([str(writer.get_extra_info('peername')) for writer in self.client_writers])
        status_message = f"Connected clients:\n{clients}\n"
        writer.write(status_message.encode())
        await writer.drain()

    async def send_uptime(self, writer):
        """Send the uptime of the relay server."""
        uptime_seconds = time.time() - self.start_time
        uptime_message = f"Server Uptime: {uptime_seconds:.2f} seconds\n"
        writer.write(uptime_message.encode())
        await writer.drain()

    async def send_status_to_single_client(self, writer):
        """Send the server connection status to a single client."""
        status_message = "Server Connection Status:\n"
        for server_name, (_, _, address, alive) in self.server_connections.items():
            note = self.notes.get(server_name, "")
            direction = self.server_directions.get(server_name, 'both')
            status_message += f"{server_name} ({address}): {'Connected' if alive else 'Disconnected'} - {note} [direction: {direction}]\n"
        status_message += "\n"
        writer.write(status_message.encode())
        await writer.drain()

    async def connect_to_all_servers(self):
        """Attempt to reconnect to all servers that are currently not connected."""
        for server_name, (_, _, address, alive) in list(self.server_connections.items()):
            if not alive:
                logging.debug(f'Server {server_name} is not connected, attempting to reconnect')
                address, port = address.split(':')
                await self.connect_to_server(address, int(port), server_name)

    async def relay_server_data(self, reader, writer, server_name):
        """Relay data from server to clients and handle server responses."""
        try:
            while True:
                if self.server_timeout:
                    data = await asyncio.wait_for(reader.read(1024), timeout=self.server_timeout)
                else:
                    data = await reader.read(1024)
                if not data:
                    logging.debug(f'No more data from server {server_name}')
                    break
                logging.debug(f'Received data from server {server_name}: {data}')

                if b"call" in data or b"sign:" in data or b"login" in data:
                    logging.debug(f'Received "call:" or "callsign:" or "login:" from {server_name}, sending response')
                    await asyncio.sleep(2)  # 2-second delay for login response
                    if server_name == 'Server1':
                        response = f"{self.callsign}\r\n".encode()  # Full callsign for Server1
                    else:
                        base_callsign = strip_callsign_suffix(self.callsign)
                        response = f"{base_callsign}\r\n".encode()  # Stripped callsign for others
                    writer.write(response)
                    await writer.drain()

                if self._direction_allows_inbound(server_name):
                    for client_writer in self.client_writers[:]:
                        if not client_writer.is_closing():
                            client_writer.write(data)
                            await client_writer.drain()
                            logging.debug(f'Relayed data from server {server_name} to client')

        except asyncio.TimeoutError:
            logging.error(f'No data from {server_name} for {self.server_timeout} seconds, reconnecting...')
        except (ConnectionResetError, BrokenPipeError) as e:
            logging.error(f'Connection to server {server_name} lost: {e}')
        finally:
            logging.debug(f'Closing connection to server {server_name}')
            self.server_connections[server_name] = (None, None, self.server_connections[server_name][2], False)
            writer.close()
            await writer.wait_closed()
            await self.reconnect_to_server(server_name)

    async def reconnect_to_server(self, server_name):
        """Reconnect to a specific server after disconnection."""
        address, port = self.server_connections[server_name][2].split(':')
        await self.connect_to_server(address, int(port), server_name)

    async def send_status_to_clients(self):
        """Periodically send connection status to all connected clients."""
        while True:
            try:
                status_message = "Server Connection Status:\n"
                for server_name, (_, _, address, alive) in self.server_connections.items():
                    note = self.notes.get(server_name, "")
                    direction = self.server_directions.get(server_name, 'both')
                    status_message += f"{server_name} ({address}): {'Connected' if alive else 'Disconnected'} - {note} [direction: {direction}]\n"
                status_message += "\n"
                for client_writer in self.client_writers[:]:
                    if not client_writer.is_closing():
                        client_writer.write(status_message.encode())
                        await client_writer.drain()
                await asyncio.sleep(STATUS_INTERVAL)
            except Exception as e:
                logging.error(f'Status update task failed: {e}, restarting in 5 seconds')
                await asyncio.sleep(5)

    async def start_relay(self):
        """Start the relay server and manage all connections."""
        logging.debug('Starting relay')

        for i, server in enumerate(self.servers):
            if server:
                addr, port = server.split(':')
                asyncio.create_task(self.connect_to_server(addr, int(port), f'Server{i+1}'))

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

    servers = [args.server1, args.server2, args.server3, args.server4]
    notes = {
        'Server1': args.note1 or '',
        'Server2': args.note2 or '',
        'Server3': args.note3 or '',
        'Server4': args.note4 or ''
    }
    server_directions = {
        'Server1': args.server1_direction,
        'Server2': args.server2_direction,
        'Server3': args.server3_direction,
        'Server4': args.server4_direction
    }

    client_timeout = args.client_timeout if args.client_timeout > 0 else None
    server_timeout = args.server_timeout if args.server_timeout > 0 else None

    relay = TelnetRelay(servers, notes, args.listen_port, args.callsign, args.login_prompt,
                        client_timeout=client_timeout, server_timeout=server_timeout,
                        server_directions=server_directions)
    asyncio.run(relay.start_relay())
