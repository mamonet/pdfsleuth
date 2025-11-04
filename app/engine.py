# app/engine.py
"""Engine. final: reconcile on every fill and every broker snapshot.

v3 persisted state but never checked it against the broker, so drift (a missed fill, a
fee costed differently, a mark-source gap) could accumulate silently. final wires in
the Reconciler: after each fill it pulls the broker's pnl_snapshot and compares, and it
exposes on_broker_snapshot() to reconcile against a pushed/polled broker view. Anything
past tolerance is surfaced as a break located to the exact symbol.

Reconciliation also confirms terminal order states: a query_order ack flows through
orders.confirm_terminal, so a locally-assumed CANCELLED/FILLED is only trusted once the
broker agrees (the confirmed-terminal guarantee from orders.py). A cancel therefore
records a provisional CANCELLED and only confirm_order() makes it final.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable, Dict, Optional

from app import orders
from app.brokers.base import Broker, OrderAck, PnlSnapshot
from app.config import Config
from app.idempotency import IdempotencyRegistry, derive_key
from app.models import Fill, Order, OrderStatus, OrderType, Side, ZERO
from app.pnl import PnlEngine
from app.positions import PositionBook
from app.reconcile import Reconciler, ReconcileResult
from app.store import Store


class OversellError(Exception):
    def __init__(self, symbol: str, held: Decimal, requested: Decimal):
        self.symbol = symbol
        self.held = held
        self.requested = requested
        super().__init__(f"{symbol}: sell {requested} exceeds available {held}")


class Engine:
    def __init__(self, broker: Broker, config: Config, store: Store,
                 on_break: Optional[Callable[[ReconcileResult], None]] = None) -> None:
        self._broker = broker
        self._config = config
        self._store = store
        self._orders: Dict[str, Order] = {}
        self._pnl = PnlEngine(config)
        self._positions = PositionBook()
        self._idem = IdempotencyRegistry()
        self._reconciler = Reconciler(config.reconcile)
        self._on_break = on_break
        self._last_result: Optional[ReconcileResult] = None
        self._seen_execs: set[str] = set()

    def load(self) -> None:
        for order in self._store.load_orders():
            self._orders[order.order_id] = order
            if order.client_order_id:
                self._idem.reserve(order.client_order_id, order)
                self._idem.mark_sent(order.client_order_id)
        for fill in self._store.load_fills():
            if fill.broker_exec_id:
                self._seen_execs.add(fill.broker_exec_id)
            self._positions.apply(fill)
            self._pnl.apply(fill)

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
        self._store.upsert_order(order)

        ack = await self._broker.submit(order)
        self._apply_ack(order, ack)
        self._idem.mark_sent(key)
        self._store.upsert_order(order)
        return order

    async def cancel(self, order_id: str) -> Order:
        order = self._orders[order_id]
        ack = await self._broker.cancel(order.broker_order_id)
        # Provisional CANCELLED, not trusted until confirm_order() (see orders.final).
        if ack.status is OrderStatus.CANCELLED:
            orders.mark_cancelled(order, confirmed=False)
        self._store.upsert_order(order)
        return order

    async def confirm_order(self, order_id: str) -> Order:
        """Query the broker for the true status and trust its terminal verdict."""
        order = self._orders[order_id]
        ack = await self._broker.query_order(order.broker_order_id)
        orders.confirm_terminal(order, ack.status)
        self._store.upsert_order(order)
        return order

    def _apply_ack(self, order: Order, ack: OrderAck) -> None:
        if order.broker_order_id is None:
            order.broker_order_id = ack.broker_order_id
        if order.status is OrderStatus.NEW:
            orders.mark_submitted(order, ack.broker_order_id)
        if ack.status is OrderStatus.CANCELLED:
            orders.mark_cancelled(order, confirmed=False)

    async def on_fill(self, fill: Fill) -> Optional[ReconcileResult]:
        if fill.broker_exec_id and fill.broker_exec_id in self._seen_execs:
            return None
        if fill.broker_exec_id:
            self._seen_execs.add(fill.broker_exec_id)
        order = self._orders[fill.order_id]
        orders.apply_fill(order, fill)
        self._positions.apply(fill)
        self._pnl.apply(fill)
        self._store.record_fill(fill)
        self._store.upsert_order(order)
        self._store.upsert_position(self._positions.get(fill.symbol))
        # Reconcile against the broker on every fill.
        snapshot = await self._broker.pnl_snapshot()
        return self._reconcile(snapshot)

    def on_broker_snapshot(self, snapshot: PnlSnapshot) -> ReconcileResult:
        """Reconcile our book against a broker snapshot (pushed or polled)."""
        return self._reconcile(snapshot)

    def _reconcile(self, snapshot: PnlSnapshot) -> ReconcileResult:
        result = self._reconciler.reconcile(
            positions=list(self._positions.all()),
            pnl=self._pnl.report(),
            broker=snapshot,
        )
        self._last_result = result
        if result.has_breaks and self._on_break is not None:
            self._on_break(result)
        return result

    def get_order(self, order_id: str) -> Order:
        return self._orders[order_id]

    def pnl_report(self):
        return self._pnl.report()

    @property
    def last_reconcile(self) -> Optional[ReconcileResult]:
        return self._last_result
