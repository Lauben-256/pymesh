"""
PyMesh Chat — Trust Store (TOFU)
Trust On First Use peer authentication.

First time a peer connects:
  - Their public key fingerprint is displayed
  - User is prompted to accept or reject
  - If accepted, fingerprint saved to ~/.pymesh/known_peers.json

Subsequent connections from the same peer:
  - Fingerprint checked silently against known_peers.json
  - If it matches — trusted, proceed normally
  - If it changed — LOUD WARNING, connection blocked

known_peers.json format:
{
    "<fingerprint_hex>": {
        "alias":        "alice",
        "first_seen":   1706123456,
        "last_seen":    1706123456,
        "public_key":   "<hex>"
    }
}
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from pymesh.utils.constants import KNOWN_PEERS_FILE, PYMESH_DIR

log = logging.getLogger(__name__)


class TrustError(Exception):
    """Raised when a peer fails trust verification."""


class KeyChangedError(TrustError):
    """
    Raised when a known peer reconnects with a different public key.
    This is a serious security warning — possible MITM attack.
    """
    def __init__(self, alias: str, known_fp: str, new_fp: str):
        self.alias = alias
        self.known_fp = known_fp
        self.new_fp = new_fp
        super().__init__(
            f"Key change detected for '{alias}'! "
            f"Known: {known_fp[:16]}... New: {new_fp[:16]}... "
            f"Possible man-in-the-middle attack."
        )


class TrustStore:
    """
    Manages the known_peers registry on disk.
    Thread-safe for asyncio (single-threaded reads/writes).
    """

    def __init__(self, path: str = None):
        if path is None:
            path = KNOWN_PEERS_FILE
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self._path = Path(path)
        self._peers: dict = self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def is_known(self, fingerprint: str) -> bool:
        """Return True if this fingerprint has been trusted before."""
        return fingerprint in self._peers

    def check(self, alias: str, fingerprint: str, public_key_hex: str) -> str:
        """
        Check a connecting peer against the trust store.

        Returns one of:
          "new"     — never seen before, needs TOFU prompt
          "trusted" — known fingerprint, all good

        Raises:
          KeyChangedError — known alias but different fingerprint (security alert)
        """
        if fingerprint in self._peers:
            # Known fingerprint — update last_seen and proceed
            self._peers[fingerprint]["last_seen"] = int(time.time())
            if alias != self._peers[fingerprint].get("alias"):
                # Alias changed — update it (not a security concern)
                self._peers[fingerprint]["alias"] = alias
            self._save()
            return "trusted"

        # Check if this alias was seen before with a DIFFERENT fingerprint
        for known_fp, info in self._peers.items():
            if info.get("alias", "").lower() == alias.lower():
                raise KeyChangedError(
                    alias=alias,
                    known_fp=known_fp,
                    new_fp=fingerprint,
                )

        # Brand new peer
        return "new"

    def trust(self, alias: str, fingerprint: str, public_key_hex: str) -> None:
        """
        Add a peer to the trust store.
        Called after the user accepts a TOFU prompt.
        """
        now = int(time.time())
        self._peers[fingerprint] = {
            "alias":      alias,
            "first_seen": now,
            "last_seen":  now,
            "public_key": public_key_hex,
        }
        self._save()
        log.info("Trusted new peer: %s (fp: %s...)", alias, fingerprint[:16])

    def get_public_key(self, fingerprint: str) -> Optional[str]:
        """Return the stored public key hex for a known fingerprint, or None."""
        entry = self._peers.get(fingerprint)
        return entry.get("public_key") if entry else None

    def remove(self, fingerprint: str) -> bool:
        """Remove a peer from the trust store. Returns True if it existed."""
        if fingerprint in self._peers:
            del self._peers[fingerprint]
            self._save()
            return True
        return False

    def all_peers(self) -> list:
        """Return a list of all trusted peers as dicts."""
        return [
            {"fingerprint": fp, **info}
            for fp, info in self._peers.items()
        ]

    # ── Internal ─────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not load known_peers.json: %s — starting fresh", exc)
        return {}

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._peers, indent=2))
            self._path.chmod(0o600)
        except OSError as exc:
            log.error("Could not save known_peers.json: %s", exc)
