"""
PyMesh Chat — Identity
Manages the local peer's persistent cryptographic identity.

Each user has one Ed25519 keypair that never changes. It is:
  - Generated once on first launch
  - Private key stored encrypted at ~/.pymesh/identity.key
  - Public key fingerprint shown on startup and shared during handshake

The private key file is encrypted with AES-256-GCM using a key derived
from the user's passphrase via PBKDF2-HMAC-SHA256.

File format (~/.pymesh/identity.key) — JSON:
{
    "version": 1,
    "salt":    "<hex>",   # 32-byte PBKDF2 salt
    "nonce":   "<hex>",   # 12-byte AES-GCM nonce
    "tag":     "<hex>",   # 16-byte AES-GCM auth tag
    "ct":      "<hex>"    # Ciphertext (encrypted Ed25519 private key seed)
}
"""

import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes, serialization

from pymesh.utils.constants import IDENTITY_FILE, PYMESH_DIR

log = logging.getLogger(__name__)

# PBKDF2 iterations — high enough to be brute-force resistant
PBKDF2_ITERATIONS = 310_000


class IdentityError(Exception):
    """Raised when identity operations fail."""


class Identity:
    """
    The local peer's cryptographic identity.

    Holds the Ed25519 keypair and exposes:
      - public_key_bytes  : raw 32-byte public key
      - fingerprint       : SHA-256 hex digest of public key (shown to users)
      - sign(data)        : sign arbitrary bytes with private key
      - verify(sig, data) : verify a signature against this identity's public key
    """

    def __init__(self, private_key: Ed25519PrivateKey):
        self._private_key = private_key
        self._public_key = private_key.public_key()
        self._pub_bytes = self._public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        # Fingerprint: SHA-256 of raw public key bytes, hex encoded
        import hashlib
        self.fingerprint = hashlib.sha256(self._pub_bytes).hexdigest()

    @property
    def public_key_bytes(self) -> bytes:
        """Raw 32-byte Ed25519 public key."""
        return self._pub_bytes

    @property
    def public_key_hex(self) -> str:
        return self._pub_bytes.hex()

    def sign(self, data: bytes) -> bytes:
        """Sign data with the private key. Returns 64-byte signature."""
        return self._private_key.sign(data)

    def verify(self, signature: bytes, data: bytes) -> bool:
        """Verify a signature made by THIS identity's private key."""
        try:
            self._public_key.verify(signature, data)
            return True
        except Exception:
            return False

    @staticmethod
    def verify_with_public_key(public_key_bytes: bytes, signature: bytes, data: bytes) -> bool:
        """Verify a signature using an arbitrary Ed25519 public key."""
        try:
            pub = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            pub.verify(signature, data)
            return True
        except Exception:
            return False


# ── Key generation ────────────────────────────────────────────────────────────

def generate_identity() -> Identity:
    """Generate a fresh Ed25519 keypair."""
    private_key = Ed25519PrivateKey.generate()
    return Identity(private_key)


# ── Key persistence ───────────────────────────────────────────────────────────

def save_identity(identity: Identity, passphrase: str) -> None:
    """
    Encrypt and save the identity to ~/.pymesh/identity.key.
    Creates the ~/.pymesh directory if it does not exist.
    """
    os.makedirs(PYMESH_DIR, mode=0o700, exist_ok=True)

    # Extract the raw 32-byte private key seed
    seed = identity._private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )

    # Derive AES key from passphrase
    salt = os.urandom(32)
    aes_key = _derive_key(passphrase, salt)

    # Encrypt seed with AES-256-GCM
    nonce = os.urandom(12)
    aesgcm = AESGCM(aes_key)
    ct_with_tag = aesgcm.encrypt(nonce, seed, None)
    # AESGCM appends the 16-byte tag to the ciphertext
    ct = ct_with_tag[:-16]
    tag = ct_with_tag[-16:]

    payload = {
        "version": 1,
        "salt":  salt.hex(),
        "nonce": nonce.hex(),
        "tag":   tag.hex(),
        "ct":    ct.hex(),
    }

    path = Path(IDENTITY_FILE)
    path.write_text(json.dumps(payload, indent=2))
    path.chmod(0o600)  # Owner read/write only

    log.info("Identity saved to %s", IDENTITY_FILE)


def load_identity(passphrase: str) -> Identity:
    """
    Load and decrypt the identity from ~/.pymesh/identity.key.
    Raises IdentityError if the file is missing, corrupted, or passphrase is wrong.
    """
    path = Path(IDENTITY_FILE)
    if not path.exists():
        raise IdentityError(f"Identity file not found: {IDENTITY_FILE}")

    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise IdentityError(f"Could not read identity file: {exc}")

    version = payload.get("version", 0)
    if version != 1:
        raise IdentityError(f"Unknown identity file version: {version}")

    try:
        salt  = bytes.fromhex(payload["salt"])
        nonce = bytes.fromhex(payload["nonce"])
        tag   = bytes.fromhex(payload["tag"])
        ct    = bytes.fromhex(payload["ct"])
    except (KeyError, ValueError) as exc:
        raise IdentityError(f"Malformed identity file: {exc}")

    aes_key = _derive_key(passphrase, salt)

    try:
        aesgcm = AESGCM(aes_key)
        seed = aesgcm.decrypt(nonce, ct + tag, None)
    except Exception:
        raise IdentityError("Wrong passphrase or corrupted identity file")

    try:
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
    except Exception as exc:
        raise IdentityError(f"Could not reconstruct keypair: {exc}")

    log.info("Identity loaded from %s", IDENTITY_FILE)
    return Identity(private_key)


def identity_exists() -> bool:
    """Return True if an identity file already exists."""
    return Path(IDENTITY_FILE).exists()


def get_or_create_identity(passphrase: str) -> Identity:
    """
    Load existing identity or generate and save a new one.
    This is the main entry point called on app startup.
    """
    if identity_exists():
        return load_identity(passphrase)
    else:
        log.info("No identity found — generating new Ed25519 keypair")
        identity = generate_identity()
        save_identity(identity, passphrase)
        return identity


# ── Internal ──────────────────────────────────────────────────────────────────

def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 32-byte AES key from a passphrase using PBKDF2-HMAC-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))
