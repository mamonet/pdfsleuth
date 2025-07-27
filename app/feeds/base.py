# app/feeds/base.py
"""Market-data feed interface.

The feed supplies marks (a current reference price per symbol) that the P&L engine uses
for unrealized P&L. Like brokers, feeds sit behind this interface so a real one
(e.g. a Polygon.io WebSocket) drops in without the engine knowing.

Contract a real feed must honour
--------------------------------
1. Marks are Decimal. Parse provider JSON with Decimal(str(px)).
2. subscribe() is additive and idempotent: subscribing to a symbol twice is a no-op.
3. stream_marks() is an async iterator that yields Mark(symbol, price, ts) as they arrive.
   It must be reconnect-safe: on a dropped socket it reconnects with backoff and resumes
   without raising into the consumer. Gaps are surfaced via Mark.gap so the engine knows a
   value may be stale rather than fresh.
4. Never block the event loop; wrap blocking client SDKs in an executor.

Endpoint and key come from config/env with placeholder defaults. No real URL or key here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Iterable


@dataclass(frozen=True)
class Mark:
    """A single mark print. gap=True means this value follows a detected feed gap and the
    engine should treat prior marks as possibly stale."""

    symbol: str
    price: Decimal
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    gap: bool = False


class Feed(ABC):
    name: str = "abstract"

    @abstractmethod
    async def subscribe(self, symbols: Iterable[str]) -> None:
        """Register interest in one or more symbols. Idempotent."""

    @abstractmethod
    def stream_marks(self) -> AsyncIterator[Mark]:
        """Async iterator of marks. Reconnect-safe; never raises a transient socket error
        into the consumer."""

    async def close(self) -> None:
        """Release the connection. Default no-op."""
        return None
