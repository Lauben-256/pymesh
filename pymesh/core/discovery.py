"""
PyMesh Chat — mDNS Discovery
Advertises this node on the LAN and discovers other PyMesh peers using
Zeroconf/mDNS (the same technology as Apple Bonjour).

This module is OPTIONAL at import time — if the `zeroconf` package is not
installed, discovery is disabled and the user must connect manually.
All imports are deferred so the rest of the app works without zeroconf.
"""

import asyncio
import logging
import socket
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── Availability check ────────────────────────────────────────────────────────
try:
    from zeroconf import ServiceInfo, Zeroconf, ServiceBrowser, ServiceListener
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    log.warning(
        "zeroconf not installed — mDNS peer discovery disabled. "
        "Install with: pip install zeroconf"
    )


def get_local_ip() -> str:
    """
    Best-effort: find the primary LAN IP of this machine.
    Falls back to 127.0.0.1 if detection fails.
    """
    try:
        # Trick: connect to a public address to determine outgoing interface.
        # No packets are actually sent (UDP connect doesn't transmit).
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


class DiscoveryService:
    """
    Wraps Zeroconf to:
      1. Advertise this node as a _pymesh._tcp.local. service
      2. Discover other _pymesh._tcp.local. services on the LAN
      3. Fire callbacks when peers appear or disappear
    """

    SERVICE_TYPE = "_pymesh._tcp.local."

    def __init__(
        self,
        alias: str,
        session_name: str,
        fingerprint: str,
        port: int,
        on_peer_found: Callable,     # async (host: str, port: int, info: dict) -> None
        on_peer_lost: Callable,      # async (fingerprint: str) -> None
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._alias = alias
        self._session_name = session_name
        self._fingerprint = fingerprint
        self._port = port
        self._on_peer_found = on_peer_found
        self._on_peer_lost = on_peer_lost
        self._loop = loop  # resolved in start() if None

        self._zeroconf: Optional[object] = None
        self._service_info: Optional[object] = None
        self._browser: Optional[object] = None
        self._own_name: str = ""

        self._available = ZEROCONF_AVAILABLE

    async def start(self) -> bool:
        """
        Start advertising and browsing.
        Returns True if Zeroconf is available and started, False otherwise.
        """
        if not self._available:
            log.info("mDNS discovery not available (zeroconf not installed)")
            return False

        # Resolve the event loop now that we are inside an async context
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        local_ip = get_local_ip()
        log.info("Local IP detected as %s", local_ip)

        # Build a unique service name: "alias (fingerprint[:8])._pymesh._tcp.local."
        short_fp = self._fingerprint[:8] if self._fingerprint else "nofp"
        self._own_name = f"{self._alias}-{short_fp}.{self.SERVICE_TYPE}"

        # Properties broadcast alongside the mDNS advertisement
        properties = {
            b"alias": self._alias.encode(),
            b"session": self._session_name.encode(),
            b"fp": self._fingerprint.encode(),
            b"ver": b"1",
        }

        self._service_info = ServiceInfo(
            type_=self.SERVICE_TYPE,
            name=self._own_name,
            addresses=[socket.inet_aton(local_ip)],
            port=self._port,
            properties=properties,
        )

        # Run Zeroconf in a thread (it uses blocking I/O internally)
        self._zeroconf = Zeroconf()

        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self._zeroconf.register_service, self._service_info
            )
            log.info("mDNS service registered: %s on port %d", self._own_name, self._port)
        except Exception as exc:
            log.error("Failed to register mDNS service: %s", exc)
            return False

        # Start browsing for peers
        listener = _PymeshServiceListener(
            own_name=self._own_name,
            on_peer_found=self._on_peer_found,
            on_peer_lost=self._on_peer_lost,
            loop=self._loop,
        )
        self._browser = ServiceBrowser(self._zeroconf, self.SERVICE_TYPE, listener)
        log.info("mDNS browser started for service type: %s", self.SERVICE_TYPE)
        return True

    async def stop(self) -> None:
        """Unregister and shut down Zeroconf."""
        if self._zeroconf is None:
            return
        try:
            if self._service_info:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._zeroconf.unregister_service, self._service_info
                )
            await asyncio.get_running_loop().run_in_executor(
                None, self._zeroconf.close
            )
            log.info("mDNS service unregistered and Zeroconf closed")
        except Exception as exc:
            log.warning("Error during mDNS shutdown: %s", exc)
        finally:
            self._zeroconf = None


class _PymeshServiceListener:
    """
    Internal Zeroconf ServiceListener.
    Fires async callbacks on the event loop when peers appear or disappear.
    """

    def __init__(
        self,
        own_name: str,
        on_peer_found: Callable,
        on_peer_lost: Callable,
        loop: asyncio.AbstractEventLoop,
    ):
        self._own_name = own_name
        self._on_peer_found = on_peer_found
        self._on_peer_lost = on_peer_lost
        self._loop = loop

    def add_service(self, zc, type_: str, name: str) -> None:
        """Called by Zeroconf when a new service is discovered."""
        if name == self._own_name:
            return  # Ignore our own advertisement

        info = zc.get_service_info(type_, name)
        if not info:
            return

        host = socket.inet_ntoa(info.addresses[0]) if info.addresses else None
        if not host:
            return

        props = {
            k.decode(): v.decode() if isinstance(v, bytes) else v
            for k, v in (info.properties or {}).items()
        }

        peer_info = {
            "alias": props.get("alias", "unknown"),
            "session": props.get("session", ""),
            "fingerprint": props.get("fp", ""),
        }

        log.info(
            "Discovered peer: %s @ %s:%d (session=%s)",
            peer_info["alias"], host, info.port, peer_info["session"]
        )

        asyncio.run_coroutine_threadsafe(
            self._on_peer_found(host, info.port, peer_info),
            self._loop,
        )

    def remove_service(self, zc, type_: str, name: str) -> None:
        """Called by Zeroconf when a service disappears."""
        if name == self._own_name:
            return

        log.info("Peer left mDNS: %s", name)
        # Extract fingerprint from service name (alias-fp8.SERVICE_TYPE)
        fingerprint_hint = ""
        try:
            fingerprint_hint = name.split("-")[-1].split(".")[0]
        except Exception:
            pass

        asyncio.run_coroutine_threadsafe(
            self._on_peer_lost(fingerprint_hint),
            self._loop,
        )

    def update_service(self, zc, type_: str, name: str) -> None:
        """Called when a service record is updated — treat as re-add."""
        self.add_service(zc, type_, name)
