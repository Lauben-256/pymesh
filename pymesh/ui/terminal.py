# -*- coding: utf-8 -*-
"""
PyMesh Chat -- ANSI Split-Pane TUI  (Windows-native, macOS/Linux --simple)

Replicates the curses TUI layout using ANSI escape sequences:

  ┌──────────────────────────────────────────┬────────────────────┐
  │  PyMesh  dev-team            Tracy 14:32 │ PEERS           2  │
  ├──────────────────────────────────────────┤ ──────────────────  │
  │                                          │ * Tracy (you)       │
  │  14:30  GROUP  bob    hello everyone     │ * bob               │
  │  14:31  GROUP  Tracy  hey bob!     [ok]  │                     │
  │  14:31  PRIV   bob->  secret msg         │ FILES               │
  │  >> bob is typing...                     │ v photo.jpg  75%    │
  │                                          │ [######----]        │
  ├──────────────────────────────────────────┴────────────────────┤
  │ Tracy>  hello world_                     2 peers  /help        │
  └────────────────────────────────────────────────────────────────┘

No curses. No external dependencies.
Works on: Windows 10+ cmd/PowerShell/Terminal, macOS, Linux.

Key input: msvcrt on Windows, termios on Unix — character-by-character,
no Enter required (just like curses).  Backspace, Enter, printable chars.
"""

import asyncio
import ctypes
import ctypes.wintypes
import logging
import os
import platform
import shutil
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pymesh.core.node import Node
from pymesh.core.peer import PeerInfo
from pymesh.messaging.typing import TypingTracker
from pymesh.files.transfer import OutboundTransfer, InboundTransfer, _fmt_size
from pymesh.utils.constants import DEFAULT_PORT, MSG_CHAT, APP_VERSION

log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


# ==============================================================================
# ANSI + Windows console setup
# ==============================================================================

def _enable_windows_ansi() -> bool:
    if not IS_WINDOWS:
        return True
    try:
        kernel32 = ctypes.windll.kernel32            # type: ignore[attr-defined]
        handle   = kernel32.GetStdHandle(-11)        # STD_OUTPUT_HANDLE
        mode     = ctypes.wintypes.DWORD()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VT
    except Exception:
        return False

_ANSI_OK  = _enable_windows_ansi()


def _get_window_size() -> tuple:
    """
    Return (cols, rows) of the VISIBLE terminal window — not the scroll buffer.

    Windows CMD stores a huge scroll buffer (9001 rows by default).
    shutil.get_terminal_size() returns that buffer height, not the visible
    window height — putting the input bar at row 8999, completely off screen.

    We use two methods in order:
      1. Win32 GetConsoleScreenBufferInfo().srWindow — the visible rectangle
      2. ANSI DSR/CPR escape sequence — ask the terminal for its size
      3. shutil fallback, capped at 200 rows (no real terminal window is taller)
    """
    # ── Method 1: Win32 API (Windows only) ────────────────────────────────────
    if IS_WINDOWS:
        try:
            import ctypes

            class _COORD(ctypes.Structure):
                _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

            class _SMALL_RECT(ctypes.Structure):
                _fields_ = [
                    ("Left", ctypes.c_short), ("Top",    ctypes.c_short),
                    ("Right",ctypes.c_short), ("Bottom", ctypes.c_short),
                ]

            class _CSBI(ctypes.Structure):
                _fields_ = [
                    ("dwSize",              _COORD),
                    ("dwCursorPosition",    _COORD),
                    ("wAttributes",         ctypes.c_ushort),
                    ("srWindow",            _SMALL_RECT),
                    ("dwMaximumWindowSize", _COORD),
                ]

            kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
            handle   = kernel32.GetStdHandle(-11)      # STD_OUTPUT_HANDLE
            info     = _CSBI()
            if kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
                w = info.srWindow.Right  - info.srWindow.Left + 1
                h = info.srWindow.Bottom - info.srWindow.Top  + 1
                if 10 <= w <= 500 and 5 <= h <= 300:
                    return (w, h)
        except Exception:
            pass

    # ── Method 2: shutil with a hard cap ──────────────────────────────────────
    # No real terminal window exceeds 300 rows.  If we get more, it's the
    # scroll buffer leaking through — cap it so the layout stays on screen.
    s = shutil.get_terminal_size((80, 24))
    cols = max(20, min(s.columns, 500))
    rows = max(10, min(s.lines, 300))
    return (cols, rows)


USE_COLOR = bool(
    _ANSI_OK
    and hasattr(sys.stdout, "isatty")
    and sys.stdout.isatty()
    and os.environ.get("TERM") != "dumb"
    and not os.environ.get("NO_COLOR")
)

# ANSI escape helpers
ESC = "\033["
RESET        = "\033[0m"
BOLD         = "\033[1m"
DIM          = "\033[2m"
CYAN         = "\033[96m"
GREEN        = "\033[92m"
YELLOW       = "\033[93m"
RED          = "\033[91m"
MAGENTA      = "\033[95m"
BLUE         = "\033[94m"
WHITE        = "\033[97m"
BG_CYAN      = "\033[46m"
BG_BLACK     = "\033[40m"
BG_DARK      = "\033[100m"   # bright black bg
HIDE_CURSOR  = "\033[?25l"
SHOW_CURSOR  = "\033[?25h"
CLEAR_SCREEN = "\033[2J\033[H"
SAVE_POS     = "\033[s"
RESTORE_POS  = "\033[u"

def _mv(row: int, col: int) -> str:
    """ANSI cursor move to 1-based row,col."""
    return f"\033[{row};{col}H"

def _clr() -> str:
    """Clear to end of line."""
    return "\033[K"

def c(text: str, *codes: str) -> str:
    if not USE_COLOR:
        return text
    return "".join(codes) + text + RESET

def _strip_ansi(s: str) -> str:
    """Remove ANSI codes to get printable width."""
    import re
    return re.sub(r'\033\[[0-9;]*[mHKJsulABCDfSTirnRhp]', '', s)

def _vis_len(s: str) -> int:
    return len(_strip_ansi(s))


# ==============================================================================
# Non-blocking keyboard input
# ==============================================================================

class _KeyReader:
    """
    Cross-platform non-blocking character reader.
    Windows: msvcrt.getwch()
    Unix:    termios raw mode + select
    """

    def __init__(self):
        self._old_settings = None
        if not IS_WINDOWS:
            self._setup_unix()

    def _setup_unix(self):
        try:
            import termios, tty
            fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(fd)
            tty.setraw(fd)
        except Exception:
            pass

    def restore(self):
        if not IS_WINDOWS and self._old_settings is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass

    def read_key(self) -> Optional[str]:
        """
        Return a single key press or None if no key is pending.
        Returns special strings for control keys:
          'ENTER'  'BACKSPACE'  'UP'  'DOWN'  'PGUP'  'PGDN'  'F1'  'ESC'
        """
        if IS_WINDOWS:
            return self._read_windows()
        else:
            return self._read_unix()

    def _read_windows(self) -> Optional[str]:
        try:
            import msvcrt
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ('\r', '\n'):
                return 'ENTER'
            if ch == '\x08':
                return 'BACKSPACE'
            if ch == '\x03':
                return 'CTRL_C'
            if ch == '\x00' or ch == '\xe0':   # extended key prefix
                ch2 = msvcrt.getwch()
                codes = {
                    'I': 'PGUP',   # 0x49
                    'Q': 'PGDN',   # 0x51
                    'H': 'UP',
                    'P': 'DOWN',
                    'K': 'LEFT',   # 0x4B
                    'M': 'RIGHT',  # 0x4D
                    'G': 'HOME',   # 0x47
                    'O': 'END',    # 0x4F
                    ';': 'F1',
                }
                return codes.get(ch2, None)
            if ' ' <= ch <= '~':
                return ch
            return None
        except Exception:
            return None

    def _read_unix(self) -> Optional[str]:
        try:
            import select
            r, _, _ = select.select([sys.stdin], [], [], 0)
            if not r:
                return None
            ch = sys.stdin.read(1)
            if ch == '\r' or ch == '\n':
                return 'ENTER'
            if ch in ('\x7f', '\x08'):
                return 'BACKSPACE'
            if ch == '\x03':
                return 'CTRL_C'
            if ch == '\x1b':   # escape sequence
                r2, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not r2:
                    return 'ESC'
                seq = sys.stdin.read(1)
                if seq != '[':
                    return 'ESC'
                seq2 = ''
                while True:
                    r3, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if not r3:
                        break
                    byte = sys.stdin.read(1)
                    seq2 += byte
                    if byte.isalpha() or byte == '~':
                        break
                mapping = {
                    'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                    '5~': 'PGUP', '6~': 'PGDN',
                    'H': 'HOME', 'F': 'END',
                    '[A': 'F1',   # some terminals F1
                }
                return mapping.get(seq2, None)
            if ' ' <= ch <= '~':
                return ch
            return None
        except Exception:
            return None


# ==============================================================================
# Data classes (mirrors tui.py)
# ==============================================================================

@dataclass
class DisplayMessage:
    ts:        str
    scope:     str       # "group" | "private" | "system" | "warn"
    sender:    str
    recipient: str
    text:      str
    is_own:    bool = False
    delivered: bool = False
    msg_id:    str  = ""


@dataclass
class PeerDisplay:
    alias:   str
    address: str
    port:    int
    fp:      str
    unread:  int = 0


@dataclass
class FileDisplay:
    transfer_id: str
    name:        str
    direction:   str   # "up" | "down"
    peer:        str
    pct:         int  = 0
    done:        bool = False
    failed:      bool = False


# ==============================================================================
# AnsiTUI — the split-pane class
# ==============================================================================

class AnsiTUI:
    """
    Split-pane terminal UI using ANSI escape sequences.
    Identical layout and feature set to the curses TUI.
    Used automatically on Windows; available as --simple on macOS/Linux.
    """

    SIDEBAR_W = 16    # narrow sidebar — more message space on small screens
    HEADER_H  = 2
    INPUT_H   = 3
    TICK_S    = 0.05   # 50ms render tick
    MIN_W     = 40     # minimum usable width  (fits 80x25 CMD and narrower)
    MIN_H     = 10     # minimum usable height (fits tiny CMD, always shows input)

    def __init__(self, node: Node, connect_on_start: Optional[Tuple[str, int]] = None):
        self._node             = node
        self._connect_on_start = connect_on_start

        # Display state
        self._messages: deque              = deque(maxlen=1000)
        self._peers:    List[PeerDisplay]  = []
        self._files:    Dict[str, FileDisplay] = {}
        self._typing_who: List[str]        = []
        self._input_buf:  List[str]        = []
        self._cursor_pos: int              = 0   # index into _input_buf
        self._scroll_offset: int           = 0
        self._msg_id_map: Dict[str, DisplayMessage] = {}
        self._pending_offers: Dict[str, FileDisplay] = {}

        # Modal overlay: None or {"type", "lines", "future"(optional)}
        self._modal: Optional[dict] = None

        # Event queue
        self._events: asyncio.Queue = asyncio.Queue()
        self._loop:   Optional[asyncio.AbstractEventLoop] = None

        # Keyboard
        self._keys = _KeyReader()

        # Typing tracker
        self._typing = TypingTracker(
            on_peer_started = self._typing_started,
            on_peer_stopped = self._typing_stopped,
            send_start      = node.send_typing_start,
            send_stop       = node.send_typing_stop,
        )

        self._running = False

    # ── Entry point ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wire_callbacks()
        self._wire_security_callbacks()

        if self._connect_on_start:
            host, port = self._connect_on_start
            asyncio.create_task(self._delayed_connect(host, port))

        try:
            await self._loop.run_in_executor(None, self._render_main)
        finally:
            self._keys.restore()
            # Show cursor and reset terminal
            sys.stdout.write(SHOW_CURSOR + "\033[0m\n")
            sys.stdout.flush()
        self._typing.stop()

    # ── Render main loop (runs in thread executor) ────────────────────────────

    def _render_main(self) -> None:
        sys.stdout.write(HIDE_CURSOR + CLEAR_SCREEN)
        sys.stdout.flush()

        self._running = True
        self._add_system("PyMesh Chat started. Type /help for commands.")

        while self._running:
            self._drain_events()
            self._handle_keys()
            self._redraw()
            time.sleep(self.TICK_S)

        # Restore terminal on exit
        sys.stdout.write(SHOW_CURSOR + CLEAR_SCREEN)
        sys.stdout.flush()

    # ── Event queue ───────────────────────────────────────────────────────────

    def _post(self, event: dict) -> None:
        try:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is self._loop:
                self._events.put_nowait(event)
            else:
                self._loop.call_soon_threadsafe(self._events.put_nowait, event)
        except Exception:
            pass

    def _drain_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
                self._handle_event(event)
            except Exception:
                break

    def _handle_event(self, ev: dict) -> None:
        t = ev.get("type")

        if t == "message":
            self._messages.append(ev["msg"])
            if ev["msg"].msg_id:
                self._msg_id_map[ev["msg"].msg_id] = ev["msg"]
            self._scroll_offset = 0

        elif t == "warn":
            self._add_warn(ev["text"])

        elif t == "peer_joined":
            info = ev["info"]
            self._peers.append(PeerDisplay(
                alias=info.alias, address=info.address,
                port=info.port, fp=info.fingerprint
            ))
            self._add_system(f"@{info.alias} joined ({info.address}:{info.port})")

        elif t == "peer_left":
            info = ev["info"]
            self._peers    = [p for p in self._peers if p.fp != info.fingerprint]
            self._typing_who = [a for a in self._typing_who if a != info.alias]
            self._add_system(f"@{info.alias} left")

        elif t == "delivered":
            msg_id = ev["msg_id"]
            if msg_id in self._msg_id_map:
                self._msg_id_map[msg_id].delivered = True

        elif t == "typing_start":
            alias = ev["alias"]
            if alias not in self._typing_who:
                self._typing_who.append(alias)

        elif t == "typing_stop":
            alias = ev["alias"]
            self._typing_who = [a for a in self._typing_who if a != alias]

        elif t == "file_offer":
            fd = FileDisplay(
                transfer_id=ev["transfer_id"], name=ev["name"],
                direction="down", peer=ev["sender"],
            )
            self._files[ev["transfer_id"]]          = fd
            self._pending_offers[ev["transfer_id"]] = fd
            self._add_system(
                f"@{ev['sender']} wants to send {ev['name']} "
                f"({_fmt_size(ev['size'])}) -- "
                f"/accept {ev['transfer_id'][:8]} or /reject {ev['transfer_id'][:8]}"
            )

        elif t == "file_progress":
            tid  = ev["transfer_id"]
            done = ev["done"]; total = ev["total"]
            if tid in self._files:
                self._files[tid].pct = int(done / total * 100) if total else 0

        elif t == "file_complete":
            tid  = ev["transfer_id"]; path = ev["path"]
            if tid in self._files:
                self._files[tid].done = True
                self._files[tid].pct  = 100
            self._pending_offers.pop(tid, None)
            self._add_system(f"Transfer complete: {os.path.basename(path)} -> {path}")

        elif t == "file_error":
            tid = ev["transfer_id"]
            if tid in self._files:
                self._files[tid].failed = True
            self._pending_offers.pop(tid, None)
            self._add_warn(f"Transfer failed: {ev['reason']}")

        elif t == "file_rejected":
            tid = ev["transfer_id"]
            if tid in self._files:
                self._files[tid].failed = True
            self._add_system(f"@{ev['peer']} declined the file transfer.")

        elif t == "file_outbound":
            fd = FileDisplay(
                transfer_id=ev["transfer_id"], name=ev["name"],
                direction="up", peer=ev["peer"],
            )
            self._files[ev["transfer_id"]] = fd

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        try:
            w, h = _get_window_size()

            # Hard sanity cap: if height is unreasonably large it is still
            # the scroll-buffer leak — clamp to something sensible.
            if h > 300:
                h = 40
            if w > 500:
                w = 200

            if w < self.MIN_W or h < self.MIN_H:
                sys.stdout.write(
                    CLEAR_SCREEN + _mv(1, 1) +
                    f"Window too small ({w}x{h}) — "
                    f"need {self.MIN_W}x{self.MIN_H}. Resize and it will appear."
                )
                sys.stdout.flush()
                return

            # Sidebar: scale with terminal width, minimum 10 cols
            sidebar_w = min(self.SIDEBAR_W, max(10, w // 5))
            chat_w    = w - sidebar_w - 1   # -1 for the divider column
            msg_h     = h - self.HEADER_H - self.INPUT_H
            msg_h     = max(1, msg_h)       # always at least 1 message row

            buf = []
            self._draw_header(buf, w, chat_w)
            self._draw_messages(buf, chat_w, msg_h)
            self._draw_divider(buf, chat_w, h)
            self._draw_sidebar(buf, chat_w + 2, sidebar_w, msg_h)
            self._draw_input(buf, h, w)
            if self._modal:
                self._draw_modal(buf, h, w)

            sys.stdout.write("".join(buf))
            sys.stdout.flush()
        except Exception:
            pass

    def _draw_header(self, buf: list, w: int, chat_w: int) -> None:
        # Row 1: title bar (full width, cyan bg)
        title = f" PyMesh  {self._node.session_name}"
        ts    = time.strftime("%H:%M")
        n_peers = len(self._peers)
        right = f"{n_peers} peer{'s' if n_peers!=1 else ''}  {ts} "
        if USE_COLOR:
            # Cyan background header
            row1 = (
                c(" " * w, BG_CYAN + WHITE)  # clear row
            )
            title_str = c(f" PyMesh ", BG_CYAN + WHITE + BOLD) + c(f" {self._node.session_name}", BG_CYAN + WHITE)
            right_str = c(right, BG_CYAN + WHITE)
            # Build row: title left, right-aligned info
            gap = w - _vis_len(c(f" PyMesh  {self._node.session_name}", BG_CYAN + WHITE)) - len(right) - 1
            gap = max(0, gap)
            row1_content = (
                c(f" PyMesh ", BG_CYAN + WHITE + BOLD) +
                c(f" {self._node.session_name}", BG_CYAN + WHITE) +
                c(" " * gap, BG_CYAN + WHITE) +
                c(f" {right}", BG_CYAN + WHITE)
            )
        else:
            row1_content = f" PyMesh  {self._node.session_name}" + " " * max(0, w - len(title) - len(right) - 2) + right

        buf.append(_mv(1, 1))
        buf.append(row1_content[:w + 30])  # +30 for ANSI codes overhead

        # Row 2: alias + fingerprint hint — padded to full width and cleared
        fp_hint = f" {self._node.alias}  fp:{self._node.fingerprint[:12]}..."
        fp_padded = fp_hint[:w].ljust(w)
        if USE_COLOR:
            buf.append(_mv(2, 1))
            buf.append(c(fp_padded, BG_DARK + WHITE))
        else:
            buf.append(_mv(2, 1))
            buf.append(fp_padded)
        buf.append("[0m[K")

    def _draw_messages(self, buf: list, chat_w: int, msg_h: int) -> None:
        lines = []
        for msg in self._messages:
            lines.extend(self._render_message(msg, chat_w - 1))

        # Typing indicator
        if self._typing_who:
            who    = ", ".join(self._typing_who)
            suffix = "is" if len(self._typing_who) == 1 else "are"
            lines.append(c(f"  >>  {who} {suffix} typing...", YELLOW + DIM))

        # Apply scroll
        total   = len(lines)
        visible_start = max(0, total - msg_h - self._scroll_offset)
        visible_start = min(visible_start, max(0, total - msg_h))
        visible = lines[visible_start: visible_start + msg_h]

        for row_idx in range(msg_h):
            buf.append(_mv(self.HEADER_H + 1 + row_idx, 1))
            if row_idx < len(visible):
                line = visible[row_idx]
                # Pad visible content to chat_w to erase any stale chars from prior frame
                vl  = _vis_len(line)
                pad = " " * max(0, chat_w - 1 - vl)
                buf.append(line + pad)
            else:
                buf.append(" " * (chat_w - 1))
            buf.append("[K")   # clear any remaining stale chars past chat_w

        # Scroll indicator
        if self._scroll_offset > 0:
            indicator = c(f" ^ {self._scroll_offset} more above ", YELLOW + BOLD)
            buf.append(_mv(self.HEADER_H + 1, chat_w - 18))
            buf.append(indicator)

    def _render_message(self, msg: DisplayMessage, width: int) -> list:
        if msg.scope == "system":
            return [c(f"  .  {msg.text}", YELLOW + DIM)]
        if msg.scope == "warn":
            return [c(f"  !  {msg.text}", RED + BOLD)]

        label = "GROUP" if msg.scope == "group" else "PRIV "
        lc    = GREEN   if msg.scope == "group" else CYAN
        tick  = c(" [ok]", GREEN) if msg.delivered else ""
        arrow = "-> " if msg.is_own else ""

        prefix_plain = f"  {msg.ts}  {label}  {arrow}{msg.sender}  "
        avail        = width - len(prefix_plain) - 1
        if avail < 8:
            avail = width - 2
            prefix_plain = ""

        body    = msg.text + (tick if not USE_COLOR else "")
        wrapped = _word_wrap(body, avail)

        if USE_COLOR:
            prefix_col = (
                c(f"  {msg.ts}  ", DIM) +
                c(label, lc + BOLD) +
                c(f"  {arrow}{msg.sender}  ", BOLD if msg.is_own else WHITE)
            )
        else:
            prefix_col = prefix_plain

        result = []
        for i, line in enumerate(wrapped):
            if i == 0:
                tail = tick if USE_COLOR else ""
                if msg.is_own:
                    result.append(c(prefix_col if USE_COLOR else prefix_plain, CYAN) + line + tail)
                else:
                    result.append((prefix_col if USE_COLOR else prefix_plain) + line + tail)
            else:
                result.append(" " * len(prefix_plain) + line)
        return result

    def _draw_divider(self, buf: list, col: int, h: int) -> None:
        for row in range(self.HEADER_H + 1, h - self.INPUT_H + 1):
            buf.append(_mv(row, col + 1))
            buf.append(c("|", WHITE + DIM))

    def _draw_sidebar(self, buf: list, left: int, width: int, msg_h: int) -> None:
        row = self.HEADER_H + 1
        bottom = self.HEADER_H + 1 + msg_h

        def sw(text: str, color: str = "", bold: bool = False) -> None:
            nonlocal row
            if row >= bottom:
                return
            raw = text[:width - 1].ljust(width - 1)
            if USE_COLOR and color:
                line = c(raw, color + (BOLD if bold else ""))
            else:
                line = raw
            buf.append(_mv(row, left))
            buf.append(line)
            row += 1

        # PEERS section
        sw("PEERS", CYAN, bold=True)
        sw("-" * (width - 2), DIM)
        sw(f"* {self._node.alias} (you)", CYAN, bold=True)
        for peer in self._peers:
            badge = f" [{peer.unread}]" if peer.unread > 0 else ""
            sw(f"* {peer.alias[:width-6]}{badge}", WHITE)
        if not self._peers:
            sw("  (no peers)", DIM)

        # FILES section
        if row < bottom - 2:
            row += 1
            sw("FILES", CYAN, bold=True)
            sw("-" * (width - 2), DIM)

            active = [f for f in self._files.values() if not f.done and not f.failed]
            done   = [f for f in self._files.values() if f.done][-3:]

            if not active and not done:
                sw("  (none)", DIM)

            for fd in active:
                arrow = "^" if fd.direction == "up" else "v"
                color = YELLOW if fd.direction == "up" else BLUE
                sw(f"{arrow} {fd.name[:width-5]}", color, bold=True)
                # Progress bar
                if row < bottom:
                    bar_w  = width - 7
                    filled = int(bar_w * fd.pct / 100)
                    bar    = "#" * filled + "-" * (bar_w - filled)
                    pct_s  = f"{fd.pct:3d}%"
                    pb_line = c(f"[{bar}]", CYAN) + " " + c(pct_s, color)
                    buf.append(_mv(row, left))
                    buf.append(pb_line[:width])
                    row += 1

            for fd in done:
                arrow = "^" if fd.direction == "up" else "v"
                sw(f"{arrow} {fd.name[:width-7]} [ok]", GREEN)

        # F1 help hint
        if row < bottom - 1:
            buf.append(_mv(bottom - 1, left))
            buf.append(c("F1 help", DIM))

    def _draw_input(self, buf: list, h: int, w: int) -> None:
        """
        Draw the 3-row input area at the bottom of the screen.

        Matches the macOS curses TUI exactly:
          Row -3: separator line (full width dashes)
          Row -2: " alias>  <typed text>█"  with black background
          Row -1: status bar (peers, typing indicator, hints)

        All three rows are padded to full terminal width so that
        characters from a previous (longer) frame are always erased.
        ESC[K (clear to end of line) is appended after each row as an
        additional safeguard against stale pixels on window resize.
        """
        input_row = h - self.INPUT_H + 1   # 1-based ANSI row of separator

        # ── Row 1: separator ──────────────────────────────────────────────────
        sep = "-" * (w - 1)
        buf.append(_mv(input_row, 1))
        if USE_COLOR:
            buf.append(c(sep, WHITE + DIM))
        else:
            buf.append(sep)
        buf.append("[K")   # clear any leftover pixels to the right

        # ── Row 2: prompt + typed text + block cursor ─────────────────────────
        prompt  = f" {self._node.alias}> "
        typed   = "".join(self._input_buf)
        cursor  = min(self._cursor_pos, len(self._input_buf))

        # Visible slice: keep the cursor visible by scrolling the view
        avail   = max(1, w - len(prompt) - 2)
        # Scroll window so cursor is always in view
        if cursor > avail:
            view_start = cursor - avail
        else:
            view_start = 0
        view_end   = view_start + avail
        visible    = typed[view_start:view_end]
        cursor_col = cursor - view_start   # cursor column within visible slice

        # Build display: insert █ at cursor position
        before = visible[:cursor_col]
        after  = visible[cursor_col:]
        content  = prompt + before + "█" + after
        # Right-pad to fill the full width so no stale chars remain
        padded   = content.ljust(w - 1)[:w - 1]

        buf.append(_mv(input_row + 1, 1))
        if USE_COLOR:
            # Black background row (mirrors C_INPUT_BAR in curses) + bold white text
            buf.append(c(padded, BG_BLACK + WHITE + BOLD))
        else:
            buf.append(padded)
        buf.append("[K[0m")   # clear EOL + reset so next row isn't on black bg

        # ── Row 3: status bar ─────────────────────────────────────────────────
        n_peers = len(self._peers)
        status  = f"  {n_peers} peer{'s' if n_peers!=1 else ''}"
        if self._typing_who:
            who     = ", ".join(self._typing_who)
            suffix  = "is" if len(self._typing_who) == 1 else "are"
            status += f"  >>  {who} {suffix} typing..."
        status += "  PgUp/Dn  /help"
        # Pad to full width so stale typing-indicator text is erased
        padded_status = status.ljust(w - 1)[:w - 1]

        buf.append(_mv(input_row + 2, 1))
        if USE_COLOR:
            buf.append(c(padded_status, WHITE + DIM))
        else:
            buf.append(padded_status)
        buf.append("[K")

    def _draw_modal(self, buf: list, h: int, w: int) -> None:
        modal = self._modal
        if not modal:
            return
        lines = modal.get("lines", [])
        mw    = min(64, w - 4)
        mh    = len(lines) + 4
        top   = (h - mh) // 2
        left  = (w - mw) // 2

        border_h = c("+" + "-" * (mw - 2) + "+", WHITE + BOLD + BG_BLACK)
        border_s = c("|" + " " * (mw - 2) + "|", WHITE + BG_BLACK)

        buf.append(_mv(top, left))
        buf.append(border_h)
        for r in range(1, mh - 1):
            buf.append(_mv(top + r, left))
            buf.append(border_s)
        buf.append(_mv(top + mh - 1, left))
        buf.append(border_h)

        for i, (text, color, bold_flag) in enumerate(lines):
            text = text[:mw - 4]
            if USE_COLOR:
                styled = c(text.ljust(mw - 4), color + (BOLD if bold_flag else ""))
            else:
                styled = text
            buf.append(_mv(top + 2 + i, left + 2))
            buf.append(styled)

    # ── Keyboard handling ─────────────────────────────────────────────────────

    def _handle_keys(self) -> None:
        while True:
            key = self._keys.read_key()
            if key is None:
                break

            # Modal takes priority
            if self._modal:
                self._handle_modal_key(key)
                continue

            if key == 'CTRL_C':
                self._running = False
                return

            if key == 'PGUP':
                h, w  = shutil.get_terminal_size((80, 24))
                msg_h = h - self.HEADER_H - self.INPUT_H
                self._scroll_offset = min(
                    self._scroll_offset + msg_h // 2,
                    len(self._messages)
                )
                continue

            if key == 'PGDN':
                h, w  = shutil.get_terminal_size((80, 24))
                msg_h = h - self.HEADER_H - self.INPUT_H
                self._scroll_offset = max(0, self._scroll_offset - msg_h // 2)
                continue

            if key == 'F1':
                self._show_help()
                continue

            if key == 'BACKSPACE':
                if self._input_buf and self._cursor_pos > 0:
                    del self._input_buf[self._cursor_pos - 1]
                    self._cursor_pos -= 1
                    asyncio.run_coroutine_threadsafe(
                        self._typing.local_keystroke(), self._loop
                    )
                continue

            if key == 'LEFT':
                if self._cursor_pos > 0:
                    self._cursor_pos -= 1
                continue

            if key == 'RIGHT':
                if self._cursor_pos < len(self._input_buf):
                    self._cursor_pos += 1
                continue

            if key == 'HOME':
                self._cursor_pos = 0
                continue

            if key == 'END':
                self._cursor_pos = len(self._input_buf)
                continue

            if key == 'ENTER':
                line = "".join(self._input_buf).strip()
                self._input_buf.clear()
                self._cursor_pos = 0
                if line:
                    asyncio.run_coroutine_threadsafe(
                        self._submit(line), self._loop
                    )
                continue

            if isinstance(key, str) and len(key) == 1 and ' ' <= key <= '~':
                self._input_buf.insert(self._cursor_pos, key)
                self._cursor_pos += 1
                asyncio.run_coroutine_threadsafe(
                    self._typing.local_keystroke(), self._loop
                )

    def _handle_modal_key(self, key: str) -> None:
        modal = self._modal
        if not modal:
            return
        mtype = modal.get("type")

        if mtype == "help":
            self._modal = None

        elif mtype == "tofu":
            if key == 'y':
                self._modal = None
                fut = modal.get("future")
                if fut:
                    self._loop.call_soon_threadsafe(fut.set_result, True)
            elif key == 'n':
                self._modal = None
                fut = modal.get("future")
                if fut:
                    self._loop.call_soon_threadsafe(fut.set_result, False)

        elif mtype == "keychange":
            self._modal = None   # any key dismisses

    # ── Help modal ────────────────────────────────────────────────────────────

    def _show_help(self) -> None:
        # Use same color constants as the curses TUI modals
        _W  = WHITE
        _C  = CYAN
        _D  = DIM
        _Y  = YELLOW
        _B  = BOLD
        self._modal = {
            "type": "help",
            "lines": [
                ("  Commands",                         CYAN,   True),
                ("  " + "-" * 37,                     DIM,    False),
                ("  /help               this screen",  WHITE,  False),
                ("  /peers              list peers",   WHITE,  False),
                ("  /connect <ip>       connect",      WHITE,  False),
                ("  /msg @alias <text>  private msg",  WHITE,  False),
                ("  /history [n]        history",      WHITE,  False),
                ("  /whoami             your info",    WHITE,  False),
                ("  /quit               exit",         WHITE,  False),
                ("",                                   WHITE,  False),
                ("  File Transfer",                    CYAN,   True),
                ("  " + "-" * 37,                     DIM,    False),
                ("  /sendfile @alias <path>",          WHITE,  False),
                ("  /sendfile <path>  (broadcast)",    WHITE,  False),
                ("  /accept <id>  /reject <id>",       WHITE,  False),
                ("  /transfers  (active)",             WHITE,  False),
                ("",                                   WHITE,  False),
                ("  Navigation",                       CYAN,   True),
                ("  " + "-" * 37,                     DIM,    False),
                ("  PgUp/PgDn  scroll messages",       WHITE,  False),
                ("  F1         this help",             WHITE,  False),
                ("  Ctrl+C     exit",                  WHITE,  False),
                ("",                                   WHITE,  False),
                ("  Press any key to close",           YELLOW, True),
            ]
        }

    # ── Input submission ──────────────────────────────────────────────────────

    async def _submit(self, line: str) -> None:
        await self._typing.local_sent()
        if line.startswith("/"):
            await self._handle_command(line)
        else:
            count = await self._node.broadcast_message(line)
            if count == 0:
                self._add_system("No peers connected -- message not sent.")
            else:
                msg = DisplayMessage(
                    ts=time.strftime("%H:%M"), scope="group",
                    sender=self._node.alias, recipient="",
                    text=line, is_own=True,
                )
                self._post({"type": "message", "msg": msg})

    async def _handle_command(self, line: str) -> None:
        parts = line.split(None, 2)
        cmd   = parts[0].lower()

        if cmd == "/help":
            self._show_help()

        elif cmd in ("/quit", "/exit", "/q"):
            self._add_system("Disconnecting...")
            self._running = False
            asyncio.create_task(self._node.stop())

        elif cmd == "/peers":
            peers = await self._node.get_peers()
            if not peers:
                self._add_system("No peers connected.")
            else:
                self._add_system(f"{len(peers)} peer(s):")
                for p in peers:
                    self._add_system(f"  @{p.alias}  {p.address}:{p.port}  fp:{p.fingerprint[:16]}...")

        elif cmd == "/connect":
            if len(parts) < 2:
                self._add_warn("Usage: /connect <ip>  or  /connect <ip:port>")
                return
            host = parts[1]; port = DEFAULT_PORT
            if ":" in host:
                host, ps = host.rsplit(":", 1)
                try:    port = int(ps)
                except: self._add_warn(f"Invalid port: {ps}"); return
            self._add_system(f"Connecting to {host}:{port}...")
            ok = await self._node.connect_to(host, port)
            if not ok:
                self._add_warn(f"Could not connect to {host}:{port}")

        elif cmd == "/msg":
            if len(parts) < 3:
                self._add_warn("Usage: /msg @alias <message>"); return
            target = parts[1].lstrip("@"); text = parts[2]
            conn   = await self._node._find_peer_by_alias(target)
            if not conn or not conn.info:
                self._add_warn(f"No peer named '{target}'"); return
            ok = await self._node.send_private_message(conn.info.fingerprint, text)
            if ok:
                self._post({"type": "message", "msg": DisplayMessage(
                    ts=time.strftime("%H:%M"), scope="private",
                    sender=self._node.alias, recipient=target,
                    text=text, is_own=True,
                )})
            else:
                self._add_warn(f"Could not send to {target}")

        elif cmd == "/say":
            text = line[len("/say"):].strip()
            if not text:
                self._add_warn("Usage: /say <message>"); return
            count = await self._node.broadcast_message(text)
            if count == 0:
                self._add_system("No peers connected.")
            else:
                self._post({"type": "message", "msg": DisplayMessage(
                    ts=time.strftime("%H:%M"), scope="group",
                    sender=self._node.alias, recipient="",
                    text=text, is_own=True,
                )})

        elif cmd == "/history":
            n = 20
            if len(parts) >= 2:
                try: n = int(parts[1])
                except: pass
            records = self._node.history.get_recent(n)
            self._add_system(f"Last {len(records)} message(s):")
            for r in records:
                scope = "PRIV" if r.scope == "private" else "GROUP"
                self._add_system(f"  [{r.ts_display}] {scope}  {r.sender}: {r.text}")

        elif cmd == "/whoami":
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("8.8.8.8", 80))
                    ip = s.getsockname()[0]
            except Exception:
                ip = "127.0.0.1"
            self._add_system(f"Alias: {self._node.alias}")
            self._add_system(f"Session: {self._node.session_name}")
            self._add_system(f"IP: {ip}  Port: {self._node._port}")
            self._add_system(f"Fingerprint: {self._node.fingerprint}")

        elif cmd == "/sendfile":
            if len(parts) < 2:
                self._add_warn("Usage: /sendfile @alias <path>  or  /sendfile <path>"); return
            if parts[1].startswith("@") and len(parts) >= 3:
                target = parts[1].lstrip("@"); fp = parts[2]
                self._add_system(f"Offering {fp} to @{target}...")
                tid = await self._node.send_file(fp, target)
                if not tid:
                    self._add_warn(f"Could not offer file to {target}.")
                else:
                    self._post({"type": "file_outbound", "transfer_id": tid,
                                "name": os.path.basename(fp), "peer": target})
            else:
                fp = parts[1]
                self._add_system(f"Broadcasting {fp} to all peers...")
                tids = await self._node.broadcast_file(fp)
                if not tids:
                    self._add_warn("No peers connected or file invalid.")
                else:
                    for tid in tids:
                        self._post({"type": "file_outbound", "transfer_id": tid,
                                    "name": os.path.basename(fp), "peer": "all"})

        elif cmd == "/accept":
            if len(parts) < 2:
                if not self._pending_offers:
                    self._add_system("No pending file offers.")
                else:
                    self._add_system("Pending offers:")
                    for tid, fd in self._pending_offers.items():
                        self._add_system(f"  {tid[:8]}  {fd.name} from @{fd.peer}")
                return
            short   = parts[1]
            matched = next((t for t in self._pending_offers if t.startswith(short)), None)
            if not matched:
                self._add_warn(f"No pending offer matching '{short}'"); return
            ok = await self._node.accept_file(matched)
            if ok:
                self._add_system(f"Accepted -- downloading {self._pending_offers[matched].name}...")
            else:
                self._add_warn("Could not accept transfer.")

        elif cmd == "/reject":
            if len(parts) < 2:
                self._add_warn("Usage: /reject <id>"); return
            short   = parts[1]
            matched = next((t for t in self._pending_offers if t.startswith(short)), None)
            if not matched:
                self._add_warn(f"No pending offer matching '{short}'"); return
            ok = await self._node.reject_file(matched)
            if ok:
                self._pending_offers.pop(matched, None)
                self._add_system("File offer declined.")

        elif cmd == "/transfers":
            active = self._node.files.active_transfers()
            if not active:
                self._add_system("No active transfers.")
            else:
                for t in active:
                    if isinstance(t, OutboundTransfer):
                        pct = int(t.bytes_sent / t.file_size * 100) if t.file_size else 0
                        self._add_system(f"  ^ {t.file_name} -> @{t.peer_alias}  {pct}%")
                    else:
                        pct = int(t.bytes_received / t.file_size * 100) if t.file_size else 0
                        self._add_system(f"  v {t.file_name} <- @{t.sender_alias}  {pct}%")

        else:
            self._add_warn(f"Unknown command: {cmd}  -- type /help")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _add_system(self, text: str) -> None:
        self._post({"type": "message", "msg": DisplayMessage(
            ts=time.strftime("%H:%M"), scope="system",
            sender="", recipient="", text=text
        )})

    def _add_warn(self, text: str) -> None:
        self._post({"type": "message", "msg": DisplayMessage(
            ts=time.strftime("%H:%M"), scope="warn",
            sender="", recipient="", text=text
        )})

    def _typing_started(self, alias: str) -> None:
        self._post({"type": "typing_start", "alias": alias})

    def _typing_stopped(self, alias: str) -> None:
        self._post({"type": "typing_stop", "alias": alias})

    async def _delayed_connect(self, host: str, port: int) -> None:
        await asyncio.sleep(0.8)
        self._add_system(f"Connecting to {host}:{port}...")
        ok = await self._node.connect_to(host, port)
        if not ok:
            self._add_warn(f"Could not connect to {host}:{port}")

    # ── Callback wiring ───────────────────────────────────────────────────────

    def _wire_callbacks(self) -> None:
        n = self._node

        async def on_message(peer_info, msg):
            if msg.get("type") != MSG_CHAT: return
            scope = msg.get("scope", "group")
            alias = msg.get("sender_alias", peer_info.alias)
            text  = msg.get("text", "")
            ts_ms = msg.get("ts", int(time.time()*1000))
            ts    = time.strftime("%H:%M", time.localtime(ts_ms/1000))
            dm = DisplayMessage(ts=ts, scope=scope, sender=alias,
                                recipient=self._node.alias if scope=="private" else "",
                                text=text, is_own=False,
                                msg_id=msg.get("msg_id",""))
            self._post({"type": "message", "msg": dm})
            if scope == "private":
                for p in self._peers:
                    if p.alias == alias:
                        p.unread += 1

        async def on_peer_joined(info):
            self._post({"type": "peer_joined", "info": info})

        async def on_peer_left(info):
            self._post({"type": "peer_left", "info": info})

        async def on_warn_timeout(info, secs):
            self._post({"type": "warn",
                        "text": f"Inactivity: {info.alias} closes in {secs}s"})

        async def on_delivery(msg_id, alias):
            self._post({"type": "delivered", "msg_id": msg_id})

        async def on_typing_start(alias):
            self._typing.peer_started(alias)

        async def on_typing_stop(alias):
            self._typing.peer_stopped(alias)

        async def on_file_offer(tid, sender, name, size):
            self._post({"type": "file_offer", "transfer_id": tid,
                        "sender": sender, "name": name, "size": size})

        async def on_file_progress(tid, done, total):
            self._post({"type": "file_progress", "transfer_id": tid,
                        "done": done, "total": total})

        async def on_file_complete(tid, path):
            self._post({"type": "file_complete", "transfer_id": tid, "path": path})

        async def on_file_error(tid, reason):
            self._post({"type": "file_error", "transfer_id": tid, "reason": reason})

        async def on_file_rejected(tid, peer):
            self._post({"type": "file_rejected", "transfer_id": tid, "peer": peer})

        n._on_message       = on_message
        n._on_peer_joined   = on_peer_joined
        n._on_peer_left     = on_peer_left
        n._on_warn_timeout  = on_warn_timeout
        n._on_delivery      = on_delivery
        n._on_typing_start  = on_typing_start
        n._on_typing_stop   = on_typing_stop
        n._on_file_offer    = on_file_offer
        n._on_file_progress = on_file_progress
        n._on_file_complete = on_file_complete
        n._on_file_error    = on_file_error
        n._on_file_rejected = on_file_rejected

    def _wire_security_callbacks(self) -> None:
        n = self._node

        async def on_tofu_prompt(alias, fingerprint, pub_key_hex) -> bool:
            fut = self._loop.create_future()
            lines = [
                ("  New Peer -- Verify Identity",         YELLOW, True),
                ("  " + "-" * 48,                         DIM,    False),
                (f"  Alias       : {alias}",              WHITE,  True),
                (f"  Fingerprint : {fingerprint[:32]}",   WHITE,  False),
                (f"               {fingerprint[32:]}",    WHITE,  False),
                ("",                                      WHITE,  False),
                ("  Verify out-of-band before accepting.", YELLOW, False),
                ("",                                      WHITE,  False),
                ("  Press  Y  to accept    N  to reject", GREEN,  True),
            ]
            self._modal = {"type": "tofu", "lines": lines, "future": fut}
            accepted = await fut
            if accepted:
                n._trust_store.trust(alias, fingerprint, pub_key_hex)
                self._add_system(f"@{alias} trusted and saved.")
            else:
                self._add_system(f"Connection from @{alias} rejected.")
            return accepted

        async def on_key_changed(alias, known_fp, new_fp):
            reset_cmd = (r"del %USERPROFILE%\.pymesh\known_peers.json"
                         if IS_WINDOWS else "rm ~/.pymesh/known_peers.json")
            lines = [
                ("  [!] SECURITY WARNING",                  RED,    True),
                ("  " + "-" * 48,                           DIM,    False),
                (f"  Key change detected for: {alias}",     RED,    True),
                ("",                                        WHITE,  False),
                (f"  Known : {known_fp[:40]}",              WHITE,  False),
                (f"  New   : {new_fp[:40]}",                WHITE,  False),
                ("",                                        WHITE,  False),
                ("  Possible MITM attack. BLOCKED.",         RED,    True),
                (f"  To reset: {reset_cmd}",                YELLOW, False),
                ("",                                        WHITE,  False),
                ("  Press any key to dismiss",              DIM,    False),
            ]
            self._modal = {"type": "keychange", "lines": lines}

        n._on_tofu_prompt = on_tofu_prompt
        n._on_key_changed = on_key_changed


# ==============================================================================
# Word wrap (same as tui.py)
# ==============================================================================

def _word_wrap(text: str, width: int) -> List[str]:
    if width <= 0: return [text]
    if len(text) <= width: return [text]
    lines = []
    while len(text) > width:
        cut = text.rfind(" ", 0, width)
        if cut <= 0: cut = width
        lines.append(text[:cut])
        text = text[cut:].lstrip()
    if text: lines.append(text)
    return lines or [""]


# Alias so main.py import works either way
TerminalUI = AnsiTUI
