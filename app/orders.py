# app/orders.py
"""Order state machine. v2: partial fills accumulate.

v1 could set FILLED/PARTIALLY_FILLED but nothing tracked how much was actually
done, so two 50-share partials on a 100-share order looked the same as one.
v2 applies fills to the order: filled_qty accumulates, avg_fill_price is a
qty-weighted running mean, and the status is DERIVED from filled_qty rather
than set by hand. Stays PARTIALLY_FILLED until remaining hits zero.
"""

from __future__ import annotations

from typing import Dict, FrozenSet

from app.models import Fill, Order, OrderStatus, ZERO

LEGAL_TRANSITIONS: Dict[OrderStatus, FrozenSet[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED}),
    OrderStatus.SUBMITTED: frozenset({
        OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCELLED, OrderStatus.REJECTED,
    }),
    OrderStatus.WORKING: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCELLED, OrderStatus.REJECTED,
    }),
    OrderStatus.PARTIALLY_FILLED: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED,
    }),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class IllegalTransition(Exception):
    def __init__(self, order_id: str, current: OrderStatus, requested: OrderStatus):
        self.order_id = order_id
        self.current = current
        self.requested = requested
        super().__init__(
            f"order {order_id}: illegal transition {current.value} -> {requested.value}"
        )


class OverFill(Exception):
    """A fill would push filled_qty past the ordered qty. The ledger is corrupt if so."""


def can_transition(current: OrderStatus, requested: OrderStatus) -> bool:
    return requested in LEGAL_TRANSITIONS.get(current, frozenset())


def transition(order: Order, requested: OrderStatus) -> Order:
    if not can_transition(order.status, requested):
        raise IllegalTransition(order.order_id, order.status, requested)
    order.status = requested
    order.touch()
    return order


def mark_submitted(order: Order, broker_order_id: str) -> Order:
    transition(order, OrderStatus.SUBMITTED)
    order.broker_order_id = broker_order_id
    return order


def mark_working(order: Order) -> Order:
    return transition(order, OrderStatus.WORKING)


def apply_fill(order: Order, fill: Fill) -> Order:
    """Accumulate a fill and derive the resulting status.

    Status is a function of quantity, never asserted directly: partial while
    some remains, filled once none does.
    """
    if fill.order_id != order.order_id:
        raise ValueError(f"fill {fill.fill_id} is for a different order")

    new_filled = order.filled_qty + fill.qty
    if new_filled > order.qty:
        raise OverFill(
            f"order {order.order_id}: fill {fill.qty} over remaining {order.remaining_qty}"
        )

    # qty-weighted average execution price
    prior_notional = order.avg_fill_price * order.filled_qty
    order.avg_fill_price = (prior_notional + fill.price * fill.qty) / new_filled
    order.filled_qty = new_filled
    order.fees += fill.fee

    target = OrderStatus.FILLED if order.is_complete else OrderStatus.PARTIALLY_FILLED
    # Guard the edge even though we derived the target.
    transition(order, target)
    return order


def _derived_status(order: Order) -> OrderStatus:
    if order.filled_qty == ZERO:
        return order.status
    return OrderStatus.FILLED if order.is_complete else OrderStatus.PARTIALLY_FILLED
