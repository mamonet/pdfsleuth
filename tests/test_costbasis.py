# tests/test_costbasis.py
"""Cost-basis strategies on a KNOWN fill sequence, plus fee application through the P&L
engine. Expected values are derived by hand in the comments.

Sequence (one symbol):
    BUY  100 @ 10.00  fee 1.00
    BUY  100 @ 12.00  fee 1.00
    SELL 120 @ 15.00  fee 1.20

AVERAGE:
    avg after two buys = (100*10.00 + 100*12.00) / 200 = 11.00
    realized on sell   = (15.00 - 11.00) * 120 = 480.00   (gross, before fees)
    remaining qty      = 80  (still @ 11.00)

FIFO (oldest lots first):
    100 @ 10.00 -> (15.00 - 10.00) * 100 = 500.00
     20 @ 12.00 -> (15.00 - 12.00) *  20 =  60.00
    realized on sell   = 560.00   (gross, before fees)
    remaining qty      = 80  (from the 12.00 lot)

Fees total = 1.00 + 1.00 + 1.20 = 3.20. The cost-basis strategies are pre-fee; the P&L
engine deducts fees from realized, so engine realized (AVERAGE) = 480.00 - 3.20 = 476.80.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import Config
from app.costbasis import AverageCostBasis, FifoCostBasis
from app.models import Fill, Side
from app.pnl import PnlEngine

SYMBOL = "X"


def _fill(side, qty, price, fee):
    return Fill(order_id="o1", symbol=SYMBOL, side=side, qty=Decimal(qty),
                price=Decimal(price), fee=Decimal(fee))


SEQUENCE = [
    _fill(Side.BUY, "100", "10.00", "1.00"),
    _fill(Side.BUY, "100", "12.00", "1.00"),
    _fill(Side.SELL, "120", "15.00", "1.20"),
]


def test_average_cost_basis():
    cb = AverageCostBasis(SYMBOL)
    results = [cb.apply(f) for f in SEQUENCE]
    sell = results[-1]
    assert sell.realized_pnl == Decimal("480.00")
    assert cb.realized_pnl == Decimal("480.00")
    assert cb.qty == Decimal("80")
    assert cb.avg_cost == Decimal("11.00")


def test_fifo_cost_basis():
    cb = FifoCostBasis(SYMBOL)
    results = [cb.apply(f) for f in SEQUENCE]
    sell = results[-1]
    assert sell.realized_pnl == Decimal("560.00")
    assert cb.realized_pnl == Decimal("560.00")
    assert cb.qty == Decimal("80")


def test_methods_agree_on_qty_disagree_on_realized():
    avg = AverageCostBasis(SYMBOL)
    fifo = FifoCostBasis(SYMBOL)
    for f in SEQUENCE:
        avg.apply(f)
        fifo.apply(f)
    assert avg.qty == fifo.qty == Decimal("80")
    assert fifo.realized_pnl - avg.realized_pnl == Decimal("80.00")   # 560 - 480


def test_fees_reduce_realized_in_pnl_engine():
    # AVERAGE gross realized is 480.00; the engine nets the 3.20 of fees.
    engine = PnlEngine(Config())      # default cost basis is AVERAGE
    for f in SEQUENCE:
        engine.apply(f)
    report = engine.report()
    assert report.realized == Decimal("476.80")
    assert report.fees == Decimal("3.20")
