# app/orders.py
"""Order state machine. v1: legal transitions only.

The whole point is that an order's status can only move along edges we declared.
Anything else raises instead of silently mutating, because a status that went
somewhere impossible means we have already lost track of the broker's truth.
"""

from __future__ import annotations

from typing import Dict, FrozenSet

from app.models import Order, OrderStatus

# Adjacency for the status graph. Absent key == no outbound edges (terminal).
LEGAL_TRANSITIONS: Dict[OrderStatus, FrozenSet[OrderStatus]] = {
    OrderStatus.NEW: frozenset({
        OrderStatus.SUBMITTED,
        OrderStatus.REJECTED,     # pre-trade risk can reject before it ever leaves
    }),
    OrderStatus.SUBMITTED: frozenset({
        OrderStatus.WORKING,
        OrderStatus.PARTIALLY_FILLED,   # a fast venue can fill before the ack lands
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    }),
    OrderStatus.WORKING: frozenset({
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    }),
    OrderStatus.PARTIALLY_FILLED: frozenset({
        OrderStatus.PARTIALLY_FILLED,   # each additional partial re-enters the state
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,          # cancelled with a partial already done
    }),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class IllegalTransition(Exception):
    """Raised when something tries to move an order along an edge that does not exist."""

    def __init__(self, order_id: str, current: OrderStatus, requested: OrderStatus):
        self.order_id = order_id
        self.current = current
        self.requested = requested
        super().__init__(
            f"order {order_id}: illegal transition {current.value} -> {requested.value}"
        )


def can_transition(current: OrderStatus, requested: OrderStatus) -> bool:
    return requested in LEGAL_TRANSITIONS.get(current, frozenset())


def transition(order: Order, requested: OrderStatus) -> Order:
    """Move the order, or raise. Mutates in place and returns it for chaining."""
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


def mark_filled(order: Order) -> Order:
    return transition(order, OrderStatus.FILLED)
