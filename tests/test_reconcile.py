# tests/test_reconcile.py
"""Reconciliation: zero breaks on a matched book, and a non-zero break located to the
right symbol/leg with a plausible cause on an injected mismatch."""

from __future__ import annotations

from decimal import Decimal

from app.brokers.base import PnlSnapshot
from app.config import ReconcileConfig
from app.models import BrokerPosition, Position, ZERO
from app.pnl import PnlLine, PnlReport
from app.reconcile import Cause, Reconciler


def _position(symbol, qty, avg):
    return Position(symbol=symbol, qty=Decimal(qty), avg_cost=Decimal(avg))


def _pnl(symbol, qty, avg, realized, unrealized):
    line = PnlLine(symbol=symbol, qty=Decimal(qty), avg_cost=Decimal(avg),
                   mark=Decimal(avg), mark_source="FEED_MID", mark_stale=False,
                   realized=Decimal(realized), unrealized=Decimal(unrealized))
    return PnlReport([line], mark_source="FEED_MID")


def _broker(symbol, qty, avg, unrealized, realized="0"):
    bp = BrokerPosition(symbol=symbol, qty=Decimal(qty), avg_cost=Decimal(avg),
                        unrealized_pnl=Decimal(unrealized))
    return PnlSnapshot(realized_pnl=Decimal(realized), unrealized_pnl=Decimal(unrealized),
                       positions=[bp])


def test_matched_book_has_no_breaks():
    rec = Reconciler(ReconcileConfig())
    result = rec.reconcile(
        positions=[_position("AAPL", "100", "190.00")],
        pnl=_pnl("AAPL", "100", "190.00", "0", "100.00"),
        broker=_broker("AAPL", "100", "190.00", "100.00"),
    )
    assert result.clean
    assert not result.has_breaks


def test_avg_drift_located_and_classified():
    rec = Reconciler(ReconcileConfig())
    result = rec.reconcile(
        positions=[_position("AAPL", "100", "190.00")],
        pnl=_pnl("AAPL", "100", "190.00", "0", "100.00"),
        broker=_broker("AAPL", "100", "190.50", "100.00"),   # avg off by 0.50, qty matches
    )
    assert result.has_breaks
    aapl = result.breaks_for("AAPL")
    legs = {b.leg for b in aapl}
    assert "avg_cost" in legs
    assert "qty" not in legs                       # located to the avg, not the qty
    avg_break = next(b for b in aapl if b.leg == "avg_cost")
    assert avg_break.diff == Decimal("-0.50")
    assert avg_break.cause is Cause.FEE_TREATMENT


def test_qty_mismatch_reads_as_missing_fill():
    rec = Reconciler(ReconcileConfig())
    result = rec.reconcile(
        positions=[_position("AAPL", "100", "190.00")],
        pnl=_pnl("AAPL", "100", "190.00", "0", "0"),
        broker=_broker("AAPL", "80", "190.00", "0"),         # broker missed 20 shares
    )
    qty_break = next(b for b in result.breaks_for("AAPL") if b.leg == "qty")
    assert qty_break.diff == Decimal("20")
    assert qty_break.cause is Cause.MISSING_FILL


def test_tolerance_absorbs_tiny_diff():
    # price tolerance of a cent swallows a half-cent avg drift; unrealized agrees.
    rec = Reconciler(ReconcileConfig(price_tolerance=Decimal("0.01")))
    result = rec.reconcile(
        positions=[_position("AAPL", "100", "190.00")],
        pnl=_pnl("AAPL", "100", "190.00", "0", "100.00"),
        broker=_broker("AAPL", "100", "190.005", "100.00"),
    )
    assert result.clean
