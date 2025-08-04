# app/marks.py
"""Current mark per symbol, resilient to feed gaps.

A mark is (price, source, timestamp). The store keeps the last good mark per symbol
per source, so a WebSocket gap does not blank the price; instead the mark goes STALE
(age past max_age) and callers can see that and discount unrealized P&L. Never
invents a price to fill a gap.

Mark source is not decided here; the store holds whatever sources the feed/broker
supply and hands back the one the P&L engine asks for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional, Tuple

from app.config import MarkSource
from app.models import ZERO


@dataclass
class Mark:
    symbol: str
    price: Decimal
    source: MarkSource
    ts: float                 # epoch seconds
    max_age: float

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.ts)

    @property
    def is_stale(self) -> bool:
        return self.age > self.max_age


class MarkStore:
    def __init__(self, max_age_seconds: float = 5.0,
                 clock=time.time) -> None:
        self._max_age = max_age_seconds
        self._clock = clock
        # keyed by (symbol, source) so mid and last are retained independently
        self._marks: Dict[Tuple[str, MarkSource], Mark] = {}

    def update(self, symbol: str, price: Decimal, source: MarkSource,
               ts: Optional[float] = None) -> Mark:
        """Record a fresh mark. Overwrites the last-good for that symbol+source."""
        if price < ZERO:
            raise ValueError(f"mark price must be non-negative, got {price}")
        mark = Mark(symbol=symbol, price=price, source=source,
                    ts=ts if ts is not None else self._clock(),
                    max_age=self._max_age)
        self._marks[(symbol, source)] = mark
        return mark

    def update_bid_ask(self, symbol: str, bid: Decimal, ask: Decimal,
                       ts: Optional[float] = None) -> Mark:
        """Convenience: store the mid from a top-of-book update."""
        mid = (bid + ask) / Decimal(2)
        return self.update(symbol, mid, MarkSource.FEED_MID, ts)

    def get(self, symbol: str, source: MarkSource) -> Optional[Mark]:
        """Last-good mark for the requested source, or None if never seen. A returned
        mark may be stale; check mark.is_stale.
        """
        return self._marks.get((symbol, source))

    def price(self, symbol: str, source: MarkSource) -> Optional[Decimal]:
        m = self.get(symbol, source)
        return m.price if m is not None else None

    def is_stale(self, symbol: str, source: MarkSource) -> bool:
        m = self.get(symbol, source)
        return m is None or m.is_stale

    def staleness(self, symbol: str, source: MarkSource) -> Optional[float]:
        """Age in seconds of the current mark, or None if we have never had one."""
        m = self.get(symbol, source)
        return m.age if m is not None else None
