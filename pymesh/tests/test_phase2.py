"""
PyMesh Chat — Phase 2 Tests
All tests that touch the filesystem use temporary directories
so the real ~/.pymesh/ is never polluted.
"""

import asyncio
import os
import sys
import json
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pymesh.utils.constants as C
from pymesh.crypto.identity import (
    Identity, generate_identity, save_identity, load_identity,
    get_or_create_identity, identity_exists, IdentityError,
)
from pymesh.crypto.cipher import SessionCipher, derive_session_key, CipherError
from pymesh.crypto.trust import TrustStore, KeyChangedError
from pymesh.crypto.handshake import perform_crypto_handshake, CryptoHandshakeError
from pymesh.core.node import Node
from pymesh.utils.constants import MSG_CHAT

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

results = {"pass": 0, "fail": 0}


# ── Helpers ───────────────────────────────────────────────────────────────────

@contextmanager
def temp_pymesh_dir():
    """
    Context manager that redirects all ~/.pymesh/ file paths to a
    temporary directory for the duration of a test. Guarantees the
    real user directory is never touched.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        original_dir        = C.PYMESH_DIR
        original_identity   = C.IDENTITY_FILE
        original_peers      = C.KNOWN_PEERS_FILE

        C.PYMESH_DIR       = tmpdir
        C.IDENTITY_FILE    = os.path.join(tmpdir, "identity.key")
        C.KNOWN_PEERS_FILE = os.path.join(tmpdir, "known_peers.json")

        try:
            yield tmpdir
        finally:
            C.PYMESH_DIR       = original_dir
            C.IDENTITY_FILE    = original_identity
            C.KNOWN_PEERS_FILE = original_peers


async def make_pipe():
    server_conn = {}
    async def _handler(reader, writer):
        server_conn['reader'] = reader
        server_conn['writer'] = writer
    server = await asyncio.start_server(_handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    reader_a, writer_a = await asyncio.open_connection('127.0.0.1', port)
    await asyncio.sleep(0.05)
    server.close()
    return (reader_a, writer_a), (server_conn['reader'], server_conn['writer'])


async def run_test(name, coro):
    try:
        await coro
        print(f"  {PASS} {name}")
        results["pass"] += 1
    except AssertionError as e:
        print(f"  {FAIL} {name}")
        print(f"       AssertionError: {e}")
        results["fail"] += 1
    except Exception as e:
        import traceback
        print(f"  {FAIL} {name}")
        traceback.print_exc()
        results["fail"] += 1


# ══════════════════════════════════════════════════════════════════════════════
# 1. Identity
# ══════════════════════════════════════════════════════════════════════════════

async def t_generate_identity():
    identity = generate_identity()
    assert identity.fingerprint
    assert len(identity.fingerprint) == 64
    assert len(identity.public_key_bytes) == 32


async def t_sign_and_verify():
    identity = generate_identity()
    data = b"test message to sign"
    sig  = identity.sign(data)
    assert len(sig) == 64
    assert identity.verify(sig, data)
    assert not identity.verify(sig, b"tampered data")


async def t_verify_with_foreign_public_key():
    alice = generate_identity()
    bob   = generate_identity()
    data  = b"signed by alice"
    sig   = alice.sign(data)
    assert Identity.verify_with_public_key(alice.public_key_bytes, sig, data)
    assert not Identity.verify_with_public_key(bob.public_key_bytes, sig, data)


async def t_save_and_load_identity():
    with temp_pymesh_dir():
        identity    = generate_identity()
        original_fp = identity.fingerprint
        save_identity(identity, "test-passphrase-123")
        loaded = load_identity("test-passphrase-123")
        assert loaded.fingerprint == original_fp


async def t_wrong_passphrase_raises():
    with temp_pymesh_dir():
        identity = generate_identity()
        save_identity(identity, "correct-passphrase")
        try:
            load_identity("wrong-passphrase")
            assert False, "Should have raised IdentityError"
        except IdentityError:
            pass


async def t_two_identities_have_different_fingerprints():
    a = generate_identity()
    b = generate_identity()
    assert a.fingerprint != b.fingerprint


# ══════════════════════════════════════════════════════════════════════════════
# 2. Cipher
# ══════════════════════════════════════════════════════════════════════════════

async def t_encrypt_decrypt_roundtrip():
    key    = os.urandom(32)
    cipher = SessionCipher(key)
    plain  = b"Hello, encrypted world!"
    ct     = cipher.encrypt(plain)
    assert ct != plain
    assert cipher.decrypt(ct) == plain


async def t_different_nonce_each_time():
    key  = os.urandom(32)
    c    = SessionCipher(key)
    ct1  = c.encrypt(b"same message")
    ct2  = c.encrypt(b"same message")
    assert ct1 != ct2


async def t_tampered_ciphertext_raises():
    key  = os.urandom(32)
    c    = SessionCipher(key)
    ct   = bytearray(c.encrypt(b"sensitive data"))
    ct[20] ^= 0xFF
    try:
        c.decrypt(bytes(ct))
        assert False, "Should have raised CipherError"
    except CipherError:
        pass


async def t_wrong_key_raises():
    c1 = SessionCipher(os.urandom(32))
    c2 = SessionCipher(os.urandom(32))
    ct = c1.encrypt(b"secret")
    try:
        c2.decrypt(ct)
        assert False, "Should have raised CipherError"
    except CipherError:
        pass


async def t_derive_session_key_both_sides_match():
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    priv_i = X25519PrivateKey.generate()
    priv_r = X25519PrivateKey.generate()
    pub_i  = priv_i.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub_r  = priv_r.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    shared_i = priv_i.exchange(priv_r.public_key())
    shared_r = priv_r.exchange(priv_i.public_key())

    key_i = derive_session_key(shared_i, pub_i, pub_r)
    key_r = derive_session_key(shared_r, pub_i, pub_r)

    assert key_i == key_r
    assert len(key_i) == 32


# ══════════════════════════════════════════════════════════════════════════════
# 3. Trust Store
# ══════════════════════════════════════════════════════════════════════════════

async def t_trust_new_peer():
    with temp_pymesh_dir() as tmpdir:
        path  = os.path.join(tmpdir, "peers.json")
        store = TrustStore(path)
        assert store.check("alice", "fp-alice-001", "pubkey-hex") == "new"
        store.trust("alice", "fp-alice-001", "pubkey-hex")
        assert store.is_known("fp-alice-001")


async def t_known_peer_returns_trusted():
    with temp_pymesh_dir() as tmpdir:
        path  = os.path.join(tmpdir, "peers.json")
        store = TrustStore(path)
        store.trust("bob", "fp-bob-001", "pubkey-hex")
        assert store.check("bob", "fp-bob-001", "pubkey-hex") == "trusted"


async def t_key_change_raises():
    with temp_pymesh_dir() as tmpdir:
        path  = os.path.join(tmpdir, "peers.json")
        store = TrustStore(path)
        store.trust("carol", "fp-carol-original", "pubkey-hex")
        try:
            store.check("carol", "fp-carol-DIFFERENT", "pubkey-hex-new")
            assert False, "Should have raised KeyChangedError"
        except KeyChangedError as e:
            assert e.alias    == "carol"
            assert e.known_fp == "fp-carol-original"
            assert e.new_fp   == "fp-carol-DIFFERENT"


async def t_trust_persists_across_instances():
    with temp_pymesh_dir() as tmpdir:
        path   = os.path.join(tmpdir, "peers.json")
        store1 = TrustStore(path)
        store1.trust("dave", "fp-dave-001", "pubkey-hex")
        store2 = TrustStore(path)
        assert store2.is_known("fp-dave-001")


# ══════════════════════════════════════════════════════════════════════════════
# 4. Crypto Handshake
# ══════════════════════════════════════════════════════════════════════════════

async def t_crypto_handshake_succeeds():
    alice_id = generate_identity()
    bob_id   = generate_identity()
    (ra, wa), (rb, wb) = await make_pipe()

    t1 = asyncio.create_task(perform_crypto_handshake(ra, wa, True,  "alice", "test", alice_id))
    t2 = asyncio.create_task(perform_crypto_handshake(rb, wb, False, "bob",   "test", bob_id))
    (alice_peer, _), (bob_peer, _) = await asyncio.gather(t1, t2)

    assert alice_peer.alias       == "bob"
    assert bob_peer.alias         == "alice"
    assert alice_peer.fingerprint == bob_id.fingerprint
    assert bob_peer.fingerprint   == alice_id.fingerprint

    wa.close(); wb.close()


async def t_crypto_handshake_derives_same_session_key():
    alice_id = generate_identity()
    bob_id   = generate_identity()
    (ra, wa), (rb, wb) = await make_pipe()

    t1 = asyncio.create_task(perform_crypto_handshake(ra, wa, True,  "alice", "test", alice_id))
    t2 = asyncio.create_task(perform_crypto_handshake(rb, wb, False, "bob",   "test", bob_id))
    (_, alice_cipher), (_, bob_cipher) = await asyncio.gather(t1, t2)

    ct = alice_cipher.encrypt(b"cross-cipher test")
    assert bob_cipher.decrypt(ct) == b"cross-cipher test"

    wa.close(); wb.close()


async def t_crypto_handshake_session_mismatch_fails():
    alice_id = generate_identity()
    bob_id   = generate_identity()
    (ra, wa), (rb, wb) = await make_pipe()

    t1 = asyncio.create_task(perform_crypto_handshake(ra, wa, True,  "alice", "room-A", alice_id))
    t2 = asyncio.create_task(perform_crypto_handshake(rb, wb, False, "bob",   "room-B", bob_id))
    raw = await asyncio.gather(t1, t2, return_exceptions=True)
    assert any(isinstance(r, Exception) for r in raw)

    wa.close(); wb.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. End-to-End Encrypted Nodes
# ══════════════════════════════════════════════════════════════════════════════

async def t_two_encrypted_nodes_exchange_messages():
    with temp_pymesh_dir() as tmpdir:
        received = []
        async def on_msg(peer_info, msg): received.append(msg)

        alice_id = generate_identity()
        bob_id   = generate_identity()

        import os as _os
        alice = Node(alias="alice", session_name="test", identity=alice_id, port=0, trust_store_path=_os.path.join(tmpdir, "peers_alice.json"))
        bob   = Node(alias="bob",   session_name="test", identity=bob_id,   port=0, on_message=on_msg, trust_store_path=_os.path.join(tmpdir, "peers_bob.json"))

        alice_port = await alice.start()
        bob_port   = await bob.start()

        ok = await alice.connect_to("127.0.0.1", bob_port)
        assert ok
        await asyncio.sleep(0.4)

        assert alice.peer_count == 1, f"Alice has {alice.peer_count} peers"
        assert bob.peer_count   == 1, f"Bob has {bob.peer_count} peers"

        count = await alice.broadcast_message("Encrypted hello!")
        assert count == 1
        await asyncio.sleep(0.2)

        assert len(received) == 1
        assert received[0]["text"]  == "Encrypted hello!"
        assert received[0]["scope"] == "group"

        await alice.stop()
        await bob.stop()


async def t_private_encrypted_message():
    with temp_pymesh_dir() as tmpdir:
        bob_received   = []
        carol_received = []
        async def bob_h(pi, m):   bob_received.append(m)
        async def carol_h(pi, m): carol_received.append(m)

        alice_id = generate_identity()
        bob_id   = generate_identity()
        carol_id = generate_identity()

        import os as _os
        alice = Node(alias="alice", session_name="test", identity=alice_id, port=0, trust_store_path=_os.path.join(tmpdir, "peers_alice.json"))
        bob   = Node(alias="bob",   session_name="test", identity=bob_id,   port=0, on_message=bob_h, trust_store_path=_os.path.join(tmpdir, "peers_bob.json"))
        carol = Node(alias="carol", session_name="test", identity=carol_id, port=0, on_message=carol_h, trust_store_path=_os.path.join(tmpdir, "peers_carol.json"))

        await alice.start()
        bob_port   = await bob.start()
        carol_port = await carol.start()

        await alice.connect_to("127.0.0.1", bob_port)
        await alice.connect_to("127.0.0.1", carol_port)
        await asyncio.sleep(0.4)

        peers  = await alice.get_peers()
        bob_fp = next(p.fingerprint for p in peers if p.alias == "bob")

        ok = await alice.send_private_message(bob_fp, "Eyes only for Bob")
        assert ok
        await asyncio.sleep(0.2)

        chat_bob   = [m for m in bob_received   if m.get("type") == MSG_CHAT]
        chat_carol = [m for m in carol_received if m.get("type") == MSG_CHAT]

        assert len(chat_bob)   == 1
        assert chat_bob[0]["text"] == "Eyes only for Bob"
        assert len(chat_carol) == 0

        await alice.stop()
        await bob.stop()
        await carol.stop()


async def t_fingerprints_are_real_ed25519():
    import hashlib
    alice_id = generate_identity()
    expected = hashlib.sha256(alice_id.public_key_bytes).hexdigest()
    assert alice_id.fingerprint == expected
    assert len(alice_id.fingerprint) == 64


async def t_session_isolation_still_works():
    with temp_pymesh_dir() as tmpdir:
        alice_id = generate_identity()
        bob_id   = generate_identity()

        import os as _os
        alice = Node(alias="alice", session_name="room-A", identity=alice_id, port=0, trust_store_path=_os.path.join(tmpdir, "peers_alice.json"))
        bob   = Node(alias="bob",   session_name="room-B", identity=bob_id,   port=0, trust_store_path=_os.path.join(tmpdir, "peers_bob.json"))

        await alice.start()
        bob_port = await bob.start()

        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        assert alice.peer_count == 0, "Cross-session connection should be rejected"
        assert bob.peer_count   == 0

        await alice.stop()
        await bob.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("\n\033[1m━━━  PyMesh Chat — Phase 2 Test Suite  ━━━\033[0m\n")

    sections = [
        ("Identity (Ed25519 keypairs)", [
            ("generate identity",                           t_generate_identity),
            ("sign and verify",                             t_sign_and_verify),
            ("verify with foreign public key",              t_verify_with_foreign_public_key),
            ("save and load identity (encrypted on disk)",  t_save_and_load_identity),
            ("wrong passphrase raises IdentityError",       t_wrong_passphrase_raises),
            ("two identities have different fingerprints",  t_two_identities_have_different_fingerprints),
        ]),
        ("Cipher (AES-256-GCM)", [
            ("encrypt/decrypt roundtrip",                   t_encrypt_decrypt_roundtrip),
            ("different nonce every encryption",            t_different_nonce_each_time),
            ("tampered ciphertext raises CipherError",      t_tampered_ciphertext_raises),
            ("wrong key raises CipherError",                t_wrong_key_raises),
            ("both sides derive identical session key",     t_derive_session_key_both_sides_match),
        ]),
        ("Trust Store (TOFU)", [
            ("new peer returns status 'new'",               t_trust_new_peer),
            ("known peer returns status 'trusted'",         t_known_peer_returns_trusted),
            ("key change raises KeyChangedError",           t_key_change_raises),
            ("trust persists across TrustStore instances",  t_trust_persists_across_instances),
        ]),
        ("Crypto Handshake (X25519 + Ed25519)", [
            ("handshake completes both sides",              t_crypto_handshake_succeeds),
            ("both sides derive same session key",          t_crypto_handshake_derives_same_session_key),
            ("session mismatch fails handshake",            t_crypto_handshake_session_mismatch_fails),
        ]),
        ("End-to-End Encrypted Nodes", [
            ("two nodes exchange encrypted messages",       t_two_encrypted_nodes_exchange_messages),
            ("private message reaches only target",         t_private_encrypted_message),
            ("fingerprints are real Ed25519 SHA-256",       t_fingerprints_are_real_ed25519),
            ("session isolation still works",               t_session_isolation_still_works),
        ]),
    ]

    for section_name, tests in sections:
        print(f"\033[1m  {section_name}\033[0m")
        for test_name, test_fn in tests:
            await run_test(test_name, test_fn())

    total  = results["pass"] + results["fail"]
    colour = "\033[92m" if results["fail"] == 0 else "\033[91m"
    print(f"\n\033[1m━━━  Results: {colour}{results['pass']}/{total} passed\033[0m")

    if results["fail"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
