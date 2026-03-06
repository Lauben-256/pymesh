"""
PyMesh Chat — Cryptographic Handshake (Phase 2)
Replaces the Phase 1 plain-text handshake with a fully authenticated,
encrypted key exchange.

Sequence (Initiator = I, Responder = R):

  Step 1  I → R   CRYPTO_HELLO
                  { alias, session, peer_id, ed25519_pub_hex, x25519_pub_hex }

  Step 2  R → I   CRYPTO_HELLO_ACK
                  { alias, session, peer_id, ed25519_pub_hex, x25519_pub_hex,
                    signature_hex }
                  signature = Ed25519_sign(ed25519_priv_R,
                              "pymesh-handshake:" + x25519_pub_I + x25519_pub_R)

  Step 3  I → R   CRYPTO_CONFIRM
                  { signature_hex }
                  signature = Ed25519_sign(ed25519_priv_I,
                              "pymesh-handshake:" + x25519_pub_I + x25519_pub_R)

  Step 4  Both sides:
          - Verify the other's Ed25519 signature
          - Compute X25519 shared secret
          - Derive AES-256 session key via HKDF
          - All subsequent messages encrypted with SessionCipher

Security properties:
  - Mutual authentication (both sides sign the key exchange)
  - Forward secrecy (fresh X25519 keys each session)
  - Binding (HKDF info includes both public keys)
  - TOFU (fingerprints checked against known_peers.json)
"""

import asyncio
import logging
import os

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives import serialization

from pymesh.core.protocol import FrameReader, FrameWriter, ProtocolError, build_message
from pymesh.core.peer import PeerInfo
from pymesh.crypto.identity import Identity
from pymesh.crypto.cipher import SessionCipher, derive_session_key
from pymesh.utils.constants import HANDSHAKE_TIMEOUT, APP_PROTOCOL_VERSION

log = logging.getLogger(__name__)

MSG_CRYPTO_HELLO   = "CRYPTO_HELLO"
MSG_CRYPTO_ACK     = "CRYPTO_HELLO_ACK"
MSG_CRYPTO_CONFIRM = "CRYPTO_CONFIRM"

SIGN_PREFIX = b"pymesh-handshake:"


class CryptoHandshakeError(Exception):
    """Raised when the cryptographic handshake fails."""


async def perform_crypto_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    is_initiator: bool,
    local_alias: str,
    local_session: str,
    identity: Identity,
) -> tuple:
    """
    Execute the Phase 2 cryptographic handshake.

    Returns: (PeerInfo, SessionCipher)
      - PeerInfo  : metadata about the remote peer
      - SessionCipher : ready-to-use cipher for this connection

    Raises CryptoHandshakeError on any failure.
    """
    framer_r = FrameReader(reader)
    framer_w = FrameWriter(writer)

    try:
        return await asyncio.wait_for(
            _do_crypto_handshake(
                framer_r, framer_w, is_initiator,
                local_alias, local_session, identity,
            ),
            timeout=HANDSHAKE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise CryptoHandshakeError(
            f"Cryptographic handshake timed out after {HANDSHAKE_TIMEOUT}s"
        )
    except CryptoHandshakeError:
        raise
    except Exception as exc:
        raise CryptoHandshakeError(f"Handshake error: {exc}")


async def _do_crypto_handshake(
    framer_r: FrameReader,
    framer_w: FrameWriter,
    is_initiator: bool,
    local_alias: str,
    local_session: str,
    identity: Identity,
) -> tuple:
    """Internal handshake implementation."""
    import uuid

    # Generate a fresh X25519 keypair for this session
    x25519_priv = X25519PrivateKey.generate()
    x25519_pub  = x25519_priv.public_key()
    x25519_pub_bytes = x25519_pub.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    local_peer_id = str(uuid.uuid4())

    if is_initiator:
        # ── Step 1: Send CRYPTO_HELLO ─────────────────────────────────────────
        hello = build_message(
            MSG_CRYPTO_HELLO,
            alias=local_alias,
            session=local_session,
            peer_id=local_peer_id,
            protocol_version=APP_PROTOCOL_VERSION,
            ed25519_pub=identity.public_key_hex,
            x25519_pub=x25519_pub_bytes.hex(),
        )
        await framer_w.send_message(hello)
        log.debug("Crypto handshake: sent CRYPTO_HELLO")

        # ── Step 2: Receive CRYPTO_HELLO_ACK ─────────────────────────────────
        ack = await framer_r.read_message()
        if ack.get("type") != MSG_CRYPTO_ACK:
            raise CryptoHandshakeError(
                f"Expected {MSG_CRYPTO_ACK}, got {ack.get('type')}"
            )

        peer_ed25519_pub_bytes = _parse_peer_hello(ack, local_session)
        peer_x25519_pub_bytes  = bytes.fromhex(ack["x25519_pub"])
        peer_signature         = bytes.fromhex(ack["signature"])
        peer_alias             = ack["alias"]
        peer_id                = ack["peer_id"]

        # Verify responder's signature
        sign_data = SIGN_PREFIX + x25519_pub_bytes + peer_x25519_pub_bytes
        if not Identity.verify_with_public_key(
            peer_ed25519_pub_bytes, peer_signature, sign_data
        ):
            raise CryptoHandshakeError(
                f"Responder signature verification failed for '{peer_alias}'"
            )
        log.debug("Crypto handshake: responder signature verified")

        # ── Step 3: Send CRYPTO_CONFIRM ───────────────────────────────────────
        our_signature = identity.sign(sign_data)
        confirm = build_message(
            MSG_CRYPTO_CONFIRM,
            signature=our_signature.hex(),
        )
        await framer_w.send_message(confirm)
        log.debug("Crypto handshake: sent CRYPTO_CONFIRM")

        # Derive session key (initiator pub first)
        shared_secret = x25519_priv.exchange(
            _x25519_pub_from_bytes(peer_x25519_pub_bytes)
        )
        session_key = derive_session_key(
            shared_secret, x25519_pub_bytes, peer_x25519_pub_bytes
        )

    else:
        # ── Step 1: Receive CRYPTO_HELLO ──────────────────────────────────────
        hello = await framer_r.read_message()
        if hello.get("type") != MSG_CRYPTO_HELLO:
            raise CryptoHandshakeError(
                f"Expected {MSG_CRYPTO_HELLO}, got {hello.get('type')}"
            )

        peer_ed25519_pub_bytes = _parse_peer_hello(hello, local_session)
        peer_x25519_pub_bytes  = bytes.fromhex(hello["x25519_pub"])
        peer_alias             = hello["alias"]
        peer_id                = hello["peer_id"]
        log.debug("Crypto handshake: received CRYPTO_HELLO from %s", peer_alias)

        # ── Step 2: Send CRYPTO_HELLO_ACK ─────────────────────────────────────
        sign_data    = SIGN_PREFIX + peer_x25519_pub_bytes + x25519_pub_bytes
        our_signature = identity.sign(sign_data)

        ack = build_message(
            MSG_CRYPTO_ACK,
            alias=local_alias,
            session=local_session,
            peer_id=local_peer_id,
            protocol_version=APP_PROTOCOL_VERSION,
            ed25519_pub=identity.public_key_hex,
            x25519_pub=x25519_pub_bytes.hex(),
            signature=our_signature.hex(),
        )
        await framer_w.send_message(ack)
        log.debug("Crypto handshake: sent CRYPTO_HELLO_ACK")

        # ── Step 3: Receive CRYPTO_CONFIRM ────────────────────────────────────
        confirm = await framer_r.read_message()
        if confirm.get("type") != MSG_CRYPTO_CONFIRM:
            raise CryptoHandshakeError(
                f"Expected {MSG_CRYPTO_CONFIRM}, got {confirm.get('type')}"
            )

        peer_signature = bytes.fromhex(confirm["signature"])
        if not Identity.verify_with_public_key(
            peer_ed25519_pub_bytes, peer_signature, sign_data
        ):
            raise CryptoHandshakeError(
                f"Initiator signature verification failed for '{peer_alias}'"
            )
        log.debug("Crypto handshake: initiator signature verified")

        # Derive session key (initiator pub first)
        shared_secret = x25519_priv.exchange(
            _x25519_pub_from_bytes(peer_x25519_pub_bytes)
        )
        session_key = derive_session_key(
            shared_secret, peer_x25519_pub_bytes, x25519_pub_bytes
        )

    # ── Both sides: build PeerInfo and SessionCipher ──────────────────────────
    import hashlib
    peer_fingerprint = hashlib.sha256(peer_ed25519_pub_bytes).hexdigest()

    peer_info = PeerInfo(
        alias=peer_alias,
        fingerprint=peer_fingerprint,
        session_name=local_session,
        address="",   # filled in by Node
        port=0,       # filled in by Node
        peer_id=peer_id,
    )

    cipher = SessionCipher(session_key)

    log.info(
        "Crypto handshake complete with %s (fp: %s...)",
        peer_alias, peer_fingerprint[:16]
    )
    return peer_info, cipher


def _parse_peer_hello(msg: dict, local_session: str) -> bytes:
    """
    Validate a HELLO or HELLO_ACK message and return the peer's Ed25519 public key bytes.
    Raises CryptoHandshakeError on any problem.
    """
    # Session check
    their_session = msg.get("session", "").strip()
    if their_session.lower() != local_session.lower():
        raise CryptoHandshakeError(
            f"Session mismatch: ours='{local_session}' theirs='{their_session}'"
        )

    # Protocol version
    their_version = msg.get("protocol_version", 0)
    if their_version != APP_PROTOCOL_VERSION:
        raise CryptoHandshakeError(
            f"Protocol version mismatch: ours=v{APP_PROTOCOL_VERSION} "
            f"theirs=v{their_version}"
        )

    # Required fields
    for field in ("alias", "peer_id", "ed25519_pub", "x25519_pub"):
        if not msg.get(field):
            raise CryptoHandshakeError(f"Missing required field: {field}")

    try:
        ed25519_pub_bytes = bytes.fromhex(msg["ed25519_pub"])
        x25519_pub_bytes  = bytes.fromhex(msg["x25519_pub"])
    except ValueError as exc:
        raise CryptoHandshakeError(f"Invalid hex in handshake message: {exc}")

    if len(ed25519_pub_bytes) != 32:
        raise CryptoHandshakeError(
            f"Ed25519 public key must be 32 bytes, got {len(ed25519_pub_bytes)}"
        )
    if len(x25519_pub_bytes) != 32:
        raise CryptoHandshakeError(
            f"X25519 public key must be 32 bytes, got {len(x25519_pub_bytes)}"
        )

    return ed25519_pub_bytes


def _x25519_pub_from_bytes(raw: bytes):
    """Reconstruct an X25519PublicKey from raw bytes."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    return X25519PublicKey.from_public_bytes(raw)
