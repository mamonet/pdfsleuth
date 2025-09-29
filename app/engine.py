# app/engine.py
"""Engine. v2: oversell guard.

v1 would happily submit a sell larger than the position held, letting the book go
short by accident. v2 tracks net qty per symbol and rejects a sell whose size exceeds
the current long position BEFORE it reaches the broker. Open sell orders reserve their
qty so two sells cannot each pass the guard against the same shares. (Deliberate
shorting would be an explicit config, not this accidental path.)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional

from app import orders
from app.brokers.base import Broker, OrderAck
from app.config import Config
from app.idempotency import IdempotencyRegistry, derive_key
from app.models import Fill, Order, OrderStatus, OrderType, Side, ZERO
from app.pnl import PnlEngine
from app.positions import PositionBook


class OversellError(Exception):
    def __init__(self, symbol: str, held: Decimal, requested: Decimal):
        self.symbol = symbol
        self.held = held
        self.requested = requested
        super().__init__(f"{symbol}: sell {requested} exceeds available {held}")


class Engine:
    def __init__(self, broker: Broker, config: Config) -> None:
        self._broker = broker
        self._config = config
        self._orders: Dict[str, Order] = {}
        self._pnl = PnlEngine(config)
        self._positions = PositionBook()
        self._idem = IdempotencyRegistry()
        self._seen_execs: set[str] = set()

    def _held(self, symbol: str) -> Decimal:
        return self._positions.get(symbol).qty

    def _reserved_sells(self, symbol: str) -> Decimal:
        total = ZERO
        for o in self._orders.values():
            if o.symbol == symbol and o.side is Side.SELL and o.status.is_open:
                total += o.remaining_qty
        return total

    async def submit(self, symbol: str, side: Side, qty: Decimal,
                     order_type: OrderType = OrderType.MARKET,
                     limit_price: Optional[Decimal] = None, nonce: str = "") -> Order:
        if side is Side.SELL:
            available = self._held(symbol) - self._reserved_sells(symbol)
            if qty > available:
                raise OversellError(symbol, available, qty)

        key = derive_key(symbol, side.value, qty, order_type.value, limit_price, nonce)
        existing = self._idem.get(key)
        if existing is not None:
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
        if order.broker_order_id is None:
            order.broker_order_id = ack.broker_order_id
        if order.status is OrderStatus.NEW:
            orders.mark_submitted(order, ack.broker_order_id)
        if ack.status is OrderStatus.CANCELLED:
            orders.mark_cancelled(order)

    def on_fill(self, fill: Fill) -> None:
        if fill.broker_exec_id and fill.broker_exec_id in self._seen_execs:
            return
        if fill.broker_exec_id:
            self._seen_execs.add(fill.broker_exec_id)
        order = self._orders[fill.order_id]
        orders.apply_fill(order, fill)
        self._positions.apply(fill)
        self._pnl.apply(fill)

    def get_order(self, order_id: str) -> Order:
        return self._orders[order_id]

    def pnl_report(self):
        return self._pnl.report()
