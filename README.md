# Spot Collector

**Spot Collector** is a lightweight Python-based Telnet relay tool designed to connect to multiple DX cluster servers and merge their data streams into a single output. It is ideal for consolidating spots from various clusters into one logging service—similar in spirit to wintelnetx.

## Overview

Spot Collector connects to up to four Telnet servers (DX clusters) concurrently. It listens for incoming client connections and relays messages between clients and the configured Telnet servers. The script supports a number of built-in commands (such as `status`, `connect`, `list`, and `uptime`) to help you monitor and control its behavior.

When the script receives specific prompts (e.g., “call:”, “sign:”, or “login”), it sends the appropriate callsign:
- **Server1** receives the full callsign (e.g., `S53M-23`). The 1st server will also receive all user input, e.g. sh/dx, sh/u, dx 14230 S55OO No ears etc.
- **Other servers** receive a modified callsign with any trailing numeric suffix removed (e.g., `S53M`).

## Features

- **Multi-Server Connectivity:** Connect to up to four DX cluster servers simultaneously.
- **Automatic Reconnection:** Automatically attempts to reconnect if a server connection is lost.
- **Client Commands:** Supports commands for checking status, reconnecting servers, listing connected clients, and viewing uptime.
- **Customizable Callsign:** Sends a full callsign to the primary server while stripping the numeric suffix for secondary servers.
- **SystemD Integration:** Easily run as a background service using the provided SystemD unit file.

## Requirements

- **Python 3.7+** – The script uses Python's `asyncio` library and standard modules.
- **SystemD** (optional) – To use the provided service file for running Spot Collector as a service on Unix-like systems.

## Installation

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/s53zo/spot-collector.git
   cd spot-collector

2. **(Optional) Set Up a Virtual Environment:**

    ```bash
    python3 -m venv venv
    source venv/bin/activate

3. **Command-Line Arguments**
Run the script using the following required arguments:

   ```bash
    --server1: Address and port of the primary DX cluster server (e.g., s50dxs.s53m.com:8000).
    --server2: Address and port of the secondary DX cluster server.
    --listen-port: Port on which the relay listens for incoming connections.
    --callsign: Callsign to be sent. The primary server gets the full callsign (e.g., S53M-23), while other servers get the base callsign (e.g., S53M).

    Additional optional arguments include:

    --server3 and --server4: Addresses and ports for additional servers.
    --note1, --note2, --note3, --note4: Descriptive notes for each server.
    --debug: Enable debug logging for troubleshooting.

4. **Example command:**
   ```bash
   python3 spot-collector.py \
     --server1 s50dxs.s53m.com:8000 \
     --server2 10.0.10.101:7300 \
     --server3 10.0.10.104:7300 \
     --server4 10.0.10.154:7373 \
     --listen-port 8000 \
     --callsign S53M-23 \
     --note1 "S50DXS" \
     --note2 "Local Skimmer 1" \
     --note3 "Local Skimmer 2" \
     --note4 "Flexradio Skimmer"


 
