# tests/test_api.py
"""API surface via TestClient with a stub engine on app.state, so no broker, feed, or
database is touched. Covers /health, /positions, /reconcile shape, and POST /orders."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.brokers.base import PnlSnapshot
from app.config import ReconcileConfig
from app.main import app
from app.models import BrokerPosition, Order, OrderStatus, OrderType, Position, Side
from app.pnl import PnlLine, PnlReport
from app.reconcile import Reconciler


class StubEngine:
    """Minimal facade shape the HTTP layer depends on."""

    def __init__(self):
        self._positions = [Position(symbol="AAPL", qty=Decimal("100"),
                                    avg_cost=Decimal("190.00"))]
        self._orders = [Order(symbol="AAPL", side=Side.BUY, qty=Decimal("100"),
                              client_order_id="cid-1", status=OrderStatus.FILLED,
                              filled_qty=Decimal("100"), avg_fill_price=Decimal("190.00"),
                              terminal_confirmed=True)]

    def list_positions(self):
        return self._positions

    def list_orders(self):
        return self._orders

    async def reconcile(self):
        pnl = PnlReport([PnlLine("AAPL", Decimal("100"), Decimal("190.00"),
                                 Decimal("190.00"), "FEED_MID", False,
                                 realized=Decimal("0"), unrealized=Decimal("0"))],
                        mark_source="FEED_MID")
        snap = PnlSnapshot(realized_pnl=Decimal("0"), unrealized_pnl=Decimal("0"),
                           positions=[BrokerPosition("AAPL", Decimal("100"),
                                                     Decimal("190.00"))])
        return Reconciler(ReconcileConfig()).reconcile(
            positions=self._positions, pnl=pnl, broker=snap)

    async def submit(self, symbol, side, qty, order_type, limit_price):
        order = Order(symbol=symbol, side=side, qty=qty, order_type=order_type,
                      limit_price=limit_price, status=OrderStatus.FILLED,
                      filled_qty=qty, avg_fill_price=Decimal("190.00"))
        self._orders.append(order)
        return order


def _client():
    app.state.engine = StubEngine()   # preset so lifespan skips building the real engine
    return TestClient(app)


def test_health():
    with _client() as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_positions_shape():
    with _client() as client:
        r = client.get("/positions")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["symbol"] == "AAPL"
    assert body[0]["qty"] == "100"            # Decimal serialised as string
    assert body[0]["avg_cost"] == "190.00"


def test_reconcile_shape_clean():
    with _client() as client:
        r = client.get("/reconcile")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"clean", "has_breaks", "breaks", "mark_source"}
    assert body["clean"] is True
    assert body["has_breaks"] is False
    assert body["breaks"] == []


def test_submit_order():
    with _client() as client:
        r = client.post("/orders", json={"symbol": "AAPL", "side": "BUY", "qty": "50"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "AAPL"
    assert body["status"] == "FILLED"
    assert body["filled_qty"] == "50"


def test_submit_order_rejects_bad_qty():
    with _client() as client:
        r = client.post("/orders", json={"symbol": "AAPL", "side": "BUY", "qty": "-5"})
    assert r.status_code == 422
