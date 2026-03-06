# PyMesh Chat

> Secure, serverless peer-to-peer terminal chat and file sharing — no central server, no middleman, no plaintext.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Phase](https://img.shields.io/badge/phase-1%20of%206-orange)
![Tests](https://img.shields.io/badge/tests-14%2F14%20passing-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What is PyMesh Chat?

PyMesh Chat is a fully decentralised terminal application for secure group and private messaging and file sharing over a local area network. Every node on the network acts simultaneously as a client and a server — there is no central relay, no account registration, and no data leaving your network.

- **No server** — peers connect directly to each other
- **Encrypted** — end-to-end encryption via Ed25519 / AES-256-GCM (Phase 2)
- **Auto-discovery** — peers on the same LAN find each other automatically via mDNS
- **Group and private** — broadcast to everyone or send privately to one peer
- **File sharing** — send any file type to one peer or the whole group (Phase 4)
- **Cross-platform** — macOS and Windows standalone binaries (Phase 6)

---

## Project Status

| Phase | Description | Status |
|-------|-------------|--------|
| **Phase 1** | Core networking — TCP, handshake, session model, peer discovery | ✅ Complete |
| **Phase 2** | End-to-end encryption — Ed25519 keypairs, X25519, AES-256-GCM | 🔜 Next |
| **Phase 3** | Full messaging — group broadcast, private 1-to-1, recipient prompts | ⏳ Planned |
| **Phase 4** | File transfer — encrypted, chunked, broadcast + private | ⏳ Planned |
| **Phase 5** | Rich terminal UI — Textual layout, peer sidebar, command completion | ⏳ Planned |
| **Phase 6** | Packaging — PyInstaller binaries for macOS and Windows | ⏳ Planned |

---

## Requirements

- Python 3.11 or newer
- No third-party packages required to run Phase 1

Install optional dependencies for the best experience:

```bash
pip install -r requirements.txt
```

| Package | Purpose | Required? |
|---------|---------|-----------|
| `zeroconf` | LAN auto-discovery (peers find each other automatically) | Optional |
| `cryptography` | End-to-end encryption (Phase 2) | Optional now, required Phase 2+ |
| `textual` | Rich terminal UI (Phase 5) | Optional now, required Phase 5+ |
| `rich` | Text formatting and progress bars | Optional now, required Phase 5+ |

---

## Quick Start

**Clone the repo:**
```bash
git clone https://github.com/yourusername/pymesh-chat.git
cd pymesh-chat
```

**Run the tests first:**
```bash
python3 pymesh/run_tests.py
# Expected: 14/14 passed
```

**Start a session:**

Terminal 1 — Alice starts first:
```bash
python3 pymesh_start.py --alias alice --session dev-team
```

The app will print Alice's IP address and the exact command Bob needs:
```
  ┌─ Share this with peers ──────────────────────────────────┐
  │  IP Address  : 192.168.1.42                              │
  │  Port        : 55400                                     │
  │  Command     : --connect 192.168.1.42                    │
  └─────────────────────────────────────────────────────────┘
```

Terminal 2 — Bob connects to Alice:
```bash
python3 pymesh_start.py --alias bob --session dev-team --connect 192.168.1.42
```

If `zeroconf` is installed, peers on the same session find each other automatically — no `--connect` flag needed.

---

## Commands

Once inside the app:

| Command | Description |
|---------|-------------|
| Type anything + Enter | Broadcast message to all peers |
| `/say <message>` | Broadcast message to all peers |
| `/msg @alias <message>` | Send a private message to one peer |
| `/peers` | List all connected peers |
| `/connect <ip>` | Manually connect to a peer by IP address |
| `/connect <ip:port>` | Connect with a custom port |
| `/whoami` | Show your IP, port, alias, and fingerprint |
| `/help` | Show all commands |
| `/quit` | Disconnect and exit |

---

## CLI Options

```
python3 pymesh_start.py --help

options:
  --alias ALIAS        Your display name shown to other peers (required)
  --session SESSION    Session name to join (default: 'default')
  --port PORT          TCP listener port (default: 55400)
  --connect HOST:PORT  Connect to a peer on startup
  --timeout SECONDS    Inactivity timeout before auto-disconnect (default: 900)
  --verbose            Enable debug logging to stderr
```

---

## Project Structure

```
pymesh-chat/
├── pymesh_start.py        ← Entry point — run this
├── requirements.txt
├── README.md
├── LICENSE
└── pymesh/
    ├── main.py            ← Argument parsing and app bootstrap
    ├── pyproject.toml     ← Package config for pip install / PyInstaller
    ├── run_tests.py       ← Test suite (no pytest needed)
    ├── core/
    │   ├── node.py        ← Central orchestrator — owns all peer connections
    │   ├── peer.py        ← Per-connection state machine (reader/writer/watchdog)
    │   ├── listener.py    ← TCP server — accepts inbound connections
    │   ├── connector.py   ← Outbound TCP connections
    │   ├── handshake.py   ← Identity exchange before session begins
    │   ├── protocol.py    ← Wire framing (4-byte length prefix + JSON payload)
    │   └── discovery.py   ← mDNS/Zeroconf LAN auto-discovery
    ├── crypto/            ← Phase 2: encryption and key management
    ├── ui/
    │   └── terminal.py    ← Phase 1 terminal UI (Phase 5 upgrades to Textual)
    ├── tests/
    │   └── test_phase1.py ← Full test suite
    └── utils/
        └── constants.py   ← All configuration constants and protocol definitions
```

---

## Architecture

PyMesh Chat uses a **full-mesh P2P topology** — every peer connects directly to every other peer in the session. There is no relay node.

```
Alice ──── Bob
  \       /
   \     /
    Carol
```

Peer discovery on the LAN uses **mDNS/Zeroconf** (the same technology as Apple Bonjour / AirDrop). Each node broadcasts a `_pymesh._tcp.local.` service announcement. Any peer on the same network listening for the same session name auto-connects.

All messages use a **length-prefixed wire protocol**:
```
[4-byte big-endian uint32 = payload length] [JSON payload bytes]
```

Connections stay open indefinitely until the inactivity timeout elapses (default 15 minutes), the peer sends `/quit`, or the TCP connection drops. Periodic PING/PONG heartbeats keep idle connections alive.

---

## Contributing

This project is under active development. Contributions, issues, and feedback are welcome.

---

## License

MIT — see [LICENSE](LICENSE)
