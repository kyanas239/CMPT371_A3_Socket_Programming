"""
=============================================================================
  CMPT 371 A3: Walkie-Talkie Voice Chat (Push-to-Talk Application)
  WalkiePy - Push-to-Talk Voice Chat Client
=============================================================================
  Architecture : UDP client connecting to a WalkiePy server
  GUI          : Tkinter (built-in Python) — styled with a dark military/
                 radio aesthetic using custom Canvas drawing and ttk styling.

  Key design decisions
  ─────────────────────
  • UDP transport   - matches server; low-latency for real-time audio.
  • PyAudio         cross-platform audio I/O (PortAudio backend).
  • Threading       - audio capture and network I/O each run on daemon
                     threads so the Tkinter event loop stays responsive.
  • Push-to-Talk    - SPACE bar (held) OR the GUI button triggers transmission (TX).
                     Releasing stops transmission and clears the floor.
  • Heartbeat       - a lightweight UDP packet sent every 2 seconds keeps the
                     server from timing out the connection.

  Thread map
  ──────────
  Main thread          → Tkinter event loop
  _receive_loop        → UDP socket recv; dispatches by packet type
  _heartbeat_loop      → sends PKT_HEARTBEAT every 2 seconds
  _audio_capture_loop  → reads from mic and sends PKT_AUDIO when PTT held
  _audio_playback_loop → reads from playback queue and feeds PyAudio stream
=============================================================================
Reference: Claude used to help create the interface/frontend and clean up extensive comments.
"""

import socket
import threading
import queue
import time
import json
import logging
import tkinter as tk
from tkinter import messagebox
from datetime import datetime

# Graceful degradation if PyAudio missing
try:
    import pyaudio
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("WalkiePy-Client")

# ─────────────────────────────────────────────────────────────────────────────
#  Protocol constants  (must mirror server.py exactly)
# ─────────────────────────────────────────────────────────────────────────────
PKT_REGISTER    = 0x01
PKT_HEARTBEAT   = 0x02
PKT_AUDIO       = 0x03
PKT_CHAT        = 0x04
PKT_DISCONNECT  = 0x05
PKT_CLIENT_LIST = 0x06
PKT_TRANSMIT    = 0x07

# ─────────────────────────────────────────────────────────────────────────────
#  Audio constants
#  16 kHz, mono, 16-bit PCM is the sweet spot: good speech quality, low
#  bandwidth (~32 KB/s uncompressed), and universally supported.
# ─────────────────────────────────────────────────────────────────────────────
AUDIO_RATE      = 16000   # samples per second
AUDIO_CHANNELS  = 1       # mono
AUDIO_FORMAT    = pyaudio.paInt16 if AUDIO_AVAILABLE else None
CHUNK_SIZE      = 1024    # frames per buffer (~64 ms at 16 kHz)
MAX_PACKET_SIZE = 65535

# ─────────────────────────────────────────────────────────────────────────────
#  Design Palette  (dark military-radio aesthetic)
# ─────────────────────────────────────────────────────────────────────────────
PALETTE = {
    "bg_dark":      "#0d0f0e",   # near-black background
    "bg_panel":     "#141a16",   # slightly lighter panel
    "bg_input":     "#1a2420",   # input fields
    "accent":       "#39ff7a",   # electric green — the "signal" color
    "accent_dim":   "#1a5c35",   # dimmed green for inactive states
    "accent_tx":    "#ff4c2b",   # red for active transmission
    "text_primary": "#d4e8dc",   # soft light green-white
    "text_dim":     "#4a6b57",   # muted text
    "border":       "#1f3028",   # subtle border
    "chat_system":  "#4a9e6e",   # system message color
    "chat_sender":  "#39ff7a",   # own message label
    "chat_other":   "#7dd4a8",   # others' message label
}


# =============================================================================
#  NetworkClient  –  handles all UDP socket I/O
# =============================================================================

class NetworkClient:
    """
    Manages the UDP socket connection to the WalkiePy server.
    Separates all networking logic from the GUI layer.
    """

    def __init__(self, server_host: str, server_port: int, username: str,
                 on_audio, on_chat, on_client_list, on_transmit, on_error):
        """
        Parameters
        ──────────
        on_audio       : callable(sender: str, pcm_data: bytes)
        on_chat        : callable(sender: str, msg: str, timestamp: str)
        on_client_list : callable(names: list[str])
        on_transmit    : callable(username: str)
        on_error       : callable(message: str)
        """
        self.server_addr = (server_host, server_port)
        self.username    = username

        # Callbacks into the GUI / audio layers
        self.on_audio       = on_audio
        self.on_chat        = on_chat
        self.on_client_list = on_client_list
        self.on_transmit    = on_transmit
        self.on_error       = on_error

        self.sock    = None
        self.running = False

    def connect(self):
        """Open the UDP socket and register with the server."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Non-blocking recv with a 1-second timeout so the loop can check
        # the self.running flag periodically
        self.sock.settimeout(1.0)
        self.running = True

        # Send registration packet
        payload = bytes([PKT_REGISTER]) + self.username.encode("utf-8")
        self.sock.sendto(payload, self.server_addr)
        log.info(f"Registered as '{self.username}' at {self.server_addr}")

        # Start background threads
        threading.Thread(target=self._receive_loop,    daemon=True).start()
        threading.Thread(target=self._heartbeat_loop,  daemon=True).start()

    def disconnect(self):
        """Send a graceful disconnect and close the socket."""
        if self.sock and self.running:
            try:
                self.sock.sendto(bytes([PKT_DISCONNECT]), self.server_addr)
            except OSError:
                pass
        self.running = False
        if self.sock:
            self.sock.close()

    # ──────────────────────────────────────────────────────────────────────
    #  Send helpers
    # ──────────────────────────────────────────────────────────────────────

    def send_audio(self, pcm_data: bytes):
        """Transmit a raw PCM chunk to the server (push-to-talk)."""
        if self.sock and self.running:
            packet = bytes([PKT_AUDIO]) + pcm_data
            try:
                self.sock.sendto(packet, self.server_addr)
            except OSError as e:
                log.warning(f"Audio send error: {e}")

    def send_chat(self, message: str):
        """Send a text chat message to the server."""
        if self.sock and self.running:
            payload = json.dumps({"msg": message}).encode("utf-8")
            packet  = bytes([PKT_CHAT]) + payload
            try:
                self.sock.sendto(packet, self.server_addr)
            except OSError as e:
                log.warning(f"Chat send error: {e}")

    # ──────────────────────────────────────────────────────────────────────
    #  Background loops
    # ──────────────────────────────────────────────────────────────────────

    def _receive_loop(self):
        """
        Continuously receive UDP packets and send off to the correct callback.
        Handles the 1-second timeout gracefully to allow clean shutdown.
        """
        while self.running:
            try:
                data, _ = self.sock.recvfrom(MAX_PACKET_SIZE)
            except socket.timeout:
                continue    # just re-check self.running
            except OSError:
                break       # socket closed

            if not data:
                continue

            pkt_type = data[0]
            payload  = data[1:]

            if pkt_type == PKT_AUDIO:
                # First byte after type = username length
                if len(payload) < 2:
                    continue
                name_len  = payload[0]
                sender    = payload[1:1 + name_len].decode("utf-8", errors="replace")
                pcm_data  = payload[1 + name_len:]
                self.on_audio(sender, pcm_data)

            elif pkt_type == PKT_CHAT:
                try:
                    body = json.loads(payload.decode("utf-8"))
                    self.on_chat(body["sender"], body["msg"], body.get("time", ""))
                except (json.JSONDecodeError, KeyError):
                    pass

            elif pkt_type == PKT_CLIENT_LIST:
                try:
                    names = json.loads(payload.decode("utf-8"))
                    self.on_client_list(names)
                except json.JSONDecodeError:
                    pass

            elif pkt_type == PKT_TRANSMIT:
                username = payload.decode("utf-8", errors="replace")
                self.on_transmit(username)

    def _heartbeat_loop(self):
        """Send a heartbeat packet every 2 seconds to stay registered."""
        while self.running:
            time.sleep(2)
            if self.sock and self.running:
                try:
                    self.sock.sendto(bytes([PKT_HEARTBEAT]), self.server_addr)
                except OSError:
                    break


# =============================================================================
#  AudioManager  –  microphone capture + speaker playback via PyAudio
# =============================================================================

class AudioManager:
    """
    Encapsulates all PyAudio interactions.
    Capture and playback run on separate threads to avoid blocking each other.
    """

    def __init__(self, on_capture: callable):
        """
        Parameters
        ──────────
        on_capture : called with raw PCM bytes each time a chunk is captured
                     while PTT is active.
        """
        if not AUDIO_AVAILABLE:
            return

        self.on_capture     = on_capture
        self.ptt_active     = False          # True while push-to-talk held
        self.playback_queue = queue.Queue()  # PCM chunks waiting to play
        self.running        = False

        self._pa         = None
        self._in_stream  = None
        self._out_stream = None

    def start(self):
        """Open PyAudio streams and launch background threads."""
        if not AUDIO_AVAILABLE:
            return

        self._pa = pyaudio.PyAudio()

        # Input stream (microphone)
        self._in_stream = self._pa.open(
            format=AUDIO_FORMAT,
            channels=AUDIO_CHANNELS,
            rate=AUDIO_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )

        # Output stream (speakers)
        self._out_stream = self._pa.open(
            format=AUDIO_FORMAT,
            channels=AUDIO_CHANNELS,
            rate=AUDIO_RATE,
            output=True,
            frames_per_buffer=CHUNK_SIZE,
        )

        self.running = True

        threading.Thread(target=self._capture_loop,   daemon=True).start()
        threading.Thread(target=self._playback_loop,  daemon=True).start()
        log.info("Audio streams open.")

    def stop(self):
        """Stop all audio I/O."""
        if not AUDIO_AVAILABLE:
            return
        self.running = False
        time.sleep(0.2)
        if self._in_stream:
            self._in_stream.stop_stream()
            self._in_stream.close()
        if self._out_stream:
            self._out_stream.stop_stream()
            self._out_stream.close()
        if self._pa:
            self._pa.terminate()
        log.info("Audio streams closed.")

    def push_to_talk(self, active: bool):
        """Called by the GUI when the PTT button/key is pressed or released."""
        self.ptt_active = active

    def enqueue_audio(self, pcm_data: bytes):
        """Called by NetworkClient when incoming audio arrives."""
        if AUDIO_AVAILABLE:
            self.playback_queue.put(pcm_data)

    def _capture_loop(self):
        """
        Continuously read from the microphone.
        Only forwards data to the network when PTT is active.
        Silence (no PTT) is simply discarded to save bandwidth.
        """
        while self.running:
            try:
                chunk = self._in_stream.read(CHUNK_SIZE, exception_on_overflow=False)
            except OSError:
                break
            if self.ptt_active:
                self.on_capture(chunk)

    def _playback_loop(self):
        """
        Drain the playback queue and write chunks to the output stream.
        Uses a short timeout so the loop can exit cleanly when stopped.
        """
        while self.running:
            try:
                chunk = self.playback_queue.get(timeout=0.5)
                self._out_stream.write(chunk)
            except queue.Empty:
                continue
            except OSError:
                break


# =============================================================================
#  WalkieTalkieApp  –  Tkinter GUI
# =============================================================================

class WalkieTalkieApp(tk.Tk):
    """
    Main application window.

    Layout (left → right):
      ┌─────────────────────────────────────────────────────┐
      │  Header bar - logo + username + connection status   │
      ├──────────────────────┬──────────────────────────────┤
      │  Users panel         │  Chat / log panel            │
      │  (online list)       │  (scrolled messages)         │
      ├──────────────────────┴──────────────────────────────┤
      │  PTT button  (center, large)  +  chat input row     │
      └─────────────────────────────────────────────────────┘
    """

    def __init__(self):
        super().__init__()

        self.title("WalkiePy  •  Push-to-Talk")
        self.geometry("820x600")
        self.minsize(700, 520)
        self.configure(bg=PALETTE["bg_dark"])
        self.resizable(True, True)

        # Application state
        self.username        = ""
        self.connected       = False
        self.ptt_held        = False
        self.network_client  = None
        self.audio_manager   = None

        # Build the GUI
        self._build_gui()
        self._bind_keys()

        # Ask for connection details on startup
        self.after(200, self._show_connect_dialog)

    # ──────────────────────────────────────────────────────────────────────
    #  GUI Construction
    # ──────────────────────────────────────────────────────────────────────

    def _build_gui(self):
        """Assemble all widgets."""
        self._build_header()
        self._build_main_area()
        self._build_ptt_area()

    def _build_header(self):
        """Top bar: app name, username badge, connection status dot."""
        header = tk.Frame(self, bg=PALETTE["bg_panel"], height=52)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        # App logo / title
        tk.Label(
            header,
            text="◈  WALKIEPY",
            font=("Courier New", 15, "bold"),
            fg=PALETTE["accent"],
            bg=PALETTE["bg_panel"],
        ).pack(side="left", padx=20, pady=12)

        # Connection status badge (right side)
        self._status_frame = tk.Frame(header, bg=PALETTE["bg_panel"])
        self._status_frame.pack(side="right", padx=16)

        self._status_dot = tk.Canvas(
            self._status_frame, width=12, height=12,
            bg=PALETTE["bg_panel"], highlightthickness=0,
        )
        self._status_dot.pack(side="left", pady=2)
        self._status_circle = self._status_dot.create_oval(
            2, 2, 10, 10, fill=PALETTE["text_dim"], outline=""
        )

        self._status_label = tk.Label(
            self._status_frame,
            text="OFFLINE",
            font=("Courier New", 10, "bold"),
            fg=PALETTE["text_dim"],
            bg=PALETTE["bg_panel"],
        )
        self._status_label.pack(side="left", padx=(5, 0))

        # Username label
        self._username_label = tk.Label(
            header,
            text="—",
            font=("Courier New", 10),
            fg=PALETTE["text_dim"],
            bg=PALETTE["bg_panel"],
        )
        self._username_label.pack(side="right", padx=4)

        tk.Label(
            header,
            text="callsign:",
            font=("Courier New", 9),
            fg=PALETTE["text_dim"],
            bg=PALETTE["bg_panel"],
        ).pack(side="right")

    def _build_main_area(self):
        """Two-column area: user list on left, chat log on right."""
        main = tk.Frame(self, bg=PALETTE["bg_dark"])
        main.pack(fill="both", expand=True, padx=0, pady=0)

        # ── Left panel: online users ──────────────────────────────────────
        left = tk.Frame(main, bg=PALETTE["bg_panel"], width=180)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        tk.Label(
            left,
            text="CHANNEL",
            font=("Courier New", 9, "bold"),
            fg=PALETTE["text_dim"],
            bg=PALETTE["bg_panel"],
            anchor="w",
        ).pack(fill="x", padx=12, pady=(14, 2))

        tk.Frame(left, bg=PALETTE["border"], height=1).pack(fill="x", padx=12)

        self._users_listbox = tk.Listbox(
            left,
            bg=PALETTE["bg_panel"],
            fg=PALETTE["text_primary"],
            selectbackground=PALETTE["accent_dim"],
            selectforeground=PALETTE["accent"],
            font=("Courier New", 11),
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            activestyle="none",
        )
        self._users_listbox.pack(fill="both", expand=True, padx=8, pady=8)

        # Transmitter indicator (shown below user list when PTT is active)
        self._tx_indicator = tk.Label(
            left,
            text="",
            font=("Courier New", 9),
            fg=PALETTE["accent_tx"],
            bg=PALETTE["bg_panel"],
            wraplength=160,
            justify="center",
        )
        self._tx_indicator.pack(pady=(0, 10))

        # ── Right panel: chat log ─────────────────────────────────────────
        right = tk.Frame(main, bg=PALETTE["bg_dark"])
        right.pack(side="left", fill="both", expand=True)

        tk.Label(
            right,
            text="COMMS LOG",
            font=("Courier New", 9, "bold"),
            fg=PALETTE["text_dim"],
            bg=PALETTE["bg_dark"],
            anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 2))

        tk.Frame(right, bg=PALETTE["border"], height=1).pack(fill="x", padx=14)

        # ScrolledText for chat
        self._chat_box = tk.Text(
            right,
            bg=PALETTE["bg_dark"],
            fg=PALETTE["text_primary"],
            font=("Courier New", 11),
            wrap="word",
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            state="disabled",
            cursor="arrow",
            padx=10,
            pady=8,
            spacing1=3,
            spacing2=2,
        )
        self._chat_box.pack(fill="both", expand=True, padx=4)

        # Scrollbar for chat
        chat_scroll = tk.Scrollbar(right, command=self._chat_box.yview)
        self._chat_box.configure(yscrollcommand=chat_scroll.set)

        # Configure text tags for colored messages
        self._chat_box.tag_configure("system",    foreground=PALETTE["chat_system"], font=("Courier New", 10, "italic"))
        self._chat_box.tag_configure("sender",    foreground=PALETTE["chat_sender"], font=("Courier New", 11, "bold"))
        self._chat_box.tag_configure("other",     foreground=PALETTE["chat_other"],  font=("Courier New", 11, "bold"))
        self._chat_box.tag_configure("timestamp", foreground=PALETTE["text_dim"],    font=("Courier New", 9))
        self._chat_box.tag_configure("body",      foreground=PALETTE["text_primary"])

    def _build_ptt_area(self):
        """Bottom area: large PTT button + chat input field."""
        bottom = tk.Frame(self, bg=PALETTE["bg_panel"], height=100)
        bottom.pack(fill="x", side="bottom")
        bottom.pack_propagate(False)

        tk.Frame(bottom, bg=PALETTE["border"], height=1).pack(fill="x")

        content = tk.Frame(bottom, bg=PALETTE["bg_panel"])
        content.pack(fill="both", expand=True, padx=16, pady=10)

        # ── PTT Button ────────────────────────────────────────────────────
        self._ptt_btn = tk.Button(
            content,
            text="◉  TRANSMIT  [ SPACE ]",
            font=("Courier New", 12, "bold"),
            fg=PALETTE["bg_dark"],
            bg=PALETTE["accent_dim"],
            activeforeground=PALETTE["bg_dark"],
            activebackground=PALETTE["accent_tx"],
            relief="flat",
            bd=0,
            padx=24,
            pady=8,
            cursor="hand2",
        )
        self._ptt_btn.pack(side="left")
        self._ptt_btn.bind("<ButtonPress-1>",   lambda e: self._ptt_press())
        self._ptt_btn.bind("<ButtonRelease-1>", lambda e: self._ptt_release())

        # ── Chat input ────────────────────────────────────────────────────
        chat_frame = tk.Frame(content, bg=PALETTE["bg_panel"])
        chat_frame.pack(side="left", fill="x", expand=True, padx=(16, 0))

        self._chat_entry = tk.Entry(
            chat_frame,
            font=("Courier New", 11),
            bg=PALETTE["bg_input"],
            fg=PALETTE["text_primary"],
            insertbackground=PALETTE["accent"],
            relief="flat",
            bd=6,
        )
        self._chat_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._chat_entry.bind("<Return>", lambda e: self._send_chat())

        tk.Button(
            chat_frame,
            text="SEND",
            font=("Courier New", 10, "bold"),
            fg=PALETTE["bg_dark"],
            bg=PALETTE["accent"],
            activebackground=PALETTE["accent_dim"],
            relief="flat",
            bd=0,
            padx=14,
            pady=6,
            cursor="hand2",
            command=self._send_chat,
        ).pack(side="left", padx=(6, 0))

    # ──────────────────────────────────────────────────────────────────────
    #  Connection Dialog
    # ──────────────────────────────────────────────────────────────────────

    def _show_connect_dialog(self):
        """Modal dialog to collect server host, port, and username."""
        dialog = tk.Toplevel(self)
        dialog.title("Connect to Server")
        dialog.configure(bg=PALETTE["bg_dark"])
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        # Center the dialog
        dialog.geometry("360x280+%d+%d" % (
            self.winfo_x() + 230, self.winfo_y() + 160
        ))

        tk.Label(
            dialog,
            text="⬡  WALKIEPY",
            font=("Courier New", 16, "bold"),
            fg=PALETTE["accent"],
            bg=PALETTE["bg_dark"],
        ).pack(pady=(20, 4))

        tk.Label(
            dialog,
            text="Push-to-Talk Voice Chat",
            font=("Courier New", 9),
            fg=PALETTE["text_dim"],
            bg=PALETTE["bg_dark"],
        ).pack(pady=(0, 16))

        fields_frame = tk.Frame(dialog, bg=PALETTE["bg_dark"])
        fields_frame.pack(padx=30, fill="x")

        def labeled_entry(label_text, default=""):
            row = tk.Frame(fields_frame, bg=PALETTE["bg_dark"])
            row.pack(fill="x", pady=4)
            tk.Label(
                row, text=label_text,
                font=("Courier New", 9), fg=PALETTE["text_dim"],
                bg=PALETTE["bg_dark"], width=10, anchor="w",
            ).pack(side="left")
            entry = tk.Entry(
                row,
                font=("Courier New", 11),
                bg=PALETTE["bg_input"],
                fg=PALETTE["text_primary"],
                insertbackground=PALETTE["accent"],
                relief="flat", bd=4,
            )
            entry.pack(side="left", fill="x", expand=True, ipady=4)
            entry.insert(0, default)
            return entry

        host_entry = labeled_entry("Server IP:", "127.0.0.1")
        port_entry = labeled_entry("Port:",      "9000")
        name_entry = labeled_entry("Callsign:",  "")
        name_entry.focus_set()

        def do_connect():
            host = host_entry.get().strip()
            port_str = port_entry.get().strip()
            name = name_entry.get().strip()

            if not host or not name:
                messagebox.showerror("Missing info", "Please fill all fields.", parent=dialog)
                return
            try:
                port = int(port_str)
                assert 1 <= port <= 65535
            except (ValueError, AssertionError):
                messagebox.showerror("Invalid port", "Port must be 1–65535.", parent=dialog)
                return

            self.username = name
            dialog.destroy()
            self._connect(host, port)

        tk.Button(
            dialog,
            text="CONNECT",
            font=("Courier New", 11, "bold"),
            fg=PALETTE["bg_dark"],
            bg=PALETTE["accent"],
            activebackground=PALETTE["accent_dim"],
            relief="flat", bd=0,
            padx=20, pady=8,
            cursor="hand2",
            command=do_connect,
        ).pack(pady=16)

        dialog.bind("<Return>", lambda e: do_connect())

    # ──────────────────────────────────────────────────────────────────────
    #  Connection Logic
    # ──────────────────────────────────────────────────────────────────────

    def _connect(self, host: str, port: int):
        """Initialise network + audio, then connect to the server."""
        # Update header
        self._username_label.config(text=self.username, fg=PALETTE["text_primary"])

        # Wire up the audio manager
        if AUDIO_AVAILABLE:
            self.audio_manager = AudioManager(on_capture=self._on_audio_captured)
            self.audio_manager.start()
        else:
            self._append_chat("System", "⚠ PyAudio not installed — audio disabled.", "")

        # Wire up the network client
        self.network_client = NetworkClient(
            server_host    = host,
            server_port    = port,
            username       = self.username,
            on_audio       = self._on_audio_received,
            on_chat        = self._on_chat_received,
            on_client_list = self._on_client_list,
            on_transmit    = self._on_transmit,
            on_error       = self._on_error,
        )
        self.network_client.connect()
        self.connected = True

        # Update status indicator
        self._status_dot.itemconfig(self._status_circle, fill=PALETTE["accent"])
        self._status_label.config(text="ONLINE", fg=PALETTE["accent"])

        self._append_chat("System", f"Connected to {host}:{port} as '{self.username}'.", "")
        if not AUDIO_AVAILABLE:
            self._append_chat("System",
                "Install PyAudio for voice: pip install pyaudio", "")

    def disconnect(self):
        """Clean up audio and network on close."""
        if self.audio_manager:
            self.audio_manager.stop()
        if self.network_client:
            self.network_client.disconnect()

    # ──────────────────────────────────────────────────────────────────────
    #  PTT (Push-to-Talk)
    # ──────────────────────────────────────────────────────────────────────

    def _ptt_press(self):
        """Activate transmission: light up button, tell audio manager."""
        if not self.connected or self.ptt_held:
            return
        self.ptt_held = True
        self._ptt_btn.config(
            bg=PALETTE["accent_tx"],
            text="● TRANSMITTING…",
        )
        if self.audio_manager:
            self.audio_manager.push_to_talk(True)
        log.debug("PTT ON")

    def _ptt_release(self):
        """Release PTT."""
        if not self.ptt_held:
            return
        self.ptt_held = False
        self._ptt_btn.config(
            bg=PALETTE["accent_dim"],
            text="◉  TRANSMIT  [ SPACE ]",
        )
        if self.audio_manager:
            self.audio_manager.push_to_talk(False)
        log.debug("PTT OFF")

    def _bind_keys(self):
        """Map SPACE to PTT and Escape to release."""
        self.bind("<KeyPress-space>",   lambda e: self._ptt_press()   if not isinstance(self.focus_get(), tk.Entry) else None)
        self.bind("<KeyRelease-space>", lambda e: self._ptt_release() if not isinstance(self.focus_get(), tk.Entry) else None)

    # ──────────────────────────────────────────────────────────────────────
    #  Chat
    # ──────────────────────────────────────────────────────────────────────

    def _send_chat(self):
        """Send the text in the chat input box."""
        if not self.connected:
            return
        text = self._chat_entry.get().strip()
        if not text:
            return
        self._chat_entry.delete(0, tk.END)
        self.network_client.send_chat(text)

    def _append_chat(self, sender: str, message: str, timestamp: str):
        """
        Append a message to the chat log (thread-safe via after()).
        Uses Tkinter text tags to colour-code system vs user messages.
        """
        def _insert():
            self._chat_box.config(state="normal")

            if sender == "System":
                self._chat_box.insert("end", f"  ⬡ {message}\n", "system")
            else:
                ts_str  = f"[{timestamp}] " if timestamp else ""
                name_tag = "sender" if sender == self.username else "other"
                self._chat_box.insert("end", f"  {ts_str}", "timestamp")
                self._chat_box.insert("end", f"{sender}: ", name_tag)
                self._chat_box.insert("end", f"{message}\n", "body")

            self._chat_box.config(state="disabled")
            self._chat_box.see("end")  # auto-scroll to latest

        self.after(0, _insert)

    # ──────────────────────────────────────────────────────────────────────
    #  Callbacks from NetworkClient
    # ──────────────────────────────────────────────────────────────────────

    def _on_audio_received(self, sender: str, pcm_data: bytes):
        """Route incoming PCM to the audio manager's playback queue."""
        if self.audio_manager:
            self.audio_manager.enqueue_audio(pcm_data)

    def _on_audio_captured(self, pcm_data: bytes):
        """Called by AudioManager when mic data is ready; forward to network."""
        if self.network_client:
            self.network_client.send_audio(pcm_data)

    def _on_chat_received(self, sender: str, message: str, timestamp: str):
        self._append_chat(sender, message, timestamp)

    def _on_client_list(self, names: list):
        """Refresh the user list widget (must happen on main thread)."""
        def _update():
            self._users_listbox.delete(0, tk.END)
            for name in names:
                prefix = "▶ " if name == self.username else "  "
                self._users_listbox.insert(tk.END, f"{prefix}{name}")
        self.after(0, _update)

    def _on_transmit(self, username: str):
        """Show/hide the 'X is transmitting' indicator."""
        def _update():
            if username == self.username:
                self._tx_indicator.config(text="")
            else:
                self._tx_indicator.config(text=f"📻  {username}\ntransmitting…")
        self.after(0, _update)

    def _on_error(self, message: str):
        self.after(0, lambda: messagebox.showerror("Connection Error", message))

    # ──────────────────────────────────────────────────────────────────────
    #  Window close
    # ──────────────────────────────────────────────────────────────────────

    def on_close(self):
        """Called when the user closes the window."""
        self.disconnect()
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = WalkieTalkieApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()