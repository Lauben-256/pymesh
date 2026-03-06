"""
PyMesh Chat — Handshake (Phase 2)
Thin wrapper that delegates to the cryptographic handshake in pymesh/crypto/.

Phase 1 plain-text handshake is replaced entirely. Every connection now:
  1. Exchanges Ed25519 public keys
  2. Performs X25519 ECDH key exchange
  3. Mutually authenticates via Ed25519 signatures
  4. Derives a unique AES-256-GCM session key via HKDF

Returns both a PeerInfo and a SessionCipher ready for immediate use.
"""

import asyncio
import logging

from pymesh.core.peer import PeerInfo
from pymesh.crypto.handshake import (
    perform_crypto_handshake,
    CryptoHandshakeError,
)
from pymesh.crypto.identity import Identity

log = logging.getLogger(__name__)

# Re-export so node.py only needs to import from core.handshake
HandshakeError = CryptoHandshakeError


async def perform_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    is_initiator: bool,
    local_alias: str,
    local_fingerprint: str,   # kept for API compatibility, derived from identity
    local_session: str,
    identity: Identity = None,
) -> tuple:
    """
    Execute the handshake on an established TCP connection.

    Returns (PeerInfo, SessionCipher).
    Raises HandshakeError on failure.

    identity must be provided — Phase 1 placeholder fingerprints are gone.
    """
    if identity is None:
        raise HandshakeError(
            "Identity is required for Phase 2 handshake. "
            "Ensure the Node is started with a valid Identity."
        )

    return await perform_crypto_handshake(
        reader=reader,
        writer=writer,
        is_initiator=is_initiator,
        local_alias=local_alias,
        local_session=local_session,
        identity=identity,
    )
