"""
PyMesh Chat — Phase 4 Tests
Tests for: file transfer engine (unit), SHA-256 integrity,
progress callbacks, accept/reject flow, and full end-to-end
two-node file transfer over encrypted connections.
"""

import asyncio
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pymesh.crypto.identity import generate_identity
from pymesh.core.node import Node
from pymesh.files.transfer import (
    FileTransferManager, TransferState,
    _sha256_file, _fmt_size, _unique_path,
)

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

results = {"pass": 0, "fail": 0}


def make_node(alias, session, tmpdir, **kwargs):
    return Node(
        alias=alias, session_name=session,
        identity=generate_identity(), port=0,
        trust_store_path=os.path.join(tmpdir, f"peers_{alias}.json"),
        download_dir=os.path.join(tmpdir, f"downloads_{alias}"),
        **kwargs,
    )


def make_test_file(directory: str, name: str, size_bytes: int) -> str:
    """Create a test file filled with deterministic content."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    content = (b"PyMesh test data 1234567890abcdef" * ((size_bytes // 32) + 1))[:size_bytes]
    with open(path, "wb") as f:
        f.write(content)
    return path


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
# 1. Transfer Engine — Unit Tests
# ══════════════════════════════════════════════════════════════════════════════

async def t_sha256_file():
    with tempfile.TemporaryDirectory() as d:
        path = make_test_file(d, "test.txt", 1024)
        sha  = _sha256_file(path)
        assert len(sha) == 64
        # Verify it matches manual computation
        with open(path, "rb") as f:
            expected = hashlib.sha256(f.read()).hexdigest()
        assert sha == expected


async def t_fmt_size():
    assert _fmt_size(500)          == "500.0 B"
    assert _fmt_size(1024)         == "1.0 KB"
    assert _fmt_size(1024 * 1024)  == "1.0 MB"
    assert "GB" in _fmt_size(1024 ** 3)


async def t_unique_path_no_collision():
    with tempfile.TemporaryDirectory() as d:
        path = _unique_path(d, "file.txt")
        assert path == os.path.join(d, "file.txt")


async def t_unique_path_collision():
    with tempfile.TemporaryDirectory() as d:
        # Create the file so it collides
        open(os.path.join(d, "file.txt"), "w").close()
        path = _unique_path(d, "file.txt")
        assert path == os.path.join(d, "file (1).txt")


async def t_prepare_offer_valid_file():
    with tempfile.TemporaryDirectory() as d:
        src  = make_test_file(d, "send.txt", 2048)
        mgr  = FileTransferManager(download_dir=os.path.join(d, "dl"))
        t    = mgr.prepare_offer(src, peer_alias="bob")
        assert t.file_name == "send.txt"
        assert t.file_size == 2048
        assert len(t.sha256) == 64
        assert t.state == TransferState.PENDING


async def t_prepare_offer_missing_file():
    with tempfile.TemporaryDirectory() as d:
        mgr = FileTransferManager(download_dir=os.path.join(d, "dl"))
        try:
            mgr.prepare_offer("/nonexistent/file.txt", peer_alias="bob")
            assert False, "Should raise FileNotFoundError"
        except FileNotFoundError:
            pass


async def t_prepare_offer_empty_file():
    with tempfile.TemporaryDirectory() as d:
        empty = os.path.join(d, "empty.txt")
        open(empty, "w").close()
        mgr = FileTransferManager(download_dir=os.path.join(d, "dl"))
        try:
            mgr.prepare_offer(empty, peer_alias="bob")
            assert False, "Should raise ValueError for empty file"
        except ValueError:
            pass


async def t_register_offer_sanitises_filename():
    with tempfile.TemporaryDirectory() as d:
        mgr = FileTransferManager(download_dir=d)
        t   = mgr.register_offer(
            transfer_id="tid-1", file_name="../../../etc/passwd",
            file_size=100, expected_sha="abc", sender_alias="evil", sender_fp="fp"
        )
        # Filename must not contain path traversal
        assert "/" not in t.file_name
        assert t.file_name == "passwd"


async def t_accept_and_reject():
    with tempfile.TemporaryDirectory() as d:
        mgr = FileTransferManager(download_dir=d)
        mgr.register_offer("tid-1", "file.txt", 100, "sha", "alice", "fp1")
        mgr.register_offer("tid-2", "other.txt", 200, "sha", "alice", "fp1")

        assert mgr.accept_transfer("tid-1")
        assert mgr.get_inbound("tid-1").state == TransferState.ACTIVE

        assert mgr.reject_transfer("tid-2")
        assert mgr.get_inbound("tid-2").state == TransferState.REJECTED


async def t_chunk_integrity_check():
    with tempfile.TemporaryDirectory() as d:
        src = make_test_file(d, "data.bin", 512)
        dl  = os.path.join(d, "dl")

        sent_chunks = []
        async def capture(msg): sent_chunks.append(msg)

        mgr_s = FileTransferManager(download_dir=dl)
        mgr_r = FileTransferManager(download_dir=dl)

        transfer = mgr_s.prepare_offer(src, peer_alias="bob")
        mgr_r.register_offer(
            transfer_id  = transfer.transfer_id,
            file_name    = transfer.file_name,
            file_size    = transfer.file_size,
            expected_sha = transfer.sha256,
            sender_alias = "alice",
            sender_fp    = "fp-alice",
        )
        mgr_r.accept_transfer(transfer.transfer_id)

        await mgr_s.send_chunks(transfer, capture)

        for msg in sent_chunks:
            if msg["type"] == "FILE_CHUNK":
                err = mgr_r.handle_chunk(msg)
                assert err is None, f"Chunk error: {err}"

        done_msg = sent_chunks[-1]
        assert done_msg["type"] == "FILE_DONE"
        ok, err, final_path = mgr_r.handle_done(done_msg)

        assert ok, f"Verification failed: {err}"
        assert final_path and os.path.exists(final_path)

        # Verify received file matches original
        with open(src, "rb") as f: original = f.read()
        with open(final_path, "rb") as f: received = f.read()
        assert original == received


async def t_sha256_mismatch_detected():
    with tempfile.TemporaryDirectory() as d:
        src = make_test_file(d, "data.bin", 256)
        dl  = os.path.join(d, "dl")

        sent_chunks = []
        async def capture(msg): sent_chunks.append(msg)

        mgr_s = FileTransferManager(download_dir=dl)
        mgr_r = FileTransferManager(download_dir=dl)

        transfer = mgr_s.prepare_offer(src, peer_alias="bob")
        # Register with WRONG expected SHA
        mgr_r.register_offer(
            transfer_id  = transfer.transfer_id,
            file_name    = transfer.file_name,
            file_size    = transfer.file_size,
            expected_sha = "a" * 64,   # wrong hash
            sender_alias = "alice",
            sender_fp    = "fp-alice",
        )
        mgr_r.accept_transfer(transfer.transfer_id)

        await mgr_s.send_chunks(transfer, capture)
        for msg in sent_chunks:
            if msg["type"] == "FILE_CHUNK":
                mgr_r.handle_chunk(msg)

        done_msg = sent_chunks[-1]
        ok, err, final_path = mgr_r.handle_done(done_msg)

        assert not ok, "Should have failed on hash mismatch"
        assert "mismatch" in err.lower()
        # Temp file should be cleaned up — no corrupt file left behind
        t = mgr_r.get_inbound(transfer.transfer_id)
        assert not os.path.exists(t._tmp_path)


async def t_progress_callback_called():
    with tempfile.TemporaryDirectory() as d:
        # Use a file larger than one chunk to guarantee multiple progress calls
        from pymesh.utils.constants import FILE_CHUNK_SIZE
        src = make_test_file(d, "big.bin", FILE_CHUNK_SIZE * 3)
        dl  = os.path.join(d, "dl")

        progress_calls = []
        def on_progress(tid, done, total): progress_calls.append((done, total))

        sent_chunks = []
        async def capture(msg): sent_chunks.append(msg)

        mgr_s = FileTransferManager(download_dir=dl, on_progress=on_progress)
        transfer = mgr_s.prepare_offer(src, peer_alias="bob")
        await mgr_s.send_chunks(transfer, capture)

        assert len(progress_calls) >= 3   # one per chunk
        assert progress_calls[-1][0] == progress_calls[-1][1]   # final: done == total


async def t_file_ack_marks_done():
    with tempfile.TemporaryDirectory() as d:
        src = make_test_file(d, "ack_test.txt", 128)
        mgr = FileTransferManager(download_dir=d)
        t   = mgr.prepare_offer(src, peer_alias="bob")
        assert t.state == TransferState.PENDING
        mgr.handle_file_ack(t.transfer_id)
        assert t.state == TransferState.DONE


async def t_file_reject_marks_rejected():
    with tempfile.TemporaryDirectory() as d:
        src = make_test_file(d, "rej_test.txt", 128)
        mgr = FileTransferManager(download_dir=d)
        t   = mgr.prepare_offer(src, peer_alias="bob")
        mgr.handle_file_reject(t.transfer_id)
        assert t.state == TransferState.REJECTED


# ══════════════════════════════════════════════════════════════════════════════
# 2. End-to-End File Transfer Between Nodes
# ══════════════════════════════════════════════════════════════════════════════

async def t_e2e_small_file_transfer():
    with tempfile.TemporaryDirectory() as tmpdir:
        src   = make_test_file(tmpdir, "hello.txt", 1024)
        done  = asyncio.Event()
        paths = []

        async def on_offer(tid, sender, name, size):
            # Auto-accept for test
            await bob.accept_file(tid)

        async def on_complete(tid, path):
            paths.append(path)
            done.set()

        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir,
                          on_file_offer=on_offer,
                          on_file_complete=on_complete)

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        tid = await alice.send_file(src, "bob")
        assert tid is not None

        await asyncio.wait_for(done.wait(), timeout=10.0)

        assert len(paths) == 1
        assert os.path.exists(paths[0])

        # Verify contents match
        with open(src, "rb") as f:    original = f.read()
        with open(paths[0], "rb") as f: received = f.read()
        assert original == received

        await alice.stop(); await bob.stop()


async def t_e2e_large_file_transfer():
    """Transfer a 500 KB file — multiple chunks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from pymesh.utils.constants import FILE_CHUNK_SIZE
        src  = make_test_file(tmpdir, "large.bin", FILE_CHUNK_SIZE * 8)
        done = asyncio.Event()
        paths = []

        async def on_offer(tid, sender, name, size):
            await bob.accept_file(tid)

        async def on_complete(tid, path):
            paths.append(path)
            done.set()

        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir,
                          on_file_offer=on_offer,
                          on_file_complete=on_complete)

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await alice.send_file(src, "bob")
        await asyncio.wait_for(done.wait(), timeout=15.0)

        with open(src, "rb") as f:    original = f.read()
        with open(paths[0], "rb") as f: received = f.read()
        assert original == received

        await alice.stop(); await bob.stop()


async def t_e2e_file_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        src      = make_test_file(tmpdir, "offer.txt", 512)
        rejected = asyncio.Event()
        rej_info = []

        async def on_offer(tid, sender, name, size):
            await bob.reject_file(tid)

        async def on_rejected(tid, alias):
            rej_info.append(alias)
            rejected.set()

        alice = make_node("alice", "test", tmpdir, on_file_rejected=on_rejected)
        bob   = make_node("bob",   "test", tmpdir, on_file_offer=on_offer)

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await alice.send_file(src, "bob")
        await asyncio.wait_for(rejected.wait(), timeout=5.0)

        assert rej_info[0] == "bob"
        t = alice.files.get_outbound(list(alice.files._outbound.keys())[0])
        assert t.state == TransferState.REJECTED

        await alice.stop(); await bob.stop()


async def t_e2e_broadcast_file():
    """Alice broadcasts a file to Bob and Carol — both receive it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src    = make_test_file(tmpdir, "broadcast.txt", 2048)
        bob_done   = asyncio.Event()
        carol_done = asyncio.Event()

        async def bob_offer(tid, sender, name, size):
            await bob.accept_file(tid)

        async def carol_offer(tid, sender, name, size):
            await carol.accept_file(tid)

        async def bob_complete(tid, path):   bob_done.set()
        async def carol_complete(tid, path): carol_done.set()

        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir,
                          on_file_offer=bob_offer, on_file_complete=bob_complete)
        carol = make_node("carol", "test", tmpdir,
                          on_file_offer=carol_offer, on_file_complete=carol_complete)

        await alice.start()
        bob_port   = await bob.start()
        carol_port = await carol.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await alice.connect_to("127.0.0.1", carol_port)
        await asyncio.sleep(0.4)

        tids = await alice.broadcast_file(src)
        assert len(tids) == 2

        await asyncio.wait_for(
            asyncio.gather(bob_done.wait(), carol_done.wait()), timeout=15.0
        )

        await alice.stop(); await bob.stop(); await carol.stop()


async def t_no_corrupt_file_on_hash_failure():
    """If hash verification fails, no file should land in downloads folder."""
    with tempfile.TemporaryDirectory() as tmpdir:
        error_called = asyncio.Event()

        async def on_error(tid, reason):
            error_called.set()

        # Monkey-patch: make receiver use wrong expected hash
        original_register = FileTransferManager.register_offer

        def patched_register(self, transfer_id, file_name, file_size, expected_sha,
                             sender_alias, sender_fp):
            return original_register(self, transfer_id, file_name, file_size,
                                    "f" * 64, sender_alias, sender_fp)

        FileTransferManager.register_offer = patched_register

        try:
            src   = make_test_file(tmpdir, "corrupt_test.txt", 512)
            alice = make_node("alice", "test", tmpdir)
            bob   = make_node("bob",   "test", tmpdir,
                              on_file_offer=lambda tid, s, n, sz: bob.accept_file(tid),
                              on_file_error=on_error)

            await alice.start()
            bob_port = await bob.start()
            await alice.connect_to("127.0.0.1", bob_port)
            await asyncio.sleep(0.4)

            await alice.send_file(src, "bob")
            await asyncio.wait_for(error_called.wait(), timeout=8.0)

            # No complete file should exist in bob's downloads
            dl_dir = os.path.join(tmpdir, "downloads_bob")
            real_files = [f for f in os.listdir(dl_dir) if not f.startswith(".")]
            assert len(real_files) == 0, f"Corrupt file leaked: {real_files}"

            await alice.stop(); await bob.stop()
        finally:
            FileTransferManager.register_offer = original_register


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("\n\033[1m━━━  PyMesh Chat — Phase 4 Test Suite  ━━━\033[0m\n")

    sections = [
        ("Transfer Engine — Unit Tests", [
            ("SHA-256 file hash is correct",              t_sha256_file),
            ("_fmt_size formats correctly",               t_fmt_size),
            ("_unique_path returns original if free",     t_unique_path_no_collision),
            ("_unique_path appends counter on collision", t_unique_path_collision),
            ("prepare_offer validates file",              t_prepare_offer_valid_file),
            ("prepare_offer: missing file raises",        t_prepare_offer_missing_file),
            ("prepare_offer: empty file raises",          t_prepare_offer_empty_file),
            ("register_offer sanitises filename",         t_register_offer_sanitises_filename),
            ("accept and reject change state",            t_accept_and_reject),
            ("chunks reassemble to identical file",       t_chunk_integrity_check),
            ("SHA-256 mismatch detected and cleaned up",  t_sha256_mismatch_detected),
            ("progress callback fired per chunk",         t_progress_callback_called),
            ("FILE_ACK marks outbound as DONE",           t_file_ack_marks_done),
            ("FILE_REJECT marks outbound as REJECTED",    t_file_reject_marks_rejected),
        ]),
        ("End-to-End File Transfer", [
            ("small file transferred intact",             t_e2e_small_file_transfer),
            ("large file (multi-chunk) transferred",      t_e2e_large_file_transfer),
            ("rejected transfer notifies sender",         t_e2e_file_rejected),
            ("broadcast file reaches all peers",          t_e2e_broadcast_file),
            ("hash failure leaves no corrupt file",       t_no_corrupt_file_on_hash_failure),
        ]),
    ]

    for section_name, tests in sections:
        print(f"\033[1m  {section_name}\033[0m")
        for test_name, test_fn in tests:
            await run_test(test_name, test_fn())

    total  = results["pass"] + results["fail"]
    colour = "\033[92m" if results["fail"] == 0 else "\033[91m"
    print(f"\n\033[1m━━━  Results: {colour}{results['pass']}/{total} passed\033[0m\n")

    if results["fail"] > 0:
        import sys; sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
