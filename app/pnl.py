# app/pnl.py
"""P&L from the fill ledger. v2: pluggable cost basis (average vs FIFO).

v1 was welded to average cost via PositionBook. v2 routes each symbol's fills through
a costbasis strategy chosen by config, so realized P&L follows the configured method.
The per-symbol CostBasis object holds qty, avg_cost and realized; we mark the open qty
to compute unrealized.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List

from app.config import CostBasisMethod
from app.costbasis import CostBasis, make_cost_basis
from app.models import Fill, ZERO


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
    def __init__(self, method: CostBasisMethod = CostBasisMethod.AVERAGE) -> None:
        self._method = method
        self._basis: Dict[str, CostBasis] = {}

    def _for(self, symbol: str) -> CostBasis:
        cb = self._basis.get(symbol)
        if cb is None:
            cb = make_cost_basis(self._method, symbol)
            self._basis[symbol] = cb
        return cb

    def apply(self, fill: Fill) -> None:
        self._for(fill.symbol).apply(fill)

    def apply_many(self, fills: Iterable[Fill]) -> None:
        for f in fills:
            self.apply(f)

    def report(self, marks: Dict[str, Decimal]) -> PnlReport:
        lines: List[PnlLine] = []
        for symbol, cb in self._basis.items():
            mark = marks.get(symbol, cb.avg_cost)
            unrealized = (mark - cb.avg_cost) * cb.qty  # qty carries sign
            lines.append(PnlLine(
                symbol=symbol,
                qty=cb.qty,
                avg_cost=cb.avg_cost,
                mark=mark,
                realized=cb.realized_pnl,
                unrealized=unrealized,
            ))
        return PnlReport(lines)
