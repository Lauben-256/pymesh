"""
PyMesh Chat — Node (Phase 4)
Adds file transfer: send/receive files with SHA-256 integrity,
progress tracking, accept/decline prompts, and broadcast support.
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Callable, Dict, List, Optional

from pymesh.core.listener import Listener
from pymesh.core.connector import Connector
from pymesh.core.peer import PeerConnection, PeerInfo, PeerState
from pymesh.core.handshake import perform_handshake, HandshakeError
from pymesh.core.discovery import DiscoveryService
from pymesh.core.protocol import build_message
from pymesh.crypto.identity import Identity
from pymesh.crypto.trust import TrustStore, KeyChangedError
from pymesh.messaging.history import MessageHistory
from pymesh.files.transfer import FileTransferManager, _fmt_size
from pymesh.utils.constants import (
    DEFAULT_PORT,
    DEFAULT_INACTIVITY_TIMEOUT,
    DEFAULT_DOWNLOAD_DIR,
    MSG_CHAT,
    MSG_ACK,
    MSG_TYPING_START,
    MSG_TYPING_STOP,
    MSG_FILE_OFFER,
    MSG_FILE_ACCEPT,
    MSG_FILE_REJECT,
    MSG_FILE_CHUNK,
    MSG_FILE_DONE,
    MSG_FILE_ACK,
    MSG_FILE_ERROR,
    SESSION_NAME_MAX_LEN,
    ALIAS_MAX_LEN,
)

log = logging.getLogger(__name__)


class Node:
    def __init__(
        self,
        alias: str,
        session_name: str,
        identity: Identity,
        port: int = DEFAULT_PORT,
        inactivity_timeout: int = DEFAULT_INACTIVITY_TIMEOUT,
        download_dir: str = DEFAULT_DOWNLOAD_DIR,
        on_message: Optional[Callable] = None,
        on_peer_joined: Optional[Callable] = None,
        on_peer_left: Optional[Callable] = None,
        on_warn_timeout: Optional[Callable] = None,
        on_tofu_prompt: Optional[Callable] = None,
        on_key_changed: Optional[Callable] = None,
        on_delivery: Optional[Callable] = None,
        on_typing_start: Optional[Callable] = None,
        on_typing_stop: Optional[Callable] = None,
        on_file_offer: Optional[Callable] = None,
        on_file_progress: Optional[Callable] = None,
        on_file_complete: Optional[Callable] = None,
        on_file_error: Optional[Callable] = None,
        on_file_rejected: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        trust_store_path: str = None,
    ):
        self.alias        = alias.strip()[:ALIAS_MAX_LEN]
        self.session_name = session_name.strip()[:SESSION_NAME_MAX_LEN]
        self.node_id      = str(uuid.uuid4())
        self.identity     = identity
        self.fingerprint  = identity.fingerprint

        self._port               = port
        self._inactivity_timeout = inactivity_timeout
        self._trust_store        = TrustStore(trust_store_path)
        self.history             = MessageHistory()

        self._on_message       = on_message
        self._on_peer_joined   = on_peer_joined
        self._on_peer_left     = on_peer_left
        self._on_warn_timeout  = on_warn_timeout
        self._on_tofu_prompt   = on_tofu_prompt
        self._on_key_changed   = on_key_changed
        self._on_delivery      = on_delivery
        self._on_typing_start  = on_typing_start
        self._on_typing_stop   = on_typing_stop
        self._on_error         = on_error
        self._on_file_offer    = on_file_offer
        self._on_file_progress = on_file_progress
        self._on_file_complete = on_file_complete
        self._on_file_error    = on_file_error
        self._on_file_rejected = on_file_rejected

        self.files = FileTransferManager(
            download_dir      = download_dir,
            on_offer_received = self._sync_file_offer,
            on_progress       = self._sync_file_progress,
            on_complete       = self._sync_file_complete,
            on_error          = self._sync_file_error,
        )

        self._peers: Dict[str, PeerConnection] = {}
        self._peers_lock = asyncio.Lock()
        self._connecting: set = set()
        self._transfer_peer: Dict[str, str] = {}

        self._listener  = Listener(on_new_connection=self._on_new_connection, port=port)
        self._connector = Connector(on_new_connection=self._on_new_connection)
        self._discovery: Optional[DiscoveryService] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> int:
        bound_port = await self._listener.start()
        self._port = bound_port
        self._discovery = DiscoveryService(
            alias=self.alias, session_name=self.session_name,
            fingerprint=self.fingerprint, port=bound_port,
            on_peer_found=self._on_peer_discovered,
            on_peer_lost=self._on_peer_lost_mdns,
            loop=asyncio.get_running_loop(),
        )
        await self._discovery.start()
        self._running = True
        log.info("Node started: alias=%s port=%d", self.alias, bound_port)
        return bound_port

    async def stop(self) -> None:
        self._running = False
        if self._discovery:
            await self._discovery.stop()
        async with self._peers_lock:
            peers = list(self._peers.values())
        for conn in peers:
            await conn.disconnect(reason="node shutting down")
        await self._listener.stop()
        log.info("Node stopped")

    # ── Chat ──────────────────────────────────────────────────────────────────

    async def broadcast_message(self, text: str) -> int:
        msg_id = str(uuid.uuid4())
        ts     = int(time.time() * 1000)
        msg    = build_message(
            MSG_CHAT, msg_id=msg_id, scope="group",
            sender_alias=self.alias, sender_fingerprint=self.fingerprint,
            text=text, ts=ts, recipient_fingerprint=None,
        )
        self.history.add(scope="group", sender=self.alias, text=text,
                         ts=ts, is_own=True, msg_id=msg_id)
        sent = 0
        async with self._peers_lock:
            peers = list(self._peers.values())
        for conn in peers:
            if conn.state == PeerState.ACTIVE:
                await conn.send(msg)
                sent += 1
        return sent

    async def send_private_message(self, target_fingerprint: str, text: str) -> bool:
        conn = await self._find_peer_by_fingerprint(target_fingerprint)
        if not conn or not conn.info:
            return False
        msg_id = str(uuid.uuid4())
        ts     = int(time.time() * 1000)
        msg    = build_message(
            MSG_CHAT, msg_id=msg_id, scope="private",
            sender_alias=self.alias, sender_fingerprint=self.fingerprint,
            text=text, ts=ts, recipient_fingerprint=target_fingerprint,
        )
        self.history.add(scope="private", sender=self.alias, text=text,
                         ts=ts, recipient=conn.info.alias, is_own=True, msg_id=msg_id)
        await conn.send(msg)
        return True

    async def send_typing_start(self) -> None:
        msg = build_message(MSG_TYPING_START, sender_alias=self.alias)
        async with self._peers_lock:
            peers = list(self._peers.values())
        for conn in peers:
            if conn.state == PeerState.ACTIVE:
                await conn.send(msg)

    async def send_typing_stop(self) -> None:
        msg = build_message(MSG_TYPING_STOP, sender_alias=self.alias)
        async with self._peers_lock:
            peers = list(self._peers.values())
        for conn in peers:
            if conn.state == PeerState.ACTIVE:
                await conn.send(msg)

    # ── File transfer — outbound ──────────────────────────────────────────────

    async def send_file(self, file_path: str, target_alias: str) -> Optional[str]:
        """Send a file to one peer. Returns transfer_id or None."""
        conn = await self._find_peer_by_alias(target_alias)
        if not conn or not conn.info:
            log.warning("send_file: no peer named '%s'", target_alias)
            return None
        try:
            transfer = self.files.prepare_offer(
                file_path=file_path, peer_alias=target_alias,
                peer_fp=conn.info.fingerprint,
            )
        except (FileNotFoundError, ValueError) as exc:
            log.error("send_file: %s", exc)
            if self._on_file_error:
                await self._on_file_error("", str(exc))
            return None

        self._transfer_peer[transfer.transfer_id] = conn.info.peer_id
        await conn.send(build_message(
            MSG_FILE_OFFER,
            transfer_id  = transfer.transfer_id,
            file_name    = transfer.file_name,
            file_size    = transfer.file_size,
            sha256       = transfer.sha256,
            sender_alias = self.alias,
        ))
        log.info("FILE_OFFER sent: %s → %s", transfer.file_name, target_alias)
        return transfer.transfer_id

    async def broadcast_file(self, file_path: str) -> List[str]:
        """Send a file to all connected peers. Returns list of transfer_ids."""
        async with self._peers_lock:
            peers = list(self._peers.values())
        active = [c for c in peers if c.state == PeerState.ACTIVE and c.info]
        if not active:
            return []
        ids = []
        for conn in active:
            tid = await self.send_file(file_path, conn.info.alias)
            if tid:
                ids.append(tid)
        return ids

    async def accept_file(self, transfer_id: str) -> bool:
        """Accept an incoming FILE_OFFER."""
        t = self.files.get_inbound(transfer_id)
        if not t:
            return False
        if not self.files.accept_transfer(transfer_id):
            return False
        conn = await self._find_peer_by_fingerprint(t.sender_fp)
        if not conn:
            self.files.reject_transfer(transfer_id)
            return False
        await conn.send(build_message(MSG_FILE_ACCEPT, transfer_id=transfer_id))
        log.info("Accepted transfer %s from %s", transfer_id[:8], t.sender_alias)
        return True

    async def reject_file(self, transfer_id: str) -> bool:
        """Decline an incoming FILE_OFFER."""
        t = self.files.get_inbound(transfer_id)
        if not t:
            return False
        self.files.reject_transfer(transfer_id)
        conn = await self._find_peer_by_fingerprint(t.sender_fp)
        if conn:
            await conn.send(build_message(MSG_FILE_REJECT, transfer_id=transfer_id))
        log.info("Rejected transfer %s", transfer_id[:8])
        return True

    # ── Peer registry ─────────────────────────────────────────────────────────

    async def get_peers(self) -> List[PeerInfo]:
        async with self._peers_lock:
            return [
                conn.info for conn in self._peers.values()
                if conn.state == PeerState.ACTIVE and conn.info is not None
            ]

    @property
    def peer_count(self) -> int:
        return len(self._peers)

    # ── Internal: connection lifecycle ────────────────────────────────────────

    async def _on_new_connection(self, reader, writer, is_initiator: bool) -> None:
        peername    = writer.get_extra_info("peername")
        remote_ip   = peername[0] if peername else "unknown"
        remote_port = peername[1] if peername else 0

        try:
            peer_info, cipher = await perform_handshake(
                reader=reader, writer=writer, is_initiator=is_initiator,
                local_alias=self.alias, local_fingerprint=self.fingerprint,
                local_session=self.session_name, identity=self.identity,
            )
        except HandshakeError as exc:
            log.warning("Handshake failed: %s", exc)
            try: writer.close()
            except Exception: pass
            return

        peer_info.address = remote_ip
        peer_info.port    = remote_port

        if not await self._check_trust(peer_info, writer):
            return

        async with self._peers_lock:
            if peer_info.peer_id in self._peers:
                try: writer.close()
                except Exception: pass
                return

        conn = PeerConnection(
            reader=reader, writer=writer, is_initiator=is_initiator,
            cipher=cipher, inactivity_timeout=self._inactivity_timeout,
            on_message=self._handle_peer_message,
            on_disconnect=self._handle_peer_disconnect,
            on_warn_timeout=self._handle_warn_timeout,
        )
        conn.info = peer_info
        conn.activate()

        async with self._peers_lock:
            self._peers[peer_info.peer_id] = conn

        log.info("Peer joined: %s @ %s:%d", peer_info.alias, remote_ip, remote_port)
        await conn.start()
        if self._on_peer_joined:
            await self._on_peer_joined(peer_info)

    # ── Internal: message routing ─────────────────────────────────────────────

    async def _handle_peer_message(self, conn: PeerConnection, msg: dict) -> None:
        t = msg.get("type", "")
        if   t == MSG_CHAT:          await self._handle_chat(conn, msg)
        elif t == MSG_ACK:           await self._handle_ack(conn, msg)
        elif t == MSG_TYPING_START:
            alias = msg.get("sender_alias", conn.info.alias if conn.info else "?")
            if self._on_typing_start: await self._on_typing_start(alias)
        elif t == MSG_TYPING_STOP:
            alias = msg.get("sender_alias", conn.info.alias if conn.info else "?")
            if self._on_typing_stop:  await self._on_typing_stop(alias)
        elif t == MSG_FILE_OFFER:    await self._handle_file_offer(conn, msg)
        elif t == MSG_FILE_ACCEPT:   await self._handle_file_accept(conn, msg)
        elif t == MSG_FILE_REJECT:   await self._handle_file_reject(conn, msg)
        elif t == MSG_FILE_CHUNK:    await self._handle_file_chunk(conn, msg)
        elif t == MSG_FILE_DONE:     await self._handle_file_done(conn, msg)
        elif t == MSG_FILE_ACK:      self.files.handle_file_ack(msg.get("transfer_id",""))
        elif t == MSG_FILE_ERROR:
            self.files.handle_file_error(msg.get("transfer_id",""), msg.get("reason",""))
        else:
            if self._on_message and conn.info:
                await self._on_message(conn.info, msg)

    async def _handle_chat(self, conn, msg):
        if not conn.info: return
        msg_id = msg.get("msg_id", str(uuid.uuid4()))
        scope  = msg.get("scope", "group")
        alias  = msg.get("sender_alias", conn.info.alias)
        text   = msg.get("text", "")
        ts     = msg.get("ts", int(time.time() * 1000))
        self.history.add(scope=scope, sender=alias, text=text, ts=ts,
                         recipient=self.alias if scope=="private" else None,
                         is_own=False, msg_id=msg_id)
        await conn.send(build_message(MSG_ACK, msg_id=msg_id,
                                      sender_fingerprint=self.fingerprint))
        if self._on_message: await self._on_message(conn.info, msg)

    async def _handle_ack(self, conn, msg):
        msg_id    = msg.get("msg_id", "")
        sender_fp = msg.get("sender_fingerprint", "")
        alias     = conn.info.alias if conn.info else "?"
        if msg_id:
            self.history.mark_delivered(msg_id, sender_fp)
            if self._on_delivery: await self._on_delivery(msg_id, alias)

    async def _handle_file_offer(self, conn, msg):
        if not conn.info: return
        self.files.register_offer(
            transfer_id  = msg.get("transfer_id", ""),
            file_name    = msg.get("file_name", "unknown"),
            file_size    = msg.get("file_size", 0),
            expected_sha = msg.get("sha256", ""),
            sender_alias = msg.get("sender_alias", conn.info.alias),
            sender_fp    = conn.info.fingerprint,
        )
        self._transfer_peer[msg.get("transfer_id","")] = conn.info.peer_id

    async def _handle_file_accept(self, conn, msg):
        transfer_id = msg.get("transfer_id", "")
        transfer    = self.files.get_outbound(transfer_id)
        if not transfer:
            return
        log.info("%s accepted %s — sending chunks",
                 conn.info.alias if conn.info else "?", transfer.file_name)

        async def _send():
            ok = await self.files.send_chunks(transfer, conn.send)
            if not ok:
                await conn.send(build_message(MSG_FILE_ERROR,
                                              transfer_id=transfer_id,
                                              reason="Sender IO error"))
        asyncio.create_task(_send(), name=f"send-{transfer_id[:8]}")

    async def _handle_file_reject(self, conn, msg):
        transfer_id = msg.get("transfer_id", "")
        peer_alias  = conn.info.alias if conn.info else "?"
        self.files.handle_file_reject(transfer_id)
        if self._on_file_rejected:
            await self._on_file_rejected(transfer_id, peer_alias)

    async def _handle_file_chunk(self, conn, msg):
        error = self.files.handle_chunk(msg)
        if error:
            await conn.send(build_message(MSG_FILE_ERROR,
                                          transfer_id=msg.get("transfer_id",""),
                                          reason=error))

    async def _handle_file_done(self, conn, msg):
        transfer_id        = msg.get("transfer_id", "")
        success, err, path = self.files.handle_done(msg)
        if success:
            await conn.send(build_message(MSG_FILE_ACK, transfer_id=transfer_id))
        else:
            await conn.send(build_message(MSG_FILE_ERROR,
                                          transfer_id=transfer_id, reason=err))

    # ── Trust ─────────────────────────────────────────────────────────────────

    async def _check_trust(self, peer_info: PeerInfo, writer) -> bool:
        pub_key_hex = self._trust_store.get_public_key(peer_info.fingerprint) or ""
        try:
            status = self._trust_store.check(
                alias=peer_info.alias,
                fingerprint=peer_info.fingerprint,
                public_key_hex=pub_key_hex,
            )
        except KeyChangedError as exc:
            log.warning("KEY CHANGE: %s", exc)
            if self._on_key_changed:
                await self._on_key_changed(exc.alias, exc.known_fp, exc.new_fp)
            try: writer.close()
            except Exception: pass
            return False

        if status == "new":
            if self._on_tofu_prompt:
                accepted = await self._on_tofu_prompt(
                    peer_info.alias, peer_info.fingerprint, pub_key_hex
                )
                if not accepted:
                    try: writer.close()
                    except Exception: pass
                    return False
            self._trust_store.trust(peer_info.alias, peer_info.fingerprint, pub_key_hex)
        return True

    async def _handle_peer_disconnect(self, conn: PeerConnection) -> None:
        if not conn.info: return
        async with self._peers_lock:
            self._peers.pop(conn.info.peer_id, None)
        log.info("Peer left: %s", conn.info.alias)
        if self._on_peer_left: await self._on_peer_left(conn.info)

    async def _handle_warn_timeout(self, conn: PeerConnection, secs: int) -> None:
        if self._on_warn_timeout and conn.info:
            await self._on_warn_timeout(conn.info, secs)

    # ── Sync → async bridges for FileTransferManager callbacks ───────────────

    def _sync_file_offer(self, tid, sender, name, size):
        if self._on_file_offer:
            asyncio.create_task(self._on_file_offer(tid, sender, name, size))

    def _sync_file_progress(self, tid, done, total):
        if self._on_file_progress:
            asyncio.create_task(self._on_file_progress(tid, done, total))

    def _sync_file_complete(self, tid, path):
        if self._on_file_complete:
            asyncio.create_task(self._on_file_complete(tid, path))

    def _sync_file_error(self, tid, reason):
        if self._on_file_error:
            asyncio.create_task(self._on_file_error(tid, reason))

    # ── mDNS & helpers ───────────────────────────────────────────────────────

    async def _on_peer_discovered(self, host, port, info):
        if not self._running: return
        if info.get("session","").lower() != self.session_name.lower(): return
        async with self._peers_lock:
            existing = [c for c in self._peers.values() if c.info and c.info.address==host]
        if existing: return
        await self.connect_to(host, port)

    async def _on_peer_lost_mdns(self, fp): pass

    async def connect_to(self, host: str, port: int = DEFAULT_PORT) -> bool:
        key = f"{host}:{port}"
        if key in self._connecting: return False
        self._connecting.add(key)
        try:
            return await self._connector.connect(host, port)
        finally:
            self._connecting.discard(key)

    async def _find_peer_by_fingerprint(self, fp):
        async with self._peers_lock:
            for conn in self._peers.values():
                if conn.info and conn.info.fingerprint == fp:
                    return conn
        return None

    async def _find_peer_by_alias(self, alias):
        async with self._peers_lock:
            for conn in self._peers.values():
                if conn.info and conn.info.alias.lower() == alias.lower():
                    return conn
        return None
