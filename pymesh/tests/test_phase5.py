"""
PyMesh Chat — Phase 5 Tests
Tests for TUI components: event queue, message rendering,
typing indicator integration, and node callback wiring.
We test the logic layer without invoking curses rendering.
"""

import asyncio
import os
import sys
import time
import tempfile
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pymesh.crypto.identity import generate_identity
from pymesh.core.node import Node
from pymesh.core.peer import PeerInfo
from pymesh.ui.tui import TUI, DisplayMessage, PeerDisplay, FileDisplay, _word_wrap
from pymesh.files.transfer import _fmt_size

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

results = {"pass": 0, "fail": 0}


def make_node(alias, session, tmpdir, **kwargs):
    return Node(
        alias=alias, session_name=session,
        identity=generate_identity(), port=0,
        trust_store_path=os.path.join(tmpdir, f"peers_{alias}.json"),
        **kwargs,
    )


def make_tui(node):
    """Create a TUI instance with a mocked event loop."""
    tui = TUI.__new__(TUI)
    tui._node             = node
    tui._connect_on_start = None
    tui._messages         = __import__('collections').deque(maxlen=1000)
    tui._peers            = []
    tui._files            = {}
    tui._typing_who       = []
    tui._input_buf        = []
    tui._scroll_offset    = 0
    tui._msg_id_map       = {}
    tui._modal            = None
    tui._pending_offers   = {}
    tui._running          = False
    tui._stdscr           = None
    tui._loop             = asyncio.get_running_loop()
    tui._events           = asyncio.Queue()
    from pymesh.messaging.typing import TypingTracker
    tui._typing = TypingTracker(
        on_peer_started = tui._typing_started,
        on_peer_stopped = tui._typing_stopped,
        send_start      = node.send_typing_start,
        send_stop       = node.send_typing_stop,
    )
    return tui


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
# 1. Word Wrap
# ══════════════════════════════════════════════════════════════════════════════

async def t_wordwrap_short():
    lines = _word_wrap("hello world", 80)
    assert lines == ["hello world"]

async def t_wordwrap_exact():
    lines = _word_wrap("ab cd", 5)
    assert lines == ["ab cd"]

async def t_wordwrap_long():
    text  = "one two three four five six seven eight nine ten"
    lines = _word_wrap(text, 20)
    assert len(lines) > 1
    for line in lines:
        assert len(line) <= 20

async def t_wordwrap_no_space():
    # No space → hard cut at width
    text  = "a" * 30
    lines = _word_wrap(text, 10)
    assert len(lines) == 3
    assert all(len(l) <= 10 for l in lines)

async def t_wordwrap_empty():
    lines = _word_wrap("", 20)
    assert lines == [""]


# ══════════════════════════════════════════════════════════════════════════════
# 2. DisplayMessage rendering
# ══════════════════════════════════════════════════════════════════════════════

async def t_render_group_message():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        msg  = DisplayMessage(ts="14:32", scope="group", sender="bob",
                               recipient="", text="hello")
        lines = tui._render_message(msg, 80)
        assert len(lines) >= 1
        tag, text, pair, attr = lines[0]
        assert "bob" in text
        assert "GROUP" in text
        assert "14:32" in text

async def t_render_private_message():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        msg  = DisplayMessage(ts="14:32", scope="private", sender="bob",
                               recipient="alice", text="secret")
        lines = tui._render_message(msg, 80)
        tag, text, pair, attr = lines[0]
        assert "PRIV" in text

async def t_render_system_message():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        msg  = DisplayMessage(ts="14:32", scope="system", sender="",
                               recipient="", text="peer joined")
        lines = tui._render_message(msg, 80)
        tag, text, pair, attr = lines[0]
        assert "peer joined" in text

async def t_render_own_message():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        msg  = DisplayMessage(ts="14:32", scope="group", sender="alice",
                               recipient="", text="my message", is_own=True)
        lines = tui._render_message(msg, 80)
        tag, text, pair, attr = lines[0]
        assert "→" in text   # own message gets arrow indicator

async def t_render_delivered_tick():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        msg  = DisplayMessage(ts="14:32", scope="group", sender="alice",
                               recipient="", text="delivered msg",
                               is_own=True, delivered=True)
        lines = tui._render_message(msg, 80)
        full_text = " ".join(t for _, t, _, _ in lines)
        assert "✓" in full_text

async def t_render_long_message_wraps():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        msg  = DisplayMessage(ts="14:32", scope="group", sender="bob",
                               recipient="", text="word " * 20)
        lines = tui._render_message(msg, 40)
        assert len(lines) > 1


# ══════════════════════════════════════════════════════════════════════════════
# 3. Event queue handling
# ══════════════════════════════════════════════════════════════════════════════

async def t_event_message_appended():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        msg  = DisplayMessage(ts="14:32", scope="group", sender="bob",
                               recipient="", text="hello")
        tui._events.put_nowait({"type": "message", "msg": msg})
        tui._drain_events()
        assert len(tui._messages) == 1
        assert tui._messages[0].text == "hello"

async def t_event_peer_joined():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        info = PeerInfo(alias="bob", fingerprint="fp-bob",
                        session_name="test", address="127.0.0.1",
                        port=55400, peer_id="pid-bob")
        tui._events.put_nowait({"type": "peer_joined", "info": info})
        tui._drain_events()
        assert len(tui._peers) == 1
        assert tui._peers[0].alias == "bob"

async def t_event_peer_left():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        tui._peers.append(PeerDisplay(alias="bob", address="127.0.0.1",
                                       port=55400, fp="fp-bob"))
        info = PeerInfo(alias="bob", fingerprint="fp-bob",
                        session_name="test", address="127.0.0.1",
                        port=55400, peer_id="pid-bob")
        tui._events.put_nowait({"type": "peer_left", "info": info})
        tui._drain_events()
        assert len(tui._peers) == 0

async def t_event_typing_start():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        tui._events.put_nowait({"type": "typing_start", "alias": "bob"})
        tui._drain_events()
        assert "bob" in tui._typing_who

async def t_event_typing_stop():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        tui._typing_who = ["bob"]
        tui._events.put_nowait({"type": "typing_stop", "alias": "bob"})
        tui._drain_events()
        assert "bob" not in tui._typing_who

async def t_event_delivery_marks_message():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        msg  = DisplayMessage(ts="14:32", scope="group", sender="alice",
                               recipient="", text="test", is_own=True,
                               msg_id="msg-001")
        tui._messages.append(msg)
        tui._msg_id_map["msg-001"] = msg
        tui._events.put_nowait({"type": "delivered", "msg_id": "msg-001"})
        tui._drain_events()
        assert tui._msg_id_map["msg-001"].delivered

async def t_event_file_offer():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        tui._events.put_nowait({
            "type": "file_offer", "transfer_id": "tid-1",
            "sender": "bob", "name": "report.pdf", "size": 1024 * 1024
        })
        tui._drain_events()
        assert "tid-1" in tui._files
        assert tui._files["tid-1"].direction == "down"
        assert "tid-1" in tui._pending_offers

async def t_event_file_progress():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        tui._files["tid-1"] = FileDisplay("tid-1", "file.txt", "down", "bob")
        tui._events.put_nowait({
            "type": "file_progress", "transfer_id": "tid-1",
            "done": 512, "total": 1024
        })
        tui._drain_events()
        assert tui._files["tid-1"].pct == 50

async def t_event_file_complete():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        tui._files["tid-1"]        = FileDisplay("tid-1", "file.txt", "down", "bob")
        tui._pending_offers["tid-1"] = tui._files["tid-1"]
        tui._events.put_nowait({
            "type": "file_complete", "transfer_id": "tid-1",
            "path": "/tmp/file.txt" if __import__("platform").system() != "Windows" else "C:/Temp/file.txt"
        })
        tui._drain_events()
        assert tui._files["tid-1"].done
        assert tui._files["tid-1"].pct == 100
        assert "tid-1" not in tui._pending_offers

async def t_event_file_error():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        tui._files["tid-1"]          = FileDisplay("tid-1", "file.txt", "down", "bob")
        tui._pending_offers["tid-1"] = tui._files["tid-1"]
        tui._events.put_nowait({
            "type": "file_error", "transfer_id": "tid-1",
            "reason": "SHA-256 mismatch"
        })
        tui._drain_events()
        assert tui._files["tid-1"].failed
        assert "tid-1" not in tui._pending_offers

async def t_scroll_offset_resets_on_new_message():
    with tempfile.TemporaryDirectory() as d:
        node = make_node("alice", "test", d)
        tui  = make_tui(node)
        tui._scroll_offset = 10
        msg = DisplayMessage(ts="14:32", scope="group", sender="bob",
                              recipient="", text="new message")
        tui._events.put_nowait({"type": "message", "msg": msg})
        tui._drain_events()
        assert tui._scroll_offset == 0


# ══════════════════════════════════════════════════════════════════════════════
# 4. Callback wiring — end-to-end with live nodes
# ══════════════════════════════════════════════════════════════════════════════

async def t_wired_callbacks_receive_message():
    """Verify TUI receives messages via wired node callbacks."""
    with tempfile.TemporaryDirectory() as d:
        alice = make_node("alice", "test", d)
        bob   = make_node("bob",   "test", d)

        tui = make_tui(alice)
        tui._wire_callbacks()

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await bob.broadcast_message("hello alice")
        await asyncio.sleep(0.3)

        tui._drain_events()

        texts = [m.text for m in tui._messages if m.scope == "group"]
        assert "hello alice" in texts, f"Expected 'hello alice' in {texts}"

        await alice.stop(); await bob.stop()


async def t_wired_callbacks_peer_list_updates():
    """Peer list in TUI updates when peers join/leave."""
    with tempfile.TemporaryDirectory() as d:
        alice = make_node("alice", "test", d)
        bob   = make_node("bob",   "test", d)

        tui = make_tui(alice)
        tui._wire_callbacks()

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)
        tui._drain_events()

        assert any(p.alias == "bob" for p in tui._peers), \
            f"Expected bob in peers: {[p.alias for p in tui._peers]}"

        await bob.stop()
        await asyncio.sleep(0.3)
        tui._drain_events()

        assert not any(p.alias == "bob" for p in tui._peers)

        await alice.stop()


async def t_wired_callbacks_typing_indicators():
    """Typing indicators flow through wired callbacks into TUI state."""
    with tempfile.TemporaryDirectory() as d:
        alice = make_node("alice", "test", d)
        bob   = make_node("bob",   "test", d)

        tui = make_tui(alice)
        tui._wire_callbacks()

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await bob.send_typing_start()
        await asyncio.sleep(0.2)
        tui._drain_events()
        assert "bob" in tui._typing_who, f"Expected bob in typing_who: {tui._typing_who}"

        await bob.send_typing_stop()
        await asyncio.sleep(0.2)
        tui._drain_events()
        assert "bob" not in tui._typing_who

        await alice.stop(); await bob.stop()


async def t_wired_callbacks_delivery_receipt():
    """Delivery receipt from bob marks alice's message as delivered in history."""
    with tempfile.TemporaryDirectory() as d:
        alice = make_node("alice", "test", d)
        bob   = make_node("bob",   "test", d)

        tui = make_tui(alice)
        tui._wire_callbacks()

        await alice.start()
        bob_port = await bob.start()
        await alice.connect_to("127.0.0.1", bob_port)
        await asyncio.sleep(0.4)

        await alice.broadcast_message("confirm this")
        await asyncio.sleep(0.4)
        tui._drain_events()

        # History should show the message as delivered
        delivered = [r for r in alice.history.get_all() if r.delivered]
        assert len(delivered) >= 1, "Expected at least one delivered message in history"

        await alice.stop(); await bob.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("\n\033[1m━━━  PyMesh Chat — Phase 5 Test Suite  ━━━\033[0m\n")

    sections = [
        ("Word Wrap", [
            ("short text unchanged",           t_wordwrap_short),
            ("exact width unchanged",          t_wordwrap_exact),
            ("long text wrapped on spaces",    t_wordwrap_long),
            ("no-space text hard-cut",         t_wordwrap_no_space),
            ("empty string handled",           t_wordwrap_empty),
        ]),
        ("Message Rendering", [
            ("group message has alias+label",  t_render_group_message),
            ("private message has PRIV label", t_render_private_message),
            ("system message rendered",        t_render_system_message),
            ("own message has arrow",          t_render_own_message),
            ("delivered message has tick",     t_render_delivered_tick),
            ("long message word-wraps",        t_render_long_message_wraps),
        ]),
        ("Event Queue", [
            ("message event appended",         t_event_message_appended),
            ("peer_joined adds to peer list",  t_event_peer_joined),
            ("peer_left removes from list",    t_event_peer_left),
            ("typing_start adds to who list",  t_event_typing_start),
            ("typing_stop removes from list",  t_event_typing_stop),
            ("delivery marks message done",    t_event_delivery_marks_message),
            ("file_offer registered",          t_event_file_offer),
            ("file_progress updates pct",      t_event_file_progress),
            ("file_complete marks done",       t_event_file_complete),
            ("file_error marks failed",        t_event_file_error),
            ("new message resets scroll",      t_scroll_offset_resets_on_new_message),
        ]),
        ("Callback Wiring — Live Nodes", [
            ("message received via callbacks", t_wired_callbacks_receive_message),
            ("peer list updates on join/leave",t_wired_callbacks_peer_list_updates),
            ("typing indicators wired up",     t_wired_callbacks_typing_indicators),
            ("delivery receipt wired up",      t_wired_callbacks_delivery_receipt),
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
