# app/orders.py
"""Order state machine. v3: cancel and reject terminal paths.

v2 handled the happy path but had no way to actually cancel or reject an order,
and no notion of a cancel that races a fill. v3 adds:
  - mark_cancelled / mark_rejected with the reason recorded
  - a partially-filled cancel keeps the filled qty, it does not unwind it
  - a cancel that arrives after the order already completed is a no-op, not an error,
    because the venue and our stream can legitimately cross
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Optional

from app.models import Fill, Order, OrderStatus, ZERO

LEGAL_TRANSITIONS: Dict[OrderStatus, FrozenSet[OrderStatus]] = {
    OrderStatus.NEW: frozenset({
        OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELLED,
    }),
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

TERMINAL = (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


class IllegalTransition(Exception):
    def __init__(self, order_id: str, current: OrderStatus, requested: OrderStatus):
        self.order_id = order_id
        self.current = current
        self.requested = requested
        super().__init__(
            f"order {order_id}: illegal transition {current.value} -> {requested.value}"
        )


class OverFill(Exception):
    """A fill would push filled_qty past the ordered qty."""


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
    if fill.order_id != order.order_id:
        raise ValueError(f"fill {fill.fill_id} is for a different order")
    if order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
        # A fill against a cancelled order means the cancel lost the race at the venue.
        # Refusing it here would drop a real execution, so surface it loudly instead.
        raise IllegalTransition(order.order_id, order.status, OrderStatus.PARTIALLY_FILLED)

    new_filled = order.filled_qty + fill.qty
    if new_filled > order.qty:
        raise OverFill(
            f"order {order.order_id}: fill {fill.qty} over remaining {order.remaining_qty}"
        )

    prior_notional = order.avg_fill_price * order.filled_qty
    order.avg_fill_price = (prior_notional + fill.price * fill.qty) / new_filled
    order.filled_qty = new_filled
    order.fees += fill.fee

    target = OrderStatus.FILLED if order.is_complete else OrderStatus.PARTIALLY_FILLED
    transition(order, target)
    return order


def mark_cancelled(order: Order, reason: Optional[str] = None) -> Order:
    """Cancel. Any qty already filled stays filled; only the remainder dies.

    Idempotent against a completed order: if it filled first, the cancel simply lost.
    """
    if order.status is OrderStatus.FILLED:
        return order  # cancel raced a complete fill and lost
    if order.status is OrderStatus.CANCELLED:
        return order  # duplicate cancel ack
    transition(order, OrderStatus.CANCELLED)
    if reason:
        order.reject_reason = reason
    return order


def mark_rejected(order: Order, reason: str) -> Order:
    """Reject is terminal and carries a reason. Never retried (see retry.py)."""
    if order.status is OrderStatus.REJECTED:
        return order
    if order.filled_qty > ZERO:
        # A venue does not reject an order it has already partially executed.
        raise IllegalTransition(order.order_id, order.status, OrderStatus.REJECTED)
    transition(order, OrderStatus.REJECTED)
    order.reject_reason = reason
    return order


def is_terminal(order: Order) -> bool:
    return order.status in TERMINAL
