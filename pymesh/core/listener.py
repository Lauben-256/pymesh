"""
PyMesh Chat — Listener
Opens a TCP server socket and accepts inbound connections from other peers.

On each new connection, a PeerConnection is instantiated and the handshake
is kicked off. Successfully handshaked connections are handed off to the Node.
"""

import asyncio
import logging
from typing import Callable, Awaitable, Optional

from pymesh.utils.constants import DEFAULT_PORT, HANDSHAKE_TIMEOUT

log = logging.getLogger(__name__)


class Listener:
    """
    Wraps asyncio.start_server and manages the lifecycle of the TCP server.

    The `on_new_connection` callback is called for every accepted connection
    BEFORE handshaking — the caller (Node) handles the handshake.
    """

    def __init__(
        self,
        on_new_connection: Callable,  # async (reader, writer, is_initiator=False) -> None
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
    ):
        self._on_new_connection = on_new_connection
        self._host = host
        self._port = port
        self._server: Optional[asyncio.Server] = None

    async def start(self) -> int:
        """
        Start the TCP listener.
        Returns the actual port bound (useful if port=0 was requested).
        """
        import platform
        # reuse_address=True has correct SO_REUSEADDR semantics on Linux/Mac
        # but on Windows it allows port hijacking, so we skip it there.
        server_kwargs = {}
        if platform.system() != "Windows":
            server_kwargs["reuse_address"] = True

        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._host,
            port=self._port,
            **server_kwargs,
        )

        bound_port = self._server.sockets[0].getsockname()[1]
        log.info("Listener started on %s:%d", self._host, bound_port)
        self._port = bound_port
        return bound_port

    async def stop(self) -> None:
        """Gracefully stop accepting new connections."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("Listener stopped")

    @property
    def port(self) -> int:
        return self._port

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Called by asyncio for each accepted connection."""
        peername = writer.get_extra_info("peername")
        addr = f"{peername[0]}:{peername[1]}" if peername else "unknown"
        log.info("Inbound connection from %s", addr)

        try:
            # Delegate to Node — is_initiator=False because THEY connected to US
            await self._on_new_connection(reader, writer, is_initiator=False)
        except Exception as exc:
            log.exception("Error handling inbound connection from %s: %s", addr, exc)
            try:
                writer.close()
            except Exception:
                pass
