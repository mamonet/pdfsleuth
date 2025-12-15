# tests/test_store.py
"""Persistence: state survives a simulated restart, and a failed write leaves no
half-applied fill (the fill/order/position land together or not at all)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import Fill, Order, OrderStatus, Position, Side
from app.store import Store


def _order(symbol="AAPL", qty="100", status=OrderStatus.PARTIALLY_FILLED):
    return Order(symbol=symbol, side=Side.BUY, qty=Decimal(qty),
                 client_order_id=f"cid-{symbol}", status=status,
                 filled_qty=Decimal("40"), avg_fill_price=Decimal("190.00"))


def _fill(order, qty="40", exec_id="EX-1"):
    return Fill(order_id=order.order_id, symbol=order.symbol, side=Side.BUY,
                qty=Decimal(qty), price=Decimal("190.00"), fee=Decimal("0.20"),
                broker_exec_id=exec_id)


def _position(symbol="AAPL", qty="40"):
    return Position(symbol=symbol, qty=Decimal(qty), avg_cost=Decimal("190.00"),
                    fees_paid=Decimal("0.20"))


def test_state_survives_restart(tmp_path):
    db = str(tmp_path / "state.db")

    store = Store(db).open().init()
    order = _order()
    store.apply_fill(_fill(order), order, _position())
    store.close()

    # "restart": brand-new Store over the same file
    store2 = Store(db).open().init()
    orders = store2.load_orders()
    fills = store2.load_fills()
    positions = store2.load_positions()
    store2.close()

    assert len(orders) == 1
    assert orders[0].order_id == order.order_id
    assert orders[0].filled_qty == Decimal("40")
    assert len(fills) == 1
    assert fills[0].qty == Decimal("40")
    assert positions[0].qty == Decimal("40")
    # Decimal round-trips exactly, not as float
    assert isinstance(positions[0].avg_cost, Decimal)
    assert positions[0].avg_cost == Decimal("190.00")


def test_duplicate_exec_id_is_ignored(tmp_path):
    db = str(tmp_path / "dupe.db")
    store = Store(db).open().init()
    order = _order()
    assert store.apply_fill(_fill(order, exec_id="EX-dup"), order, _position()) is True
    # replayed stream: same broker_exec_id must not double-apply
    assert store.apply_fill(_fill(order, exec_id="EX-dup"), order, _position()) is False
    assert len(store.load_fills()) == 1
    store.close()


def test_failed_write_leaves_no_half_applied_fill(tmp_path, monkeypatch):
    db = str(tmp_path / "atomic.db")
    store = Store(db).open().init()
    order = _order()

    # Blow up after the fill insert but before the transaction commits.
    def boom(cur, position):
        raise RuntimeError("disk gave up mid-write")

    monkeypatch.setattr(Store, "_write_position", staticmethod(boom))

    with pytest.raises(RuntimeError):
        store.apply_fill(_fill(order), order, _position())

    # Rolled back: no fill, no order, no position persisted.
    assert store.load_fills() == []
    assert store.load_orders() == []
    assert store.load_positions() == []
    store.close()
