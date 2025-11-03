# app/reconcile.py
"""Reconciler: compare engine positions and P&L against the broker's snapshot.

v2: adds realized and unrealized P&L. Per symbol we now compare qty, avg_cost and
unrealized P&L; at the account level, realized and unrealized totals. Still no tolerance
config or cause classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Sequence

from app.brokers.base import PnlSnapshot
from app.models import BrokerPosition, Position, ZERO
from app.pnl import PnlReport


@dataclass
class Break:
    symbol: str            # "*" for account-level
    leg: str               # qty | avg_cost | realized | unrealized
    engine: Decimal
    broker: Decimal

    @property
    def diff(self) -> Decimal:
        return self.engine - self.broker


@dataclass
class ReconcileResult:
    breaks: List[Break] = field(default_factory=list)
    checked: int = 0

    @property
    def has_breaks(self) -> bool:
        return len(self.breaks) > 0

    @property
    def clean(self) -> bool:
        return not self.has_breaks

    def render(self) -> str:
        lines = [f"reconcile: checked={self.checked} breaks={len(self.breaks)}"]
        for b in self.breaks:
            lines.append(f"  {b.symbol}/{b.leg}: engine={b.engine} broker={b.broker} "
                         f"diff={b.diff}")
        return "\n".join(lines)


def _index(items: Iterable) -> Dict[str, object]:
    return {i.symbol: i for i in items}


class Reconciler:
    def __init__(self, qty_tolerance: Decimal = ZERO,
                 price_tolerance: Decimal = Decimal("0.0001"),
                 pnl_tolerance: Decimal = Decimal("0.01")):
        self.qty_tol = qty_tolerance
        self.price_tol = price_tolerance
        self.pnl_tol = pnl_tolerance

    def reconcile(self, positions: Sequence[Position], pnl: PnlReport,
                  broker: PnlSnapshot) -> ReconcileResult:
        eng = _index(positions)
        brk = _index(broker.positions)
        eng_unreal = {l.symbol: l.unrealized for l in pnl.lines}
        result = ReconcileResult()

        for symbol in sorted(set(eng) | set(brk)):
            e = eng.get(symbol)
            b = brk.get(symbol)
            e_qty = e.qty if e else ZERO
            b_qty = b.qty if b else ZERO
            e_avg = e.avg_cost if e else ZERO
            b_avg = b.avg_cost if b else ZERO
            e_un = eng_unreal.get(symbol, ZERO)
            b_un = b.unrealized_pnl if b else ZERO
            result.checked += 3
            if abs(e_qty - b_qty) > self.qty_tol:
                result.breaks.append(Break(symbol, "qty", e_qty, b_qty))
            if abs(e_avg - b_avg) > self.price_tol:
                result.breaks.append(Break(symbol, "avg_cost", e_avg, b_avg))
            if abs(e_un - b_un) > self.pnl_tol:
                result.breaks.append(Break(symbol, "unrealized", e_un, b_un))

        result.checked += 2
        if abs(pnl.realized - broker.realized_pnl) > self.pnl_tol:
            result.breaks.append(Break("*", "realized", pnl.realized, broker.realized_pnl))
        if abs(pnl.unrealized - broker.unrealized_pnl) > self.pnl_tol:
            result.breaks.append(Break("*", "unrealized", pnl.unrealized, broker.unrealized_pnl))
        return result
