# app/engine.py
"""Engine. v1: owns orders + positions, submits via the broker interface, applies fills.

The engine talks ONLY to the Broker adapter interface (app.brokers.base), never a
concrete broker. It holds the order book and the position/P&L state and is the single
place fills are folded in. All broker calls are async, matching the adapter surface.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional

from app import orders
from app.brokers.base import Broker, OrderAck   # interface only, never a concrete broker
from app.config import Config
from app.idempotency import IdempotencyRegistry, derive_key
from app.models import Fill, Order, OrderStatus, OrderType, Side
from app.pnl import PnlEngine


class Engine:
    def __init__(self, broker: Broker, config: Config) -> None:
        self._broker = broker
        self._config = config
        self._orders: Dict[str, Order] = {}
        self._pnl = PnlEngine(config)
        self._idem = IdempotencyRegistry()
        self._seen_execs: set[str] = set()   # broker_exec_id dedupe for the fill stream

    # --- orders ---------------------------------------------------------------
    async def submit(self, symbol: str, side: Side, qty: Decimal,
                     order_type: OrderType = OrderType.MARKET,
                     limit_price: Optional[Decimal] = None, nonce: str = "") -> Order:
        key = derive_key(symbol, side.value, qty, order_type.value, limit_price, nonce)
        existing = self._idem.get(key)
        if existing is not None:
            # Duplicate submit: return the order we already have, do not re-send.
            return self._orders[existing.order_id]

        order = Order(symbol=symbol, side=side, qty=qty,
                      order_type=order_type, limit_price=limit_price)
        self._idem.reserve(key, order)
        self._orders[order.order_id] = order

        ack = await self._broker.submit(order)
        self._apply_ack(order, ack)
        self._idem.mark_sent(key)
        return order

    async def cancel(self, order_id: str) -> Order:
        order = self._orders[order_id]
        ack = await self._broker.cancel(order.broker_order_id)
        self._apply_ack(order, ack)
        return order

    def _apply_ack(self, order: Order, ack: OrderAck) -> None:
        """Fold a broker ack into the order's state machine."""
        if order.broker_order_id is None:
            order.broker_order_id = ack.broker_order_id
        if order.status is OrderStatus.NEW:
            orders.mark_submitted(order, ack.broker_order_id)
        if ack.status is OrderStatus.CANCELLED:
            orders.mark_cancelled(order)

    # --- fills ----------------------------------------------------------------
    def on_fill(self, fill: Fill) -> None:
        if fill.broker_exec_id and fill.broker_exec_id in self._seen_execs:
            return  # at-least-once stream replay; already applied
        if fill.broker_exec_id:
            self._seen_execs.add(fill.broker_exec_id)
        order = self._orders[fill.order_id]
        orders.apply_fill(order, fill)
        self._pnl.apply(fill)

    # --- reads ----------------------------------------------------------------
    def get_order(self, order_id: str) -> Order:
        return self._orders[order_id]

    def pnl_report(self):
        return self._pnl.report()
