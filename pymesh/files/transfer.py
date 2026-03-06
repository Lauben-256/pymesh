# -*- coding: utf-8 -*-
"""
PyMesh Chat — File Transfer Engine (Phase 4)
Handles the full lifecycle of a file transfer on both the sending
and receiving side.

Protocol flow:
  Sender                              Receiver
  ──────                              ────────
  FILE_OFFER (name, size, sha256) ──→
                                  ←── FILE_ACCEPT  (user said yes)
                                   OR FILE_REJECT  (user said no)
  FILE_CHUNK × N              ──→
  FILE_DONE                   ──→
                                  ←── FILE_ACK     (hash verified OK)
                                   OR FILE_ERROR   (hash mismatch)

Each FILE_CHUNK carries:
  transfer_id  : unique ID linking all chunks to the offer
  chunk_index  : 0-based sequence number
  data         : base64-encoded chunk bytes
  size         : byte count of this chunk

Security:
  - Chunks are encrypted by the existing SessionCipher (transparent)
  - SHA-256 of the complete file is sent in FILE_OFFER and
    re-computed by the receiver after the last chunk
  - If hashes differ the file is deleted and FILE_ERROR sent back
  - Partial files never land in the downloads folder

Progress:
  Sender and receiver each track bytes_done / total_bytes
  and call an optional on_progress(transfer_id, bytes_done, total) callback.
"""

import asyncio
import base64
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Dict, Optional

from pymesh.utils.constants import FILE_CHUNK_SIZE, DEFAULT_DOWNLOAD_DIR

log = logging.getLogger(__name__)


# ── State enums ───────────────────────────────────────────────────────────────

class TransferState(Enum):
    PENDING    = auto()   # Offer sent / waiting for accept
    ACTIVE     = auto()   # Chunks flowing
    VERIFYING  = auto()   # All chunks received, checking hash
    DONE       = auto()   # Completed successfully
    REJECTED   = auto()   # Remote peer declined
    FAILED     = auto()   # Error (hash mismatch, IO error, etc.)
    CANCELLED  = auto()   # Cancelled locally


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class OutboundTransfer:
    """Tracks a file we are sending."""
    transfer_id:   str
    file_path:     str
    file_name:     str
    file_size:     int
    sha256:        str
    peer_alias:    str
    peer_fp:       str          # fingerprint of recipient (empty = broadcast)
    state:         TransferState = TransferState.PENDING
    bytes_sent:    int = 0
    chunk_count:   int = 0
    _task:         Optional[asyncio.Task] = field(default=None, repr=False)


@dataclass
class InboundTransfer:
    """Tracks a file we are receiving."""
    transfer_id:   str
    file_name:     str
    file_size:     int
    expected_sha:  str
    sender_alias:  str
    sender_fp:     str
    download_dir:  str
    state:         TransferState = TransferState.PENDING
    bytes_received: int = 0
    chunks_received: int = 0
    _tmp_path:     str = ""      # path to partial file being written
    _out_path:     str = ""      # final destination path
    _hasher:       object = field(default=None, repr=False)

    def __post_init__(self):
        self._hasher = hashlib.sha256()


# ── Transfer manager ──────────────────────────────────────────────────────────

class FileTransferManager:
    """
    Central manager for all in-flight file transfers.
    One instance lives on the Node.

    Callers register callbacks:
      on_offer_received  (transfer_id, sender, filename, size) → None
      on_progress        (transfer_id, bytes_done, total)      → None
      on_complete        (transfer_id, path)                   → None
      on_error           (transfer_id, reason)                 → None
    """

    def __init__(
        self,
        download_dir: str = DEFAULT_DOWNLOAD_DIR,
        on_offer_received: Optional[Callable] = None,
        on_progress:       Optional[Callable] = None,
        on_complete:       Optional[Callable] = None,
        on_error:          Optional[Callable] = None,
    ):
        self.download_dir      = download_dir
        self._on_offer         = on_offer_received
        self._on_progress      = on_progress
        self._on_complete      = on_complete
        self._on_error         = on_error

        self._outbound: Dict[str, OutboundTransfer] = {}
        self._inbound:  Dict[str, InboundTransfer]  = {}

        os.makedirs(download_dir, mode=0o755, exist_ok=True)

    # ── Outbound: prepare offer ───────────────────────────────────────────────

    def prepare_offer(
        self,
        file_path: str,
        peer_alias: str,
        peer_fp: str = "",
    ) -> OutboundTransfer:
        """
        Validate the file, compute its SHA-256, and create an OutboundTransfer.
        The caller is responsible for sending FILE_OFFER over the wire.
        Raises FileNotFoundError or ValueError on problems.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if not path.is_file():
            raise ValueError(f"Not a file: {file_path}")

        file_size = path.stat().st_size
        if file_size == 0:
            raise ValueError("Cannot send empty file")

        sha256 = _sha256_file(file_path)
        transfer_id = str(uuid.uuid4())

        transfer = OutboundTransfer(
            transfer_id = transfer_id,
            file_path   = str(path.resolve()),
            file_name   = path.name,
            file_size   = file_size,
            sha256      = sha256,
            peer_alias  = peer_alias,
            peer_fp     = peer_fp,
        )
        self._outbound[transfer_id] = transfer
        log.info(
            "Prepared offer: %s → %s (%s, sha256=%s...)",
            path.name, peer_alias, _fmt_size(file_size), sha256[:16]
        )
        return transfer

    # ── Outbound: send chunks ─────────────────────────────────────────────────

    async def send_chunks(
        self,
        transfer: OutboundTransfer,
        send_fn: Callable,          # async (msg: dict) -> None
    ) -> bool:
        """
        Read the file in FILE_CHUNK_SIZE chunks and call send_fn for each.
        Sends FILE_DONE at the end.
        Returns True on success, False on failure.
        """
        transfer.state = TransferState.ACTIVE
        chunk_index = 0

        try:
            with open(transfer.file_path, "rb") as fh:
                while True:
                    chunk = fh.read(FILE_CHUNK_SIZE)
                    if not chunk:
                        break

                    msg = {
                        "type":        "FILE_CHUNK",
                        "transfer_id": transfer.transfer_id,
                        "chunk_index": chunk_index,
                        "data":        base64.b64encode(chunk).decode("ascii"),
                        "size":        len(chunk),
                    }
                    await send_fn(msg)

                    transfer.bytes_sent += len(chunk)
                    chunk_index         += 1
                    transfer.chunk_count = chunk_index

                    if self._on_progress:
                        self._on_progress(
                            transfer.transfer_id,
                            transfer.bytes_sent,
                            transfer.file_size,
                        )

                    # Yield to event loop — don't starve other connections
                    await asyncio.sleep(0)

            # All chunks sent — send FILE_DONE
            await send_fn({
                "type":        "FILE_DONE",
                "transfer_id": transfer.transfer_id,
                "chunk_count": chunk_index,
            })

            transfer.state = TransferState.VERIFYING
            log.info(
                "All chunks sent for %s (%d chunks, %s)",
                transfer.file_name, chunk_index, _fmt_size(transfer.bytes_sent)
            )
            return True

        except OSError as exc:
            transfer.state = TransferState.FAILED
            log.error("IO error reading %s: %s", transfer.file_path, exc)
            if self._on_error:
                self._on_error(transfer.transfer_id, f"Read error: {exc}")
            return False

    # ── Outbound: handle incoming ACK / ERROR ─────────────────────────────────

    def handle_file_ack(self, transfer_id: str) -> None:
        t = self._outbound.get(transfer_id)
        if not t:
            return
        t.state = TransferState.DONE
        log.info("Transfer %s confirmed by receiver", transfer_id[:8])
        if self._on_complete:
            self._on_complete(transfer_id, t.file_path)

    def handle_file_error(self, transfer_id: str, reason: str) -> None:
        t = self._outbound.get(transfer_id)
        if not t:
            return
        t.state = TransferState.FAILED
        log.warning("Transfer %s failed on receiver: %s", transfer_id[:8], reason)
        if self._on_error:
            self._on_error(transfer_id, reason)

    def handle_file_reject(self, transfer_id: str) -> None:
        t = self._outbound.get(transfer_id)
        if not t:
            return
        t.state = TransferState.REJECTED
        log.info("Transfer %s rejected by %s", transfer_id[:8], t.peer_alias)

    # ── Inbound: register incoming offer ─────────────────────────────────────

    def register_offer(
        self,
        transfer_id:  str,
        file_name:    str,
        file_size:    int,
        expected_sha: str,
        sender_alias: str,
        sender_fp:    str,
    ) -> InboundTransfer:
        """Register an incoming FILE_OFFER. Returns the InboundTransfer."""
        # Sanitise the filename — strip path components
        safe_name = Path(file_name).name or "unnamed_file"

        transfer = InboundTransfer(
            transfer_id  = transfer_id,
            file_name    = safe_name,
            file_size    = file_size,
            expected_sha = expected_sha,
            sender_alias = sender_alias,
            sender_fp    = sender_fp,
            download_dir = self.download_dir,
        )

        # Temp path (partial) and final path
        transfer._tmp_path = os.path.join(self.download_dir, f".{transfer_id}.part")
        transfer._out_path = _unique_path(self.download_dir, safe_name)

        self._inbound[transfer_id] = transfer
        log.info(
            "Incoming offer: %s from %s (%s)",
            safe_name, sender_alias, _fmt_size(file_size)
        )

        if self._on_offer:
            self._on_offer(transfer_id, sender_alias, safe_name, file_size)

        return transfer

    def accept_transfer(self, transfer_id: str) -> bool:
        t = self._inbound.get(transfer_id)
        if not t or t.state != TransferState.PENDING:
            return False
        t.state = TransferState.ACTIVE
        # Open the temp file for writing
        try:
            open(t._tmp_path, "wb").close()   # touch / truncate
        except OSError as exc:
            log.error("Cannot create temp file %s: %s", t._tmp_path, exc)
            t.state = TransferState.FAILED
            return False
        return True

    def reject_transfer(self, transfer_id: str) -> bool:
        t = self._inbound.get(transfer_id)
        if not t:
            return False
        t.state = TransferState.REJECTED
        self._cleanup_tmp(t)
        return True

    # ── Inbound: receive chunks ───────────────────────────────────────────────

    def handle_chunk(self, msg: dict) -> Optional[str]:
        """
        Process one FILE_CHUNK message.
        Returns an error string if something is wrong, else None.
        """
        transfer_id = msg.get("transfer_id", "")
        t = self._inbound.get(transfer_id)

        if not t:
            return f"Unknown transfer_id: {transfer_id[:8]}"
        if t.state != TransferState.ACTIVE:
            return f"Transfer {transfer_id[:8]} not active (state={t.state.name})"

        try:
            data = base64.b64decode(msg["data"])
        except Exception as exc:
            return f"Bad chunk data: {exc}"

        try:
            with open(t._tmp_path, "ab") as fh:
                fh.write(data)
        except OSError as exc:
            t.state = TransferState.FAILED
            self._cleanup_tmp(t)
            return f"Write error: {exc}"

        t._hasher.update(data)
        t.bytes_received  += len(data)
        t.chunks_received += 1

        if self._on_progress:
            self._on_progress(transfer_id, t.bytes_received, t.file_size)

        return None

    def handle_done(self, msg: dict) -> tuple:
        """
        Process FILE_DONE — verify SHA-256, move file to final path.
        Returns (success: bool, error_reason: str or None, final_path: str or None)
        """
        transfer_id = msg.get("transfer_id", "")
        t = self._inbound.get(transfer_id)

        if not t:
            return False, f"Unknown transfer_id: {transfer_id[:8]}", None
        if t.state != TransferState.ACTIVE:
            return False, f"Transfer not active", None

        t.state = TransferState.VERIFYING

        # Verify SHA-256
        actual_sha = t._hasher.hexdigest()
        if actual_sha != t.expected_sha:
            t.state = TransferState.FAILED
            self._cleanup_tmp(t)
            reason = (
                f"SHA-256 mismatch for {t.file_name}: "
                f"expected {t.expected_sha[:16]}... "
                f"got {actual_sha[:16]}..."
            )
            log.error(reason)
            if self._on_error:
                self._on_error(transfer_id, reason)
            return False, reason, None

        # Hash OK — move temp file to final destination
        try:
            os.rename(t._tmp_path, t._out_path)
        except OSError as exc:
            t.state = TransferState.FAILED
            self._cleanup_tmp(t)
            reason = f"Could not move file to downloads: {exc}"
            if self._on_error:
                self._on_error(transfer_id, reason)
            return False, reason, None

        t.state = TransferState.DONE
        log.info(
            "Transfer complete: %s → %s (%s)",
            t.file_name, t._out_path, _fmt_size(t.bytes_received)
        )
        if self._on_complete:
            self._on_complete(transfer_id, t._out_path)

        return True, None, t._out_path

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_outbound(self, transfer_id: str) -> Optional[OutboundTransfer]:
        return self._outbound.get(transfer_id)

    def get_inbound(self, transfer_id: str) -> Optional[InboundTransfer]:
        return self._inbound.get(transfer_id)

    def active_transfers(self) -> list:
        out = [t for t in self._outbound.values() if t.state == TransferState.ACTIVE]
        inc = [t for t in self._inbound.values()  if t.state == TransferState.ACTIVE]
        return out + inc

    # ── Internal ─────────────────────────────────────────────────────────────

    def _cleanup_tmp(self, t: InboundTransfer) -> None:
        if t._tmp_path and os.path.exists(t._tmp_path):
            try:
                os.remove(t._tmp_path)
            except OSError:
                pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    """Compute SHA-256 of a file. Reads in chunks to handle large files."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(FILE_CHUNK_SIZE)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _fmt_size(n: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _unique_path(directory: str, filename: str) -> str:
    """
    Return a unique file path — if filename already exists,
    append (1), (2), etc. before the extension.
    """
    base = Path(directory) / filename
    if not base.exists():
        return str(base)
    stem = base.stem
    suffix = base.suffix
    counter = 1
    while True:
        candidate = Path(directory) / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return str(candidate)
        counter += 1
