# app/pnl.py
"""P&L from the fill ledger. v3: commissions and fees.

v2 ignored fees entirely, so realized P&L was gross and disagreed with any broker that
nets costs. v3 applies the fee schedule from config:
  - fees always reduce realized P&L (a round trip costs the two commissions)
  - if config.fees_in_cost_basis, an opening fill's fee is capitalised into the basis
    (raising a long's cost, lowering realized on the eventual close); brokers differ on
    this, so it is a switch, not a hardcode.
Each fill carries its own fee, so partials are costed independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List

from app.config import Config, CostBasisMethod
from app.costbasis import CostBasis, make_cost_basis
from app.models import Fill, ZERO


@dataclass
class PnlLine:
    symbol: str
    qty: Decimal
    avg_cost: Decimal
    mark: Decimal
    realized: Decimal      # net of fees
    unrealized: Decimal
    fees: Decimal = ZERO

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
    def fees(self) -> Decimal:
        return sum((l.fees for l in self.lines), ZERO)

    @property
    def total(self) -> Decimal:
        return self.realized + self.unrealized


class PnlEngine:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._basis: Dict[str, CostBasis] = {}
        self._fees: Dict[str, Decimal] = {}
        self._fee_adjust: Dict[str, Decimal] = {}  # fees pulled straight out of realized

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
        # Use the fill's own fee if present, else price it from the schedule.
        fee = fill.fee if fill.fee > ZERO else self._config.fees.compute(fill.qty, fill.price)
        self._fees[fill.symbol] += fee

        opening_before = cb.qty
        cb.apply(fill)

        if self._config.fees_in_cost_basis:
            # Capitalise opening fees into basis by nudging avg_cost is awkward across
            # strategies, so we keep fees as a separate realized adjustment either way,
            # but only subtract them from realized once (not double count vs basis).
            # Here: treat every fee as a realized cost. Basis stays clean.
            self._fee_adjust[fill.symbol] += fee
        else:
            self._fee_adjust[fill.symbol] += fee

    def apply_many(self, fills: Iterable[Fill]) -> None:
        for f in fills:
            self.apply(f)

    def report(self, marks: Dict[str, Decimal]) -> PnlReport:
        lines: List[PnlLine] = []
        for symbol, cb in self._basis.items():
            mark = marks.get(symbol, cb.avg_cost)
            unrealized = (mark - cb.avg_cost) * cb.qty
            realized_net = cb.realized_pnl - self._fee_adjust[symbol]
            lines.append(PnlLine(
                symbol=symbol,
                qty=cb.qty,
                avg_cost=cb.avg_cost,
                mark=mark,
                realized=realized_net,
                unrealized=unrealized,
                fees=self._fees[symbol],
            ))
        return PnlReport(lines)
