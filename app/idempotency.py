# app/idempotency.py
"""Submit idempotency. A retry or a duplicate submit must resolve to the SAME order,
never a second live order at the venue.

The client_order_id is derived deterministically from the order's economic intent, so
retrying the same submit produces the same key. The registry remembers key -> order_id;
a second submit under a known key returns the existing order instead of sending again.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional

from app.models import Order


def derive_key(symbol: str, side: str, qty: Decimal, order_type: str,
               limit_price: Optional[Decimal], nonce: str) -> str:
    """Deterministic key over the fields that define the order.

    nonce lets a caller who genuinely wants a second identical order get a distinct key;
    a retry of the same intent reuses the same nonce and so collapses onto one order.
    """
    parts = [symbol, side, str(qty), order_type, str(limit_price or ""), nonce]
    raw = "|".join(parts).encode("utf-8")
    return "cid_" + hashlib.sha256(raw).hexdigest()[:24]


@dataclass
class _Entry:
    order_id: str
    sent: bool  # whether this key has actually been dispatched to the broker


class IdempotencyRegistry:
    """key -> order mapping. Thread-safe; the reserve/commit split closes the window
    where two threads race the same key.
    """

    def __init__(self) -> None:
        self._by_key: Dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[_Entry]:
        with self._lock:
            e = self._by_key.get(key)
            return _Entry(e.order_id, e.sent) if e else None

    def reserve(self, key: str, order: Order) -> _Entry:
        """Claim a key for an order before sending. If the key already exists, return
        the existing entry and DO NOT overwrite; the caller must not send again.
        """
        with self._lock:
            existing = self._by_key.get(key)
            if existing is not None:
                return _Entry(existing.order_id, existing.sent)
            order.client_order_id = key
            entry = _Entry(order.order_id, sent=False)
            self._by_key[key] = entry
            return entry

    def mark_sent(self, key: str) -> None:
        """Flip the entry to sent once the broker has accepted the dispatch."""
        with self._lock:
            e = self._by_key.get(key)
            if e is not None:
                e.sent = True

    def is_known(self, key: str) -> bool:
        with self._lock:
            return key in self._by_key


class DuplicateSubmit(Exception):
    """Raised when a caller insists on sending a key that is already in flight."""

    def __init__(self, key: str, order_id: str):
        self.key = key
        self.order_id = order_id
        super().__init__(f"submit for key {key} already maps to order {order_id}")
