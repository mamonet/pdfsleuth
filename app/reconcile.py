# app/reconcile.py
"""Reconciler: compare engine positions against the broker's book, emit a diff.

v1: positions only. For each symbol either side knows, compare net qty and average cost
and record a Break where they differ. No P&L, no cause classification yet.

Takes plain position lists, not the engine, so it never drags a broker/feed import into
the engine and is trivial to test in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Sequence

from app.brokers.base import PnlSnapshot
from app.models import BrokerPosition, Position, ZERO


@dataclass
class Break:
    symbol: str
    leg: str            # "qty" | "avg_cost"
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
    def __init__(self, qty_tolerance: Decimal = ZERO, price_tolerance: Decimal = Decimal("0.0001")):
        self.qty_tol = qty_tolerance
        self.price_tol = price_tolerance

    def reconcile(self, positions: Sequence[Position], broker: PnlSnapshot) -> ReconcileResult:
        eng = _index(positions)
        brk = _index(broker.positions)
        result = ReconcileResult()
        for symbol in sorted(set(eng) | set(brk)):
            e = eng.get(symbol)
            b = brk.get(symbol)
            e_qty = e.qty if e else ZERO
            b_qty = b.qty if b else ZERO
            e_avg = e.avg_cost if e else ZERO
            b_avg = b.avg_cost if b else ZERO
            result.checked += 2
            if abs(e_qty - b_qty) > self.qty_tol:
                result.breaks.append(Break(symbol, "qty", e_qty, b_qty))
            if abs(e_avg - b_avg) > self.price_tol:
                result.breaks.append(Break(symbol, "avg_cost", e_avg, b_avg))
        return result
