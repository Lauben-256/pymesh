"""
PyMesh Chat — Peer Connection (Phase 2)
Same structure as Phase 1 but every message is now encrypted/decrypted
transparently using the SessionCipher established during the handshake.

Encryption is invisible to callers — send() accepts plain dicts,
the writer encrypts before sending. The reader decrypts before dispatch.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Callable

from pymesh.core.protocol import (
    FrameReader, FrameWriter,
    ProtocolError, ConnectionClosedError,
    build_message,
)
from pymesh.crypto.cipher import SessionCipher, CipherError
from pymesh.utils.constants import (
    DEFAULT_INACTIVITY_TIMEOUT,
    INACTIVITY_WARN_BEFORE,
    MSG_PING, MSG_PONG, MSG_DISCONNECT,
)

log = logging.getLogger(__name__)


class PeerState(Enum):
    CONNECTING    = auto()
    ACTIVE        = auto()
    DISCONNECTING = auto()
    CLOSED        = auto()


@dataclass
class PeerInfo:
    alias: str
    fingerprint: str
    session_name: str
    address: str
    port: int
    peer_id: str


class PeerConnection:
    """
    Owns one encrypted TCP connection to one remote peer.
    All messages are AES-256-GCM encrypted on the wire.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        is_initiator: bool,
        cipher: SessionCipher,
        inactivity_timeout: int = DEFAULT_INACTIVITY_TIMEOUT,
        on_message: Optional[Callable] = None,
        on_disconnect: Optional[Callable] = None,
        on_warn_timeout: Optional[Callable] = None,
    ):
        self._reader = reader
        self._writer = writer
        self.is_initiator = is_initiator
        self._cipher = cipher          # AES-256-GCM session cipher

        self.framer_r = FrameReader(reader)
        self.framer_w = FrameWriter(writer)

        self.state = PeerState.CONNECTING
        self.info: Optional[PeerInfo] = None

        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._on_warn_timeout = on_warn_timeout

        self._send_queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._inactivity_timeout = inactivity_timeout
        self._last_activity: Optional[float] = None
        self._warned_timeout = False
        self._tasks: list = []

        try:
            peername = writer.get_extra_info("peername")
            self.remote_addr = f"{peername[0]}:{peername[1]}" if peername else "unknown"
        except Exception:
            self.remote_addr = "unknown"

    # ── Public API ────────────────────────────────────────────────────────────

    def activate(self) -> None:
        """Start inactivity clock and mark ACTIVE. Call after handshake."""
        self._last_activity = time.monotonic()
        self._warned_timeout = False
        self.state = PeerState.ACTIVE
        log.debug("Connection to %s activated (encrypted)", self.remote_addr)

    async def start(self) -> None:
        """Spawn reader, writer, and watchdog coroutines."""
        if self.state != PeerState.ACTIVE:
            raise RuntimeError("activate() must be called before start()")
        self._tasks = [
            asyncio.create_task(self._reader_loop(),   name=f"reader-{self.remote_addr}"),
            asyncio.create_task(self._writer_loop(),   name=f"writer-{self.remote_addr}"),
            asyncio.create_task(self._watchdog_loop(), name=f"watchdog-{self.remote_addr}"),
        ]

    async def send(self, msg: dict) -> None:
        """Queue a message for encrypted delivery."""
        if self.state in (PeerState.CLOSED, PeerState.DISCONNECTING):
            return
        try:
            self._send_queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("Send queue full for %s — dropping", self.remote_addr)

    async def disconnect(self, reason: str = "user request") -> None:
        """Gracefully close: send encrypted DISCONNECT then tear down."""
        if self.state in (PeerState.CLOSED, PeerState.DISCONNECTING):
            return
        self.state = PeerState.DISCONNECTING
        try:
            goodbye = build_message(MSG_DISCONNECT, reason=reason)
            await asyncio.wait_for(self._send_encrypted(goodbye), timeout=3.0)
        except Exception:
            pass
        await self._close()

    def record_activity(self) -> None:
        self._last_activity = time.monotonic()
        self._warned_timeout = False

    @property
    def seconds_idle(self) -> float:
        if self._last_activity is None:
            return 0.0
        return time.monotonic() - self._last_activity

    # ── Internal: encrypted send/receive ──────────────────────────────────────

    async def _send_encrypted(self, msg: dict) -> None:
        """Encrypt a message dict and send it as a framed payload."""
        plaintext = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        try:
            ciphertext = self._cipher.encrypt(plaintext)
        except CipherError as exc:
            raise ProtocolError(f"Encryption failed: {exc}")
        # Wrap ciphertext in a framed envelope so FrameReader can read it
        await self.framer_w.send_message({"enc": ciphertext.hex()})

    async def _recv_decrypted(self) -> dict:
        """Read a framed payload, decrypt it, and return the message dict."""
        envelope = await self.framer_r.read_message()
        enc_hex = envelope.get("enc")
        if not enc_hex:
            raise ProtocolError("Received unencrypted message on encrypted channel")
        try:
            ciphertext = bytes.fromhex(enc_hex)
            plaintext  = self._cipher.decrypt(ciphertext)
            return json.loads(plaintext.decode("utf-8"))
        except CipherError as exc:
            raise ProtocolError(f"Decryption failed: {exc}")
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"Invalid decrypted payload: {exc}")

    # ── Internal: reader ─────────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        try:
            while self.state == PeerState.ACTIVE:
                msg = await self._recv_decrypted()
                self.record_activity()
                await self._dispatch(msg)
        except ConnectionClosedError as exc:
            log.info("Connection closed by %s: %s", self.remote_addr, exc)
            await self._close()
        except ProtocolError as exc:
            log.warning("Protocol error from %s: %s", self.remote_addr, exc)
            await self._close()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("Unexpected reader error for %s: %s", self.remote_addr, exc)
            await self._close()

    # ── Internal: writer ─────────────────────────────────────────────────────

    async def _writer_loop(self) -> None:
        try:
            while self.state in (PeerState.ACTIVE, PeerState.DISCONNECTING):
                try:
                    msg = await asyncio.wait_for(
                        self._send_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                if self.state == PeerState.CLOSED:
                    break
                try:
                    await self._send_encrypted(msg)
                    self.record_activity()
                except ConnectionClosedError as exc:
                    log.info("Send failed for %s: %s", self.remote_addr, exc)
                    await self._close()
                    return
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("Unexpected writer error for %s: %s", self.remote_addr, exc)
            await self._close()

    # ── Internal: watchdog ───────────────────────────────────────────────────

    async def _watchdog_loop(self) -> None:
        PING_INTERVAL = max(20, min(60, self._inactivity_timeout // 3))
        last_ping = time.monotonic()

        try:
            while self.state == PeerState.ACTIVE:
                await asyncio.sleep(5)
                if self.state != PeerState.ACTIVE:
                    break

                idle      = self.seconds_idle
                remaining = self._inactivity_timeout - idle

                if remaining <= INACTIVITY_WARN_BEFORE and not self._warned_timeout:
                    self._warned_timeout = True
                    if self._on_warn_timeout:
                        try:
                            await self._on_warn_timeout(self, max(0, int(remaining)))
                        except Exception:
                            pass

                if idle >= self._inactivity_timeout:
                    log.info("Disconnecting %s after %ds idle", self.remote_addr, int(idle))
                    await self.disconnect(reason="inactivity timeout")
                    return

                if time.monotonic() - last_ping >= PING_INTERVAL:
                    await self.send(build_message(MSG_PING))
                    last_ping = time.monotonic()

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.exception("Watchdog error for %s: %s", self.remote_addr, exc)

    # ── Internal: dispatch ───────────────────────────────────────────────────

    async def _dispatch(self, msg: dict) -> None:
        msg_type = msg.get("type", "")

        if msg_type == MSG_PING:
            await self.send(build_message(MSG_PONG))
            return
        if msg_type == MSG_PONG:
            return
        if msg_type == MSG_DISCONNECT:
            log.info("Peer %s sent DISCONNECT: %s", self.remote_addr, msg.get("reason"))
            await self._close()
            return

        if self._on_message:
            try:
                await self._on_message(self, msg)
            except Exception as exc:
                log.exception("Error in on_message callback: %s", exc)

    # ── Internal: teardown ───────────────────────────────────────────────────

    async def _close(self) -> None:
        if self.state == PeerState.CLOSED:
            return
        self.state = PeerState.CLOSED

        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()

        self.framer_w.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), timeout=3.0)
        except Exception:
            pass

        log.info("Encrypted connection to %s closed", self.remote_addr)

        if self._on_disconnect:
            try:
                await self._on_disconnect(self)
            except Exception:
                log.exception("Error in disconnect callback")
