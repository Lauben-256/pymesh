"""
PyMesh Chat — Phase 1 Tests (updated for Phase 2 API)
Protocol framing, handshake, and two-node integration.
Now uses the crypto handshake — identities are generated inline for each test.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pymesh.utils.constants as C
from pymesh.core.protocol import (
    FrameReader, FrameWriter, ProtocolError, ConnectionClosedError, build_message
)
from pymesh.core.handshake import perform_handshake, HandshakeError
from pymesh.core.node import Node
from pymesh.crypto.identity import generate_identity
from pymesh.utils.constants import MSG_CHAT, APP_PROTOCOL_VERSION

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

results = {"pass": 0, "fail": 0}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def make_pipe():
    server_conn = {}
    async def _handler(r, w):
        server_conn['reader'] = r
        server_conn['writer'] = w
    server = await asyncio.start_server(_handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    ra, wa = await asyncio.open_connection('127.0.0.1', port)
    await asyncio.sleep(0.05)
    server.close()
    return (ra, wa), (server_conn['reader'], server_conn['writer'])


def make_node(alias, session, tmpdir, **kwargs):
    path = os.path.join(tmpdir, f"peers_{alias}.json")
    return Node(
        alias=alias,
        session_name=session,
        identity=generate_identity(),
        port=0,
        trust_store_path=path,
        **kwargs,
    )


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
# 1. Protocol Framing
# ══════════════════════════════════════════════════════════════════════════════

async def t_send_receive_simple():
    (ra, wa), (rb, wb) = await make_pipe()
    writer = FrameWriter(wa)
    reader = FrameReader(rb)
    msg = build_message("TEST", text="hello")
    await writer.send_message(msg)
    received = await reader.read_message()
    assert received["type"] == "TEST"
    assert received["text"] == "hello"
    wa.close(); wb.close()


async def t_multiple_messages():
    (ra, wa), (rb, wb) = await make_pipe()
    writer = FrameWriter(wa)
    reader = FrameReader(rb)
    for i in range(5):
        await writer.send_message(build_message("MSG", index=i))
    for i in range(5):
        m = await reader.read_message()
        assert m["index"] == i
    wa.close(); wb.close()


async def t_large_payload():
    (ra, wa), (rb, wb) = await make_pipe()
    writer = FrameWriter(wa)
    reader = FrameReader(rb)
    big = "x" * 500_000
    await writer.send_message(build_message("BIG", data=big))
    m = await reader.read_message()
    assert len(m["data"]) == 500_000
    wa.close(); wb.close()


async def t_closed_connection_raises():
    (ra, wa), (rb, wb) = await make_pipe()
    reader = FrameReader(rb)
    import struct
    wa.write(b"\x00\x00")
    await wa.drain()
    wa.close()
    try:
        await reader.read_message()
        assert False, "Should have raised"
    except ConnectionClosedError:
        pass
    wb.close()


async def t_build_message_fields():
    m = build_message("PING", extra="val")
    assert m["type"] == "PING"
    assert m["version"] == APP_PROTOCOL_VERSION
    assert "ts" in m
    assert m["extra"] == "val"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Handshake
# ══════════════════════════════════════════════════════════════════════════════

async def t_successful_handshake():
    alice_id = generate_identity()
    bob_id   = generate_identity()
    (ra, wa), (rb, wb) = await make_pipe()

    t1 = asyncio.create_task(
        perform_handshake(ra, wa, True,  "alice", "", "test", identity=alice_id)
    )
    t2 = asyncio.create_task(
        perform_handshake(rb, wb, False, "bob",   "", "test", identity=bob_id)
    )
    (alice_result, _), (bob_result, _) = await asyncio.gather(t1, t2)

    assert alice_result.alias      == "bob"
    assert bob_result.alias        == "alice"
    assert alice_result.fingerprint == bob_id.fingerprint
    assert bob_result.fingerprint  == alice_id.fingerprint
    wa.close(); wb.close()


async def t_session_mismatch_raises():
    alice_id = generate_identity()
    bob_id   = generate_identity()
    (ra, wa), (rb, wb) = await make_pipe()

    t1 = asyncio.create_task(
        perform_handshake(ra, wa, True,  "alice", "", "room-A", identity=alice_id)
    )
    t2 = asyncio.create_task(
        perform_handshake(rb, wb, False, "bob",   "", "room-B", identity=bob_id)
    )
    raw = await asyncio.gather(t1, t2, return_exceptions=True)
    assert any(isinstance(r, Exception) for r in raw)
    wa.close(); wb.close()


async def t_unique_peer_ids():
    alice_id = generate_identity()
    bob_id   = generate_identity()
    (ra, wa), (rb, wb) = await make_pipe()

    t1 = asyncio.create_task(
        perform_handshake(ra, wa, True,  "alice", "", "s", identity=alice_id)
    )
    t2 = asyncio.create_task(
        perform_handshake(rb, wb, False, "bob",   "", "s", identity=bob_id)
    )
    (p1, _), (p2, _) = await asyncio.gather(t1, t2)
    assert p1.peer_id != p2.peer_id
    wa.close(); wb.close()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Two-Node Integration
# ══════════════════════════════════════════════════════════════════════════════

async def t_connect_and_exchange():
    received = []
    async def on_msg(pi, m): received.append(m)

    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir, on_message=on_msg)

        await alice.start()
        bob_port = await bob.start()

        ok = await alice.connect_to("127.0.0.1", bob_port)
        assert ok
        await asyncio.sleep(0.4)

        count = await alice.broadcast_message("hello bob")
        assert count == 1
        await asyncio.sleep(0.2)

        chat = [m for m in received if m.get("type") == MSG_CHAT]
        assert len(chat) == 1
        assert chat[0]["text"] == "hello bob"

        await alice.stop(); await bob.stop()


async def t_private_message():
    bob_recv   = []
    carol_recv = []
    async def bob_h(pi, m):   bob_recv.append(m)
    async def carol_h(pi, m): carol_recv.append(m)

    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir, on_message=bob_h)
        carol = make_node("carol", "test", tmpdir, on_message=carol_h)

        await alice.start()
        bob_port   = await bob.start()
        carol_port = await carol.start()

        await alice.connect_to("127.0.0.1", bob_port)
        await alice.connect_to("127.0.0.1", carol_port)
        await asyncio.sleep(0.4)

        peers  = await alice.get_peers()
        bob_fp = next(p.fingerprint for p in peers if p.alias == "bob")

        ok = await alice.send_private_message(bob_fp, "private for bob")
        assert ok
        await asyncio.sleep(0.2)

        assert len([m for m in bob_recv   if m.get("type") == MSG_CHAT]) == 1
        assert len([m for m in carol_recv if m.get("type") == MSG_CHAT]) == 0

        await alice.stop(); await bob.stop(); await carol.stop()


async def t_peer_joined_callback():
    joined = []
    async def on_join(pi): joined.append(pi)

    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "test", tmpdir, on_peer_joined=on_join)
        bob   = make_node("bob",   "test", tmpdir)

        await alice.start()
        bob_port = await bob.start()

        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        assert len(joined) == 1
        assert joined[0].alias == "bob"

        await alice.stop(); await bob.stop()


async def t_peer_left_callback():
    left = []
    async def on_leave(pi): left.append(pi)

    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "test", tmpdir, on_peer_left=on_leave)
        bob   = make_node("bob",   "test", tmpdir)

        await alice.start()
        bob_port = await bob.start()

        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await bob.stop()
        await asyncio.sleep(0.3)

        assert len(left) == 1
        assert left[0].alias == "bob"

        await alice.stop()


async def t_session_isolation():
    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "room-A", tmpdir)
        bob   = make_node("bob",   "room-B", tmpdir)

        await alice.start()
        bob_port = await bob.start()

        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        assert alice.peer_count == 0
        assert bob.peer_count   == 0

        await alice.stop(); await bob.stop()


async def t_get_peers():
    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir)

        await alice.start()
        bob_port = await bob.start()

        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        peers = await alice.get_peers()
        assert len(peers) == 1
        assert peers[0].alias == "bob"

        await alice.stop(); await bob.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("\n\033[1m━━━  PyMesh Chat — Phase 1 Test Suite  ━━━\033[0m\n")

    sections = [
        ("Protocol Framing", [
            ("send and receive simple message",     t_send_receive_simple),
            ("multiple messages in sequence",       t_multiple_messages),
            ("large payload (500 KB)",              t_large_payload),
            ("ConnectionClosedError on closed conn",t_closed_connection_raises),
            ("build_message includes required fields", t_build_message_fields),
        ]),
        ("Handshake", [
            ("successful handshake both sides",     t_successful_handshake),
            ("session mismatch raises error",       t_session_mismatch_raises),
            ("unique peer_ids assigned",            t_unique_peer_ids),
        ]),
        ("Two-Node Integration", [
            ("connect and exchange group messages", t_connect_and_exchange),
            ("private message reaches only target", t_private_message),
            ("on_peer_joined callback fires",       t_peer_joined_callback),
            ("on_peer_left callback fires",         t_peer_left_callback),
            ("session isolation blocks cross-session", t_session_isolation),
            ("get_peers returns correct PeerInfo",  t_get_peers),
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
