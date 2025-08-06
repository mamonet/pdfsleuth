# app/pnl.py
"""P&L from the fill ledger. final: configurable mark source.

v3 marked positions off whatever dict was passed in. final resolves the mark through
config.mark_source against the MarkStore, which can serve a feed mid, a feed last, or
the broker's own last. This matters because unrealized P&L is (mark - avg_cost) * qty,
and two systems that agree on every fill will STILL show different unrealized P&L if
one marks to the feed mid and the other to the broker's last print. In practice that
mark-source difference is often the entire reason a P&L "disagrees" with the broker,
so it is surfaced explicitly (mark + staleness) rather than hidden.

Realized P&L is unaffected by mark source; only unrealized moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from app.config import Config, MarkSource
from app.costbasis import CostBasis, make_cost_basis
from app.marks import MarkStore
from app.models import Fill, ZERO


@dataclass
class PnlLine:
    symbol: str
    qty: Decimal
    avg_cost: Decimal
    mark: Decimal
    mark_source: str
    mark_stale: bool
    realized: Decimal      # net of fees
    unrealized: Decimal
    fees: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.realized + self.unrealized


@dataclass
class PnlReport:
    lines: List[PnlLine]
    mark_source: str

    @property
    def realized(self) -> Decimal:
        return sum((l.realized for l in self.lines), ZERO)

    @property
    def unrealized(self) -> Decimal:
        # Unrealized on a stale mark is untrustworthy; still reported, but callers can
        # see mark_stale per line and discount it.
        return sum((l.unrealized for l in self.lines), ZERO)

    @property
    def fees(self) -> Decimal:
        return sum((l.fees for l in self.lines), ZERO)

    @property
    def total(self) -> Decimal:
        return self.realized + self.unrealized


class PnlEngine:
    def __init__(self, config: Config, marks: Optional[MarkStore] = None) -> None:
        self._config = config
        self._marks = marks or MarkStore(max_age_seconds=config.reconcile.max_mark_age_seconds)
        self._basis: Dict[str, CostBasis] = {}
        self._fees: Dict[str, Decimal] = {}
        self._fee_adjust: Dict[str, Decimal] = {}

    @property
    def marks(self) -> MarkStore:
        return self._marks

    def _for(self, symbol: str) -> CostBasis:
        cb = self._basis.get(symbol)
        if cb is None:
            cb = make_cost_basis(self._config.cost_basis, symbol)
            self._basis[symbol] = cb
            self._fees[symbol] = ZERO
            self._fee_adjust[symbol] = ZERO
        return cb

    def apply(self, fill: Fill) -> None:
        cb = self._for(fill.symbol)
        fee = fill.fee if fill.fee > ZERO else self._config.fees.compute(fill.qty, fill.price)
        self._fees[fill.symbol] += fee
        cb.apply(fill)
        # Fees reduce realized P&L. fees_in_cost_basis is honoured by MarkStore/basis
        # setup upstream; here fees are a straight realized deduction.
        self._fee_adjust[fill.symbol] += fee

    def apply_many(self, fills: Iterable[Fill]) -> None:
        for f in fills:
            self.apply(f)

    def _resolve_mark(self, symbol: str, avg_cost: Decimal) -> tuple[Decimal, bool]:
        """Pick the mark per config.mark_source. Fall back to avg_cost if we have none,
        which zeroes unrealized rather than inventing a price.
        """
        src = self._config.mark_source
        m = self._marks.get(symbol, src)
        if m is None:
            return avg_cost, True
        return m.price, m.is_stale

    def report(self) -> PnlReport:
        lines: List[PnlLine] = []
        for symbol, cb in self._basis.items():
            mark, stale = self._resolve_mark(symbol, cb.avg_cost)
            unrealized = (mark - cb.avg_cost) * cb.qty
            realized_net = cb.realized_pnl - self._fee_adjust[symbol]
            lines.append(PnlLine(
                symbol=symbol,
                qty=cb.qty,
                avg_cost=cb.avg_cost,
                mark=mark,
                mark_source=self._config.mark_source.value,
                mark_stale=stale,
                realized=realized_net,
                unrealized=unrealized,
                fees=self._fees[symbol],
            ))
        return PnlReport(lines, mark_source=self._config.mark_source.value)
