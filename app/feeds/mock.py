# app/feeds/mock.py
"""Mock market-data feed: a WebSocket-style async mark stream. No network, no credentials.

v1: subscribe to symbols, then stream_marks() yields a deterministic random-ish walk around
each symbol's base price. Async generator with a small sleep to mimic a socket cadence.
"""

from __future__ import annotations

import asyncio
import random
from decimal import Decimal
from typing import AsyncIterator, Dict, Iterable, Optional

from .base import Feed, Mark

ZERO = Decimal("0")


class MockFeed(Feed):
    name = "mock"

    def __init__(
        self,
        base_prices: Optional[Dict[str, Decimal]] = None,
        interval: float = 0.05,
        seed: int = 7,
    ):
        self._base: Dict[str, Decimal] = dict(base_prices or {"AAPL": Decimal("190.00")})
        self._last: Dict[str, Decimal] = dict(self._base)
        self._subs: set[str] = set()
        self._interval = interval
        self._rng = random.Random(seed)

    async def subscribe(self, symbols: Iterable[str]) -> None:
        for s in symbols:
            self._subs.add(s)
            self._last.setdefault(s, self._base.get(s, Decimal("100.00")))

    def _step(self, symbol: str) -> Decimal:
        # +/- one cent walk, quantized
        drift = Decimal(self._rng.randint(-1, 1)) * Decimal("0.01")
        px = (self._last[symbol] + drift).quantize(Decimal("0.01"))
        if px <= ZERO:
            px = Decimal("0.01")
        self._last[symbol] = px
        return px

    async def stream_marks(self) -> AsyncIterator[Mark]:
        while True:
            if not self._subs:
                await asyncio.sleep(self._interval)
                continue
            for symbol in sorted(self._subs):
                yield Mark(symbol=symbol, price=self._step(symbol))
            await asyncio.sleep(self._interval)
