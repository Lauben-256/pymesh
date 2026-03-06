# -*- coding: utf-8 -*-
"""
PyMesh Chat — Phase 5 TUI
Full split-pane terminal interface built on curses.

Layout:
┌──────────────────────────────────────────┬────────────────────┐
│ PyMesh  dev-team              14:32       │ PEERS           3  │
├──────────────────────────────────────────┤ ────────────────── │
│                                          │ ● you (alice)      │
│  14:30  GROUP  alice  hello everyone     │ ● bob              │
│  14:31  GROUP  bob    hey alice!         │ ● carol       [2]  │
│  14:31  PRIV   bob→   secret msg        │                    │
│  ✎ carol is typing...                   │ FILES              │
│                                          │ ↑ report.pdf  80%  │
│                                          │ ↓ photo.jpg   done │
├──────────────────────────────────────────┴────────────────────┤
│ alice>  _                                                      │
└────────────────────────────────────────────────────────────────┘

Features:
  - Messages scroll independently from input
  - Input box stays fixed at bottom
  - Peer list updates live
  - Typing indicator in message pane
  - File transfer progress in sidebar
  - Unread private message badges
  - PageUp/PageDown scroll history
  - F1 help overlay
  - Ctrl+C / /quit to exit
  - TOFU and key-change prompts as modal overlays
"""

import asyncio
import curses
import curses.textpad
import logging
import os
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
from pymesh.utils.constants import (
    DEFAULT_PORT, MSG_CHAT, APP_VERSION,
    MSG_FILE_OFFER,
)

log = logging.getLogger(__name__)


# ── Colour pair IDs ───────────────────────────────────────────────────────────
C_DEFAULT    = 0
C_HEADER     = 1   # header bar
C_HEADER_DIM = 2   # header secondary text
C_MSG_TIME   = 3   # timestamp
C_MSG_GROUP  = 4   # GROUP label
C_MSG_PRIV   = 5   # PRIV label
C_MSG_ALIAS  = 6   # sender alias
C_MSG_TEXT   = 7   # message body
C_MSG_OWN    = 8   # our own messages
C_MSG_DELIV  = 9   # delivery tick
C_TYPING     = 10  # typing indicator
C_PEER_DOT   = 11  # online dot
C_PEER_ALIAS = 12  # peer name
C_PEER_BADGE = 13  # unread badge
C_PEER_YOU   = 14  # your own name
C_SIDEBAR_H  = 15  # sidebar section header
C_FILE_UP    = 16  # outbound file
C_FILE_DOWN  = 17  # inbound file
C_FILE_DONE  = 18  # completed file
C_INPUT_BAR  = 19  # input bar background
C_INPUT_TEXT = 20  # input text
C_BORDER     = 21  # border lines
C_SYSTEM     = 22  # system messages
C_WARN       = 23  # warnings
C_MODAL_BG   = 24  # modal background
C_MODAL_WARN = 25  # modal warning text
C_MODAL_OK   = 26  # modal accept text
C_PROGRESS   = 27  # progress bar fill
C_PROGRESS_E = 28  # progress bar empty


def _init_colors() -> None:
    """Initialise all colour pairs. Called once after curses.start_color()."""
    curses.start_color()
    curses.use_default_colors()

    # Palette — dark background with vivid accents
    # bg = -1 means terminal default (transparent)
    bg = -1

    def p(pair_id, fg, bg=bg):
        curses.init_pair(pair_id, fg, bg)

    p(C_HEADER,     curses.COLOR_BLACK,  curses.COLOR_CYAN)
    p(C_HEADER_DIM, curses.COLOR_BLACK,  curses.COLOR_CYAN)
    p(C_MSG_TIME,   curses.COLOR_WHITE,  bg)
    p(C_MSG_GROUP,  curses.COLOR_GREEN,  bg)
    p(C_MSG_PRIV,   curses.COLOR_CYAN,   bg)
    p(C_MSG_ALIAS,  curses.COLOR_WHITE,  bg)
    p(C_MSG_TEXT,   curses.COLOR_WHITE,  bg)
    p(C_MSG_OWN,    curses.COLOR_CYAN,   bg)
    p(C_MSG_DELIV,  curses.COLOR_GREEN,  bg)
    p(C_TYPING,     curses.COLOR_YELLOW, bg)
    p(C_PEER_DOT,   curses.COLOR_GREEN,  bg)
    p(C_PEER_ALIAS, curses.COLOR_WHITE,  bg)
    p(C_PEER_BADGE, curses.COLOR_BLACK,  curses.COLOR_CYAN)
    p(C_PEER_YOU,   curses.COLOR_CYAN,   bg)
    p(C_SIDEBAR_H,  curses.COLOR_CYAN,   bg)
    p(C_FILE_UP,    curses.COLOR_YELLOW, bg)
    p(C_FILE_DOWN,  curses.COLOR_CYAN,   bg)
    p(C_FILE_DONE,  curses.COLOR_GREEN,  bg)
    p(C_INPUT_BAR,  curses.COLOR_WHITE,  curses.COLOR_BLACK)
    p(C_INPUT_TEXT, curses.COLOR_WHITE,  curses.COLOR_BLACK)
    p(C_BORDER,     curses.COLOR_WHITE,  bg)
    p(C_SYSTEM,     curses.COLOR_YELLOW, bg)
    p(C_WARN,       curses.COLOR_RED,    bg)
    p(C_MODAL_BG,   curses.COLOR_WHITE,  curses.COLOR_BLACK)
    p(C_MODAL_WARN, curses.COLOR_RED,    curses.COLOR_BLACK)
    p(C_MODAL_OK,   curses.COLOR_GREEN,  curses.COLOR_BLACK)
    p(C_PROGRESS,   curses.COLOR_BLACK,  curses.COLOR_CYAN)
    p(C_PROGRESS_E, curses.COLOR_WHITE,  bg)


# ── Message record for display ────────────────────────────────────────────────

@dataclass
class DisplayMessage:
    ts:         str        # "HH:MM"
    scope:      str        # "group" | "private" | "system" | "warn"
    sender:     str
    recipient:  str        # empty for group
    text:       str
    is_own:     bool = False
    delivered:  bool = False
    msg_id:     str  = ""


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
    direction:   str   # "up" or "down"
    peer:        str
    pct:         int   = 0
    done:        bool  = False
    failed:      bool  = False


# ══════════════════════════════════════════════════════════════════════════════
# Main TUI class
# ══════════════════════════════════════════════════════════════════════════════

class TUI:
    """
    Phase 5 curses-based split-pane interface.
    Runs entirely inside a single asyncio task via run_in_executor.
    All node callbacks post events to a thread-safe asyncio.Queue
    which the curses loop drains on each tick.
    """

    SIDEBAR_W  = 22   # width of right-side panel
    HEADER_H   = 2    # header rows
    INPUT_H    = 3    # input area rows
    TICK_MS    = 50   # render tick in milliseconds

    def __init__(self, node: Node, connect_on_start: Optional[Tuple[str, int]] = None):
        self._node             = node
        self._connect_on_start = connect_on_start

        # Display state
        self._messages: deque    = deque(maxlen=1000)
        self._peers: List[PeerDisplay] = []
        self._files: Dict[str, FileDisplay] = {}
        self._typing_who: List[str] = []
        self._input_buf: List[str]  = []
        self._scroll_offset: int    = 0    # lines scrolled up from bottom
        self._msg_id_map: Dict[str, DisplayMessage] = {}

        # Pending TOFU / modal state
        self._modal: Optional[dict] = None   # {"type", "text", "resolve"}

        # Event queue — node callbacks post here, curses loop reads
        self._events: asyncio.Queue = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Typing tracker
        self._typing = TypingTracker(
            on_peer_started = self._typing_started,
            on_peer_stopped = self._typing_stopped,
            send_start      = node.send_typing_start,
            send_stop       = node.send_typing_stop,
        )

        # Pending file offers: transfer_id → FileDisplay
        self._pending_offers: Dict[str, FileDisplay] = {}

        self._running = False
        self._stdscr  = None

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wire_callbacks()
        self._wire_security_callbacks()

        if self._connect_on_start:
            host, port = self._connect_on_start
            asyncio.create_task(self._delayed_connect(host, port))

        # Run curses in a thread executor so it doesn't block the event loop
        await self._loop.run_in_executor(None, self._curses_main)
        self._typing.stop()

    # ── Curses main ───────────────────────────────────────────────────────────

    def _curses_main(self) -> None:
        try:
            curses.wrapper(self._run_curses)
        except Exception as exc:
            log.error("Curses error: %s", exc)

    def _run_curses(self, stdscr) -> None:
        self._stdscr = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)

        if curses.has_colors():
            _init_colors()

        self._running = True
        self._add_system("PyMesh Chat started. Type /help for commands.")

        while self._running:
            # Drain event queue
            self._drain_events()

            # Handle keyboard input
            self._handle_keys()

            # Redraw everything
            self._redraw()

            curses.napms(self.TICK_MS)

    # ── Event queue ───────────────────────────────────────────────────────────

    def _post(self, event: dict) -> None:
        """Post an event to the queue — safe from any thread or coroutine."""
        try:
            # If we're already on the event loop thread, put directly
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
        """Process all pending events from the queue."""
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
            self._scroll_offset = 0   # scroll to bottom on new message

        elif t == "system":
            self._add_system(ev["text"])

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
            self._peers = [p for p in self._peers if p.fp != info.fingerprint]
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
                transfer_id = ev["transfer_id"],
                name        = ev["name"],
                direction   = "down",
                peer        = ev["sender"],
            )
            self._files[ev["transfer_id"]] = fd
            self._pending_offers[ev["transfer_id"]] = fd
            self._add_system(
                f"@{ev['sender']} wants to send {ev['name']} "
                f"({_fmt_size(ev['size'])})  "
                f"— /accept {ev['transfer_id'][:8]}  or  /reject {ev['transfer_id'][:8]}"
            )

        elif t == "file_progress":
            tid   = ev["transfer_id"]
            done  = ev["done"]
            total = ev["total"]
            if tid in self._files:
                self._files[tid].pct = int(done / total * 100) if total else 0

        elif t == "file_complete":
            tid  = ev["transfer_id"]
            path = ev["path"]
            if tid in self._files:
                self._files[tid].done = True
                self._files[tid].pct  = 100
            self._pending_offers.pop(tid, None)
            self._add_system(
                f"Transfer complete: {os.path.basename(path)}  →  {path}"
            )

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
                transfer_id = ev["transfer_id"],
                name        = ev["name"],
                direction   = "up",
                peer        = ev["peer"],
            )
            self._files[ev["transfer_id"]] = fd

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        if not self._stdscr:
            return
        try:
            h, w = self._stdscr.getmaxyx()
            if h < 10 or w < 40:
                self._stdscr.clear()
                self._stdscr.addstr(0, 0, "Terminal too small")
                self._stdscr.refresh()
                return

            self._stdscr.erase()

            sidebar_w = min(self.SIDEBAR_W, w // 3)
            chat_w    = w - sidebar_w - 1    # -1 for divider
            msg_h     = h - self.HEADER_H - self.INPUT_H

            self._draw_header(h, w, chat_w, sidebar_w)
            self._draw_messages(self.HEADER_H, 0, msg_h, chat_w)
            self._draw_divider(self.HEADER_H, chat_w, h)
            self._draw_sidebar(self.HEADER_H, chat_w + 1, h - self.INPUT_H, sidebar_w)
            self._draw_input(h - self.INPUT_H, 0, w)

            if self._modal:
                self._draw_modal(h, w)

            self._stdscr.refresh()
        except curses.error:
            pass

    def _draw_header(self, h, w, chat_w, sidebar_w) -> None:
        scr = self._stdscr
        try:
            # Full-width header bar
            scr.attron(curses.color_pair(C_HEADER) | curses.A_BOLD)
            scr.addstr(0, 0, " " * w)
            scr.attroff(curses.color_pair(C_HEADER) | curses.A_BOLD)

            title    = f" ⬡ PyMesh  {self._node.session_name}"
            ts       = time.strftime("%H:%M")
            peer_cnt = f"{len(self._peers)} peer{'s' if len(self._peers)!=1 else ''}"
            right    = f"{peer_cnt}  {ts} "

            scr.attron(curses.color_pair(C_HEADER) | curses.A_BOLD)
            scr.addstr(0, 0, title[:w-1])
            if len(right) < w:
                scr.addstr(0, w - len(right) - 1, right)
            scr.attroff(curses.color_pair(C_HEADER) | curses.A_BOLD)

            # Second row: alias + fingerprint hint
            fp_hint = f" {self._node.alias}  ·  fp:{self._node.fingerprint[:12]}..."
            scr.attron(curses.color_pair(C_HEADER_DIM))
            scr.addstr(1, 0, " " * w)
            scr.addstr(1, 0, fp_hint[:w-1])
            scr.attroff(curses.color_pair(C_HEADER_DIM))
        except curses.error:
            pass

    def _draw_messages(self, top, left, height, width) -> None:
        scr = self._stdscr
        if height <= 0 or width <= 0:
            return

        # Build rendered lines from message list
        lines = []
        msgs  = list(self._messages)

        for msg in msgs:
            rendered = self._render_message(msg, width)
            lines.extend(rendered)

        # Typing indicator at end
        if self._typing_who:
            who = ", ".join(self._typing_who)
            suffix = "is" if len(self._typing_who) == 1 else "are"
            lines.append(("typing", f"  ✎  {who} {suffix} typing...", C_TYPING, 0))

        # Apply scroll
        total = len(lines)
        if self._scroll_offset > total - height:
            self._scroll_offset = max(0, total - height)

        visible_start = max(0, total - height - self._scroll_offset)
        visible       = lines[visible_start : visible_start + height]

        # Fill empty space
        for row in range(height):
            try:
                if row < len(visible):
                    tag, text, pair, attr = visible[row]
                    # Truncate to width
                    text = text[:width - 1]
                    pad  = " " * (width - len(text) - 1)
                    scr.addstr(top + row, left, text + pad,
                               curses.color_pair(pair) | attr)
                else:
                    scr.addstr(top + row, left, " " * (width - 1))
            except curses.error:
                pass

        # Scroll indicator
        if self._scroll_offset > 0:
            try:
                indicator = f" ↑ {self._scroll_offset} lines above "
                scr.attron(curses.color_pair(C_SYSTEM) | curses.A_BOLD)
                scr.addstr(top, left + width - len(indicator) - 2, indicator)
                scr.attroff(curses.color_pair(C_SYSTEM) | curses.A_BOLD)
            except curses.error:
                pass

    def _render_message(self, msg: DisplayMessage, width: int) -> list:
        """
        Render a DisplayMessage into a list of (tag, text, color_pair, attr) tuples.
        Returns multiple lines for word-wrapped messages.
        """
        lines = []

        if msg.scope == "system":
            text = f"  ·  {msg.text}"
            lines.append(("sys", text, C_SYSTEM, curses.A_DIM))
            return lines

        if msg.scope == "warn":
            text = f"  ⚠  {msg.text}"
            lines.append(("warn", text, C_WARN, curses.A_BOLD))
            return lines

        # Chat message
        if msg.scope == "group":
            label = "GROUP"
            lc    = C_MSG_GROUP
        else:
            label = f"PRIV"
            lc    = C_MSG_PRIV

        tick  = " ✓" if msg.delivered else ""
        arrow = "→ " if msg.is_own else ""

        # Prefix: "  14:32  GROUP  alice  "
        prefix = f"  {msg.ts}  {label}  {arrow}{msg.sender}  "
        body   = msg.text + tick

        # Word-wrap body to fit remaining width
        avail = width - len(prefix) - 2
        if avail < 10:
            avail = width - 2
            prefix = ""

        wrapped = _word_wrap(body, avail)

        for i, line in enumerate(wrapped):
            if i == 0:
                full = prefix + line
                attr = curses.A_BOLD if msg.is_own else 0
                pair = C_MSG_OWN if msg.is_own else C_MSG_TEXT
                lines.append(("msg", full, pair, attr))
            else:
                indent = " " * len(prefix)
                lines.append(("msg", indent + line, C_MSG_TEXT, 0))

        return lines

    def _draw_divider(self, top, col, h) -> None:
        scr = self._stdscr
        try:
            scr.attron(curses.color_pair(C_BORDER))
            for row in range(top, h):
                scr.addch(row, col, curses.ACS_VLINE)
            scr.attroff(curses.color_pair(C_BORDER))
        except curses.error:
            pass

    def _draw_sidebar(self, top, left, bottom, width) -> None:
        scr = self._stdscr
        if width <= 2:
            return

        row = top

        def swrite(text, pair=C_DEFAULT, attr=0, bold=False):
            nonlocal row
            if row >= bottom:
                return
            try:
                a = attr | (curses.A_BOLD if bold else 0)
                text = (" " + text)[:width - 1]
                pad  = " " * (width - len(text) - 1)
                scr.addstr(row, left, text + pad, curses.color_pair(pair) | a)
                row += 1
            except curses.error:
                pass

        # ── Peers section ──────────────────────────────────────────────────
        swrite("PEERS", C_SIDEBAR_H, curses.A_BOLD)
        swrite("─" * (width - 2), C_BORDER)

        # Ourselves first
        swrite(f"● {self._node.alias} (you)", C_PEER_YOU, curses.A_BOLD)

        for peer in self._peers:
            badge = f" [{peer.unread}]" if peer.unread > 0 else ""
            name  = peer.alias[:width - 6] + badge
            swrite(f"● {name}", C_PEER_ALIAS)

        if not self._peers:
            swrite("  (no peers)", C_BORDER)

        # ── Files section ──────────────────────────────────────────────────
        if row < bottom - 2:
            row += 1
            swrite("FILES", C_SIDEBAR_H, curses.A_BOLD)
            swrite("─" * (width - 2), C_BORDER)

            active_files = [
                f for f in self._files.values()
                if not f.done and not f.failed
            ]
            done_files = [
                f for f in self._files.values()
                if f.done
            ][-3:]   # show last 3 completed

            if not active_files and not done_files:
                swrite("  (none)", C_BORDER)

            for fd in active_files:
                arrow = "↑" if fd.direction == "up" else "↓"
                pair  = C_FILE_UP if fd.direction == "up" else C_FILE_DOWN
                name  = fd.name[:width - 9]
                swrite(f"{arrow} {name}", pair, curses.A_BOLD)
                # Progress bar
                if row < bottom:
                    bar_w   = width - 7
                    filled  = int(bar_w * fd.pct / 100)
                    empty   = bar_w - filled
                    bar     = "█" * filled + "░" * empty
                    pct_str = f"{fd.pct:3d}%"
                    try:
                        scr.addstr(row, left + 1, bar[:bar_w],
                                   curses.color_pair(C_PROGRESS))
                        scr.addstr(row, left + 1 + bar_w + 1, pct_str,
                                   curses.color_pair(pair))
                        row += 1
                    except curses.error:
                        row += 1

            for fd in done_files:
                arrow = "↑" if fd.direction == "up" else "↓"
                name  = fd.name[:width - 8]
                swrite(f"{arrow} {name} ✓", C_FILE_DONE)

        # ── Help hint at bottom ────────────────────────────────────────────
        if row < bottom - 1:
            row = bottom - 2
            swrite("F1 help", C_BORDER, curses.A_DIM)

    def _draw_input(self, top, left, width) -> None:
        scr = self._stdscr
        try:
            # Separator line
            scr.attron(curses.color_pair(C_BORDER))
            scr.addstr(top, left, "─" * (width - 1))
            scr.attroff(curses.color_pair(C_BORDER))

            # Input line
            prompt = f" {self._node.alias}> "
            buf    = "".join(self._input_buf)
            # Truncate visible portion if line is long
            avail  = width - len(prompt) - 2
            if len(buf) > avail:
                visible = buf[len(buf) - avail:]
            else:
                visible = buf
            cursor = "█"   # block cursor

            line = prompt + visible + cursor
            scr.attron(curses.color_pair(C_INPUT_BAR) | curses.A_BOLD)
            scr.addstr(top + 1, left, " " * (width - 1))
            scr.addstr(top + 1, left, line[:width - 1],
                       curses.color_pair(C_INPUT_TEXT) | curses.A_BOLD)
            scr.attroff(curses.color_pair(C_INPUT_BAR) | curses.A_BOLD)

            # Status line
            peer_count = len(self._peers)
            status = f"  {peer_count} peer{'s' if peer_count!=1 else ''}  ·  "
            if self._typing_who:
                who = ", ".join(self._typing_who)
                status += f"✎ {who} typing...  ·  "
            status += "PgUp/Dn scroll  ·  /help"
            scr.attron(curses.color_pair(C_BORDER) | curses.A_DIM)
            scr.addstr(top + 2, left, status[:width - 1])
            scr.attroff(curses.color_pair(C_BORDER) | curses.A_DIM)
        except curses.error:
            pass

    def _draw_modal(self, h, w) -> None:
        """Draw a centred modal overlay."""
        if not self._modal:
            return

        modal    = self._modal
        lines    = modal.get("lines", [""])
        mw       = min(70, w - 4)
        mh       = len(lines) + 4
        top      = (h - mh) // 2
        left     = (w - mw) // 2

        scr = self._stdscr
        try:
            # Shadow / background
            for r in range(mh):
                scr.attron(curses.color_pair(C_MODAL_BG))
                scr.addstr(top + r, left, " " * mw)
                scr.attroff(curses.color_pair(C_MODAL_BG))

            # Border
            scr.attron(curses.color_pair(C_MODAL_BG) | curses.A_BOLD)
            scr.addstr(top, left, "┌" + "─" * (mw - 2) + "┐")
            scr.addstr(top + mh - 1, left, "└" + "─" * (mw - 2) + "┘")
            for r in range(1, mh - 1):
                scr.addstr(top + r, left, "│")
                scr.addstr(top + r, left + mw - 1, "│")
            scr.attroff(curses.color_pair(C_MODAL_BG) | curses.A_BOLD)

            # Content
            for i, (text, pair, attr) in enumerate(lines):
                text = text[:mw - 4]
                scr.attron(curses.color_pair(pair) | attr)
                scr.addstr(top + 1 + i, left + 2, text)
                scr.attroff(curses.color_pair(pair) | attr)
        except curses.error:
            pass

    def _draw_help(self, h, w) -> None:
        lines = [
            ("  Commands", C_SIDEBAR_H, curses.A_BOLD),
            ("  ─────────────────────────────────────", C_BORDER, 0),
            ("  /help               this screen", C_MSG_TEXT, 0),
            ("  /peers              list peers", C_MSG_TEXT, 0),
            ("  /connect <ip>       connect to peer", C_MSG_TEXT, 0),
            ("  /msg @alias <text>  private message", C_MSG_TEXT, 0),
            ("  /history [n]        show last n messages", C_MSG_TEXT, 0),
            ("  /whoami             your info", C_MSG_TEXT, 0),
            ("  /quit               exit", C_MSG_TEXT, 0),
            ("", C_DEFAULT, 0),
            ("  File Transfer", C_SIDEBAR_H, curses.A_BOLD),
            ("  ─────────────────────────────────────", C_BORDER, 0),
            ("  /sendfile @alias <path>   send to peer", C_MSG_TEXT, 0),
            ("  /sendfile <path>          broadcast", C_MSG_TEXT, 0),
            ("  /accept <id>              accept offer", C_MSG_TEXT, 0),
            ("  /reject <id>              decline offer", C_MSG_TEXT, 0),
            ("  /transfers                active transfers", C_MSG_TEXT, 0),
            ("", C_DEFAULT, 0),
            ("  Navigation", C_SIDEBAR_H, curses.A_BOLD),
            ("  ─────────────────────────────────────", C_BORDER, 0),
            ("  PgUp / PgDn        scroll messages", C_MSG_TEXT, 0),
            ("  F1                 toggle this help", C_MSG_TEXT, 0),
            ("  Ctrl+C / /quit     exit", C_MSG_TEXT, 0),
            ("", C_DEFAULT, 0),
            ("  Press any key to close", C_SYSTEM, curses.A_BOLD),
        ]
        self._modal = {"lines": lines, "type": "help"}

    # ── Keyboard input ────────────────────────────────────────────────────────

    def _handle_keys(self) -> None:
        scr = self._stdscr
        try:
            key = scr.getch()
        except curses.error:
            return

        if key == curses.ERR:
            return

        # If modal is open, any key closes it (unless TOFU)
        if self._modal:
            mtype = self._modal.get("type")
            if mtype == "help":
                self._modal = None
                return
            elif mtype in ("tofu", "keychange"):
                self._handle_modal_key(key)
                return

        # Navigation keys
        if key == curses.KEY_PPAGE:    # Page Up
            h, w = scr.getmaxyx()
            msg_h = h - self.HEADER_H - self.INPUT_H
            self._scroll_offset = min(
                self._scroll_offset + msg_h // 2,
                len(self._messages)
            )
            return

        if key == curses.KEY_NPAGE:    # Page Down
            h, w = scr.getmaxyx()
            msg_h = h - self.HEADER_H - self.INPUT_H
            self._scroll_offset = max(0, self._scroll_offset - msg_h // 2)
            return

        if key == curses.KEY_F1:
            self._draw_help(*scr.getmaxyx())
            return

        # Backspace
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self._input_buf:
                self._input_buf.pop()
                asyncio.run_coroutine_threadsafe(
                    self._typing.local_keystroke(), self._loop
                )
            return

        # Enter — submit
        if key in (10, 13, curses.KEY_ENTER):
            line = "".join(self._input_buf).strip()
            self._input_buf.clear()
            if line:
                asyncio.run_coroutine_threadsafe(
                    self._submit(line), self._loop
                )
            return

        # Printable character
        if 32 <= key <= 126:
            self._input_buf.append(chr(key))
            asyncio.run_coroutine_threadsafe(
                self._typing.local_keystroke(), self._loop
            )
            return

    def _handle_modal_key(self, key: int) -> None:
        modal = self._modal
        if not modal:
            return
        ch = chr(key).lower() if 32 <= key <= 126 else ""

        if modal["type"] == "tofu":
            if ch in ("y",):
                self._modal = None
                fut = modal.get("future")
                if fut:
                    self._loop.call_soon_threadsafe(fut.set_result, True)
            elif ch in ("n",):
                self._modal = None
                fut = modal.get("future")
                if fut:
                    self._loop.call_soon_threadsafe(fut.set_result, False)

        elif modal["type"] == "keychange":
            self._modal = None   # dismiss on any key

    # ── Input submission ──────────────────────────────────────────────────────

    async def _submit(self, line: str) -> None:
        await self._typing.local_sent()

        if line.startswith("/"):
            await self._handle_command(line)
        else:
            count = await self._node.broadcast_message(line)
            if count == 0:
                self._add_system("No peers connected — message not sent.")
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
            h, w = self._stdscr.getmaxyx() if self._stdscr else (24, 80)
            self._draw_help(h, w)

        elif cmd == "/quit" or cmd == "/exit" or cmd == "/q":
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
            host = parts[1]
            port = DEFAULT_PORT
            if ":" in host:
                host, port_str = host.rsplit(":", 1)
                try: port = int(port_str)
                except ValueError:
                    self._add_warn(f"Invalid port: {port_str}")
                    return
            self._add_system(f"Connecting to {host}:{port}...")
            ok = await self._node.connect_to(host, port)
            if not ok:
                self._add_warn(f"Could not connect to {host}:{port}")

        elif cmd == "/msg":
            if len(parts) < 3:
                self._add_warn("Usage: /msg @alias <message>")
                return
            target = parts[1].lstrip("@")
            text   = parts[2]
            conn   = await self._node._find_peer_by_alias(target)
            if not conn or not conn.info:
                self._add_warn(f"No peer named '{target}'")
                return
            ok = await self._node.send_private_message(conn.info.fingerprint, text)
            if ok:
                msg = DisplayMessage(
                    ts=time.strftime("%H:%M"), scope="private",
                    sender=self._node.alias, recipient=target,
                    text=text, is_own=True,
                )
                self._post({"type": "message", "msg": msg})
            else:
                self._add_warn(f"Could not send to {target}")

        elif cmd == "/say":
            if len(parts) < 2:
                self._add_warn("Usage: /say <message>")
                return
            text  = line[len("/say"):].strip()
            count = await self._node.broadcast_message(text)
            if count == 0:
                self._add_system("No peers connected.")
            else:
                msg = DisplayMessage(
                    ts=time.strftime("%H:%M"), scope="group",
                    sender=self._node.alias, recipient="",
                    text=text, is_own=True,
                )
                self._post({"type": "message", "msg": msg})

        elif cmd == "/history":
            n = 20
            if len(parts) >= 2:
                try: n = int(parts[1])
                except ValueError: pass
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
                self._add_warn("Usage: /sendfile @alias <path>  or  /sendfile <path>")
                return
            if parts[1].startswith("@") and len(parts) >= 3:
                target    = parts[1].lstrip("@")
                file_path = parts[2]
                self._add_system(f"Offering {file_path} to @{target}...")
                tid = await self._node.send_file(file_path, target)
                if not tid:
                    self._add_warn(f"Could not offer file to {target}.")
                else:
                    self._post({"type": "file_outbound", "transfer_id": tid,
                                "name": os.path.basename(file_path), "peer": target})
            else:
                file_path = parts[1]
                self._add_system(f"Broadcasting {file_path} to all peers...")
                tids = await self._node.broadcast_file(file_path)
                if not tids:
                    self._add_warn("No peers connected or file invalid.")
                else:
                    for tid in tids:
                        self._post({"type": "file_outbound", "transfer_id": tid,
                                    "name": os.path.basename(file_path), "peer": "all"})

        elif cmd == "/accept":
            if len(parts) < 2:
                if not self._pending_offers:
                    self._add_system("No pending file offers.")
                else:
                    self._add_system("Pending offers:")
                    for tid, fd in self._pending_offers.items():
                        self._add_system(f"  {tid[:8]}  {fd.name} from @{fd.peer}")
                return
            short = parts[1]
            matched = next((t for t in self._pending_offers if t.startswith(short)), None)
            if not matched:
                self._add_warn(f"No pending offer matching '{short}'")
                return
            ok = await self._node.accept_file(matched)
            if ok:
                self._add_system(f"Accepted — downloading {self._pending_offers[matched].name}...")
            else:
                self._add_warn("Could not accept transfer.")

        elif cmd == "/reject":
            if len(parts) < 2:
                self._add_warn("Usage: /reject <id>")
                return
            short   = parts[1]
            matched = next((t for t in self._pending_offers if t.startswith(short)), None)
            if not matched:
                self._add_warn(f"No pending offer matching '{short}'")
                return
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
                        self._add_system(f"  ↑ {t.file_name} → @{t.peer_alias}  {pct}%")
                    else:
                        pct = int(t.bytes_received / t.file_size * 100) if t.file_size else 0
                        self._add_system(f"  ↓ {t.file_name} ← @{t.sender_alias}  {pct}%")

        else:
            self._add_warn(f"Unknown command: {cmd}  —  type /help")

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

    # ── Node callback wiring ──────────────────────────────────────────────────

    def _wire_callbacks(self) -> None:
        n = self._node

        async def on_message(peer_info, msg):
            mt = msg.get("type")
            if mt != MSG_CHAT: return
            scope = msg.get("scope", "group")
            alias = msg.get("sender_alias", peer_info.alias)
            text  = msg.get("text", "")
            ts_ms = msg.get("ts", int(time.time()*1000))
            ts    = time.strftime("%H:%M", time.localtime(ts_ms/1000))
            dm = DisplayMessage(
                ts=ts, scope=scope, sender=alias,
                recipient=self._node.alias if scope=="private" else "",
                text=text, is_own=False,
                msg_id=msg.get("msg_id",""),
            )
            self._post({"type": "message", "msg": dm})
            # Badge for private messages
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
        # TOFU and key-change are wired separately in _wire_security_callbacks()
        # so tests can use _wire_callbacks() without needing a running curses modal

    def _wire_security_callbacks(self) -> None:
        """Wire TOFU and key-change callbacks — only called when running interactively."""
        n = self._node

        async def on_tofu_prompt(alias, fingerprint, pub_key_hex) -> bool:
            fut = self._loop.create_future()
            lines = [
                ("  New Peer — Verify Identity", C_SYSTEM, curses.A_BOLD),
                ("  ─────────────────────────────────────────────────────", C_BORDER, 0),
                (f"  Alias       : {alias}", C_MSG_TEXT, curses.A_BOLD),
                (f"  Fingerprint : {fingerprint[:32]}", C_MSG_TEXT, 0),
                (f"               {fingerprint[32:]}", C_MSG_TEXT, 0),
                ("", C_DEFAULT, 0),
                ("  Verify this fingerprint out-of-band before accepting.", C_SYSTEM, 0),
                ("", C_DEFAULT, 0),
                ("  Press  Y  to accept    N  to reject", C_MODAL_OK, curses.A_BOLD),
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
            lines = [
                ("  ⚠  SECURITY WARNING", C_WARN, curses.A_BOLD),
                ("  ─────────────────────────────────────────────────────", C_BORDER, 0),
                (f"  Key change detected for: {alias}", C_WARN, curses.A_BOLD),
                ("", C_DEFAULT, 0),
                (f"  Known : {known_fp[:48]}", C_MSG_TEXT, 0),
                (f"  New   : {new_fp[:48]}", C_MSG_TEXT, 0),
                ("", C_DEFAULT, 0),
                ("  Possible MITM attack. Connection BLOCKED.", C_WARN, curses.A_BOLD),
                ("  To reset: rm ~/.pymesh/known_peers.json", C_SYSTEM, 0),
                ("", C_DEFAULT, 0),
                ("  Press any key to dismiss", C_BORDER, curses.A_DIM),
            ]
            self._modal = {"type": "keychange", "lines": lines}

        n._on_tofu_prompt = on_tofu_prompt
        n._on_key_changed = on_key_changed


# ── Word wrap helper ──────────────────────────────────────────────────────────

def _word_wrap(text: str, width: int) -> List[str]:
    """Wrap text to width, breaking on spaces."""
    if width <= 0:
        return [text]
    if len(text) <= width:
        return [text]
    lines = []
    while len(text) > width:
        cut = text.rfind(" ", 0, width)
        if cut <= 0:
            cut = width
        lines.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        lines.append(text)
    return lines or [""]
