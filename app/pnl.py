# app/pnl.py
"""P&L from the fill ledger. v1: realized + unrealized on average cost.

Realized P&L accrues as positions close; unrealized is the open position marked to a
supplied price. This version hardcodes average cost and takes marks as a plain dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List

from app.models import Fill, Position, ZERO
from app.positions import PositionBook


@dataclass
class PnlLine:
    symbol: str
    qty: Decimal
    avg_cost: Decimal
    mark: Decimal
    realized: Decimal
    unrealized: Decimal

    @property
    def total(self) -> Decimal:
        return self.realized + self.unrealized


@dataclass
class PnlReport:
    lines: List[PnlLine]

    @property
    def realized(self) -> Decimal:
        return sum((l.realized for l in self.lines), ZERO)

    @property
    def unrealized(self) -> Decimal:
        return sum((l.unrealized for l in self.lines), ZERO)

    @property
    def total(self) -> Decimal:
        return self.realized + self.unrealized


class PnlEngine:
    def __init__(self) -> None:
        self._book = PositionBook()

    def apply(self, fill: Fill) -> None:
        self._book.apply(fill)

    def apply_many(self, fills: Iterable[Fill]) -> None:
        for f in fills:
            self._book.apply(f)

    def report(self, marks: Dict[str, Decimal]) -> PnlReport:
        lines: List[PnlLine] = []
        for pos in self._book.all():
            mark = marks.get(pos.symbol, pos.avg_cost)
            lines.append(PnlLine(
                symbol=pos.symbol,
                qty=pos.qty,
                avg_cost=pos.avg_cost,
                mark=mark,
                realized=pos.realized_pnl,
                unrealized=pos.unrealized(mark),
            ))
        return PnlReport(lines)
