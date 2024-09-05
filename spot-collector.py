import asyncio
import logging
import argparse
import re
import time

RECONNECT_INTERVAL = 300  # 5 minutes
STATUS_INTERVAL = 300  # 5 minutes
INACTIVITY_TIMEOUT = 300  # 5 minutes

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Telnet Relay Script for relaying data between multiple Telnet servers and clients.\n"
                    "This script connects to multiple Telnet servers and relays data from clients to these servers. "
                    "The first server receives the full callsign as provided, while other servers receive a modified "
                    "callsign without the suffix (e.g., '-23'). The script also handles 'status', 'connect', 'list', "
                    "and 'uptime' commands from clients to manage connections and display information."
    )
    parser.add_argument('--server1', type=str, required=True, help="Address and port of the first server in the format address:port. Data from clients will be relayed only to this server.")
    parser.add_argument('--server2', type=str, required=True, help="Address and port of the second server in the format address:port")
    parser.add_argument('--server3', type=str, help="Address and port of the third server in the format address:port")
    parser.add_argument('--server4', type=str, help="Address and port of the fourth server in the format address:port")
    parser.add_argument('--listen-port', type=int, required=True, help="Port on which the relay listens for incoming connections")
    parser.add_argument('--callsign', type=str, required=True, help="Callsign to send when 'call:' or 'login:' is received. The full callsign is sent to the first server (e.g., S53M-23); a modified version without the suffix is sent to others (e.g., S53M).")
    parser.add_argument('--note1', type=str, help="Note for the first server")
    parser.add_argument('--note2', type=str, help="Note for the second server")
    parser.add_argument('--note3', type=str, help="Note for the third server")
    parser.add_argument('--note4', type=str, help="Note for the fourth server")
    parser.add_argument('--debug', action='store_true', help="Enable debug logging")

    return parser.parse_args()

def strip_callsign_suffix(callsign):
    """
    Strip the suffix from the callsign if it exists. 
    The suffix is defined as '-' followed by one or more digits.
    """
    return re.sub(r'-\d+$', '', callsign)

class TelnetRelay:
    def __init__(self, servers, notes, listen_port, callsign):
        self.servers = servers
        self.notes = notes
        self.listen_port = listen_port
        self.callsign = callsign
        self.client_writers = []
        self.server_connections = {}  # Use a dictionary to store server connections
        self.start_time = time.time()  # Track when the server started
        logging.debug(f'TelnetRelay initialized with servers: {servers}, listen_port: {listen_port}, callsign: {callsign}, notes: {notes}')

    async def connect_to_server(self, address, port, server_name):
        logging.debug(f'Connecting to server {address}:{port}')
        while True:
            try:
                reader, writer = await asyncio.open_connection(address, port)
                logging.debug(f'Connected to server {address}:{port}')
                self.server_connections[server_name] = (reader, writer, address)
                asyncio.create_task(self.relay_server_data(reader, writer, server_name))
                break
            except Exception as e:
                logging.error(f'Error connecting to server {address}:{port} - {e}')
                logging.debug(f'Retrying connection to server {address}:{port} in {RECONNECT_INTERVAL} seconds')
                await asyncio.sleep(RECONNECT_INTERVAL)

    async def handle_client(self, reader, writer):
        client_address = writer.get_extra_info('peername')
        logging.debug(f'New client connection from {client_address}')
        self.client_writers.append(writer)
        try:
            while True:
                data = await reader.read(100)
                if not data:
                    logging.debug(f'No more data from client {client_address}')
                    break
                try:
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

                    server1_writer = self.server_connections.get('Server1', (None, None, None))[1]
                    if server1_writer:
                        server1_writer.write(data)
                        await server1_writer.drain()
                        logging.debug(f'Relayed data from client {client_address} to server1')

                except UnicodeDecodeError as e:
                    logging.error(f'Failed to decode data from client {client_address}: {e}')
                    # Optionally: close the connection or ignore the message
                    break

        except (ConnectionResetError, BrokenPipeError) as e:
            logging.error(f'Connection to client {client_address} lost: {e}')
        finally:
            logging.debug(f'Closing client connection {client_address}')
            self.client_writers.remove(writer)
            writer.close()
            await writer.wait_closed()

    async def list_connected_clients(self, writer):
        """
        List all currently connected clients.
        """
        clients = "\n".join([str(writer.get_extra_info('peername')) for writer in self.client_writers])
        status_message = f"Connected clients:\n{clients}\n"
        writer.write(status_message.encode())
        await writer.drain()

    async def send_uptime(self, writer):
        """
        Send the uptime of the relay server.
        """
        uptime_seconds = time.time() - self.start_time
        uptime_message = f"Server Uptime: {uptime_seconds:.2f} seconds\n"
        writer.write(uptime_message.encode())
        await writer.drain()

    async def send_status_to_single_client(self, writer):
        """
        Send the server connection status to a single client.
        """
        status_message = "Server Connection Status:\n"
        for server_name, connection in self.server_connections.items():
            address = connection[2]
            note = self.notes.get(server_name, "")
            status_message += f"{server_name} ({address}): {'Connected' if connection else 'Disconnected'} - {note}\n"
        status_message += "\n"
        writer.write(status_message.encode())
        await writer.drain()

    async def connect_to_all_servers(self):
        """
        Attempt to reconnect to all servers that are currently not connected.
        """
        for server_name, (reader, writer, address) in self.server_connections.items():
            if reader is None or reader.at_eof():
                logging.debug(f'Server {server_name} is not connected, attempting to reconnect')
                address, port = address.split(':')
                await self.connect_to_server(address, int(port), server_name)

    async def relay_server_data(self, reader, writer, server_name):
        try:
            while True:
                data = await asyncio.wait_for(reader.read(100), timeout=INACTIVITY_TIMEOUT)
                if not data:
                    logging.debug(f'No more data from server {server_name}')
                    break
                logging.debug(f'Received data from server {server_name}: {data}')

                if b"call" in data or b"sign:" in data or b"login" in data:
                    logging.debug(f'Received "call:" or "callsign:" or "login:" from {server_name}, sending response to servers')

                    # Determine which callsign to send based on the server
                    if server_name == 'Server1':
                        response = f"{self.callsign}\r\n".encode()  # Send full callsign to the first server
                    else:
                        base_callsign = strip_callsign_suffix(self.callsign)
                        response = f"{base_callsign}\r\n".encode()  # Send stripped callsign to other servers

                    writer.write(response)
                    await writer.drain()

                for client_writer in self.client_writers:
                    client_writer.write(data)
                    await client_writer.drain()

                logging.debug(f'Relayed data from server {server_name} to clients')
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError) as e:
            if isinstance(e, asyncio.TimeoutError):
                logging.error(f'No data received from server {server_name} for {INACTIVITY_TIMEOUT} seconds, reconnecting...')
            else:
                logging.error(f'Connection to server {server_name} lost: {e}')
        finally:
            logging.debug(f'Closing connection to server {server_name}')
            writer.close()
            await writer.wait_closed()
            await self.reconnect_to_server(server_name)

    async def reconnect_to_server(self, server_name):
        address, port = next(
            (addr.split(':') for name, addr in zip([f'Server{i+1}' for i in range(len(self.servers))], self.servers) if name == server_name),
            (None, None)
        )
        if address and port:
            del self.server_connections[server_name]
            await self.connect_to_server(address, int(port), server_name)

    async def send_status_to_clients(self):
        while True:
            status_message = "Server Connection Status:\n"
            for server_name, connection in self.server_connections.items():
                address = connection[2]
                note = self.notes.get(server_name, "")
                status_message += f"{server_name} ({address}): {'Connected' if connection else 'Disconnected'} - {note}\n"
            status_message += "\n"
            for client_writer in self.client_writers:
                client_writer.write(status_message.encode())
                await client_writer.drain()
            await asyncio.sleep(STATUS_INTERVAL)

    async def start_relay(self):
        logging.debug('Starting relay')

        for i, server in enumerate(self.servers):
            if server:
                addr, port = server.split(':')
                asyncio.create_task(self.connect_to_server(addr, int(port), f'Server{i+1}'))

        asyncio.create_task(self.send_status_to_clients())

        server = await asyncio.start_server(
            self.handle_client,
            '0.0.0.0', self.listen_port
        )
        logging.debug(f'Relay server started, listening on port {self.listen_port}')

        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    args = parse_arguments()

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

    relay = TelnetRelay(servers, notes, args.listen_port, args.callsign)
    asyncio.run(relay.start_relay())
