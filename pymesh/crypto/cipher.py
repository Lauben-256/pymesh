"""
PyMesh Chat — Cipher
AES-256-GCM encryption and decryption for all messages and payloads.

Every message transmitted after the handshake is encrypted with a
per-session AES-256-GCM key. Each encryption call uses a unique random
96-bit nonce — reusing nonces with GCM would be catastrophic, so we
generate a fresh one for every single message.

Wire format for an encrypted payload:
  [12 bytes nonce][ciphertext + 16 byte GCM auth tag]

The session key is established during the cryptographic handshake
(X25519 ECDH → HKDF → 32-byte AES key) and never transmitted.
"""

import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

log = logging.getLogger(__name__)

NONCE_SIZE = 12   # 96-bit nonce for AES-GCM
KEY_SIZE   = 32   # 256-bit AES key


class CipherError(Exception):
    """Raised when encryption or decryption fails."""


class SessionCipher:
    """
    Encrypts and decrypts messages using a fixed session key.

    One SessionCipher is created per peer connection after the
    cryptographic handshake establishes the shared session key.
    """

    def __init__(self, session_key: bytes):
        if len(session_key) != KEY_SIZE:
            raise CipherError(
                f"Session key must be {KEY_SIZE} bytes, got {len(session_key)}"
            )
        self._aesgcm = AESGCM(session_key)

    def encrypt(self, plaintext: bytes) -> bytes:
        """
        Encrypt plaintext bytes.
        Returns: nonce (12 bytes) + ciphertext + GCM tag (16 bytes)
        """
        nonce = os.urandom(NONCE_SIZE)
        try:
            ct = self._aesgcm.encrypt(nonce, plaintext, None)
        except Exception as exc:
            raise CipherError(f"Encryption failed: {exc}")
        return nonce + ct

    def decrypt(self, ciphertext: bytes) -> bytes:
        """
        Decrypt a payload produced by encrypt().
        Raises CipherError if authentication fails (tampered data).
        """
        if len(ciphertext) < NONCE_SIZE + 16:
            raise CipherError(
                f"Ciphertext too short: {len(ciphertext)} bytes "
                f"(minimum {NONCE_SIZE + 16})"
            )
        nonce = ciphertext[:NONCE_SIZE]
        ct    = ciphertext[NONCE_SIZE:]
        try:
            return self._aesgcm.decrypt(nonce, ct, None)
        except Exception:
            raise CipherError(
                "Decryption failed — message may be tampered or key is wrong"
            )


# ── Key derivation ────────────────────────────────────────────────────────────

def derive_session_key(
    shared_secret: bytes,
    initiator_pub: bytes,
    responder_pub: bytes,
) -> bytes:
    """
    Derive a 32-byte AES-256 session key from an X25519 shared secret
    using HKDF-SHA256.

    Both sides must pass the public keys in the same order
    (initiator first, responder second) to derive the identical key.
    The public keys are used as HKDF info to bind the key to this
    specific connection — prevents key reuse across different sessions.
    """
    info = b"pymesh-session-v1:" + initiator_pub + b":" + responder_pub
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=None,
        info=info,
    )
    return hkdf.derive(shared_secret)
