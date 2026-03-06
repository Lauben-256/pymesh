"""
PyMesh Chat — Connector
Initiates outbound TCP connections to remote peers, either discovered via
mDNS or specified manually by the user.
"""

import asyncio
import logging
from typing import Callable, Optional

from pymesh.utils.constants import DEFAULT_PORT, CONNECTION_TIMEOUT

log = logging.getLogger(__name__)


class Connector:
    """
    Creates outbound TCP connections to remote peers.

    The `on_new_connection` callback matches the Listener's signature so the
    Node can handle both inbound and outbound connections uniformly.
    """

    def __init__(self, on_new_connection: Callable):
        # async (reader, writer, is_initiator=True) -> None
        self._on_new_connection = on_new_connection

    async def connect(self, host: str, port: int = DEFAULT_PORT) -> bool:
        """
        Attempt a TCP connection to (host, port).

        Returns True on success, False on failure.
        The on_new_connection callback handles the rest asynchronously.
        """
        log.info("Connecting to %s:%d …", host, port)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=CONNECTION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("Connection to %s:%d timed out", host, port)
            return False
        except OSError as exc:
            log.warning("Could not connect to %s:%d — %s", host, port, exc)
            return False

        log.info("Connected to %s:%d", host, port)

        try:
            # is_initiator=True because WE opened the connection
            asyncio.create_task(
                self._on_new_connection(reader, writer, is_initiator=True),
                name=f"conn-{host}:{port}",
            )
        except Exception as exc:
            log.exception("Error handing off connection to %s:%d: %s", host, port, exc)
            try:
                writer.close()
            except Exception:
                pass
            return False

        return True
