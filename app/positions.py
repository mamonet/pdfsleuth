# app/positions.py
"""Positions from the fill ledger. v1: net qty + average cost.

Fills are applied in order; the book holds one Position per symbol. Net qty is the
signed sum of fills. Average cost is maintained as a running qty-weighted mean of
every execution price seen on the symbol.
"""

from __future__ import annotations

from typing import Dict, Iterable

from app.models import Fill, Position, ZERO


class PositionBook:
    def __init__(self) -> None:
        self._positions: Dict[str, Position] = {}

    def get(self, symbol: str) -> Position:
        pos = self._positions.get(symbol)
        if pos is None:
            pos = Position(symbol=symbol)
            self._positions[symbol] = pos
        return pos

    def all(self) -> Iterable[Position]:
        return list(self._positions.values())

    def apply(self, fill: Fill) -> Position:
        """Fold one fill into the symbol's position.

        v1 keeps a running weighted-average price across ALL fills and nets the signed
        quantity. Simple, and it makes the net qty right. (The average-cost handling is
        wrong on closing fills; positions.final fixes that.)
        """
        pos = self.get(fill.symbol)
        old_abs = pos.qty.copy_abs()
        # Blend every fill's price into the average, regardless of open/close.
        total_qty = old_abs + fill.qty
        pos.avg_cost = (pos.avg_cost * old_abs + fill.price * fill.qty) / total_qty
        pos.qty = pos.qty + fill.signed_qty
        pos.fees_paid += fill.fee
        pos.updated_at = fill.ts
        return pos

    def apply_many(self, fills: Iterable[Fill]) -> None:
        for f in fills:
            self.apply(f)
