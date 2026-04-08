"""
=============================================================================
  CMPT 371 A3: Walkie-Talkie Voice Chat (Push-to-Talk Application)
  WalkiePy - Push-to-Talk Voice Chat Client
=============================================================================
  Architecture : Client-Server over UDP
  Transport    : UDP (User Datagram Protocol)
  Purpose      : Receives audio packets from a transmitting client and
                 broadcasts them in real-time to all other connected clients.

  Server responsibilities:
    - Maintain a registry of connected clients (heartbeat-based).
    - Receive audio packets from any transmitting client.
    - Broadcast those packets to every other registered client.
    - Broadcast text chat messages to all clients.
    - Remove stale clients that stop sending heartbeats.
=============================================================================
Reference: Claude used to help create the interface/frontend and clean-up extensive comments.
"""

import socket
import threading
import time
import json
import logging
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
#  Logging Configuration
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("WalkiePy-Server")

# ─────────────────────────────────────────────────────────────────────────────
#  Protocol Constants
#  All packets begin with a 1-byte TYPE header so the server/client can
#  quickly route incoming data without parsing the full payload.
# ─────────────────────────────────────────────────────────────────────────────
PKT_REGISTER   = 0x01   # Client announces itself to the server
PKT_HEARTBEAT  = 0x02   # Client keeps its slot alive (sent every ~2 s)
PKT_AUDIO      = 0x03   # Raw PCM audio chunk
PKT_CHAT       = 0x04   # UTF-8 encoded text message
PKT_DISCONNECT = 0x05   # Graceful client shutdown
PKT_CLIENT_LIST= 0x06   # Server → client: current user list
PKT_TRANSMIT   = 0x07   # Server → client: who is currently transmitting
PKT_STOP_TX    = 0x08   # Client → server: PTT released

# How many seconds of silence before a client is considered gone
HEARTBEAT_TIMEOUT = 8   # seconds

# Maximum UDP payload size (safe below typical MTU of 1500 bytes)
MAX_PACKET_SIZE = 65535


class WalkieTalkieServer:
    """
    Central hub for all WalkiePy clients.

    Thread model
    ────────────
    • Main thread          - spins up everything, then blocks on input()
    • receive_loop thread  - single UDP recv() loop; dispatches by packet type
    • cleanup_loop thread  - evicts clients that missed heartbeats
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9000): # Default host is local network (same machine)
        self.host = host
        self.port = port

        # addr → {"username": str, "last_seen": float}
        self.clients: dict[tuple, dict] = {}
        self.clients_lock = threading.Lock()

        # Address of whoever currently holds the PTT floor (or None)
        self.current_transmitter: tuple | None = None

        # Create and bind the UDP socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))

        self.running = False

    # ──────────────────────────────────────────────────────────────────────
    #  Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    def start(self):
        """Start background threads and begin serving."""
        self.running = True
        log.info(f"Server listening on {self.host}:{self.port}  (UDP)")

        # Background: receive all incoming UDP packets
        recv_thread = threading.Thread(target=self._receive_loop, daemon=True)
        recv_thread.start()

        # Background: evict clients that stopped sending heartbeats
        cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        cleanup_thread.start()

        log.info("Server ready. Press Ctrl+C to stop.")
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """Gracefully shut down the server."""
        log.info("Shutting down server…")
        self.running = False
        self.sock.close()

    # ──────────────────────────────────────────────────────────────────────
    #  Receive Loop  (runs on its own thread)
    # ──────────────────────────────────────────────────────────────────────

    def _receive_loop(self):
        """
        Continuously read from the UDP socket and dispatch each packet
        to the appropriate handler based on its first byte (packet type).
        """
        while self.running:
            try:
                data, addr = self.sock.recvfrom(MAX_PACKET_SIZE)
            except OSError:
                # Socket was closed — time to exit
                break

            if not data:
                continue

            pkt_type = data[0]

            if pkt_type == PKT_REGISTER:
                self._handle_register(data[1:], addr)

            elif pkt_type == PKT_HEARTBEAT:
                self._handle_heartbeat(addr)

            elif pkt_type == PKT_AUDIO:
                self._handle_audio(data[1:], addr)
            
            elif pkt_type == PKT_STOP_TX:
                self._handle_stop_transmit(addr)

            elif pkt_type == PKT_CHAT:
                self._handle_chat(data[1:], addr)

            elif pkt_type == PKT_DISCONNECT:
                self._handle_disconnect(addr)

    # ──────────────────────────────────────────────────────────────────────
    #  Packet Handlers
    # ──────────────────────────────────────────────────────────────────────

    def _handle_register(self, payload: bytes, addr: tuple):
        """
        Register a new client.
        Payload format: UTF-8 encoded username string.
        """
        username = payload.decode("utf-8", errors="replace").strip()
        if not username:
            username = f"User_{addr[1]}"

        with self.clients_lock:
            self.clients[addr] = {
                "username": username,
                "last_seen": time.time(),
            }

        log.info(f"+ JOINED  {username}  ({addr[0]}:{addr[1]})")
        self._broadcast_client_list()
        self._send_system_chat(f"{username} joined the channel.")

    def _handle_heartbeat(self, addr: tuple):
        """Refresh the last-seen timestamp for a client."""
        with self.clients_lock:
            if addr in self.clients:
                self.clients[addr]["last_seen"] = time.time()

    def _handle_audio(self, audio_data: bytes, sender_addr: tuple):
        """
        Relay raw PCM audio to every client except the sender.

        Packet structure sent to listeners:
          [PKT_AUDIO (1 byte)] [username_len (1 byte)] [username] [raw PCM]
        """
        with self.clients_lock:
            if sender_addr not in self.clients:
                return
            sender_name = self.clients[sender_addr]["username"]
            self.clients[sender_addr]["last_seen"] = time.time()
            recipients = [a for a in self.clients if a != sender_addr]

        # Mark who is transmitting so GUIs can show an indicator
        if self.current_transmitter != sender_addr:
            self.current_transmitter = sender_addr
            self._broadcast_transmitter(sender_name)

        # Build forwarding packet: type + name length + name + PCM bytes
        name_bytes = sender_name.encode("utf-8")
        header = bytes([PKT_AUDIO, len(name_bytes)]) + name_bytes
        packet = header + audio_data

        for addr in recipients:
            try:
                self.sock.sendto(packet, addr)
            except OSError as e:
                log.warning(f"Could not send audio to {addr}: {e}")

    def _handle_stop_transmit(self, addr: tuple):
        """Client released PTT — clear the floor and notify everyone."""
        if self.current_transmitter == addr:
            self.current_transmitter = None
            self._broadcast_transmitter("")   # empty string = nobody transmitting

    def _handle_chat(self, payload: bytes, sender_addr: tuple):
        """
        Broadcast a chat message to all connected clients.
        Payload format: UTF-8 JSON → {"msg": "..."}
        Forwarded packet: [PKT_CHAT] [JSON bytes]
        """
        with self.clients_lock:
            if sender_addr not in self.clients:
                return
            sender_name = self.clients[sender_addr]["username"]
            self.clients[sender_addr]["last_seen"] = time.time()
            all_clients = list(self.clients.keys())

        try:
            body = json.loads(payload.decode("utf-8"))
            text = body.get("msg", "").strip()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not text:
            return

        timestamp = datetime.now().strftime("%H:%M")
        envelope = json.dumps({
            "sender": sender_name,
            "msg": text,
            "time": timestamp,
        }).encode("utf-8")

        packet = bytes([PKT_CHAT]) + envelope
        log.info(f"[CHAT] {sender_name}: {text}")

        for addr in all_clients:
            try:
                self.sock.sendto(packet, addr)
            except OSError as e:
                log.warning(f"Could not send chat to {addr}: {e}")

    def _handle_disconnect(self, addr: tuple):
        """Remove a client that sent a graceful disconnect packet."""
        with self.clients_lock:
            info = self.clients.pop(addr, None)

        if info:
            log.info(f"- LEFT    {info['username']}  ({addr[0]}:{addr[1]})")
            self._broadcast_client_list()
            self._send_system_chat(f"{info['username']} left the channel.")

        if self.current_transmitter == addr:
            self.current_transmitter = None

    # ──────────────────────────────────────────────────────────────────────
    #  Broadcast Helpers
    # ──────────────────────────────────────────────────────────────────────

    def _broadcast_client_list(self):
        """
        Push an updated user list to every client.
        Packet: [PKT_CLIENT_LIST] [JSON: list of usernames]
        """
        with self.clients_lock:
            names = [v["username"] for v in self.clients.values()]
            all_addrs = list(self.clients.keys())

        payload = json.dumps(names).encode("utf-8")
        packet = bytes([PKT_CLIENT_LIST]) + payload

        for addr in all_addrs:
            try:
                self.sock.sendto(packet, addr)
            except OSError:
                pass

    def _broadcast_transmitter(self, username: str):
        """
        Notify all clients who is currently transmitting.
        Packet: [PKT_TRANSMIT] [UTF-8 username]
        """
        packet = bytes([PKT_TRANSMIT]) + username.encode("utf-8")
        with self.clients_lock:
            all_addrs = list(self.clients.keys())

        for addr in all_addrs:
            try:
                self.sock.sendto(packet, addr)
            except OSError:
                pass

    def _send_system_chat(self, message: str):
        """Broadcast a server-generated system message as a chat packet."""
        timestamp = datetime.now().strftime("%H:%M")
        envelope = json.dumps({
            "sender": "System",
            "msg": message,
            "time": timestamp,
        }).encode("utf-8")

        packet = bytes([PKT_CHAT]) + envelope
        with self.clients_lock:
            all_addrs = list(self.clients.keys())

        for addr in all_addrs:
            try:
                self.sock.sendto(packet, addr)
            except OSError:
                pass

    # ──────────────────────────────────────────────────────────────────────
    #  Cleanup Loop  (runs on its own thread)
    # ──────────────────────────────────────────────────────────────────────

    def _cleanup_loop(self):
        """
        Every 5 seconds, check all registered clients.
        Any client whose last heartbeat is older than HEARTBEAT_TIMEOUT
        is considered disconnected and removed from the registry.
        """
        while self.running:
            time.sleep(5)
            now = time.time()
            stale = []

            with self.clients_lock:
                for addr, info in self.clients.items():
                    if now - info["last_seen"] > HEARTBEAT_TIMEOUT:
                        stale.append((addr, info["username"]))

            for addr, username in stale:
                log.warning(f"⚠ TIMEOUT  {username}  ({addr[0]}:{addr[1]})")
                self._handle_disconnect(addr)


# ─────────────────────────────────────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="WalkiePy UDP Voice Chat Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9000, help="UDP port (default: 9000)")
    args = parser.parse_args()

    server = WalkieTalkieServer(host=args.host, port=args.port)
    server.start()