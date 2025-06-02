# app/positions.py
"""Positions from the fill ledger. final: correct average cost on closes.

DEFECT in v1: apply() blended every fill into the average price, including closing
fills. Selling part of a long at a different price dragged the cost basis of the
remaining shares, so unrealized P&L on the rest was wrong and no realized P&L was
ever booked. A round trip that should show a clean profit instead smeared it.

FIX: average cost moves ONLY on an opening or increasing fill. A closing (reducing)
fill leaves avg_cost untouched and instead realizes P&L on the closed quantity:
    realized += (exit_price - avg_cost) * closed_qty * position_direction
A fill that crosses through zero is split into a close of the old side (realized at
the old avg_cost) and an open of the new side (avg_cost := fill price).
"""

from __future__ import annotations

from decimal import Decimal
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
        pos = self.get(fill.symbol)
        delta = fill.signed_qty
        old_qty = pos.qty

        opening = old_qty == ZERO or (old_qty > ZERO) == (delta > ZERO)

        if opening:
            # Increasing exposure on the same side: blend cost only over the added qty.
            old_abs = old_qty.copy_abs()
            new_abs = old_abs + fill.qty
            pos.avg_cost = (pos.avg_cost * old_abs + fill.price * fill.qty) / new_abs
            pos.qty = old_qty + delta
        else:
            # Reducing or flipping. avg_cost of the remaining shares does NOT change.
            direction = Decimal(1) if old_qty > ZERO else Decimal(-1)
            closed = min(fill.qty, old_qty.copy_abs())
            # Realized P&L is priced off the untouched basis, not the new fill.
            pos.realized_pnl += (fill.price - pos.avg_cost) * closed * direction

            new_qty = old_qty + delta
            if new_qty == ZERO:
                pos.avg_cost = ZERO          # flat: no basis to carry
            elif (new_qty > ZERO) != (old_qty > ZERO):
                # Crossed through zero. Overshoot opens the new side at the fill price.
                pos.avg_cost = fill.price
            # else: partial close, basis of the survivors is unchanged. Leave avg_cost.
            pos.qty = new_qty

        pos.fees_paid += fill.fee
        pos.updated_at = fill.ts
        return pos

    def apply_many(self, fills: Iterable[Fill]) -> None:
        for f in fills:
            self.apply(f)
