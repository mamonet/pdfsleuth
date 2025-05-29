# app/orders.py
"""Order state machine. final: confirmed-terminal handling.

DEFECT in v3: mark_cancelled/mark_rejected flipped the order to a terminal state on
the strength of a local event alone. If we assumed CANCELLED after firing a cancel
and the connection then dropped before the venue confirmed, the order could still be
live at the venue, keep filling, and strand us in a terminal state we made up. The
reconciler would then compare against a position that never stopped moving.

FIX: separate LOCAL intent from BROKER-CONFIRMED truth. A terminal state is only
trusted (terminal_confirmed=True) once query_order returns it from the broker. Until
then the order sits in a pending-terminal state that still accepts late fills and
still gets re-queried. confirm_terminal() is the only path that sets the trusted flag.
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
    # Terminal states keep a self-edge so a broker re-confirmation is not an illegal move.
    OrderStatus.FILLED: frozenset({OrderStatus.FILLED}),
    OrderStatus.CANCELLED: frozenset({OrderStatus.CANCELLED, OrderStatus.FILLED}),
    OrderStatus.REJECTED: frozenset({OrderStatus.REJECTED}),
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
    """Accumulate a fill. Accepts late fills even against a locally-assumed terminal
    state, as long as that state is not yet broker-confirmed. This is exactly the
    dropped-cancel case the fix exists for.
    """
    if fill.order_id != order.order_id:
        raise ValueError(f"fill {fill.fill_id} is for a different order")
    if order.terminal_confirmed:
        # Broker has confirmed done. A fill after that is a genuine anomaly.
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

    # From a locally-assumed CANCELLED, a late fill re-opens the order. Route it back
    # through PARTIALLY_FILLED/FILLED rather than trusting the stale terminal.
    if order.status is OrderStatus.CANCELLED:
        order.status = OrderStatus.FILLED if order.is_complete else OrderStatus.PARTIALLY_FILLED
        order.touch()
        return order

    target = OrderStatus.FILLED if order.is_complete else OrderStatus.PARTIALLY_FILLED
    transition(order, target)
    return order


def mark_cancelled(order: Order, reason: Optional[str] = None, confirmed: bool = False) -> Order:
    """Request/observe a cancel. confirmed=False means WE assumed it (fired the cancel,
    lost the connection); the state is provisional and stays re-queryable. confirmed=True
    means the broker reported it. Only the latter is trusted as final.
    """
    if order.status is OrderStatus.FILLED and order.terminal_confirmed:
        return order
    if order.status is not OrderStatus.CANCELLED:
        transition(order, OrderStatus.CANCELLED)
    if reason:
        order.reject_reason = reason
    if confirmed:
        order.terminal_confirmed = True
    return order


def mark_rejected(order: Order, reason: str, confirmed: bool = True) -> Order:
    """Reject. A reject only ever comes FROM the broker, so it is confirmed by default."""
    if order.terminal_confirmed and order.status is OrderStatus.REJECTED:
        return order
    if order.filled_qty > ZERO:
        raise IllegalTransition(order.order_id, order.status, OrderStatus.REJECTED)
    if order.status is not OrderStatus.REJECTED:
        transition(order, OrderStatus.REJECTED)
    order.reject_reason = reason
    order.terminal_confirmed = confirmed
    return order


def confirm_terminal(order: Order, broker_status: OrderStatus) -> Order:
    """Apply a status straight from query_order and mark it trusted.

    This is the ONLY function that sets terminal_confirmed on a fill/cancel. If the
    broker says the order is still open, we drop any local terminal assumption.
    """
    if broker_status.is_open:
        # Broker disagrees with our assumed-terminal; believe the broker.
        if order.status.is_terminal and not order.terminal_confirmed:
            order.status = broker_status
            order.terminal_confirmed = False
            order.touch()
        return order

    if order.status != broker_status:
        # Force to the broker's terminal even if our local edge would not allow it;
        # the broker is authoritative here.
        order.status = broker_status
        order.touch()
    order.terminal_confirmed = True
    return order


def is_terminal(order: Order) -> bool:
    """Trusted-terminal only. A locally-assumed terminal is NOT terminal yet."""
    return order.status in TERMINAL and order.terminal_confirmed


def needs_confirmation(order: Order) -> bool:
    """True when the order looks terminal locally but the broker has not confirmed."""
    return order.status in TERMINAL and not order.terminal_confirmed
