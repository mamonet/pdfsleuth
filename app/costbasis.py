# app/costbasis.py
"""Cost-basis strategies behind one interface.

Both answer the same two questions for a symbol's fill sequence:
  - what is the average cost of the open position
  - how much realized P&L does a closing fill release, and against which basis

AVERAGE keeps a single blended basis. FIFO keeps an explicit lot queue and closes
the oldest lots first, so realized P&L depends on WHICH shares are deemed sold. Under
different-priced lots the two methods produce different realized P&L from identical
fills, which is a routine source of a P&L that disagrees with a broker on a method mismatch.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Deque, List

from app.models import Fill, Side, ZERO


@dataclass
class CloseResult:
    realized_pnl: Decimal
    closed_qty: Decimal


class CostBasis(ABC):
    """One symbol's basis state. Feed it fills; read qty/avg_cost/realized off it."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.qty: Decimal = ZERO          # signed
        self.realized_pnl: Decimal = ZERO

    @property
    @abstractmethod
    def avg_cost(self) -> Decimal:
        ...

    @abstractmethod
    def apply(self, fill: Fill) -> CloseResult:
        """Apply a fill; return the realized P&L and closed qty it produced (0 if opening)."""
        ...


class AverageCostBasis(CostBasis):
    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self._avg: Decimal = ZERO

    @property
    def avg_cost(self) -> Decimal:
        return self._avg

    def apply(self, fill: Fill) -> CloseResult:
        delta = fill.signed_qty
        old = self.qty
        opening = old == ZERO or (old > ZERO) == (delta > ZERO)

        if opening:
            old_abs = old.copy_abs()
            new_abs = old_abs + fill.qty
            self._avg = (self._avg * old_abs + fill.price * fill.qty) / new_abs
            self.qty = old + delta
            return CloseResult(ZERO, ZERO)

        direction = Decimal(1) if old > ZERO else Decimal(-1)
        closed = min(fill.qty, old.copy_abs())
        realized = (fill.price - self._avg) * closed * direction
        self.realized_pnl += realized
        new_qty = old + delta
        if new_qty == ZERO:
            self._avg = ZERO
        elif (new_qty > ZERO) != (old > ZERO):
            self._avg = fill.price
        self.qty = new_qty
        return CloseResult(realized, closed)


@dataclass
class _Lot:
    qty: Decimal        # always positive; side tracked by the book's sign
    price: Decimal


class FifoCostBasis(CostBasis):
    """Explicit lot queue. Oldest lots close first. Longs and shorts each queue on
    their own side; a fill on the opposite side consumes lots front-to-back.
    """

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self._lots: Deque[_Lot] = deque()
        self._side_sign: int = 0   # +1 long book, -1 short book, 0 flat

    @property
    def avg_cost(self) -> Decimal:
        total_qty = sum((lot.qty for lot in self._lots), ZERO)
        if total_qty == ZERO:
            return ZERO
        total_cost = sum((lot.qty * lot.price for lot in self._lots), ZERO)
        return total_cost / total_qty

    def apply(self, fill: Fill) -> CloseResult:
        delta_sign = fill.side.sign
        remaining = fill.qty
        realized = ZERO
        closed_total = ZERO

        if self._side_sign == 0:
            self._side_sign = delta_sign

        if delta_sign == self._side_sign:
            # Same side: just a new lot.
            self._lots.append(_Lot(remaining, fill.price))
            self.qty += fill.signed_qty
            return CloseResult(ZERO, ZERO)

        # Opposite side: consume lots oldest-first.
        while remaining > ZERO and self._lots:
            lot = self._lots[0]
            take = min(remaining, lot.qty)
            # Long book closed by a sell: (sell - buy). Short book closed by a buy: (sell - buy)
            # with the short's entry as the "sell". _side_sign gives the direction.
            realized += (fill.price - lot.price) * take * Decimal(self._side_sign)
            lot.qty -= take
            remaining -= take
            closed_total += take
            if lot.qty == ZERO:
                self._lots.popleft()

        self.realized_pnl += realized
        self.qty += fill.signed_qty

        if remaining > ZERO:
            # Flipped through zero: the leftover opens a fresh lot on the new side.
            self._side_sign = delta_sign
            self._lots.append(_Lot(remaining, fill.price))
        elif not self._lots:
            self._side_sign = 0

        return CloseResult(realized, closed_total)


def make_cost_basis(method: str, symbol: str) -> CostBasis:
    from app.config import CostBasisMethod

    m = CostBasisMethod(method) if not isinstance(method, CostBasisMethod) else method
    if m is CostBasisMethod.FIFO:
        return FifoCostBasis(symbol)
    return AverageCostBasis(symbol)
