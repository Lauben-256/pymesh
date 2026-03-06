"""
PyMesh Chat — Message History
In-memory log of all messages. Capped at MAX_HISTORY entries.
Never written to disk — privacy by default.

Each MessageRecord holds:
  id        : unique message id
  ts        : UTC millisecond timestamp
  scope     : "group" or "private"
  sender    : alias
  recipient : alias (private only)
  text      : message content
  is_own    : True if we sent this
  delivered : set of peer fingerprints that ACKed
"""

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Set

from pymesh.utils.constants import MAX_HISTORY


@dataclass
class MessageRecord:
    id:        str
    ts:        int
    scope:     str
    sender:    str
    recipient: Optional[str]
    text:      str
    is_own:    bool
    delivered: Set[str] = field(default_factory=set)

    @property
    def ts_display(self) -> str:
        t = time.localtime(self.ts / 1000)
        return f"{t.tm_hour:02d}:{t.tm_min:02d}"


class MessageHistory:
    """In-memory message log, capped at MAX_HISTORY entries."""

    def __init__(self):
        self._log: deque = deque(maxlen=MAX_HISTORY)

    def add(
        self,
        scope: str,
        sender: str,
        text: str,
        ts: int = None,
        recipient: str = None,
        is_own: bool = False,
        msg_id: str = None,
    ) -> MessageRecord:
        record = MessageRecord(
            id        = msg_id or str(uuid.uuid4()),
            ts        = ts or int(time.time() * 1000),
            scope     = scope,
            sender    = sender,
            recipient = recipient,
            text      = text,
            is_own    = is_own,
        )
        self._log.append(record)
        return record

    def mark_delivered(self, msg_id: str, fingerprint: str) -> bool:
        for record in self._log:
            if record.id == msg_id:
                record.delivered.add(fingerprint)
                return True
        return False

    def get_recent(self, n: int = 50) -> List[MessageRecord]:
        return list(self._log)[-n:]

    def get_all(self) -> List[MessageRecord]:
        return list(self._log)

    def clear(self) -> None:
        self._log.clear()

    def __len__(self) -> int:
        return len(self._log)
