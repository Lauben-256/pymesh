"""
PyMesh Chat — Typing Indicators
Tracks who is currently typing and manages the debounce logic
for sending TYPING_START / TYPING_STOP messages.

Two sides to this:

OUTBOUND (our typing):
  - User starts typing → send TYPING_START to all peers
  - User pauses for TYPING_DEBOUNCE seconds → send TYPING_STOP
  - User sends the message → send TYPING_STOP immediately

INBOUND (peer typing):
  - Receive TYPING_START → mark peer as typing, start expiry timer
  - Receive TYPING_STOP → mark peer as stopped
  - No message for TYPING_TIMEOUT seconds → assume stopped (connection hiccup)
"""

import asyncio
import logging
import time
from typing import Callable, Dict, Optional, Set

from pymesh.utils.constants import TYPING_DEBOUNCE, TYPING_TIMEOUT

log = logging.getLogger(__name__)


class TypingTracker:
    """
    Manages typing state for both local user and remote peers.

    Usage:
        tracker = TypingTracker(
            on_started=lambda alias: print(f"{alias} is typing..."),
            on_stopped=lambda alias: print(f"{alias} stopped"),
            send_typing_start=node.send_typing_start,
            send_typing_stop=node.send_typing_stop,
        )

        # Call when user types a character
        await tracker.local_keystroke()

        # Call when user sends or clears their message
        await tracker.local_sent()

        # Call when TYPING_START received from peer
        tracker.peer_started(alias)

        # Call when TYPING_STOP received from peer
        tracker.peer_stopped(alias)
    """

    def __init__(
        self,
        on_peer_started: Optional[Callable] = None,   # (alias) -> None
        on_peer_stopped: Optional[Callable] = None,   # (alias) -> None
        send_start: Optional[Callable] = None,         # async () -> None
        send_stop: Optional[Callable] = None,          # async () -> None
    ):
        self._on_peer_started = on_peer_started
        self._on_peer_stopped = on_peer_stopped
        self._send_start      = send_start
        self._send_stop       = send_stop

        # Outbound state
        self._we_are_typing     = False
        self._debounce_task: Optional[asyncio.Task] = None

        # Inbound state: alias → timestamp of last TYPING_START
        self._peer_typing: Dict[str, float] = {}
        self._peer_expiry_tasks: Dict[str, asyncio.Task] = {}

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def local_keystroke(self) -> None:
        """
        Called every time the local user types a character.
        Sends TYPING_START if not already sent, resets debounce timer.
        """
        # Cancel existing debounce
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        # Send TYPING_START only on first keystroke
        if not self._we_are_typing:
            self._we_are_typing = True
            if self._send_start:
                try:
                    await self._send_start()
                except Exception as exc:
                    log.debug("Failed to send TYPING_START: %s", exc)

        # Schedule TYPING_STOP after silence
        self._debounce_task = asyncio.create_task(self._debounce_stop())

    async def local_sent(self) -> None:
        """
        Called when the local user sends or clears their message.
        Sends TYPING_STOP immediately.
        """
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        if self._we_are_typing:
            self._we_are_typing = False
            if self._send_stop:
                try:
                    await self._send_stop()
                except Exception as exc:
                    log.debug("Failed to send TYPING_STOP: %s", exc)

    async def _debounce_stop(self) -> None:
        """Send TYPING_STOP after TYPING_DEBOUNCE seconds of silence."""
        try:
            await asyncio.sleep(TYPING_DEBOUNCE)
            if self._we_are_typing:
                self._we_are_typing = False
                if self._send_stop:
                    await self._send_stop()
        except asyncio.CancelledError:
            pass

    # ── Inbound ───────────────────────────────────────────────────────────────

    def peer_started(self, alias: str) -> None:
        """Call when TYPING_START received from a peer."""
        was_typing = alias in self._peer_typing
        self._peer_typing[alias] = time.monotonic()

        # Cancel existing expiry task
        if alias in self._peer_expiry_tasks:
            task = self._peer_expiry_tasks[alias]
            if not task.done():
                task.cancel()

        # Schedule auto-expiry
        self._peer_expiry_tasks[alias] = asyncio.create_task(
            self._expire_peer(alias)
        )

        # Only fire callback on transition from not-typing to typing
        if not was_typing and self._on_peer_started:
            self._on_peer_started(alias)

    def peer_stopped(self, alias: str) -> None:
        """Call when TYPING_STOP received from a peer."""
        if alias not in self._peer_typing:
            return

        del self._peer_typing[alias]

        if alias in self._peer_expiry_tasks:
            task = self._peer_expiry_tasks.pop(alias)
            if not task.done():
                task.cancel()

        if self._on_peer_stopped:
            self._on_peer_stopped(alias)

    def peer_disconnected(self, alias: str) -> None:
        """Call when a peer leaves — clean up their typing state."""
        self.peer_stopped(alias)

    def who_is_typing(self) -> list:
        """Return list of aliases currently typing."""
        return list(self._peer_typing.keys())

    async def _expire_peer(self, alias: str) -> None:
        """Auto-stop a peer's typing indicator after TYPING_TIMEOUT."""
        try:
            from pymesh.utils.constants import TYPING_TIMEOUT
            await asyncio.sleep(TYPING_TIMEOUT)
            if alias in self._peer_typing:
                log.debug("Typing indicator for %s expired", alias)
                self.peer_stopped(alias)
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        """Cancel all background tasks."""
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        for task in self._peer_expiry_tasks.values():
            if not task.done():
                task.cancel()
        self._peer_expiry_tasks.clear()
        self._peer_typing.clear()
