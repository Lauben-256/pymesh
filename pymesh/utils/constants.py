"""
PyMesh Chat — Constants
Central location for all configuration defaults and protocol constants.
"""

# ── Application ───────────────────────────────────────────────────────────────
APP_NAME             = "PyMesh Chat"
APP_VERSION          = "0.3.0"
APP_PROTOCOL_VERSION = 1

# ── Network ───────────────────────────────────────────────────────────────────
DEFAULT_PORT       = 55400
MDNS_SERVICE_TYPE  = "_pymesh._tcp.local."
MDNS_SERVICE_NAME  = "pymesh"
BUFFER_SIZE        = 65536
MAX_MESSAGE_SIZE   = 10_737_418_240   # 10 GB [10_485_760    10 MB]
FILE_CHUNK_SIZE    = 65536        # 64 KB
CONNECTION_TIMEOUT = 10
HANDSHAKE_TIMEOUT  = 15

# ── Session ───────────────────────────────────────────────────────────────────
DEFAULT_INACTIVITY_TIMEOUT = 900   # 15 minutes
INACTIVITY_WARN_BEFORE     = 60
SESSION_NAME_MAX_LEN       = 32
ALIAS_MAX_LEN              = 24
MAX_HISTORY                = 500   # Max messages kept in memory per session

# ── Typing indicator ──────────────────────────────────────────────────────────
TYPING_DEBOUNCE    = 2.0   # Seconds of silence before sending TYPING_STOPPED
TYPING_TIMEOUT     = 5.0   # Seconds before we assume peer stopped typing

# ── Paths ─────────────────────────────────────────────────────────────────────
import os
PYMESH_DIR          = os.path.expanduser("~/.pymesh")
IDENTITY_FILE       = os.path.join(PYMESH_DIR, "identity.key")
KNOWN_PEERS_FILE    = os.path.join(PYMESH_DIR, "known_peers.json")
CONFIG_FILE         = os.path.join(PYMESH_DIR, "config.toml")
DEFAULT_DOWNLOAD_DIR = os.path.expanduser("~/pymesh-downloads")

# ── Wire Protocol — Message Types ─────────────────────────────────────────────
MSG_HANDSHAKE_HELLO  = "HELLO"
MSG_HANDSHAKE_ACK    = "HELLO_ACK"

# Messaging
MSG_CHAT             = "CHAT"         # Group or private chat message
MSG_ACK              = "MSG_ACK"      # Delivery acknowledgement
MSG_TYPING_START     = "TYPING_START" # Peer started typing
MSG_TYPING_STOP      = "TYPING_STOP"  # Peer stopped typing

# File transfer (Phase 4)
MSG_FILE_OFFER       = "FILE_OFFER"
MSG_FILE_ACCEPT      = "FILE_ACCEPT"
MSG_FILE_REJECT      = "FILE_REJECT"
MSG_FILE_CHUNK       = "FILE_CHUNK"
MSG_FILE_DONE        = "FILE_DONE"
MSG_FILE_ACK         = "FILE_ACK"
MSG_FILE_ERROR       = "FILE_ERROR"

# Infrastructure
MSG_PING             = "PING"
MSG_PONG             = "PONG"
MSG_PEER_LIST        = "PEER_LIST"
MSG_DISCONNECT       = "DISCONNECT"

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FORMAT      = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"
