# app/feeds/mock.py
"""Mock market-data feed: reconnect-safe WebSocket-style async mark stream.

No network, no credentials. Deterministic walk around each symbol's base price.

Reconnect-safe behaviour (what a real feed must also do):
  - The underlying "socket" can drop (simulated by drop_every). stream_marks() catches it,
    backs off exponentially (capped), reconnects, and resumes without raising to the caller.
  - Gap detection: the first mark after a reconnect is flagged gap=True so the engine knows
    the price may have moved unobserved and should trust it as a fresh anchor, not assume
    continuity.
  - Last-good-mark retained: a subscriber can always read the last price via last_mark()
    even while disconnected, so unrealized P&L never blanks out during an outage.
"""

from __future__ import annotations

import asyncio
import random
from decimal import Decimal
from typing import AsyncIterator, Dict, Iterable, Optional

from .base import Feed, Mark

ZERO = Decimal("0")


class _SocketDropped(Exception):
    """Internal signal that the simulated transport died."""


class MockFeed(Feed):
    name = "mock"

    def __init__(
        self,
        base_prices: Optional[Dict[str, Decimal]] = None,
        interval: float = 0.05,
        seed: int = 7,
        drop_every: int = 0,            # 0 = never drop; N = drop after N marks
        backoff_base: float = 0.02,
        backoff_cap: float = 1.0,
    ):
        self._base: Dict[str, Decimal] = dict(base_prices or {"AAPL": Decimal("190.00")})
        self._last: Dict[str, Mark] = {}
        self._subs: set[str] = set()
        self._interval = interval
        self._rng = random.Random(seed)
        self._drop_every = drop_every
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._emitted = 0

    async def subscribe(self, symbols: Iterable[str]) -> None:
        for s in symbols:
            self._subs.add(s)
            if s not in self._last:
                px = self._base.get(s, Decimal("100.00"))
                self._last[s] = Mark(symbol=s, price=px)

    def last_mark(self, symbol: str) -> Optional[Mark]:
        """Last good mark, available even mid-outage. None if never seen."""
        return self._last.get(symbol)

    def _step(self, symbol: str, gap: bool = False) -> Mark:
        prev = self._last[symbol].price
        drift = Decimal(self._rng.randint(-1, 1)) * Decimal("0.01")
        px = (prev + drift).quantize(Decimal("0.01"))
        if px <= ZERO:
            px = Decimal("0.01")
        mark = Mark(symbol=symbol, price=px, gap=gap)
        self._last[symbol] = mark
        return mark

    async def _emit_once(self, gap: bool) -> list[Mark]:
        """One transport cycle. Raises _SocketDropped to simulate a lost connection."""
        out: list[Mark] = []
        first = gap
        for symbol in sorted(self._subs):
            out.append(self._step(symbol, gap=first))
            first = False
            self._emitted += 1
            if self._drop_every and self._emitted % self._drop_every == 0:
                raise _SocketDropped()
        return out

    async def stream_marks(self) -> AsyncIterator[Mark]:
        backoff = self._backoff_base
        gap_next = False  # flag the first batch after any reconnect
        while True:
            try:
                if not self._subs:
                    await asyncio.sleep(self._interval)
                    continue
                batch = await self._emit_once(gap_next)
                gap_next = False
                backoff = self._backoff_base  # healthy cycle resets backoff
                for mark in batch:
                    yield mark
                await asyncio.sleep(self._interval)
            except _SocketDropped:
                # reconnect with capped exponential backoff; do not raise to the consumer
                await asyncio.sleep(min(backoff, self._backoff_cap))
                backoff = min(backoff * 2, self._backoff_cap)
                gap_next = True  # next real mark is a post-gap anchor
