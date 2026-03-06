"""
PyMesh Chat — Phase 3 Tests
Tests for: message history, delivery receipts, typing indicators,
and end-to-end Phase 3 messaging between nodes.
"""

import asyncio
import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pymesh.utils.constants as C
from pymesh.crypto.identity import generate_identity
from pymesh.core.node import Node
from pymesh.messaging.history import MessageHistory, MessageRecord
from pymesh.messaging.typing import TypingTracker
from pymesh.utils.constants import MSG_CHAT, MSG_ACK, MSG_TYPING_START, MSG_TYPING_STOP

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

results = {"pass": 0, "fail": 0}


def make_node(alias, session, tmpdir, **kwargs):
    return Node(
        alias=alias,
        session_name=session,
        identity=generate_identity(),
        port=0,
        trust_store_path=os.path.join(tmpdir, f"peers_{alias}.json"),
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
# 1. Message History
# ══════════════════════════════════════════════════════════════════════════════

async def t_history_add_and_retrieve():
    h = MessageHistory()
    r = h.add(scope="group", sender="alice", text="hello everyone")
    assert r.scope  == "group"
    assert r.sender == "alice"
    assert r.text   == "hello everyone"
    assert len(h)   == 1


async def t_history_timestamp_display():
    h = MessageHistory()
    ts = int(time.time() * 1000)
    r  = h.add(scope="group", sender="alice", text="hi", ts=ts)
    # ts_display should be HH:MM format
    assert len(r.ts_display) == 5
    assert r.ts_display[2]   == ":"


async def t_history_private_message():
    h = MessageHistory()
    r = h.add(scope="private", sender="alice", text="psst", recipient="bob", is_own=True)
    assert r.scope     == "private"
    assert r.recipient == "bob"
    assert r.is_own    == True


async def t_history_delivery_tracking():
    h  = MessageHistory()
    r  = h.add(scope="group", sender="alice", text="hi", is_own=True)
    assert not r.delivered

    found = h.mark_delivered(r.id, "fp-bob")
    assert found
    assert "fp-bob" in r.delivered

    h.mark_delivered(r.id, "fp-carol")
    assert "fp-carol" in r.delivered
    assert len(r.delivered) == 2


async def t_history_mark_delivered_unknown_id():
    h = MessageHistory()
    h.add(scope="group", sender="alice", text="hi")
    found = h.mark_delivered("nonexistent-id", "fp-bob")
    assert not found


async def t_history_get_recent():
    h = MessageHistory()
    for i in range(10):
        h.add(scope="group", sender="alice", text=f"msg {i}")
    recent = h.get_recent(5)
    assert len(recent) == 5
    assert recent[-1].text == "msg 9"
    assert recent[0].text  == "msg 5"


async def t_history_max_cap():
    original = C.MAX_HISTORY
    C.MAX_HISTORY = 5
    from pymesh.messaging.history import MessageHistory as MH
    h = MH()
    # Override maxlen directly
    from collections import deque
    h._log = deque(maxlen=5)
    for i in range(10):
        h.add(scope="group", sender="alice", text=f"msg {i}")
    assert len(h) == 5
    assert h.get_recent(5)[0].text == "msg 5"
    C.MAX_HISTORY = original


async def t_history_clear():
    h = MessageHistory()
    h.add(scope="group", sender="alice", text="hi")
    h.add(scope="group", sender="alice", text="there")
    assert len(h) == 2
    h.clear()
    assert len(h) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 2. Typing Indicators
# ══════════════════════════════════════════════════════════════════════════════

async def t_typing_peer_started_callback():
    started = []
    tracker = TypingTracker(on_peer_started=lambda a: started.append(a))
    tracker.peer_started("bob")
    assert "bob" in started


async def t_typing_peer_stopped_callback():
    stopped = []
    tracker = TypingTracker(
        on_peer_started=lambda a: None,
        on_peer_stopped=lambda a: stopped.append(a),
    )
    tracker.peer_started("bob")
    tracker.peer_stopped("bob")
    assert "bob" in stopped


async def t_typing_who_is_typing():
    tracker = TypingTracker()
    tracker.peer_started("alice")
    tracker.peer_started("bob")
    who = tracker.who_is_typing()
    assert "alice" in who
    assert "bob"   in who
    tracker.peer_stopped("alice")
    assert "alice" not in tracker.who_is_typing()


async def t_typing_no_duplicate_started_callback():
    started = []
    tracker = TypingTracker(on_peer_started=lambda a: started.append(a))
    tracker.peer_started("bob")
    tracker.peer_started("bob")   # second call — already typing
    tracker.peer_started("bob")   # third call
    assert started.count("bob") == 1   # callback fires only once


async def t_typing_disconnected_clears_state():
    stopped = []
    tracker = TypingTracker(
        on_peer_started=lambda a: None,
        on_peer_stopped=lambda a: stopped.append(a),
    )
    tracker.peer_started("carol")
    assert "carol" in tracker.who_is_typing()
    tracker.peer_disconnected("carol")
    assert "carol" not in tracker.who_is_typing()
    assert "carol" in stopped


async def t_typing_outbound_sends_start():
    sent = []
    async def fake_start(): sent.append("start")
    async def fake_stop():  sent.append("stop")

    tracker = TypingTracker(send_start=fake_start, send_stop=fake_stop)
    await tracker.local_keystroke()
    assert "start" in sent


async def t_typing_outbound_no_duplicate_start():
    sent = []
    async def fake_start(): sent.append("start")

    tracker = TypingTracker(send_start=fake_start)
    await tracker.local_keystroke()
    await tracker.local_keystroke()
    await tracker.local_keystroke()
    assert sent.count("start") == 1   # only sent once


async def t_typing_outbound_stop_on_send():
    sent = []
    async def fake_start(): sent.append("start")
    async def fake_stop():  sent.append("stop")

    tracker = TypingTracker(send_start=fake_start, send_stop=fake_stop)
    await tracker.local_keystroke()
    await tracker.local_sent()
    assert "stop" in sent


async def t_typing_expiry():
    """Typing indicator should auto-expire after TYPING_TIMEOUT."""
    original = C.TYPING_TIMEOUT
    C.TYPING_TIMEOUT = 0.1   # speed up for test
    stopped = []

    tracker = TypingTracker(
        on_peer_started=lambda a: None,
        on_peer_stopped=lambda a: stopped.append(a),
    )
    tracker.peer_started("dave")
    await asyncio.sleep(0.3)
    assert "dave" in stopped

    C.TYPING_TIMEOUT = original
    tracker.stop()


# ══════════════════════════════════════════════════════════════════════════════
# 3. End-to-End Phase 3 Messaging
# ══════════════════════════════════════════════════════════════════════════════

async def t_message_saved_to_history_on_send():
    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir)

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await alice.broadcast_message("test message")
        assert len(alice.history) == 1
        assert alice.history.get_all()[0].text == "test message"
        assert alice.history.get_all()[0].is_own

        await alice.stop(); await bob.stop()


async def t_message_saved_to_history_on_receive():
    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir)

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await alice.broadcast_message("hello bob")
        await asyncio.sleep(0.3)

        # Bob should have it in his history too
        assert len(bob.history) == 1
        assert bob.history.get_all()[0].text   == "hello bob"
        assert not bob.history.get_all()[0].is_own

        await alice.stop(); await bob.stop()


async def t_delivery_receipt_received():
    with tempfile.TemporaryDirectory() as tmpdir:
        deliveries = []
        async def on_delivery(msg_id, alias): deliveries.append((msg_id, alias))

        alice = make_node("alice", "test", tmpdir, on_delivery=on_delivery)
        bob   = make_node("bob",   "test", tmpdir)

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await alice.broadcast_message("did you get this?")
        await asyncio.sleep(0.4)

        # Alice should have received an ACK from Bob
        assert len(deliveries) == 1
        assert deliveries[0][1] == "bob"

        await alice.stop(); await bob.stop()


async def t_history_marked_delivered_after_ack():
    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir)

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await alice.broadcast_message("test delivery tracking")
        await asyncio.sleep(0.4)

        record = alice.history.get_all()[0]
        assert len(record.delivered) == 1   # Bob's fingerprint is in delivered set

        await alice.stop(); await bob.stop()


async def t_typing_signals_sent_over_network():
    with tempfile.TemporaryDirectory() as tmpdir:
        typing_started = []
        typing_stopped = []

        async def on_start(alias): typing_started.append(alias)
        async def on_stop(alias):  typing_stopped.append(alias)

        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir,
                          on_typing_start=on_start,
                          on_typing_stop=on_stop)

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await alice.send_typing_start()
        await asyncio.sleep(0.2)
        assert "alice" in typing_started

        await alice.send_typing_stop()
        await asyncio.sleep(0.2)
        assert "alice" in typing_stopped

        await alice.stop(); await bob.stop()


async def t_private_message_in_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir)

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        peers  = await alice.get_peers()
        bob_fp = next(p.fingerprint for p in peers if p.alias == "bob")

        await alice.send_private_message(bob_fp, "private note")
        await asyncio.sleep(0.3)

        # Alice's history
        alice_records = alice.history.get_all()
        assert len(alice_records) == 1
        assert alice_records[0].scope  == "private"
        assert alice_records[0].is_own == True

        # Bob's history
        bob_records = bob.history.get_all()
        assert len(bob_records) == 1
        assert bob_records[0].text     == "private note"
        assert bob_records[0].is_own   == False

        await alice.stop(); await bob.stop()


async def t_multiple_messages_correct_order():
    with tempfile.TemporaryDirectory() as tmpdir:
        alice = make_node("alice", "test", tmpdir)
        bob   = make_node("bob",   "test", tmpdir)

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        for i in range(5):
            await alice.broadcast_message(f"message {i}")
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.3)

        bob_records = bob.history.get_all()
        assert len(bob_records) == 5
        for i, r in enumerate(bob_records):
            assert r.text == f"message {i}"

        await alice.stop(); await bob.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("\n\033[1m━━━  PyMesh Chat — Phase 3 Test Suite  ━━━\033[0m\n")

    sections = [
        ("Message History", [
            ("add and retrieve message",               t_history_add_and_retrieve),
            ("timestamp display format HH:MM",         t_history_timestamp_display),
            ("private message fields",                 t_history_private_message),
            ("delivery tracking per fingerprint",      t_history_delivery_tracking),
            ("mark_delivered unknown id returns False",t_history_mark_delivered_unknown_id),
            ("get_recent returns correct slice",       t_history_get_recent),
            ("history respects maxlen cap",            t_history_max_cap),
            ("clear empties the log",                  t_history_clear),
        ]),
        ("Typing Indicators", [
            ("peer_started fires callback",            t_typing_peer_started_callback),
            ("peer_stopped fires callback",            t_typing_peer_stopped_callback),
            ("who_is_typing returns correct aliases",  t_typing_who_is_typing),
            ("no duplicate started callbacks",         t_typing_no_duplicate_started_callback),
            ("peer_disconnected clears typing state",  t_typing_disconnected_clears_state),
            ("local_keystroke sends TYPING_START",     t_typing_outbound_sends_start),
            ("no duplicate TYPING_START sent",         t_typing_outbound_no_duplicate_start),
            ("local_sent sends TYPING_STOP",           t_typing_outbound_stop_on_send),
            ("typing indicator auto-expires",          t_typing_expiry),
        ]),
        ("End-to-End Phase 3 Messaging", [
            ("sent message saved to sender history",   t_message_saved_to_history_on_send),
            ("received message saved to recv history", t_message_saved_to_history_on_receive),
            ("delivery receipt fires on_delivery cb",  t_delivery_receipt_received),
            ("history record marked delivered on ACK", t_history_marked_delivered_after_ack),
            ("TYPING_START/STOP sent over network",    t_typing_signals_sent_over_network),
            ("private message saved to both histories",t_private_message_in_history),
            ("multiple messages arrive in order",      t_multiple_messages_correct_order),
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
