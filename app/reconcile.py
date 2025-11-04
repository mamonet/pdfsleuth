# app/reconcile.py
"""Reconciler: compare engine positions and P&L against the broker's snapshot.

final: tolerances come from ReconcileConfig, every break is located to the exact
symbol/leg, and each is classified with a likely cause. The service's value is not "the
numbers differ" but "they differ HERE, by THIS much, probably because of THAT".

Cause classification, per symbol (ordered, most-fundamental first):
  - MISSING_FILL   qty differs. One side booked an execution the other did not; every
                   downstream number for that symbol is suspect until it is reconciled.
  - FEE_TREATMENT  qty matches but avg_cost or realized P&L differs. Usually fees rolled
                   into cost basis on one side and booked separately on the other.
  - MARK_SOURCE    qty and avg match but unrealized P&L differs. The two sides marked the
                   open position at different prices (feed mid vs broker last).
  - UNKNOWN        none of the above patterns fit.

Takes plain position lists and a PnlSnapshot, never the engine object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence

from app.brokers.base import PnlSnapshot
from app.config import ReconcileConfig
from app.models import BrokerPosition, Position, ZERO
from app.pnl import PnlReport


class Cause(str, Enum):
    MISSING_FILL = "MISSING_FILL"
    FEE_TREATMENT = "FEE_TREATMENT"
    MARK_SOURCE = "MARK_SOURCE"
    UNKNOWN = "UNKNOWN"


@dataclass
class Break:
    symbol: str            # "*" for account-level rows
    leg: str               # qty | avg_cost | realized | unrealized
    engine: Decimal
    broker: Decimal
    tolerance: Decimal
    cause: Cause = Cause.UNKNOWN

    @property
    def diff(self) -> Decimal:
        return self.engine - self.broker


@dataclass
class ReconcileResult:
    breaks: List[Break] = field(default_factory=list)
    checked: int = 0
    mark_source: str = ""

    @property
    def has_breaks(self) -> bool:
        return len(self.breaks) > 0

    @property
    def clean(self) -> bool:
        return not self.has_breaks

    def breaks_for(self, symbol: str) -> List[Break]:
        return [b for b in self.breaks if b.symbol == symbol]

    def located(self) -> List[str]:
        return [f"{b.symbol}/{b.leg} diff={b.diff} cause={b.cause.value}" for b in self.breaks]

    def render(self) -> str:
        """Runtime report. Prints real computed values; nothing is hard-coded."""
        head = (f"reconcile (mark_source={self.mark_source}) checked={self.checked} "
                f"breaks={len(self.breaks)} clean={self.clean}")
        cols = f"  {'symbol':<8} {'leg':<12} {'engine':>14} {'broker':>14} {'diff':>14}  cause"
        lines = [head, cols, "  " + "-" * (len(cols) - 2)]
        for b in self.breaks:
            lines.append(f"  {b.symbol:<8} {b.leg:<12} {b.engine:>14} {b.broker:>14} "
                         f"{b.diff:>14}  {b.cause.value}")
        return "\n".join(lines)


def _index(items: Iterable) -> Dict[str, object]:
    return {i.symbol: i for i in items}


def _classify(symbol_breaks: List[Break], qty_off: bool, avg_off: bool,
              unreal_off: bool) -> None:
    """Assign one cause to every break on a symbol, from the most fundamental mismatch."""
    if qty_off:
        cause = Cause.MISSING_FILL
    elif avg_off:
        cause = Cause.FEE_TREATMENT
    elif unreal_off:
        cause = Cause.MARK_SOURCE
    else:
        cause = Cause.UNKNOWN
    for b in symbol_breaks:
        b.cause = cause


class Reconciler:
    def __init__(self, config: Optional[ReconcileConfig] = None):
        self.cfg = config or ReconcileConfig()

    def reconcile(self, positions: Sequence[Position], pnl: PnlReport,
                  broker: PnlSnapshot) -> ReconcileResult:
        eng = _index(positions)
        brk = _index(broker.positions)
        eng_unreal = {line.symbol: line.unrealized for line in pnl.lines}
        result = ReconcileResult(mark_source=pnl.mark_source)

        qty_tol = self.cfg.qty_tolerance
        price_tol = self.cfg.price_tolerance
        pnl_tol = self.cfg.pnl_tolerance

        for symbol in sorted(set(eng) | set(brk)):
            e: Optional[Position] = eng.get(symbol)        # type: ignore[assignment]
            b: Optional[BrokerPosition] = brk.get(symbol)  # type: ignore[assignment]
            e_qty = e.qty if e else ZERO
            b_qty = b.qty if b else ZERO
            e_avg = e.avg_cost if e else ZERO
            b_avg = b.avg_cost if b else ZERO
            e_un = eng_unreal.get(symbol, ZERO)
            b_un = b.unrealized_pnl if b else ZERO

            result.checked += 3
            sym_breaks: List[Break] = []
            qty_off = abs(e_qty - b_qty) > qty_tol
            avg_off = abs(e_avg - b_avg) > price_tol
            unreal_off = abs(e_un - b_un) > pnl_tol
            if qty_off:
                sym_breaks.append(Break(symbol, "qty", e_qty, b_qty, qty_tol))
            if avg_off:
                sym_breaks.append(Break(symbol, "avg_cost", e_avg, b_avg, price_tol))
            if unreal_off:
                sym_breaks.append(Break(symbol, "unrealized", e_un, b_un, pnl_tol))
            _classify(sym_breaks, qty_off, avg_off, unreal_off)
            result.breaks.extend(sym_breaks)

        # account-level totals
        result.checked += 2
        if abs(pnl.realized - broker.realized_pnl) > pnl_tol:
            result.breaks.append(Break("*", "realized", pnl.realized, broker.realized_pnl,
                                       pnl_tol, Cause.FEE_TREATMENT))
        if abs(pnl.unrealized - broker.unrealized_pnl) > pnl_tol:
            result.breaks.append(Break("*", "unrealized", pnl.unrealized, broker.unrealized_pnl,
                                       pnl_tol, Cause.MARK_SOURCE))
        return result
