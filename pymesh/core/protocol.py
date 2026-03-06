"""
PyMesh Chat — Wire Protocol
Handles length-prefixed framing so messages are reliably read from TCP streams.

Wire format:
  [4 bytes big-endian uint32 = payload length] [payload bytes]

All payloads are JSON-encoded dicts.

IMPORTANT: read_message() does NOT impose any timeout on waiting for the next
message. An idle connection legitimately sits here for minutes waiting for the
next incoming frame. Liveness is managed externally by the watchdog + PING/PONG
in peer.py. Putting a timeout here was the root cause of random disconnections.
"""

import asyncio
import json
import struct
import logging
from typing import Any

from pymesh.utils.constants import MAX_MESSAGE_SIZE

log = logging.getLogger(__name__)


class ProtocolError(Exception):
    """Raised when the wire protocol is violated (malformed data, oversized frame)."""


class ConnectionClosedError(Exception):
    """Raised when the remote peer closed the connection cleanly."""


class FrameReader:
    """
    Reads length-prefixed frames from an asyncio StreamReader.
    Each frame: [4-byte big-endian length][payload bytes].
    """

    def __init__(self, reader: asyncio.StreamReader):
        self._reader = reader

    async def read_message(self) -> dict:
        """
        Wait for and read the next complete message from the stream.

        Blocks indefinitely until a message arrives — this is intentional.
        The caller's watchdog loop handles disconnect-on-inactivity separately.

        Raises:
          ConnectionClosedError  — peer closed the connection cleanly
          ProtocolError          — malformed frame or oversized message
        """
        # ── Read the 4-byte length header ─────────────────────────────────────
        # No timeout here. Idle connections legitimately wait here for a long
        # time. A timeout here was the root cause of random disconnections.
        try:
            header = await self._reader.readexactly(4)
        except asyncio.IncompleteReadError:
            raise ConnectionClosedError("Peer closed the connection")
        except (ConnectionResetError, OSError) as exc:
            raise ConnectionClosedError(f"Connection lost: {exc}")

        (length,) = struct.unpack(">I", header)

        if length == 0:
            raise ProtocolError("Received zero-length frame")
        if length > MAX_MESSAGE_SIZE:
            raise ProtocolError(
                f"Frame too large: {length} bytes (max {MAX_MESSAGE_SIZE})"
            )

        # ── Read exactly `length` bytes of payload ────────────────────────────
        # A generous timeout only for the payload itself (peer sent the header
        # so they should send the body promptly). 120s is more than enough.
        try:
            payload = await asyncio.wait_for(
                self._reader.readexactly(length),
                timeout=120.0,
            )
        except asyncio.IncompleteReadError:
            raise ConnectionClosedError("Connection closed mid-payload")
        except asyncio.TimeoutError:
            raise ProtocolError("Timed out waiting for message payload after header")
        except (ConnectionResetError, OSError) as exc:
            raise ConnectionClosedError(f"Connection lost reading payload: {exc}")

        # ── Decode JSON ───────────────────────────────────────────────────────
        try:
            return json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProtocolError(f"Invalid JSON payload: {exc}")


class FrameWriter:
    """
    Writes length-prefixed frames to an asyncio StreamWriter.
    """

    def __init__(self, writer: asyncio.StreamWriter):
        self._writer = writer

    async def send_message(self, msg: dict) -> None:
        """
        Encode msg as JSON and write it as a length-prefixed frame.

        Raises:
          ProtocolError          — message too large or JSON encoding failed
          ConnectionClosedError  — connection was lost while sending
        """
        try:
            payload = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"Failed to encode message as JSON: {exc}")

        if len(payload) > MAX_MESSAGE_SIZE:
            raise ProtocolError(
                f"Message too large: {len(payload)} bytes (max {MAX_MESSAGE_SIZE})"
            )

        header = struct.pack(">I", len(payload))
        try:
            self._writer.write(header + payload)
            await self._writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            raise ConnectionClosedError(f"Connection lost while sending: {exc}")

    def close(self) -> None:
        """Signal that no more data will be written."""
        try:
            self._writer.close()
        except Exception:
            pass


def build_message(msg_type: str, **fields: Any) -> dict:
    """
    Construct a well-formed PyMesh wire message dict.
    Every message carries: type, version, and a millisecond UTC timestamp.
    """
    import time
    from pymesh.utils.constants import APP_PROTOCOL_VERSION

    msg = {
        "type": msg_type,
        "version": APP_PROTOCOL_VERSION,
        "ts": int(time.time() * 1000),
    }
    msg.update(fields)
    return msg
